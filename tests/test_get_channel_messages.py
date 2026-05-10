"""Tests for get_channel_messages cursor + structured-fetch additions.

Two new parameters:
- since_id: id-based cursor (overrides since_minutes when > 0)
- format: "text" (default, render markdown) | "json" (structured records)

Both are required for the factory backlog adapter (DT) — time-based windows
break the lossless contract on retries (overlap = duplicates, drift = missed
items), and regex-parsing the markdown render is brittle.

Backward compat: existing callers (text default, since_id=0) continue to
get exactly the previous behavior.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from mcp_hub.server import create_server


@pytest.fixture
def server(tmp_path: Path):
    s = create_server(db_path=tmp_path / "test.db")
    return s


async def _call_tool(server, name: str, args: dict) -> str:
    result = await server._tool_manager.call_tool(name, args)
    if hasattr(result, "content"):
        for block in result.content:
            if hasattr(block, "text"):
                return block.text
    if isinstance(result, list):
        for block in result:
            if hasattr(block, "text"):
                return block.text
    if isinstance(result, str):
        return result
    return str(result)


async def _seed_channel(server, channel: str, messages: list[str]) -> None:
    """Helper: create channel + post messages. Returns nothing; the seeded
    messages are queried back via get_channel_messages."""
    await _call_tool(
        server, "create_channel",
        {"name": channel, "created_by": "alice"},
    )
    for m in messages:
        await _call_tool(
            server, "post",
            {"from_agent": "alice", "channel": channel, "message": m, "priority": "low"},
        )


# ---------------------------------------------------------------------------
# Backward compat — text format, time-based filtering
# ---------------------------------------------------------------------------


async def test_default_text_format_unchanged(server):
    await _seed_channel(server, "deploys", ["one", "two"])
    out = await _call_tool(
        server, "get_channel_messages", {"channel": "deploys"},
    )
    assert "**alice**" in out
    assert "one" in out
    assert "two" in out
    # text rendering uses [hh:mm:ss] timestamps, not raw ts
    assert "[" in out and ":" in out


async def test_text_format_explicit(server):
    await _seed_channel(server, "deploys", ["hello"])
    out = await _call_tool(
        server, "get_channel_messages",
        {"channel": "deploys", "format": "text"},
    )
    assert "**alice**" in out
    assert "hello" in out


async def test_empty_text_returns_empty_string(server):
    await _call_tool(server, "create_channel", {"name": "empty", "created_by": "alice"})
    out = await _call_tool(
        server, "get_channel_messages", {"channel": "empty"},
    )
    assert out == ""


# ---------------------------------------------------------------------------
# JSON format
# ---------------------------------------------------------------------------


async def test_json_format_returns_structured_records(server):
    await _seed_channel(server, "deploys", ["first", "second"])
    out = await _call_tool(
        server, "get_channel_messages",
        {"channel": "deploys", "format": "json"},
    )
    records = json.loads(out)
    assert isinstance(records, list)
    assert len(records) == 2
    for r in records:
        assert set(r.keys()) == {"id", "ts", "from_agent", "body", "priority"}
        assert isinstance(r["id"], int)
        assert isinstance(r["ts"], (int, float))
        assert r["from_agent"] == "alice"
        assert r["priority"] == "low"  # we seeded with priority=low
    # Bodies in id-order
    assert records[0]["body"] == "first"
    assert records[1]["body"] == "second"
    # IDs strictly increasing
    assert records[1]["id"] > records[0]["id"]


async def test_json_format_empty_returns_empty_array(server):
    """Empty result must be a parseable empty array, not the empty string."""
    await _call_tool(server, "create_channel", {"name": "empty", "created_by": "alice"})
    out = await _call_tool(
        server, "get_channel_messages",
        {"channel": "empty", "format": "json"},
    )
    assert json.loads(out) == []


async def test_invalid_format_rejected(server):
    out = await _call_tool(
        server, "get_channel_messages",
        {"channel": "deploys", "format": "yaml"},
    )
    assert "Invalid format" in out
    assert "yaml" in out


# ---------------------------------------------------------------------------
# since_id cursor
# ---------------------------------------------------------------------------


async def test_since_id_returns_only_newer_messages(server):
    await _seed_channel(server, "deploys", ["a", "b", "c"])
    # Get all; record their ids
    all_records = json.loads(
        await _call_tool(
            server, "get_channel_messages",
            {"channel": "deploys", "format": "json"},
        )
    )
    assert len(all_records) == 3
    cursor = all_records[1]["id"]  # max id we've "seen"

    # Cursor at second message; next call should return only the third
    out = await _call_tool(
        server, "get_channel_messages",
        {"channel": "deploys", "since_id": cursor, "format": "json"},
    )
    records = json.loads(out)
    assert len(records) == 1
    assert records[0]["body"] == "c"
    assert records[0]["id"] > cursor


async def test_since_id_zero_falls_back_to_time_window(server):
    """since_id=0 (the default) must use since_minutes filtering. This
    preserves backward compat for callers that don't know about cursors."""
    await _seed_channel(server, "deploys", ["one"])
    out = await _call_tool(
        server, "get_channel_messages",
        {"channel": "deploys", "since_id": 0, "format": "json"},
    )
    records = json.loads(out)
    assert len(records) == 1


