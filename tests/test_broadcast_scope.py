"""Broadcast scoping — who a fleet message actually reaches.

WHY THIS EXISTS, measured 2026-07-27: `broadcast` had no scoping, so one
squad's multi-turn investigation woke every agent on the hub. Three separate
instances of an uninvolved agent (an original and two of its own transported
copies) independently answered into a lane whose context none of them held,
within 21 seconds of each other and unaware of one another.

The unit is a TEAM, not a project and not an org. That squad spanned
dreamteam-ai-labs/{pm,factory-operations,dreamteam,spike} AND
monkeypashion/vps-hetzner — four projects, two orgs, collaborating correctly.
Scoping by project would have severed them; by org would have grouped vps with
the agents that needed excluding. Both cut across the real boundary.

THE TRAP THESE TESTS EXIST FOR: there are TWO delivery paths. A broadcast
reaches an agent by live push AND by the Stop-hook cursor catch-up. Filtering
only the first looks like a fix and is cosmetic — several of the messages that
caused the cross-lane replies arrived through the second.
"""
from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import patch

import pytest

from mcp_hub.server import create_server


@pytest.fixture
def server(tmp_path: Path):
    return create_server(db_path=tmp_path / "test.db")


async def _call(server, name: str, args: dict) -> str:
    result = await server._tool_manager.call_tool(name, args)
    if hasattr(result, "content"):
        for block in result.content:
            if hasattr(block, "text"):
                return block.text
    return str(result)


