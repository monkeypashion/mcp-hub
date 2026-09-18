"""The owner notice rides the next block; it never buys one of its own.

An agent with an open card used to block on EVERY natural Stop, because
`card_notice` sat in `build_hook_response`'s happy-path condition. A hook
block continues the session and that continuation IS a model turn, so the
operator saw every answer TWICE for as long as any card stayed open — in
their own words, "every time I ask you a question I am getting x2 replies"
(reported by slipstream-dev-vm-1, 2026-09-18, with the code read done).

MEASURED BEFORE THE CHANGE, from this box's own drain records
(~/.mcp-hub/activity-log.jsonl, kind=drain, `surfaced`): over 14 hours,
lanes WITH an open card blocked on 100% of their natural Stops (25/25;
per-lane 12/12, 6/6, 4/4) against 36% (54/150) for lanes without one. The
loop backstop kept it to exactly one extra turn per turn, which is why the
symptom is a doubling and not a runaway.

WHY DROPPING IT LOSES NOTHING, which is what these tests pin:
  * the notice is CONTEXT, not a correction — `decision_clear` writes no
    state, and its own text says "if you're still waiting, nothing is
    needed". A card closes on an operator answer, a DECIDED or supersession,
    never by decay, so no ask can be lost by not mentioning it this turn;
  * unlike a drained DM it is DERIVED, not consumed: every Stop re-reads it
    from the open card, so it needs no spool — it is simply assembled onto
    the next block that has real content;
  * anything genuinely actionable still blocks AND still carries it.

The last point is the one worth guarding: a fix that quieted the notice
everywhere would trade a doubled reply for a silent ask.
"""
from __future__ import annotations

from mcp_hub.cli import build_hook_response

NOTICE = "Card #994 still open on the operator's board: RESTATED — stage 1 only"
DM = "[09:10] **bob** ⟨hub.msg/1?id=1⟩: the thing you asked about is ready"


def _resp(**kw):
    base = dict(agent_name="alice", project="p", messages_text="",
                is_online=True)
    base.update(kw)
    return build_hook_response(**base)


# --- the two halves of the deputy's positive control ------------------------

def test_a_notice_only_stop_costs_no_turn():
    """Nothing owed but the notice → no block, so no continuation turn."""
    assert _resp(card_notice=NOTICE) is None


def test_a_stop_with_real_content_still_surfaces_AND_carries_the_notice():
    """The must-still-fire case: a different drained item blocks as before,
    and the notice rides along on it rather than being swallowed."""
    response = _resp(messages_text=DM, card_notice=NOTICE)
    assert response is not None
    assert response["decision"] == "block"
    assert DM in response["reason"]              # the real content surfaced
    assert "Card #994 still open" in response["reason"]   # and the notice rode
    assert "DECIDED:" in response["reason"]      # including how to close it


# --- the notice must not silence anything else ------------------------------

def test_an_offline_agent_still_blocks_with_a_notice_open():
    """The rebind correction is actionable; the notice must not mask it."""
    response = _resp(is_online=False, card_notice=NOTICE)
    assert response is not None
    assert "Card #994 still open" in response["reason"]


def test_a_held_lane_still_surfaces_with_a_notice_open():
    """Being stopped is the one thing a lane must not learn by its pane
    disappearing — it outranks every quieting rule on this path."""
    response = _resp(card_notice=NOTICE, held_notice="⛔ this lane is HELD")
    assert response is not None
    assert "HELD" in response["reason"]


def test_the_authoring_nag_still_blocks_and_the_notice_replaces_it():
    """A waiting turn with no card still earns its one-shot nag; the notice
    is what gets rendered, because 'you have no card' is then false.

    ⚠️ This is the case the first version of the fix broke: zeroing the nag
    and then gating on the nag made a turn that SAID "waiting on you" as
    quiet as one that said nothing. With one open card per agent, an agent
    waiting on something new must be told which card is on the board before
    it can restate — so a turn that asked for the notice still gets it."""
    response = _resp(card_nag=True, card_notice=NOTICE)
    assert response is not None
    assert "no DECISION card" not in response["reason"]
    assert "Card #994 still open" in response["reason"]


def test_a_cardless_quiet_stop_is_unchanged():
    """Control: the happy path did not depend on the notice being present."""
    assert _resp() is None


# --- the saving has to be visible to the instrument that will measure it ----

def test_the_suppressed_notice_is_recorded_in_the_drain_trace():
    """Bar 47's rule pointed at this branch: a drain that returns None is
    exactly the one no other instrument can see, so the record carries it."""
    trace: dict[str, object] = {}
    assert _resp(card_notice=NOTICE, trace=trace) is None
    assert trace.get("notice_deferred") is True


def test_a_block_does_not_claim_a_deferred_notice():
    """The field must not read true on a turn where the notice was printed —
    a saving counted on a turn that still cost one is a fabricated number."""
    trace: dict[str, object] = {}
    assert _resp(messages_text=DM, card_notice=NOTICE, trace=trace) is not None
    assert "notice_deferred" not in trace


def test_a_quiet_stop_with_no_card_claims_no_saving():
    """Control for the control: no card, no notice, nothing to report."""
    trace: dict[str, object] = {}
    assert _resp(trace=trace) is None
    assert "notice_deferred" not in trace
