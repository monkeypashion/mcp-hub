"""v2-fleet r18 (F59, T74; j6-the-doorbell-says-what-it-is-and-if-it-runs):
the lane doorbell's nudge says it is a machine signal, its errors name their
cause, and its status says whether its loop still runs.

All three were seen live in the trial helper on 27 Oct. THE RULE:
  (1) LABEL: NUDGE_TEXT ends with LABEL below and still names no message,
      sender or body.
  (2) CAUSE: a hub error row names the innermost cause (through nested
      ExceptionGroups, the first exception at each level) by class name and
      says it was wrapped; a TimeoutError row names the step that timed out
      (connect, initialize or unread_ids) and its bound in seconds. Never an
      exception's message, a body, the session file's content or the token.
  (3) ALIVE: the loop writes an alive row at least every ALIVE_SECONDS (300),
      naming the helper's pid and the errors in a row, through quiet idle and
      through reconnects alike. `mcp-hub lane-doorbell` with no action adds
      the helper's state: running (pid, newest row's age) only when the
      lane's log's newest row is at most ALIVE_SECONDS + 120 s old and the pid
      its newest alive or watching row names is live; stale (pid live, row
      too old); else not running. rc 0, first words as before.

The seams this file drives, beyond r17's (run, open_hub, session_status, the
"mcp_hub.lane_doorbell" logger, the CLI):
  * run(lane, *, hub_url, session_file, socket_path, sleep, clock): clock is
    a zero-argument callable giving epoch seconds; row times and the alive
    timing come from it (the default is the wall clock).
  * ALIVE_SECONDS, and HUB_TIMEOUT_SECONDS read when each hub step starts.
  * log_handler(lane): the logging handler that writes rows to the lane's
    log, the log `--from-session-start` writes and the status form reads.
  * the real open_hub takes ClientSession from the mcp package and
    streamablehttp_client from mcp.client.streamable_http when it runs.
  * row wording: an alive row holds the word "alive", "pid <pid>" and
    "<n> errors in a row"; the watching row at start holds "watching" and
    "pid <pid>".

Off the seat: temp state dir, temp HOME, a temp unix socket standing for the
session's inbox, a stub hub (no network). FALSIFIERS (red on the parent): L1,
C1, C2, A1, A2, S1, S2, S3. GUARDS: (5) and (6). The carried r17
lane-doorbell, edge-watch doorbell and test_cli.py tests are guard (4), in
the same command.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import inspect
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest
from mcp.types import CallToolResult, TextContent

from mcp_hub import cli

LANE = "lane-r18"
KEY = "cd" * 32
SESSION_MARK = "MARK-r18-session-7d1a"
HUB_URL = "http://127.0.0.1:9/mcp"
LOGGER = "mcp_hub.lane_doorbell"
SOCKET_ENV = "CLAUDE_CODE_MESSAGING_SOCKET"
TOKEN_ENV = "CLAUDE_CODE_MESSAGING_TOKEN"
TOKEN = "TOKEN-r18-3b8e0c"
SECRET = "s3cret-msg"
LABEL = ("[machine signal from the lane's own doorbell; no message content; "
         "never the operator's word]")
BOUND = 0.25
STEP_S = 30.0
PID_RE = re.compile(r"\bpid (\d+)\b")
ERRS_RE = re.compile(r"\b(\d+) errors? in a row\b")

_OPENED: list[str] | None = None


def _audit(event, args):
    if _OPENED is not None and event == "open" and args:
        path = args[0]
        if isinstance(path, (str, bytes, os.PathLike)):
            with contextlib.suppress(Exception):
                _OPENED.append(os.path.realpath(os.fsdecode(path)))


sys.addaudithook(_audit)


def _mod():
    try:
        return importlib.import_module("mcp_hub.lane_doorbell")
    except ImportError:
        return None


def _need():
    mod = _mod()
    assert mod is not None, "no mcp_hub.lane_doorbell"
    return mod


def _need_clock(mod):
    assert "clock" in inspect.signature(mod.run).parameters, "run() takes no clock"


def _need_log(mod):
    assert callable(getattr(mod, "log_handler", None)), "no lane_doorbell.log_handler"


@pytest.fixture(autouse=True)
def _private(monkeypatch, tmp_path):
    monkeypatch.setenv("MCP_HUB_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("MCP_HUB_URL", raising=False)
    monkeypatch.delenv(SOCKET_ENV, raising=False)
    monkeypatch.setenv(TOKEN_ENV, TOKEN)
    (tmp_path / "home").mkdir()


@pytest.fixture
def short_dir():
    """AF_UNIX paths are capped near 100 bytes: the socket lives in a short
    temp dir."""
    d = Path(tempfile.mkdtemp(prefix="r18-"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


class _Inbox:
    """A unix socket standing for the session's inbox; each connection that
    carries bytes is one nudge."""

    def __init__(self, path: Path):
        self.path = path
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(str(path))
        self.srv.listen(64)
        self.srv.setblocking(False)
        self.nudges: list[bytes] = []

    def collect(self) -> int:
        n = 0
        while True:
            try:
                conn, _ = self.srv.accept()
            except (BlockingIOError, OSError):
                return n
            with conn:
                conn.setblocking(True)
                conn.settimeout(2.0)
                data = b""
                with contextlib.suppress(OSError):
                    while chunk := conn.recv(65536):
                        data += chunk
            if data:
                self.nudges.append(data)
                n += 1

    def close(self):
        self.srv.close()
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()


class _Hub:
    """A stub hub. `unread` answers unread_ids; `raise_call` makes unread_ids
    raise it; `hang_call` makes it hang; `fail_connect` makes every
    connection fail."""

    def __init__(self):
        self.unread: list[tuple[int, str]] = []
        self.connects = 0
        self.calls: list[str] = []
        self.raise_call: BaseException | None = None
        self.hang_call = False
        self.fail_connect = False

    def open(self, hub_url, headers=None, **_kw):
        hub = self

        @contextlib.asynccontextmanager
        async def _cm():
            hub.connects += 1
            if hub.fail_connect:
                raise ConnectionRefusedError(SECRET)
            yield hub

        return _cm()

    async def call_tool(self, name, arguments=None, **_kw):
        self.calls.append(name)
        if self.raise_call is not None:
            raise self.raise_call
        if self.hang_call:
            await asyncio.sleep(3600)
        body = {"count": len(self.unread),
                "unread": [{"id": i, "priority": p} for i, p in self.unread]}
        return CallToolResult(content=[TextContent(type="text", text=json.dumps(body))])


class _Clock:
    def __init__(self, start: float):
        self.t = start

    def __call__(self) -> float:
        return self.t


class _Lane:
    def __init__(self, tmp: Path, sock_dir: Path, hub: _Hub, monkeypatch):
        self.sessions = tmp / "sessions"
        self.session_file = self.sessions / "4242.json"
        self.sessions.mkdir(parents=True, exist_ok=True)
        self.socket_path = sock_dir / "inbox.sock"
        self.inbox = _Inbox(self.socket_path)
        self.per_poll: list[int] = []
        self.status("idle")
        key_file = cli._agent_key_file(LANE)
        key_file.parent.mkdir(parents=True, exist_ok=True)
        key_file.write_text(KEY + "\n", encoding="utf-8")
        self.real_open = _need().open_hub
        monkeypatch.setattr(_need(), "open_hub", hub.open)

    def status(self, value: str):
        self.session_file.write_text(json.dumps({
            "status": value, "sessionId": SESSION_MARK, "cwd": "/w/" + SESSION_MARK,
            "name": SESSION_MARK, "pid": 4242}), encoding="utf-8")

    async def drive(self, steps: int, clock=None, advance: float = 0.0) -> int:
        """Run the loop for `steps` sleeps, then remove the socket. Each sleep
        collects nudges and advances `clock` by `advance` seconds."""
        mod = _need()
        left = [steps]

        async def _sleep(*_a, **_k):
            self.per_poll.append(self.inbox.collect())
            if clock is not None:
                clock.t += advance
            left[0] -= 1
            if left[0] < 0:
                self.inbox.close()
            await asyncio.sleep(0)

        kw = {}
        if clock is not None:
            _need_clock(mod)
            kw["clock"] = clock
        rc = await asyncio.wait_for(mod.run(
            LANE, hub_url=HUB_URL, session_file=self.session_file,
            socket_path=self.socket_path, sleep=_sleep, **kw), timeout=60)
        self.per_poll.append(self.inbox.collect())
        return rc


@pytest.fixture
def hub():
    return _Hub()


@pytest.fixture
def lane(tmp_path, short_dir, hub, monkeypatch):
    ln = _Lane(tmp_path, short_dir, hub, monkeypatch) if _mod() else None
    yield ln
    if ln is not None:
        ln.inbox.close()


def _have(lane):
    assert lane is not None, "no mcp_hub.lane_doorbell"
    return lane


def _rows(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name.startswith(LOGGER)]


# =============================================================================
# L1 THE NUDGE IS LABELLED
# =============================================================================


async def test_l1_the_nudge_ends_with_the_machine_signal_label(lane, hub, caplog):
    lane = _have(lane)
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    hub.unread = [(1101, "normal")]
    rc = await lane.drive(2)
    assert rc == 0 and sum(lane.per_poll) == 1, lane.per_poll
    lines = [json.loads(ln) for ln in lane.inbox.nudges[0].decode().splitlines() if ln.strip()]
    assert len(lines) == 2 and lines[0] == {"type": "auth", "token": TOKEN}, lines
    content = lines[1]["message"]["content"]
    assert content.endswith(LABEL), f"no machine-signal label: {content!r}"
    assert SESSION_MARK not in content and "1101" not in content, content
    assert TOKEN not in "\n".join(_rows(caplog))


# =============================================================================
# C1 THE CAUSE, NOT THE WRAPPER
# =============================================================================


async def test_c1_a_wrapped_error_names_its_innermost_cause(lane, hub, caplog):
    lane = _have(lane)
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    hub.raise_call = ExceptionGroup(
        "outer", [ExceptionGroup("inner", [ConnectionResetError(SECRET)])])
    rc = await lane.drive(2)
    assert rc == 0
    rows = _rows(caplog)
    hits = [r for r in rows if "ConnectionResetError" in r]
    assert hits, f"no row names the cause: {rows}"
    assert any("wrapped" in r for r in hits), f"no row says it was wrapped: {hits}"
    alone = [r for r in rows if "ExceptionGroup" in r and "ConnectionResetError" not in r]
    assert not alone, f"a row names only the wrapper: {alone}"
    assert SECRET not in "\n".join(rows), "an exception's message is in a row"


# =============================================================================
# C2 A TIMEOUT NAMES ITS STEP
# =============================================================================


async def test_c2_an_unread_ids_timeout_names_its_step_and_bound(lane, hub, caplog, monkeypatch):
    lane = _have(lane)
    mod = _need()
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    monkeypatch.setattr(mod, "HUB_TIMEOUT_SECONDS", BOUND)
    hub.hang_call = True
    rc = await lane.drive(1)
    assert rc == 0
    rows = [r for r in _rows(caplog) if "TimeoutError" in r]
    assert rows, f"no TimeoutError row: {_rows(caplog)}"
    assert any("unread_ids" in r and str(BOUND) in r for r in rows), rows


async def test_c2_an_initialize_timeout_names_initialize(lane, hub, caplog, monkeypatch):
    lane = _have(lane)
    mod = _need()
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    monkeypatch.setattr(mod, "HUB_TIMEOUT_SECONDS", BOUND)
    import mcp
    import mcp.client.streamable_http as sh

    @contextlib.asynccontextmanager
    async def _transport(*_a, **_k):
        yield (None, None, None)

    class _Session:
        def __init__(self, *_a, **_k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def initialize(self):
            await asyncio.sleep(3600)

    monkeypatch.setattr(sh, "streamablehttp_client", _transport)
    monkeypatch.setattr(mcp, "ClientSession", _Session)
    monkeypatch.setattr(mod, "open_hub", lane.real_open)   # the module's own
    rc = await lane.drive(1)
    assert rc == 0
    rows = [r for r in _rows(caplog) if "TimeoutError" in r]
    assert rows, f"no TimeoutError row: {_rows(caplog)}"
    assert any("initialize" in r and str(BOUND) in r for r in rows), rows


# =============================================================================
# A1 ALIVE WHILE QUIET; A2 ALIVE WHILE FAILING
# =============================================================================


def _alive(rows: list[str]) -> list[str]:
    return [r for r in rows if re.search(r"\balive\b", r)]


async def test_a1_an_idle_lane_with_nothing_to_ring_writes_alive_rows(lane, hub, caplog):
    lane = _have(lane)
    mod = _need()
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    _need_clock(mod)
    assert mod.ALIVE_SECONDS <= 300, mod.ALIVE_SECONDS
    hub.unread = []
    steps = int(3 * mod.ALIVE_SECONDS / STEP_S) + 4
    rc = await lane.drive(steps, clock=_Clock(time.time()), advance=STEP_S)
    assert rc == 0 and sum(lane.per_poll) == 0
    alive = _alive(_rows(caplog))
    assert len(alive) >= 3, f"{len(alive)} alive rows over 3 x ALIVE_SECONDS: {alive}"
    for row in alive:
        m = PID_RE.search(row)
        assert m and int(m.group(1)) == os.getpid(), f"an alive row without the pid: {row}"
    assert SESSION_MARK not in "\n".join(_rows(caplog))


async def test_a2_a_failing_hub_still_gives_alive_rows_with_errors_rising(lane, hub, caplog):
    lane = _have(lane)
    mod = _need()
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    _need_clock(mod)
    hub.fail_connect = True
    steps = int(3 * mod.ALIVE_SECONDS / STEP_S) + 4
    rc = await lane.drive(steps, clock=_Clock(time.time()), advance=STEP_S)
    assert rc == 0
    alive = _alive(_rows(caplog))
    assert len(alive) >= 3, f"{len(alive)} alive rows while failing: {alive}"
    counts = []
    for row in alive:
        m = ERRS_RE.search(row)
        assert m, f"an alive row without the errors in a row: {row}"
        counts.append(int(m.group(1)))
    assert counts == sorted(counts) and counts[-1] > counts[0] and counts[-1] >= 3, counts
    assert SECRET not in "\n".join(_rows(caplog))


# =============================================================================
# S1 RUNNING; S2 STALE; S3 NOT RUNNING
# =============================================================================


def _status(capsys) -> tuple[int, str]:
    capsys.readouterr()
    rc = cli.main(["lane-doorbell", "--name", LANE])
    return rc, capsys.readouterr().out


def _opt_in(capsys):
    assert cli.main(["lane-doorbell", "opt-in", "--name", LANE]) == 0
    capsys.readouterr()


async def _rows_into_the_log(lane, clock: _Clock) -> None:
    mod = _need()
    _need_log(mod)
    _need_clock(mod)
    handler = mod.log_handler(LANE)
    lg = logging.getLogger(LOGGER)
    old = lg.level
    lg.setLevel(logging.INFO)
    lg.addHandler(handler)
    try:
        assert await lane.drive(2, clock=clock) == 0
    finally:
        lg.removeHandler(handler)
        handler.close()
        lg.setLevel(old)


async def test_s1_a_fresh_row_and_a_live_pid_is_running(lane, hub, capsys):
    lane = _have(lane)
    _opt_in(capsys)
    await _rows_into_the_log(lane, _Clock(time.time()))
    rc, out = _status(capsys)
    assert rc == 0
    assert out.startswith(f"lane-doorbell: {LANE} opted in"), out
    assert "running" in out and "not running" not in out and "stale" not in out, out
    assert str(os.getpid()) in out, out


async def test_s2_a_row_too_old_with_a_live_pid_is_stale(lane, hub, capsys):
    lane = _have(lane)
    mod = _need()
    _opt_in(capsys)
    _need_clock(mod)
    old = time.time() - (mod.ALIVE_SECONDS + 120 + 60)
    await _rows_into_the_log(lane, _Clock(old))
    rc, out = _status(capsys)
    assert rc == 0
    assert out.startswith(f"lane-doorbell: {LANE} opted in"), out
    assert "stale" in out and str(os.getpid()) in out, out


_DEAD_HELPER = """
import asyncio, logging, sys, time
from mcp_hub import lane_doorbell as m
lg = logging.getLogger("mcp_hub.lane_doorbell")
lg.setLevel(logging.INFO)
h = m.log_handler(sys.argv[1])
lg.addHandler(h)
async def _sleep():
    return None
