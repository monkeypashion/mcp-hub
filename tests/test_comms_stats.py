"""comms_stats returns FIGURES about traffic, never the traffic itself.

Track 3 of the 2026-06-14 comms-weight plan: the per-agent breakdown that
took a prod ssh + hand SQL becomes a read-only tool any seat can call. The
load-bearing property is negative — no message BODY may appear in the
output, or the stats tool joins the context tax it exists to measure.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mcp_hub.server import create_server

pytestmark = pytest.mark.asyncio

# A body distinctive enough that a leak cannot hide in formatting.
MARKER = "SECRET-BODY-a2f9x"


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "test.db"


@pytest.fixture
def server(db_path: Path):
    return create_server(db_path=db_path)


async def _call_tool(server, name: str, args: dict) -> str:
    result = await server._tool_manager.call_tool(name, args)
    if hasattr(result, "content"):
        for block in result.content:
            if hasattr(block, "text"):
                return block.text
    return str(result)


async def _seed(server) -> None:
    await _call_tool(server, "register", {"name": "alice"})
    await _call_tool(server, "register", {"name": "bob"})
    await _call_tool(server, "send", {
        "from_agent": "alice", "to": "bob",
        "message": MARKER + " dm", "priority": "low",
    })
    await _call_tool(server, "send", {
        "from_agent": "bob", "to": "alice",
        "message": MARKER + " reply", "priority": "urgent",
    })
    await _call_tool(server, "create_channel", {
        "name": "topic", "created_by": "alice", "description": "t",
    })
    await _call_tool(server, "post", {
        "from_agent": "alice", "channel": "topic",
        "message": MARKER + " post",
    })


async def test_counts_by_type_priority_and_sender(server):
    await _seed(server)
    out = await _call_tool(server, "comms_stats", {})
    assert "3 messages" in out
    assert "dm 2" in out
    assert "channel post 1" in out
    # priorities: one low, one urgent, one normal (the post's default)
    assert "low 1" in out and "urgent 1" in out and "normal 1" in out
    # senders with per-sender counts
    assert "alice: 2 msgs" in out
    assert "bob: 1 msgs" in out


async def test_no_message_body_ever_leaks(server):
    await _seed(server)
    out = await _call_tool(server, "comms_stats", {})
    assert MARKER not in out


async def test_window_excludes_older_traffic(server, db_path):
    """The window is time-based: a row aged past it must leave the figures.

    Ages one DM by editing its timestamp directly — the seed writes
    everything 'now', so without surgery the window can never be seen
    excluding anything and the days parameter would be untestable.
    """
    import sqlite3

    await _seed(server)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE messages SET ts = ts - 10 * 86400 WHERE from_agent = 'bob'"
    )
    conn.commit()
    conn.close()
    out = await _call_tool(server, "comms_stats", {"days": 7})
    assert "2 messages" in out
    assert "bob" not in out


async def test_days_is_clamped_not_trusted(server):
    await _seed(server)
    out = await _call_tool(server, "comms_stats", {"days": 100000})
    assert "Last 90d" in out
    out = await _call_tool(server, "comms_stats", {"days": 0})
    assert "Last 1d" in out
