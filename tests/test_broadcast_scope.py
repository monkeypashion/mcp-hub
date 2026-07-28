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


async def _enrol(server, name: str, project: str, squads: str = "") -> None:
    await _call(server, "register",
                {"name": name, "project": project, "squads": squads})


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


async def test_a_sender_with_no_squad_is_REFUSED_not_sent_fleet_wide(server):
    """This assertion is INVERTED from the version shipped hours earlier, and
    the inversion is the point.

    That build let a squadless sender reach everyone, reasoning that a group we
    cannot NAME is not one we may silently exclude people from. The operator's
    model reverses which way the danger runs: with squads explicit, an agent
    that belongs to none is precisely the one that must NOT reach the whole
    fleet — a squadless broadcast going everywhere IS the 2026-07-27 incident.

    So it refuses, loudly, naming the alternatives. Loud-and-instructive beats
    silent-and-fleet-wide."""
    await _squad(server)
    await _enrol(server, "stranger", "someone/else")      # no squad
    with patch.object(server._hub_registry, "names", lambda: FLEET + ["stranger"]):
        out = await _call(server, "broadcast",
                          {"from_agent": "stranger", "message": "hub redeploying"})
    assert "belongs to no squad" in out, out
    assert "send()" in out and "fleet" in out, f"refusal must say what to do instead: {out}"
    assert "woke" not in out, f"refused but delivered anyway: {out}"


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


async def test_broadcasting_to_a_squad_you_are_not_in_is_refused(server):
    await _squad(server)
    out = await _call(server, "broadcast",
                      {"from_agent": "pm", "message": "x", "scope": "hublane"})
    assert "not in squad 'hublane'" in out, out
    assert "woke" not in out, f"refused but delivered anyway: {out}"


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
    await _call(server, "set_squads", {"name": "hub", "squads": "dreamteam"})
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


# ---- multi-squad membership ------------------------------------------------

async def test_an_agent_can_belong_to_several_squads_and_hears_all_of_them(server):
    """Operator, 2026-07-27: "an agent can be in any number of squads (like a
    human developer can)". That is why membership is a table and not a column,
    and it is where the workspace model lands — an agent sitting in three squad
    workspaces is in three squads."""
    await _squad(server)
    await _call(server, "set_squads", {"name": "hub", "squads": "dreamteam, hublane"})

    await _call(server, "broadcast", {"from_agent": "pm", "message": "from dreamteam"})
    await _call(server, "broadcast",
                {"from_agent": "hub-clone", "message": "from hublane"})

    out = await _call(server, "get_broadcasts_for_agent",
                      {"agent_name": "hub", "bind": False})
    assert "from dreamteam" in out and "from hublane" in out, \
        f"a member of two squads must hear both:\n{out}"


async def test_a_sender_in_several_squads_must_say_which_one(server):
    """Not guessed on purpose. Picking one for the sender is how a message
    reaches the wrong lane — the failure this whole feature exists to stop."""
    await _squad(server)
    await _call(server, "set_squads", {"name": "hub", "squads": "dreamteam,hublane"})

    out = await _call(server, "broadcast", {"from_agent": "hub", "message": "which?"})
    assert "name the one you mean" in out, out
    assert "dreamteam" in out and "hublane" in out, f"refusal must list them: {out}"
    assert "woke" not in out, f"refused but delivered anyway: {out}"

    ok = await _call(server, "broadcast",
                     {"from_agent": "hub", "message": "this one", "scope": "hublane"})
    assert "woke" in ok, f"naming the squad should have worked: {ok}"


async def test_set_squads_is_authoritative_and_can_empty_the_list(server):
    """register(squads=) is additive and treats empty as "no opinion", so it can
    never remove. This is the tool that can — including all the way to none."""
    await _squad(server)
    await _call(server, "set_squads", {"name": "hub", "squads": "dreamteam,hublane"})
    out = await _call(server, "set_squads", {"name": "hub", "squads": "hublane"})
    assert "hublane" in out and "dreamteam" not in out, out

    await _call(server, "broadcast", {"from_agent": "pm", "message": "dreamteam only"})
    after = await _call(server, "get_broadcasts_for_agent",
                        {"agent_name": "hub", "bind": False})
    assert "dreamteam only" not in after, f"still in dreamteam after removal:\n{after}"

    emptied = await _call(server, "set_squads", {"name": "hub", "squads": ""})
    assert "no squad" in emptied, emptied


