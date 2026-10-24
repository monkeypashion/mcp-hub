"""blocked-by — the first lineage edge that can become FALSE.

docs/lineage-blocked-by.md is the design; these tests are its gate. Every
other predicate records a past fact that stays true forever; "cannot start
until that clears" stops being true the moment it clears, and the hub
refuses to infer completion — so the edge is a PAIR of declarations with a
lifecycle (declared → cleared), and the clear is first-class.

The three hazards, each with tests:
  AUTHORITY  a lane provably declaring about someone else's work is
             refused; unowned subjects are allowed and ATTRIBUTED.
  LIFECYCLE  cleared edges are kept, never deleted; clears against nothing
             refuse loudly; only the declaring authority (or operator)
             clears.
  RENDERING  cleared edges leave the path view by default but stay
             queryable; live edges carry declared_at so staleness is
             RENDERED, never resolved away.
"""

from __future__ import annotations

import sqlite3

import pytest

from mcp_hub import lineage, refs
from mcp_hub.refs import RefError


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    lineage.ensure_schema(c)
    return c


MSG_A = "hub.msg/1?id=101"
MSG_B = "hub.msg/1?id=202"
WORK = "ra.feature/1?feature_set_key=k&id=7"


def _author(conn, subject, agent):
    lineage.write_edge(conn, subject, "authored-by",
                       refs.make_ref("hub.agent/1", name=agent), "auto")


def _edge(conn, s, o):
    return conn.execute(
        "SELECT * FROM lineage_edges WHERE subject = ? AND "
        "predicate = 'blocked-by' AND object = ?", (s, o),
    ).fetchone()


# ---------------------------------------------------------------- authority


class TestAuthority:
    def test_owner_declares_own_work(self, conn):
        _author(conn, MSG_A, "alice")
        assert lineage.declare_blocked(conn, MSG_A, MSG_B, "alice") is True
        assert _edge(conn, MSG_A, MSG_B)["declared_by"] == "alice"

    def test_declaring_someone_elses_work_is_REFUSED(self, conn):
        """The wrong answer that ships easily: any lane painting any other
        lane stuck. Provable non-ownership refuses."""
        _author(conn, MSG_A, "alice")
        with pytest.raises(RefError, match="owns the blocked work"):
            lineage.declare_blocked(conn, MSG_A, MSG_B, "mallory")
        assert _edge(conn, MSG_A, MSG_B) is None

    def test_unowned_subject_is_allowed_and_attributed(self, conn):
        """External work items have no ownership model on the hub. Refusing
        would make the most useful refs undeclarable; the honest middle is
        allow + attribute, so a consumer can weight it."""
        assert lineage.declare_blocked(conn, WORK, MSG_B, "dt-poc") is True
        assert _edge(conn, WORK, MSG_B)["declared_by"] == "dt-poc"

    def test_a_prefix_name_cannot_pass_for_the_author(self, conn):
        """Ref-equality, not substring: 'bob' must not pass for a subject
        authored by 'alice-bob' (nor vice versa)."""
        _author(conn, MSG_A, "alice-bob")
        with pytest.raises(RefError):
            lineage.declare_blocked(conn, MSG_A, MSG_B, "bob")

    def test_anonymous_declaration_is_refused(self, conn):
        with pytest.raises(RefError, match="declarer"):
            lineage.declare_blocked(conn, MSG_A, MSG_B, "")

    def test_write_edge_refuses_the_predicate_outright(self, conn):
        """Defence in depth: the generic writer would mint an unattributed,
        unclearable blockage."""
        with pytest.raises(RefError, match="declare_blocked"):
            lineage.write_edge(conn, MSG_A, "blocked-by", MSG_B, "declared")

    def test_self_blockage_is_refused(self, conn):
        with pytest.raises(RefError, match="self-edge"):
            lineage.declare_blocked(conn, MSG_A, MSG_A, "alice")


# ---------------------------------------------------------------- lifecycle


