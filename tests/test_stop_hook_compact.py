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
    at the next Stop boundary. Compact mode must collapse it to one line —
    but ONLY when there's positive evidence the stream actually rendered it.
    A genuine live-render is modelled by an INDEPENDENT ack (the agent reacted
    to the wake with a tool call) before the Stop-hook drain."""
    registry = await _setup(server)
    await _send(server)
    # The agent saw the wake and reacted — an interactive ack that clears the
    # expectation before its Stop drain. THIS is what proves render (a deaf
    # stream never produces it). Without it we must fail safe (see the deaf
    # test below).
    registry.wake_ack("bob")

    out = await _call_tool(
        server, "get_messages", {"agent_name": "bob", "bind": False, "compact": True}
    )

    assert "already delivered live" in out
    assert "line two" not in out, "full body was reprinted despite live delivery"
    assert "alice" in out, "sender must still be identifiable"


async def test_deaf_delivered_push_is_not_falsely_compacted(server):
    """Regression for the 2026-07-23 deaf-⚡ bug (fireblade, proven on Windows):
    a push that SUCCEEDED server-side (delivered, binding still held) but was
    never actually rendered — because the stream was half-dead after a redeploy
    reconnect — must NOT be claimed "already delivered live" and truncated.

    Modelled by the wake-ack expectation still being PENDING at drain time: the
    delivered wake produced no independent ack, so render is unproven. This is
    exactly the fleet-wide post-redeploy window. Fail safe → full reprint."""
    await _setup(server)
    await _send(server)  # push succeeds + arms expect_wake_ack, NO ack follows

    out = await _call_tool(
        server, "get_messages", {"agent_name": "bob", "bind": False, "compact": True}
    )

    assert "already delivered live" not in out, (
        "a delivered-but-unrendered push was falsely claimed seen-live"
    )
    assert "line two" in out, "unrendered message must be reprinted in FULL"


async def test_deaf_push_still_full_after_wake_ack_expiry(server):
    """The 2026-07-25 sequel to the test above, proven live on FB WSL.

    The deaf-⚡ gate is only sound while the expectation is PENDING. Once the
    reaper's sweep_wake_acks() runs (WAKE_ACK_TIMEOUT_SECONDS = 90s), the
    expired expectation is DELETED and a strike recorded — so
    has_pending_wake_ack() reverts to False and the gate reports "render not in
    doubt" for a stream that never rendered anything.

    Real incident: push at 12:21:09 (hub time), agent deaf, Stop-hook drain
    ~10 MINUTES later. Long past the 90-SECOND expiry, so the message was
    rendered "(already delivered live — ...)" to an agent that had never seen
    it — it only recovered the message by polling.

    Note the claim is NOT reliably false — it tracks ELAPSED TIME, not delivery,
    so it also fires correctly on a genuinely-live push once 90s have passed
    (observed both ways the same day). The signal carries no information about
    delivery either way, which is why this must fail safe rather than guess.

    Anything beyond the 90s window must still fail safe → full reprint.
    """
    registry = await _setup(server)
    await _send(server)  # push succeeds + arms expect_wake_ack, NO ack follows

    # The reaper sweeps: expectation expires, strike 1, binding survives.
    # This is the ⚡-but-deaf steady state — and note a SECOND strike (which
    # would drop the binding) never comes unless another wake is pushed.
    with registry._lock:
        registry._wake_expect["bob"] = 0.0  # force past deadline
    assert registry.sweep_wake_acks() == [], "strike 1 keeps the binding"

    out = await _call_tool(
        server, "get_messages", {"agent_name": "bob", "bind": False, "compact": True}
    )

    assert "already delivered live" not in out, (
        "an unrendered push was falsely claimed seen-live once its wake-ack "
        "expectation expired — the 90s window must not become an amnesty"
    )
    assert "line two" in out, "unrendered message must be reprinted in FULL"


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


async def test_footer_advice_actually_retrieves_the_body(server):
    """The footer tells the agent where to get the full text. FOLLOW it and
    assert the body comes back.

    The first version pointed at get_messages() — which returns nothing,
    because the compact pull marked these very rows read. A test asserting
    only that read-semantics hold (below) passed happily alongside advice it
    disproved. Assert the ADVICE, not just the mechanics.
    """
    registry = await _setup(server)
    await _send(server)
    registry.wake_ack("bob")  # independent ack → genuine live-render → compacted

    out = await _call_tool(
        server, "get_messages", {"agent_name": "bob", "bind": False, "compact": True}
    )
    assert "get_history" in out, "footer must name the tool that still has the body"
    assert "get_messages" not in out, "get_messages returns nothing once read"

    recovered = await _call_tool(server, "get_history", {"agent_or_channel": "bob"})
    assert "line two" in recovered, "following the footer must yield the full body"


async def test_capped_bulk_also_gets_a_footer(server):
    """Bulk-capped bodies were summarised with no pointer at all — an agent
    had no way to know text had been dropped, or where to find it."""
    await _setup(server, bind=False)
    for i in range(COMPACT_FULL_MESSAGES + 2):
        await _send(server, body=f"msg{i} first\nmsg{i} second", pushed=False)

    out = await _call_tool(
        server, "get_messages", {"agent_name": "bob", "bind": False, "compact": True}
    )
    assert "cap" in out and "get_history" in out


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


async def test_full_budget_bodies_are_clipped_at_the_char_limit(server):
    """Even inside the full-message budget, a single long body is clipped to
    COMPACT_FULL_BODY_CHARS. The 2-message budget was designed for backlog
    floods; the common real case is ONE long DM per Stop (2026-07-25), which
    previously landed in context whole at 2-3KB. Clipped, not dropped: the
    head must survive and the footer must point at get_history."""
    from mcp_hub.server import COMPACT_FULL_BODY_CHARS

    await _setup(server, bind=False)
    head = "HEAD-MARKER " + "x" * 100
    tail = "TAIL-MARKER-THAT-MUST-BE-CLIPPED"
    body = head + "\n" + ("filler " * ((COMPACT_FULL_BODY_CHARS // 7) + 40)) + "\n" + tail
    await _send(server, body=body, pushed=False)

    out = await _call_tool(
        server, "get_messages", {"agent_name": "bob", "bind": False, "compact": True}
    )

    assert "HEAD-MARKER" in out, "clip must keep the head"
    assert "TAIL-MARKER-THAT-MUST-BE-CLIPPED" not in out, "clip must cut the tail"
    assert "[…clipped]" in out
    assert "get_history" in out, "footer must point at the full-text retrieval"

    # And the advice must actually work: the FULL body is retrievable.
    hist = await _call_tool(server, "get_history", {"agent_or_channel": "bob"})
    assert "TAIL-MARKER-THAT-MUST-BE-CLIPPED" in hist


async def test_short_bodies_inside_budget_are_untouched(server):
    """A body under the clip limit renders byte-identical inside the budget —
    no clip marker, no footer noise for the quiet-day case."""
    await _setup(server, bind=False)
    await _send(server, body="short body\nsecond line", pushed=False)

    out = await _call_tool(
        server, "get_messages", {"agent_name": "bob", "bind": False, "compact": True}
    )
    assert "second line" in out
    assert "[…clipped]" not in out
    assert "clipped at" not in out
