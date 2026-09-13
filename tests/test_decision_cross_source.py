"""One open card per AGENT — not per (agent, source).

Every decision lookup partitioned the board by `source` as well as agent:
`WHERE agent=? AND status='open' AND source=?`. `source` records WHICH DOOR
a card arrived through ('stop-hook' from a harvested turn, 'api' from a
service); it was never meant to be a partition of the board, and the
docstring above decision_put has claimed "one OPEN card per agent" since day
one.

The consequences, reported live by reliable-ai-dev on 2026-09-13:
  * a card filed by the Stop hook and RESTATED through a service superseded
    nothing — two rows sat open under one agent name;
  * decision_resolve shares that lookup, so the only close on offer was the
    OLDER face, and a ruling on the newer card was unrecordable.

Corroboration that this was divergence and not design: `decision_answer` —
the operator's plane — has always been agent-scoped and newest-first, with
NO source filter. The operator could ANSWER the card the agent could not
RESOLVE.

NEGATIVE CONTROL. Run this file against the unfixed base (a930a72) and the
cross-source tests FAIL while the same-source and other-agent arms PASS.
The green ones are load-bearing: they are the must-not-regress arms, and a
fix that simply widened every lookup until everything matched would redden
them. A control that only ever goes one way cannot discriminate.
"""

import sqlite3
import time
from pathlib import Path

import pytest

from mcp_hub.server import create_server

ASK_A = "approve the widget rebuild"
ASK_B = "delete the staging database entirely tonight"
# Jaccard vs ASK_A = 3/5 = 0.6, i.e. NOT "different" — a rewording, so the
# supersede guard must leave it as an in-place update.
ASK_A_REWORDED = "approve rebuilding the widget"


def _card(ask: str) -> str:
    return (
        "**DECISION**\n"
        f"**ASK:** {ask}\n"
        "**WHY:** the current one is broken\n"
        "**VALUE:** dashboards work again [7/10]\n"
        "**RISK:** an hour lost if wrong [3/10]\n"
    )


@pytest.fixture
def board(tmp_path: Path):
    """The server AND its db path — some arms must reproduce prod's state.

    The legacy defect left PAIRS of open rows under one agent name. No tool
    can create that once the fix is in, so one test writes the second row
    directly rather than asserting against a state it cannot reach.
    """
    db = tmp_path / "test.db"
    return create_server(db_path=db), db


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


def _rows(db: Path, agent: str = "alice"):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return [
            dict(r) for r in conn.execute(
                "SELECT id, ask, source, status FROM decisions "
                "WHERE agent=? ORDER BY id", (agent,),
            ).fetchall()
        ]
    finally:
        conn.close()


def _statuses(db: Path, agent: str = "alice") -> list[str]:
    return [r["status"] for r in _rows(db, agent)]


# ---------------------------------------------------------------------------
# MUST NOT REGRESS — the working path, from the two live supersessions
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("source", ["api", "stop-hook"])
async def test_same_source_restate_still_supersedes(board, source):
    """#991->#994 (api/api) and #983->#1013 (stop-hook/stop-hook) are real
    supersessions from the live board. They are fixtures here, not
    anecdotes: widening the lookup must not change what already worked."""
    server, db = board
    await _call(server, "decision_put",
                {"from_agent": "alice", "card": _card(ASK_A),
                 "source": source})
    await _call(server, "decision_put",
                {"from_agent": "alice", "card": _card(ASK_B),
                 "source": source})
    assert _statuses(db) == ["superseded", "open"]


async def test_another_agents_open_card_is_never_superseded(board):
    """The arm a fix like this ships broken: dropping `source` from the
    WHERE clause must not also drop `agent`."""
    server, db = board
    await _call(server, "decision_put",
                {"from_agent": "alice", "card": _card(ASK_A),
                 "source": "stop-hook"})
    await _call(server, "decision_put",
                {"from_agent": "bob", "card": _card(ASK_B), "source": "api"})
    assert _statuses(db, "alice") == ["open"]
    assert _statuses(db, "bob") == ["open"]


# ---------------------------------------------------------------------------
# THE DEFECT — a restate through the other door
# ---------------------------------------------------------------------------

async def test_cross_source_restate_supersedes(board):
    """RA's exact shape: stop-hook predecessor, api successor."""
    server, db = board
    await _call(server, "decision_put",
                {"from_agent": "alice", "card": _card(ASK_A),
                 "source": "stop-hook"})
    await _call(server, "decision_put",
                {"from_agent": "alice", "card": _card(ASK_B),
                 "source": "api"})
    assert _statuses(db) == ["superseded", "open"]


