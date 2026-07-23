"""Server-level multi-session fan-out: a wake sent to one derived name reaches
EVERY live conversation bound to it, and list_agents surfaces the count.

These exercise the real `send` → `push_channel` path (not the registry in
isolation), which is where the fan-out loop and the per-session deliverability
gate live.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mcp_hub.server import create_server
from mcp_hub.session_registry import SessionRegistry


@pytest.fixture
def server(tmp_path: Path):
    db = tmp_path / "test.db"
    return create_server(db_path=db)


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
    return result if isinstance(result, str) else str(result)


class _FakeSess:
    """Records what it receives so we can prove a wake actually landed."""

    def __init__(self) -> None:
        self.sends: list = []

    async def send_ping(self): ...

    async def send_notification(self, n):
        self.sends.append(n)


class _DeadSess:
    """Send always raises — stands in for a session whose stream is dead."""

    async def send_ping(self): ...

    async def send_notification(self, n):
        raise RuntimeError("stream dead")


def _server_db(server):
    """(conn) for the server's DB — locates db_path via the register tool's
    closure (same trick as test_priority_routing)."""
    from mcp_hub.server import _get_db as _gdb

    fn = server._tool_manager._tools["register"].fn
    for name, cell in zip(fn.__code__.co_freevars, fn.__closure__):
        if name == "db_path":
            return _gdb(cell.cell_contents)
    raise AssertionError("couldn't locate db_path in register closure")


async def test_send_fans_out_to_both_sessions(server):
    """Two conversations under one derived name — a normal-priority DM wakes
    BOTH, not just whichever registered last (the old evict bug)."""
    registry = server._hub_registry  # type: ignore[attr-defined]
    await _call_tool(server, "register", {"name": "alice", "project": "x"})
    await _call_tool(server, "register", {"name": "pm", "project": "y"})

    tmux, cowork = _FakeSess(), _FakeSess()
    registry.bind("pm", tmux)
    registry.bind("pm", cowork)  # cowork primary, tmux demoted (not evicted)

    await _call_tool(
        server, "send",
        {"from_agent": "alice", "to": "pm", "message": "wake up"},
    )

    assert len(tmux.sends) == 1, "the demoted session must still be woken"
    assert len(cowork.sends) == 1, "the primary session must be woken"


async def test_list_agents_shows_session_count(server):
    registry = server._hub_registry  # type: ignore[attr-defined]
    await _call_tool(server, "register", {"name": "pm", "project": "y"})
    registry.bind("pm", _FakeSess())
    registry.bind("pm", _FakeSess())

    out = await _call_tool(server, "list_agents", {})
    assert "pm" in out
    assert "⚡×2" in out, out


async def test_single_session_marker_has_no_count(server):
    """A plain single-session agent still renders bare ⚡ — no ×1 noise."""
    registry = server._hub_registry  # type: ignore[attr-defined]
    await _call_tool(server, "register", {"name": "pm", "project": "y"})
    registry.bind("pm", _FakeSess())

    out = await _call_tool(server, "list_agents", {})
    assert "⚡" in out
    assert "×" not in out, out


async def test_dead_extra_pruned_at_push_time(server):
    """A wake to a name with a dead extra prunes the extra (its send raises)
    while still delivering to the live primary."""
    registry = server._hub_registry  # type: ignore[attr-defined]
    await _call_tool(server, "register", {"name": "alice", "project": "x"})
    await _call_tool(server, "register", {"name": "pm", "project": "y"})

    class _DeadSess:
        async def send_ping(self): ...
        async def send_notification(self, n):
            raise RuntimeError("stream dead")

    dead = _DeadSess()
    live = _FakeSess()
    registry.bind("pm", dead)   # dead becomes an extra when...
    registry.bind("pm", live)   # ...live registers as primary

    await _call_tool(
        server, "send",
        {"from_agent": "alice", "to": "pm", "message": "hi"},
    )

    assert len(live.sends) == 1          # primary got it
    assert registry.session_count("pm") == 1  # dead extra pruned
    assert registry.get("pm") is live    # primary intact


# ---------------------------------------------------------------------------
# Scar guards: don't reintroduce "push success ≠ seen"
# ---------------------------------------------------------------------------


async def test_stamp_skipped_when_only_extra_delivers(server):
    """The compact-render generation stamp is keyed to the PRIMARY's stream.
    If the primary didn't get the live push but an extra did, the message must
    NOT be stamped — else the primary conversation would summarise a message it
    never received (the PR #8 'seen when not seen' mistake in a new disguise).
    """
    registry = server._hub_registry  # type: ignore[attr-defined]
    await _call_tool(server, "register", {"name": "alice", "project": "x"})
    await _call_tool(server, "register", {"name": "pm", "project": "y"})

    live = _FakeSess()
    registry.bind("pm", live)        # live registers first...
    registry.bind("pm", _DeadSess()) # ...then a dead session becomes PRIMARY

    await _call_tool(
        server, "send",
        {"from_agent": "alice", "to": "pm", "message": "hi"},
    )
    assert len(live.sends) == 1, "the live extra should still get the wake"

    conn = _server_db(server)
    rows = conn.execute(
        "SELECT pushed_gen FROM messages WHERE to_agent = 'pm'"
    ).fetchall()
    assert rows, "message should be persisted"
    # pushed_gen defaults to '' (empty). A real stamp writes a "<boot>:<seq>"
    # token — so "unstamped" means falsy here, not literally NULL.
    assert all(not r["pushed_gen"] for r in rows), (
        "primary never received it → must fall back to full reprint, not stamp"
    )


async def test_stamp_applied_when_primary_delivers(server):
    """Positive control: primary got it → stamp is applied (compact render can
    safely summarise on the matching-generation pull)."""
    registry = server._hub_registry  # type: ignore[attr-defined]
    await _call_tool(server, "register", {"name": "alice", "project": "x"})
    await _call_tool(server, "register", {"name": "pm", "project": "y"})
    registry.bind("pm", _FakeSess())  # live primary

    await _call_tool(
        server, "send",
        {"from_agent": "alice", "to": "pm", "message": "hi"},
    )
    conn = _server_db(server)
    row = conn.execute(
        "SELECT pushed_gen FROM messages WHERE to_agent = 'pm'"
    ).fetchone()
    assert row["pushed_gen"], "primary delivered → message should carry a gen stamp"


async def test_broadcast_cursor_not_advanced_on_extra_only(server):
    """The per-agent broadcast cursor silences Stop-hook catch-up for EVERY
    session under the name. It must advance only when the PRIMARY got the push,
    or a primary that missed the live wake would never catch up."""
    registry = server._hub_registry  # type: ignore[attr-defined]
    await _call_tool(server, "register", {"name": "alice", "project": "x"})
    await _call_tool(server, "register", {"name": "pm", "project": "y"})

    live = _FakeSess()
    registry.bind("pm", live)
    registry.bind("pm", _DeadSess())  # dead session is PRIMARY

    conn = _server_db(server)
    before = conn.execute(
        "SELECT last_broadcast_seen_id FROM agents WHERE name = 'pm'"
    ).fetchone()["last_broadcast_seen_id"]

    await _call_tool(
        server, "broadcast",
        {"from_agent": "alice", "message": "fleet-wide"},
    )
    assert len(live.sends) == 1, "the live extra should still be woken"

    after = conn.execute(
        "SELECT last_broadcast_seen_id FROM agents WHERE name = 'pm'"
    ).fetchone()["last_broadcast_seen_id"]
    assert after == before, (
        "primary missed the wake → cursor must stay put so it catches up"
    )


async def test_broadcast_cursor_advances_when_primary_delivers(server):
    """Positive control: primary got the broadcast → cursor advances."""
    registry = server._hub_registry  # type: ignore[attr-defined]
    await _call_tool(server, "register", {"name": "alice", "project": "x"})
    await _call_tool(server, "register", {"name": "pm", "project": "y"})
    registry.bind("pm", _FakeSess())  # live primary

    conn = _server_db(server)
    before = conn.execute(
        "SELECT last_broadcast_seen_id FROM agents WHERE name = 'pm'"
    ).fetchone()["last_broadcast_seen_id"]

    await _call_tool(
        server, "broadcast",
        {"from_agent": "alice", "message": "fleet-wide"},
    )
    after = conn.execute(
        "SELECT last_broadcast_seen_id FROM agents WHERE name = 'pm'"
    ).fetchone()["last_broadcast_seen_id"]
    assert after > before


# ---------------------------------------------------------------------------
# unbind_name is a TERMINAL drop — no orphaned extras
# ---------------------------------------------------------------------------


def test_unbind_name_purges_extras():
    """A direct unbind_name() on a multi-session agent must leave NO session
    reachable — no half-offline state where sessions() is non-empty but
    is_bound() is False."""
    reg = SessionRegistry()
    try:
        a, b = _FakeSess(), _FakeSess()
        reg.bind("pm", a)
        reg.bind("pm", b)
        assert reg.session_count("pm") == 2
        reg.unbind_name("pm")
        assert not reg.is_bound("pm")
        assert reg.sessions("pm") == []
        assert reg.session_count("pm") == 0
    finally:
        reg.close()