async def test_since_id_overrides_since_minutes(server):
    """since_id > 0 ignores since_minutes — this is the load-bearing
    contract for cursor-based extraction. Without it, a cursor that's
    older than `since_minutes` would silently drop messages it should
    return, breaking the lossless guarantee on retries."""
    await _seed_channel(server, "deploys", ["msg-a", "msg-b"])

    # Tight time window that would normally exclude older messages — but
    # since_id should override it and return based on id alone.
    out = await _call_tool(
        server, "get_channel_messages",
        {
            "channel": "deploys",
            "since_id": 0,  # not yet using cursor
            "since_minutes": 0,  # zero-second window, would normally return nothing
            "format": "json",
        },
    )
    # With since_id=0, since_minutes=0 should filter everything out
    assert json.loads(out) == []

    # With since_id>0, since_minutes should be ignored — we still get nothing
    # (no messages with id > a future id) but the path is exercised
    out = await _call_tool(
        server, "get_channel_messages",
        {
            "channel": "deploys",
            "since_id": 1,  # cursor active, since_minutes ignored
            "since_minutes": 0,  # should be ignored
            "format": "json",
        },
    )
    records = json.loads(out)
    # Both seeded messages have id >= 1 (probably 1 and 2); since_id=1
    # returns those with id > 1.
    assert all(r["id"] > 1 for r in records)


async def test_since_id_lossless_under_retry(server):
    """The whole point of the cursor: same call repeated returns the same
    result, supporting safe retries without duplicates."""
    await _seed_channel(server, "deploys", ["x", "y", "z"])
    args = {"channel": "deploys", "since_id": 0, "format": "json"}

    a = json.loads(await _call_tool(server, "get_channel_messages", args))
    b = json.loads(await _call_tool(server, "get_channel_messages", args))
    assert a == b

    # Now using the max id as cursor — second call returns nothing new
    cursor = max(r["id"] for r in a)
    args_cursored = {"channel": "deploys", "since_id": cursor, "format": "json"}
    after = json.loads(await _call_tool(server, "get_channel_messages", args_cursored))
    assert after == []


async def test_since_id_returns_id_ordered_for_extraction(server):
    """When using cursor, results must be id-ordered (not ts-ordered) so
    that max(id) seen is always a safe next cursor."""
    await _seed_channel(server, "deploys", ["1", "2", "3", "4", "5"])
    out = await _call_tool(
        server, "get_channel_messages",
        {"channel": "deploys", "since_id": 0, "limit": 10, "format": "json"},
    )
    records = json.loads(out)
    ids = [r["id"] for r in records]
    assert ids == sorted(ids), "JSON records must be id-ordered for cursor advance"


# ---------------------------------------------------------------------------
# Limit
# ---------------------------------------------------------------------------


