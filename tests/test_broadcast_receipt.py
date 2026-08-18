"""A broadcast is marked seen when the agent PROVES it saw it — not when the
push succeeded.

THE DEFECT, measured 2026-07-27 by mcp-hub-dev-vm-1: the hub advanced an
agent's broadcast cursor the moment the live push returned success. Push
success is a statement about a socket, not about an agent. Six broadcast ids
(10346-10351) were advanced past dev's cursor while its GET stream was
provably dead; they were recoverable only by a hand read of the database.

That is the mark-read-on-push class — the worst bug in this hub's history —
and it was fixed for DMs in PR #8 with a per-recipient `pushed_gen` on the
message row. A broadcast row is SHARED by every recipient, so it cannot carry
a per-recipient anything, and that asymmetry is why broadcasts were
consciously left broken. The per-recipient fact simply had to live on the
per-recipient row instead (`agents.broadcast_pending_*`).

Three triggers are on record for the dead stream — redeploy churn, box sleep,
wifi flap — and the cursor advancing is precisely what silences the Stop-hook
catch-up that would otherwise be the backstop. So the loss is total and
silent: no error, no queue, nothing to notice.

⭐ The gate is EVIDENCE, not transport, and that is load-bearing. dev warned
that a SECOND mechanism produces identical hub-side symptoms — a channel
notification the client cannot parse (strict pydantic validation on the
notification params) is a push the agent never receives while every
server-side transport signal reports success. No deliverability gate can see
that. This one does, because it asks whether the AGENT did anything, and an
agent that never rendered the wake never acts on it.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mcp_hub.server import create_server


@pytest.fixture
def server(tmp_path: Path):
    return create_server(db_path=tmp_path / "test.db")


class _Stream:
    """A bound session that accepts every notification.

    It accepts them the way a DEAD stream does: `send_notification` succeeding
    is exactly the signal that misled the old code, so the fake must reproduce
    that and nothing more. Whether the agent ever SAW anything is expressed
    separately, by whether it acks.
    """

    _write_stream = object()

    def __init__(self) -> None:
        self.sent: list[object] = []

    async def send_ping(self):
        return None

    async def send_notification(self, notification):
        self.sent.append(notification)


async def _call(server, name: str, args: dict) -> str:
    result = await server._tool_manager.call_tool(name, args)
    if hasattr(result, "content"):
        for block in result.content:
            if hasattr(block, "text"):
                return block.text
    return str(result)


def _row(tmp_path: Path, name: str) -> sqlite3.Row:
    conn = sqlite3.connect(tmp_path / "test.db")
    conn.row_factory = sqlite3.Row
    try:
        r = conn.execute(
            "SELECT last_broadcast_seen_id AS cursor, broadcast_pending_id AS pending, "
            "broadcast_pending_gen AS gen FROM agents WHERE name = ?", (name,),
        ).fetchone()
    finally:
        conn.close()
    assert r is not None, f"{name} is not registered"
    return r


async def _pair(server) -> _Stream:
    """A sender and a bound listener, both in one squad."""
    await _call(server, "register",
                {"name": "sender", "project": "org/a", "squads": "team"})
    await _call(server, "register",
                {"name": "listener", "project": "org/b", "squads": "team"})
    stream = _Stream()
    server._hub_registry.bind("listener", stream)
    return stream


async def _drain(server, name: str = "listener") -> str:
    """What the Stop hook would surface. bind=False, as the real hook passes."""
    return await _call(server, "get_broadcasts_for_agent",
                       {"agent_name": name, "bind": False})


# ---- the defect ------------------------------------------------------------

async def test_a_push_into_a_stream_that_never_rendered_is_NOT_marked_seen(
    server, tmp_path,
):
    """dev's specimen, in miniature. The push succeeds — the socket takes the
    bytes — and the agent never shows any sign of having received it. The old
    code advanced the cursor here, and the broadcast was gone for good."""
    stream = await _pair(server)
    await _call(server, "broadcast",
                {"from_agent": "sender", "message": "the one that went missing",
                    "priority": "urgent"})
    assert stream.sent, "the fixture never pushed — this test proves nothing"

    out = await _drain(server)
    assert "the one that went missing" in out, \
        "a successful push silenced the catch-up: the broadcast is lost"


async def test_the_cursor_does_not_move_at_push_time(server, tmp_path):
    """The tool's return value is '' whether the cursor stalled or the message
    was eaten, so the DB is the only place this is visible — the same reason
    the scoping tests read it directly."""
    await _pair(server)
    before = _row(tmp_path, "listener")["cursor"]
    await _call(server, "broadcast", {"from_agent": "sender", "message": "hi",
        "priority": "urgent"})
    after = _row(tmp_path, "listener")
    assert after["cursor"] == before, \
        "the cursor advanced on push success — the defect is still here"
    assert after["pending"] > before, "the push was not recorded as pending"
    assert after["gen"], "pending recorded with no generation to validate it against"


# ---- and yet no double-surfacing -------------------------------------------

async def test_an_agent_that_PROVED_it_rendered_is_not_shown_it_again(server):
    """The other half, and the constraint dev banked with the defect: a fix
    that made every live broadcast surface a second time at the next Stop
    boundary would just be the sibling failure of the incident it fixes.

    `wake_ack` is the evidence — it is recorded when the agent independently
    binds or drains, i.e. does something only a rendering session does.
    """
    await _pair(server)
    await _call(server, "broadcast",
                {"from_agent": "sender", "message": "seen live", "priority": "urgent"})
    server._hub_registry.wake_ack("listener")      # the agent acted on the wake

    out = await _drain(server)
    assert out == "", f"shown twice to an agent that demonstrably saw it:\n{out}"


async def test_the_promotion_actually_moves_the_cursor(server, tmp_path):
    """Not merely 'returns nothing' — an empty return is also what a stalled
    cursor with no new rows looks like."""
    await _pair(server)
    await _call(server, "broadcast", {"from_agent": "sender", "message": "x", "priority": "urgent"})
    pending = _row(tmp_path, "listener")["pending"]
    server._hub_registry.wake_ack("listener")
    await _drain(server)
    assert _row(tmp_path, "listener")["cursor"] >= pending


# ---- the generation is what makes a pending push safe to promote -----------

async def test_a_REBIND_between_push_and_drain_refuses_the_promotion(server):
    """The relaunch case, which is the common one: the stream dies, the
    operator restarts the agent, and it binds fresh. That new session never
    received the broadcast, so promoting the old push against it would be the
    original silent loss wearing a repair's clothes — and `bind` itself counts
    as an ack, so without the generation check this WOULD promote.
    """
    await _pair(server)
    await _call(server, "broadcast",
                {"from_agent": "sender", "message": "sent to the old session",
                    "priority": "urgent"})
    server._hub_registry.bind("listener", _Stream())   # relaunched: new generation

    out = await _drain(server)
    assert "sent to the old session" in out, \
        "a broadcast was promoted against a session that never received it"


async def test_an_unbound_agent_is_never_promoted(server):
    """No binding means no push, so there is nothing that could be claimed as
    seen. The catch-up is this agent's ONLY delivery path."""
    await _call(server, "register",
                {"name": "sender", "project": "org/a", "squads": "team"})
    await _call(server, "register",
                {"name": "drifted", "project": "org/b", "squads": "team"})
    await _call(server, "broadcast", {"from_agent": "sender", "message": "catch up",
        "priority": "urgent"})
    assert "catch up" in await _drain(server, "drifted")


