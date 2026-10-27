"""v2-fleet r17 (F54, T69 part (a); j6-lane-doorbell-per-lane-opt-in): an idle
lane that opted in is rung for news, never for a low message, by a helper its
own session starts.

The repo ships the lane doorbell as src/mcp_hub/lane_doorbell.py, run as
`mcp-hub lane-doorbell` (named apart from the edge-watch doorbell). THE RULE:
one body-less nudge into the session's own inbox socket when the session's
status is idle (or shell) and an unread id of priority normal or urgent has
not been rung for; never for low, never while busy, never in focus, never
twice for one id. It reads news only with unread_ids on one kept connection,
presenting the lane's key, and reads only .status from the session file.

The seams this file drives (the interface the build provides):
  * lane_doorbell.run(lane, *, hub_url, session_file, socket_path, sleep)
    - the async loop; awaits sleep() between polls; returns 0 once
    socket_path is gone.
  * lane_doorbell.open_hub(hub_url, headers) - an async context manager
    yielding an object with `await call_tool(name, arguments)`; replaced here
    by a stub hub answering unread_ids with the hub's JSON
    ({"count": n, "unread": [{"id": .., "priority": ..}]}).
  * lane_doorbell.session_status(mapping) - the reader of a session file's
    parsed JSON.
  * the lane's focus is the 🔕 expiry in the heartbeat's status snapshot
    (cli._write_status_cache); the lane's key is cli._agent_key_file(lane).
  * decisions are logged on the "mcp_hub.lane_doorbell" logger.
  * the session's inbox socket is the path in CLAUDE_CODE_MESSAGING_SOCKET
    (inherited by the hook), never the session file's messagingSocketPath
    (a decoy here); the session file is <HOME>/.claude/sessions/<pid>.json,
    the pid being the socket's basename without its extension. A nudge is two
    JSON lines: {"type": "auth", "token": $CLAUDE_CODE_MESSAGING_TOKEN} then
    {"type": "user", "message": {"role": "user", "content": <text>}}.
  * the CLI: `lane-doorbell --from-session-start`, `lane-doorbell opt-in`,
    `lane-doorbell opt-out`, each with --name or, without it, the lane named
    by cli._resolve_agent_identity for the hook's folder (the process cwd and
    the hook payload's cwd are the same folder here).

Off the seat: temp state dir, temp HOME, a temp unix socket standing for the
session's inbox; the one TCP listener is on 127.0.0.1. FALSIFIERS (red on the
parent, which has no lane_doorbell): N1 to N11 and N8a. Part (b), the live check, is
mcp-hub's owner review and is not in this file.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import io
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Mapping
from pathlib import Path

import pytest
from mcp.types import CallToolResult, TextContent

from mcp_hub import cli

KEY_HEADER = "x-mcp-hub-agent-key"
LANE = "lane-r17"
KEY = "ab" * 32
SESSION_MARK = "MARK-r17-session-e21f"
HUB_URL = "http://127.0.0.1:9/mcp"
LOGGER = "mcp_hub.lane_doorbell"
SOCKET_ENV = "CLAUDE_CODE_MESSAGING_SOCKET"
TOKEN_ENV = "CLAUDE_CODE_MESSAGING_TOKEN"
TOKEN = "TOKEN-r17-5c9e1f"
DECOY = "/nonexistent/r17-decoy.sock"


def _mod():
    try:
        return importlib.import_module("mcp_hub.lane_doorbell")
    except ImportError:
        return None


def _need():
    mod = _mod()
    assert mod is not None, "no mcp_hub.lane_doorbell"
    return mod


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
    """AF_UNIX paths are capped near 100 bytes, so the socket lives in a short
    temp dir rather than under tmp_path."""
    d = Path(tempfile.mkdtemp(prefix="r17-"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


class _Inbox:
    """A unix socket standing for the session's inbox. Each connection that
    carries bytes is one nudge; collect() reads every queued one."""

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
    """A stub hub: records every connection (with its headers) and every tool
    called; answers unread_ids from `self.unread`."""

    def __init__(self):
        self.unread: list[tuple[int, str]] = []
        self.connects: list[dict] = []
        self.calls: list[tuple[str, dict]] = []

    def open(self, hub_url, headers=None, **_kw):
        hub = self

        @contextlib.asynccontextmanager
        async def _cm():
            hub.connects.append({k.lower(): v for k, v in dict(headers or {}).items()})
            yield hub

        return _cm()

    async def call_tool(self, name, arguments=None, **_kw):
        self.calls.append((name, dict(arguments or {})))
        if name != "unread_ids":
            return CallToolResult(content=[TextContent(type="text", text="")])
        body = {"count": len(self.unread),
                "unread": [{"id": i, "priority": p} for i, p in self.unread]}
        return CallToolResult(content=[TextContent(type="text", text=json.dumps(body))])


class _Lane:
    def __init__(self, tmp: Path, sock_dir: Path, hub: _Hub, monkeypatch):
        self.session_file = tmp / "sessions" / "4242.json"
        self.session_file.parent.mkdir(parents=True, exist_ok=True)
        self.socket_path = sock_dir / "inbox.sock"
        self.inbox = _Inbox(self.socket_path)
        self.hub = hub
        self.per_poll: list[int] = []
        self.status("idle")
        key_file = cli._agent_key_file(LANE)
        key_file.parent.mkdir(parents=True, exist_ok=True)
        key_file.write_text(KEY + "\n", encoding="utf-8")
        monkeypatch.setattr(_need(), "open_hub", hub.open)

    def status(self, value: str):
        self.session_file.write_text(json.dumps({
            "status": value, "sessionId": SESSION_MARK, "cwd": "/w/" + SESSION_MARK,
            "messagingSocketPath": DECOY, "pid": 4242,
            "name": SESSION_MARK}), encoding="utf-8")

    async def drive(self, steps) -> int:
        """Run the loop: poll 0 sees the state set before; before poll k
        (k >= 1), steps[k-1] is applied. After the last step the socket goes
        and the loop must end. Returns run()'s return code."""
        mod = _need()
        it = iter(steps)

        async def _sleep(*_a, **_k):
            self.per_poll.append(self.inbox.collect())
            step = next(it, None)
            if step is None:
                self.inbox.close()
            else:
                step()

        rc = await asyncio.wait_for(mod.run(
            LANE, hub_url=HUB_URL, session_file=self.session_file,
            socket_path=self.socket_path, sleep=_sleep), timeout=30)
        self.per_poll.append(self.inbox.collect())
        return rc


