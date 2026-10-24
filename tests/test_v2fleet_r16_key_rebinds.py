"""v2-fleet r16 (F52 v3, T67 v4; mcp-hub #70): a lane's key rebinds it after a
deploy.

Lanes that reach the hub through the shared ~/.claude.json entry carry no
?agent= name, so after every hub restart the url-rebind sweep has nothing to
bind them by and they stay unwakeable until a turn of their own. Every
request they send already carries the lane's hub-minted key (F41). THE RULE:
a request with no ?agent= whose key's sha256 is held by exactly one
agent_keys row tags its transport as ?agent= would, and the existing sweep
binds it under every one of its gates, unchanged.

Hermetic. Each test mints its own keys into a temp DB. Transports are the
real StreamableHTTPSessionManager's table holding fake transports; requests
go through the manager's real (patched) handle_request with no task group,
so the hub's capture half runs and the library half raises and is ignored.
The sweep runs its real loop with anyio.sleep stubbed for a fixed number of
iterations. Nothing here opens a socket.

GUARDS pass on the parent and must still pass: (2), (3a), (4)'s ?agent=
binding, (5), (7)'s off case, (1)'s no-agents-row case.
FALSIFIERS fail on the parent by assertion (a key-only transport is never
tagged there): (1), (3b), (4)'s mismatch row, (6)'s key-bound half, (7)'s
enforce and unarmed cases, (8), and (9a) and (9b) (red at their first bind).
"""

from __future__ import annotations

import logging
import re
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import anyio
import pytest
from mcp.server.streamable_http import GET_STREAM_KEY

import mcp_hub.server as hub
from mcp_hub.server import create_server

pytestmark = pytest.mark.anyio

KEY_HEADER = "x-mcp-hub-agent-key"
MODE_ENV = "MCP_HUB_AGENT_KEYS"
PEER = "100.64.0.77"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv(MODE_ENV, "observe")
    monkeypatch.setenv("MCP_HUB_URL_REBIND", "1")
    monkeypatch.delenv("MCP_HUB_OPERATOR_TOKEN", raising=False)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _Sess:
    """A ServerSession stand-in: its write stream is its identity."""

    def __init__(self, client_name: str = "claude-code") -> None:
        self._write_stream = object()
        self.sent: list[object] = []
        if client_name:
            self.client_params = SimpleNamespace(
                clientInfo=SimpleNamespace(name=client_name))

    async def send_ping(self):
        return None

    async def send_notification(self, notification, *a, **k):
        self.sent.append(notification)


class _Transport:
    def __init__(self, sess: _Sess, get: bool = True) -> None:
        self._write_stream = sess._write_stream
        self._request_streams = {GET_STREAM_KEY: object()} if get else {}
        self.is_terminated = False


class _Req:
    def __init__(self, headers: dict[str, str]):
        self.headers = {k.lower(): v for k, v in headers.items()}
        self.client = SimpleNamespace(host=PEER, port=1)


class _Ctx:
    def __init__(self, session=None, headers: dict[str, str] | None = None):
        self.session = session or _Sess("")
        self.request_context = SimpleNamespace(request=_Req(headers or {}))


def _text(result) -> str:
    if isinstance(result, str):
        return result
    for block in getattr(result, "content", None) or result:
        if hasattr(block, "text"):
            return block.text
    return str(result)


async def _call(server, name, args, ctx=None) -> str:
    return _text(await server._tool_manager.call_tool(name, args, context=ctx))


async def _receive():
    return {"type": "http.request", "body": b"", "more_body": False}


async def _send(_message):
    return None


class _Hub:
    """One hub process over a DB file: a fresh one is a deploy."""

    def __init__(self, db: Path) -> None:
        self.db = db
        self.server = create_server(db_path=db)
        self.server.streamable_http_app()
        self.manager = self.server.session_manager
        self.registry = self.server._hub_registry
        self.sessions: list[_Sess] = []
        self._n = 0

    def connect(self, sess: _Sess, get: bool = True) -> str:
        self._n += 1
        sid = f"r16sid{self._n:04d}"
        self.manager._server_instances[sid] = _Transport(sess, get)
        self.sessions.append(sess)
        return sid

    async def request(self, sid: str, key: str | None = None,
                      agent: str = "", n: int = 1) -> None:
        for _ in range(n):
            headers = [(b"mcp-session-id", sid.encode())]
            if key is not None:
                headers.append((KEY_HEADER.encode(), key.encode("latin-1")))
            scope = {
                "type": "http", "method": "POST", "path": "/mcp",
                "query_string": (f"agent={agent}".encode() if agent else b""),
                "headers": headers, "client": (PEER, 5555),
            }
            try:
                await self.manager.handle_request(scope, _receive, _send)
            except RuntimeError:
                pass  # no task group: the library half never runs here

    async def sweep(self, iterations: int = 1) -> None:
        real_sleep = anyio.sleep
        calls = 0

        async def fast_sleep(_delay):
            nonlocal calls
            calls += 1
            if calls > iterations:
                raise anyio.get_cancelled_exc_class()()
            await real_sleep(0)

        live = list(self.sessions)
        with patch.object(anyio, "sleep", fast_sleep), \
                patch.object(hub, "live_server_sessions", lambda: live):
            try:
                await self.server._hub_url_rebind_sweep()
            except anyio.get_cancelled_exc_class():
                pass


