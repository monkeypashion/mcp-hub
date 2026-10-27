"""The lane doorbell (v2-fleet F54): an idle lane that opted in is rung for
news, never for a low message, by a helper its own session starts.

Named apart from the edge-watch doorbell (doorbell.py). Run as
`mcp-hub lane-doorbell --from-session-start` from an async SessionStart hook:

  * PER-LANE OPT-IN. Nothing starts unless the lane wrote its record with
    `mcp-hub lane-doorbell opt-in` (removed by `opt-out`; default off).
  * STARTED ATTACHED. The loop runs in the hook's own process — never setsid,
    nohup, a fork or a unit — so it stays a live descendant of the session
    (a bypass-permissions lane does not hold its nudge) and inherits the
    session's inbox socket. One helper per session (a pidfile); it ends when
    the socket goes.
  * IT RINGS ONLY FOR NEWS THAT MAY INTERRUPT. News is read with the hub's
    `unread_ids` (ids and priorities, nothing marked read) on ONE kept
    connection presenting the lane's key. A body-less nudge goes into the
    inbox socket when the session is idle (or shell), the lane is not in
    🔕 focus, and an unread id of priority normal or urgent has not been rung
    for. Never for a low-only inbox, never while busy (it rings at the next
    idle reading if the id is still unread), never twice for one id.
  * It reads nothing from the session file but `.status`, and the socket path
    never comes from that file. Logs carry ids, names and times only — never
    a body, the session file's content or the messaging token.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import pathlib
import re
import socket
import sys
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

logger = logging.getLogger(__name__)

SOCKET_ENV = "CLAUDE_CODE_MESSAGING_SOCKET"
TOKEN_ENV = "CLAUDE_CODE_MESSAGING_TOKEN"

# Seconds between polls. Each poll is one local file read and, only when the
# session is idle, one small hub read.
POLL_SECONDS = 2.0
# Every hub step (connect + initialize, each call) is bounded: a hub that
# accepts and drops the connection must not hang the loop past the session.
HUB_TIMEOUT_SECONDS = 8.0
# Socket write bound for the nudge.
NUDGE_TIMEOUT_SECONDS = 2.0

IDLE_STATUSES = frozenset({"idle", "shell"})
RINGING_PRIORITIES = frozenset({"normal", "urgent"})

# Fixed text: names no message, sender or body.
NUDGE_TEXT = (
    "📬 hub: new messages are waiting for this lane. "
    "Read them with get_messages."
)

_PID_RE = re.compile(r"[0-9]+")


# ---------------------------------------------------------------------------
# Seams
# ---------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def open_hub(hub_url: str, headers: dict[str, str] | None):
    """One hub connection, initialized, yielding the ClientSession."""
    import anyio
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async with streamablehttp_client(
        hub_url, headers=headers or None, timeout=HUB_TIMEOUT_SECONDS,
    ) as (read, write, _):
        async with ClientSession(read, write) as session:
            with anyio.fail_after(HUB_TIMEOUT_SECONDS):
                await session.initialize()
            yield session


def session_status(mapping: Any) -> str | None:
    """The session file's status, reading no other key; None if absent."""
    if not isinstance(mapping, Mapping):
        return None
    try:
        value = mapping["status"]
    except (KeyError, TypeError):
        return None
    return value if isinstance(value, str) else None


def session_pid(socket_path: str | os.PathLike[str]) -> str | None:
    """The session's pid: the socket's basename up to its first '.', digits
    only (12345.sock is 12345). None for any other name."""
    head = os.path.basename(os.fspath(socket_path)).split(".", 1)[0]
    return head if _PID_RE.fullmatch(head) else None


