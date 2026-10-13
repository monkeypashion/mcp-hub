"""MH-H1 (audit 2026-10-13): /mcp has no identity. Any caller that can reach
the hub can register as any agent name, and read or empty any inbox.

The scheme pinned here is `tofu-agent-key`: no secret is placed by hand.

  * The hub MINTS a per-agent key the first time a name claims one
    (`claim_agent_key`, trust-on-first-use), stores only its sha256, and
    returns it exactly once. The CLI (`mcp-hub agent-key ensure`) persists it
    0600 before any use; an interactive session may not claim, so a model
    never sees a key.
  * The key travels in the `x-mcp-hub-agent-key` header on every request.
  * A name's key is ENFORCED only once it is ARMED: the hub has seen a
    register() present it. A lane whose interactive transport does not carry
    the header yet is never locked out by its own hook having claimed a key.
  * `MCP_HUB_AGENT_KEYS` = off | observe | enforce, default OFF: deploying the
    code issues no key. Observe mints and records every would-be refusal and
    never refuses. The tests run with keys on (observe) unless they say not.

RED tests fail today by assertion; GUARD tests pass today and must still
pass after the fix (the live agent keeps working, observe never refuses).
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from mcp_hub.server import create_server

AGENT_KEY_HEADER = "x-mcp-hub-agent-key"
MODE_ENV = "MCP_HUB_AGENT_KEYS"
KEY_RE = re.compile(r"AGENT-KEY: ([0-9a-f]{64})")


@pytest.fixture(autouse=True)
def _keys_on(monkeypatch):
    monkeypatch.setenv(MODE_ENV, "observe")
    monkeypatch.delenv("MCP_HUB_OPERATOR_TOKEN", raising=False)


@pytest.fixture
def enforce(monkeypatch):
    monkeypatch.setenv(MODE_ENV, "enforce")


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "hub.db"


@pytest.fixture
def server(db: Path):
    return create_server(db_path=db)


class _Sess:
    _write_stream = object()

    def __init__(self, client_name: str = "") -> None:
        self.sent: list[object] = []
        if client_name:
            self.client_params = SimpleNamespace(
                clientInfo=SimpleNamespace(name=client_name))

    async def send_ping(self):
        return None

    async def send_notification(self, notification):
        self.sent.append(notification)


class _Req:
    def __init__(self, headers: dict[str, str]):
        self.headers = {k.lower(): v for k, v in headers.items()}
        self.client = SimpleNamespace(host="100.64.0.9", port=1)


class _Ctx:
    """An HTTP request's context: a transport session plus its headers."""

    def __init__(self, session=None, headers: dict[str, str] | None = None):
        self.session = session or _Sess()
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


async def _call_opt(server, name, args, ctx=None) -> str:
    """A tool the fix adds: absent today reads as an empty answer."""
    try:
        return await _call(server, name, args, ctx)
    except Exception as exc:  # noqa: BLE001 - ToolError("Unknown tool: ...")
        if "Unknown tool" in str(exc):
            return ""
        raise