async def test_limit_caps_results(server):
    await _seed_channel(server, "deploys", [f"msg-{i}" for i in range(10)])
    out = await _call_tool(
        server, "get_channel_messages",
        {"channel": "deploys", "limit": 3, "format": "json"},
    )
    assert len(json.loads(out)) == 3


# ---------------------------------------------------------------------------
# from_agent filter — for "what have I posted" dedup pattern
# ---------------------------------------------------------------------------


async def _seed_channel_multi_agent(server, channel: str, posts: list[tuple[str, str]]) -> None:
    """Seed `channel` with one post per (agent, body) tuple."""
    await _call_tool(
        server, "create_channel",
        {"name": channel, "created_by": "alice"},
    )
    for agent, msg in posts:
        await _call_tool(
            server, "register",
            {"name": agent, "project": "x"},
        )
        await _call_tool(
            server, "post",
            {"from_agent": agent, "channel": channel, "message": msg, "priority": "low"},
        )


async def test_from_agent_filter_returns_only_that_agents_messages(server):
    """The dedup-on-re-asks pattern: an agent calls
    get_channel_messages(channel=..., from_agent=their-name) before
    re-posting to see what they already contributed."""
    await _seed_channel_multi_agent(
        server, "factory-backlog",
        [
            ("alice", "alice-item-1"),
            ("bob", "bob-item-1"),
            ("alice", "alice-item-2"),
            ("bob", "bob-item-2"),
        ],
    )

    out = await _call_tool(
        server, "get_channel_messages",
        {"channel": "factory-backlog", "from_agent": "alice", "format": "json"},
    )
    records = json.loads(out)
    assert len(records) == 2
    bodies = {r["body"] for r in records}
    assert bodies == {"alice-item-1", "alice-item-2"}


async def test_from_agent_filter_composes_with_since_id(server):
    """from_agent + since_id must compose so an agent can ask 'what
    have I posted SINCE my last cursor' for incremental dedup."""
    await _seed_channel_multi_agent(
        server, "factory-backlog",
        [
            ("alice", "old-1"),
            ("bob", "interleaved"),
            ("alice", "new-1"),
            ("alice", "new-2"),
        ],
    )

    # First read: alice gets all her posts; remember max id
    out = await _call_tool(
        server, "get_channel_messages",
        {"channel": "factory-backlog", "from_agent": "alice", "format": "json"},
    )
    records = json.loads(out)
    cursor = max(r["id"] for r in records if r["body"] == "old-1")

    # Now: alice asks "what have I posted since old-1's id"
    out = await _call_tool(
        server, "get_channel_messages",
        {
            "channel": "factory-backlog",
            "from_agent": "alice",
            "since_id": cursor,
            "format": "json",
        },
    )
    records = json.loads(out)
    bodies = [r["body"] for r in records]
    # Bob's "interleaved" must NOT appear (filtered out by from_agent),
    # and alice's old-1 must NOT appear (filtered out by since_id).
    assert bodies == ["new-1", "new-2"]


async def test_from_agent_filter_unknown_agent_returns_empty(server):
    """A from_agent value with no matching messages returns empty cleanly,
    not an error. Useful for first-time askers — the filter result is
    'you've posted nothing yet, post freely'."""
    await _seed_channel_multi_agent(
        server, "factory-backlog",
        [("alice", "first-post")],
    )

    out = await _call_tool(
        server, "get_channel_messages",
        {"channel": "factory-backlog", "from_agent": "ghost", "format": "json"},
    )
    assert json.loads(out) == []


async def test_from_agent_default_empty_returns_all_messages(server):
    """Default value (empty string) means 'no filter' — backward
    compatible. Existing callers without the param see no behavior change."""
    await _seed_channel_multi_agent(
        server, "factory-backlog",
        [("alice", "a"), ("bob", "b")],
    )

    out = await _call_tool(
        server, "get_channel_messages",
        {"channel": "factory-backlog", "format": "json"},
    )
    records = json.loads(out)
    assert len(records) == 2
