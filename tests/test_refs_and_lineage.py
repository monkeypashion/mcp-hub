"""W3.1 + W3.2 — the ref envelope, the scheme registry, and the edge store.

The bar items these pin (docs/verification/wave-3.md): A1, A4, A5, A8 (walk),
A9 (coverage), B1-B5, plus the write-time refusals that keep garbage out of
the graph. B6 and the FJ-rule refusals live with the ra.feature/1 adapter.
"""

from __future__ import annotations

import sqlite3

import pytest

from mcp_hub import lineage, refs
from mcp_hub.refs import RefError, Scheme, canonical, make_ref, parse_ref


@pytest.fixture(autouse=True)
def _registry_isolated(monkeypatch):
    """Each test sees the real registry plus whatever it registers, and
    leaves no trace — a scheme registered in one test must not leak into the
    next, or 'already registered' refusals fire on innocent tests."""
    monkeypatch.setattr(refs, "_REGISTRY", dict(refs._REGISTRY))


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    lineage.ensure_schema(c)
    # the coverage query joins against messages — give it the real shape
    c.execute("CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY,"
              " body TEXT)")
    return c


MSG1 = "hub.msg/1?id=1"
MSG2 = "hub.msg/1?id=2"
AGENT = "hub.agent/1?name=alice"


# ---------------------------------------------------------------------------
# B1/B4/B5 — the envelope and the native schemes
# ---------------------------------------------------------------------------


class TestEnvelope:
    def test_hub_native_schemes_are_registered_first(self):
        """B5 — the hub dogfoods its own envelope: its schemes exist before
        any external adapter registers, through the same interface."""
        for key in ("hub.msg/1", "hub.decision/1", "hub.agent/1",
                    "hub.channel/1"):
            assert key in refs.registered_schemes()

    def test_positive_control_a_ref_round_trips(self):
        ref = parse_ref(MSG1)
        assert canonical(ref) == MSG1
        assert ref.get("id") == "1"

    def test_two_field_orders_collapse_to_ONE_node(self):
        """A4's deliberate negative. A graph whose node keys serialize two
        ways silently splits one node into two and lies about connectivity.

        Mutation: drop the sort in _validate -> this fails.
        """
        refs.register_scheme(Scheme("test.pair", 1,
                                    required=frozenset({"a", "b"})))
        one = parse_ref("test.pair/1?a=x&b=y")
        two = parse_ref("test.pair/1?b=y&a=x")
        assert canonical(one) == canonical(two) == "test.pair/1?a=x&b=y"

    def test_an_unknown_scheme_is_refused_NAMING_the_registered(self):
        """B3 — never resolved by a default; the refusal names the exits."""
        with pytest.raises(RefError, match="hub.msg/1"):
            parse_ref("no.such/1?id=1")

    @pytest.mark.parametrize("bad", [
        "hub.msg?id=1",        # version-less
        "hub.msg/0?id=1",      # zero version
        "hub.msg/1",           # no fields at all
        "hub.msg/1?id=",       # empty value — half an identity is missing
        "hub.msg/1?id=1&id=2", # duplicate field
        "",                    # empty
    ])
    def test_malformed_envelopes_are_refused(self, bad):
        """B4 — a version names the CONTRACT and is required."""
        with pytest.raises(RefError):
            parse_ref(bad)

    def test_versions_coexist_in_the_registry(self):
        """B4 — ra.feature/1 and a future ra.feature/2 are two schemes."""
        refs.register_scheme(Scheme("test.v", 1, required=frozenset({"id"})))
        refs.register_scheme(Scheme("test.v", 2, required=frozenset({"id"})))
        assert parse_ref("test.v/1?id=x").scheme == "test.v/1"
        assert parse_ref("test.v/2?id=x").scheme == "test.v/2"

    def test_a_forbidden_field_is_refused_CITING_the_reason(self):
        """The mechanism FJ rule 2 rides on — the refusal must carry the
        reason the scheme banned the field, not a bare no."""
        refs.register_scheme(Scheme(
            "test.pin", 1, required=frozenset({"id"}),
            forbidden={"version": "rule 2 — display-only or absent"},
        ))
        with pytest.raises(RefError, match="rule 2"):
            parse_ref("test.pin/1?id=x&version=3")

    def test_undefined_fields_are_refused_naming_the_defined(self):
        with pytest.raises(RefError, match="id"):
            parse_ref("hub.msg/1?id=1&surprise=x")

    def test_percent_encoding_round_trips_hostile_values(self):
        refs.register_scheme(Scheme("test.enc", 1, required=frozenset({"k"})))
        ref = make_ref("test.enc/1", k="a b&c=d?e%f")
        assert parse_ref(canonical(ref)) == ref