def _conn(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    return conn


def _unread(db: Path, to: str) -> int:
    return _conn(db).execute(
        "SELECT COUNT(*) FROM messages WHERE to_agent=? AND read=0", (to,)
    ).fetchone()[0]


async def _claim(server, name, ctx=None) -> tuple[str, str]:
    out = await _call_opt(server, "claim_agent_key", {"name": name},
                          ctx or _Ctx())
    m = KEY_RE.search(out)
    return out, (m.group(1) if m else "")


async def _keyed(server, name, squads=""):
    """The rollout path for one lane: the CLI claims (unbound HTTP client),
    then the lane's interactive session registers presenting the key, which
    ARMS the name."""
    _out, key = await _claim(server, name)
    sess = _Sess("claude-code")
    ctx = _Ctx(sess, {AGENT_KEY_HEADER: key})
    await _call(server, "register",
                {"name": name, "project": "p", "squads": squads}, ctx)
    return key, ctx, sess


async def _setup_dm(server, to="alice", body="secret plan"):
    await _call(server, "register", {"name": "bob", "project": "p"})
    await _call(server, "send", {"from_agent": "bob", "to": to, "message": body})


# =============================================================================
# RED: the scheme exists and holds
# =============================================================================


async def test_a_new_name_claims_its_key_exactly_once(server):
    first, key = await _claim(server, "alice")
    assert key, f"a never-seen name must be able to claim a key: {first!r}"
    again, key2 = await _claim(server, "alice")
    assert again.startswith("REFUSED") and not key2, again


async def test_the_hub_stores_only_a_hash_of_the_key(server, db):
    _out, key = await _claim(server, "alice")
    assert key, "no key was minted"
    raw = b"".join(p.read_bytes() for p in db.parent.glob(db.name + "*"))
    assert key.encode() not in raw, "the raw key must never be stored"
    assert hashlib.sha256(key.encode()).hexdigest().encode() in raw


async def test_an_interactive_session_cannot_claim_so_no_model_sees_a_key(server):
    out, key = await _claim(server, "alice", _Ctx(_Sess("claude-code")))
    assert out.startswith("REFUSED") and not key, out


async def test_operator_names_cannot_be_claimed(server):
    for name in ("operator", "operator-console"):
        out, key = await _claim(server, name)
        assert out.startswith("REFUSED") and not key, (name, out)


async def test_second_caller_cannot_register_as_an_armed_name(server, enforce):
    _key, _ctx, owner = await _keyed(server, "alice")
    for headers in ({}, {AGENT_KEY_HEADER: "0" * 64}):
        out = await _call(server, "register", {"name": "alice", "project": "p"},
                          _Ctx(_Sess("claude-code"), headers))
        assert out.startswith("REFUSED"), (headers, out)
    assert server._hub_registry.get("alice") is owner, (
        "the wake binding must still be the key holder's")


async def test_second_caller_cannot_drain_an_armed_inbox(server, db, enforce):
    await _keyed(server, "alice")
    await _setup_dm(server)
    out = await _call(server, "get_messages",
                      {"agent_name": "alice", "bind": False}, _Ctx())
    assert out.startswith("REFUSED"), out
    assert "secret plan" not in out
    assert _unread(db, "alice") == 1, "a refused drain must mark nothing read"


async def test_second_caller_cannot_advance_an_armed_broadcast_cursor(
    server, db, enforce
):
    await _keyed(server, "alice", squads="sq")
    await _call(server, "register", {"name": "bob", "project": "p", "squads": "sq"})
    await _call(server, "broadcast", {"from_agent": "bob", "message": "to sq"})
    before = _conn(db).execute(
        "SELECT last_broadcast_seen_id FROM agents WHERE name='alice'"
    ).fetchone()[0]
    out = await _call(server, "get_broadcasts_for_agent",
                      {"agent_name": "alice"}, _Ctx())
    assert out.startswith("REFUSED"), out
    after = _conn(db).execute(
        "SELECT last_broadcast_seen_id FROM agents WHERE name='alice'"
    ).fetchone()[0]
    assert after == before


async def test_second_caller_cannot_read_an_armed_history(server, enforce):
    await _keyed(server, "alice")
    await _setup_dm(server)
    out = await _call(server, "get_history", {"agent_or_channel": "alice"},
                      _Ctx())
    assert out.startswith("REFUSED") and "secret plan" not in out, out


async def test_second_caller_cannot_send_as_an_armed_name(server, db, enforce):
    await _keyed(server, "alice")
    await _call(server, "register", {"name": "bob", "project": "p"})
    out = await _call(server, "send",
                      {"from_agent": "alice", "to": "bob", "message": "forged"},
                      _Ctx())
    assert out.startswith("REFUSED"), out
    n = _conn(db).execute(
        "SELECT COUNT(*) FROM messages WHERE body='forged'").fetchone()[0]
    assert n == 0, "the record must not be written"


async def test_url_rebind_needs_the_key_of_an_armed_name(server, enforce):
    key, _ctx, _sess = await _keyed(server, "alice")
    allowed = getattr(server, "_hub_url_bind_allowed", None)
    assert allowed is not None, "the ?agent= rebind has no key check"
    assert allowed("alice", "") is False
    assert allowed("alice", hashlib.sha256(b"wrong").hexdigest()) is False
    assert allowed("alice", hashlib.sha256(key.encode()).hexdigest()) is True
    assert allowed("legacy-unkeyed", "") is True


async def test_an_unbound_key_holder_speaks_verified(server, db):
    key, _ctx, _sess = await _keyed(server, "alice")
    await _call(server, "register", {"name": "bob", "project": "p"})
    # The Stop hook / daemon shape: an unbound HTTP client presenting the key.
    await _call(server, "send",
                {"from_agent": "alice", "to": "bob", "message": "from my hook"},
                _Ctx(headers={AGENT_KEY_HEADER: key}))
    grade = _conn(db).execute(
        "SELECT attribution FROM messages WHERE body='from my hook'"
    ).fetchone()[0]
    assert grade == "key-verified", grade
    out = await _call(server, "get_messages", {"agent_name": "bob", "bind": False})
    assert "**alice** ·verified" in out, out


async def test_observe_mode_records_the_would_be_refusal(server):
    await _keyed(server, "alice")
    await _setup_dm(server)
    await _call(server, "get_messages", {"agent_name": "alice", "bind": False},
                _Ctx())
    status = await _call(server, "hub_status", {})
    m = re.search(r"Agent keys: mode=observe.*would-refuse (\d+)", status)
    assert m and int(m.group(1)) >= 1, status


async def test_a_second_claim_is_surfaced_as_a_conflict(server):
    await _claim(server, "alice")
    await _claim(server, "alice")
    status = await _call(server, "hub_status", {})
    m = re.search(r"claim conflicts (\d+)", status)
    assert m and int(m.group(1)) == 1, status


async def test_a_key_reset_is_local_or_operator_only(server, monkeypatch):
    await _claim(server, "alice")
    remote = await _call_opt(server, "reset_agent_key", {"name": "alice"},
                             _Ctx())
    assert remote.startswith("REFUSED"), remote
    local = await _call_opt(server, "reset_agent_key", {"name": "alice"})
    assert local.startswith("Reset"), local
    _out, key = await _claim(server, "alice")
    assert key, "after a reset the name can be claimed again"


# =============================================================================
# GUARDS: green today, green after
# =============================================================================


async def test_guard_the_original_agent_keeps_working_under_enforce(
    server, db, enforce
):
    key, ctx, sess = await _keyed(server, "alice")
    await _setup_dm(server, body="hello alice")
    # Its own bound session: drains, speaks, re-registers.
    assert "hello alice" in await _call(
        server, "get_messages", {"agent_name": "alice", "bind": False}, ctx)
    out = await _call(server, "send",
                      {"from_agent": "alice", "to": "bob", "message": "hi"}, ctx)
    assert not out.startswith("REFUSED"), out
    out = await _call(server, "register", {"name": "alice", "project": "p"}, ctx)
    assert out.startswith("Registered"), out
    assert server._hub_registry.get("alice") is sess
    # Its Stop hook: unbound, presenting the key.
    await _call(server, "send",
                {"from_agent": "bob", "to": "alice", "message": "again"})
    hook = _Ctx(headers={AGENT_KEY_HEADER: key})
    assert "again" in await _call(
        server, "get_messages", {"agent_name": "alice", "bind": False}, hook)
    # Its next session (a restart) registers with the key and takes over.
    fresh = _Sess("claude-code")
    out = await _call(server, "register", {"name": "alice", "project": "p"},
                      _Ctx(fresh, {AGENT_KEY_HEADER: key}))
    assert out.startswith("Registered"), out
    assert server._hub_registry.get("alice") is fresh


@pytest.mark.parametrize("mode", ["", "observe", "off", "bogus"])
async def test_guard_no_mode_but_enforce_ever_refuses(server, db, monkeypatch,
                                                      mode):
    await _keyed(server, "alice")   # claimed while keys are on (observe)
    if mode:
        monkeypatch.setenv(MODE_ENV, mode)
    else:
        monkeypatch.delenv(MODE_ENV, raising=False)
    await _setup_dm(server, body="readable")
    stranger = _Ctx(_Sess("claude-code"))
    out = await _call(server, "get_messages",
                      {"agent_name": "alice", "bind": False}, _Ctx())
    assert "readable" in out, out
    out = await _call(server, "send",
                      {"from_agent": "alice", "to": "bob", "message": "x"}, _Ctx())
    assert not out.startswith("REFUSED"), out
    out = await _call(server, "register", {"name": "alice", "project": "p"},
                      stranger)
    assert out.startswith("Registered"), out


async def test_guard_an_unkeyed_legacy_agent_works_under_enforce(
    server, enforce
):
    await _call(server, "register", {"name": "alice", "project": "p"},
                _Ctx(_Sess("claude-code")))
    await _setup_dm(server, body="legacy ok")
    out = await _call(server, "get_messages",
                      {"agent_name": "alice", "bind": False}, _Ctx())
    assert "legacy ok" in out
    out = await _call(server, "send",
                      {"from_agent": "alice", "to": "bob", "message": "x"}, _Ctx())
    assert not out.startswith("REFUSED"), out


async def test_guard_a_claimed_but_unarmed_name_is_not_locked_out(
    server, enforce
):
    # The lane's hook claimed a key, but its interactive transport does not
    # carry the header yet: it must keep registering and draining.
    sess = _Sess("claude-code")
    await _call(server, "register", {"name": "alice", "project": "p"},
                _Ctx(sess))
    await _claim(server, "alice")
    await _setup_dm(server, body="still mine")
    after_restart = _Sess("claude-code")
    out = await _call(server, "register", {"name": "alice", "project": "p"},
                      _Ctx(after_restart))
    assert out.startswith("Registered"), out
    out = await _call(server, "get_messages",
                      {"agent_name": "alice", "bind": False}, _Ctx(after_restart))
    assert "still mine" in out, out


async def test_guard_a_session_bound_before_its_key_keeps_working(
    server, enforce
):
    sess = _Sess("claude-code")
    ctx = _Ctx(sess)
    await _call(server, "register", {"name": "alice", "project": "p"}, ctx)
    await _claim(server, "alice")
    await _setup_dm(server, body="continuity")
    out = await _call(server, "get_messages",
                      {"agent_name": "alice", "bind": False}, ctx)
    assert "continuity" in out, out


async def test_guard_local_stdio_calls_are_trusted_under_enforce(server, enforce):
    await _keyed(server, "alice")
    await _setup_dm(server, body="local")
    out = await _call(server, "get_messages", {"agent_name": "alice", "bind": False})
    assert "local" in out, out


@pytest.mark.parametrize("mode", ["", "off", "bogus"])
async def test_with_keys_off_no_key_is_minted(server, db, monkeypatch, mode):
    """Deploying the code issues no credential: until the mode is switched on
    deliberately, a claim is refused and agent_keys stays empty."""
    if mode:
        monkeypatch.setenv(MODE_ENV, mode)
    else:
        monkeypatch.delenv(MODE_ENV, raising=False)
    out = await _call(server, "claim_agent_key", {"name": "alice"}, _Ctx())
    assert out.startswith("REFUSED"), out
    assert "AGENT-KEY" not in out
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM agent_keys").fetchone()[0] == 0
