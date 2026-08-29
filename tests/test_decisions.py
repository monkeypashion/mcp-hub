"""Decision cards — the operator-triage currency (2026-07-26 design).

Covers the parser (two-scale scoring, legacy SCORE, tolerance), the four
hub tools (put/upsert, clear, list, answer), the live-push clip (the
formerly-unclipped 840KB/day path), and the sender verbosity advisory.
"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from mcp_hub.server import (
    COMPACT_FULL_BODY_CHARS,
    DECISION_STALE_AFTER_SECONDS,
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


def _age_card(server_dir_db, card_id: int, seconds: float) -> None:
    """Backdate a card's substantive timestamps — the only honest way to
    make one stale now that staleness is the ask's own age (card #237)."""
    import sqlite3 as _sq
    conn = _sq.connect(server_dir_db)
    conn.execute(
        "UPDATE decisions SET submitted_at = submitted_at - ?, "
        "updated_at = updated_at - ? WHERE id=?",
        (seconds, seconds, card_id),
    )
    conn.commit()
    conn.close()


async def test_clear_is_pure_notice_and_never_stales(server, tmp_path):
    """The turn-rate clock is retired (card #237, operator-approved
    2026-08-28): any number of cardless turns leaves the card exactly as
    fresh as its own age says. The old rule demoted a 6-minute-old
    top-scored ask after three quiet turns — punishing the lane that
    obediently stopped restating."""
    await _call_tool(server, "decision_put", {"from_agent": "alice", "card": CARD_V2})
    for _ in range(5):
        out = await _call_tool(server, "decision_clear", {"from_agent": "alice"})
        assert "#1 still open" in out
        assert "STALE" not in out            # no restate-to-keep-fresh prompt
    listing = await _call_tool(server, "decision_list", {})
    assert "alice" in listing
    assert "STALE" not in listing
    rows = json.loads(
        await _call_tool(server, "decision_list", {"format": "json"})
    )
    assert rows[0]["stale"] == 0


async def test_staleness_is_the_age_of_the_ask(server, tmp_path):
    """A card nobody has touched for DECISION_STALE_AFTER_SECONDS is stale
    regardless of the lane's turn cadence — and a purged owner's card can
    no longer read fresh forever by taking no turns (the #439 shape)."""
    await _call_tool(server, "decision_put", {"from_agent": "alice", "card": CARD_V2})
    _age_card(tmp_path / "test.db", 1, DECISION_STALE_AFTER_SECONDS + 60)
    listing = await _call_tool(server, "decision_list", {})
    assert "· STALE" in listing
    rows = json.loads(
        await _call_tool(server, "decision_list", {"format": "json"})
    )
    assert rows[0]["stale"] == 1             # computed, truthful in json too


async def test_restatement_is_the_honest_still_live(server, tmp_path):
    """Restating a week-old ask refreshes its clock — that is the one
    legitimate 'still blocking' signal, priced at once per threshold
    rather than demanded every three turns."""
    await _call_tool(server, "decision_put", {"from_agent": "alice", "card": CARD_V2})
    _age_card(tmp_path / "test.db", 1, DECISION_STALE_AFTER_SECONDS + 60)
    assert "· STALE" in await _call_tool(server, "decision_list", {})
    await _call_tool(server, "decision_put", {"from_agent": "alice", "card": CARD_V2})
    listing = await _call_tool(server, "decision_list", {})
    assert "alice" in listing
    assert "STALE" not in listing            # updated_at moved, clock reset


