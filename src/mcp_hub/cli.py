"""Hub CLI — utility commands for agents.

Currently provides one subcommand:

    mcp-hub stop-hook --name=<agent> [--project=<proj>] [--hub-url=<url>]

Designed to be wired into an agent's `~/.claude/settings.json` Stop hook so
queued hub messages surface automatically at every turn boundary, plus a
re-register call if the agent has drifted off the wake path (e.g., after a
hub redeploy wiped the in-memory session registry).

The hook protocol contract:
    - Hook process exits 0
    - If we want Claude to take an extra turn to process content, write JSON
      to stdout: `{"decision": "block", "reason": "<text>"}`. Claude treats
      `reason` as a new prompt and continues.
    - If we want Stop to proceed normally, write nothing.

Fail-open philosophy: any hub error (unreachable, timeout, malformed
response) MUST result in writing nothing and exiting 0. The hook should
NEVER block an agent's Stop because of hub flakiness.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import subprocess
import sys
import time
from typing import Any

DEFAULT_HUB_URL = os.environ.get("MCP_HUB_URL", "https://mcp.monkeypashion.co.uk/mcp")

# Marker file each project uses to declare its agent identity to the hub. Lets
# a single global Stop hook (in ~/.claude/settings.json) work across the whole
# fleet — the cli reads cwd from the hook's stdin payload, looks here, and
# uses the values it finds. Projects without this file silently no-op.
AGENT_MARKER_PATH = pathlib.Path(".claude") / "hub-agent.json"


# ---------------------------------------------------------------------------
# Hub interaction (thin wrapper over the MCP client)
# ---------------------------------------------------------------------------


async def _query_hub(
    hub_url: str, agent_name: str
) -> tuple[str, str, bool]:
    """Connect to the hub, return (dm_text, broadcast_text, is_online).

    - `dm_text` is the rendered output of `get_messages` (empty if no unread).
    - `broadcast_text` is the rendered output of `get_broadcasts_for_agent`,
      which atomically returns broadcasts since the agent's per-agent cursor
      and advances the cursor (so subsequent calls don't re-deliver). Empty
      string if no unseen broadcasts.
    - `is_online` is True when the agent's name appears in `list_agents`
      (status='online' / 🟢). Deliberately NOT keyed on the ⚡ marker — see
      the inline note below on why ⚡ is the wrong signal for the rebind nag.

    On any error, raises — the caller is responsible for fail-open handling.
    """
    # Lazy import so missing-deps doesn't break --help / arg parsing
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async with streamablehttp_client(hub_url, timeout=10) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # bind=False on the calls below: this client is the Stop hook's
            # ephemeral streamablehttp_client — its session_id is DELETEd
            # when the `async with` block exits. Letting the hub auto-bind
            # the agent's name to this short-lived session would clobber
            # the agent's real (long-lived) wake target. The hub's
            # touch_session honours bind=False and skips the binding.
            #
            # mark_idle=True: Stop hook fires at end of turn, which is the
            # idle transition for the agent. The hub uses this flag for the
            # Case 1 wake-on-low-prio path — a peer's low-prio DM to an
            # idle recipient fires a wake (drain-batched) instead of just
            # queuing.
            messages_result = await session.call_tool(
                "get_messages",
                {"agent_name": agent_name, "bind": False, "mark_idle": True},
            )
            broadcasts_result = await session.call_tool(
                "get_broadcasts_for_agent",
                {"agent_name": agent_name, "bind": False},
            )
            agents_result = await session.call_tool("list_agents", {})

    messages_text = _extract_text(messages_result)
    broadcasts_text = _extract_text(broadcasts_result)
    agents_text = _extract_text(agents_result)

    # Is the agent shown as online? list_agents (default include_offline=False)
    # lists ONLY agents with status='online', so the name appearing at all
    # means they're connected to this hub instance (PR #3's 🟢 semantics).
    #
    # We deliberately do NOT key on the ⚡ marker. Since PR #3, ⚡ means
    # "push-deliverable RIGHT NOW" — an open GET /mcp stream. A perfectly
    # healthy agent lacks ⚡ while idle between turns, and the Stop hook fires
    # exactly at that idle transition. Keying the rebind nag on ⚡ produced a
    # false "you're not bound, re-register" alarm on every Stop for every idle
    # agent — a fleet-wide loop that register() couldn't clear (re-binding
    # doesn't reopen the GET stream at idle). Online (🟢) is the correct
    # "is this agent bound?" signal; ⚡ is not.
    is_online = f"**{agent_name}**" in agents_text

    return messages_text, broadcasts_text, is_online


def _extract_text(call_tool_result: Any) -> str:
    """Pull the text payload out of an MCP call_tool result."""
    if call_tool_result is None:
        return ""
    content = getattr(call_tool_result, "content", None)
    if content is None and isinstance(call_tool_result, list):
        content = call_tool_result
    if content is None:
        return ""
    for block in content:
        text = getattr(block, "text", None)
        if text is not None:
            return text
    return ""


# ---------------------------------------------------------------------------
# Hook output building
# ---------------------------------------------------------------------------


def build_hook_response(
    *,
    agent_name: str,
    project: str | None,
    messages_text: str,
    broadcasts_text: str = "",
    is_online: bool,
    stop_hook_active: bool = False,
) -> dict[str, Any] | None:
    """Decide whether to emit a hook block and what the reason should be.

    Returns the JSON payload Claude Code expects, or None to mean "no block,
    let Stop proceed normally."

    A block is emitted whenever there's anything actionable:
      - Queued DMs (with discipline reminder)
      - Unseen broadcasts (with discipline reminder; same gating rule —
        urgent always responds, related/important inline, FYI noted-and-defer)
      - Agent genuinely offline / absent from list_agents (rebind hint, with
        or without other content). NOTE: this keys on online status (🟢), NOT
        the ⚡ wake-marker — an idle-but-online agent is not nagged, since it
        legitimately lacks ⚡ between turns. Keying on ⚡ caused a fleet-wide
        false-rebind loop after PR #3 tightened ⚡ to "deliverable now".

    Online agent with empty inbox AND no unseen broadcasts → return None,
    Stop proceeds normally. This is the steady-state happy path: most Stop
    fires are no-op when the agent is up-to-date.

    `stop_hook_active` is Claude Code's flag for "this Stop is firing because
    a prior Stop-hook block already fired". It's a loop backstop: a re-fire
    has no fresh content (DMs were marked read, the broadcast cursor advanced
    on the first fire), so a content-less block would re-emit forever. When
    it's set and there's nothing new to surface, we let Stop proceed.
    """
    has_messages = bool(messages_text.strip())
    has_broadcasts = bool(broadcasts_text.strip())
    has_content = has_messages or has_broadcasts

    # Loop backstop: never re-block a Stop that's only firing because a prior
    # block fired, when there's nothing new to surface. Guards against any
    # content-less block (e.g. a rebind nag) wedging the agent in a re-block
    # loop, independent of the online/⚡ fix above.
    if stop_hook_active and not has_content:
        return None

    # No work needed: online + nothing queued. (Online — not ⚡ — is the gate:
    # an idle agent legitimately lacks ⚡ between turns.)
    if not has_content and is_online:
        return None

    parts: list[str] = []

    if has_content:
        parts.append("📬 Auto-checked at Stop boundary — queued items below:")
        if has_messages:
            parts.extend(["", "**Direct messages:**", messages_text.strip()])
        if has_broadcasts:
            parts.extend(["", "**Broadcasts (since you last looked):**", broadcasts_text.strip()])

    if not is_online:
        rebind_args = [f'name="{agent_name}"']
        if project:
            rebind_args.append(f'project="{project}"')
        rebind_call = f"register({', '.join(rebind_args)})"

        if has_content:
            warning = (
                f"⚠️ Your hub session isn't showing as online in "
                f"list_agents (likely after a hub redeploy or a dropped "
                f"connection). Call `{rebind_call}` to re-register "
                f"before processing the queue."
            )
        else:
            warning = (
                f"⚠️ Auto-checked at Stop boundary: your hub session "
                f"isn't showing as online in list_agents (likely after a "
                f"hub redeploy or a dropped connection). No queued items "
                f"to process. Call `{rebind_call}` to re-register, then "
                f"continue what you were doing."
            )
        if has_content:
            parts.extend(["", warning])
        else:
            parts.append(warning)

    if has_content:
        parts.extend(
            [
                "",
                (
                    "Discipline reminder: process if related/important to current "
                    "work; otherwise note (one-line ack) and continue. Don't deeply "
                    "context-switch on FYI/low-priority items. Urgent always "
                    "responds."
                ),
            ]
        )

    return {"decision": "block", "reason": "\n".join(parts)}


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------


def _read_hook_stdin() -> dict[str, Any]:
    """Read the JSON payload Claude Code sends to hooks on stdin.

    Returns {} on any error (no input, malformed JSON, no stdin attached).
    Callers should treat absent fields as "unknown" — the CLI is designed
    to no-op rather than fail when context is missing.
    """
    try:
        if sys.stdin.isatty():
            return {}
        raw = sys.stdin.read()
        if not raw.strip():
            return {}
        return json.loads(raw)
    except Exception:  # noqa: BLE001
        return {}


def _discover_agent_from_marker(cwd: str | None) -> tuple[str | None, str | None]:
    """Look for `<cwd>/.claude/hub-agent.json` and read agent identity.

    The marker file shape:
        {"name": "dreamteam-lead", "project": "dreamteam"}

    Returns (name, project) — either or both may be None if the marker
    doesn't exist or is malformed. The cli silently no-ops in that case;
    not every project on the system is a hub agent, and most aren't.
    """
    if not cwd:
        return None, None
    marker = pathlib.Path(cwd) / AGENT_MARKER_PATH
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None, None
    name = data.get("name")
    project = data.get("project")
    return (
        name if isinstance(name, str) and name else None,
        project if isinstance(project, str) and project else None,
    )


def _resolve_agent_identity(
    args: argparse.Namespace,
    payload: dict[str, Any] | None = None,
) -> tuple[str | None, str | None]:
    """Resolve which agent this hook invocation is for.

    Resolution order:
      1. Explicit --name (and --project) on the CLI — overrides everything.
         Useful for tests, manual checks, non-standard setups.
      2. Project marker file at <cwd>/.claude/hub-agent.json — discovered
         from the cwd Claude Code passes to hooks via stdin. Lets a single
         global hook config cover the whole fleet.
      3. Nothing — return (None, None) and the cli will silently no-op.

    The marker file path is fixed (`.claude/hub-agent.json`) so each
    project self-declares with no central registry to maintain.

    `payload` is the already-parsed Stop-hook stdin. Callers that also need
    other stdin fields (e.g. `stop_hook_active`) must read stdin ONCE and
    pass it in — stdin is not re-readable, so a second `_read_hook_stdin()`
    would return {}. If None, we read it here.
    """
    if args.name:
        return args.name, args.project

    if payload is None:
        payload = _read_hook_stdin()
    cwd = payload.get("cwd")
    return _discover_agent_from_marker(cwd)


def stop_hook_command(args: argparse.Namespace) -> int:
    """Run the stop-hook subcommand. Always returns 0 (fail-open)."""
    # Read stdin ONCE — both agent identity (cwd marker) and the
    # stop_hook_active loop-backstop flag come from this single payload.
    payload = _read_hook_stdin()
    name, project = _resolve_agent_identity(args, payload)
    if name is None:
        # No identity resolved — this project isn't onboarded as a hub agent.
        # Silent no-op: most projects on the box aren't hub agents and the
        # global Stop hook fires in all of them. We don't want noise.
        return 0

    # True when this Stop is firing because a prior Stop-hook block fired.
    stop_hook_active = bool(payload.get("stop_hook_active"))

    # Self-heal the keep-alive daemon before anything else — this runs even if
    # the hub is unreachable (the daemon will retry-connect on its own), so a
    # dead/absent daemon is revived at the next turn regardless of hub health.
    _ensure_daemon_alive(name, args.hub_url)

    try:
        messages_text, broadcasts_text, is_online = asyncio.run(
            _query_hub(args.hub_url, name)
        )
    except Exception as exc:  # noqa: BLE001
        # Fail open — never block the agent on hub flakiness.
        print(f"[mcp-hub stop-hook] hub query failed: {exc!r}", file=sys.stderr)
        return 0

    response = build_hook_response(
        agent_name=name,
        project=project,
        messages_text=messages_text,
        broadcasts_text=broadcasts_text,
        is_online=is_online,
        stop_hook_active=stop_hook_active,
    )

    if response is None:
        return 0  # No block — Stop proceeds normally

    print(json.dumps(response))
    return 0


# ---------------------------------------------------------------------------
# session-start subcommand — inject "register now" into the agent's first turn
# ---------------------------------------------------------------------------


def session_start_command(args: argparse.Namespace) -> int:
    """Emit SessionStart hook JSON instructing the agent to call register()
    immediately. This makes the agent ⚡ from session start without operator
    nudging — closes the chicken-and-egg gap where the heartbeat daemon
    can't bring an agent online (it doesn't bind, by design), only sustain
    a binding once one exists.

    Reads `<cwd>/.claude/hub-agent.json` for identity, same pattern as
    stop-hook. Silent no-op if no marker (project isn't a hub agent —
    don't spam the SessionStart of unrelated projects with register
    instructions).

    Output format: SessionStart hooks support JSON with
    `hookSpecificOutput.additionalContext` (or plain `additionalContext`)
    that gets injected into the agent's context before the first turn.
    Claude reads it and acts on it proactively.
    """
    name, project = _resolve_agent_identity(args)
    if name is None:
        return 0  # no marker → silent no-op

    project_str = f', project="{project}"' if project else ""
    register_call = f'mcp__hub__register(name="{name}"{project_str})'

    context = (
        f"You are the hub agent **{name}**"
        + (f" (project: {project})" if project else "")
        + ". Your first action this session is to register on the hub for "
        + "channel-push wake.\n"
        + f"1. Call `{register_call}` now.\n"
        + "2. If the hub's tools aren't available yet, the `hub` MCP server is "
        + "simply still connecting — this is common right after a resume. Do "
        + "NOT conclude the hub is down and do NOT just hold: wait for it to "
        + "finish connecting, then register. If a `WaitForMcpServers` tool is "
        + "available, use it to wait for the `hub` server; otherwise pause "
        + "briefly and retry. The connection usually settles within ~30s.\n"
        + "3. Once register succeeds you are bound for wake and the heartbeat "
        + "daemon keeps the binding alive across idle periods. Until you "
        + "register, your inbox is queue-only (messages wait for the next Stop "
        + "hook auto-pull) and your statusline shows `hub ✖ REGISTER`."
    )

    output = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    }
    print(json.dumps(output))
    return 0


def session_rewake_command(args: argparse.Namespace) -> int:
    """Companion to `session-start`: writes the register instruction to
    stderr and exits with code 2 to trigger Claude Code's asyncRewake
    behaviour. The intent is to fire an unprompted first turn at session
    start so Claude calls register() before the operator has typed
    anything.

    This is empirically untested in this codebase — the docs are ambiguous
    about whether asyncRewake fires from a cold session start (no prior
    turns to "wake" from) or only between turns of an active conversation.
    If it doesn't fire, this is a no-op; the synchronous `session-start`
    hook's additionalContext still drives the register on first prompt.

    Reads `<cwd>/.claude/hub-agent.json`; silent no-op (exit 0) if no
    marker found, so non-hub projects don't get spurious wake events.
    """
    name, project = _resolve_agent_identity(args)
    if name is None:
        return 0  # no marker → exit 0, no wake

    project_str = f', project="{project}"' if project else ""
    register_call = f'mcp__hub__register(name="{name}"{project_str})'
    msg = (
        f"Hub auto-register: call `{register_call}` now to bind your "
        f"interactive MCP session for channel-push wake. The heartbeat "
        f"daemon (separate process) will then keep your binding alive."
    )
    print(msg, file=sys.stderr)
    return 2  # asyncRewake trigger


# ---------------------------------------------------------------------------
# heartbeat-daemon subcommand
# ---------------------------------------------------------------------------


HEARTBEAT_INTERVAL_SECONDS = 60
HEARTBEAT_RETRY_DELAY_SECONDS = 60
# Statusline cache is refreshed more often than the heartbeat so register/
# offline transitions show up promptly (not up to a full heartbeat late).
STATUS_REFRESH_SECONDS = 15


# ---------------------------------------------------------------------------
# Singleton enforcement — stop the daemon leak.
#
# Each SessionStart spawns a heartbeat daemon. On POSIX the OS reaps it when
# the parent Claude Code process dies; on Windows it does NOT — so every
# session restart (and every SessionStart-resume, which fires on each hub
# reconnect) leaves the old daemon running AND adds a new one. Observed
# 2026-05-29: ~12 daemons accumulated on one machine, and pre-cutover ones
# kept pinging the dead public hub URL.
#
# We enforce one-daemon-per-agent with an atomic pidfile claim:
#   - First daemon for an agent atomically creates the pidfile (O_EXCL) and
#     runs the heartbeat loop.
#   - Any later daemon finds the pidfile, sees a live daemon already owns it,
#     and EXITS immediately instead of looping forever. No accumulation.
#   - If the pidfile is stale (owner dead/crashed, or garbage), the newcomer
#     removes it and claims it — self-healing.
#
# Design choices and why:
#   * "Old wins" (incumbent keeps running, newcomer exits) rather than
#     "new wins (kill incumbent)". The daemon is FUNGIBLE — it only calls
#     heartbeat(agent_name), which refreshes whatever binding currently
#     exists for that agent, regardless of which process sends it. So one
#     surviving daemon per agent is fully sufficient even across session
#     restarts. Old-wins also means we NEVER terminate another process —
#     zero risk of killing a PID-reused stranger — and the atomic O_EXCL
#     create makes it race-safe (two near-simultaneous daemons can't both
#     win). New-wins-by-kill had a kill-war race and PID-reuse hazard.
#   * Tradeoff: a config change (e.g. the MCP_HUB_URL cutover) is NOT
#     auto-adopted while an old-config daemon is still alive — the newcomer
#     with the new config exits. Mitigation: a one-off `Stop-Process` sweep
#     of the daemons on the rare config change (operationally cheap; we did
#     exactly this for the 2026-05-29 cutover). Frequency strongly favours
#     this: the leak/accumulation happens on every restart; config changes
#     are rare and operator-driven.
#   * A parent-death watch can't fix the Windows leak: the daemon's parent is
#     the `mcp-hub.exe` console-script launcher, which leaks alongside the
#     python daemon when Claude Code dies, so the parent PID stays "alive".
#
# See project_heartbeat_daemon_leak memory.
# ---------------------------------------------------------------------------

# Stable per-agent pidfile directory. Deliberately NOT tempfile.gettempdir():
# that honours TMPDIR/TEMP, which Claude Code overrides for its subprocesses
# (observed: ...\Temp\claude). A daemon spawned by the SessionStart hook could
# then land on a different temp dir than another context, so the pidfiles
# wouldn't find each other and dedup would silently fail. The home dir is
# invariant across however the daemon gets spawned.
_PIDFILE_DIR = pathlib.Path.home() / ".mcp-hub"


def _heartbeat_pidfile(agent_name: str) -> pathlib.Path:
    """Stable per-agent pidfile path under ~/.mcp-hub.

    Per-agent (not global) so each agent on a shared machine keeps its own
    single daemon — the claim only ever considers the same agent's daemon.
    """
    safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in agent_name)
    return _PIDFILE_DIR / f"heartbeat-{safe}.pid"


def _is_live_daemon(pid: int) -> bool:
    """True if `pid` is a live process that looks like a heartbeat daemon.

    Conservative: returns False whenever identity is uncertain (so a recycled
    PID belonging to an unrelated process is treated as 'not a daemon' and the
    newcomer takes over the stale pidfile rather than deferring to a stranger).
    Note this function never kills anything — it's purely a liveness/identity
    probe for the claim logic.
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        # Windows os.kill(pid, 0) is not a safe liveness probe (non-CTRL
        # signals unconditionally TerminateProcess the target), so use
        # tasklist and verify the PID exists AND its image is python/launcher.
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return False
        low = out.lower()
        return str(pid) in out and ("python" in low or "mcp-hub" in low)
    # POSIX: signal 0 is a real liveness probe.
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Alive but owned by another user — not our daemon.
        return False
    except OSError:
        return False
    # When /proc is available (Linux), confirm it's actually a heartbeat
    # daemon. Elsewhere (e.g. macOS) trust liveness — the leak this guards
    # against is Windows-only anyway.
    cmdline = pathlib.Path(f"/proc/{pid}/cmdline")
    try:
        data = cmdline.read_bytes().replace(b"\x00", b" ").decode("utf-8", "replace")
    except (FileNotFoundError, OSError):
        return True
    return "heartbeat-daemon" in data