def _focus(on: bool):
    mark = " 🔕 45m" if on else ""
    cli._write_status_cache(LANE, f"🟢 **{LANE}** ⚡ 💤{mark} (p) — r17\n")


def _noop():
    return None


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


# =============================================================================
# N1 NORMAL RINGS AN IDLE LANE, ONCE PER ID
# =============================================================================


async def test_n1_a_normal_id_rings_an_idle_lane_once(lane, hub, caplog):
    lane = _have(lane)
    caplog.set_level(logging.DEBUG)
    hub.unread = [(101, "normal")]
    rc = await lane.drive([_noop] * 4)
    assert rc == 0
    assert sum(lane.per_poll) == 1, f"one nudge for one id, got {lane.per_poll}"
    assert lane.per_poll[0] == 1, lane.per_poll
    nudge = lane.inbox.nudges[0]
    assert SESSION_MARK.encode() not in nudge, nudge
    lines = [json.loads(ln) for ln in nudge.decode().splitlines() if ln.strip()]
    assert len(lines) == 2, nudge
    assert lines[0] == {"type": "auth", "token": TOKEN}, lines[0]
    assert lines[1].get("type") == "user", lines[1]
    message = lines[1].get("message") or {}
    assert message.get("role") == "user", lines[1]
    assert isinstance(message.get("content"), str) and message["content"], lines[1]
    assert TOKEN not in message["content"], lines[1]
    assert TOKEN not in caplog.text, "the token reached a log line"


# =============================================================================
# N2 LOW NEVER RINGS
# =============================================================================


async def test_n2_low_never_rings_and_normal_after_it_rings_once(lane, hub):
    lane = _have(lane)
    hub.unread = [(201, "low"), (202, "low")]

    def _normal():
        hub.unread = [(201, "low"), (202, "low"), (203, "normal")]

    rc = await lane.drive([_noop] * 5 + [_normal, _noop, _noop])
    assert rc == 0
    assert sum(lane.per_poll[:6]) == 0, f"a low-only inbox rang: {lane.per_poll}"
    assert sum(lane.per_poll) == 1, f"one nudge for the normal id: {lane.per_poll}"


