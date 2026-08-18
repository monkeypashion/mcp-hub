"""Card #56 — the shared delivery record, tested against its contract.

The two delivery paths (live channel push, Stop-hook drain) used to share no
record of what actually rendered, so the drain guessed from a binding
generation — an inference shadow-surface.jsonl measured wrong in BOTH
directions (113 double surfaces, 76 false compactions when this shipped).
The fix: the Stop hook reports the message ids its own transcript PROVES
rendered (`rendered_refs`), the hub records them per (message, agent) in
`delivery_receipts`, and the compact drain keys on that record.

The contract these tests pin:
  P1 exactly-once (healthy): a receipted message drains as ONE line, never a
     second full body.
  P2 never lost (failure): no receipt → full reprint, even in states the old
     inference would have truncated (that was the false-compaction class).
     The "" sentinel keeps old clients on the legacy inference — degraded
     never means dropped.
  P3 bounded drain: receipted rows cost one line and no budget; an unproven
     urgent prints in full past every cap.
  P4 no regression: a session with no receipts (relaunch, deaf stream)
     replays everything in full; and the receipt-mode broadcast drain stops
     the pending-jump absorbing queue-only rows sitting below a pushed one.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from mcp_hub import receipts
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
    if isinstance(result, list):
        for block in result:
            if hasattr(block, "text"):
                return block.text
    return result if isinstance(result, str) else str(result)


class _FakeSess:
    """Stand-in for a bound ServerSession — identity is all the registry uses."""


class _Stream:
    """A bound session whose send_notification succeeds like a dead stream's
    does — render evidence is expressed separately, by receipts."""

    _write_stream = object()

    def __init__(self) -> None:
        self.sent: list[object] = []

    async def send_ping(self):
        return None

    async def send_notification(self, notification):
        self.sent.append(notification)


BODY = "line one of the body\nline two which should not appear when summarised"


def _last_msg_id(tmp_path: Path) -> int:
    import sqlite3

    conn = sqlite3.connect(tmp_path / "test.db")
    try:
        return conn.execute("SELECT MAX(id) FROM messages").fetchone()[0]
    finally:
        conn.close()


async def _setup_dm(server):
    registry = server._hub_registry  # type: ignore[attr-defined]
    await _call(server, "register", {"name": "bob", "project": "p"})
    await _call(server, "register", {"name": "alice", "project": "p"})
    registry.bind("bob", _FakeSess())
    return registry


async def _send(server, body: str = BODY, priority: str = "normal"):
    with patch.object(
        server._hub_registry,  # type: ignore[attr-defined]
        "push",
        AsyncMock(return_value=True),
    ):
        return await _call(
            server,
            "send",
            {"from_agent": "alice", "to": "bob", "message": body,
             "priority": priority},
        )


async def _drain_dms(server, rendered_refs: str) -> str:
    return await _call(
        server, "get_messages",
        {"agent_name": "bob", "bind": False, "compact": True,
         "rendered_refs": rendered_refs},
    )


# ---- receipts.py: what counts as render evidence ---------------------------


def _transcript(tmp_path: Path, records: list[dict]) -> str:
    p = tmp_path / "transcript.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return str(p)


def _user_record(text: str) -> dict:
    return {"type": "user", "message": {"content": text}}


def test_live_tag_heads_mint_receipts(tmp_path):
    path = _transcript(tmp_path, [
        _user_record('<channel source="hub">DM from alice '
                     "⟨hub.msg/1?id=7⟩: hello</channel>"),
        _user_record('<channel source="hub">BROADCAST from ops '
                     "⟨hub.msg/1?id=9⟩: fleet note</channel>"),
        _user_record('<channel source="hub">#general post from ops '
                     "⟨hub.msg/1?id=11⟩: topical</channel>"),
    ])
    assert receipts.rendered_message_ids(path) == [7, 9, 11]


def test_queue_plumbing_does_not_mint_receipts(tmp_path):
    """The founding distinction (shadow.py): the enqueue/remove pair carries
    the identical tag and proves only that the CLIENT got the notification.
    A receipt minted from one would truncate a message the agent never saw —
    the exact class this design exists to kill."""
    tag = 'DM from alice ⟨hub.msg/1?id=7⟩: hello'
    path = _transcript(tmp_path, [
        {"type": "queue-operation", "operation": "enqueue",
         "content": f"<channel>{tag}</channel>"},
        {"type": "queue-operation", "operation": "remove",
         "content": f"<channel>{tag}</channel>"},
    ])
    assert receipts.rendered_message_ids(path) == []


def test_a_mid_turn_attachment_is_a_render(tmp_path):
    """The mid-turn delivery path (verified against a live transcript,
    msg 13239): the content enters context as a queued_command attachment in
    the conversation chain, and no user record ever follows. Missing this
    class would double-print every message that arrives while the recipient
    is working — the dominant case on a busy lane."""
    path = _transcript(tmp_path, [
        {"type": "attachment", "isSidechain": False,
         "attachment": {"type": "queued_command",
                        "prompt": '<channel source="hub">\nDM from alice '
                                  "⟨hub.msg/1?id=8⟩: mid-turn</channel>"}},
    ])
    assert receipts.rendered_message_ids(path) == [8]


def test_a_sidechain_render_is_not_the_agents_context(tmp_path):
    path = _transcript(tmp_path, [
        {"type": "user", "isSidechain": True, "message": {"content":
            '<channel>DM from alice ⟨hub.msg/1?id=7⟩: hello</channel>'}},
        {"type": "attachment", "isSidechain": True,
         "attachment": {"type": "queued_command",
                        "prompt": '<channel>DM from alice '
                                  "⟨hub.msg/1?id=8⟩: hi</channel>"}},
    ])
    assert receipts.rendered_message_ids(path) == []


def test_a_ref_quoted_in_prose_does_not_mint_a_receipt(tmp_path):
    """Agents cite refs constantly ("re your ⟨hub.msg/1?id=99⟩ …"). A citation
    is not a render of the cited message."""
    path = _transcript(tmp_path, [
        _user_record('<channel source="hub">DM from alice ⟨hub.msg/1?id=7⟩: '
                     "replying to your ⟨hub.msg/1?id=99⟩ from earlier"
                     "</channel>"),
    ])
    ids = receipts.rendered_message_ids(path)
    assert 7 in ids
    assert 99 not in ids, "a QUOTED ref was counted as a render"


def test_drain_batch_lines_mint_receipts(tmp_path):
    """A drain-batched wake surfaces several messages as get_messages-style
    lines inside one channel event; each is a genuine render."""
    path = _transcript(tmp_path, [
        _user_record('<channel source="hub">'
                     "[12:00:00] **alice** ⟨hub.msg/1?id=5⟩ [low]: one\n"
                     "[12:00:01] **carol** ⟨hub.msg/1?id=6⟩: two</channel>"),
    ])
    assert receipts.rendered_message_ids(path) == [5, 6]


def test_encode_report_never_returns_the_old_client_sentinel():
    assert receipts.encode_report([]) == "none"
    assert receipts.encode_report([3, 1]) == "3,1"


# ---- P1: exactly-once on the healthy path ----------------------------------


async def test_receipted_dm_drains_as_one_line(server, tmp_path):
    await _setup_dm(server)
    await _send(server)
    mid = _last_msg_id(tmp_path)

    out = await _drain_dms(server, rendered_refs=str(mid))

    assert "already delivered live" in out
    assert "line two" not in out, "full body reprinted despite a receipt"
    assert "alice" in out and f"id={mid}" in out, (
        "the one line must carry sender and ref"
    )
    # …and it is marked read: nothing on a second pull.
    again = await _drain_dms(server, rendered_refs="none")
    assert again == ""


async def test_a_receipt_recorded_earlier_still_counts(server, tmp_path):
    """Receipts persist per (message, agent): a report that landed on one
    call (here the broadcast leg) compacts the DM drain even when THIS call
    reports nothing new — the record is shared, not per-request."""
    await _setup_dm(server)
    await _send(server)
    mid = _last_msg_id(tmp_path)
    # The Stop hook reports the same list to both drain tools; model the
    # broadcast leg landing first.
    await _call(server, "get_broadcasts_for_agent",
                {"agent_name": "bob", "bind": False, "compact": True,
                 "rendered_refs": str(mid)})

    out = await _drain_dms(server, rendered_refs="none")

    assert "already delivered live" in out
    assert "line two" not in out


# ---- P2: never lost — no receipt means full reprint ------------------------


async def test_no_receipt_reprints_full_even_where_the_old_inference_lied(
    server,
):
    """THE false-compaction killer (76 on record in shadow-surface.jsonl).

    Generation matches, wake was acked — every condition the legacy
    inference read as "already delivered live" — but the transcript minted
    no receipt. The record outranks the inference: full reprint."""
    registry = await _setup_dm(server)
    await _send(server)
    registry.wake_ack("bob")  # the state the old code truncated on

    out = await _drain_dms(server, rendered_refs="none")

    assert "already delivered live" not in out, (
        "compacted on inference in receipt mode — the false-compaction "
        "class is back"
    )
    assert "line two" in out


async def test_old_client_sentinel_keeps_the_legacy_inference(server):
    """`rendered_refs=""` (parameter absent) is an old client. It must get
    the pre-receipt behaviour byte-for-byte — compaction on the generation
    inference — so the migration has no flag day. This pin is what makes
    deleting the legacy branch a deliberate act later, not an accident."""
    registry = await _setup_dm(server)
    await _send(server, priority="urgent")  # urgent still pushes (card #59)
    registry.wake_ack("bob")

    out = await _call(
        server, "get_messages",
        {"agent_name": "bob", "bind": False, "compact": True},
    )

    assert "already delivered live" in out, (
        "legacy clients lost their compaction — every old lane double-prints"
    )


# ---- P3: the drain is bounded ----------------------------------------------


async def test_unproven_urgent_prints_in_full_past_every_cap(server):
    """Two unproven messages exhaust the full-text budget; the urgent third
    must still print whole. Urgency is the wrong place to economise on a
    failure path."""
    await _setup_dm(server)
    await _send(server, body="filler one\nfiller one tail")
    await _send(server, body="filler two\nfiller two tail")
    await _send(server, body=BODY, priority="urgent")

    out = await _drain_dms(server, rendered_refs="none")

    assert "line two" in out, "urgent was summarised past the bulk cap"
    assert "[urgent]" in out


async def test_receipted_rows_do_not_consume_the_full_text_budget(
    server, tmp_path,
):
    """A one-liner costs no budget: with one receipted DM ahead of them, two
    unproven DMs must both still land inside COMPACT_FULL_MESSAGES."""
    await _setup_dm(server)
    await _send(server, body="seen already\nseen tail")
    seen_id = _last_msg_id(tmp_path)
    await _send(server, body="fresh one\nfresh one tail")
    await _send(server, body="fresh two\nfresh two tail")

    out = await _drain_dms(server, rendered_refs=str(seen_id))

    assert "seen tail" not in out, "receipted row printed in full"
    assert "fresh one tail" in out and "fresh two tail" in out, (
        "a one-line receipt consumed the full-text budget"
    )


# ---- P4: no regression — replay, and the broadcast leg ---------------------


async def _bc_pair(server) -> _Stream:
    await _call(server, "register",
                {"name": "sender", "project": "org/a", "squads": "team"})
    await _call(server, "register",
                {"name": "listener", "project": "org/b", "squads": "team"})
    stream = _Stream()
    server._hub_registry.bind("listener", stream)
    return stream


async def test_relaunched_session_replays_everything_in_full(server):
    """A relaunch (or deaf stream) has no receipts to report, whatever the
    server-side push bookkeeping says — the 2026-08-16 outage shape. Every
    row replays whole."""
    registry = await _setup_dm(server)
    await _send(server)
    registry.wake_ack("bob")  # server-side state says all healthy

    out = await _drain_dms(server, rendered_refs="none")

    assert "line two" in out


async def test_receipted_broadcast_drains_as_one_line_and_advances_cursor(
    server, tmp_path,
):
    stream = await _bc_pair(server)
    await _call(server, "broadcast",
                {"from_agent": "sender", "message": "seen live\nseen tail",
                 "priority": "urgent"})
    assert stream.sent, "the fixture never pushed — this test proves nothing"
    mid = _last_msg_id(tmp_path)

    out = await _call(server, "get_broadcasts_for_agent",
                      {"agent_name": "listener", "bind": False,
                       "compact": True, "rendered_refs": str(mid)})

    assert "already delivered live" in out
    assert "seen tail" not in out
    assert "1 already surfaced live" in out
    # Cursor advanced: a repeat drain is empty.
    again = await _call(server, "get_broadcasts_for_agent",
                        {"agent_name": "listener", "bind": False,
                         "compact": True, "rendered_refs": "none"})
    assert again == ""


async def test_legacy_pending_jump_absorbs_queue_only_rows_below_it(
    server, tmp_path,
):
    """The latent defect in the legacy jump, pinned so the receipt-mode fix
    below is legible: a low-priority broadcast never pushes, so when a LATER
    pushed broadcast is promoted by id-range, the low row beneath it is
    absorbed unseen. Receipt mode retires exactly this (next test). If this
    pin ever fails, the legacy path changed — check the receipt path too."""
    await _bc_pair(server)
    await _call(server, "broadcast",
                {"from_agent": "sender", "message": "quiet low note",
                 "priority": "low"})
    await _call(server, "broadcast",
                {"from_agent": "sender", "message": "loud normal one",
                 "priority": "urgent"})
    server._hub_registry.wake_ack("listener")

    out = await _call(server, "get_broadcasts_for_agent",
                      {"agent_name": "listener", "bind": False,
                       "compact": True})

    assert "quiet low note" not in out, (
        "legacy behaviour changed: the jump no longer absorbs — update the "
        "receipt-mode test and this pin together"
    )


async def test_receipt_mode_surfaces_the_queue_only_row_the_jump_ate(
    server, tmp_path,
):
    """Same setup as the pin above, but the client reports receipts: the
    pushed row drains as one line and the low row it used to bury arrives
    in full."""
    await _bc_pair(server)
    await _call(server, "broadcast",
                {"from_agent": "sender", "message": "quiet low note",
                 "priority": "low"})
    await _call(server, "broadcast",
                {"from_agent": "sender", "message": "loud normal one",
                 "priority": "urgent"})
    pushed_id = _last_msg_id(tmp_path)
    server._hub_registry.wake_ack("listener")

    out = await _call(server, "get_broadcasts_for_agent",
                      {"agent_name": "listener", "bind": False,
                       "compact": True, "rendered_refs": str(pushed_id)})

    assert "quiet low note" in out, "the low row is still being absorbed"
    assert "(already delivered live — loud normal one" in out