def _conn(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    return conn


def _status(db: Path, name: str) -> str:
    row = _conn(db).execute(
        "SELECT status FROM agents WHERE name = ?", (name,)).fetchone()
    return row[0] if row else ""


def _events(db: Path, verdict: str | None = None) -> list[tuple]:
    rows = _conn(db).execute(
        "SELECT name, action, verdict, count FROM agent_key_events"
    ).fetchall()
    return [tuple(r) for r in rows if verdict is None or r[2] == verdict]


async def _claim(server, name: str) -> str:
    """The CLI's claim: an unbound, non-interactive HTTP client."""
    out = await _call(server, "claim_agent_key", {"name": name}, _Ctx())
    assert "AGENT-KEY: " in out, out
    return out.split("AGENT-KEY: ", 1)[1].split()[0]


async def _lane(server, name: str, arm: bool = True) -> str:
    """A keyed lane before the deploy: claimed, and (armed) registered by
    its interactive session presenting the key."""
    if not arm:
        await _call(server, "register", {"name": name, "project": "p"},
                    _Ctx(_Sess("claude-code")))
        return await _claim(server, name)
    key = await _claim(server, name)
    await _call(server, "register", {"name": name, "project": "p"},
                _Ctx(_Sess("claude-code"), {KEY_HEADER: key}))
    return key


def _deploy(db: Path) -> _Hub:
    """A fresh hub process over the same DB; the old one's sessions are
    gone and the restart left everyone offline."""
    _conn(db).execute("UPDATE agents SET status = 'offline'").connection.commit()
    return _Hub(db)


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "hub.db"


@pytest.fixture
async def keyed(db: Path):
    """alice holds a minted, armed key; bob holds his own."""
    before = _Hub(db)
    alice = await _lane(before.server, "alice")
    bob = await _lane(before.server, "bob")
    return alice, bob


class _Lookups:
    """Counts key lookups: SELECTs on agent_keys by key_hash, on every
    connection the hub opens through _get_db (T67 v3 (8))."""

    def __init__(self) -> None:
        self.stmts: list[str] = []

    def reset(self) -> None:
        self.stmts.clear()

    @property
    def count(self) -> int:
        n = 0
        for sql in self.stmts:
            s = " ".join(sql.split()).lower()
            where = s.split(" where ", 1)[1] if " where " in s else ""
            if (s.startswith("select") and "agent_keys" in s
                    and "key_hash" in where):
                n += 1
        return n


@pytest.fixture
def lookups(monkeypatch) -> _Lookups:
    rec = _Lookups()
    original = hub._get_db

    def traced(db_path=hub.DB_PATH):
        conn = original(db_path)
        conn.set_trace_callback(rec.stmts.append)
        return conn

    monkeypatch.setattr(hub, "_get_db", traced)
    yield rec
    for conn in getattr(hub._local, "conns", {}).values():
        conn.set_trace_callback(None)


# ---------------------------------------------------------------------------
# (1) KEY-ONLY BINDS AFTER A RESTART  [falsifier]
# ---------------------------------------------------------------------------


async def test_1_a_key_only_lane_is_rebound_after_a_deploy(db, keyed):
    alice_key, _bob_key = keyed
    h = _deploy(db)
    assert h.registry.get("alice") is None
    sess = _Sess("claude-code")
    sid = h.connect(sess)
    await h.request(sid, key=alice_key, n=3)
    await h.sweep()
    assert h.registry.get("alice") is sess, (
        "a key-only interactive transport must be bound to its key's name")
    assert _status(db, "alice") == "online"
    out = await _call(h.server, "list_agents", {})
    line = next((ln for ln in out.splitlines() if "alice" in ln), "")
    assert "⚡" in line, out
    await _call(h.server, "send", {"from_agent": "bob", "to": "alice",
                                   "message": "wake up", "priority": "urgent"},
                _Ctx(h.registry.get("bob") or _Sess(""), {KEY_HEADER: keyed[1]}))
    await anyio.sleep(0.05)
    assert sess.sent, "the DM to alice must be pushed to her rebound session"


async def test_1_guard_a_key_without_an_agents_row_binds_nothing(db):
    before = _Hub(db)
    ghost = await _claim(before.server, "ghost")  # claimed, never registered
    h = _deploy(db)
    sess = _Sess("claude-code")
    sid = h.connect(sess)
    await h.request(sid, key=ghost)
    await h.sweep()
    assert h.registry.get("ghost") is None


# ---------------------------------------------------------------------------
# (2) EPHEMERAL NEVER BINDS  [guard]
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("client_name", ["mcp", ""])
async def test_2_guard_a_hook_or_heartbeat_with_the_key_binds_nothing(
        db, keyed, client_name):
    alice_key, _ = keyed
    h = _deploy(db)
    sid = h.connect(_Sess(client_name))
    await h.request(sid, key=alice_key, n=3)
    await h.sweep(2)
    assert h.registry.get("alice") is None
    assert _status(db, "alice") == "offline"


# ---------------------------------------------------------------------------
# (3) BAD KEY  [guard: binds nothing; falsifier: logged once, never the key]
# ---------------------------------------------------------------------------


async def _bad_keys(db: Path, alice_key: str) -> dict[str, str]:
    """unknown, non-hex, held-by-two, near-miss; the revoked case is its
    own. Every name the bad rows carry is a registered agent, so only the
    key lookup stands between a bad key and a binding."""
    before = _Hub(db)
    for name in ("carol", "dave", "erin"):
        await _call(before.server, "register", {"name": name, "project": "p"},
                    _Ctx(_Sess("claude-code")))
    conn = _conn(db)
    # Two rows holding one hash (no live path makes them; the lookup must
    # still refuse to choose between them).
    two = "two-holders-" + "a" * 52
    for name in ("carol", "dave"):
        conn.execute(
            "INSERT INTO agent_keys (name, key_hash, created) VALUES (?, ?, 0)",
            (name, hub._agent_key_hash(two)),
        )
    # A row whose hash shares the presented key's first 16 hex digits and
    # nothing else: a match is the whole hash or nothing.
    near = "e" * 64
    conn.execute(
        "INSERT INTO agent_keys (name, key_hash, created) VALUES (?, ?, 0)",
        ("erin", hub._agent_key_hash(near)[:16] + "0" * 48),
    )
    conn.commit()
    assert alice_key not in (two, near)
    return {
        "unknown": "f" * 64,
        "non-hex": "not a key; " + "z" * 40,
        "held-by-two": two,
        "near-miss": near,
    }


async def test_3a_guard_a_bad_key_binds_nothing(db, keyed):
    alice_key, _ = keyed
    bad = await _bad_keys(db, alice_key)
    h = _deploy(db)
    for _label, key in bad.items():
        sid = h.connect(_Sess("claude-code"))
        await h.request(sid, key=key, n=3)
    await h.sweep(2)
    for name in ("alice", "bob", "carol", "dave", "erin"):
        assert h.registry.get(name) is None, name
    assert _status(db, "alice") == "offline"


async def test_3a_guard_a_revoked_key_binds_nothing(db, keyed):
    alice_key, _ = keyed
    h = _deploy(db)
    out = await _call(h.server, "reset_agent_key", {"name": "alice"})
    assert "Reset" in out, out
    sid = h.connect(_Sess("claude-code"))
    await h.request(sid, key=alice_key, n=3)
    await h.sweep(2)
    assert h.registry.get("alice") is None


async def test_3b_a_bad_key_is_logged_once_per_transport_never_the_key(
        db, keyed, caplog):
    alice_key, _ = keyed
    bad = await _bad_keys(db, alice_key)
    h = _deploy(db)
    await _call(h.server, "reset_agent_key", {"name": "alice"})
    bad["revoked"] = alice_key
    caplog.set_level(logging.DEBUG)
    for label, key in bad.items():
        caplog.clear()
        sid = h.connect(_Sess("claude-code"))
        await h.request(sid, key=key, n=5)
        await h.sweep(2)
        for name in ("alice", "bob", "carol", "dave", "erin"):
            assert h.registry.get(name) is None, f"{label}: {name} bound"
        texts = [r.getMessage() for r in caplog.records]
        assert not any(key in t for t in texts), f"{label}: the key was logged"
        noted = [r for r in caplog.records
                 if r.levelno >= logging.INFO and PEER in r.getMessage()]
        assert len(noted) == 1, (
            f"{label}: a key that binds nothing is logged once per transport "
            f"with the peer; got {[r.getMessage() for r in noted]}")


# ---------------------------------------------------------------------------
# (4) ?agent= WINS
# ---------------------------------------------------------------------------


async def test_4_guard_agent_param_binds_as_today_with_any_key(db, keyed):
    alice_key, _ = keyed
    h = _deploy(db)
    s_bob, s_alice = _Sess("claude-code"), _Sess("claude-code")
    sid_bob, sid_alice = h.connect(s_bob), h.connect(s_alice)
    await h.request(sid_bob, key=alice_key, agent="bob", n=3)
    await h.request(sid_alice, key=alice_key, agent="alice", n=3)
    await h.sweep()
    assert h.registry.get("bob") is s_bob
    assert h.registry.get("alice") is s_alice
    assert _events(db, "mismatch") == [] or all(
        e[0] == "bob" for e in _events(db, "mismatch"))


async def test_4_a_mismatched_key_is_recorded_under_the_named_agent(db, keyed):
    alice_key, _ = keyed
    h = _deploy(db)
    s_bob, s_alice = _Sess("claude-code"), _Sess("claude-code")
    sid_bob, sid_alice = h.connect(s_bob), h.connect(s_alice)
    await h.request(sid_bob, key=alice_key, agent="bob", n=5)
    await h.request(sid_alice, key=alice_key, agent="alice", n=5)
    await h.sweep()
    assert h.registry.get("bob") is s_bob
    assert _events(db, "mismatch") == [("bob", "url-rebind", "mismatch", 1)], (
        _events(db))


# ---------------------------------------------------------------------------
# (5) CROSS-IDENTITY  [guard]
# ---------------------------------------------------------------------------


async def test_5_guard_a_session_owning_another_name_is_never_rebound(
        db, keyed):
    alice_key, bob_key = keyed
    h = _deploy(db)
    sess = _Sess("claude-code")
    sid = h.connect(sess)
    await _call(h.server, "register", {"name": "bob", "project": "p"},
                _Ctx(sess, {KEY_HEADER: bob_key}))
    assert h.registry.get("bob") is sess
    await h.request(sid, key=alice_key, n=3)
    await h.sweep(2)
    assert h.registry.get("alice") is None
    assert h.registry.get("bob") is sess


# ---------------------------------------------------------------------------
# (6) NOTHING ELSE CHANGES  [falsifier: the key-bound half]
# ---------------------------------------------------------------------------


async def _bound_run(tmp_path: Path, how: str) -> tuple[list[str], list]:
    db = tmp_path / how / "hub.db"
    db.parent.mkdir()
    before = _Hub(db)
    alice_key = await _lane(before.server, "alice")
    await _lane(before.server, "bob")
    h = _deploy(db)
    sess = _Sess("claude-code")
    sid = h.connect(sess)
    await h.request(sid, key=alice_key,
                    agent=("alice" if how == "param" else ""), n=3)
    await h.sweep()
    assert h.registry.get("alice") is sess, f"{how}: alice is not bound"
    ctx = _Ctx(sess, {KEY_HEADER: alice_key})
    await _call(h.server, "send", {"from_agent": "bob", "to": "alice",
                                   "message": "ping"},
                _Ctx(_Sess(""), {}))
    outs = [
        await _call(h.server, "send", {"from_agent": "alice", "to": "bob",
                                       "message": "same words"}, ctx),
        await _call(h.server, "get_messages", {"agent_name": "alice"}, ctx),
        await _call(h.server, "register", {"name": "alice", "project": "p"},
                    ctx),
    ]
    grades = _conn(db).execute(
        "SELECT from_agent, to_agent, attribution FROM messages ORDER BY id"
    ).fetchall()
    return [_same_clock(o.replace(sid, "<sid>")) for o in outs], [
        tuple(g) for g in grades]


def _same_clock(text: str) -> str:
    """The two runs are a second apart: wall-clock stamps and ages are the
    only bytes allowed to differ."""
    text = re.sub(r"\[\d\d:\d\d:\d\d\]", "[hh:mm:ss]", text)
    return re.sub(r"\b\d+(s|m|h) ago\b", r"N\1 ago", text)


async def test_6_a_key_bound_lane_answers_as_a_param_bound_lane(tmp_path):
    param_outs, param_grades = await _bound_run(tmp_path, "param")
    key_outs, key_grades = await _bound_run(tmp_path, "key")
    assert key_grades == param_grades
    for p, k in zip(param_outs, key_outs):
        assert k == p


# ---------------------------------------------------------------------------
# (7) MODES
# ---------------------------------------------------------------------------


async def test_7_guard_with_keys_off_nothing_binds_and_no_key_is_read(
        db, keyed, monkeypatch, lookups):
    alice_key, _ = keyed
    monkeypatch.setenv(MODE_ENV, "off")
    h = _deploy(db)
    lookups.reset()
    sid = h.connect(_Sess("claude-code"))
    await h.request(sid, key=alice_key, n=5)
    await h.sweep(2)
    assert h.registry.get("alice") is None
    assert lookups.count == 0, lookups.stmts


async def test_7_enforce_binds_like_observe(db, keyed, monkeypatch):
    alice_key, _ = keyed
    monkeypatch.setenv(MODE_ENV, "enforce")
    h = _deploy(db)
    sess = _Sess("claude-code")
    sid = h.connect(sess)
    await h.request(sid, key=alice_key, n=3)
    await h.sweep()
    assert h.registry.get("alice") is sess


async def test_7_an_unarmed_name_binds_on_its_matching_key(db):
    before = _Hub(db)
    key = await _lane(before.server, "alice", arm=False)
    row = _conn(db).execute(
        "SELECT armed_at FROM agent_keys WHERE name = 'alice'").fetchone()
    assert row is not None and row[0] is None, "alice must be unarmed"
    h = _deploy(db)
    sess = _Sess("claude-code")
    sid = h.connect(sess)
    await h.request(sid, key=key, n=3)
    await h.sweep()
    assert h.registry.get("alice") is sess


# ---------------------------------------------------------------------------
# (8) COST  [falsifier]
# ---------------------------------------------------------------------------


async def test_8_one_key_lookup_per_transport(db, keyed, lookups):
    alice_key, _ = keyed
    h = _deploy(db)
    sess = _Sess("claude-code")
    sid = h.connect(sess)
    lookups.reset()
    await h.request(sid, key=alice_key, n=20)
    await h.sweep(3)
    await h.request(sid, key=alice_key, n=20)
    await h.sweep(1)
    assert h.registry.get("alice") is sess
    assert lookups.count <= 1, (
        f"{lookups.count} key lookups for one transport and key")


# ---------------------------------------------------------------------------
# (9) REVOKED WHILE LIVE  [falsifier: red on the parent at its first bind]
# ---------------------------------------------------------------------------


async def test_9a_a_key_reset_while_live_stops_the_rebind(db, keyed):
    alice_key, _ = keyed
    h = _deploy(db)
    sess = _Sess("claude-code")
    sid = h.connect(sess)
    await h.request(sid, key=alice_key, n=3)
    await h.sweep()
    assert h.registry.get("alice") is sess, "the key-tagged transport binds"
    out = await _call(h.server, "reset_agent_key", {"name": "alice"})
    assert "Reset" in out, out
    h.registry.unbind_name("alice")
    await h.request(sid, key=alice_key, n=3)
    await h.sweep(2)
    assert h.registry.get("alice") is None, (
        "a key reset while the transport is live must stop its rebind")


async def test_9b_a_reclaimed_name_never_rebinds_the_old_key(db, keyed):
    """Reset and re-claimed (a new key, a new hash) while the old key-tagged
    transport is live: the name's row exists again, but not with the old
    transport's hash, so the old transport never binds."""
    alice_key, _ = keyed
    h = _deploy(db)
    sess = _Sess("claude-code")
    sid = h.connect(sess)
    await h.request(sid, key=alice_key, n=3)
    await h.sweep()
    assert h.registry.get("alice") is sess, "the key-tagged transport binds"
    out = await _call(h.server, "reset_agent_key", {"name": "alice"})
    assert "Reset" in out, out
    new_key = await _claim(h.server, "alice")
    assert new_key and new_key != alice_key
    h.registry.unbind_name("alice")
    await h.request(sid, key=alice_key, n=3)
    await h.sweep(2)
    assert h.registry.get("alice") is None, (
        "a transport tagged by a reset key must not bind after a re-claim")
