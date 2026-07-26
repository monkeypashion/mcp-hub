"""Decision cards — the operator-triage currency (2026-07-26 design).

Covers the parser (two-scale scoring, legacy SCORE, tolerance), the four
hub tools (put/upsert, clear, list, answer), the live-push clip (the
formerly-unclipped 840KB/day path), and the sender verbosity advisory.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from mcp_hub.server import (
    COMPACT_FULL_BODY_CHARS,
    create_server,
    parse_decision_card,
)

CARD_V2 = (
    "**DECISION**\n"
    "**ASK:** approve the widget rebuild\n"
    "**WHY:** the current one is broken\n"
    "**VALUE:** dashboards work again [7/10]\n"
    "**RISK:** an hour lost if wrong [3/10]\n"
    "**TAGS:** design, Ops\n"
)

CARD_LEGACY = (
    "**DECISION**\n"
    "**ASK:** rotate the PAT\n"
    "**WHY:** exposed three ways\n"
    "**VALUE:** closes the exposure\n"
    "**RISK:** brief CI breakage\n"
    "**SCORE:** 8/10\n"
)


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
    if isinstance(result, str):
        return result
    return str(result)


# ---------------------------------------------------------------------------
# parse_decision_card
# ---------------------------------------------------------------------------


def test_parse_v2_card_two_scale_scoring():
    f = parse_decision_card(CARD_V2)
    assert f["ask"] == "approve the widget rebuild"
    assert f["why"] == "the current one is broken"
    assert f["value_text"] == "dashboards work again"
    assert f["value_score"] == 7
    assert f["risk_text"] == "an hour lost if wrong"
    assert f["risk_score"] == 3
    assert f["net_score"] == 4  # computed, never asserted by the author
    assert f["tags"] == "design,ops"  # normalised lowercase


def test_parse_legacy_score_card_maps_to_net():
    f = parse_decision_card(CARD_LEGACY)
    assert f["ask"] == "rotate the PAT"
    assert f["value_score"] is None and f["risk_score"] is None
    assert f["net_score"] == 8


def test_parse_component_scores_beat_legacy_score():
    """When both component scores AND a legacy SCORE line are present, the
    computed net wins — the author cannot assert a total that contradicts
    their own components."""
    card = CARD_V2 + "**SCORE:** 10/10\n"
    f = parse_decision_card(card)
    assert f["net_score"] == 4


def test_parse_garbage_never_raises():
    f = parse_decision_card("no structure here at all")
    assert f["ask"] == ""
    assert f["net_score"] is None


def test_parse_pipe_separated_v1_card():
    """The original single-line format still parses — v1 cards exist in the
    wild (dreamteam's first ask)."""
    f = parse_decision_card(
        "DECISION — ASK: build the registry | WHY: nobody can tell | "
        "VALUE: verifiable fact | RISK: an hour | SCORE: 8/10"
    )
    assert f["ask"].startswith("build the registry")
    assert f["net_score"] == 8


# ---------------------------------------------------------------------------
# decision_put / decision_clear — one open card per agent, upsert semantics
# ---------------------------------------------------------------------------


async def test_put_opens_then_restate_updates_in_place(server):
    out1 = await _call_tool(
        server, "decision_put", {"from_agent": "alice", "card": CARD_V2},
    )
    assert "#1 opened" in out1
    out2 = await _call_tool(
        server, "decision_put",
        {"from_agent": "alice", "card": CARD_V2.replace("[7/10]", "[9/10]")},
    )
    assert "#1 updated" in out2  # same card, not a duplicate
    listing = await _call_tool(server, "decision_list", {})
    assert listing.count("alice") == 1
    assert "net +6" in listing  # 9 - 3, recomputed on restate


async def test_clear_withdraws_open_card(server):
    await _call_tool(server, "decision_put", {"from_agent": "alice", "card": CARD_V2})
    out = await _call_tool(server, "decision_clear", {"from_agent": "alice"})
    assert "1 card(s) withdrawn" in out
    assert "No open decision cards" in await _call_tool(server, "decision_list", {})


async def test_clear_with_nothing_open_is_silent(server):
    assert await _call_tool(server, "decision_clear", {"from_agent": "alice"}) == ""


async def test_stop_hook_clear_never_touches_api_cards(server):
    """A service-submitted card is not the agent's to auto-withdraw — the
    agent's turn ending says nothing about the service's ask."""
    await _call_tool(
        server, "decision_put",
        {"from_agent": "suggestion-service", "card": CARD_V2, "source": "api"},
    )
    out = await _call_tool(
        server, "decision_clear", {"from_agent": "suggestion-service"},
    )
    assert out == ""  # stop-hook-source clear finds nothing
    assert "suggestion-service" in await _call_tool(server, "decision_list", {})


async def test_put_merges_param_tags_with_card_tags(server):
    await _call_tool(
        server, "decision_put",
        {"from_agent": "alice", "card": CARD_V2, "tags": "deploy"},
    )
    import json as _json
    rows = _json.loads(
        await _call_tool(server, "decision_list", {"format": "json"})
    )
    assert rows[0]["tags"] == "deploy,design,ops"


# ---------------------------------------------------------------------------
# decision_list ordering
# ---------------------------------------------------------------------------


async def test_list_orders_by_net_desc_nulls_last(server):
    await _call_tool(server, "decision_put",
                     {"from_agent": "low",
                      "card": CARD_V2.replace("[7/10]", "[4/10]")})
    await _call_tool(server, "decision_put",
                     {"from_agent": "high",
                      "card": CARD_V2.replace("[7/10]", "[10/10]")})
    await _call_tool(server, "decision_put",
                     {"from_agent": "unscored",
                      "card": "**DECISION**\n**ASK:** something\n"})
    listing = await _call_tool(server, "decision_list", {})
    assert listing.index("high") < listing.index("low") < listing.index("unscored")


# ---------------------------------------------------------------------------
# decision_answer — close + DM the asker (the answer leg)
# ---------------------------------------------------------------------------


async def test_answer_closes_card_and_dms_asker(server):
    await _call_tool(server, "register", {"name": "alice", "project": "p"})
    await _call_tool(server, "decision_put", {"from_agent": "alice", "card": CARD_V2})
    out = await _call_tool(
        server, "decision_answer",
        {"decision": "yes", "agent": "alice", "note": "ship it"},
    )
    assert "decided: yes" in out
    # Card closed
    assert "No open decision cards" in await _call_tool(server, "decision_list", {})
    listing = await _call_tool(server, "decision_list", {"status": "decided"})
    assert "-> yes ship it" in listing
    # The verdict travelled as a DM from 'operator'
    inbox = await _call_tool(server, "get_messages", {"agent_name": "alice"})
    assert "DECISION ANSWERED (YES)" in inbox
    assert "approve the widget rebuild" in inbox


async def test_answer_no_open_card_is_clean(server):
    out = await _call_tool(
        server, "decision_answer", {"decision": "yes", "agent": "ghost"},
    )
    assert "No matching open decision card" in out


# ---------------------------------------------------------------------------
# Live push clip — the formerly-unclipped path
# ---------------------------------------------------------------------------


async def _captured_push_content(server, tool: str, args: dict) -> str:
    registry = server._hub_registry
    captured = {}

    async def _capture(name, notification):
        params = getattr(notification, "params", None)
        if params is None and isinstance(notification, dict):
            params = notification.get("params")
        captured.setdefault("content", params["content"])
        return True

    class _FakeSess:
        async def send_ping(self): ...
        async def send_notification(self, _n): ...

    registry.bind("bob", _FakeSess())
    with patch.object(registry, "push", side_effect=_capture):
        await server._tool_manager.call_tool(tool, args)
    return captured.get("content", "")


async def test_send_push_render_is_clipped(server):
    await _call_tool(server, "register", {"name": "alice", "project": "p"})
    await _call_tool(server, "register", {"name": "bob", "project": "p"})
    long = "headline\n" + "x" * 4000
    content = await _captured_push_content(
        server, "send", {"from_agent": "alice", "to": "bob", "message": long},
    )
    assert len(content) < 1000
    assert "[…clipped] (full text: get_history)" in content


async def test_broadcast_push_render_is_clipped(server):
    await _call_tool(server, "register", {"name": "alice", "project": "p"})
    await _call_tool(server, "register", {"name": "bob", "project": "p"})
    long = "headline\n" + "y" * 4000
    content = await _captured_push_content(
        server, "broadcast", {"from_agent": "alice", "message": long},
    )
    assert len(content) < 1000
    assert "(full text: get_history)" in content


async def test_short_push_render_untouched(server):
    await _call_tool(server, "register", {"name": "alice", "project": "p"})
    await _call_tool(server, "register", {"name": "bob", "project": "p"})
    content = await _captured_push_content(
        server, "send", {"from_agent": "alice", "to": "bob", "message": "short one"},
    )
    assert content == "DM from alice: short one"


async def test_inbox_keeps_full_body_after_clipped_push(server):
    """The clip is render-only: the stored message must remain complete —
    the 06-01 constraint (never destroy the source of truth)."""
    await _call_tool(server, "register", {"name": "alice", "project": "p"})
    await _call_tool(server, "register", {"name": "bob", "project": "p"})
    long = "headline\n" + "z" * (COMPACT_FULL_BODY_CHARS * 3)
    await _captured_push_content(
        server, "send", {"from_agent": "alice", "to": "bob", "message": long},
    )
    history = await _call_tool(server, "get_history", {"agent_or_channel": "bob"})
    assert "z" * (COMPACT_FULL_BODY_CHARS * 3) in history


# ---------------------------------------------------------------------------
# Sender verbosity advisory
# ---------------------------------------------------------------------------


async def test_long_broadcast_without_tldr_gets_advisory(server):
    await _call_tool(server, "register", {"name": "alice", "project": "p"})
    body = "word " * 400  # one giant 2000-char line, no summary lead
    out = await _call_tool(
        server, "broadcast", {"from_agent": "alice", "message": body},
    )
    assert "📏 Advisory" in out


async def test_long_broadcast_with_tldr_lead_no_advisory(server):
    await _call_tool(server, "register", {"name": "alice", "project": "p"})
    body = "TL;DR: the short version.\n" + ("detail " * 400)
    out = await _call_tool(
        server, "broadcast", {"from_agent": "alice", "message": body},
    )
    assert "Advisory" not in out


async def test_short_message_no_advisory(server):
    await _call_tool(server, "register", {"name": "alice", "project": "p"})
    await _call_tool(server, "register", {"name": "bob", "project": "p"})
    out = await _call_tool(
        server, "send", {"from_agent": "alice", "to": "bob", "message": "hi"},
    )
    assert "Advisory" not in out


async def test_long_tldr_line_is_still_compliance(server):
    """fb-wsl's live false positive (2026-07-26): a message whose first line
    IS the TL;DR but runs past 200 chars must not be flagged — an explicit
    marker beats the line-length heuristic. A false positive in a training
    signal teaches people to ignore it."""
    await _call_tool(server, "register", {"name": "alice", "project": "p"})
    body = ("TL;DR: " + "a genuinely long but deliberate summary line " * 6
            + "\n" + ("detail " * 400))
    assert len(body.splitlines()[0]) > 200
    out = await _call_tool(
        server, "broadcast", {"from_agent": "alice", "message": body},
    )
    assert "Advisory" not in out


async def test_bold_summary_marker_counts_too(server):
    await _call_tool(server, "register", {"name": "alice", "project": "p"})
    body = ("**Summary** — " + "the gist stated at some length " * 8
            + "\n" + ("detail " * 400))
    out = await _call_tool(
        server, "broadcast", {"from_agent": "alice", "message": body},
    )
    assert "Advisory" not in out


# ---------------------------------------------------------------------------
# decision_resolve — in-pane answers recorded by the agent that got them
# ---------------------------------------------------------------------------


async def test_resolve_closes_with_agent_recorded_verdict(server):
    await _call_tool(server, "decision_put", {"from_agent": "alice", "card": CARD_V2})
    out = await _call_tool(
        server, "decision_resolve",
        {"from_agent": "alice", "verdict": "yes — operator said ship it"},
    )
    assert "Card resolved" in out
    import json as _json
    rows = _json.loads(
        await _call_tool(server, "decision_list",
                         {"status": "decided", "format": "json"})
    )
    assert rows[0]["decision"] == "in-pane"
    assert rows[0]["decision_note"] == "[agent-recorded] yes — operator said ship it"


async def test_resolve_with_nothing_open_is_silent(server):
    out = await _call_tool(
        server, "decision_resolve", {"from_agent": "ghost", "verdict": "yes"},
    )
    assert out == ""


async def test_resolve_never_touches_api_cards(server):
    await _call_tool(
        server, "decision_put",
        {"from_agent": "svc", "card": CARD_V2, "source": "api"},
    )
    out = await _call_tool(
        server, "decision_resolve", {"from_agent": "svc", "verdict": "yes"},
    )
    assert out == ""
    assert "svc" in await _call_tool(server, "decision_list", {})
