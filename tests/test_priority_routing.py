"""Tests for priority-aware send/broadcast routing.

The contract: priority controls whether the hub fires a channel-push wake.
- "low":    inbox only, no wake
- "normal": inbox + wake (default)
- "urgent": inbox + wake (with priority="urgent" surfaced in meta)

These tests bypass the MCP transport layer and call tool functions directly
via the FastMCP tool manager — that's enough to validate the routing logic
without spinning up a real client.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from mcp_hub.server import _NO_WAKE_PRIORITIES, _VALID_PRIORITIES, create_server


@pytest.fixture
def server(tmp_path: Path):
    """Fresh in-memory server per test."""
    db = tmp_path / "test.db"
    return create_server(db_path=db)


async def _call_tool(server, name: str, args: dict) -> str:
    """Invoke a registered tool by name with the given args; return its
    string result. Strips the FastMCP content wrapper down to the raw text."""
    result = await server._tool_manager.call_tool(name, args)
    # FastMCP returns either a list of content blocks or a structured result;
    # extract the text payload.
    if hasattr(result, "content"):
        for block in result.content:
            if hasattr(block, "text"):
                return block.text
    if isinstance(result, list):
        for block in result:
            if hasattr(block, "text"):
                return block.text
    if isinstance(result, str):
        return result
    return str(result)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


async def test_send_rejects_invalid_priority(server):
    out = await _call_tool(
        server, "send",
        {"from_agent": "alice", "to": "bob", "message": "hi", "priority": "extreme"},
    )
    assert "Invalid priority" in out
    assert "extreme" in out


async def test_broadcast_rejects_invalid_priority(server):
    out = await _call_tool(
        server, "broadcast",
        {"from_agent": "alice", "message": "hi", "priority": "spicy"},
    )
    assert "Invalid priority" in out


# ---------------------------------------------------------------------------
# Low priority skips channel push
# ---------------------------------------------------------------------------


async def test_send_low_priority_skips_channel_push(server):
    """A low-priority send must NOT call push_channel (not even an attempt
    against an unbound recipient). It just queues to inbox."""
    registry = server._hub_registry  # type: ignore[attr-defined]
    with patch.object(registry, "push", AsyncMock(return_value=False)) as push:
        out = await _call_tool(
            server, "send",
            {"from_agent": "alice", "to": "bob", "message": "fyi", "priority": "low"},
        )
    push.assert_not_called()
    assert "no wake" in out.lower()
    assert "low" in out


async def test_broadcast_low_priority_skips_channel_push(server):
    registry = server._hub_registry  # type: ignore[attr-defined]
    with patch.object(registry, "push", AsyncMock(return_value=False)) as push:
        out = await _call_tool(
            server, "broadcast",
            {"from_agent": "alice", "message": "EOD recap", "priority": "low"},
        )
    push.assert_not_called()
    assert "no wake" in out.lower()


# ---------------------------------------------------------------------------
# Normal / urgent priorities push with priority in meta
# ---------------------------------------------------------------------------


async def test_send_normal_priority_pushes_with_meta(server):
    registry = server._hub_registry  # type: ignore[attr-defined]
    with patch.object(registry, "push", AsyncMock(return_value=True)) as push:
        await _call_tool(
            server, "send",
            {"from_agent": "alice", "to": "bob", "message": "hi"},  # default priority
        )
    push.assert_called_once()
    _, kwargs = push.call_args
    notification = kwargs.get("notification") or push.call_args.args[1]
    # Verify priority="normal" is in the channel notification's meta
    assert notification.params["meta"]["priority"] == "normal"
    assert notification.params["meta"]["from_agent"] == "alice"


async def test_send_urgent_priority_pushes_with_urgent_in_meta(server):
    registry = server._hub_registry  # type: ignore[attr-defined]
    with patch.object(registry, "push", AsyncMock(return_value=True)) as push:
        await _call_tool(
            server, "send",
            {
                "from_agent": "alice",
                "to": "bob",
                "message": "production down",
                "priority": "urgent",
            },
        )
    push.assert_called_once()
    notification = push.call_args.args[1]
    assert notification.params["meta"]["priority"] == "urgent"


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


async def test_low_priority_message_still_persisted_to_inbox(server):
    """Low priority skips wake but the message MUST still land in the inbox.
    Otherwise we'd lose messages, not just defer them."""
    registry = server._hub_registry  # type: ignore[attr-defined]
    with patch.object(registry, "push", AsyncMock(return_value=False)):
        await _call_tool(
            server, "send",
            {"from_agent": "alice", "to": "bob", "message": "fyi", "priority": "low"},
        )

    # bob's inbox should contain the message
    out = await _call_tool(server, "get_messages", {"agent_name": "bob"})
    assert "fyi" in out
    assert "alice" in out
    # And the priority tag should be visible
    assert "[low]" in out