async def test_register_is_additive_so_a_reconnect_cannot_evict_a_squad(server):
    """A register naming two squads must not remove a third the agent was put in
    from somewhere else — a settings dialogue, or another workspace. Every agent
    reconnects constantly; a reconnect must never be a membership edit."""
    await _squad(server)
    await _call(server, "set_squads", {"name": "hub", "squads": "dreamteam,hublane"})
    await _enrol(server, "hub", "monkeypashion/mcp-hub", "hublane")   # reconnect

    out = await _call(server, "list_squads", {"agent": "hub"})
    assert "dreamteam" in out and "hublane" in out, \
        f"a reconnect evicted a squad it did not name:\n{out}"


# ---- mute: membership and attention are different things -------------------

async def test_muting_a_squad_silences_BOTH_delivery_paths(server):
    """Operator: "individual members of the squad can switch off from receiving
    the broadcasts". Muting only the push would leave the whole thread waiting
    at the next Stop boundary, which is not being switched off — it is being
    delayed. Both paths, or it is not a mute."""
    await _squad(server)
    await _call(server, "mute_squad", {"name": "fo", "squad": "dreamteam"})

    with patch.object(server._hub_registry, "names", lambda: list(FLEET)):
        out = await _call(server, "broadcast",
                          {"from_agent": "pm", "message": "noisy thread"})
    assert _recipients(out) == 1, f"muted member was still woken: {out}"   # vps only

    caught = await _call(server, "get_broadcasts_for_agent",
                         {"agent_name": "fo", "bind": False})
    assert "noisy thread" not in caught, \
        f"muted on push but delivered at the Stop boundary:\n{caught}"


async def test_mute_is_per_squad_not_per_agent(server):
    """Stay in three squads, silence one. Muting the agent instead of the pair
    would make "not right now" mean "leave everything"."""
    await _squad(server)
    await _call(server, "set_squads", {"name": "hub", "squads": "dreamteam,hublane"})
    await _call(server, "mute_squad", {"name": "hub", "squad": "dreamteam"})

    # Markers must not be substrings of one another: the first version used
    # "muted one"/"unmuted one", and "muted one" IS inside "unmuted one", so the
    # leak assertion could never fail for the reason it named.
    await _call(server, "broadcast", {"from_agent": "pm", "message": "ALPHA-thread"})
    await _call(server, "broadcast",
                {"from_agent": "hub-clone", "message": "BETA-thread"})
    out = await _call(server, "get_broadcasts_for_agent",
                      {"agent_name": "hub", "bind": False})
    assert "ALPHA-thread" not in out, f"mute leaked across squads:\n{out}"
    assert "BETA-thread" in out, f"muting one squad deafened the other:\n{out}"


async def test_a_muted_member_is_still_a_member(server):
    """Mute is attention, not membership: you can still ADDRESS a squad you have
    silenced, and you still appear in it."""
    await _squad(server)
    await _call(server, "mute_squad", {"name": "fo", "squad": "dreamteam"})

    out = await _call(server, "broadcast",
                      {"from_agent": "fo", "message": "I can still speak"})
    assert "woke" in out, f"a muted member lost the ability to broadcast: {out}"
    assert "dreamteam" in await _call(server, "list_squads", {"agent": "fo"})


async def test_unmuting_does_not_backfill_what_was_muted(server):
    """A mute that merely defers the interruption has not removed it. Rows sent
    while muted are gone for that agent, not queued behind the unmute."""
    await _squad(server)
    await _call(server, "mute_squad", {"name": "fo", "squad": "dreamteam"})
    await _call(server, "broadcast", {"from_agent": "pm", "message": "while muted"})
    await _call(server, "get_broadcasts_for_agent", {"agent_name": "fo", "bind": False})

    await _call(server, "mute_squad",
                {"name": "fo", "squad": "dreamteam", "muted": False})
    await _call(server, "broadcast", {"from_agent": "pm", "message": "after unmute"})
    out = await _call(server, "get_broadcasts_for_agent",
                      {"agent_name": "fo", "bind": False})
    assert "after unmute" in out, f"unmute did not restore delivery:\n{out}"
    assert "while muted" not in out, f"unmute backfilled the muted rows:\n{out}"


async def test_muting_a_squad_you_are_not_in_is_refused(server):
    await _squad(server)
    out = await _call(server, "mute_squad", {"name": "hub", "squad": "dreamteam"})
    assert "not in squad" in out, out


# ---- the reserved name -----------------------------------------------------

