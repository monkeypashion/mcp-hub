"""W2.1 — the two squad registries stay in step (bar A1-A6).

THE SPLIT: `squad_members` is the fact (it alone decides broadcast audience);
`api_squads` is a record sidecar gating member-PUT, capsule compose and the
squad read routes. Nothing kept them in step, so a squad created through
`register`/`set_squads` was invisible to `GET /api/v1/squads`, 404'd on
member-PUT, and could not be composed — with its members sitting right there.
This class had ZERO test coverage: every existing squad test creates through
ONE surface and reads back through the SAME one.

The asymmetry under test is deliberate: `set_squads` REFUSES an archived
squad (authoritative, deliberate act — a mistake worth surfacing), while
`register` DROPS it with a notice (refusing a reconnect over bookkeeping
would take an agent offline, and every agent reconnects constantly).
"""

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from mcp_hub.server import create_server

OP = {"Authorization": "Bearer op-token"}


@pytest.fixture()
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MCP_HUB_API_TOKEN", "op-token")
    db_path = tmp_path / "hub.db"
    server = create_server(db_path=db_path)
    with TestClient(server.streamable_http_app()) as c:
        yield server, c, db_path


async def _tool(server, _tool_name, **args) -> str:
    res = await server._tool_manager.call_tool(_tool_name, args)
    if isinstance(res, str):
        return res
    content = getattr(res, "content", res)
    if isinstance(content, list):
        return "".join(getattr(c, "text", "") for c in content)
    return str(content)


def _squads(c) -> list[str]:
    return [s["name"] for s in c.get("/api/v1/squads", headers=OP).json()["squads"]]


# ---------------------------------------------------------------------------
# A2 — cross-surface round-trips (zero coverage before this file)
# ---------------------------------------------------------------------------


class TestCrossSurface:
    async def test_positive_control_rest_created_squad_is_visible(self, rig):
        """The harness can see a squad created the OTHER way — without this,
        the assertions below prove nothing about the new upsert."""
        _server, c, _db = rig
        assert c.post("/api/v1/squads", json={"name": "viaREST"},
                      headers=OP).status_code in (200, 201)
        assert "viaREST" in _squads(c)

    async def test_a_squad_created_by_register_is_visible_to_the_runtime(
        self, rig
    ):
        """Mutation: remove the _ensure_api_squad call from register →
        fails (the squad exists for comms and the runtime cannot see it)."""
        server, c, _db = rig
        await _tool(server, "register", name="alice", project="p",
                    squads="spike-x")
        assert "spike-x" in _squads(c)

    async def test_a_squad_created_by_set_squads_accepts_member_PUT(self, rig):
        """The 404 that made this concrete: member-PUT is gated on an
        api_squads row, so an MCP-created squad refused its own members.

        Mutation: remove the call from set_squads → 404 here."""
        server, c, _db = rig
        await _tool(server, "register", name="alice", project="p")
        await _tool(server, "set_squads", name="alice", squads="runtime")
        r = c.put("/api/v1/squads/runtime/members/some-seat", json={},
                  headers=OP)
        assert r.status_code in (200, 201), r.text

    async def test_an_mcp_created_squad_can_be_composed_into_a_capsule(
        self, rig
    ):
        """`capsules compose --register` exists because composing a LIVE
        squad 404'd with its members sitting right there."""
        server, c, _db = rig
        await _tool(server, "register", name="alice", project="p",
                    squads="podteam")
        r = c.post("/api/v1/capsules", json={"squad": "podteam"}, headers=OP)
        assert r.status_code in (200, 201), r.text

    async def test_member_count_reflects_the_comms_side(self, rig):
        server, c, _db = rig
        await _tool(server, "register", name="alice", project="p",
                    squads="counted")
        row = next(s for s in c.get("/api/v1/squads", headers=OP).json()["squads"]
                   if s["name"] == "counted")
        assert row["member_count"] == 1


# ---------------------------------------------------------------------------
# A3 — the archived asymmetry, tested BOTH ways
# ---------------------------------------------------------------------------


