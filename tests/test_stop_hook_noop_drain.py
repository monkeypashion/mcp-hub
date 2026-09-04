"""Bar 47 (g#24): a drain carrying only already-delivered items costs no turn.

The hook block arrives as a BLOCKING Stop error, so every block the lane
receives costs exactly one model turn to acknowledge. Measured 2026-09-04 on
this box: 12 Stop-hook drains landed across three lanes and ALL 12 carried
only items the hub had already rendered live — 12 turns spent re-reading what
was already in context.

This is the mirror of `test_stop_hook_defer_low.py` with the danger removed.
There, the hub had marked messages read and the spool was the only copy, so
every uncertain reading had to BLOCK to avoid losing one. Here the suppressed
lines are the hub's own claim that the full text already reached this context
— there is no only-copy — so the failure direction is a wasted turn, never a
lost message, and the parser still fails open on anything it cannot read.
"""
from __future__ import annotations

import pytest

from mcp_hub import cli


def live(ref: int, sender: str = "operator-console") -> str:
    """A line the hub says already surfaced live in this lane."""
    return (
        f"[06:36:14] **{sender}** ·verified ⟨hub.msg/1?id={ref}⟩: "
        f"(already delivered live — ▶ play pressed again on the thread)"
    )


def fresh(ref: int, sender: str = "alice", prio: str = "") -> str:
    tag = f" [{prio}]" if prio else ""
    return f"[06:36:14] **{sender}** ·verified ⟨hub.msg/1?id={ref}⟩{tag}: body text"


FOOTER = (
    "(1 already surfaced live — shortened to save context, and now marked "
    "read. Full text: get_history('lane-1'))"
)


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_HUB_STATE_DIR", str(tmp_path))


def build(**kw):
    base = dict(
        agent_name="lane-1", project="org/repo", messages_text="",
        broadcasts_text="", is_online=True,
    )
    base.update(kw)
    return cli.build_hook_response(**base)


# --- the win ---------------------------------------------------------------

def test_a_drain_of_only_already_delivered_items_does_not_block():
    assert build(messages_text=live(19477)) is None


def test_the_renders_own_footer_does_not_make_it_block():
    assert build(messages_text=live(19477) + "\n" + FOOTER) is None


def test_several_already_delivered_items_still_do_not_block():
    text = "\n".join([live(1), live(2, "squad-proxy"), live(3), FOOTER])
    assert build(messages_text=text) is None


def test_it_covers_broadcasts_too():
    assert build(broadcasts_text=live(19477) + "\n" + FOOTER) is None


# --- what must still surface ----------------------------------------------

def test_one_fresh_item_blocks_the_whole_drain():
    assert build(messages_text="\n".join([live(1), fresh(2), live(3)])) is not None


def test_a_fresh_broadcast_blocks_even_when_the_dms_are_all_stale():
    assert build(
        messages_text=live(1), broadcasts_text=fresh(2, "bob")
    ) is not None


def test_an_unparseable_line_fails_open():
    # A render shape this parser does not know must never be suppressed.
    assert build(messages_text=live(1) + "\nsomething we cannot read") is not None


def test_a_full_bodys_continuation_line_fails_open():
    body = fresh(2) + "\nsecond line of the body"
    assert build(messages_text=body) is not None


def test_an_empty_drain_is_unchanged():
    # Nothing to suppress: the happy path already returned None, and the
    # helper must not turn a no-content call into a claim of coverage.
    assert build(messages_text="") is None
    assert cli._all_already_delivered("", "") is False


def test_an_offline_lane_still_gets_its_rebind_warning():
    # Suppression is about TRAFFIC. A correction the lane owes is not traffic.
    out = build(messages_text=live(1), is_online=False)
    assert out is not None
    assert "register(" in out["reason"]


def test_a_held_lane_still_surfaces():
    out = build(messages_text=live(1), held_notice="you are being stopped")
    assert out is not None
    assert "you are being stopped" in out["reason"]


def test_a_card_nag_still_surfaces():
    assert build(messages_text=live(1), card_nag=True) is not None


def test_a_held_low_spool_always_wins():
    # The spool IS the only copy of its contents; an all-stale drain must
    # never be the reason it stays unprinted.
    cli._spool_append("lane-1", "**Direct messages:**\n" + fresh(9, prio="low"))
    out = build(messages_text=live(1), defer_low=True)
    assert out is not None
    assert "id=9" in out["reason"]