# =============================================================================
# N3 URGENT RINGS; BUSY WAITS; SHELL COUNTS AS IDLE
# =============================================================================


async def test_n3_busy_waits_then_rings_and_shell_counts_as_idle(lane, hub):
    lane = _have(lane)
    lane.status("busy")
    hub.unread = [(301, "urgent")]

    def _idle():
        lane.status("idle")

    def _shell_and_news():
        lane.status("shell")
        hub.unread = [(301, "urgent"), (302, "normal")]

    rc = await lane.drive([_noop, _noop, _idle, _noop, _shell_and_news, _noop])
    assert rc == 0
    assert sum(lane.per_poll[:3]) == 0, f"rang while busy: {lane.per_poll}"
    assert lane.per_poll[3] == 1, f"no ring at the first idle reading: {lane.per_poll}"
    assert sum(lane.per_poll) == 2, f"shell must count as idle: {lane.per_poll}"


# =============================================================================
# N4 FOCUS
# =============================================================================


async def test_n4_a_lane_in_focus_is_not_rung(lane, hub):
    lane = _have(lane)
    _focus(True)
    hub.unread = [(401, "urgent")]

    def _unfocus():
        _focus(False)

    rc = await lane.drive([_noop] * 4 + [_unfocus, _noop])
    assert rc == 0
    assert sum(lane.per_poll[:5]) == 0, f"rang in focus: {lane.per_poll}"
    assert sum(lane.per_poll) == 1, f"focus over, the id rings once: {lane.per_poll}"


# =============================================================================
# N5 ONLY .STATUS IS READ
# =============================================================================


class _Recording(Mapping):
    def __init__(self, data: dict):
        self._data = dict(data)
        self.read: list[str] = []

    def __getitem__(self, key):
        self.read.append(key)
        return self._data[key]

    def __iter__(self):
        self.read.append("<iter>")
        return iter(self._data)

    def __len__(self):
        self.read.append("<len>")
        return len(self._data)


def test_n5_only_status_is_read():
    mod = _need()
    data = _Recording({"status": "idle", "sessionId": SESSION_MARK,
                       "cwd": "/w/" + SESSION_MARK, "messagingSocketPath": "/s",
                       "pid": 1, "name": SESSION_MARK})
    assert mod.session_status(data) == "idle"
    assert set(data.read) == {"status"}, data.read


# =============================================================================
# N6 IT READS ONLY UNREAD_IDS, ON ONE KEPT CONNECTION, WITH THE KEY
# =============================================================================


async def test_n6_only_unread_ids_on_one_connection_with_the_key(lane, hub):
    lane = _have(lane)
    hub.unread = [(601, "low")]

    def _news():
        hub.unread = [(601, "low"), (602, "normal")]

    rc = await lane.drive([_noop, _noop, _news, _noop, _noop])
    assert rc == 0
    assert sum(lane.per_poll) == 1, lane.per_poll
    assert len(hub.connects) == 1, f"one kept connection, got {len(hub.connects)}"
    assert hub.connects[0].get(KEY_HEADER) == KEY, "the lane's key was not presented"
    names = [n for n, _a in hub.calls]
    assert names and set(names) == {"unread_ids"}, names
    assert all(a.get("agent_name") == LANE for _n, a in hub.calls), hub.calls


# =============================================================================
# N7 OPT-IN, STARTED ATTACHED, ONE PER SESSION, OPT-OUT
# =============================================================================


def test_n7_no_opt_in_exits_at_once_and_starts_nothing(hub, monkeypatch):
    mod = _need()
    monkeypatch.setattr(mod, "open_hub", hub.open)
    t0 = time.monotonic()
    rc = cli.main(["lane-doorbell", "--from-session-start", "--name", LANE,
                   "--hub-url", HUB_URL])
    assert rc == 0
    assert time.monotonic() - t0 < 1.0, "not 'at once'"
    assert hub.connects == [] and hub.calls == [], "a lane that did not opt in was touched"


def _spawn(home: Path, hub_url: str, sock: Path) -> subprocess.Popen:
    src = str(Path(cli.__file__).resolve().parents[1])
    env = dict(os.environ)
    env[SOCKET_ENV] = str(sock)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (src, env.get("PYTHONPATH", "")) if p)
    env["HOME"] = str(home)
    return subprocess.Popen(
        [sys.executable, "-m", "mcp_hub.cli", "lane-doorbell", "--from-session-start",
         "--name", LANE, "--hub-url", hub_url],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env=env)


