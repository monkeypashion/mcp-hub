"""An unparsed card was the one card supersession could not protect.

`parse_decision_card` is tolerant BY DESIGN: a card with no `**ASK:**` line
is still stored, with `ask=""`, so a fumbled hand loses nothing. But
`decision_put` decided restatement-vs-new-ask from the ASK field alone:

    different = bool(old) and bool(new) and jaccard(old, new) < 0.5

An empty set fails the `bool()` guard, so `different` is False — and False
is the arm that UPDATEs the open row IN PLACE, overwriting raw/ask/why/
value/risk/tags. The consequence ran in exactly the wrong direction:

  * a new card with no ASK line could never SUPERSEDE, so it silently
    destroyed the open card's text however unrelated the two asks were;
  * an open row with an empty ask could never BE superseded, so the next
    card that agent filed destroyed it, on any topic.

Supersession is the non-destructive arm — it closes the old row with its
text intact and writes a `supersedes` lineage edge. Overwrite-in-place is
the destructive one. The card the parser could not read is precisely the
card with no parsed fields to fall back on, and that is the card the ledger
threw away.

Measured on the DEPLOYED hub (commit 16a6808, `/health`), 2026-09-20: card
#1075 was in this state on the live board — raw prose, no ASK line,
ask="", one in-place overwrite already taken (submitted 09-18T19:03:43Z,
updated 09-20T08:36:57Z), with the earlier wording unrecoverable.

THE FIX: when either side's ask is empty, compare the RAW bodies instead —
the same question asked of both sides. That is deliberately not "supersede
whenever unsure": an unparsed RESTATEMENT still reads as a restatement and
still updates in place, which `test_unparsed_restate_still_updates_in_place`
holds. A blanket supersede would trade silent destruction for a superseded
row per rewording, and the ledger would fill with churn.

NEGATIVE CONTROL: the two `test_unparsed_*_supersedes` tests fail against
the unfixed tree (and against deployed 16a6808) — they are the defect. The
in-place and well-formed tests pass either way; they are the guard that the
fix did not over-correct.
"""

import json
from pathlib import Path

import pytest

from mcp_hub.server import create_server


def _card(ask: str) -> str:
    """A well-formed card — the parser finds an ASK."""
    return (
        "**DECISION**\n"
        f"**ASK:** {ask}\n"
        "**WHY:** the board needs one face per agent\n"
        "**VALUE:** the ask is answerable [7/10]\n"
        "**RISK:** an hour lost if wrong [3/10]\n"
    )


# Prose with no ASK/WHY/VALUE/RISK labels at all — parses to ask="".
# This is the shape #1075 is in on the live board.
PROSE_A = (
    "Press every secrets card open on your page. The count keeps changing "
    "and this ask will not name a number again, so it names the class."
)
PROSE_B = (
    "Relaunch the three unbound seats in one quiet window, or tell me to "
    "hold. Nothing is blocked; the drain is working."
)
# A rewording of PROSE_A, still unparseable — a restatement, not a new ask.
PROSE_A_REWORDED = (
    "Press every secrets card open on your page. The count keeps changing "
    "and this ask will not name a number again, so it names the class now."
)

ASK_A = "approve the widget rebuild"
ASK_B = "delete the staging database entirely tonight"


@pytest.fixture
def server(tmp_path: Path):
    return create_server(db_path=tmp_path / "test.db")


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


async def _rows(server, status: str = "all") -> list[dict]:
    return json.loads(
        await _call(server, "decision_list", {"status": status, "format": "json"})
    )


async def _statuses(server) -> dict[int, str]:
    return {r["id"]: r["status"] for r in await _rows(server)}


async def _by_id(server, card_id: int) -> dict:
    return next(r for r in await _rows(server) if r["id"] == card_id)


# ---------------------------------------------------------------------------
# MUST-FIRE 1 — an ASK-less new card supersedes instead of destroying
# ---------------------------------------------------------------------------


