"""Tests for compact Stop-hook rendering of the inbox.

The Stop hook fires at EVERY turn boundary and its output lands verbatim in
the agent's context. Messages pushed live are deliberately left unread (push
success != seen — see PR #8), so without compaction every DM is rendered
twice in full: once live, once reprinted.

`get_messages(compact=True)` shortens two cases:
  1. the message was pushed to the binding generation the agent STILL holds
     (positive evidence it surfaced live), and
  2. bulk beyond COMPACT_FULL_MESSAGES.

The invariant that matters more than either economy: **nothing is ever
dropped, only ever shortened** — and anything with the slightest doubt about
live delivery is reprinted in full. These tests pin that invariant.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from mcp_hub.server import COMPACT_FULL_MESSAGES, create_server


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
    """Stand-in for a bound ServerSession — identity is all the registry uses."""


BODY = "line one of the body\nline two which should not appear when summarised"


async def _setup(server, *, bind: bool = True):
    registry = server._hub_registry  # type: ignore[attr-defined]
    await _call_tool(server, "register", {"name": "bob", "project": "p"})
    await _call_tool(server, "register", {"name": "alice", "project": "p"})
    if bind:
        registry.bind("bob", _FakeSess())
    return registry


async def _send(server, body: str = BODY, pushed: bool = True):
    with patch.object(
        server._hub_registry,  # type: ignore[attr-defined]
        "push",
        AsyncMock(return_value=pushed),
    ):
        return await _call_tool(
            server,
            "send",
            {"from_agent": "alice", "to": "bob", "message": body},
        )


async def test_live_delivered_message_is_summarised_not_reprinted(server):
    """The complaint: a DM the agent already saw live gets reprinted in full
    at the next Stop boundary. Compact mode must collapse it to one line."""
    await _setup(server)
    await _send(server)

    out = await _call_tool(
        server, "get_messages", {"agent_name": "bob", "bind": False, "compact": True}
    )

    assert "already delivered live" in out
    assert "line two" not in out, "full body was reprinted despite live delivery"
    assert "alice" in out, "sender must still be identifiable"


async def test_rebind_forces_full_reprint(server):
    """If the agent rebound after the push, the push may have gone into a
    stream that died — the exact case that silently destroyed messages before.
    Doubt must resolve to a FULL reprint."""
    registry = await _setup(server)
    await _send(server)

    registry.bind("bob", _FakeSess())  # new session => new generation

    out = await _call_tool(
        server, "get_messages", {"agent_name": "bob", "bind": False, "compact": True}
    )

    assert "line two" in out, "message must be reprinted in full after a rebind"
    assert "already delivered live" not in out


async def test_unbound_recipient_forces_full_reprint(server):
    """No binding at pull time => no evidence of live delivery => full text."""
    registry = await _setup(server)
    await _send(server)
    registry.unbind_name("bob")

    out = await _call_tool(
        server, "get_messages", {"agent_name": "bob", "bind": False, "compact": True}
    )
    assert "line two" in out


async def test_queued_never_pushed_message_is_printed_in_full(server):
    """A message that never reached a live stream is the whole reason the
    Stop-hook pull exists. It must never be summarised away."""
    await _setup(server)
    await _send(server, pushed=False)

    out = await _call_tool(
        server, "get_messages", {"agent_name": "bob", "bind": False, "compact": True}
    )
    assert "line two" in out
    assert "already delivered live" not in out


async def test_bulk_beyond_budget_is_summarised_but_present(server):
    """Bulk cap: bodies beyond the budget are shortened, but every sender and
    timestamp still appears — shortened, not dropped."""
    await _setup(server, bind=False)  # unbound => nothing counts as live-seen
    total = COMPACT_FULL_MESSAGES + 3
    for i in range(total):
        await _send(server, body=f"msg{i} first line\nmsg{i} second line", pushed=False)

    out = await _call_tool(
        server, "get_messages", {"agent_name": "bob", "bind": False, "compact": True}
    )

    assert out.count("first line") == total, "every message must still be listed"
    full = sum(1 for i in range(total) if f"msg{i} second line" in out)
    assert full == COMPACT_FULL_MESSAGES


async def test_compact_off_is_byte_for_byte_unchanged(server):
    """Interactive callers (an agent running get_messages itself) must see the
    old behaviour exactly — compaction is a Stop-hook concern only."""
    await _setup(server)
    await _send(server)

    out = await _call_tool(
        server, "get_messages", {"agent_name": "bob", "bind": False}
    )
    assert "line two" in out
    assert "already delivered live" not in out


async def test_messages_are_still_marked_read_when_summarised(server):
    """Summarising must not change read semantics: the row is consumed, so the
    next pull is empty rather than repeating forever."""
    await _setup(server)
    await _send(server)

    first = await _call_tool(
        server, "get_messages", {"agent_name": "bob", "bind": False, "compact": True}
    )
    assert first
    second = await _call_tool(
        server, "get_messages", {"agent_name": "bob", "bind": False, "compact": True}
    )
    assert second == ""