class TestArchivedAsymmetry:
    async def test_set_squads_REFUSES_an_archived_squad(self, rig):
        """Pre-fix, set_squads silently resurrected an archived name.

        Mutation: drop the refusal branch → fails."""
        server, c, _db = rig
        c.post("/api/v1/squads", json={"name": "retired"}, headers=OP)
        c.delete("/api/v1/squads/retired", headers=OP)
        await _tool(server, "register", name="alice", project="p")
        out = await _tool(server, "set_squads", name="alice",
                          squads="retired")
        assert "REFUSED" in out and "archived" in out

    async def test_the_refusal_changes_NOTHING(self, rig):
        """A refusal that half-applied would be worse than none — assert the
        state, not just the message (the estate's own rule)."""
        server, c, _db = rig
        c.post("/api/v1/squads", json={"name": "retired"}, headers=OP)
        c.delete("/api/v1/squads/retired", headers=OP)
        await _tool(server, "register", name="alice", project="p",
                    squads="keepme")
        await _tool(server, "set_squads", name="alice",
                    squads="retired,alsonew")
        listing = await _tool(server, "list_squads", agent="alice")
        assert "keepme" in listing          # not dropped by the failed call
        assert "alsonew" not in listing     # not half-applied
        assert "alsonew" not in _squads(c)

    async def test_register_DROPS_an_archived_squad_with_a_notice(self, rig):
        """The opposite branch, deliberately: a reconnect must not fail over
        bookkeeping — but the drop must be VISIBLE or the agent believes it
        joined and simply never hears the squad.

        Mutation: make register refuse instead → fails (no registration)."""
        server, c, _db = rig
        c.post("/api/v1/squads", json={"name": "gone"}, headers=OP)
        c.delete("/api/v1/squads/gone", headers=OP)
        out = await _tool(server, "register", name="bob", project="p",
                          squads="gone,live")
        assert "Registered as 'bob'" in out
        assert "NOT joined" in out and "gone" in out
        listing = await _tool(server, "list_squads", agent="bob")
        assert "live" in listing
        assert "gone" not in listing


# ---------------------------------------------------------------------------
# A4 — preserved behaviours (each one a thing the upsert could have broken)
# ---------------------------------------------------------------------------


class TestPreserved:
    async def test_archived_name_stays_reserved(self, rig):
        _server, c, _db = rig
        c.post("/api/v1/squads", json={"name": "res"}, headers=OP)
        c.delete("/api/v1/squads/res", headers=OP)
        r = c.post("/api/v1/squads", json={"name": "res"}, headers=OP)
        assert r.status_code == 409

    async def test_rm_without_purge_keeps_the_broadcast_audience(self, rig):
        """`squads rm` deliberately leaves memberships — an archived squad
        still has a live audience until purge. The upsert must not disturb
        that."""
        server, c, _db = rig
        await _tool(server, "register", name="alice", project="p",
                    squads="team")
        c.delete("/api/v1/squads/team", headers=OP)
        listing = await _tool(server, "list_squads", agent="alice")
        assert "team" in listing

    async def test_empty_squads_stay_legal(self, rig):
        _server, c, _db = rig
        c.post("/api/v1/squads", json={"name": "empty"}, headers=OP)
        row = next(s for s in c.get("/api/v1/squads", headers=OP).json()["squads"]
                   if s["name"] == "empty")
        assert row["member_count"] == 0

    async def test_register_with_no_squads_still_preserves(self, rig):
        """EMPTY PRESERVES — the reconnect rule. The upsert loop must not
        turn "no opinion" into "leave everything"."""
        server, _c, _db = rig
        await _tool(server, "register", name="alice", project="p",
                    squads="keep")
        await _tool(server, "register", name="alice", project="p")
        listing = await _tool(server, "list_squads", agent="alice")
        assert "keep" in listing


# ---------------------------------------------------------------------------
# A6 — membership provenance stops being half-wired
# ---------------------------------------------------------------------------


class TestProvenance:
    async def test_register_and_set_squads_stamp_their_source(self, rig):
        """`source` shipped half-wired: only REST writes set it, so every
        register/set_squads row said ''. Mutation: drop the source column
        from either INSERT → fails."""
        server, _c, db_path = rig
        import sqlite3 as _sq

        await _tool(server, "register", name="alice", project="p",
                    squads="fromreg")
        await _tool(server, "set_squads", name="alice",
                    squads="fromreg,fromset")
        con = _sq.connect(db_path)
        try:
            rows = dict(con.execute(
                "SELECT squad, source FROM squad_members WHERE agent = 'alice'"
            ).fetchall())
        finally:
            con.close()
        assert rows["fromset"] == "set_squads"
        assert rows["fromreg"] in ("register", "set_squads")