def _listener() -> socket.socket:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(16)
    srv.settimeout(20)
    return srv


def _reap(*procs):
    for p in procs:
        if p is not None and p.poll() is None:
            p.kill()
            p.wait(timeout=10)


def test_n7_opt_in_runs_attached_once_per_session_and_opt_out_removes_it(
        tmp_path, short_dir):
    _need()
    home = tmp_path / "home"
    # This test process is the session: the hook's helper is its child, and
    # inherits the session's socket in its environment.
    sock = short_dir / f"{os.getpid()}.sock"
    inbox = _Inbox(sock)
    sessions = home / ".claude" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / f"{os.getpid()}.json").write_text(json.dumps({
        "status": "busy", "messagingSocketPath": DECOY, "pid": os.getpid(),
        "sessionId": SESSION_MARK}), encoding="utf-8")
    listener = _listener()
    hub_url = f"http://127.0.0.1:{listener.getsockname()[1]}/mcp"
    quiet = None
    first = second = third = None
    try:
        assert cli.main(["lane-doorbell", "opt-in", "--name", LANE]) == 0
        first = _spawn(home, hub_url, sock)
        conn, _ = listener.accept()   # the helper is past its start and polling
        conn.close()
        assert first.poll() is None, "after opt-in the hook process must run the loop itself"
        assert os.getsid(first.pid) == os.getsid(0), "the helper left the session (setsid)"
        assert os.getpgid(first.pid) == os.getpgid(0), "the helper left the process group"
        second = _spawn(home, hub_url, sock)
        assert second.wait(timeout=15) == 0
        assert first.poll() is None, "a second start must not replace the first"
        inbox.close()   # the session's socket goes: the helper ends (N8)
        assert first.wait(timeout=30) == 0
        assert cli.main(["lane-doorbell", "opt-out", "--name", LANE]) == 0
        inbox = _Inbox(sock)
        quiet = _listener()
        third = _spawn(home, f"http://127.0.0.1:{quiet.getsockname()[1]}/mcp", sock)
        assert third.wait(timeout=15) == 0
        quiet.settimeout(0.5)
        with pytest.raises(OSError):
            quiet.accept()   # after opt-out, nothing connected
    finally:
        _reap(first, second, third)
        listener.close()
        if quiet is not None:
            quiet.close()
        inbox.close()


# =============================================================================
# N10 A FOLDER LANE IS ITS OWN
# =============================================================================


def _elsewhere(tmp_path: Path) -> Path:
    """The helper's own working directory when the payload names the lane's
    folder: a folder of ANOTHER lane, so reading the wrong cwd shows."""
    other = tmp_path / "elsewhere"
    (other / ".claude").mkdir(parents=True)
    (other / ".claude" / "hub-agent.json").write_text(
        json.dumps({"name": "elsewhere-lane", "project": "p"}), encoding="utf-8")
    _hold_key("elsewhere-lane", "02" * 32)
    return other


def _start_in(folder: Path, hub, monkeypatch, short_dir: Path, tag: str,
              *, run_from: Path | None = None, payload_cwd: bool = True) -> None:
    """Opt in from `folder`, then start with no --name, in this process,
    against the stub hub: the SessionStart payload on stdin names `folder`
    (or carries no cwd) while the working directory is `run_from` (default
    `folder`). The inbox goes after the first unread_ids, ending the loop."""
    mod = _need()
    monkeypatch.setattr(mod, "open_hub", hub.open)
    monkeypatch.chdir(folder)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert cli.main(["lane-doorbell", "opt-in"]) == 0
    monkeypatch.chdir(run_from or folder)
    body = {"hook_event_name": "SessionStart"}
    if payload_cwd:
        body["cwd"] = str(folder)
    payload = json.dumps(body)
    pid = {"f": 7101, "g": 7102}.get(tag, 7100)
    sock = short_dir / f"{pid}.sock"
    inbox = _Inbox(sock)
    monkeypatch.setenv(SOCKET_ENV, str(sock))
    sessions = Path(os.environ["HOME"]) / ".claude" / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / f"{pid}.json").write_text(json.dumps({
        "status": "idle", "messagingSocketPath": DECOY, "pid": pid}), encoding="utf-8")
    real_call = hub.call_tool

    async def _call_then_end(name, arguments=None, **kw):
        out = await real_call(name, arguments, **kw)
        inbox.close()
        return out

    monkeypatch.setattr(hub, "call_tool", _call_then_end)
    try:
        monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
        assert cli.main(["lane-doorbell", "--from-session-start",
                         "--hub-url", HUB_URL]) == 0
    finally:
        inbox.close()