async def test_cross_source_restate_the_other_way_too(board):
    """The door is not directional — api predecessor, stop-hook successor."""
    server, db = board
    await _call(server, "decision_put",
                {"from_agent": "alice", "card": _card(ASK_A),
                 "source": "api"})
    await _call(server, "decision_put",
                {"from_agent": "alice", "card": _card(ASK_B),
                 "source": "stop-hook"})
    assert _statuses(db) == ["superseded", "open"]


async def test_cross_source_rewording_updates_in_place(board):
    """Widening the lookup must not weaken the Jaccard guard: a REWORDED
    ask through the other door is still the same card, so it updates in
    place and supersedes nothing. Exactly one row, still open."""
    server, db = board
    first = await _call(server, "decision_put",
                        {"from_agent": "alice", "card": _card(ASK_A),
                         "source": "stop-hook"})
    second = await _call(server, "decision_put",
                         {"from_agent": "alice",
                          "card": _card(ASK_A_REWORDED), "source": "api"})
    rows = _rows(db)
    assert len(rows) == 1 and rows[0]["status"] == "open"
    assert "opened" in first and "updated" in second


# ---------------------------------------------------------------------------
# resolve — the verdict that could not be recorded
# ---------------------------------------------------------------------------

async def test_resolve_closes_a_card_filed_through_the_other_door(board):
    server, db = board
    await _call(server, "decision_put",
                {"from_agent": "alice", "card": _card(ASK_A),
                 "source": "api"})
    out = await _call(server, "decision_resolve",
                      {"from_agent": "alice", "verdict": "approved",
                       "source": "stop-hook"})
    assert "resolved" in out
    assert _statuses(db) == ["decided"]


async def test_resolve_names_the_door_it_closed_across(board):
    """`source` stopped narrowing the lookup, so a caller who passed one is
    owed the difference OUT LOUD — otherwise the widening trades a silent
    miss for a silent surprise."""
    server, _ = board
    await _call(server, "decision_put",
                {"from_agent": "alice", "card": _card(ASK_A),
                 "source": "api"})
    out = await _call(server, "decision_resolve",
                      {"from_agent": "alice", "verdict": "approved",
                       "source": "stop-hook"})
    assert "api" in out


async def test_resolve_explicit_card_no_longer_refuses_on_source(board):
    """`card=<id>` asserts the target. It was refusing a card the agent
    genuinely owned, purely because the doors disagreed."""
    server, db = board
    await _call(server, "decision_put",
                {"from_agent": "alice", "card": _card(ASK_A),
                 "source": "api"})
    card_id = _rows(db)[0]["id"]
    out = await _call(server, "decision_resolve",
                      {"from_agent": "alice", "verdict": "approved",
                       "source": "stop-hook", "card": card_id})
    assert "REFUSED" not in out
    assert _statuses(db) == ["decided"]


async def test_clear_notice_names_a_card_filed_through_the_other_door(board):
    """A notice that went silent because the card came in the other door is
    the exact failure this channel exists to prevent."""
    server, _ = board
    await _call(server, "decision_put",
                {"from_agent": "alice", "card": _card(ASK_A),
                 "source": "api"})
    out = await _call(server, "decision_clear",
                      {"from_agent": "alice", "source": "stop-hook"})
    assert "still open" in out


async def test_two_open_cards_resolve_the_newest_and_name_the_rest(board):
    """Prod already holds pairs of open rows under one name, left by the
    old partition. ORDER BY is not decoration: an unordered fetchone over
    that state returns the OLDEST — the face a restatement was trying to
    replace, and the one a verdict is least likely to be about."""
    server, db = board
    await _call(server, "decision_put",
                {"from_agent": "alice", "card": _card(ASK_A),
                 "source": "stop-hook"})
    # `now + 1` deliberately: the row must be genuinely NEWER than the one
    # decision_put just wrote, or the test asserts nothing about ordering.
    # A literal timestamp here reads as "later" and is epoch-1970, which
    # makes the FIRST card the newest and the assertion pass for the wrong
    # reason — or fail while the code is correct.
    now = time.time()
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO decisions (agent, project, source, submitted_at, "
            "updated_at, raw, ask, why, value_text, risk_text, value_score, "
            "risk_score, net_score, tags, attribution, status) VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'open')",
            ("alice", "", "api", now + 1, now + 1, _card(ASK_B), ASK_B,
             "w", "v", "r", 7, 3, 4, "", "asserted"),
        )
        conn.commit()
    finally:
        conn.close()
    rows = _rows(db)
    assert len(rows) == 2 and [r["status"] for r in rows] == ["open", "open"]
    newest = rows[-1]["id"]
    out = await _call(server, "decision_resolve",
                      {"from_agent": "alice", "verdict": "approved",
                       "source": "stop-hook"})
    assert f"#{newest} resolved" in out
    assert [r["status"] for r in _rows(db)] == ["open", "decided"]
