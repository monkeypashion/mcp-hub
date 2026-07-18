"""Tests for truthful presence markers in list_agents.

Two bugs this covers:

A. ⚡ must mean "wakeable RIGHT NOW", not merely "has a registry binding".
   A bound session whose GET /mcp listener is gone (post-/compact, cycled
   out, or never reopened after a hub redeploy) silently drops pushes — so
   it is NOT wakeable. list_agents must run the same `_can_deliver_push`
   gate push_channel uses, and only show ⚡ when a push would actually land.

B. 🟢 (DB status='online') must not linger after a session dies. The DB
   status is persistent and was only ever flipped offline by an explicit
   unregister(), so a crash / logout / redeploy left agents as stale 🟢
   forever. Two write-paths make it truthful:
     1. Startup-reset: create_server marks all agents offline at boot (a
        fresh instance has zero live sessions until they re-register).
     2. Reaper-drop: when the activity reaper gives up on a binding, it
        marks the agent offline via the registry's on_reap callback.
"""

from __future__ import annotations

from pathlib import Path

from mcp_hub.server import _get_db, create_server
from mcp_hub.session_registry import SessionRegistry


async def _call_tool(server, name: str, args: dict) -> str:
    result = await server._tool_manager.call_tool(name, args)
    if hasattr(result, "content"):
        for block in result.content:
            if hasattr(block, "text"):
                return block.text
    if isinstance(result, list):
        for block in result:
            if hasattr(block, "text"):
                return block.text
    return str(result) if result is not None else ""


class _FakeSess:
    """A session that looks bound. With no session_manager override on the
    server, `_can_deliver_push` can't introspect and returns True (pass-through
    test mode), so this counts as deliverable."""

    _write_stream = object()

    async def send_ping(self):
        return None

    async def send_notification(self, _notif):
        return None


# ---------------------------------------------------------------------------
# Bug A — ⚡ is truthful (wakeable now), not just "bound"
# ---------------------------------------------------------------------------


async def test_list_agents_shows_lightning_for_deliverable_bound_agent(tmp_path: Path):
    """Default test mode (no session_manager) → `_can_deliver_push` passes
    through True → a bound agent is shown as ⚡."""
    server = create_server(db_path=tmp_path / "test.db")
    registry = server._hub_registry  # type: ignore[attr-defined]

    await _call_tool(server, "register", {"name": "alice", "project": "x"})
    registry.bind("alice", _FakeSess())

    out = await _call_tool(server, "list_agents", {})
    assert "**alice**" in out
    assert "⚡" in out


async def test_list_agents_no_lightning_for_undeliverable_bound_agent(tmp_path: Path):
    """The core truthfulness fix: an agent that IS bound but whose session is
    not deliverable (no matching transport / no GET listener) must show 🟢
    WITHOUT ⚡ — bound is not the same as wakeable."""
    server = create_server(db_path=tmp_path / "test.db")

    # A NON-empty manager whose transports don't match the bound session's
    # write stream → the gate falls through to "transport no longer in the
    # active set" → returns False. (An *empty* manager is treated as
    # "can't introspect" and passes through True, so it must be non-empty
    # here to exercise the real bound-but-undeliverable path.)
    class _OtherTransport:
        _write_stream = object()
        _request_streams: dict = {}

    class _FakeManager:
        _server_instances = {"some-other-session": _OtherTransport()}

    server._session_manager = _FakeManager()  # type: ignore[attr-defined]
    registry = server._hub_registry  # type: ignore[attr-defined]

    await _call_tool(server, "register", {"name": "alice", "project": "x"})

    class _StaleSession:
        _write_stream = object()

        async def send_ping(self):
            return None

        async def send_notification(self, _notif):
            raise AssertionError("should not be pushed to")

    registry.bind("alice", _StaleSession())

    out = await _call_tool(server, "list_agents", {})
    # Still listed and 🟢 (register set status online), but NOT ⚡.
    assert "🟢 **alice**" in out
    assert "⚡" not in out


# ---------------------------------------------------------------------------
# Multi-clone model — distinct names coexist under one shared project
# ---------------------------------------------------------------------------