class TestNoRegrets:
    def test_a_second_scheme_registers_with_ZERO_core_changes(self):
        """🔴 B2 — THE no-regrets test, asserted rather than intended. This
        test file is not a core file; everything below goes through the
        public interface. If registering a scheme ever requires touching
        refs.py or lineage.py, this stops being possible and the test fails
        by construction.

        Mutation: make _validate special-case hub.* schemes -> the dummy
        stops working while natives keep passing, and this fails.
        """
        refs.register_scheme(Scheme("test.dummy", 1,
                                    required=frozenset({"id"})))
        ref = parse_ref("test.dummy/1?id=42")
        assert canonical(ref) == "test.dummy/1?id=42"

    def test_the_dummy_scheme_is_immediately_a_graph_node(self, conn):
        """A4's second half — a scheme registered for refs is usable as a
        lineage node with no further work: ONE identity mechanism."""
        refs.register_scheme(Scheme("test.dummy", 1,
                                    required=frozenset({"id"})))
        assert lineage.write_edge(conn, "test.dummy/1?id=42", "authored-by",
                                  AGENT, "auto")
        assert not lineage.walk(conn, "test.dummy/1?id=42")["lineage_blind"]

    def test_double_registration_is_refused(self):
        refs.register_scheme(Scheme("test.dummy", 1,
                                    required=frozenset({"id"})))
        with pytest.raises(RefError, match="already registered"):
            refs.register_scheme(Scheme("test.dummy", 1,
                                        required=frozenset({"id"})))


# ---------------------------------------------------------------------------
# A1/A5 — the edge store's write-time refusals
# ---------------------------------------------------------------------------


class TestEdgeWrites:
    def test_positive_control_an_edge_lands(self, conn):
        assert lineage.write_edge(conn, MSG1, "authored-by", AGENT, "auto")

    def test_an_unknown_predicate_is_refused_naming_the_vocabulary(self, conn):
        """A5 — same fail-closed rule as unknown schemes and unregistered
        status targets. Mutation: drop the PREDICATES check -> fails."""
        with pytest.raises(RefError, match="authored-by"):
            lineage.write_edge(conn, MSG1, "caused-by", AGENT, "auto")

    def test_a_self_edge_is_refused_at_write_time(self, conn):
        with pytest.raises(RefError, match="self-edge"):
            lineage.write_edge(conn, MSG1, "replies-to", MSG1, "declared")

    def test_a_malformed_ref_never_enters_the_store(self, conn):
        with pytest.raises(RefError):
            lineage.write_edge(conn, "not a ref", "authored-by", AGENT, "auto")
        assert conn.execute("SELECT COUNT(*) AS n FROM lineage_edges"
                            ).fetchone()["n"] == 0

    def test_append_only_reassertion_keeps_the_FIRST_ts(self, conn):
        """Append-only: re-asserting a fact is a no-op, and the timestamp
        stays the first recording — an edge is when the hub LEARNED it."""
        assert lineage.write_edge(conn, MSG1, "authored-by", AGENT, "auto")
        ts1 = conn.execute("SELECT ts FROM lineage_edges").fetchone()["ts"]
        assert lineage.write_edge(conn, MSG1, "authored-by", AGENT,
                                  "auto") is False
        assert conn.execute("SELECT ts FROM lineage_edges"
                            ).fetchone()["ts"] == ts1

    def test_an_invalid_source_is_refused(self, conn):
        """auto|declared is the trust distinction consumers rely on — a third
        value appearing silently would make the column unreadable."""
        with pytest.raises(RefError, match="auto"):
            lineage.write_edge(conn, MSG1, "authored-by", AGENT, "inferred")