def claude_sessions_dir() -> pathlib.Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    root = pathlib.Path(base) if base else pathlib.Path.home() / ".claude"
    return root / "sessions"


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _read_status(session_file: pathlib.Path) -> tuple[str | None, str]:
    """(status, why-not). Only .status is taken from the parsed file."""
    try:
        data = json.loads(pathlib.Path(session_file).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, "session file missing"
    except (OSError, ValueError):
        return None, "session file unreadable"
    status = session_status(data)
    if status is None:
        return None, "session file has no readable status"
    return status, ""


def _in_focus(lane: str) -> bool:
    """The lane's 🔕 focus expiry, from the heartbeat's status snapshot."""
    from mcp_hub import cli

    try:
        snap = json.loads(cli._status_cache_path(lane).read_text(encoding="utf-8"))
        until = float(snap.get("focus_until") or 0)
    except (OSError, ValueError, TypeError, AttributeError):
        return False
    return until > time.time()


def _result_text(result: Any) -> str:
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            return text
    return result if isinstance(result, str) else ""


def _parse_unread(text: str) -> list[tuple[int, str]] | None:
    try:
        out = json.loads(text)
        return [(int(u["id"]), str(u["priority"])) for u in out["unread"]]
    except (ValueError, TypeError, KeyError):
        return None


def send_nudge(socket_path: str | os.PathLike[str]) -> bool:
    """One body-less nudge into the session's inbox socket: connect, two JSON
    lines, close. The token is never logged."""
    token = os.environ.get(TOKEN_ENV)
    if not token:
        logger.warning("lane-doorbell: no %s in the environment; not ringing", TOKEN_ENV)
        return False
    payload = (
        json.dumps({"type": "auth", "token": token}) + "\n"
        + json.dumps({"type": "user",
                      "message": {"role": "user", "content": NUDGE_TEXT}}) + "\n"
    ).encode("utf-8")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(NUDGE_TIMEOUT_SECONDS)
            s.connect(os.fspath(socket_path))
            s.sendall(payload)
    except OSError as exc:
        logger.warning("lane-doorbell: nudge failed (%s)", type(exc).__name__)
        return False
    return True


async def _default_sleep() -> None:
    await asyncio.sleep(POLL_SECONDS)


async def run(
    lane: str,
    *,
    hub_url: str,
    session_file: str | os.PathLike[str],
    socket_path: str | os.PathLike[str],
    sleep: Callable[[], Awaitable[Any]] = _default_sleep,
) -> int:
    """Poll until the session's socket goes; return 0 then."""
    import anyio

    from mcp_hub import cli

    session_file = pathlib.Path(session_file)
    headers = cli._agent_key_headers(lane)
    rung: set[int] = set()
    last_note = ""

    def note(msg: str, *a: Any) -> None:
        # Repeated states are logged once, not every poll.
        nonlocal last_note
        text = msg % a
        if text != last_note:
            logger.info("lane-doorbell %s %s: %s", _now(), lane, text)
            last_note = text

    def alive() -> bool:
        return os.path.exists(socket_path)

    logger.info("lane-doorbell %s %s: watching (key %s)", _now(), lane,
                "presented" if headers else "none held")
    while alive():
        try:
            async with open_hub(hub_url, headers) as hub:
                while True:
                    status, why = _read_status(session_file)
                    if status is None:
                        note("%s; ringing nothing", why)
                    elif status not in IDLE_STATUSES:
                        note("status %s; waiting", status)
                    elif _in_focus(lane):
                        note("in focus; not ringing")
                    else:
                        with anyio.fail_after(HUB_TIMEOUT_SECONDS):
                            result = await hub.call_tool(
                                "unread_ids", {"agent_name": lane})
                        unread = _parse_unread(_result_text(result))
                        if unread is None:
                            note("unread_ids gave no answer; ringing nothing")
                        else:
                            current = {i for i, _p in unread}
                            rung &= current
                            due = [i for i, p in unread
                                   if p in RINGING_PRIORITIES and i not in rung]
                            if due:
                                if send_nudge(socket_path):
                                    rung.update(due)
                                    logger.info(
                                        "lane-doorbell %s %s: rang for ids %s",
                                        _now(), lane, due)
                            else:
                                note("idle, nothing to ring for (%d unread)",
                                     len(unread))
                    if not alive():
                        break
                    await sleep()
                    if not alive():
                        break
        except Exception as exc:  # noqa: BLE001 - reconnect after any error
            logger.warning("lane-doorbell %s %s: hub error (%s); reconnecting",
                           _now(), lane, type(exc).__name__)
            if alive():
                await sleep()
    logger.info("lane-doorbell %s %s: session socket gone; exiting", _now(), lane)
    return 0


# ---------------------------------------------------------------------------
# Opt-in records and the per-session pidfile
# ---------------------------------------------------------------------------


def _home() -> pathlib.Path:
    from mcp_hub import cli

    return cli._state_dir() / "lane-doorbell"


def _safe(name: str) -> str:
    return "".join(c if (c.isalnum() or c in "-_") else "_" for c in name)


def opt_in_path(lane: str) -> pathlib.Path:
    return _home() / "opt-in" / _safe(lane)


def opted_in(lane: str) -> bool:
    return opt_in_path(lane).is_file()


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _claim_pidfile(path: pathlib.Path) -> bool:
    """True when this process now holds the session's pidfile."""
    path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            try:
                holder = int(path.read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                holder = 0
            if holder and holder != os.getpid() and _pid_alive(holder):
                return False
            with contextlib.suppress(OSError):
                path.unlink()
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))
        return True
    return False