def _claim_singleton(agent_name: str, *, getpid=os.getpid) -> pathlib.Path | None:
    """Try to become the sole heartbeat daemon for `agent_name`.

    Returns the pidfile path if we won the claim (caller should run the loop),
    or None if a live daemon already owns it (caller should exit immediately).

    Race-safe via atomic O_EXCL create. If an existing pidfile is stale
    (owner dead, or garbage contents), it's removed and the claim retried.
    """
    pidfile = _heartbeat_pidfile(agent_name)
    try:
        pidfile.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        # Can't create the state dir — fail open: run unguarded rather than
        # refuse to heartbeat. Worst case is the pre-fix behaviour (possible
        # duplicate), never a missing heartbeat.
        return pidfile

    # Bounded retry: each iteration either wins the create, defers to a live
    # owner, or clears one stale pidfile and loops. A handful of iterations is
    # plenty; the cap just prevents a pathological spin.
    for _ in range(10):
        try:
            fd = os.open(str(pidfile), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                prev = int(pidfile.read_text(encoding="utf-8").strip())
            except (FileNotFoundError, ValueError, OSError):
                prev = None
            if prev is not None and prev != getpid() and _is_live_daemon(prev):
                return None  # a live daemon already owns this agent — stand down
            # Stale/garbage/own-PID — drop it and retry the atomic create.
            try:
                pidfile.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                return None  # lost the race to another claimant — stand down
            continue
        else:
            with os.fdopen(fd, "w") as f:
                f.write(str(getpid()))
            return pidfile
    # Couldn't settle the claim (extreme contention) — fail open and run.
    return pidfile


def _release_singleton(pidfile: pathlib.Path, *, getpid=os.getpid) -> None:
    """Remove the pidfile on clean exit, but only if it still names us — so we
    never delete a successor daemon's claim."""
    try:
        owner = int(pidfile.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError, OSError):
        return
    if owner == getpid():
        try:
            pidfile.unlink()
        except OSError:
            pass


def _daemon_alive_for(agent_name: str) -> bool:
    """True if a live heartbeat daemon currently owns `agent_name`'s pidfile.

    Cheap check (reuses the singleton's liveness probe) so the Stop hook can
    decide whether to self-heal a missing/dead daemon at a turn boundary.
    """
    pidfile = _heartbeat_pidfile(agent_name)
    try:
        prev = int(pidfile.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError, OSError):
        return False
    return _is_live_daemon(prev)


def _spawn_daemon_detached(agent_name: str, hub_url: str) -> None:
    """Launch `heartbeat-daemon` fully detached so it outlives the short-lived
    Stop hook that spawns it.

    Cross-platform: POSIX uses a new session (setsid) so the daemon isn't
    killed when the hook returns; Windows uses DETACHED_PROCESS. Invoked via
    `python -m mcp_hub.cli` (not a PATH lookup for the console script) so it
    works from any venv layout. The singleton claim inside the daemon makes
    this safe to call redundantly — a second daemon stands down at once.
    """
    cmd = [
        sys.executable, "-m", "mcp_hub.cli", "heartbeat-daemon",
        "--name", agent_name, "--hub-url", hub_url,
    ]
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = (
            getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        )
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(cmd, **kwargs)  # noqa: S603 — fire-and-forget self-heal


def _ensure_daemon_alive(agent_name: str, hub_url: str) -> None:
    """Self-heal the keep-alive daemon: if none is running for `agent_name`,
    spawn one (detached).

    Called from the Stop hook so a crashed or never-started daemon is revived
    at the next turn boundary instead of leaving the agent without ⚡ keep-alive
    until a full session relaunch. Idempotent by construction — the singleton
    caps it at one daemon per agent on every platform, so a redundant call is a
    no-op. Fail-open: any error here must never disturb the Stop hook.
    """
    try:
        if _daemon_alive_for(agent_name):
            return
        _spawn_daemon_detached(agent_name, hub_url)
    except Exception as exc:  # noqa: BLE001
        print(f"[mcp-hub stop-hook] daemon self-heal failed: {exc!r}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Status cache — powers the hub segment in the Claude Code statusline.
#
# The statusline command (a fast Node script in ~/.claude) must NOT do a hub
# round-trip on every refresh — that's slow and hammers the hub. Instead the
# heartbeat daemon, which already holds an open MCP session and runs every
# minute even while the agent is idle, writes a tiny per-agent JSON snapshot of
# wakeability here. The statusline just reads that file (instant, no network,
# no Python spawn). The daemon survives a stream death (it's what keeps the
# binding 🟢), so it keeps reporting "this window went unwakeable" — exactly the
# failure we want surfaced. Staleness is the reader's job: if `ts` is older than
# a few heartbeat intervals, the daemon has stopped and the snapshot is suspect.
# ---------------------------------------------------------------------------


def _parse_status_from_agents(agents_text: str, agent_name: str) -> dict[str, Any]:
    """Parse `list_agents` rendered output into a wakeability snapshot.

    Each online agent is one line shaped like:
        🟢 **name** ⚡ 💤 (project) — bio...
    We read only the head (before the ` — ` bio separator) for markers so a
    bio that happens to contain ⚡/🟢/`**` can't skew the counts.

    Returns {online, wakeable, fleet_wakeable, fleet_total} where online/
    wakeable are this agent's own state and the fleet_* are totals across all
    listed (online) agents.
    """
    fleet_total = 0
    fleet_wakeable = 0
    self_online = False
    self_wakeable = False
    for line in agents_text.splitlines():
        head = line.split("—", 1)[0]
        if "🟢" not in head:
            continue  # not an agent row
        fleet_total += 1
        is_wakeable = "⚡" in head
        if is_wakeable:
            fleet_wakeable += 1
        if f"**{agent_name}**" in head:
            self_online = True
            self_wakeable = is_wakeable
    return {
        "online": self_online,
        "wakeable": self_wakeable,
        "fleet_wakeable": fleet_wakeable,
        "fleet_total": fleet_total,
    }


def _status_cache_path(agent_name: str) -> pathlib.Path:
    """Per-agent status snapshot path under ~/.mcp-hub (alongside pidfiles)."""
    safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in agent_name)
    return _PIDFILE_DIR / f"status-{safe}.json"


def _write_status_cache(agent_name: str, agents_text: str) -> None:
    """Parse `agents_text` and atomically write this agent's status snapshot.

    Fail-soft by contract: a parse/write error must NEVER propagate into the
    heartbeat loop (a broken statusline cache is cosmetic; a broken heartbeat
    drops the binding). Atomic tmp+replace so the reader never sees a partial
    file.
    """
    try:
        status = _parse_status_from_agents(agents_text, agent_name)
        status["agent"] = agent_name
        status["ts"] = int(time.time())
        path = _status_cache_path(agent_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(status), encoding="utf-8")
        tmp.replace(path)
    except Exception:  # noqa: BLE001
        pass


async def _heartbeat_loop(hub_url: str, agent_name: str) -> None:
    """Long-lived loop: connect to hub, ping `heartbeat(agent_name)` every
    HEARTBEAT_INTERVAL_SECONDS. On any connection error, sleep and reconnect.

    Single MCP session is held open across heartbeats — this is the right
    shape because heartbeat doesn't bind, so the session lifetime is just
    a connection-pooling concern, not a wake-target concern.

    The status cache is refreshed every STATUS_REFRESH_SECONDS (snappy
    statusline) while heartbeat fires every HEARTBEAT_INTERVAL_SECONDS (binding
    keep-alive) — decoupled so a register/offline change shows up promptly
    instead of up to a full heartbeat late. list_agents failing is treated as a
    connection problem (propagates to the reconnect handler, same as a failed
    heartbeat); only the parse/write step is fail-soft (inside
    _write_status_cache).
    """
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    while True:
        try:
            async with streamablehttp_client(hub_url, timeout=10) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    # Force a heartbeat on the first tick after each (re)connect.
                    since_heartbeat = HEARTBEAT_INTERVAL_SECONDS
                    while True:
                        if since_heartbeat >= HEARTBEAT_INTERVAL_SECONDS:
                            await session.call_tool(
                                "heartbeat", {"agent_name": agent_name}
                            )
                            since_heartbeat = 0
                        agents_result = await session.call_tool("list_agents", {})
                        _write_status_cache(
                            agent_name, _extract_text(agents_result)
                        )
                        await asyncio.sleep(STATUS_REFRESH_SECONDS)
                        since_heartbeat += STATUS_REFRESH_SECONDS
        except Exception as exc:  # noqa: BLE001
            # Connection / init / call failure — log and reconnect after a
            # delay. Fail-open: heartbeat outages don't crash the daemon.
            print(
                f"[mcp-hub heartbeat] connection error ({type(exc).__name__}: "
                f"{exc}); retrying in {HEARTBEAT_RETRY_DELAY_SECONDS}s",
                file=sys.stderr,
            )
            await asyncio.sleep(HEARTBEAT_RETRY_DELAY_SECONDS)


def heartbeat_daemon_command(args: argparse.Namespace) -> int:
    """Run the heartbeat-daemon subcommand. Long-running; only returns on
    KeyboardInterrupt or unrecoverable error.

    Designed to be spawned by an async SessionStart hook in
    ~/.claude/settings.json. On POSIX the OS reaps the daemon when Claude
    Code exits; on Windows it does not, so we enforce one-daemon-per-agent
    via `_claim_singleton`: the first daemon for an agent atomically claims a
    pidfile and runs; any later daemon sees a live owner and stands down
    instead of looping forever. This caps accumulation at one per agent. See
    the _claim_singleton block comment for the old-wins rationale.
    """
    name, _project = _resolve_agent_identity(args)
    if name is None:
        # Silent no-op — same fail-open contract as stop-hook. Lets the
        # global SessionStart hook fire in every project without
        # needing per-project opt-out for non-hub projects.
        return 0

    pidfile = _claim_singleton(name)
    if pidfile is None:
        # A live daemon already owns this agent — stand down rather than
        # leak a second one. This is the fix for the daemon accumulation.
        return 0
    try:
        asyncio.run(_heartbeat_loop(args.hub_url, name))
    except KeyboardInterrupt:
        return 0
    finally:
        _release_singleton(pidfile)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcp-hub",
        description="MCP Hub — server + utility CLI",
    )
    sub = parser.add_subparsers(dest="subcommand")

    stop_hook = sub.add_parser(
        "stop-hook",
        help="Auto-check hub messages at Stop boundaries (for settings.json hooks)",
        description=(
            "Queries the hub for queued DMs to the active agent and emits "
            "Claude Code Stop hook JSON if any are pending. Designed to be "
            "wired into a global ~/.claude/settings.json Stop hook with no "
            "args — the cli auto-discovers agent identity from the project's "
            ".claude/hub-agent.json marker. Use explicit --name to override "
            "auto-discovery (e.g. for testing). Fail-open — never blocks Stop "
            "on hub errors or missing markers."
        ),
    )
    stop_hook.add_argument(
        "--name",
        default=None,
        help=(
            "Agent name on the hub. If omitted, auto-discovers from "
            "<cwd>/.claude/hub-agent.json via the cwd Claude Code passes to "
            "hooks on stdin."
        ),
    )
    stop_hook.add_argument(
        "--project",
        default=None,
        help="Project name (used in re-register hint when drifted)",
    )
    stop_hook.add_argument(
        "--hub-url",
        default=DEFAULT_HUB_URL,
        help=f"Hub MCP endpoint (default: {DEFAULT_HUB_URL}, or $MCP_HUB_URL)",
    )

    session_start = sub.add_parser(
        "session-start",
        help="Inject auto-register instruction into the agent's first turn (for SessionStart hooks)",
        description=(
            "Reads <cwd>/.claude/hub-agent.json and emits SessionStart hook "
            "JSON with `additionalContext` telling the agent to call "
            "register() at session start. Claude sees the context before its "
            "first turn and binds the hub session automatically. Silent "
            "no-op if no marker found."
        ),
    )
    session_start.add_argument(
        "--name",
        default=None,
        help="Agent name override (otherwise auto-discovered from marker).",
    )
    session_start.add_argument(
        "--project",
        default=None,
        help="Project name override (otherwise auto-discovered from marker).",
    )

    session_rewake = sub.add_parser(
        "session-rewake",
        help="Try to trigger an unprompted first turn via asyncRewake (for SessionStart hooks)",
        description=(
            "Companion to session-start. Writes the register instruction to "
            "stderr and exits with code 2 to trigger Claude Code's "
            "asyncRewake behaviour. If asyncRewake fires from a cold "
            "session start, Claude takes an unprompted first turn and "
            "calls register before the operator types anything. If it "
            "doesn't fire, this is a no-op; session-start's additionalContext "
            "still drives the register on first prompt."
        ),
    )
    session_rewake.add_argument(
        "--name",
        default=None,
        help="Agent name override (otherwise auto-discovered from marker).",
    )
    session_rewake.add_argument(
        "--project",
        default=None,
        help="Project name override (otherwise auto-discovered from marker).",
    )

    heartbeat = sub.add_parser(
        "heartbeat-daemon",
        help="Long-running per-minute heartbeat to the hub (for SessionStart hooks)",
        description=(
            "Long-lived daemon that pings the hub's heartbeat tool every "
            f"{HEARTBEAT_INTERVAL_SECONDS}s, proving the agent's Claude Code "
            "session is still alive. Designed to be spawned by an async "
            "SessionStart hook in ~/.claude/settings.json. Reads agent "
            "identity from <cwd>/.claude/hub-agent.json (same marker as "
            "stop-hook). Silent no-op if no marker found. Reconnects on "
            "transient hub errors."
        ),
    )
    heartbeat.add_argument(
        "--name",
        default=None,
        help=(
            "Agent name on the hub. If omitted, auto-discovers from "
            "<cwd>/.claude/hub-agent.json via the cwd Claude Code passes "
            "to hooks on stdin."
        ),
    )
    heartbeat.add_argument(
        "--project",
        default=None,
        help="Project name (currently informational; reserved for future use)",
    )
    heartbeat.add_argument(
        "--hub-url",
        default=DEFAULT_HUB_URL,
        help=f"Hub MCP endpoint (default: {DEFAULT_HUB_URL}, or $MCP_HUB_URL)",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.subcommand == "stop-hook":
        return stop_hook_command(args)
    if args.subcommand == "session-start":
        return session_start_command(args)
    if args.subcommand == "session-rewake":
        return session_rewake_command(args)
    if args.subcommand == "heartbeat-daemon":
        return heartbeat_daemon_command(args)

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
