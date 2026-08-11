"""W2.1 A5 — a lapsed loan on the paths that actually DELIVER.

`purge_expired_memberships` has six call sites. Its own docstring names the
failure it exists to prevent: a filter applied to some readers and not others
gives the worst outcome — the loan reads as over everywhere the operator
LOOKS, and is still live on the path that pushes messages.

Until now that claim was tested at the helper and through the REST members
list — both operator-facing reads. The three sites below are the ones where a
survivor is a wrongly-delivered message rather than a wrong number on a
screen:

  server.broadcast              live push recipient filter
  server.get_broadcasts_for_agent   the Stop-hook catch-up (via _squads_of)
  api_v1.compose_capsule        a capsule composed after a loan lapsed

A lapsed row is planted by direct DB write on purpose: `add_squad_member`
REFUSES a deadline already in the past (422, tested elsewhere), so the state
under test is unreachable through the API — the same construct-it-directly
rule W1.1's archived-seat test follows, stated rather than hidden.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from mcp_hub.server import create_server


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "test.db"


@pytest.fixture
def server(db: Path):
    return create_server(db_path=db)


async def _call(server, name: str, args: dict) -> str:
    result = await server._tool_manager.call_tool(name, args)
    if hasattr(result, "content"):
        for block in result.content:
            if hasattr(block, "text"):
                return block.text
    return str(result)


def _lapse(db: Path, agent: str, squad: str) -> None:
    """Plant a membership whose deadline has already passed."""
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO squad_members (agent, squad, muted, "
            "joined, expires) VALUES (?, ?, 0, ?, ?)",
            (agent, squad, time.time() - 100, time.time() - 10),
        )
        conn.commit()
    finally:
        conn.close()


def _members(db: Path, squad: str) -> list[str]:
    conn = sqlite3.connect(db)
    try:
        return [r[0] for r in conn.execute(
            "SELECT agent FROM squad_members WHERE squad = ?", (squad,))]
    finally:
        conn.close()


class TestBroadcastDeliveryPaths:
    """The two paths a broadcast reaches an agent by. Both must forget a
    lapsed member, or 'muted'/'expired' would only ever mean 'delayed'."""

    @pytest.mark.anyio
    async def test_a_lapsed_loan_is_dropped_from_the_LIVE_PUSH_audience(
        self, server, db
    ):
        """🔴 The path the feature is about. `broadcast()` resolves its
        recipients from `squad_members`; a survivor here is a message actually
        delivered to someone whose loan ended.

        Mutation: remove purge_expired_memberships from broadcast() -> fails.
        """
        await _call(server, "register",
                    {"name": "alice", "project": "p", "squads": "spike"})
        await _call(server, "register",
                    {"name": "carol", "project": "p", "squads": "spike"})
        _lapse(db, "borrowed", "spike")
        assert "borrowed" in _members(db, "spike"), "the fixture planted nothing"

        with patch.object(server._hub_registry, "names",
                          lambda: ["alice", "carol", "borrowed"]):
            await _call(server, "broadcast",
                        {"from_agent": "alice", "message": "spike news",
                         "scope": "spike"})

        # the purge is the enforcement — the row is GONE, not merely filtered
        after = _members(db, "spike")
        assert "borrowed" not in after, (
            "the lapsed loan survived a broadcast — it would have been "
            "delivered to an agent whose membership had ended")
        # control: the purge took the LAPSED row and nothing else
        assert sorted(after) == ["alice", "carol"]

    @pytest.mark.anyio
    async def test_a_lapsed_loan_is_dropped_from_the_CATCH_UP_path(
        self, server, db
    ):
        """The second delivery path. Several of the 2026-07-27 cross-lane
        replies arrived through the Stop-hook catch-up rather than a live
        push, so a fix that covered only the push would be cosmetic.

        Mutation: remove purge_expired_memberships from _squads_of -> fails.
        """
        await _call(server, "register", {"name": "borrowed", "project": "p"})
        _lapse(db, "borrowed", "spike")
        assert "borrowed" in _members(db, "spike")

        await _call(server, "get_broadcasts_for_agent",
                    {"agent_name": "borrowed", "bind": False})

        assert _members(db, "spike") == [], (
            "the catch-up read the squad without enforcing the deadline")


class TestCapsuleComposition:
    @pytest.mark.anyio
    async def test_a_capsule_composed_after_a_loan_lapsed_does_not_RESURRECT_it(
        self, server, db, monkeypatch
    ):
        """A capsule freezes a squad. Freezing a membership that had already
        ended would carry it into every future placement of that capsule —
        the loan outliving its deadline by being photographed.

        Mutation: remove purge_expired_memberships from compose_capsule
        -> this fails.
        """
        from starlette.testclient import TestClient

        token = "test-operator-token"
        monkeypatch.setenv("MCP_HUB_API_TOKEN", token)
        headers = {"Authorization": f"Bearer {token}"}

        await _call(server, "register",
                    {"name": "alice", "project": "p", "squads": "spike"})
        _lapse(db, "borrowed", "spike")
        assert "borrowed" in _members(db, "spike")

        with TestClient(server.streamable_http_app()) as client:
            client.post("/api/v1/squads", json={"name": "spike"},
                        headers=headers)
            r = client.post("/api/v1/capsules", json={"squad": "spike"},
                            headers=headers)
            assert r.status_code in (200, 201), r.text
            seats = [s.get("identity") for s in r.json().get("seats", [])]

        assert "borrowed" not in seats, (
            "a lapsed loan was frozen into a capsule — it would be placed "
            "every time that capsule is placed")
        assert _members(db, "spike") == ["alice"]
