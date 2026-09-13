"""One open card per AGENT — not per (agent, source).

The defect (2026-09-13, reported by reliable-ai-dev, root-caused here):
`decision_put`, `decision_clear` and `decision_resolve` all looked their
predecessor up with `WHERE agent=? AND status='open' AND source=?`. So a
card filed through the Stop hook (source='stop-hook') and a restatement of
the same ask filed through a service (source='api') were invisible to each
other: the restate superseded NOTHING, both rows sat open under one name,
and `decision_resolve` could only ever offer the older face — which is why
a ruling on the newer card could not be recorded on the board at all.
It was never refusing on merit. It could not see the card.

The comment above `decision_put` said "One OPEN card per agent" the whole
time. The code implemented one per (agent, source). These tests hold the
code to the invariant the comment already claimed.

Fixture shapes are the LIVE ones the deputy named, reproduced by their
structure — the source pair and a low-overlap restate — not by their
verbatim prod text, which is not this suite's to copy:

  #991 -> #994   reliable-ai-dev   api        -> api        (worked before)
  #983 -> #1013  money-talks       stop-hook  -> stop-hook  (worked before)
  #997 -> #998   reliable-ai-dev   stop-hook  -> api        (the defect)

NEGATIVE CONTROL: every test in this file fails against a0604a6.
"""

import json
from pathlib import Path

import pytest

from mcp_hub.server import create_server


def _card(ask: str, value: int = 7, risk: int = 3) -> str:
    return (
        "**DECISION**\n"
        f"**ASK:** {ask}\n"
        "**WHY:** the board needs one face per agent\n"
        f"**VALUE:** the ask is answerable [{value}/10]\n"
        f"**RISK:** an hour lost if wrong [{risk}/10]\n"
    )


# Two asks with token overlap well under 0.5 — a genuine move to a new ask.
ASK_A = "approve the widget rebuild"
ASK_B = "delete the staging database entirely tonight"
# ... and a rewording of ASK_A, overlap 3/5 — a restatement, not a new ask.
ASK_A_REWORDED = "approve rebuilding the widget"


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


# ---------------------------------------------------------------------------
# MUST-FIRE 1 — the working path must not regress (both live same-source pairs)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "agent,source",
    [
        ("reliable-ai-dev", "api"),        # #991 -> #994
        ("money-talks", "stop-hook"),      # #983 -> #1013
    ],
)
async def test_same_source_restate_still_supersedes(server, agent, source):
    """The two pairs that worked before the fix still work after it."""
    first = await _call(
        server, "decision_put",
        {"from_agent": agent, "card": _card(ASK_A), "source": source},
    )
    assert "#1 opened" in first
    second = await _call(
        server, "decision_put",
        {"from_agent": agent, "card": _card(ASK_B), "source": source},
    )
    assert "#2 opened" in second
    assert await _statuses(server) == {1: "superseded", 2: "open"}


# ---------------------------------------------------------------------------
# MUST-FIRE 2 — the defect: a CROSS-source restate supersedes
# ---------------------------------------------------------------------------


async def test_cross_source_restate_supersedes(server):
    """#997 (stop-hook) -> #998 (api): the exact reported shape."""
    await _call(
        server, "decision_put",
        {"from_agent": "reliable-ai-dev", "card": _card(ASK_A),
         "source": "stop-hook"},
    )
    out = await _call(
        server, "decision_put",
        {"from_agent": "reliable-ai-dev", "card": _card(ASK_B),
         "source": "api"},
    )
    assert "#2 opened" in out
    assert await _statuses(server) == {1: "superseded", 2: "open"}
    # and the board shows ONE face for the agent, which is the whole point
    assert len(await _rows(server, "open")) == 1


async def test_cross_source_restate_supersedes_the_other_way_too(server):
    """api predecessor, stop-hook successor — symmetry, not a special case."""
    await _call(
        server, "decision_put",
        {"from_agent": "svc-and-agent", "card": _card(ASK_A), "source": "api"},
    )
    await _call(
        server, "decision_put",
        {"from_agent": "svc-and-agent", "card": _card(ASK_B),
         "source": "stop-hook"},
    )
    assert await _statuses(server) == {1: "superseded", 2: "open"}


# ---------------------------------------------------------------------------
# MUST-FIRE 3 — NOTHING auto-supersedes that should not
# ---------------------------------------------------------------------------