async def test_normal_priority_message_no_priority_tag_in_output(server):
    """Cleanliness: normal-priority messages don't show a [normal] tag."""
    registry = server._hub_registry  # type: ignore[attr-defined]
    with patch.object(registry, "push", AsyncMock(return_value=False)):
        await _call_tool(
            server, "send",
            {"from_agent": "alice", "to": "bob", "message": "regular"},
        )

    out = await _call_tool(server, "get_messages", {"agent_name": "bob"})
    assert "regular" in out
    assert "[normal]" not in out  # no clutter for the default
    assert "[low]" not in out


async def test_send_marks_message_read_when_push_succeeds(server):
    """When channel-push delivers successfully, the recipient saw the message
    inline as a <channel> event — content is already in their context. The
    DB row must be marked read=1 so Stop-hook auto-pulls and explicit
    get_messages don't re-surface it. Without this fix, every successfully-
    pushed DM gets delivered twice (once via channel push, once via inbox)."""
    registry = server._hub_registry  # type: ignore[attr-defined]
    # Simulate a successful channel push
    with patch.object(registry, "push", AsyncMock(return_value=True)):
        await _call_tool(
            server, "send",
            {"from_agent": "alice", "to": "bob", "message": "delivered via push"},
        )

    # bob's inbox should be EMPTY because the message was delivered live and
    # marked read on the way out. Re-delivery would cause double-processing.
    out = await _call_tool(server, "get_messages", {"agent_name": "bob"})
    assert out == ""


async def test_send_keeps_message_unread_when_push_fails(server):
    """If push fails (recipient offline / zombie), the message MUST stay
    unread so the recipient picks it up via the inbox path on next register
    or Stop-hook auto-pull. This is the load-bearing case for the wake-path-
    drift recovery: agents who've drifted off ⚡ rely on inbox catch-up."""
    registry = server._hub_registry  # type: ignore[attr-defined]
    with patch.object(registry, "push", AsyncMock(return_value=False)):
        await _call_tool(
            server, "send",
            {"from_agent": "alice", "to": "bob", "message": "queued for offline bob"},
        )

    out = await _call_tool(server, "get_messages", {"agent_name": "bob"})
    assert "queued for offline bob" in out


async def test_low_priority_does_not_mark_read(server):
    """Low priority skips push entirely. The message MUST stay unread so
    the recipient sees it on their next register / get_messages call —
    that's the whole point of low-priority queueing."""
    registry = server._hub_registry  # type: ignore[attr-defined]
    with patch.object(registry, "push", AsyncMock(return_value=False)) as push:
        await _call_tool(
            server, "send",
            {"from_agent": "alice", "to": "bob", "message": "fyi", "priority": "low"},
        )
    # Sanity: low priority skipped push entirely
    push.assert_not_called()

    out = await _call_tool(server, "get_messages", {"agent_name": "bob"})
    assert "fyi" in out


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------


def test_no_wake_priorities_subset_of_valid():
    """Sanity: every no-wake priority must be a valid priority."""
    assert _NO_WAKE_PRIORITIES <= _VALID_PRIORITIES


def test_valid_priorities_includes_expected():
    assert {"low", "normal", "urgent"} <= _VALID_PRIORITIES


def test_normal_is_not_no_wake():
    """The default priority must wake — that's the load-bearing default."""
    assert "normal" not in _NO_WAKE_PRIORITIES


def test_urgent_is_not_no_wake():
    """Urgent priority must always wake — the whole point."""
    assert "urgent" not in _NO_WAKE_PRIORITIES


# ---------------------------------------------------------------------------
# Channels & post()
# ---------------------------------------------------------------------------