def _release_pidfile(path: pathlib.Path) -> None:
    try:
        if int(path.read_text(encoding="utf-8").strip()) == os.getpid():
            path.unlink()
    except (OSError, ValueError):
        pass


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def add_arguments(p: argparse.ArgumentParser, default_hub_url: str) -> None:
    p.add_argument("action", nargs="?", choices=("opt-in", "opt-out"),
                   help="Write or remove this lane's opt-in record")
    p.add_argument("--from-session-start", action="store_true",
                   help="SessionStart hook form: run attached if this lane opted in")
    p.add_argument("--name", default=None, help="Lane name (default: derived)")
    p.add_argument("--hub-url", default=default_hub_url,
                   help="Hub URL (default: $MCP_HUB_URL or built-in)")
    p.set_defaults(project=None)


def command(args: argparse.Namespace) -> int:
    from mcp_hub import cli

    payload: dict[str, Any] = {}
    if args.from_session_start and not args.name:
        payload = cli._read_hook_stdin()   # read once
    cwd = payload.get("cwd") if isinstance(payload, dict) else None
    if not cwd:
        cwd = os.getcwd()
    lane, _project = cli._resolve_agent_identity(args, {"cwd": cwd})

    if args.action in ("opt-in", "opt-out"):
        if not lane:
            print("lane-doorbell: no lane identity for this folder", file=sys.stderr)
            return 1
        path = opt_in_path(lane)
        if args.action == "opt-in":
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"lane": lane, "ts": int(time.time())}),
                            encoding="utf-8")
            print(f"lane-doorbell: {lane} opted in")
        else:
            with contextlib.suppress(FileNotFoundError):
                path.unlink()
            print(f"lane-doorbell: {lane} opted out")
        return 0

    if not args.from_session_start:
        state = "opted in" if lane and opted_in(lane) else "not opted in"
        print(f"lane-doorbell: {lane or '(no lane)'} {state}")
        return 0

    # --from-session-start: nothing at all unless this lane opted in.
    if not lane or not opted_in(lane):
        return 0
    return _run_attached(lane, cli._ephemeral_hub_url(args.hub_url))


def _run_attached(lane: str, hub_url: str) -> int:
    handler: logging.Handler | None = None
    old_level = logger.level
    try:
        log_dir = _home() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(log_dir / f"{_safe(lane)}.log", encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
        if logger.level == logging.NOTSET or logger.level > logging.INFO:
            logger.setLevel(logging.INFO)
    except OSError:
        handler = None
    try:
        sock = os.environ.get(SOCKET_ENV)
        if not sock:
            logger.info("lane-doorbell %s %s: no %s in the environment; exiting",
                        _now(), lane, SOCKET_ENV)
            return 0
        pid = session_pid(sock)
        if pid is None:
            logger.info("lane-doorbell %s %s: socket basename is not a pid; exiting",
                        _now(), lane)
            return 0
        pidfile = _home() / "sessions" / f"{pid}.pid"
        if not _claim_pidfile(pidfile):
            logger.info("lane-doorbell %s %s: session %s already has a helper; exiting",
                        _now(), lane, pid)
            return 0
        try:
            return asyncio.run(run(
                lane, hub_url=hub_url,
                session_file=claude_sessions_dir() / f"{pid}.json",
                socket_path=sock))
        finally:
            _release_pidfile(pidfile)
    except Exception as exc:  # noqa: BLE001 - a hook helper never fails its hook
        logger.warning("lane-doorbell %s %s: stopped (%s)", _now(), lane,
                       type(exc).__name__)
        return 0
    finally:
        if handler is not None:
            logger.removeHandler(handler)
            handler.close()
        logger.setLevel(old_level)