def _cursor_of(tmp_path: Path, name: str) -> int:
    """The agent's broadcast cursor, read straight from the DB — the stall is
    invisible from the tool's return value, which is '' either way."""
    import sqlite3
    conn = sqlite3.connect(tmp_path / "test.db")
    try:
        row = conn.execute(
            "SELECT last_broadcast_seen_id FROM agents WHERE name = ?", (name,)
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, f"{name} is not registered"
    return row[0]


async def _enrol(server, name: str, project: str, team: str = "") -> None:
    await _call(server, "register",
                {"name": name, "project": project, "team": team})


async def _squad(server) -> None:
    """The real shape that motivated this: one squad, four projects, two orgs —
    plus an uninvolved agent that kept answering into it."""
    await _enrol(server, "pm", "dreamteam-ai-labs/pm", "dreamteam")
    await _enrol(server, "fo", "dreamteam-ai-labs/factory-operations", "dreamteam")
    await _enrol(server, "vps", "monkeypashion/vps-hetzner", "dreamteam")   # other ORG
    await _enrol(server, "hub", "monkeypashion/mcp-hub", "hublane")
    await _enrol(server, "hub-clone", "monkeypashion/mcp-hub", "hublane")


# ---- the live push path ---------------------------------------------------
#
# Observed through broadcast's own "woke N/M" return, where M is the recipient
# count after filtering. registry.names() is faked because these agents have no
# real bound sessions; push_channel itself is a closure inside create_server and
# cannot be patched by name.

FLEET = ["pm", "fo", "vps", "hub", "hub-clone"]


def _recipients(out: str) -> int:
    m = re.search(r"woke \d+/(\d+) connected agents", out)
    assert m, f"no recipient count in: {out}"
    return int(m.group(1))


async def test_a_team_broadcast_reaches_the_team_across_projects_and_orgs(server):
    """The collaboration that WORKED must keep working. vps is in a different
    org from pm and fo and belongs with them anyway."""
    await _squad(server)
    with patch.object(server._hub_registry, "names", lambda: list(FLEET)):
        out = await _call(server, "broadcast",
                          {"from_agent": "pm", "message": "who deleted it?"})
    # fo + vps, not hub or hub-clone, not the sender
    assert _recipients(out) == 2, out


async def test_a_sender_with_no_team_still_reaches_everyone(server):
    """Backwards compatibility, and the safety rule: a group we cannot NAME is
    not a group we may silently exclude people from."""
    await _squad(server)
    await _enrol(server, "stranger", "someone/else")      # no team
    with patch.object(server._hub_registry, "names", lambda: FLEET + ["stranger"]):
        out = await _call(server, "broadcast",
                          {"from_agent": "stranger", "message": "hub redeploying"})
    assert _recipients(out) == len(FLEET), out


async def test_scope_fleet_stays_available_and_explicit(server):
    """A genuine all-hands announcement must still be possible from inside a
    team — it just has to be asked for."""
    await _squad(server)
    with patch.object(server._hub_registry, "names", lambda: list(FLEET)):
        team = await _call(server, "broadcast",
                           {"from_agent": "pm", "message": "x"})
        fleet = await _call(server, "broadcast",
                            {"from_agent": "pm", "message": "hub down", "scope": "fleet"})
    assert _recipients(team) == 2 and _recipients(fleet) == 4, (team, fleet)


async def test_an_unknown_scope_is_refused(server):
    await _squad(server)
    out = await _call(server, "broadcast",
                      {"from_agent": "pm", "message": "x", "scope": "everyone"})
    assert "Invalid scope" in out


# ---- the Stop-hook catch-up path — the one that actually bit --------------

async def test_the_catch_up_path_filters_too(server):
    """THE test. Filtering only the live push is cosmetic: an out-of-team agent
    would still be handed the whole thread at its next turn boundary, which is
    exactly where the cross-lane replies came from."""
    await _squad(server)
    await _call(server, "broadcast", {"from_agent": "pm", "message": "squad-only detail"})

    out_of_team = await _call(server, "get_broadcasts_for_agent",
                              {"agent_name": "hub", "bind": False})
    assert "squad-only detail" not in out_of_team, \
        f"out-of-team agent caught up on it anyway:\n{out_of_team}"

    in_team = await _call(server, "get_broadcasts_for_agent",
                          {"agent_name": "fo", "bind": False})
    assert "squad-only detail" in in_team, f"in-team agent missed it:\n{in_team}"


async def test_a_fleet_broadcast_still_reaches_everyone_on_catch_up(server):
    await _squad(server)
    await _call(server, "broadcast",
                    {"from_agent": "pm", "message": "all hands", "scope": "fleet"})

    out = await _call(server, "get_broadcasts_for_agent",
                      {"agent_name": "hub", "bind": False})
    assert "all hands" in out, out


async def test_a_filtered_row_is_never_re_offered(server):
    """The cursor advances past everything, including rows this agent was never
    party to. Otherwise a filtered row sits at the head of the queue forever and
    every later broadcast is stuck behind it."""
    await _squad(server)
    await _call(server, "broadcast", {"from_agent": "pm", "message": "squad-only"})
    await _call(server, "broadcast",
                    {"from_agent": "pm", "message": "for everyone", "scope": "fleet"})

    first = await _call(server, "get_broadcasts_for_agent",
                        {"agent_name": "hub", "bind": False})
    assert "for everyone" in first and "squad-only" not in first, first
    second = await _call(server, "get_broadcasts_for_agent",
                         {"agent_name": "hub", "bind": False})
    assert "squad-only" not in second and "for everyone" not in second, \
        f"re-offered after the cursor advanced:\n{second}"


async def test_the_cursor_advances_when_the_newest_row_is_filtered_out(server, tmp_path):
    """The case the test above does NOT create, and the one that actually bites.

    That test posts the filtered row FIRST and a visible row after it, so the
    visible row's id absorbs the filtered one and the cursor moves regardless.
    The stall only happens when the filtered row is the NEWEST — which at send
    time is the common case, since a team broadcast is usually the latest thing
    on the feed.

    Caught in review by dev, not by me; my fixture never created the condition.
    """
    await _squad(server)
    await _call(server, "broadcast", {"from_agent": "pm", "message": "squad-only"})

    out = await _call(server, "get_broadcasts_for_agent",
                      {"agent_name": "hub", "bind": False})
    assert out == "", f"a non-member was shown a team row:\n{out}"

    seen = _cursor_of(tmp_path, "hub")
    assert seen > 0, (
        "cursor stalled beneath a filtered row — every later broadcast queues "
        "behind it, and joining that team later dumps the backlog"
    )


async def test_joining_a_team_later_does_not_dump_its_backlog(server):
    """The damage the stall causes, stated as the invariant it breaks: a row
    that was not for you AT SEND TIME must never be re-offered, and the filter
    compares against your CURRENT team. Without the tail absorb, a stalled
    cursor plus a later join floods the agent with history it was never party
    to.

    THE BOUNDARY OF THIS GUARANTEE, stated because it is real: it holds because
    the tail absorb advances the cursor at each catch-up, which assumes
    catch-ups RAN between the team row landing and the join. An agent offline
    for that entire interval, joining a team before its first catch-up, still
    inherits whatever sits above its stalled cursor. Bounded by `limit`, and by
    the fact that those rows were fleet-readable anyway (scope is delivery, not
    confidentiality — see broadcast's docstring). Judged not worth code."""
    await _squad(server)
    await _call(server, "broadcast", {"from_agent": "pm", "message": "old squad business"})
    await _call(server, "get_broadcasts_for_agent", {"agent_name": "hub", "bind": False})

    # hub now joins the squad it was never part of when that was sent.
    await _call(server, "set_team", {"name": "hub", "team": "dreamteam"})
    await _call(server, "broadcast", {"from_agent": "pm", "message": "fresh squad business"})

    out = await _call(server, "get_broadcasts_for_agent",
                      {"agent_name": "hub", "bind": False})
    assert "fresh squad business" in out, f"joined the team but heard nothing:\n{out}"
    assert "old squad business" not in out, \
        f"backlog dumped on joining — re-offered a row it was never party to:\n{out}"


async def test_the_tail_absorb_never_skips_unread_rows_when_limit_bites(server):
    """The tail absorb must not become message LOSS, which would be worse than
    the stall it fixes. It is guarded on the scan reaching the end of the feed
    (len(rows) < limit); when LIMIT truncates instead, there may be visible rows
    beyond, and jumping to MAX(id) would silently skip them."""
    await _squad(server)
    for i in range(5):
        await _call(server, "broadcast",
                    {"from_agent": "pm", "message": f"fleet-{i}", "scope": "fleet"})

    first = await _call(server, "get_broadcasts_for_agent",
                        {"agent_name": "hub", "bind": False, "limit": 2})
    assert "fleet-0" in first and "fleet-1" in first and "fleet-2" not in first, first

    rest = ""
    for _ in range(4):
        rest += await _call(server, "get_broadcasts_for_agent",
                            {"agent_name": "hub", "bind": False, "limit": 2})
    for i in range(5):
        assert f"fleet-{i}" in (first + rest), \
            f"fleet-{i} was skipped — the tail absorb ate an unread row"


async def test_a_broadcast_arriving_mid_call_is_not_silently_absorbed(server, tmp_path):
    """The tail absorb must not eat a row that landed WHILE this call ran.

    These tools are sync defs on FastMCP's threadpool and _get_db connections
    are thread-local, so each statement is its own transaction and a later one
    sees commits an earlier one did not. Reading MAX(id) after the row scan
    would absorb a broadcast that committed in between — advancing the cursor
    past a row never returned, which no future catch-up can offer again
    (id <= cursor). Silent loss, the mark-read-on-push class.

    dev judged the race not practically unit-testable, being a two-thread
    interleaving. It is testable deterministically by injecting the commit at
    the exact point the other thread would have: immediately after the scan.
    The scan's rows are materialised BEFORE the injection so the SELECT itself
    cannot see the new row — otherwise this would test the wrong thing.

    Against the racy order (fence read after the scan) this fails; the four
    other cursor tests pass either way, which is precisely why it exists.
    """
    import time as _time

    import mcp_hub.server as srv
    await _squad(server)
    await _call(server, "broadcast",
                {"from_agent": "pm", "message": "before", "scope": "fleet"})

    real = srv._get_db(tmp_path / "test.db")
    fired = {"done": False}

    class _Rows:
        def __init__(self, rows): self._rows = rows
        def fetchall(self): return self._rows
        def fetchone(self): return self._rows[0] if self._rows else None

    class _Proxy:
        def __getattr__(self, n): return getattr(real, n)

        def execute(self, sql, params=()):
            if not fired["done"] and "ORDER BY id ASC LIMIT" in sql:
                fired["done"] = True
                rows = real.execute(sql, params).fetchall()   # materialise FIRST
                real.execute(                                  # the other thread commits
                    "INSERT INTO messages (ts, from_agent, channel, body, "
                    "priority, audience) VALUES (?, ?, ?, ?, ?, ?)",
                    (_time.time(), "pm", "general", "arrived mid-call", "normal", ""),
                )
                real.commit()
                return _Rows(rows)
            return real.execute(sql, params)

    with patch.object(srv, "_get_db", lambda db_path=None: _Proxy()):
        await _call(server, "get_broadcasts_for_agent",
                    {"agent_name": "hub", "bind": False})
    assert fired["done"], "injection never ran — the test proved nothing"

    out = await _call(server, "get_broadcasts_for_agent",
                      {"agent_name": "hub", "bind": False})
    assert "arrived mid-call" in out, (
        "a broadcast that landed mid-call was absorbed by the tail advance and "
        f"can never be offered again:\n{out}"
    )


# ---- clearing a team ------------------------------------------------------

async def test_set_team_can_clear_so_a_first_assignment_is_not_one_way(server):
    """register(team=...) merges on empty, so empty cannot also mean "remove
    me". Without a tool where the value is authoritative, the first agent to
    try register(team="test") by hand would be stuck in "test" forever — and
    team is a live parameter the moment this deploys."""
    await _squad(server)
    await _call(server, "set_team", {"name": "hub", "team": "dreamteam"})
    await _call(server, "broadcast", {"from_agent": "pm", "message": "squad ping"})
    assert "squad ping" in await _call(
        server, "get_broadcasts_for_agent", {"agent_name": "hub", "bind": False})

    out = await _call(server, "set_team", {"name": "hub", "team": ""})
    assert "cleared" in out.lower(), out

    # The discriminator has to be a TEAM post. Asserting that a FLEET post
    # still arrives proves nothing — fleet rows carry audience='' and reach
    # everyone whether or not the clear worked. (That was this test's first
    # version, and a mutant that made set_team merge-on-empty sailed through
    # it.) After a real clear, hub is no longer in dreamteam, so a dreamteam
    # post must NOT reach it; if the clear silently did nothing, it will.
    await _call(server, "broadcast", {"from_agent": "pm", "message": "squad only now"})
    await _call(server, "broadcast",
                {"from_agent": "pm", "message": "everyone now", "scope": "fleet"})
    after = await _call(server, "get_broadcasts_for_agent",
                        {"agent_name": "hub", "bind": False})
    assert "squad only now" not in after, \
        f"still in the team after clearing it:\n{after}"
    assert "everyone now" in after, f"clearing the team deafened it:\n{after}"


class _FakeSess:
    _write_stream = object()

    async def send_ping(self):
        return None


class _FakeCtx:
    def __init__(self, session):
        self.session = session


async def test_set_team_refuses_to_move_another_agent(server, tmp_path):
    """Moving someone else's team is the destructive form here: they'd be cut
    out of their squad's broadcasts while still believing they were listening —
    the silent-failure sibling of the accidental-impersonation class unregister
    is gated against.

    Reachable through the real boundary, contrary to a comment I wrote here
    earlier: ToolManager.call_tool takes a third `context` parameter and Tool.run
    injects it as the tool's ctx kwarg outside pydantic validation, so a fake
    passes. The "call_tool can't inject a Context" belief was true of this
    file's helper, which simply never passed one — not of the boundary. Found by
    dev; verified against the signature before use.

    The DB assertion is the half a direct _attribution call could never make:
    it proves set_team CONSULTS the gate, not merely that the gate refuses when
    asked."""
    await _squad(server)
    sess = _FakeSess()
    server._hub_registry.bind("hub", sess)

    result = await server._tool_manager.call_tool(
        "set_team", {"name": "pm", "team": "hijacked"}, context=_FakeCtx(sess),
    )
    out = str(getattr(result, "content", result))
    assert "REFUSED" in out, f"a bound session moved another agent's team:\n{out}"

    import sqlite3
    conn = sqlite3.connect(tmp_path / "test.db")
    team = conn.execute(
        "SELECT team FROM agents WHERE name = 'pm'"
    ).fetchone()[0]
    conn.close()
    assert team == "dreamteam", f"refused but wrote anyway — pm is now {team!r}"


# ---- team persistence -----------------------------------------------------

async def test_re_registering_without_a_team_does_not_drop_you_from_it(server):
    """An agent that hasn't learned to send a team yet must not silently leave
    its squad on a reconnect — and today every agent reconnects constantly."""
    await _enrol(server, "fo", "dreamteam-ai-labs/factory-operations", "dreamteam")
    await _call(server, "register",
                {"name": "fo", "project": "dreamteam-ai-labs/factory-operations"})

    await _enrol(server, "pm", "dreamteam-ai-labs/pm", "dreamteam")
    await _call(server, "broadcast", {"from_agent": "pm", "message": "still here?"})

    out = await _call(server, "get_broadcasts_for_agent", {"agent_name": "fo", "bind": False})
    assert "still here?" in out, f"a bare re-register dropped fo from its team:\n{out}"