# ---- ordering: the ack must be read before it is consumed ------------------

async def test_the_drains_OWN_ack_does_not_authorise_its_own_promotion(server):
    """`get_broadcasts_for_agent` calls `wake_ack` — it is itself agent
    activity. Read the render evidence AFTER that call and it always says
    'proven', so the gate is permanently open and the whole fix is inert while
    every test that only checks the happy path still passes.

    This is the one that fails if someone tidies the two lines into their
    'natural' order, so it is worth more than the rest put together: the drain
    below is the FIRST thing the agent does after the push.
    """
    stream = await _pair(server)
    await _call(server, "broadcast",
                {"from_agent": "sender", "message": "nobody acked this", "priority": "urgent"})
    assert stream.sent
    out = await _drain(server)
    assert "nobody acked this" in out, \
        "the drain's own wake_ack authorised the promotion — the gate is inert"


# ---- interaction with the parts that were already right --------------------

async def test_a_muted_squads_broadcast_is_still_filtered(server):
    """Promotion must not become a second way for a row to arrive. Mute
    suppresses BOTH delivery paths and this adds machinery to one of them."""
    await _pair(server)
    await _call(server, "mute_squad", {"name": "listener", "squad": "team"})
    await _call(server, "broadcast", {"from_agent": "sender", "message": "muted",
        "priority": "urgent"})
    assert "muted" not in await _drain(server)


async def test_a_second_drain_returns_nothing(server):
    """Read-marks-seen still holds on the catch-up path: whatever the promotion
    did or didn't do, a drain that RETURNED a row has shown it."""
    await _pair(server)
    await _call(server, "broadcast", {"from_agent": "sender", "message": "once",
        "priority": "urgent"})
    assert "once" in await _drain(server)
    assert await _drain(server) == "", "the same broadcast surfaced twice"