async def test_register_same_project_distinct_names_coexist(tmp_path: Path):
    """Two clones of one repo register derived names (<repo>-<hostname>)
    under the SAME project and must both exist as separate agents. The old
    one-agent-per-project dedup silently remapped the second register onto
    the first online agent — collapsing clones and hijacking the wake
    binding (observed live: statusline showed 1/1 on both machines)."""
    server = create_server(db_path=tmp_path / "test.db")

    r1 = await _call_tool(
        server, "register", {"name": "widgets-linux-box", "project": "acme/widgets"}
    )
    r2 = await _call_tool(
        server, "register", {"name": "widgets-win-box", "project": "acme/widgets"}
    )
    # Neither register may be remapped onto the other's name.
    assert "Registered as 'widgets-linux-box'" in r1
    assert "Registered as 'widgets-win-box'" in r2

    out = await _call_tool(server, "list_agents", {})
    assert "**widgets-linux-box**" in out
    assert "**widgets-win-box**" in out


async def test_same_project_clones_can_dm_each_other(tmp_path: Path):
    """The point of coexisting: clone→clone DMs address the intended clone,
    not a collapsed shared identity."""
    server = create_server(db_path=tmp_path / "test.db")

    await _call_tool(
        server, "register", {"name": "widgets-linux-box", "project": "acme/widgets"}
    )
    await _call_tool(
        server, "register", {"name": "widgets-win-box", "project": "acme/widgets"}
    )
    await _call_tool(
        server,
        "send",
        {
            "from_agent": "widgets-linux-box",
            "to": "widgets-win-box",
            "message": "memory note: prefer derived identity",
        },
    )
    inbox = await _call_tool(
        server, "get_messages", {"agent_name": "widgets-win-box"}
    )
    assert "memory note" in inbox
    # And it must NOT have landed on the sender's own queue.
    own = await _call_tool(
        server, "get_messages", {"agent_name": "widgets-linux-box"}
    )
    assert "memory note" not in own


# ---------------------------------------------------------------------------
# Stale-binding fix — heartbeats must not keep a dead binding warm
# ---------------------------------------------------------------------------


async def test_heartbeat_drops_stale_binding_and_marks_offline(tmp_path: Path):
    """The 2026-07-18 blind spot, end to end: agent bound, client reconnects
    (bound session no longer deliverable), daemon keeps heartbeating. The
    heartbeat must NOT keep refreshing the dead binding; after 3 consecutive
    undeliverable beats it drops it and the agent goes truthfully offline —
    which is what re-arms the Stop-hook re-register nag."""
    server = create_server(db_path=tmp_path / "test.db")

    # Non-empty session manager whose transports don't include the bound
    # session → _can_deliver_push returns False (bound-but-undeliverable).
    class _OtherTransport:
        _write_stream = object()
        _request_streams: dict = {}

    class _FakeManager:
        _server_instances = {"some-other-session": _OtherTransport()}

    server._session_manager = _FakeManager()  # type: ignore[attr-defined]
    registry = server._hub_registry  # type: ignore[attr-defined]

    await _call_tool(server, "register", {"name": "alice", "project": "x"})

    class _DeadSession:
        _write_stream = object()

    registry.bind("alice", _DeadSession())

    r1 = await _call_tool(server, "heartbeat", {"agent_name": "alice"})
    r2 = await _call_tool(server, "heartbeat", {"agent_name": "alice"})
    assert "not push-deliverable" in r1
    assert "not push-deliverable" in r2
    # Still bound (hysteresis), still online in the DB.
    assert registry.is_bound("alice")

    r3 = await _call_tool(server, "heartbeat", {"agent_name": "alice"})
    assert "dropped stale binding" in r3
    assert not registry.is_bound("alice")
    # on_reap marked the agent offline → list_agents no longer shows them,
    # so the agent's own Stop-hook will now fire the re-register nag.
    out = await _call_tool(server, "list_agents", {})
    assert "alice" not in out

    # A subsequent heartbeat is a plain no-op (nothing bound).
    r4 = await _call_tool(server, "heartbeat", {"agent_name": "alice"})
    assert "no binding" in r4


# ---------------------------------------------------------------------------
# Bug B — 🟢 status is truthful (no stale online)
# ---------------------------------------------------------------------------


