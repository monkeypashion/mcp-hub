"""Gate for per-channel subscriptions — channel wakes become opt-in.

Operator-approved 2026-07-29 after spike-runtime's live specimen: a
#deletions post woke a runtime-only clone, because post()'s fan-out was
`registry.names()` minus sender — squad walls never applied to channels.
Subscriptions close that (gap #16) and carry the per-(agent,channel) cursor
column that closes #13's storage half in the same schema.

Rules under test:
- normal/urgent posts wake ONLY subscribed agents (low stays no-wake).
- create_channel subscribes the creator; post() subscribes the poster —
  engagement opts you in; silence never does.
- unsubscribe (subscribe_channel subscribed=False) stops wakes; resubscribe
  restores them. Reading stays open to everyone — delivery, not
  confidentiality.
- Migration seed: a hub upgrading with EXISTING agents and channels
  subscribes everyone to everything — current behavior preserved exactly;
  nobody is silently silenced by the upgrade itself.
- The receipt names its denominator (backlog #14): subscribers, not
  "connected agents".
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mcp_hub.server import create_server, init_db


@pytest.fixture
def server(tmp_path: Path):
    return create_server(db_path=tmp_path / "subs.db")


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
    def __init__(self) -> None:
        self.sends: list = []

    async def send_ping(self): ...

    async def send_notification(self, n):
        self.sends.append(n)


async def _agent(server, name: str) -> _FakeSess:
    await _call(server, "register", {"name": name})
    sess = _FakeSess()
    server._hub_registry.bind(name, sess)
    return sess


@pytest.mark.asyncio
async def test_normal_post_wakes_only_subscribers(server):
    poster = await _agent(server, "poster")
    listener = await _agent(server, "listener")
    bystander = await _agent(server, "bystander")
    await _call(server, "create_channel", {"name": "topic", "created_by": "poster"})
    await _call(
        server,
        "subscribe_channel",
        {"name": "listener", "channel": "topic"},
    )
    await _call(
        server,
        "post",
        {"from_agent": "poster", "channel": "topic", "message": "hello",
         "priority": "urgent"},
    )
    assert listener.sends, "subscriber must be woken"
    assert not bystander.sends, "non-subscriber must NOT be woken — the #16 fix"
    assert not poster.sends


@pytest.mark.asyncio
async def test_receipt_names_subscribers_not_agents(server):
    await _agent(server, "poster")
    await _agent(server, "listener")
    await _call(server, "create_channel", {"name": "topic", "created_by": "poster"})
    await _call(server, "subscribe_channel", {"name": "listener", "channel": "topic"})
    out = await _call(
        server,
        "post",
        {"from_agent": "poster", "channel": "topic", "message": "hello",
         "priority": "urgent"},
    )
    # A receipt that reads the same in a broken world is a rendering, not
    # evidence (spike-runtime, backlog #14): name the population.
    assert "subscriber" in out


@pytest.mark.asyncio
async def test_creator_and_poster_are_auto_subscribed(server):
    await _agent(server, "maker")
    await _agent(server, "chime")
    await _call(server, "create_channel", {"name": "topic", "created_by": "maker"})
    await _call(
        server, "post", {"from_agent": "chime", "channel": "topic", "message": "in",
         "priority": "urgent"}
    )
    await _agent(server, "maker2")  # third seat, only ever the sender
    out = await _call(
        server,
        "post",
        {"from_agent": "maker2", "channel": "topic", "message": "who hears?",
         "priority": "urgent"},
    )
    # maker (creator) and chime (past poster) are both subscribed.
    assert "2/2" in out or "2 subscriber" in out or "woke 2" in out


@pytest.mark.asyncio
async def test_unsubscribe_stops_wakes_and_resubscribe_restores(server):
    await _agent(server, "poster")
    listener = await _agent(server, "listener")
    await _call(server, "create_channel", {"name": "topic", "created_by": "poster"})
    await _call(server, "subscribe_channel", {"name": "listener", "channel": "topic"})
    await _call(
        server,
        "subscribe_channel",
        {"name": "listener", "channel": "topic", "subscribed": False},
    )
    await _call(
        server, "post", {"from_agent": "poster", "channel": "topic", "message": "a",
         "priority": "urgent"}
    )
    assert not listener.sends
    await _call(server, "subscribe_channel", {"name": "listener", "channel": "topic"})
    await _call(
        server, "post", {"from_agent": "poster", "channel": "topic", "message": "b",
         "priority": "urgent"}
    )
    assert listener.sends


@pytest.mark.asyncio
async def test_low_posts_still_never_wake(server):
    await _agent(server, "poster")
    listener = await _agent(server, "listener")
    await _call(server, "create_channel", {"name": "topic", "created_by": "poster"})
    await _call(server, "subscribe_channel", {"name": "listener", "channel": "topic"})
    await _call(
        server,
        "post",
        {"from_agent": "poster", "channel": "topic", "message": "fyi",
         "priority": "low"},
    )
    assert not listener.sends


def test_migration_seeds_existing_fleet_fully_subscribed(tmp_path: Path):
    """A hub upgrading in place: agents and channels already exist, the
    subscriptions table doesn't. The seed must subscribe everyone to
    everything — the upgrade itself silences nobody."""
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE agents (name TEXT PRIMARY KEY, project TEXT NOT NULL
            DEFAULT '', bio TEXT NOT NULL DEFAULT '', status TEXT NOT NULL
            DEFAULT 'online', registered REAL NOT NULL, last_seen REAL NOT
            NULL, meta TEXT NOT NULL DEFAULT '{}');
        CREATE TABLE channels (name TEXT PRIMARY KEY, created_by TEXT NOT
            NULL, created_at REAL NOT NULL, description TEXT NOT NULL
            DEFAULT '');
        INSERT INTO agents (name, registered, last_seen) VALUES
            ('a1', 1, 1), ('a2', 1, 1);
        INSERT INTO channels (name, created_by, created_at) VALUES
            ('c1', 'a1', 1), ('c2', 'a2', 1);
        """
    )
    conn.commit()
    conn.close()

    init_db(db)

    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT agent, channel, subscribed FROM channel_subscriptions ORDER BY 1, 2"
    ).fetchall()
    assert len(rows) == 4  # 2 agents × 2 channels
    assert all(r[2] == 1 for r in rows)
    conn.close()


@pytest.mark.asyncio
async def test_list_channels_shows_own_subscription_state(server):
    """The newborn-seat gap (dev's review): a seat subscribed to nothing must
    be able to SEE that — silent nondelivery is invisible unless shown."""
    await _agent(server, "newborn")
    await _agent(server, "maker")
    await _call(server, "create_channel", {"name": "topic", "created_by": "maker"})
    out = await _call(server, "list_channels", {"agent": "newborn"})
    assert "not subscribed" in out
    assert "subscribed to NO channels" in out
    out_maker = await _call(server, "list_channels", {"agent": "maker"})
    assert "✔ subscribed" in out_maker
    # No agent argument: the plain listing, unchanged shape.
    plain = await _call(server, "list_channels", {})
    assert "subscribed" not in plain