# ---- the race between the push and the stamp ------------------------------

async def test_a_REBIND_DURING_the_push_is_not_stamped_with_the_new_generation(
    server,
):
    """The TOCTOU, made deterministic by injecting the rebind at exactly the
    point the racing thread would hit it — the same technique that turned the
    catch-up fence race from 'not practically unit-testable' into a test.

    If the generation is read back AFTER the fan-out instead of captured at
    push time, an agent that rebinds in between is stamped with the token of
    the session that did NOT receive the broadcast. That token then matches at
    drain time, the promotion succeeds, and the message is silently lost — the
    original defect, reintroduced by the mechanism meant to end it, and
    reachable by nothing more exotic than a relaunch landing mid-broadcast.
    """
    stream = await _pair(server)
    reg = server._hub_registry

    async def rebind_mid_push(notification):
        stream.sent.append(notification)
        # The relaunch lands: same agent, new session, new generation.
        reg.bind("listener", _Stream())

    stream.send_notification = rebind_mid_push
    await _call(server, "broadcast",
                {"from_agent": "sender", "message": "raced by a relaunch", "priority": "urgent"})
    assert stream.sent, "the injection point was never reached"

    # The relaunched agent then does ordinary work, which acks. This line is
    # what makes the test about the GENERATION rather than about the render
    # evidence: without it the new session still has an unacked wake
    # expectation, the evidence gate blocks the promotion on its own, and the
    # test passes whether or not the generation was captured correctly —
    # vacuous, and it was, until a mutation of the capture failed to kill it.
    reg.wake_ack("listener")

    out = await _drain(server)
    assert "raced by a relaunch" in out, \
        "stamped with the generation of a session that never received it"


# ---- a pending run may never span a generation change ---------------------

async def test_an_id_pushed_to_a_DEAD_generation_is_not_promoted_by_a_later_one(
    server, tmp_path,
):
    """🔴 Found by mcp-hub-dev-vm-1 by reading, 2026-08-07, in the first
    version of this fix — the fix reintroducing the bug it was written for.

    The generation validates the LAST push, but the promotion covers EVERY id
    at or below `pending`. A plain MAX() erased which generation the earlier
    claim belonged to:

        cursor=10 · 11 pushed to G1 · deploy kills G1 before it renders ·
        rebind G2 · 12 pushed to G2 and rendered · agent acks ·
        drain promotes to 12 -> 11 excluded from that select and every later
        one.

    And it bites on exactly the deploy-churn trigger the whole card was opened
    for. Note this is NOT the 'promotion only suppresses a dup' case I claimed
    when I sent the diff: promotion happens BEFORE the message select, so it
    changes what the drain RETURNS, and returned-vs-not is the entire defect.
    """
    await _pair(server)
    reg = server._hub_registry

    await _call(server, "broadcast",
                {"from_agent": "sender", "message": "pushed to the doomed stream",
                    "priority": "urgent"})

    # The deploy: that stream dies without ever rendering, and the agent comes
    # back on a new session. No ack in between — nothing proved anything.
    reg.bind("listener", _Stream())

    await _call(server, "broadcast",
                {"from_agent": "sender", "message": "pushed to the live one", "priority": "urgent"})
    reg.wake_ack("listener")          # the new session does ordinary work

    out = await _drain(server)
    assert "pushed to the doomed stream" in out, \
        "an id pushed to a dead generation was marked seen by a later push"
    assert "pushed to the live one" in out, \
        "the whole range should be re-offered — dup is the accepted direction"


async def test_a_run_within_ONE_generation_is_still_promoted_whole(server):
    """The other side of it: several broadcasts to one live stream form ONE
    run, and a single render proves all of them. Refusing that would make
    every busy period re-surface its whole backlog at the next Stop boundary.
    """
    await _pair(server)
    for i in range(3):
        await _call(server, "broadcast",
                    {"from_agent": "sender", "message": f"burst {i}",
                     "priority": "urgent"})
    server._hub_registry.wake_ack("listener")
    assert await _drain(server) == "", "a single-generation run was not promoted"


async def test_the_stale_pending_stops_blocking_once_the_drain_catches_up(server):
    """The refusal must not be permanent. After the catch-up has carried the
    cursor past the abandoned ids, the next push starts a clean run and
    promotion works again — otherwise one deploy would disable live-broadcast
    suppression for that agent forever."""
    await _pair(server)
    reg = server._hub_registry
    await _call(server, "broadcast", {"from_agent": "sender", "message": "old gen",
        "priority": "urgent"})
    reg.bind("listener", _Stream())
    await _call(server, "broadcast", {"from_agent": "sender", "message": "new gen",
        "priority": "urgent"})
    reg.wake_ack("listener")
    assert "old gen" in await _drain(server)          # the catch-up happens

    await _call(server, "broadcast", {"from_agent": "sender", "message": "after",
        "priority": "urgent"})
    reg.wake_ack("listener")
    assert await _drain(server) == "", \
        "the agent is stuck re-reading live broadcasts after one deploy"