def _hold_key(name: str, key: str) -> None:
    path = cli._agent_key_file(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(key + "\n", encoding="utf-8")


def test_n10_a_folder_lane_watches_and_presents_its_own_key(
        tmp_path, short_dir, hub, monkeypatch):
    folder = tmp_path / "folder"
    (folder / ".claude").mkdir(parents=True)
    (folder / ".claude" / "hub-agent.json").write_text(
        json.dumps({"name": "folder-lane", "project": "p"}), encoding="utf-8")
    folder_key = "cd" * 32
    _hold_key("folder-lane", folder_key)
    monkeypatch.setattr(cli, "_git_remote_url", lambda _cwd: None)   # no git
    _start_in(folder, hub, monkeypatch, short_dir, "f", run_from=_elsewhere(tmp_path))
    assert hub.calls, "a folder lane that opted in was never watched"
    assert {a.get("agent_name") for _n, a in hub.calls} == {"folder-lane"}, hub.calls
    assert hub.connects and hub.connects[0].get(KEY_HEADER) == folder_key, hub.connects


def test_n10_with_no_payload_cwd_its_working_directorys_lane(
        tmp_path, short_dir, hub, monkeypatch):
    folder = tmp_path / "folder"
    (folder / ".claude").mkdir(parents=True)
    (folder / ".claude" / "hub-agent.json").write_text(
        json.dumps({"name": "folder-lane", "project": "p"}), encoding="utf-8")
    _hold_key("folder-lane", "cd" * 32)
    monkeypatch.setattr(cli, "_git_remote_url", lambda _cwd: None)   # no git
    _start_in(folder, hub, monkeypatch, short_dir, "w", payload_cwd=False)
    assert hub.calls, "a folder lane that opted in was never watched"
    assert {a.get("agent_name") for _n, a in hub.calls} == {"folder-lane"}, hub.calls


def test_n10_a_git_clone_watches_its_derived_lane(tmp_path, short_dir, hub, monkeypatch):
    clone = tmp_path / "clone"
    (clone / ".claude").mkdir(parents=True)
    # A marker naming another lane: the derived identity wins, as before.
    (clone / ".claude" / "hub-agent.json").write_text(
        json.dumps({"name": "marker-lane", "project": "p"}), encoding="utf-8")
    monkeypatch.setattr(cli, "_git_remote_url",
                        lambda cwd: "git@github.com:org/repo.git"
                        if Path(cwd).resolve() == clone.resolve() else None)
    monkeypatch.setattr(cli, "_load_hub_config", lambda: {"projects": ["org/repo"]})
    derived, _project = cli._derive_agent_identity(str(clone))
    assert derived and derived != "marker-lane", derived
    git_key = "ef" * 32
    _hold_key(derived, git_key)
    _hold_key("marker-lane", "01" * 32)
    _start_in(clone, hub, monkeypatch, short_dir, "g", run_from=_elsewhere(tmp_path))
    assert hub.calls, "an opted-in git clone was never watched"
    assert {a.get("agent_name") for _n, a in hub.calls} == {derived}, hub.calls
    assert hub.connects and hub.connects[0].get(KEY_HEADER) == git_key, hub.connects


# =============================================================================
# N11 THE PID IS THE SOCKET'S BASENAME
# =============================================================================


def _socket_start(short_dir: Path, hub, monkeypatch, sock_name: str,
                  session: str | None, json_name: str = "12345.json",
                  end_on_call: bool = True):
    """Opt in LANE and start it with the socket named `sock_name`; the
    session file `json_name` holds `session` (None: no file). With
    end_on_call the inbox goes after the first unread_ids. Returns the inbox."""
    mod = _need()
    monkeypatch.setattr(mod, "open_hub", hub.open)
    assert cli.main(["lane-doorbell", "opt-in", "--name", LANE]) == 0
    sock = short_dir / sock_name
    inbox = _Inbox(sock)
    monkeypatch.setenv(SOCKET_ENV, str(sock))
    sessions = Path(os.environ["HOME"]) / ".claude" / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    if session is not None:
        (sessions / json_name).write_text(session, encoding="utf-8")
    hub.unread = [(1101, "normal")]
    real_call = hub.call_tool

    async def _call_then_end(name, arguments=None, **kw):
        out = await real_call(name, arguments, **kw)
        inbox.close()
        return out

    if end_on_call:
        monkeypatch.setattr(hub, "call_tool", _call_then_end)
    return inbox


def _start_lane() -> int:
    return cli.main(["lane-doorbell", "--from-session-start", "--name", LANE,
                     "--hub-url", HUB_URL])


def test_n11_a_socket_named_12345_reads_sessions_12345(short_dir, hub, monkeypatch):
    inbox = _socket_start(short_dir, hub, monkeypatch, "12345.sock",
                          json.dumps({"status": "idle", "pid": 12345}))
    try:
        assert _start_lane() == 0
    finally:
        inbox.close()
    assert [n for n, _a in hub.calls] == ["unread_ids"], (
        "an idle status in sessions/12345.json was not read", hub.calls)


@pytest.mark.parametrize("case", ["not-a-pid", "missing", "unreadable"])
def test_n11_no_pid_or_no_readable_status_rings_nothing_and_says_why(
        short_dir, hub, monkeypatch, caplog, case):
    caplog.set_level(logging.DEBUG)
    sock_name, session = {
        "not-a-pid": ("x.sock", json.dumps({"status": "idle"})),
        "missing": ("12345.sock", None),
        "unreadable": ("12345.sock", "{not json"),
    }[case]
    json_name = "x.json" if case == "not-a-pid" else "12345.json"
    # x.json holds an idle status: a helper that took "x" for a pid would ring.
    inbox = _socket_start(short_dir, hub, monkeypatch, sock_name, session, json_name,
                          end_on_call=False)

    def _end_soon():
        time.sleep(3)
        inbox.collect()
        inbox.close()

    ender = threading.Thread(target=_end_soon, daemon=True)
    ender.start()
    try:
        assert _start_lane() == 0
    finally:
        ender.join(timeout=10)
        inbox.close()
    assert inbox.nudges == [], f"{case}: rang"
    said = [r.getMessage() for r in caplog.records if r.name.startswith(LOGGER)]
    assert said, f"{case}: nothing logged"


# =============================================================================
# N8a NO SOCKET
# =============================================================================


def test_n8a_no_socket_in_the_environment_exits_and_says_why(hub, monkeypatch, caplog):
    mod = _need()
    monkeypatch.setattr(mod, "open_hub", hub.open)
    caplog.set_level(logging.DEBUG)
    assert cli.main(["lane-doorbell", "opt-in", "--name", LANE]) == 0
    sessions = Path(os.environ["HOME"]) / ".claude" / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / f"{os.getpid()}.json").write_text(json.dumps({"status": "idle"}),
                                                   encoding="utf-8")
    rc = cli.main(["lane-doorbell", "--from-session-start", "--name", LANE,
                   "--hub-url", HUB_URL])
    assert rc == 0
    assert hub.connects == [] and hub.calls == [], "rang or connected with no socket"
    said = [r for r in caplog.records if r.name.startswith(LOGGER)]
    assert said, "no socket must be logged"


# =============================================================================
# N8 IT ENDS WITH THE SESSION
# =============================================================================


async def test_n8_removing_the_socket_ends_the_loop(lane, hub):
    lane = _have(lane)
    hub.unread = []
    rc = await lane.drive([_noop, _noop])
    assert rc == 0
    assert sum(lane.per_poll) == 0


# =============================================================================
# N9 LOGS HOLD NO BODY
# =============================================================================


async def test_n9_the_decision_log_holds_ids_and_names_only(lane, hub, caplog):
    lane = _have(lane)
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    hub.unread = [(901, "normal")]
    rc = await lane.drive([_noop, _noop])
    assert rc == 0 and sum(lane.per_poll) == 1, lane.per_poll
    text = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith(LOGGER))
    assert "901" in text and LANE in text, f"the ring was not logged by id and name: {text!r}"
    assert SESSION_MARK not in text, f"the session file leaked into the log: {text!r}"