async def test_stale_card_still_answerable_and_sorts_last(server, tmp_path):
    """The whole point: the operator can still answer a stale card, and a
    stale high-net card must not outrank a fresh low-net one."""
    await _call_tool(server, "register", {"name": "alice", "project": "p"})
    await _call_tool(server, "decision_put",
                     {"from_agent": "alice",
                      "card": CARD_V2.replace("[7/10]", "[10/10]")})
    _age_card(tmp_path / "test.db", 1, DECISION_STALE_AFTER_SECONDS + 60)
    await _call_tool(server, "decision_put",
                     {"from_agent": "bob",
                      "card": CARD_V2.replace("[7/10]", "[4/10]")})
    listing = await _call_tool(server, "decision_list", {})
    assert listing.index("bob") < listing.index("alice")  # fresh beats stale
    out = await _call_tool(
        server, "decision_answer", {"decision": "yes", "agent": "alice"},
    )
    assert "decided: yes" in out
    assert "alice" not in await _call_tool(server, "decision_list", {})
    # An aged card that got ANSWERED is history, not stale — staleness is
    # a property of open asks only.
    all_listing = await _call_tool(server, "decision_list", {"status": "all"})
    alice_line = next(ln for ln in all_listing.splitlines() if "alice" in ln)
    assert "DECIDED" in alice_line
    assert "STALE" not in alice_line


async def test_legacy_stored_stale_flag_is_cleared_on_boot(tmp_path):
    """A stale=1 row written by the retired turn-rate clock must stop
    lying: the migration clears stored flags once, and a young card reads
    fresh everywhere afterwards."""
    import sqlite3 as _sq
    db = tmp_path / "test.db"
    server = create_server(db_path=db)
    await _call_tool(server, "decision_put", {"from_agent": "alice", "card": CARD_V2})
    conn = _sq.connect(db)
    conn.execute("UPDATE decisions SET stale=1 WHERE id=1")
    conn.commit()
    conn.close()
    server2 = create_server(db_path=db)
    listing = await _call_tool(server2, "decision_list", {})
    assert "alice" in listing
    assert "STALE" not in listing
    rows = json.loads(
        await _call_tool(server2, "decision_list", {"format": "json"})
    )
    assert rows[0]["stale"] == 0
    # The API computes staleness, so it cannot witness the stored column —
    # assert the migration at the DB itself, where external read-only
    # consumers (prod DB access) would still see a lying flag.
    conn = _sq.connect(db)
    assert conn.execute(
        "SELECT stale FROM decisions WHERE id=1"
    ).fetchone()[0] == 0
    conn.close()


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


async def test_all_listing_labels_history_rows(server, tmp_path):
    """A mixed-status listing must SAY which rows are closed — in a real
    `all` render on 2026-07-27, 25 of 28 rows were history and none said
    so, and the reader tallied ledger rows as live queue. (Withdrawn rows
    can no longer be minted — stale replaced withdrawal — but legacy rows
    in the prod DB still render through the same generic non-open branch
    that SUPERSEDED exercises here.)"""
    await _call_tool(server, "register", {"name": "alice", "project": "p"})
    await _call_tool(server, "decision_put", {"from_agent": "alice", "card": CARD_V2})
    await _call_tool(server, "decision_answer", {"decision": "yes", "agent": "alice"})
    await _call_tool(server, "decision_put", {"from_agent": "bob", "card": CARD_V2})
    _age_card(tmp_path / "test.db", 2, DECISION_STALE_AFTER_SECONDS + 60)
    await _call_tool(server, "decision_put", {"from_agent": "carol", "card": CARD_V2})
    different = CARD_V2.replace(
        "approve the widget rebuild", "tear down the legacy ingest cluster"
    )
    await _call_tool(server, "decision_put", {"from_agent": "carol", "card": different})
    listing = await _call_tool(server, "decision_list", {"status": "all"})
    assert "· DECIDED" in listing
    assert "· SUPERSEDED" in listing
    assert "· STALE" in listing          # bob's, demoted not removed
    # the live row carries no status tag — open is the unmarked default
    carol_headers = [ln for ln in listing.splitlines() if "carol" in ln]
    assert any(
        all(tag not in ln for tag in ("SUPERSEDED", "STALE", "DECIDED"))
        for ln in carol_headers
    )