async def test_unparsed_new_card_supersedes_a_parsed_open_row(server):
    """A well-formed card, then unrelated prose: the first must survive."""
    await _call(
        server, "decision_put",
        {"from_agent": "squad-proxy-dev-vm-1", "card": _card(ASK_A)},
    )
    out = await _call(
        server, "decision_put",
        {"from_agent": "squad-proxy-dev-vm-1", "card": PROSE_B},
    )
    assert "#2 opened" in out
    assert await _statuses(server) == {1: "superseded", 2: "open"}
    # the point of supersession over overwrite: the first ask is still READABLE
    assert ASK_A in (await _by_id(server, 1))["raw"]
    assert (await _by_id(server, 1))["ask"] == ASK_A


# ---------------------------------------------------------------------------
# MUST-FIRE 2 — an ASK-less OPEN row can be superseded (the #1075 shape)
# ---------------------------------------------------------------------------


async def test_unparsed_open_row_can_be_superseded(server):
    """#1075's live state: an open row with ask="", then an unrelated ask.

    Pre-fix the empty `old` set forced `different` False, so the prose was
    overwritten in place and its text left no record anywhere.
    """
    await _call(
        server, "decision_put",
        {"from_agent": "squad-proxy-dev-vm-1", "card": PROSE_A},
    )
    assert (await _by_id(server, 1))["ask"] == ""  # the parser found no ASK
    out = await _call(
        server, "decision_put",
        {"from_agent": "squad-proxy-dev-vm-1", "card": _card(ASK_B)},
    )
    assert "#2 opened" in out
    assert await _statuses(server) == {1: "superseded", 2: "open"}
    assert "secrets card" in (await _by_id(server, 1))["raw"]


# ---------------------------------------------------------------------------
# MUST-FIRE 3 — two unrelated ASK-less cards are two asks, not one
# ---------------------------------------------------------------------------


async def test_two_unrelated_unparsed_cards_supersede(server):
    """Neither side parses; the raw bodies still say these are different."""
    await _call(
        server, "decision_put",
        {"from_agent": "features-json-dev-vm-1", "card": PROSE_A},
    )
    await _call(
        server, "decision_put",
        {"from_agent": "features-json-dev-vm-1", "card": PROSE_B},
    )
    assert await _statuses(server) == {1: "superseded", 2: "open"}


# ---------------------------------------------------------------------------
# GUARD 1 — the fix must not over-correct into churn
# ---------------------------------------------------------------------------


async def test_unparsed_restate_still_updates_in_place(server):
    """An unparsed REWORDING is a restatement and must NOT open a new card.

    This is why the fix falls back to the raw bodies rather than treating
    "cannot tell" as "supersede": a blanket supersede would churn one dead
    row per rewording, which is the failure the 0.5 threshold exists to
    avoid in the parsed case.
    """
    await _call(
        server, "decision_put",
        {"from_agent": "hub-voice-dev-vm-1", "card": PROSE_A},
    )
    out = await _call(
        server, "decision_put",
        {"from_agent": "hub-voice-dev-vm-1", "card": PROSE_A_REWORDED},
    )
    assert "#1 updated" in out
    assert await _statuses(server) == {1: "open"}
    assert (await _by_id(server, 1))["raw"] == PROSE_A_REWORDED


# ---------------------------------------------------------------------------
# GUARD 2 — the parsed paths are untouched
# ---------------------------------------------------------------------------


async def test_parsed_cards_unaffected(server):
    """Both parsed: the ASK field still decides, exactly as before."""
    await _call(
        server, "decision_put",
        {"from_agent": "money-talks-dev-vm-1", "card": _card(ASK_A)},
    )
    await _call(
        server, "decision_put",
        {"from_agent": "money-talks-dev-vm-1", "card": _card(ASK_B)},
    )
    assert await _statuses(server) == {1: "superseded", 2: "open"}


async def test_parsed_reword_still_updates_in_place(server):
    """The restate path the 0.5 threshold was written for."""
    await _call(
        server, "decision_put",
        {"from_agent": "pm-dev-vm-1", "card": _card(ASK_A)},
    )
    out = await _call(
        server, "decision_put",
        {"from_agent": "pm-dev-vm-1", "card": _card("approve rebuilding the widget")},
    )
    assert "#1 updated" in out
    assert await _statuses(server) == {1: "open"}
