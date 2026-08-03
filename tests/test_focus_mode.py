"""Focus mode — the third state the hub was missing.

The hub models "in a turn" and "idle", and treats idle as safe to interrupt.
An agent watching a deploy or tailing a log is idle-at-the-keyboard and
operationally busy, and the hub cannot see that kind of busy. Until now the
only defence was a convention asking senders to hold off, which fails exactly
when the fleet is busy enough to need it.

Three properties matter more than the feature, and all three are pinned here:

  NOTHING IS DROPPED — focus delays a message, it never loses one.

  THE GATE IS UNIVERSAL — DMs, posts and broadcasts funnel through one push
  path. A silencer covering four of five routes is worse than none: it gets
  trusted.

  EVERY RECIPIENT HERE IS BOUND. A "no wake fired" assertion against an
  unbound agent proves nothing — it passes with focus deleted entirely, since
  push_channel returns early when there is no session. Binding first is what
  makes the absence of a wake attributable to focus.
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from mcp_hub.server import FOCUS_MAX_MINUTES, create_server


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "focus.db"


@pytest.fixture
def server(db_path: Path):
    return create_server(db_path=db_path)


class _FakeSess:
    async def send_ping(self): ...
    async def send_notification(self, _n): ...


async def _call(server, name: str, args: dict) -> str:
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


async def _register(server, *names):
    for n in names:
        await _call(server, "register", {"name": n, "project": "p"})


def _bind(server, *names):
    """Give each agent a live session — without this, 'no wake' is vacuous."""
    registry = server._hub_registry  # type: ignore[attr-defined]
    for n in names:
        registry.bind(n, _FakeSess())
    return registry


def _db(db_path):
    from mcp_hub.server import _get_db

    return _get_db(db_path)


def _focus_for(db_path, agent, seconds):
    """Set focus directly, so tests never wait on a wall clock."""
    conn = _db(db_path)
    conn.execute(
        "UPDATE agents SET focus_until = ? WHERE name = ?",
        (time.time() + seconds, agent),
    )
    conn.commit()


class TestTheGuardIsRealNotVacuous:
    async def test_a_BOUND_unfocused_agent_definitely_wakes(self, server):
        """The control. If this ever fails, every 'no wake' test below is
        measuring the binding rather than the focus."""
        await _register(server, "alice", "bob")
        registry = _bind(server, "bob")
        with patch.object(registry, "push", AsyncMock(return_value=True)) as push:
            await _call(server, "send", {
                "from_agent": "alice", "to": "bob", "message": "hi",
                "priority": "normal",
            })
        assert push.called


class TestTheGate:
    async def test_a_normal_dm_does_not_wake_a_focused_agent(self, server, db_path):
        await _register(server, "alice", "bob")
        registry = _bind(server, "bob")
        _focus_for(db_path, "bob", 600)
        with patch.object(registry, "push", AsyncMock(return_value=True)) as push:
            out = await _call(server, "send", {
                "from_agent": "alice", "to": "bob", "message": "hi",
                "priority": "normal",
            })
        push.assert_not_called()
        assert "focus mode" in out
        assert "NOT offline" in out           # the sender must not go hunting

    async def test_but_the_message_is_STILL_THERE(self, server, db_path):
        """The whole safety claim. Focus delays; it must never drop."""
        await _register(server, "alice", "bob")
        _bind(server, "bob")
        _focus_for(db_path, "bob", 600)
        await _call(server, "send", {
            "from_agent": "alice", "to": "bob", "message": "the payload",
            "priority": "normal",
        })
        inbox = await _call(server, "get_messages", {"agent_name": "bob"})
        assert "the payload" in inbox

    async def test_urgent_PIERCES_focus(self, server, db_path):
        """A focus that swallowed a production incident is one nobody would
        dare switch on."""
        await _register(server, "alice", "bob")
        registry = _bind(server, "bob")
        _focus_for(db_path, "bob", 600)
        with patch.object(registry, "push", AsyncMock(return_value=True)) as push:
            await _call(server, "send", {
                "from_agent": "alice", "to": "bob", "message": "prod is down",
                "priority": "urgent",
            })
        assert push.called

    async def test_an_EXPIRED_focus_wakes_normally(self, server, db_path):
        """Expiry is the safety design — a forgotten focus lapses by itself,
        with no sweeper to fail and nothing to remember to undo."""
        await _register(server, "alice", "bob")
        registry = _bind(server, "bob")
        _focus_for(db_path, "bob", -1)          # already in the past
        with patch.object(registry, "push", AsyncMock(return_value=True)) as push:
            await _call(server, "send", {
                "from_agent": "alice", "to": "bob", "message": "hi",
                "priority": "normal",
            })
        assert push.called

    async def test_focus_does_not_leak_to_another_agent(self, server, db_path):
        await _register(server, "alice", "bob", "carol")
        registry = _bind(server, "bob", "carol")
        _focus_for(db_path, "bob", 600)
        with patch.object(registry, "push", AsyncMock(return_value=True)) as push:
            await _call(server, "send", {
                "from_agent": "alice", "to": "carol", "message": "hi",
                "priority": "normal",
            })
        assert push.called


class TestEveryRouteIsCovered:
    """A silencer with a hole in it is worse than none — it gets trusted."""

    async def test_a_channel_post_does_not_wake_a_focused_subscriber(
        self, server, db_path
    ):
        await _register(server, "alice", "bob")
        registry = _bind(server, "bob")
        await _call(server, "create_channel", {
            "name": "eng", "created_by": "alice", "description": "d",
        })
        await _call(server, "subscribe_channel", {
            "name": "bob", "channel": "eng",
        })
        _focus_for(db_path, "bob", 600)
        with patch.object(registry, "push", AsyncMock(return_value=True)) as push:
            await _call(server, "post", {
                "from_agent": "alice", "channel": "eng", "message": "hi",
                "priority": "normal",
            })
        push.assert_not_called()

    async def test_a_broadcast_does_not_wake_a_focused_member(self, server, db_path):
        await _register(server, "alice", "bob")
        registry = _bind(server, "bob")
        _focus_for(db_path, "bob", 600)
        with patch.object(registry, "push", AsyncMock(return_value=True)) as push:
            await _call(server, "broadcast", {
                "from_agent": "alice", "message": "hi", "scope": "fleet",
                "priority": "normal",
            })
        push.assert_not_called()

    async def test_an_urgent_broadcast_still_reaches_a_focused_member(
        self, server, db_path
    ):
        await _register(server, "alice", "bob")
        registry = _bind(server, "bob")
        _focus_for(db_path, "bob", 600)
        with patch.object(registry, "push", AsyncMock(return_value=True)) as push:
            await _call(server, "broadcast", {
                "from_agent": "alice", "message": "incident", "scope": "fleet",
                "priority": "urgent",
            })
        assert push.called


class TestTheTool:
    async def test_focus_on_reports_the_window_and_that_it_self_expires(self, server):
        await _register(server, "bob")
        out = await _call(server, "focus", {"agent_name": "bob", "minutes": 30})
        assert "30m" in out
        assert "urgent still gets through" in out
        assert "expires on its own" in out

    async def test_focus_zero_ends_it(self, server):
        await _register(server, "bob")
        registry = _bind(server, "bob")
        await _call(server, "focus", {"agent_name": "bob", "minutes": 30})
        out = await _call(server, "focus", {"agent_name": "bob", "minutes": 0})
        assert "Focus off" in out
        with patch.object(registry, "push", AsyncMock(return_value=True)) as push:
            await _call(server, "send", {
                "from_agent": "a", "to": "bob", "message": "hi",
                "priority": "normal",
            })
        assert push.called

    async def test_an_absurd_duration_is_CAPPED_and_says_so(self, server, db_path):
        """An unbounded silencer is the silent-drop failure mode wearing a
        feature's clothes."""
        await _register(server, "bob")
        out = await _call(server, "focus", {"agent_name": "bob", "minutes": 100000})
        assert "capped" in out.lower()
        conn = _db(db_path)
        left = conn.execute(
            "SELECT focus_until FROM agents WHERE name = 'bob'"
        ).fetchone()["focus_until"] - time.time()
        assert left <= FOCUS_MAX_MINUTES * 60 + 5

    async def test_focusing_an_unknown_agent_is_refused(self, server):
        out = await _call(server, "focus", {"agent_name": "ghost", "minutes": 10})
        assert "No such agent" in out

    async def test_the_reason_is_carried(self, server):
        await _register(server, "bob")
        out = await _call(server, "focus", {
            "agent_name": "bob", "minutes": 10, "reason": "babysitting a deploy",
        })
        assert "babysitting a deploy" in out


class TestVisibility:
    async def test_list_agents_shows_focus_with_time_remaining(self, server, db_path):
        """A silencer nobody can see turns a delayed message into an
        apparently-ignored one."""
        await _register(server, "bob")
        _focus_for(db_path, "bob", 20 * 60)
        out = await _call(server, "list_agents", {})
        assert "🔕" in out
        assert "19m" in out or "20m" in out

    async def test_an_unfocused_agent_shows_no_marker(self, server):
        await _register(server, "bob")
        out = await _call(server, "list_agents", {})
        assert "🔕" not in out