async def test_superseded_rows_are_labeled_and_filterable(server):
    await _call_tool(server, "decision_put", {"from_agent": "alice", "card": CARD_V2})
    different = CARD_V2.replace(
        "approve the widget rebuild", "tear down the legacy ingest cluster"
    )
    await _call_tool(server, "decision_put", {"from_agent": "alice", "card": different})
    listing = await _call_tool(server, "decision_list", {"status": "superseded"})
    assert "widget rebuild" in listing
    assert "· SUPERSEDED" in await _call_tool(
        server, "decision_list", {"status": "all"}
    )


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
        server, "send", {"from_agent": "alice", "to": "bob", "message": long,
                         "priority": "urgent"},
    )
    assert len(content) < 1000
    assert "[…clipped] (full text: get_history)" in content


async def test_broadcast_push_render_is_clipped(server):
    await _call_tool(server, "register", {"name": "alice", "project": "p"})
    await _call_tool(server, "register", {"name": "bob", "project": "p"})
    long = "headline\n" + "y" * 4000
    content = await _captured_push_content(
        server, "broadcast", {"scope": "fleet", "from_agent": "alice",
                              "message": long, "priority": "urgent"},
    )
    assert len(content) < 1000
    assert "(full text: get_history)" in content


async def test_short_push_render_untouched(server):
    await _call_tool(server, "register", {"name": "alice", "project": "p"})
    await _call_tool(server, "register", {"name": "bob", "project": "p"})
    content = await _captured_push_content(
        server, "send", {"from_agent": "alice", "to": "bob",
                         "message": "short one", "priority": "urgent"},
    )
    # W3: the ⟨ref⟩ is the lineage handle — a reply can only cite a ref the
    # sender has SEEN, so every rendered message carries its own.
    # The sender carries its attribution grade now (asserted: no ctx in
    # tests) — the clip is what this test guards, not the tag.
    assert content == "DM from alice ·asserted ⟨hub.msg/1?id=1⟩: short one"


async def test_inbox_keeps_full_body_after_clipped_push(server):
    """The clip is render-only: the stored message must remain complete —
    the 06-01 constraint (never destroy the source of truth)."""
    await _call_tool(server, "register", {"name": "alice", "project": "p"})
    await _call_tool(server, "register", {"name": "bob", "project": "p"})
    long = "headline\n" + "z" * (COMPACT_FULL_BODY_CHARS * 3)
    await _captured_push_content(
        server, "send", {"from_agent": "alice", "to": "bob", "message": long,
                         "priority": "urgent"},
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
        server, "broadcast", {"scope": "fleet", "from_agent": "alice", "message": body},
    )
    assert "📏 Advisory" in out


async def test_long_broadcast_with_tldr_lead_no_advisory(server):
    await _call_tool(server, "register", {"name": "alice", "project": "p"})
    body = "TL;DR: the short version.\n" + ("detail " * 400)
    out = await _call_tool(
        server, "broadcast", {"scope": "fleet", "from_agent": "alice", "message": body},
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
        server, "broadcast", {"scope": "fleet", "from_agent": "alice", "message": body},
    )
    assert "Advisory" not in out