class TestLifecycle:
    def test_clear_marks_and_KEEPS_the_edge(self, conn):
        lineage.declare_blocked(conn, WORK, MSG_B, "dt-poc")
        lineage.clear_blocked(conn, WORK, MSG_B, "dt-poc")
        row = _edge(conn, WORK, MSG_B)
        assert row is not None, (
            "the clear DELETED the edge — a vanished edge is "
            "indistinguishable from one never declared"
        )
        assert row["cleared_at"] is not None
        assert row["cleared_by"] == "dt-poc"

    def test_clearing_nothing_refuses_loudly(self, conn):
        """A clear against nothing is a typo wearing a path's clothes."""
        with pytest.raises(RefError, match="ever declared"):
            lineage.clear_blocked(conn, WORK, MSG_B, "dt-poc")

    def test_double_clear_refuses(self, conn):
        lineage.declare_blocked(conn, WORK, MSG_B, "dt-poc")
        lineage.clear_blocked(conn, WORK, MSG_B, "dt-poc")
        with pytest.raises(RefError, match="already cleared"):
            lineage.clear_blocked(conn, WORK, MSG_B, "dt-poc")

    def test_only_the_declarer_clears(self, conn):
        lineage.declare_blocked(conn, WORK, MSG_B, "dt-poc")
        with pytest.raises(RefError, match="declaring authority"):
            lineage.clear_blocked(conn, WORK, MSG_B, "mallory")
        assert _edge(conn, WORK, MSG_B)["cleared_at"] is None

    def test_the_operator_may_clear_anything(self, conn):
        lineage.declare_blocked(conn, WORK, MSG_B, "dt-poc")
        lineage.clear_blocked(conn, WORK, MSG_B, "operator", is_operator=True)
        assert _edge(conn, WORK, MSG_B)["cleared_at"] is not None

    def test_redeclaring_a_LIVE_edge_is_idempotent_and_keeps_the_clock(self, conn):
        """Mashing "still blocked" must not shift declared_at — the age is
        the staleness instrument and resetting it would launder an old
        blockage into a fresh-looking one."""
        lineage.declare_blocked(conn, WORK, MSG_B, "dt-poc")
        first_ts = _edge(conn, WORK, MSG_B)["ts"]
        assert lineage.declare_blocked(conn, WORK, MSG_B, "dt-poc") is False
        assert _edge(conn, WORK, MSG_B)["ts"] == first_ts

    def test_redeclaring_a_CLEARED_edge_reopens_with_a_new_clock(self, conn):
        """A new blockage on an old pair is a NEW declaration — dating it by
        the dead one would misage it from birth."""
        lineage.declare_blocked(conn, WORK, MSG_B, "dt-poc")
        old_ts = _edge(conn, WORK, MSG_B)["ts"]
        lineage.clear_blocked(conn, WORK, MSG_B, "dt-poc")
        assert lineage.declare_blocked(conn, WORK, MSG_B, "dt-poc") is True
        row = _edge(conn, WORK, MSG_B)
        assert row["cleared_at"] is None, "re-open left the edge cleared"
        assert row["ts"] >= old_ts

    def test_two_independent_blockages_coexist_and_clear_separately(self, conn):
        """Work can genuinely wait on two things — no supersession."""
        lineage.declare_blocked(conn, WORK, MSG_A, "dt-poc")
        lineage.declare_blocked(conn, WORK, MSG_B, "dt-poc")
        lineage.clear_blocked(conn, WORK, MSG_A, "dt-poc")
        assert _edge(conn, WORK, MSG_A)["cleared_at"] is not None
        assert _edge(conn, WORK, MSG_B)["cleared_at"] is None


# ---------------------------------------------------------------- rendering


class TestRendering:
    def test_a_live_blockage_is_in_the_path_view_with_its_age(self, conn):
        lineage.declare_blocked(conn, WORK, MSG_B, "dt-poc")
        out = lineage.walk(conn, WORK)
        blocked = [e for e in out["edges"] if e["predicate"] == "blocked-by"]
        assert len(blocked) == 1
        assert blocked[0]["declared_at"], (
            "no declared_at — staleness cannot be rendered, and an undated "
            "forward edge is a fossil factory"
        )
        assert blocked[0]["declared_by"] == "dt-poc"

    def test_a_cleared_blockage_LEAVES_the_path_view(self, conn):
        """The whole point: the path routes around finished blockages."""
        lineage.declare_blocked(conn, WORK, MSG_B, "dt-poc")
        lineage.clear_blocked(conn, WORK, MSG_B, "dt-poc")
        out = lineage.walk(conn, WORK)
        assert not [e for e in out["edges"] if e["predicate"] == "blocked-by"], (
            "a CLEARED blockage still shows in the path view — the fossil "
            "pointing forward, the exact failure the design refuses"
        )

    def test_cleared_history_stays_queryable(self, conn):
        lineage.declare_blocked(conn, WORK, MSG_B, "dt-poc")
        lineage.clear_blocked(conn, WORK, MSG_B, "dt-poc")
        out = lineage.walk(conn, WORK, include_cleared=True)
        blocked = [e for e in out["edges"] if e["predicate"] == "blocked-by"]
        assert len(blocked) == 1
        assert blocked[0]["cleared_by"] == "dt-poc"

    def test_other_predicates_are_untouched_by_the_filter(self, conn):
        _author(conn, MSG_A, "alice")
        out = lineage.walk(conn, MSG_A)
        assert [e for e in out["edges"] if e["predicate"] == "authored-by"]