async def test_create_channel_then_post(server):
    out = await _call_tool(
        server, "create_channel",
        {"name": "deploys", "created_by": "alice", "description": "deploy chatter"},
    )
    assert "created" in out.lower()

    registry = server._hub_registry  # type: ignore[attr-defined]
    # Bind a fake "bob" session so post() has someone to iterate over.
    # Without this, registry.names() is empty and no push is attempted.
    class _FakeSess:
        async def send_ping(self): ...
        async def send_notification(self, _n): ...

    registry.bind("bob", _FakeSess())

    with patch.object(registry, "push", AsyncMock(return_value=False)) as push:
        out = await _call_tool(
            server, "post",
            {"from_agent": "alice", "channel": "deploys", "message": "shipping"},
        )
    push.assert_called_once()
    # Verify the channel is in the meta on the rendered tag
    notification = push.call_args.args[1]
    assert notification.params["meta"]["channel"] == "deploys"
    assert notification.params["meta"]["kind"] == "post"
    assert "deploys" in out


async def test_post_to_nonexistent_channel_rejected(server):
    """Channel must pre-exist; no silent auto-creation (so typos don't
    accumulate phantom channels)."""
    out = await _call_tool(
        server, "post",
        {"from_agent": "alice", "channel": "ghost-channel", "message": "?"},
    )
    assert "not found" in out.lower()
    assert "create_channel" in out


async def test_post_low_priority_skips_wake(server):
    await _call_tool(
        server, "create_channel",
        {"name": "deploys", "created_by": "alice"},
    )
    registry = server._hub_registry  # type: ignore[attr-defined]
    with patch.object(registry, "push", AsyncMock(return_value=False)) as push:
        out = await _call_tool(
            server, "post",
            {
                "from_agent": "alice",
                "channel": "deploys",
                "message": "fyi shipped 1.2.3",
                "priority": "low",
            },
        )
    push.assert_not_called()
    assert "no wake" in out.lower()


async def test_post_rejects_invalid_priority(server):
    await _call_tool(
        server, "create_channel",
        {"name": "deploys", "created_by": "alice"},
    )
    out = await _call_tool(
        server, "post",
        {
            "from_agent": "alice",
            "channel": "deploys",
            "message": "x",
            "priority": "spicy",
        },
    )
    assert "Invalid priority" in out


async def test_create_channel_rejects_reserved_general(server):
    """'general' is the global broadcast feed; users can't claim it as a
    regular channel name."""
    out = await _call_tool(
        server, "create_channel",
        {"name": "general", "created_by": "alice"},
    )
    assert "reserved" in out.lower()
    assert "broadcast" in out.lower()


async def test_post_to_general_rejected(server):
    """Posting to 'general' via post() routes to broadcast() instead.
    Reject with a hint."""
    out = await _call_tool(
        server, "post",
        {"from_agent": "alice", "channel": "general", "message": "hi"},
    )
    assert "broadcast" in out.lower()


async def test_list_channels_excludes_general(tmp_path):
    """The broadcast feed is not a channel and shouldn't appear in
    list_channels output, even if a legacy 'general' row exists in the
    channels table from before the reservation rule landed."""
    import time as _time

    from mcp_hub.server import _get_db, create_server

    db_path = tmp_path / "test.db"
    # Seed a legacy 'general' row before the server boots, simulating data
    # that pre-dates the reservation rule.
    server = create_server(db_path=db_path)
    conn = _get_db(db_path)
    conn.execute(
        "INSERT OR IGNORE INTO channels (name, created_by, created_at, description) "
        "VALUES (?, ?, ?, ?)",
        ("general", "legacy", _time.time(), "legacy"),
    )
    conn.commit()

    await _call_tool(
        server, "create_channel",
        {"name": "deploys", "created_by": "alice"},
    )
    out = await _call_tool(server, "list_channels", {})
    assert "deploys" in out
    assert "general" not in out


async def test_create_channel_idempotent_for_existing(server):
    await _call_tool(
        server, "create_channel",
        {"name": "deploys", "created_by": "alice"},
    )
    out = await _call_tool(
        server, "create_channel",
        {"name": "deploys", "created_by": "bob"},
    )
    assert "already exists" in out.lower()
