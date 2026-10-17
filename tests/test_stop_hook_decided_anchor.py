"""Card #943: the closing marker must OWN its line.

The unanchored pattern matched the token wherever it appeared, so a lane
writing a sentence ABOUT the marker closed its own card with whatever
followed the colon. Between 2026-07-27 and 2026-09-04 — 39 days — that
fabricated 15 in-pane operator verdicts across 7 lanes, every one of them
harvested from a sentence whose point was that no verdict was being
recorded.

The defect was invisible for 39 days because the corrupted row was never in
anybody's authorisation chain: every lane keyed authorisation to the console
or the pane, never to hub card state. So these tests are the only thing that
will ever notice it coming back.

Direction of every gate: FAIL TOWARD NOT CLOSING. Missing a real verdict
costs a restatement. Inventing one forges a consent record that reads
exactly like a real one.
"""
from __future__ import annotations

import pytest

from mcp_hub import cli

# The sentence shapes that actually fabricated verdicts. Each is prose in
# which a lane was REFUSING to record a verdict, or discussing the marker.
REFUSALS = [
    "I have emitted no `DECIDED:` line.",
    "Still waiting — nothing to record, so no **DECIDED:** line from me.",
    "Per the hook I am not emitting a DECIDED: marker this turn.",
    "He has not answered, so I will not write a DECIDED: verdict.",
    "The convention is to end with `DECIDED:` and I am deliberately not.",
    "Do not write DECIDED: in a reply that carries no card.",
    "A turn carrying a card is immune; one without a DECIDED: token is not.",
    "My last reply mentioned DECIDED: only to warn about it.",
    "Nothing was pressed. No DECIDED: line is owed.",
    "Quoting the hook: 'end your reply with DECIDED: <verdict>'.",
]


@pytest.mark.parametrize("text", REFUSALS)
def test_a_sentence_about_the_marker_never_closes_a_card(text):
    """The whole incident, in one assertion."""
    assert cli._extract_decided(text) == ""


@pytest.mark.parametrize("text", REFUSALS)
def test_refusal_is_inert_even_as_the_final_line(text):
    """The refusals arrived at the end of a turn, which is where the closing
    marker legitimately lives. Position alone must not rescue them."""
    assert cli._extract_decided(f"Work done this turn.\n\n{text}") == ""


def test_the_exact_byte_sequence_that_closed_936():
    """Reproduced from the stored note on card #936: the verdict was the tail
    of the sentence refusing to give one, backtick and all."""
    turn = "Nothing was decided in this pane. I have emitted no `DECIDED:` line."
    assert cli._extract_decided(turn) == ""


def test_a_genuine_closing_line_still_closes():
    """The gates must not break the convention they protect."""
    got = cli._extract_decided("I applied his ruling.\n\n**DECIDED:** approve")
    assert got.startswith("approve")


@pytest.mark.parametrize("line,verdict", [
    ("**DECIDED:** approve", "approve"),
    ("**DECIDED**: approve", "approve"),
    ("DECIDED: approve the local commit", "approve the local commit"),
    ("  **DECIDED:** go ahead, no push", "go ahead, no push"),
    ("**DECIDED:**   approve   ", "approve"),
])
def test_accepted_forms(line, verdict):
    assert cli._extract_decided(f"context\n\n{line}").split("\n")[0] == verdict


def test_the_close_carries_a_hook_distinguishable_receipt():
    """A hook-made close and a hand-made one used to be the same row. The
    receipt names the mechanism and quotes the line, so a fabricated verdict
    is falsifiable from the record alone."""
    got = cli._extract_decided("done\n\n**DECIDED:** approve")
    assert "auto-closed by stop-hook" in got
    assert "**DECIDED:** approve" in got


def test_a_fenced_example_is_documentation_not_a_verdict():
    turn = "The convention is:\n\n```\nDECIDED: <verdict>\n```"
    assert cli._extract_decided(turn) == ""


def test_marker_not_at_the_end_is_prose():
    """A closing marker closes. One with turn text after it was being
    discussed, not emitted."""
    turn = "**DECIDED:** approve\n\nBut actually I am still waiting on him."
    assert cli._extract_decided(turn) == ""


def test_last_one_wins_among_genuine_lines():
    turn = "**DECIDED:** first\n\n**DECIDED:** second"
    assert cli._extract_decided(turn).startswith("second")


@pytest.mark.parametrize("verdict", ["` line.", "' to the card", "** and stop",
                                     ") so nothing", ", nothing else", "."])
def test_scrape_signatures_are_refused(verdict):
    """A verdict torn out of a quotation begins with the punctuation that
    closed it. A real verdict starts with a word."""
    assert cli._extract_decided(f"DECIDED:{verdict}") == ""


def test_empty_and_punctuation_only_verdicts_are_refused():
    assert cli._extract_decided("DECIDED:") == ""
    assert cli._extract_decided("DECIDED:   ") == ""
    assert cli._extract_decided("DECIDED: ...") == ""


def test_no_text_at_all():
    assert cli._extract_decided("") == ""
    assert cli._extract_decided("   \n\n  ") == ""


@pytest.mark.parametrize("line", [
    "DECIDED:** and stop",
    "**DECIDED: approve",
    "*DECIDED:* approve",
])
def test_unbalanced_emphasis_is_refused(line):
    """Found by this suite, not by review: the loose form consumed a stray
    bold-close as the marker's own and handed back the remainder. Unbalanced
    emphasis means the line was torn out of something."""
    assert cli._extract_decided(line) == ""
