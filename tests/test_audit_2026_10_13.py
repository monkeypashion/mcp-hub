"""The 2026-10-13 hub-core audit: the findings fixed without per-agent
credentials, each pinned by the input that broke it.

  C2  a session bound to one name drained ANOTHER agent's inbox and marked
      it read; the owner's next drain found nothing.
  H1  a blocked_by clear compared a TYPED name, so an unbound caller (or
      anyone typing `operator` with the token unset) cleared any edge.
  M5  an urgent whose live push failed lost its focus-piercing on the hold
      sweep's backstop wake.
  H3  the post receipt promised delivery "within 10 min" that nothing does.
  L3  broadcast checked in_reply_to before identity, so a refused caller
      could probe which message ids exist.
  L6  a racing first use of the identity key could read the file EMPTY.

What still waits for credentials (C1, M1, M3, M4) is unbound-caller trust,
and the unbound path stays open here on purpose: the Stop hook drains as an
unbound caller, and gating it would lose every lane's catch-up.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from mcp_hub import identity
from mcp_hub.server import HOLD_MAX_SECONDS, create_server


@pytest.fixture
def server(tmp_path: Path):
    return create_server(db_path=tmp_path / "hub.db")


class _Sess:
    """A bound session that accepts every notification."""

    _write_stream = object()

    def __init__(self) -> None:
        self.sent: list[object] = []

    async def send_ping(self):
        return None

    async def send_notification(self, notification):
        self.sent.append(notification)


class _Ctx:
    def __init__(self, session):
        self.session = session


def _text(result) -> str:
    if isinstance(result, str):
        return result
    for block in getattr(result, "content", None) or result:
        if hasattr(block, "text"):
            return block.text
    return str(result)


async def _call(server, name, args, ctx=None) -> str:
    return _text(await server._tool_manager.call_tool(name, args, context=ctx))


async def _bound(server, name):
    await _call(server, "register", {"name": name, "project": "p"})
    sess = _Sess()
    server._hub_registry.bind(name, sess)
    return _Ctx(sess), sess


def _db(server):
    conn = sqlite3.connect(server._hub_db_path)
    conn.row_factory = sqlite3.Row
    return conn


# ---- C2: a drain by the wrong session is refused and marks nothing ---------


async def test_a_session_bound_to_another_name_cannot_drain_an_inbox(server):
    await _call(server, "register", {"name": "alice", "project": "p"})
    await _call(server, "register", {"name": "bob", "project": "p"})
    carol, _ = await _bound(server, "carol")
    await _call(server, "send",
                {"from_agent": "bob", "to": "alice", "message": "secret plan"})

    out = await _call(server, "get_messages",
                      {"agent_name": "alice", "bind": False}, carol)
    assert out.startswith("REFUSED"), out
    assert "secret plan" not in out
    unread = _db(server).execute(
        "SELECT COUNT(*) FROM messages WHERE to_agent='alice' AND read=0"
    ).fetchone()[0]
    assert unread == 1, "a refused drain must not mark anything read"

    # The owner's own drain still gets it.
    mine = await _call(server, "get_messages",
                       {"agent_name": "alice", "bind": False})
    assert "secret plan" in mine


async def test_a_session_bound_to_another_name_cannot_advance_a_broadcast_cursor(server):
    await _call(server, "register", {"name": "alice", "project": "p", "squads": "sq"})
    bob, _ = await _bound(server, "bob")
    await _call(server, "set_squads", {"name": "bob", "squads": "sq"}, bob)
    await _call(server, "broadcast",
                {"from_agent": "bob", "message": "squad news"}, bob)
    before = _db(server).execute(
        "SELECT last_broadcast_seen_id FROM agents WHERE name='alice'"
    ).fetchone()[0]

    out = await _call(server, "get_broadcasts_for_agent",
                      {"agent_name": "alice", "bind": False}, bob)
    assert out.startswith("REFUSED"), out
    after = _db(server).execute(
        "SELECT last_broadcast_seen_id FROM agents WHERE name='alice'"
    ).fetchone()[0]
    assert after == before, "a refused drain must not move the cursor"


async def test_an_unbound_drain_still_works_because_the_stop_hook_is_one(server):
    await _call(server, "register", {"name": "alice", "project": "p"})
    await _call(server, "send",
                {"from_agent": "bob", "to": "alice", "message": "hello"})
    # No context (the hook's ephemeral client), and a session bound to nobody.
    out = await _call(server, "get_messages",
                      {"agent_name": "alice", "bind": False}, _Ctx(_Sess()))
    assert "hello" in out


async def test_a_session_drains_its_own_inbox(server):
    alice, _ = await _bound(server, "alice")
    await _call(server, "send",
                {"from_agent": "bob", "to": "alice", "message": "for you"})
    out = await _call(server, "get_messages",
                      {"agent_name": "alice", "bind": False}, alice)
    assert "for you" in out


# ---- H1/H2: declaring or clearing an edge needs a verified name ------------


async def _alice_declares(server):
    alice, _ = await _bound(server, "alice")
    await _call(server, "register", {"name": "bob", "project": "p"})
    await _call(server, "send",
                {"from_agent": "alice", "to": "bob", "message": "my work"}, alice)
    mid = _db(server).execute(
        "SELECT id FROM messages WHERE body='my work'").fetchone()[0]
    edge = f"hub.msg/1?id={mid}|hub.agent/1?name=bob"
    out = await _call(server, "send",
                      {"from_agent": "alice", "to": "bob", "message": "blocked",
                       "blocked_by": edge}, alice)
    assert "refused" not in out.lower(), out
    return alice, edge


def _open_edges(server) -> int:
    return _db(server).execute(
        "SELECT COUNT(*) FROM lineage_edges "
        "WHERE predicate LIKE '%block%' AND cleared_at IS NULL"
    ).fetchone()[0]


async def test_an_unbound_caller_typing_the_declarers_name_cannot_clear(server):
    _, edge = await _alice_declares(server)
    out = await _call(server, "send",
                      {"from_agent": "alice", "to": "bob", "message": "x",
                       "blocked_by": f"clear:{edge}"})
    assert "blocked_by refused" in out, out
    assert _open_edges(server) == 1


async def test_typing_operator_with_the_token_unset_cannot_clear(server, monkeypatch):
    monkeypatch.delenv("MCP_HUB_OPERATOR_TOKEN", raising=False)
    _, edge = await _alice_declares(server)
    out = await _call(server, "send",
                      {"from_agent": "operator", "to": "bob", "message": "x",
                       "blocked_by": f"clear:{edge}"})
    assert "blocked_by refused" in out, out
    assert _open_edges(server) == 1


async def test_an_unbound_caller_cannot_declare(server):
    await _call(server, "register", {"name": "carol", "project": "p"})
    out = await _call(server, "send",
                      {"from_agent": "carol", "to": "bob", "message": "x",
                       "blocked_by": "ra.feature/1?feature_set_key=alice-svc&id=f1"
                                     "|hub.agent/1?name=bob"})
    assert "blocked_by refused" in out, out
    assert _open_edges(server) == 0


async def test_the_bound_declarer_still_clears_its_own_edge(server):
    alice, edge = await _alice_declares(server)
    out = await _call(server, "send",
                      {"from_agent": "alice", "to": "bob", "message": "x",
                       "blocked_by": f"clear:{edge}"}, alice)
    assert "refused" not in out.lower(), out
    assert _open_edges(server) == 0


# ---- M5: a held urgent pierces focus on its backstop wake ------------------


async def test_a_held_urgent_wakes_through_focus(server):
    await _call(server, "register", {"name": "alice", "project": "p"})
    await _call(server, "register", {"name": "bob", "project": "p"})
    # bob is unbound when the urgent lands, so its live push cannot deliver.
    await _call(server, "send", {"from_agent": "alice", "to": "bob",
                                 "message": "prod is down", "priority": "urgent"})
    sess = _Sess()
    server._hub_registry.bind("bob", sess)
    await _call(server, "focus", {"agent_name": "bob", "minutes": 60}, _Ctx(sess))
    conn = _db(server)
    conn.execute("UPDATE messages SET ts = ts - ? WHERE body = 'prod is down'",
                 (HOLD_MAX_SECONDS + 60,))
    conn.commit()
    sess.sent.clear()
    await server._hub_hold_sweep_pass()
    assert len(sess.sent) == 1, "focus swallowed a held urgent"
    assert "prod is down" in str(sess.sent[0])


# ---- H3: the post receipt says what happens -------------------------------


async def test_a_normal_post_receipt_does_not_promise_delivery(server):
    alice, _ = await _bound(server, "alice")
    await _call(server, "create_channel",
                {"name": "topic", "created_by": "alice", "description": "d"}, alice)
    out = await _call(server, "post", {"from_agent": "alice", "channel": "topic",
                                       "message": "fyi"}, alice)
    assert out.startswith("Posted to #topic (priority=normal;"), out
    assert "within" not in out
    assert "get_channel_messages" in out

    sub = await _call(server, "subscribe_channel",
                      {"name": "alice", "channel": "topic", "subscribed": True}, alice)
    assert "normal/urgent posts will wake" not in sub
    assert "urgent posts will wake you" in sub


# ---- L3: identity is checked before the reply ref --------------------------


async def test_a_refused_broadcaster_learns_nothing_about_message_ids(server):
    carol, _ = await _bound(server, "carol")
    out = await _call(server, "broadcast",
                      {"from_agent": "alice", "message": "x",
                       "in_reply_to": "hub.msg/1?id=999999"}, carol)
    assert "identity mismatch" in out, out


# ---- L6: a racing first use never reads the key file empty ----------------


def test_racing_first_use_agrees_on_one_complete_key(tmp_path):
    db = tmp_path / "hub.db"
    results, errors = [], []

    def grab():
        try:
            results.append(identity.public_pem(db))
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=grab) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    assert len(set(results)) == 1
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []
    assert identity.key_path(db).stat().st_mode & 0o777 == 0o600



# ---- the instructions say the same as the receipts --------------------------


def test_the_instructions_say_channel_posts_are_not_pushed(server):
    """H3's second half: the first text every agent reads must not teach the
    DM batching rules as if they covered channel posts."""
    blob = server.instructions or ""
    assert "post() is NOT pushed" in blob, blob
    assert "get_channel_messages" in blob


def test_the_instructions_carry_no_history_labels(server):
    """Card numbers, rule ids and seat names are history; the standing rule
    is what an agent needs at connect time."""
    blob = server.instructions or ""
    for label in ("card #", "rule 4a", "operator-signed", "operator-console",
                  "console"):
        assert label not in blob, label
    assert "guardrail gap" in blob