async def test_cross_source_rewording_updates_in_place_and_supersedes_nothing(server):
    """A restatement is not a new ask. Widening the lookup must not widen
    what counts as DIFFERENT: the token-overlap guard still governs, so a
    reworded ask through the other door updates the open card in place and
    marks no row superseded."""
    await _call(
        server, "decision_put",
        {"from_agent": "alice", "card": _card(ASK_A), "source": "stop-hook"},
    )
    out = await _call(
        server, "decision_put",
        {"from_agent": "alice", "card": _card(ASK_A_REWORDED), "source": "api"},
    )
    assert "#1 updated" in out
    assert await _statuses(server) == {1: "open"}          # no second row
    assert "superseded" not in await _call(
        server, "decision_list", {"status": "all"}
    )


async def test_another_agents_open_card_is_never_superseded(server):
    """The lookup drops `source`. It does NOT drop `agent`."""
    await _call(
        server, "decision_put",
        {"from_agent": "bob", "card": _card(ASK_A), "source": "api"},
    )
    await _call(
        server, "decision_put",
        {"from_agent": "alice", "card": _card(ASK_B), "source": "stop-hook"},
    )
    assert await _statuses(server) == {1: "open", 2: "open"}


# ---------------------------------------------------------------------------
# The two READ paths that shared the lookup
# ---------------------------------------------------------------------------


async def test_resolve_closes_a_card_filed_through_the_other_door(server):
    """RA's dead end: the ruling arrives, the agent records it, and the
    hub answers with silence because the card came in the other door."""
    await _call(
        server, "decision_put",
        {"from_agent": "reliable-ai-dev", "card": _card(ASK_A), "source": "api"},
    )
    out = await _call(
        server, "decision_resolve",
        {"from_agent": "reliable-ai-dev", "verdict": "approved by the deputy"},
    )
    assert "#1 resolved" in out
    assert await _statuses(server) == {1: "decided"}


async def test_resolve_names_the_source_it_closed_across(server):
    """Closing across doors is allowed, but never quietly: the receipt says
    which door filed the card it just closed."""
    await _call(
        server, "decision_put",
        {"from_agent": "alice", "card": _card(ASK_A), "source": "api"},
    )
    out = await _call(
        server, "decision_resolve",
        {"from_agent": "alice", "verdict": "yes", "source": "stop-hook"},
    )
    assert "#1 resolved" in out
    assert "api" in out


async def test_resolve_explicit_card_no_longer_refuses_on_source(server):
    """reliable-ai's old call — source='agent-recorded', the note's
    provenance used as if it named the card's origin. It was refused with a
    lecture. `source` no longer narrows, so the close simply works."""
    await _call(server, "decision_put", {"from_agent": "alice", "card": _card(ASK_A)})
    out = await _call(
        server, "decision_resolve",
        {"from_agent": "alice", "verdict": "yes", "card": 1,
         "source": "agent-recorded"},
    )
    assert "REFUSED" not in out
    assert "#1 resolved" in out


async def test_clear_notice_names_a_card_filed_through_the_other_door(server):
    """The owner-notice channel writes no state — its only failure mode is
    saying nothing while the agent's ask sits open on the board."""
    await _call(
        server, "decision_put",
        {"from_agent": "alice", "card": _card(ASK_A), "source": "api"},
    )
    out = await _call(server, "decision_clear", {"from_agent": "alice"})
    assert "#1 still open" in out


# ---------------------------------------------------------------------------
# Legacy state: the rows the defect already left open in pairs
# ---------------------------------------------------------------------------


async def test_two_open_cards_resolve_the_newest_and_name_the_rest(server, tmp_path):
    """The fix stops NEW pairs; it cannot unwrite the ones already filed.
    With two open cards under one name, a bare verdict closes the newest —
    the ask the agent is most likely recording — and the receipt names the
    other so the older one cannot be lost by silence."""
    import sqlite3
    import time

    await _call(
        server, "decision_put",
        {"from_agent": "ra", "card": _card(ASK_A), "source": "stop-hook"},
    )
    # Hand-write the second open row: after the fix the verb itself can no
    # longer create this state, and the point is the state prod is already in.
    conn = sqlite3.connect(tmp_path / "test.db")
    now = time.time()
    conn.execute(
        "INSERT INTO decisions (agent, project, source, submitted_at, "
        "updated_at, raw, ask, why, value_score, risk_score, net_score, "
        "status) VALUES (?,?,?,?,?,?,?,?,?,?,?,'open')",
        ("ra", "", "api", now + 1, now + 1, _card(ASK_B), ASK_B,
         "the board needs one face per agent", 7, 3, 4),
    )
    conn.commit()
    conn.close()

    out = await _call(
        server, "decision_resolve", {"from_agent": "ra", "verdict": "go"},
    )
    assert "#2 resolved" in out          # the newest, not the older face
    assert "#1" in out                   # the other open card is NAMED
    assert "card=" in out                # ... with the remedy for closing it
    assert await _statuses(server) == {1: "open", 2: "decided"}