async def test_fleet_cannot_be_used_as_a_squad_name(server):
    """scope="fleet" means everyone. A squad of that name would make one word
    mean two things, so it is refused where it would be created rather than
    resolved by a precedence rule nobody will remember."""
    out = await _call(server, "register",
                      {"name": "x", "project": "p", "squads": "fleet"})
    assert "reserved" in out, out
    await _enrol(server, "y", "p", "real")
    out2 = await _call(server, "set_squads", {"name": "y", "squads": "fleet"})
    assert "reserved" in out2, out2


class _FakeSess:
    _write_stream = object()

    async def send_ping(self):
        return None


class _FakeCtx:
    def __init__(self, session):
        self.session = session


async def test_set_squads_refuses_to_move_another_agent(server, tmp_path):
    """Moving someone else's membership is the destructive form here: they'd be
    cut out of their squads' broadcasts while still believing they were
    listening — the silent-failure sibling of the impersonation class that
    unregister is gated against.

    Reachable through the real boundary: ToolManager.call_tool takes a third
    `context` parameter and Tool.run injects it as the tool's ctx kwarg outside
    pydantic validation, so a fake passes. The DB assertion is the half a direct
    _attribution call could never make — it proves set_squads CONSULTS the gate,
    not merely that the gate refuses when asked."""
    await _squad(server)
    sess = _FakeSess()
    server._hub_registry.bind("hub", sess)

    result = await server._tool_manager.call_tool(
        "set_squads", {"name": "pm", "squads": "hijacked"}, context=_FakeCtx(sess),
    )
    out = str(getattr(result, "content", result))
    assert "REFUSED" in out, f"a bound session moved another agent's squads:\n{out}"

    import sqlite3
    conn = sqlite3.connect(tmp_path / "test.db")
    rows = [r[0] for r in conn.execute(
        "SELECT squad FROM squad_members WHERE agent = 'pm'").fetchall()]
    conn.close()
    assert rows == ["dreamteam"], f"refused but wrote anyway — pm is now in {rows}"


# ---- membership persistence ------------------------------------------------

async def test_re_registering_without_squads_does_not_drop_you_from_them(server):
    """An agent that hasn't learned to send squads yet must not silently leave
    its squad on a reconnect — and today every agent reconnects constantly."""
    await _enrol(server, "fo", "dreamteam-ai-labs/factory-operations", "dreamteam")
    await _call(server, "register",
                {"name": "fo", "project": "dreamteam-ai-labs/factory-operations"})

    await _enrol(server, "pm", "dreamteam-ai-labs/pm", "dreamteam")
    await _call(server, "broadcast", {"from_agent": "pm", "message": "still here?"})

    out = await _call(server, "get_broadcasts_for_agent", {"agent_name": "fo", "bind": False})
    assert "still here?" in out, f"a bare re-register dropped fo from its team:\n{out}"


# ---- the legacy migration must not resurrect what was removed --------------

def test_legacy_team_migration_runs_once_not_on_every_restart(tmp_path):
    """FOUND IN PRODUCTION, 2026-07-28: a squad I had deliberately retired came
    BACK after a redeploy.

    init_db re-runs on every hub start, and the migration that carries the old
    single-`team` column into squad_members re-imported from a column that
    still held the old value. So leaving a squad did not survive a restart —
    an agent removed from a squad would silently rejoin on the next deploy,
    while everyone believed the removal had stuck.

    The fix is to CLEAR the legacy column once its value has been carried over,
    which makes the import genuinely one-shot rather than merely idempotent
    against its own output.
    """
    import sqlite3

    from mcp_hub.server import init_db

    db = tmp_path / "legacy.db"
    init_db(db)
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO agents (name, project, status, registered, last_seen, team) "
        "VALUES ('ghost', 'p', 'online', 0, 0, 'oldsquad')"
    )
    conn.commit()
    conn.close()

    init_db(db)                      # first restart: carries it over
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT squad FROM squad_members WHERE agent='ghost'").fetchall()
    assert rows == [("oldsquad",)], f"migration did not carry the value: {rows}"

    # The agent LEAVES — exactly what set_squads(name, "") does.
    conn.execute("DELETE FROM squad_members WHERE agent='ghost'")
    conn.commit()
    conn.close()

    init_db(db)                      # second restart: must NOT resurrect it
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT squad FROM squad_members WHERE agent='ghost'").fetchall()
    conn.close()
    assert rows == [], f"a restart resurrected a squad the agent had left: {rows}"