async def test_bold_summary_marker_counts_too(server):
    await _call_tool(server, "register", {"name": "alice", "project": "p"})
    body = ("**Summary** — " + "the gist stated at some length " * 8
            + "\n" + ("detail " * 400))
    out = await _call_tool(
        server, "broadcast", {"scope": "fleet", "from_agent": "alice", "message": body},
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
    # The receipt names WHICH card closed and its ask — echoing the verdict
    # alone is how six wrong closes read as successes (2026-08-28).
    assert "#1 resolved" in out
    assert "approve the widget rebuild" in out
    rows = json.loads(
        await _call_tool(server, "decision_list",
                         {"status": "decided", "format": "json"})
    )
    assert rows[0]["decision"] == "in-pane"
    assert rows[0]["decision_note"] == "[agent-recorded] yes — operator said ship it"


async def test_resolve_refuses_verdict_naming_a_different_card(server):
    """The 2026-08-28 supersede-and-tidy defect, exactly: file a new card
    (superseding the old), then resolve with a verdict naming the OLD id —
    the tool must refuse rather than close the newer card the verdict never
    meant."""
    await _call_tool(server, "decision_put", {"from_agent": "alice", "card": CARD_V2})
    different = CARD_V2.replace(
        "approve the widget rebuild", "tear down the legacy ingest cluster"
    )
    await _call_tool(server, "decision_put", {"from_agent": "alice", "card": different})
    out = await _call_tool(
        server, "decision_resolve",
        {"from_agent": "alice", "verdict": "#1 superseded by #2"},
    )
    assert "REFUSED" in out
    assert "#1" in out and "#2" in out   # the mismatch is named, both sides
    assert "nothing was closed" in out
    listing = await _call_tool(server, "decision_list", {})
    assert "tear down the legacy ingest cluster" in listing  # #2 survives


async def test_resolve_proceeds_when_verdict_names_the_open_card(server):
    await _call_tool(server, "decision_put", {"from_agent": "alice", "card": CARD_V2})
    out = await _call_tool(
        server, "decision_resolve",
        {"from_agent": "alice", "verdict": "closing #1 as approved"},
    )
    assert "#1 resolved" in out
    assert "alice" not in await _call_tool(server, "decision_list", {})


async def test_resolve_explicit_card_targets_it(server):
    """card= is the explicit form: it closes exactly the named card."""
    await _call_tool(server, "decision_put", {"from_agent": "alice", "card": CARD_V2})
    out = await _call_tool(
        server, "decision_resolve",
        {"from_agent": "alice", "verdict": "#99 mentioned incidentally",
         "card": 1},
    )
    assert "#1 resolved" in out          # explicit target wins; no prose guard
    assert "alice" not in await _call_tool(server, "decision_list", {})


async def test_resolve_explicit_card_refuses_wrong_id(server):
    await _call_tool(server, "decision_put", {"from_agent": "alice", "card": CARD_V2})
    out = await _call_tool(
        server, "decision_resolve",
        {"from_agent": "alice", "verdict": "yes", "card": 42},
    )
    assert "REFUSED" in out
    assert "#42" in out
    assert "#1" in out                   # the actual open card is named
    assert "alice" in await _call_tool(server, "decision_list", {})


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


# ---------------------------------------------------------------------------
# Hardening (operator: "make it solid while we have it open", 2026-07-26)
# ---------------------------------------------------------------------------


async def test_different_ask_supersedes_instead_of_overwriting(server):
    """Ask A's ledger row must survive the agent moving on to ask B —
    supersede, never overwrite."""
    await _call_tool(server, "decision_put", {"from_agent": "alice", "card": CARD_V2})
    card_b = CARD_V2.replace("approve the widget rebuild",
                             "delete the staging database entirely tonight")
    out = await _call_tool(server, "decision_put", {"from_agent": "alice", "card": card_b})
    assert "#2 opened" in out
    import json as _json
    all_rows = _json.loads(
        await _call_tool(server, "decision_list", {"status": "all", "format": "json"})
    )
    by_status = {r["status"] for r in all_rows}
    assert "superseded" in by_status and "open" in by_status
    assert len(all_rows) == 2


async def test_rephrased_same_ask_updates_not_supersedes(server):
    """Agents reword when restating — token-overlap similarity must treat a
    rephrase as the SAME ask (no row churn, age preserved)."""
    await _call_tool(server, "decision_put", {"from_agent": "alice", "card": CARD_V2})
    reworded = CARD_V2.replace("approve the widget rebuild",
                               "approve rebuilding the widget")
    out = await _call_tool(server, "decision_put", {"from_agent": "alice", "card": reworded})
    assert "updated" in out
    import json as _json
    all_rows = _json.loads(
        await _call_tool(server, "decision_list", {"status": "all", "format": "json"})
    )
    assert len(all_rows) == 1


async def test_card_raw_is_capped(server):
    """A convention-breaking card (DECISION header then a ramble) must not
    store a novel."""
    huge = CARD_V2 + ("ramble " * 2000)
    await _call_tool(server, "decision_put", {"from_agent": "alice", "card": huge})
    import json as _json
    rows = _json.loads(await _call_tool(server, "decision_list", {"format": "json"}))
    assert len(rows[0]["raw"]) <= 4096