async def test_startup_reset_marks_preexisting_agents_offline(tmp_path: Path):
    """A second create_server on the same DB (simulating a restart/redeploy)
    must mark previously-online agents offline — they are not connected to the
    fresh instance until they re-register."""
    db = tmp_path / "test.db"

    server1 = create_server(db_path=db)
    await _call_tool(server1, "register", {"name": "alice", "project": "x"})

    conn = _get_db(db)
    status_before = conn.execute(
        "SELECT status FROM agents WHERE name = 'alice'"
    ).fetchone()["status"]
    assert status_before == "online"

    # Simulate a restart: a brand-new server instance on the same DB.
    create_server(db_path=db)

    status_after = conn.execute(
        "SELECT status FROM agents WHERE name = 'alice'"
    ).fetchone()["status"]
    assert status_after == "offline", (
        "startup-reset should have marked the pre-existing agent offline — a "
        "fresh instance has no live sessions until agents re-register"
    )


async def test_list_agents_empty_after_restart_until_reregister(tmp_path: Path):
    """End-to-end of the operator-observed bug: after a 'redeploy' the default
    list_agents (online-only) shows nobody until they re-register, instead of
    lying with stale 🟢 entries."""
    db = tmp_path / "test.db"
    server1 = create_server(db_path=db)
    await _call_tool(server1, "register", {"name": "alice", "project": "x"})
    await _call_tool(server1, "register", {"name": "bob", "project": "y"})

    server2 = create_server(db_path=db)  # restart
    out = await _call_tool(server2, "list_agents", {})
    assert out == "No agents registered."

    # They come back as they re-register.
    await _call_tool(server2, "register", {"name": "alice", "project": "x"})
    out2 = await _call_tool(server2, "list_agents", {})
    assert "**alice**" in out2
    assert "**bob**" not in out2  # bob hasn't reconnected


def test_reaper_drop_marks_agent_offline(tmp_path: Path):
    """When the activity reaper drops a binding for inactivity, the on_reap
    callback must mark the agent offline in the DB so list_agents status is
    truthful (the reaper giving up = the agent is gone)."""
    db = tmp_path / "test.db"
    server = create_server(db_path=db)
    registry: SessionRegistry = server._hub_registry  # type: ignore[attr-defined]
    # The reap scenario is an ABANDONED binding: stale activity AND a dead
    # connection. Force the liveness probe to report undeliverable so the
    # reaper drops it (a still-deliverable idle binding is deliberately kept
    # now — see test_session_registry deliverability tests).
    registry._liveness_probe = lambda _s: False

    conn = _get_db(db)
    # Seed an online + bound agent directly (register requires a Context for
    # binding; here we drive the registry + DB straight).
    import time as _t

    conn.execute(
        "INSERT INTO agents (name, project, status, registered, last_seen) "
        "VALUES ('alice', 'x', 'online', ?, ?)",
        (_t.time(), _t.time()),
    )
    conn.commit()
    registry.bind("alice", _FakeSess())

    # Force the activity timestamp far into the past so the reaper drops it.
    registry._last_activity["alice"] = _t.time() - 10 * SessionRegistry.ACTIVITY_TIMEOUT_SECONDS

    survived = registry._check_one("alice")
    assert survived is False
    assert "alice" not in registry  # binding dropped

    status = conn.execute(
        "SELECT status FROM agents WHERE name = 'alice'"
    ).fetchone()["status"]
    assert status == "offline", "reaper drop should have marked alice offline"


def test_reaper_callback_survives_db_error(tmp_path: Path):
    """The reaper must not crash if the offline-mark callback raises — liveness
    sweeping is more important than the DB write succeeding."""
    db = tmp_path / "test.db"
    server = create_server(db_path=db)
    registry: SessionRegistry = server._hub_registry  # type: ignore[attr-defined]
    # Abandoned binding (dead connection) → probe reports undeliverable so the
    # reaper actually drops it and invokes the on_reap callback.
    registry._liveness_probe = lambda _s: False

    import time as _t

    registry.bind("ghost", _FakeSess())  # bound but no DB row → UPDATE no-ops
    registry._last_activity["ghost"] = _t.time() - 10 * SessionRegistry.ACTIVITY_TIMEOUT_SECONDS

    # Must not raise even though there's no 'ghost' DB row.
    survived = registry._check_one("ghost")
    assert survived is False
    assert "ghost" not in registry