# ------------------------------------------------------------- the transport


class TestTransport:
    """blocked_by rides send/post/decision_put as a param, in_reply_to's
    pattern: validated BEFORE the carrying message is stored, refused
    loudly. No raw edge-write API exists — that invariant survives."""

    @pytest.fixture
    def server(self, tmp_path):
        from mcp_hub.server import create_server

        return create_server(db_path=tmp_path / "hub.db")

    async def _call(self, server, name, args):
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

    async def _pair(self, server):
        await self._call(server, "register", {"name": "alice", "project": "p"})
        await self._call(server, "register", {"name": "bob", "project": "p"})

    def _db(self, server):
        conn = sqlite3.connect(server._hub_db_path)
        conn.row_factory = sqlite3.Row
        return conn

    async def test_send_declares_and_the_message_still_lands(self, server):
        await self._pair(server)
        out = await self._call(server, "send", {
            "from_agent": "alice", "to": "bob", "message": "pkg-7 waits",
            "blocked_by": f"{WORK}|{MSG_B}",
        })
        assert "refused" not in out.lower(), out
        row = self._db(server).execute(
            "SELECT * FROM lineage_edges WHERE predicate = 'blocked-by'"
        ).fetchone()
        assert row is not None and row["declared_by"] == "alice"

    async def test_a_malformed_declaration_refuses_the_SEND_loudly(self, server):
        """The in_reply_to precedent: a bad declaration must not let the
        message sail through with the edge silently dropped."""
        await self._pair(server)
        out = await self._call(server, "send", {
            "from_agent": "alice", "to": "bob", "message": "x",
            "blocked_by": "not-a-ref-at-all",
        })
        assert "refused" in out.lower()
        n = self._db(server).execute(
            "SELECT COUNT(*) AS n FROM messages WHERE body = 'x'"
        ).fetchone()["n"]
        assert n == 0, "the carrying message was stored despite the refusal"

    async def test_clear_via_send_round_trips(self, server):
        await self._pair(server)
        await self._call(server, "send", {
            "from_agent": "alice", "to": "bob", "message": "blocked",
            "blocked_by": f"{WORK}|{MSG_B}",
        })
        out = await self._call(server, "send", {
            "from_agent": "alice", "to": "bob", "message": "unblocked",
            "blocked_by": f"clear:{WORK}|{MSG_B}",
        })
        assert "refused" not in out.lower(), out
        row = self._db(server).execute(
            "SELECT cleared_by FROM lineage_edges WHERE predicate = 'blocked-by'"
        ).fetchone()
        assert row["cleared_by"] == "alice"

    async def test_another_lane_cannot_clear_via_send(self, server):
        await self._pair(server)
        await self._call(server, "send", {
            "from_agent": "alice", "to": "bob", "message": "blocked",
            "blocked_by": f"{WORK}|{MSG_B}",
        })
        out = await self._call(server, "send", {
            "from_agent": "bob", "to": "alice", "message": "clearing yours",
            "blocked_by": f"clear:{WORK}|{MSG_B}",
        })
        assert "refused" in out.lower()

    async def test_get_lineage_renders_the_live_blockage_with_age(self, server):
        import json as _json

        await self._pair(server)
        await self._call(server, "send", {
            "from_agent": "alice", "to": "bob", "message": "blocked",
            "blocked_by": f"{WORK}|{MSG_B}",
        })
        out = await self._call(server, "get_lineage", {"ref": WORK})
        data = _json.loads(out)
        blocked = [e for e in data["edges"] if e["predicate"] == "blocked-by"]
        assert blocked and blocked[0]["declared_at"], out
