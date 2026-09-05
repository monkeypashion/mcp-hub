"""Card #59 — wake-batching, tested against the operator-signed contract.

Low/normal hub traffic stops firing its own wake and rides the recipient's
next natural turn instead (the single biggest measured usage lever: halving
one lane's wakes ≈ 47M tokens/week). Written BEFORE the implementation, per
the operator's standing tests-first law.

The five rules:
  R1 no message is ever lost or reordered within a sender's stream;
  R2 urgent always wakes immediately — unchanged;
  R3 a direct reply to something an agent sent in its LAST TURN wakes it
     immediately regardless of priority — batching must never slow an
     active conversation;
  R4 everything else queues with a maximum holding time (HOLD_MAX_SECONDS)
     so nothing rots in a quiet lane;
  R5 wakes are MEASURED — every delivered wake logs a reason, so the
     before/after ledger has a server-side witness.

"Last turn" is bounded by the recipient's idle marks: a message they sent
after their second-to-last idle transition (prev_idle_at) is last-turn or
current-turn; anything older is history and a reply to it batches like
everything else.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mcp_hub.server import HOLD_MAX_SECONDS, create_server


@pytest.fixture
def server(tmp_path: Path):
    return create_server(db_path=tmp_path / "test.db")


class _Stream:
    """A bound session that accepts every notification (the way a dead
    stream also does — delivery here is transport success, nothing more)."""

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
    if isinstance(result, list):
        for block in result:
            if hasattr(block, "text"):
                return block.text
    return result if isinstance(result, str) else str(result)


async def _pair(server) -> _Stream:
    """alice sends, bob receives; bob is bound and idle (the state that
    used to fire the Case 1 low-priority wake)."""
    await _call(server, "register", {"name": "alice", "project": "p"})
    await _call(server, "register", {"name": "bob", "project": "p"})
    stream = _Stream()
    server._hub_registry.bind("bob", stream)
    # An idle mark, as bob's Stop hook would leave him.
    await _call(server, "get_messages",
                {"agent_name": "bob", "bind": False, "mark_idle": True})
    stream.sent.clear()
    return stream


def _wakes(stream: _Stream) -> int:
    return len(stream.sent)


def _db(server):
    import sqlite3

    conn = sqlite3.connect(server._hub_db_path)
    conn.row_factory = sqlite3.Row
    return conn


# ---- the lever itself: low/normal no longer self-wake ----------------------


async def test_normal_dm_queues_instead_of_waking(server):
    stream = await _pair(server)
    out = await _call(server, "send",
                      {"from_agent": "alice", "to": "bob", "message": "fyi"})
    assert _wakes(stream) == 0, "a normal DM fired a wake — the lever is off"
    assert "queued" in out.lower()


async def test_low_dm_to_idle_recipient_no_longer_wakes(server):
    """The Case 1 immediate wake is deliberately retired by card #59: low
    traffic rides the next natural turn or the R4 hold cap, like normal."""
    stream = await _pair(server)
    await _call(server, "send",
                {"from_agent": "alice", "to": "bob", "message": "soft ask",
                 "priority": "low"})
    assert _wakes(stream) == 0


async def test_queued_messages_still_arrive_via_drain_in_order(server):
    """R1: batching changes WHEN a wake fires, never whether or in what
    order messages arrive. The drain returns the sender's stream in ts
    order."""
    await _pair(server)
    await _call(server, "send",
                {"from_agent": "alice", "to": "bob", "message": "first"})
    await _call(server, "send",
                {"from_agent": "alice", "to": "bob", "message": "second"})
    out = await _call(server, "get_messages",
                      {"agent_name": "bob", "bind": False})
    assert "first" in out and "second" in out
    assert out.index("first") < out.index("second"), "sender stream reordered"


# ---- R2: urgent is untouched ------------------------------------------------


async def test_urgent_dm_wakes_immediately(server):
    stream = await _pair(server)
    out = await _call(server, "send",
                      {"from_agent": "alice", "to": "bob",
                       "message": "prod down", "priority": "urgent"})
    assert _wakes(stream) == 1, "urgent must wake, batching or not"
    assert "sent" in out.lower()


async def test_urgent_broadcast_still_wakes(server):
    stream = await _pair(server)
    await _call(server, "set_squads", {"name": "alice", "squads": "team"})
    await _call(server, "set_squads", {"name": "bob", "squads": "team"})
    await _call(server, "broadcast",
                {"from_agent": "alice", "message": "stop the run",
                 "priority": "urgent"})
    assert _wakes(stream) == 1


async def test_normal_broadcast_queues(server):
    stream = await _pair(server)
    await _call(server, "set_squads", {"name": "alice", "squads": "team"})
    await _call(server, "set_squads", {"name": "bob", "squads": "team"})
    await _call(server, "broadcast",
                {"from_agent": "alice", "message": "fleet note"})
    assert _wakes(stream) == 0, "normal broadcast fired a wake"
    out = await _call(server, "get_broadcasts_for_agent",
                      {"agent_name": "bob", "bind": False})
    assert "fleet note" in out, "queued broadcast lost from catch-up"


# ---- R2a: the operator never waits in the hold-queue -----------------------


async def test_operator_console_dm_wakes_immediately_at_normal(server):
    """Rule 2a (operator amendment): messages FROM operator-console or
    operator wake immediately at any priority — the operator waiting on a
    lane IS the blocking case, whatever the priority field says."""
    stream = await _pair(server)
    await _call(server, "register", {"name": "operator-console",
                                     "project": "console"})
    out = await _call(server, "send",
                      {"from_agent": "operator-console", "to": "bob",
                       "message": "how is card #59 going?"})
    assert _wakes(stream) == 1, "an operator DM sat in the hold-queue"
    assert "sent" in out.lower()
    conn = _db(server)
    reasons = [r["reason"] for r in conn.execute(
        "SELECT reason FROM wake_log WHERE agent = 'bob'")]
    assert "operator" in reasons, "operator wake not witnessed as such"


async def test_operator_low_priority_dm_also_wakes(server):
    """'Regardless of priority' includes low — the sender is the signal."""
    stream = await _pair(server)
    await _call(server, "register", {"name": "operator",
                                     "project": "console"})
    await _call(server, "send",
                {"from_agent": "operator", "to": "bob",
                 "message": "quick check", "priority": "low"})
    assert _wakes(stream) == 1


async def test_operator_broadcast_wakes_at_normal(server):
    stream = await _pair(server)
    await _call(server, "register", {"name": "operator-console",
                                     "project": "console"})
    await _call(server, "set_squads", {"name": "operator-console",
                                       "squads": "team"})
    await _call(server, "set_squads", {"name": "bob", "squads": "team"})
    await _call(server, "broadcast",
                {"from_agent": "operator-console",
                 "message": "everyone: status please"})
    assert _wakes(stream) == 1, "an operator broadcast was batched"


async def test_a_regular_sender_named_like_neither_still_batches(server):
    """The exemption is exact-match on the two operator senders — no
    prefix creep (an agent named 'operator-tools-x' must still batch)."""
    stream = await _pair(server)
    await _call(server, "register", {"name": "operator-tools-x",
                                     "project": "p"})
    await _call(server, "send",
                {"from_agent": "operator-tools-x", "to": "bob",
                 "message": "fyi"})
    assert _wakes(stream) == 0, "the operator exemption leaked by prefix"


# ---- R3: an active conversation never slows --------------------------------


async def _bob_sends_then_idles(server, body: str = "question") -> str:
    """bob sends a DM (his current turn), then his Stop hook marks idle.
    Returns the ⟨ref⟩ of bob's message for alice's reply."""
    out = await _call(server, "send",
                      {"from_agent": "bob", "to": "alice", "message": body})
    conn = _db(server)
    mid = conn.execute(
        "SELECT MAX(id) AS m FROM messages WHERE from_agent='bob'"
    ).fetchone()["m"]
    await _call(server, "get_messages",
                {"agent_name": "bob", "bind": False, "mark_idle": True})
    assert out is not None
    return f"hub.msg/1?id={mid}"


async def test_reply_to_last_turn_message_wakes_immediately(server):
    stream = await _pair(server)
    ref = await _bob_sends_then_idles(server)
    stream.sent.clear()
    out = await _call(server, "send",
                      {"from_agent": "alice", "to": "bob",
                       "message": "answer", "in_reply_to": ref})
    assert _wakes(stream) == 1, "a direct reply to bob's last turn did not wake"
    assert "sent" in out.lower()


async def test_low_priority_reply_also_wakes(server):
    """R3 says 'regardless of priority'."""
    stream = await _pair(server)
    ref = await _bob_sends_then_idles(server)
    stream.sent.clear()
    await _call(server, "send",
                {"from_agent": "alice", "to": "bob", "message": "soft answer",
                 "in_reply_to": ref, "priority": "low"})
    assert _wakes(stream) == 1


async def test_reply_to_an_older_turns_message_batches(server):
    """bob's message is two turns old: he sent it, idled, took ANOTHER
    turn (a tool call), and idled again. A reply to it is history, not an
    active conversation — it batches."""
    stream = await _pair(server)
    ref = await _bob_sends_then_idles(server)
    # bob takes one more full turn: activity + idle mark.
    await _call(server, "ping", {"from_agent": "bob"})
    await _call(server, "get_messages",
                {"agent_name": "bob", "bind": False, "mark_idle": True})
    stream.sent.clear()
    await _call(server, "send",
                {"from_agent": "alice", "to": "bob", "message": "necro-reply",
                 "in_reply_to": ref})
    assert _wakes(stream) == 0, "a reply to an old turn's message woke bob"


async def test_reply_to_someone_elses_message_does_not_wake_recipient(server):
    """in_reply_to targeting a message bob did NOT author is not bob's
    conversation — it batches."""
    stream = await _pair(server)
    # alice messages bob (queued), then replies to HER OWN message.
    await _call(server, "send",
                {"from_agent": "alice", "to": "bob", "message": "opener"})
    conn = _db(server)
    mid = conn.execute(
        "SELECT MAX(id) AS m FROM messages WHERE from_agent='alice'"
    ).fetchone()["m"]
    stream.sent.clear()
    await _call(server, "send",
                {"from_agent": "alice", "to": "bob", "message": "follow-up",
                 "in_reply_to": f"hub.msg/1?id={mid}"})
    assert _wakes(stream) == 0


async def test_reply_wake_carries_the_whole_queue_in_order(server):
    """R1 × R3: the reply-wake is drain-batched — earlier queued messages
    surface WITH the reply, in ts order, never behind it."""
    stream = await _pair(server)
    ref = await _bob_sends_then_idles(server)
    stream.sent.clear()
    await _call(server, "send",
                {"from_agent": "alice", "to": "bob", "message": "earlier fyi"})
    await _call(server, "send",
                {"from_agent": "alice", "to": "bob", "message": "the answer",
                 "in_reply_to": ref})
    assert _wakes(stream) == 1
    content = str(stream.sent[0])
    assert "earlier fyi" in content and "the answer" in content
    assert content.index("earlier fyi") < content.index("the answer")


# ---- R4: nothing rots — the hold cap ---------------------------------------


async def test_hold_sweep_wakes_an_agent_with_rotting_messages(server):
    stream = await _pair(server)
    await _call(server, "send",
                {"from_agent": "alice", "to": "bob", "message": "aging fyi"})
    assert _wakes(stream) == 0
    # Age the message past the cap.
    conn = _db(server)
    conn.execute("UPDATE messages SET ts = ts - ? WHERE body = 'aging fyi'",
                 (HOLD_MAX_SECONDS + 60,))
    conn.commit()
    fired = await server._hub_hold_sweep_pass()
    assert fired == 1
    assert _wakes(stream) == 1, "the hold cap did not fire"
    assert "aging fyi" in str(stream.sent[0])


async def test_hold_sweep_does_not_fire_twice_within_the_cap(server):
    stream = await _pair(server)
    await _call(server, "send",
                {"from_agent": "alice", "to": "bob", "message": "aging fyi"})
    conn = _db(server)
    conn.execute("UPDATE messages SET ts = ts - ?", (HOLD_MAX_SECONDS + 60,))
    conn.commit()
    await server._hub_hold_sweep_pass()
    await server._hub_hold_sweep_pass()
    assert _wakes(stream) == 1, "hold sweep re-fired within the cap window"


async def test_hold_sweep_ignores_fresh_messages(server):
    stream = await _pair(server)
    await _call(server, "send",
                {"from_agent": "alice", "to": "bob", "message": "fresh fyi"})
    fired = await server._hub_hold_sweep_pass()
    assert fired == 0
    assert _wakes(stream) == 0


async def test_hold_sweep_skips_unbound_agents(server):
    await _pair(server)
    await _call(server, "send",
                {"from_agent": "alice", "to": "bob", "message": "aging fyi"})
    conn = _db(server)
    conn.execute("UPDATE messages SET ts = ts - ?", (HOLD_MAX_SECONDS + 60,))
    conn.commit()
    server._hub_registry.unbind_name("bob")
    fired = await server._hub_hold_sweep_pass()
    assert fired == 0, "sweep tried to wake an unbound agent"


async def test_broadcast_only_hold_does_not_refire_within_the_cap(server):
    """The rate-limit stamp (last_hold_wake_at) is what stops a broadcast
    hold re-firing every sweep: unlike DMs, a broadcast has no
    per-recipient pushed stamp, so without the guard an undrained agent
    would be nagged once per pass, forever. (The mutant that removed the
    guard survived the DM tests precisely because their stamp masks it —
    this is the test that kills it.)"""
    stream = await _pair(server)
    await _call(server, "set_squads", {"name": "alice", "squads": "team"})
    await _call(server, "set_squads", {"name": "bob", "squads": "team"})
    await _call(server, "broadcast",
                {"from_agent": "alice", "message": "old fleet note"})
    conn = _db(server)
    conn.execute("UPDATE messages SET ts = ts - ?", (HOLD_MAX_SECONDS + 60,))
    conn.commit()
    assert await server._hub_hold_sweep_pass() == 1
    assert await server._hub_hold_sweep_pass() == 0, (
        "broadcast hold re-fired within the cap window"
    )
    assert _wakes(stream) == 1


async def test_hold_sweep_notes_pending_broadcasts(server):
    """A broadcast-only hold still produces a wake, and the wake names the
    queued broadcasts without rendering them (the Stop drain surfaces the
    bodies with receipts; a note line mints none)."""
    stream = await _pair(server)
    await _call(server, "set_squads", {"name": "alice", "squads": "team"})
    await _call(server, "set_squads", {"name": "bob", "squads": "team"})
    await _call(server, "broadcast",
                {"from_agent": "alice", "message": "old fleet note"})
    conn = _db(server)
    conn.execute("UPDATE messages SET ts = ts - ?", (HOLD_MAX_SECONDS + 60,))
    conn.commit()
    fired = await server._hub_hold_sweep_pass()
    assert fired == 1
    content = str(stream.sent[0])
    assert "broadcast" in content.lower()
    assert "old fleet note" not in content, (
        "broadcast body rendered in the hold wake — the drain owns bodies"
    )


async def test_the_broadcast_note_does_not_promise_a_delivery(server):
    """The note names the queued broadcasts; it must not assert that they
    WILL surface.

    The wake carries DM bodies only — broadcast bodies are the client
    Stop-hook drain's job, and nothing on this side can observe whether that
    drain ran. The old wording ("they surface at this turn's end") was an
    unconditional promise about another component's future: reported live on
    2026-09-05, the note said 3 were waiting on two consecutive turns, none
    surfaced either time, and the missing row was a WITHDRAWAL of a fleet
    rule. A reader told to expect a delivery has no reason to go looking for
    it, so the note must instead say whose job it is and carry the recovery.

    SCOPE: this pins the SENTENCE. It does not couple the wake to the drain
    and does not detect a broken promise — both remain open.
    """
    stream = await _pair(server)
    await _call(server, "set_squads", {"name": "alice", "squads": "team"})
    await _call(server, "set_squads", {"name": "bob", "squads": "team"})
    await _call(server, "broadcast",
                {"from_agent": "alice", "message": "a withdrawal"})
    conn = _db(server)
    conn.execute("UPDATE messages SET ts = ts - ?", (HOLD_MAX_SECONDS + 60,))
    conn.commit()
    assert await server._hub_hold_sweep_pass() == 1
    content = str(stream.sent[0])

    assert "they surface at this turn's end" not in content, (
        "the note promises a delivery this wake does not make and cannot "
        "observe — see the comment at the extra.append() that builds it"
    )
    assert "get_broadcasts_for_agent" in content, (
        "a reader whose drain silently did nothing needs the recovery in "
        "the sentence; without it a broken promise is unactionable"
    )
    assert "Stop-hook drain" in content, (
        "the note must name which component owns the surfacing"
    )
    assert "a withdrawal" not in content, (
        "broadcast body rendered in the hold wake — the drain owns bodies"
    )


# ---- R5: wakes are measured -------------------------------------------------


async def test_every_delivered_wake_logs_a_reason(server):
    stream = await _pair(server)
    ref = await _bob_sends_then_idles(server)
    stream.sent.clear()
    await _call(server, "send",
                {"from_agent": "alice", "to": "bob", "message": "u",
                 "priority": "urgent"})
    await _call(server, "send",
                {"from_agent": "alice", "to": "bob", "message": "r",
                 "in_reply_to": ref})
    # A third message nothing has woken bob for — the reply wake above
    # covered (and stamped) everything queued at ITS moment, so the hold
    # needs traffic that arrived after it.
    await _call(server, "send",
                {"from_agent": "alice", "to": "bob", "message": "h"})
    conn = _db(server)
    conn.execute("UPDATE messages SET ts = ts - ?", (HOLD_MAX_SECONDS + 60,))
    conn.commit()
    await server._hub_hold_sweep_pass()
    rows = conn.execute(
        "SELECT reason FROM wake_log WHERE agent = 'bob' ORDER BY id"
    ).fetchall()
    reasons = [r["reason"] for r in rows]
    assert "urgent" in reasons
    assert "reply" in reasons
    assert "hold" in reasons


# ---- R4a (card #73, operator-approved 2026-08-21): low-only holds skip -----
#
# As signed, rule 4's sweep woke an idle agent even when the ONLY thing
# waiting was low-priority chatter — pre-batching, low NEVER interrupted
# anyone, and a flapping low lane (mic-watch) could buy every idle bound
# agent ~6 backstop wakes/hour (70% of all measured wakes were the backstop,
# vps's wake_log read). 4a restores low's old promise: a queue holding only
# low waits for the agent's next natural turn; anything normal-or-above in
# the held set fires the sweep exactly as before, and the wake still
# carries the WHOLE queue (R1 — low items ride along, never reordered).


async def test_low_only_dm_hold_does_not_fire_the_sweep(server):
    stream = await _pair(server)
    await _call(server, "send",
                {"from_agent": "alice", "to": "bob",
                 "message": "quiet fyi", "priority": "low"})
    conn = _db(server)
    conn.execute("UPDATE messages SET ts = ts - ?", (HOLD_MAX_SECONDS + 60,))
    conn.commit()
    fired = await server._hub_hold_sweep_pass()
    assert fired == 0, "sweep fired for a low-only hold (rule 4a)"
    assert _wakes(stream) == 0
    # Nothing is lost: still queued unread for the next natural turn.
    row = conn.execute(
        "SELECT read FROM messages WHERE body = 'quiet fyi'").fetchone()
    assert row["read"] == 0


async def test_low_plus_normal_hold_fires_and_carries_the_whole_queue(server):
    stream = await _pair(server)
    await _call(server, "send",
                {"from_agent": "alice", "to": "bob",
                 "message": "low first", "priority": "low"})
    await _call(server, "send",
                {"from_agent": "alice", "to": "bob",
                 "message": "the real one"})
    conn = _db(server)
    conn.execute("UPDATE messages SET ts = ts - ?", (HOLD_MAX_SECONDS + 60,))
    conn.commit()
    fired = await server._hub_hold_sweep_pass()
    assert fired == 1
    content = str(stream.sent[0])
    assert "low first" in content and "the real one" in content, (
        "the wake must carry the whole queue — R1 composed with 4a"
    )
    assert content.index("low first") < content.index("the real one")


async def test_low_only_broadcast_hold_does_not_fire(server):
    stream = await _pair(server)
    await _call(server, "set_squads", {"name": "alice", "squads": "team"})
    await _call(server, "set_squads", {"name": "bob", "squads": "team"})
    await _call(server, "broadcast",
                {"from_agent": "alice", "message": "flappy lane note",
                 "priority": "low"})
    conn = _db(server)
    conn.execute("UPDATE messages SET ts = ts - ?", (HOLD_MAX_SECONDS + 60,))
    conn.commit()
    fired = await server._hub_hold_sweep_pass()
    assert fired == 0, "sweep fired for a low-only broadcast hold (rule 4a)"
    assert _wakes(stream) == 0


async def test_a_low_skip_does_not_consume_the_cap_window(server):
    """The skip must leave last_hold_wake_at untouched: a mutant that stamps
    on skip would silently delay the NEXT real wake by up to a full cap."""
    stream = await _pair(server)
    await _call(server, "send",
                {"from_agent": "alice", "to": "bob",
                 "message": "quiet fyi", "priority": "low"})
    conn = _db(server)
    conn.execute("UPDATE messages SET ts = ts - ?", (HOLD_MAX_SECONDS + 60,))
    conn.commit()
    assert await server._hub_hold_sweep_pass() == 0
    await _call(server, "send",
                {"from_agent": "alice", "to": "bob", "message": "real ask"})
    conn.execute("UPDATE messages SET ts = ts - ? WHERE body = 'real ask'",
                 (HOLD_MAX_SECONDS + 60,))
    conn.commit()
    fired = await server._hub_hold_sweep_pass()
    assert fired == 1, "the low-only skip consumed the once-per-cap window"
    assert _wakes(stream) == 1


async def test_hold_wake_records_the_max_held_priority(server):
    """The rule-5 witness gains the field the 4a debate lacked: WHAT the
    backstop fired for. held_max lands only on hold rows ('normal' or
    'urgent'); every other reason keeps ''."""
    stream = await _pair(server)
    await _call(server, "send",
                {"from_agent": "alice", "to": "bob", "message": "plain ask"})
    conn = _db(server)
    conn.execute("UPDATE messages SET ts = ts - ?", (HOLD_MAX_SECONDS + 60,))
    conn.commit()
    assert await server._hub_hold_sweep_pass() == 1
    row = conn.execute(
        "SELECT reason, held_max FROM wake_log WHERE agent = 'bob' "
        "ORDER BY id DESC LIMIT 1").fetchone()
    assert row["reason"] == "hold"
    assert row["held_max"] == "normal", row["held_max"]
    # Urgent path: an urgent that queued while bob was unbound (undelivered)
    # then aged — the sweep fires and the witness says the backstop covered
    # an URGENT, which is worth alarming on in any later ledger.
    server._hub_registry.unbind_name("bob")
    await _call(server, "send",
                {"from_agent": "alice", "to": "bob",
                 "message": "missed urgent", "priority": "urgent"})
    server._hub_registry.bind("bob", stream)
    conn.execute("UPDATE messages SET ts = ts - ?, read = 0 "
                 "WHERE body = 'missed urgent'", (HOLD_MAX_SECONDS + 60,))
    conn.execute("UPDATE agents SET last_hold_wake_at = 0 WHERE name = 'bob'")
    conn.commit()
    assert await server._hub_hold_sweep_pass() == 1
    row = conn.execute(
        "SELECT reason, held_max FROM wake_log WHERE agent = 'bob' "
        "ORDER BY id DESC LIMIT 1").fetchone()
    assert row["held_max"] == "urgent", row["held_max"]
    # Non-hold rows never carry it.
    blank = conn.execute(
        "SELECT COUNT(*) AS n FROM wake_log "
        "WHERE reason != 'hold' AND held_max != ''").fetchone()
    assert blank["n"] == 0


async def test_wake_log_migration_adds_held_max_to_an_existing_table(tmp_path):
    """Prod's wake_log predates held_max: CREATE IF NOT EXISTS is a no-op
    there, so the ALTER is the only thing standing between the deploy and
    every _log_wake insert failing silently (fail-soft would eat it — the
    witness would just go dark). Build the OLD shape first, then boot."""
    import sqlite3

    db = tmp_path / "test.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """CREATE TABLE wake_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            agent TEXT NOT NULL,
            reason TEXT NOT NULL
        )"""
    )
    import time as _t

    # A recent ts — the sweep's retention prune must not eat the witness row.
    conn.execute("INSERT INTO wake_log (ts, agent, reason) "
                 "VALUES (?, 'old', 'hold')", (_t.time(),))
    conn.commit()
    conn.close()

    server = create_server(db_path=db)
    stream = await _pair(server)
    await _call(server, "send",
                {"from_agent": "alice", "to": "bob", "message": "aging"})
    conn = _db(server)
    conn.execute("UPDATE messages SET ts = ts - ?", (HOLD_MAX_SECONDS + 60,))
    conn.commit()
    assert await server._hub_hold_sweep_pass() == 1
    rows = conn.execute(
        "SELECT agent, held_max FROM wake_log ORDER BY id").fetchall()
    assert rows[0]["agent"] == "old" and rows[0]["held_max"] == "", (
        "migration must default existing rows to ''"
    )
    assert rows[-1]["agent"] == "bob" and rows[-1]["held_max"] == "normal"
    assert _wakes(stream) == 1