# ---------------------------------------------------------------------------
# A8 — the bounded walk (Q1/Q2/Q4)
# ---------------------------------------------------------------------------


class TestWalk:
    def _chain(self, conn):
        # M2 replies to M1; both authored; M1 addressed to alice
        lineage.write_edge(conn, MSG1, "authored-by",
                           "hub.agent/1?name=bob", "auto")
        lineage.write_edge(conn, MSG1, "addressed-to", AGENT, "auto")
        lineage.write_edge(conn, MSG2, "replies-to", MSG1, "declared")
        lineage.write_edge(conn, MSG2, "authored-by", AGENT, "auto")

    def test_backward_walk_answers_how_did_this_come_about(self, conn):
        """Q1 — from M2, walking OUT follows replies-to to M1."""
        self._chain(conn)
        out = lineage.walk(conn, MSG2, depth=1, direction="out")
        assert MSG1 in out["nodes"]
        assert any(e["predicate"] == "replies-to" for e in out["edges"])

    def test_forward_walk_answers_what_resulted_from_X(self, conn):
        """Q2 — from M1, incoming edges find the reply."""
        self._chain(conn)
        out = lineage.walk(conn, MSG1, depth=1, direction="in")
        assert MSG2 in out["nodes"]

    def test_depth_bounds_the_walk(self, conn):
        self._chain(conn)
        near = lineage.walk(conn, MSG2, depth=1, direction="out")
        far = lineage.walk(conn, MSG2, depth=3, direction="both")
        assert len(far["nodes"]) >= len(near["nodes"])
        assert lineage.walk(conn, MSG1, depth=99)["depth"] == lineage.MAX_DEPTH

    def test_predicate_filter_serves_Q4(self, conn):
        self._chain(conn)
        out = lineage.walk(conn, AGENT, depth=1, direction="in",
                           predicate="authored-by")
        assert all(e["predicate"] == "authored-by" for e in out["edges"])
        assert out["edges"], "positive control: the filter found something"

    def test_an_edgeless_node_reads_lineage_blind_not_root(self, conn):
        """A3/A7 — 'nothing recorded' must stay distinguishable from 'this
        is a root'. Mutation: default lineage_blind to False -> fails."""
        out = lineage.walk(conn, "hub.msg/1?id=999")
        assert out["lineage_blind"] is True
        assert out["edges"] == []

    def test_the_source_column_reaches_the_consumer(self, conn):
        """Verdict 5's refinement: a reader can weight hub-witnessed edges
        above self-reported ones only if `source` survives to the output."""
        self._chain(conn)
        srcs = {e["source"] for e in lineage.walk(conn, MSG2)["edges"]}
        assert "declared" in srcs and "auto" in srcs

    def test_an_unknown_predicate_filter_is_refused(self, conn):
        with pytest.raises(RefError):
            lineage.walk(conn, MSG1, predicate="caused-by")


class TestCoverage:
    def test_sparse_reads_as_thinly_POPULATED(self, conn):
        """A9 — absence ≠ health on our own new surface: the API must say
        'few artifacts carry lineage', never render thinness as a quiet
        graph. Mutation: return only edge counts -> fails."""
        conn.execute("INSERT INTO messages (id, body) VALUES (1, 'x')")
        conn.execute("INSERT INTO messages (id, body) VALUES (2, 'y')")
        lineage.write_edge(conn, MSG1, "authored-by", AGENT, "auto")
        cov = lineage.coverage(conn)
        assert cov["messages"]["total"] == 2
        assert cov["messages"]["with_lineage"] == 1
        assert cov["edges"] == 1