async def test_a_JUST_PROMOTED_agent_starts_a_clean_run_on_the_next_generation(
    server, tmp_path,
):
    """The `pending <= cursor` boundary, at exactly `==` (dev's mutation 2).

    After a promotion the cursor sits ON the pending id. If the fresh-run test
    were `<` rather than `<=`, that state would read as 'a run is outstanding'
    forever, and the very next generation change would refuse to stamp for good
    — one deploy after one live broadcast, and the agent never gets live
    suppression again. The boundary is one character wide and only this
    arrangement touches it.
    """
    await _pair(server)
    reg = server._hub_registry
    await _call(server, "broadcast", {"from_agent": "sender", "message": "first",
        "priority": "urgent"})
    reg.wake_ack("listener")
    await _drain(server)                                  # promote: cursor == pending
    row = _row(tmp_path, "listener")
    assert row["cursor"] == row["pending"], \
        f"fixture did not reach the == boundary: {tuple(row)}"

    reg.bind("listener", _Stream())                       # new generation
    await _call(server, "broadcast", {"from_agent": "sender", "message": "second",
        "priority": "urgent"})
    reg.wake_ack("listener")
    assert await _drain(server) == "", \
        "a clean cursor was mistaken for an outstanding run — suppression is dead"


# ---- what is NOT witnessed here -------------------------------------------
#
# The stamp is one atomic UPDATE with CASE expressions rather than a
# read-then-write, because these tools are sync defs on a threadpool with
# thread-local connections: every statement is its own transaction, so a
# read-then-write could interleave two broadcasts and lose the refuse branch.
#
# ⚠️ NO TEST HERE DISTINGUISHES THAT. Mutating the atomic statement into a
# read-then-write leaves this file green (dev, 2026-08-07 — concurrency mutants
# rarely have cheap witnesses). It is recorded rather than quietly relied upon,
# so the comment at the stamp is knowingly carrying that load alone. If this
# ever needs a witness, the technique is the one that caught the catch-up fence
# race: inject the competing commit at exactly the point the other thread would
# reach it, rather than hoping a sleep lands in the window.


# ---- the migration, against a database that already has agents ------------

def test_the_migration_preserves_existing_cursors_and_is_idempotent(tmp_path):
    """The one path that only ever runs in production.

    Every other test here starts from a fresh database. The deploy does not:
    it runs `init_db` against a live DB whose agents already carry real
    cursors, and a migration that reset or shifted those would hand the whole
    fleet a backlog (or eat one) at the moment of deploy — invisibly, because
    nothing compares before and after.

    Rewinding by dropping the two columns is a fair stand-in for the
    pre-migration schema: they are exactly what the old DB lacks.
    """
    import time as _t

    from mcp_hub.server import init_db

    db = tmp_path / "prod.db"
    init_db(db)
    conn = sqlite3.connect(db)
    conn.execute("ALTER TABLE agents DROP COLUMN broadcast_pending_id")
    conn.execute("ALTER TABLE agents DROP COLUMN broadcast_pending_gen")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(agents)")}
    assert "broadcast_pending_id" not in cols, "the rewind did not rewind"
    now = _t.time()
    seeded = {"alpha": 10346, "beta": 0, "gamma": 99999}
    for name, cur in seeded.items():
        conn.execute(
            "INSERT INTO agents (name, project, bio, registered, last_seen, "
            "last_broadcast_seen_id) VALUES (?,?,?,?,?,?)",
            (name, "org/x", "", now, now, cur),
        )
    conn.commit()
    conn.close()

    init_db(db)          # the migration
    init_db(db)          # and again — a redeploy re-runs it

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rows = list(conn.execute(
        "SELECT name, last_broadcast_seen_id AS cur, broadcast_pending_id AS pend, "
        "broadcast_pending_gen AS gen FROM agents"))
    conn.close()
    assert {r["name"]: r["cur"] for r in rows} == seeded, \
        "the migration moved an existing agent's cursor"
    # Nothing pending is the truthful state for a pre-migration hub: it
    # advanced on push, so it has no outstanding claim to carry forward.
    assert all(r["pend"] == 0 and r["gen"] == "" for r in rows), rows