asyncio.run(m.run(sys.argv[1], hub_url="http://127.0.0.1:9/mcp", session_file=sys.argv[2],
                  socket_path=sys.argv[3], sleep=_sleep, clock=time.time))
h.close()
"""


async def test_s3_a_fresh_row_from_a_dead_pid_is_not_running(lane, hub, capsys, tmp_path):
    lane = _have(lane)
    mod = _need()
    _need_log(mod)
    _need_clock(mod)
    _opt_in(capsys)
    src = str(Path(cli.__file__).resolve().parents[1])
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (src, env.get("PYTHONPATH", "")) if p)
    p = subprocess.Popen([sys.executable, "-c", _DEAD_HELPER, LANE, str(lane.session_file),
                          str(tmp_path / "gone.sock")],
                         stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, env=env)
    _out, err = p.communicate(timeout=60)
    assert p.returncode == 0, err.decode()[-400:]
    rc, out = _status(capsys)
    assert rc == 0
    assert out.startswith(f"lane-doorbell: {LANE} opted in"), out
    assert "not running" in out, out


def test_s3_no_log_is_not_running(capsys):
    _need()
    _opt_in(capsys)
    rc, out = _status(capsys)
    assert rc == 0
    assert out.startswith(f"lane-doorbell: {LANE} opted in"), out
    assert "not running" in out, out


# =============================================================================
# GUARDS
# =============================================================================


def test_5_a_lane_not_opted_in_still_says_so_and_starts_nothing(hub, capsys, monkeypatch):
    mod = _need()
    monkeypatch.setattr(mod, "open_hub", hub.open)
    rc, out = _status(capsys)
    assert rc == 0
    assert out.startswith(f"lane-doorbell: {LANE} not opted in"), out
    assert hub.connects == 0 and hub.calls == []


async def test_6_the_status_reading_touches_no_session_file(lane, hub, capsys):
    global _OPENED
    lane = _have(lane)
    _opt_in(capsys)
    await _rows_into_the_log(lane, _Clock(time.time()))
    _OPENED = []
    try:
        rc, _out = _status(capsys)
    finally:
        opened, _OPENED = _OPENED, None
    assert rc == 0
    sessions = os.path.realpath(lane.sessions)
    touched = [p for p in opened if p.startswith(sessions + os.sep)]
    assert not touched, f"the status form opened a session file: {touched}"
