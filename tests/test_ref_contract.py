"""W3.3 + W3.4 + W3.5 — the ra.feature/1 resolver, FJ's six refusal rules,
and status resolution that fails closed.

Every rule test cites `hub-work-item-ref-contract.md@7b2e0eb` — the text FJ
pasted verbatim and re-verified byte-identical from `04a4255`. The rules:

  1 ⛔ a ref missing either half of the pair
  2 ⛔ a ref carrying `version` as a pin
  3 ⛔ a ref resolving status against any repo copy (intent ≠ state)
  4 ⛔ a resolution not naming document AND vocabulary (TWO-part)
  5 ⛔ an unknown feature_set_key — fail closed, never 'new', no lineage
  6 ⚠️ a pair resolving to >1 feature fails loudly, never picks one

Rules 1/2/5/6 are about refs and live in the adapter; rules 3/4 are about
the RESOLVED ANSWER and live in status_resolution — FJ's fit-check, adopted
before build.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from mcp_hub import lineage, ra_feature, status_resolution
from mcp_hub.refs import RefError, parse_ref
from mcp_hub.server import create_server


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    ra_feature.ensure_schema(c)
    status_resolution.ensure_schema(c)
    lineage.ensure_schema(c)
    return c


def _doc(*ids):
    return {"features": [
        {"id": i, "name": f"feature {i}", "description": "d", "phase": 1,
         "status": "completed"}
        for i in ids
    ]}


# ---------------------------------------------------------------------------
# Rules 1 + 2 — the ref itself (envelope-enforced)
# ---------------------------------------------------------------------------


class TestRules1And2:
    def test_positive_control_a_full_pair_parses(self):
        ref = parse_ref("ra.feature/1?feature_set_key=spike&id=f1")
        assert ref.get("feature_set_key") == "spike"

    @pytest.mark.parametrize("half", [
        "ra.feature/1?id=f1",                    # key missing
        "ra.feature/1?feature_set_key=spike",    # id missing
    ])
    def test_rule_1_either_half_alone_is_refused(self, half):
        """⛔ Rule 1 (7b2e0eb): no key-only, no id-only. The same feature.id
        in two sets is two different features — half a pair resolves to *a*
        feature, never *the* feature.

        Mutation: drop feature_set_key or id from required -> fails.
        """
        with pytest.raises(RefError, match="requires"):
            parse_ref(half)

    def test_rule_2_a_version_PIN_is_refused_citing_the_rule(self):
        """⛔ Rule 2 (7b2e0eb), and B6's confusable case: the /1 in the
        scheme names the CONTRACT; a `version` field pinning the ITEM is a
        stale pointer masquerading as a precise one.

        Mutation: drop `version` from forbidden -> this fails.
        """
        with pytest.raises(RefError, match="rule 2"):
            parse_ref("ra.feature/1?feature_set_key=spike&id=f1&version=3")


# ---------------------------------------------------------------------------
# Rule 5 + C2 — unknown keys fail closed, and are NEVER derived
# ---------------------------------------------------------------------------


class TestRule5AndKeys:
    def test_rule_5_an_unknown_key_is_refused_and_mints_NOTHING(self, conn):
        """⛔ Rule 5 (7b2e0eb), BOTH halves: unknown means unknown (never
        'new'), and no lineage is auto-minted for it — an unknown key that
        quietly gained a graph node would be 'new' with extra steps.

        Mutation: treat unknown keys as empty sets -> first assert fails.
        Mutation: mint a node for the asked ref -> second assert fails.
        """
        ra_feature.register_feature_set(conn, "known", _doc("f1"))
        ref = parse_ref("ra.feature/1?feature_set_key=ghost&id=f1")
        with pytest.raises(RefError, match="unknown means UNKNOWN"):
            ra_feature.resolve(conn, ref)
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM lineage_edges"
        ).fetchone()["n"] == 0

    def test_the_refusal_names_the_registered_sets(self, conn):
        ra_feature.register_feature_set(conn, "known", _doc("f1"))
        with pytest.raises(RefError, match="known"):
            ra_feature.resolve(
                conn, parse_ref("ra.feature/1?feature_set_key=ghost&id=f1"))

    def test_C2_the_key_is_never_derived_from_the_repo_name(self, conn):
        """🔴 C2, with the LIVE counter-example as the fixture:
        `dreamteam-analytics-service`'s set key is `analytics-service` —
        restored as-is because repair criteria forbid editing during a
        restore. A resolver that derived the key from the repo name would
        fail-closed on the RIGHT key and resolve on the WRONG one.

        Mutation: fall back to a repo-name-derived key on miss -> the
        second assert stops raising and this fails.
        """
        ra_feature.register_feature_set(conn, "analytics-service", _doc("f1"))
        ok = ra_feature.resolve(conn, parse_ref(
            "ra.feature/1?feature_set_key=analytics-service&id=f1"))
        assert ok["resolved"] is True
        with pytest.raises(RefError, match="unknown feature_set_key"):
            ra_feature.resolve(conn, parse_ref(
                "ra.feature/1?feature_set_key=dreamteam-analytics-service"
                "&id=f1"))


# ---------------------------------------------------------------------------
# Rule 6 + C3 — ambiguity fails loudly, and is NOT "not found"
# ---------------------------------------------------------------------------


class TestRule6AndAmbiguity:
    def test_rule_6_registration_refuses_duplicate_ids_at_first_notice(
        self, conn
    ):
        """⚠️ Rule 6 (7b2e0eb): uniqueness is unenforced upstream, so the hub
        is the first place that can notice — and it notices at registration.

        Mutation: drop the dupes check in register_feature_set -> fails.
        """
        with pytest.raises(RefError, match="rule 6"):
            ra_feature.register_feature_set(conn, "spike",
                                            _doc("f1", "f1", "f2"))

    def test_rule_6_resolution_ALSO_refuses_a_planted_ambiguous_doc(
        self, conn
    ):
        """D4 — defence in depth: a row can predate the registration gate or
        arrive by other means, so resolve checks again. The ambiguous state
        is unreachable through the API (registration refuses it), so it is
        planted by direct DB write — the same construct-it-directly rule
        W1.1's archived-seat test stated and followed.

        Mutation: `matches[0]` on multi-match instead of raising -> fails.
        """
        conn.execute(
            "INSERT INTO ra_feature_sets (key, document, registered_at)"
            " VALUES ('legacy', ?, 0)",
            (json.dumps(_doc("dup", "dup")),),
        )
        conn.commit()
        with pytest.raises(RefError, match="AMBIGUOUS"):
            ra_feature.resolve(conn, parse_ref(
                "ra.feature/1?feature_set_key=legacy&id=dup"))

    def test_C3_ambiguous_and_not_found_are_DISTINCT_outcomes(self, conn):
        """A corrupt document must not be reported as a missing feature —
        they demand different repairs (fix the file vs fix the ref)."""
        conn.execute(
            "INSERT INTO ra_feature_sets (key, document, registered_at)"
            " VALUES ('legacy', ?, 0)",
            (json.dumps(_doc("dup", "dup")),),
        )
        conn.commit()
        with pytest.raises(RefError, match="corrupt DOCUMENT"):
            ra_feature.resolve(conn, parse_ref(
                "ra.feature/1?feature_set_key=legacy&id=dup"))
        missing = ra_feature.resolve(conn, parse_ref(
            "ra.feature/1?feature_set_key=legacy&id=nope"))
        assert missing == {
            "resolved": False, "reason": "not-found",
            "detail": missing["detail"],
        }


# ---------------------------------------------------------------------------
# Rule 3 — the resolver NEVER hands out status
# ---------------------------------------------------------------------------


class TestRule3:
    def test_the_resolved_answer_conspicuously_omits_status(self, conn):
        """⛔ Rule 3 (7b2e0eb): the stored document carries INTENT, and
        intent ≠ state. The fixture's feature says status=completed; the
        resolver must return status=None with the rule named — handing the
        stored value through would be the feature_outcomes mirror at the
        ref layer.

        Mutation: return f.get("status") -> this fails.
        """
        ra_feature.register_feature_set(conn, "spike", _doc("f1"))
        out = ra_feature.resolve(conn, parse_ref(
            "ra.feature/1?feature_set_key=spike&id=f1"))
        assert out["resolved"] is True
        assert out["status"] is None
        assert "rule 3" in out["status_note"]


# ---------------------------------------------------------------------------
# Rule 4 + E1-E4 — status resolution fails closed
# ---------------------------------------------------------------------------


class TestStatusResolution:
    REF = "ra.feature/1?feature_set_key=spike&id=f1"

    def test_E1_E4_an_unregistered_hub_answers_UNRESOLVABLE_not_not_done(
        self, conn
    ):
        """🔴 E4 — the load-bearing distinction: 'no instrument' vs 'a
        measurement'. A hub that answered not-done here would be issuing
        confident false delivery reports.

        Mutation: default status to 'not_attempted' with no target -> fails.
        """
        out = status_resolution.resolve_status(conn, self.REF)
        assert out["resolvable"] is False
        assert out["status"] is None
        assert "UNRESOLVABLE" in out["reason"]
        assert "NOT 'not done'" in out["reason"]

    def test_rule_3_a_non_observed_source_kind_is_refused(self, conn):
        """⛔ Rule 3 at the registration gate: an authored document or repo
        copy can never be blessed. Mutation: accept any source_kind -> fails.
        """
        with pytest.raises(RefError, match="intent ≠ state"):
            status_resolution.register_status_target(
                conn, "some-table", document="d", vocabulary="v",
                source_kind="repo-copy",
                attestation=status_resolution.REQUIRED_ATTESTATION,
            )

    def test_rule_4_missing_DOCUMENT_is_refused(self, conn):
        """⛔ Rule 4, first half. TWO negatives on purpose: a combined check
        can pass while enforcing half the rule."""
        with pytest.raises(RefError, match="WHICH DOCUMENT"):
            status_resolution.register_status_target(
                conn, "t", document="", vocabulary="v",
                source_kind="observed-log",
                attestation=status_resolution.REQUIRED_ATTESTATION,
            )

    def test_rule_4_missing_VOCABULARY_is_refused(self, conn):
        """⛔ Rule 4, second half — four vocabularies exist, not three, and
        an unnamed one is how 'skipped' silently collapses to 'blocked'."""
        with pytest.raises(RefError, match="WHICH STATUS VOCABULARY"):
            status_resolution.register_status_target(
                conn, "t", document="d", vocabulary="",
                source_kind="observed-log",
                attestation=status_resolution.REQUIRED_ATTESTATION,
            )

    def test_E3_feature_outcomes_is_refused_NAMING_the_mirror_evidence(
        self, conn
    ):
        """🔴 E3 + FJ's standing condition: rows reappearing is not the
        signal — the mirror-detector passing is. The refusal names the
        COALESCE evidence so the reason travels with the no.

        Mutation: drop the attestation gate -> this fails.
        """
        with pytest.raises(RefError, match="COALESCE"):
            status_resolution.register_status_target(
                conn, "feature_outcomes", document="d", vocabulary="v",
                source_kind="observed-log", attestation="",
            )

    def test_E2_a_properly_attested_target_names_document_AND_vocabulary(
        self, conn
    ):
        """The response contract downstream consumers build against."""
        status_resolution.register_status_target(
            conn, "observed-completions",
            document="dt-completion-log", vocabulary="FeatureStatus/4",
            source_kind="observed-log",
            attestation=status_resolution.REQUIRED_ATTESTATION,
        )
        out = status_resolution.resolve_status(conn, self.REF)
        assert out["resolvable"] is True
        assert out["document"] == "dt-completion-log"
        assert out["vocabulary"] == "FeatureStatus/4"

    def test_E2_defence_a_target_row_missing_a_half_is_unanswerable(
        self, conn
    ):
        """Planted by direct DB write (the registration gate refuses it):
        however a half-target got here, it must not produce an answer."""
        conn.execute(
            "INSERT INTO status_targets (name, document, vocabulary,"
            " source_kind, attestation, registered_at)"
            " VALUES ('bad', 'd', '', 'observed-log', ?, 0)",
            (status_resolution.REQUIRED_ATTESTATION,),
        )
        conn.commit()
        with pytest.raises(RefError, match="rule 4"):
            status_resolution.resolve_status(conn, self.REF)


# ---------------------------------------------------------------------------
# D1 — through the MCP tools (the surface agents actually touch)
# ---------------------------------------------------------------------------


class TestThroughTheTools:
    @pytest.fixture
    def server(self, tmp_path: Path):
        return create_server(db_path=tmp_path / "test.db")

    async def _call(self, server, name, args):
        result = await server._tool_manager.call_tool(name, args)
        if hasattr(result, "content"):
            for block in result.content:
                if hasattr(block, "text"):
                    return block.text
        return str(result)

    @pytest.mark.anyio
    async def test_resolve_status_on_prod_shape_REFUSES(self, server):
        """What F3's live bar will re-run against prod: a fresh hub answers
        unresolvable, with the reason, not a status."""
        out = json.loads(await self._call(
            server, "resolve_status",
            {"ref": "ra.feature/1?feature_set_key=spike&id=f1"},
        ))
        assert out["resolvable"] is False and out["status"] is None

    @pytest.mark.anyio
    async def test_resolve_ref_refuses_a_scheme_with_no_resolver(self, server):
        """hub.msg refs are identities, not resolvable work items — the
        refusal says so instead of guessing at semantics."""
        out = await self._call(server, "resolve_ref",
                               {"ref": "hub.msg/1?id=1"})
        assert out.startswith("REFUSED:") and "no resolver" in out

    @pytest.mark.anyio
    async def test_hub_msg_refusal_names_the_recovery_routes(self, server):
        """A clipped render carries a ref that this tool refuses — the
        refusal must say where the body actually lives, or the reader is
        left scanning history by guesswork (spike-runtime, 2026-08-12).
        Only message refs get the pointer: other resolverless schemes have
        no body to recover, and a false pointer is worse than none."""
        out = await self._call(server, "resolve_ref",
                               {"ref": "hub.msg/1?id=1"})
        assert "get_messages" in out and "get_history" in out
        other = await self._call(server, "resolve_ref",
                                 {"ref": "hub.agent/1?name=x"})
        assert other.startswith("REFUSED:")
        assert "get_messages" not in other

    @pytest.mark.anyio
    async def test_rule_1_reaches_the_tool_surface(self, server):
        out = await self._call(server, "resolve_ref",
                               {"ref": "ra.feature/1?id=f1"})
        assert out.startswith("REFUSED:")


class TestFeatureSetsRoute:
    """The registration door is REST + operator-token only — no agent-writable
    surface was added by this wave."""

    @pytest.fixture
    def client(self, tmp_path, monkeypatch):
        from starlette.testclient import TestClient
        monkeypatch.setenv("MCP_HUB_API_TOKEN", "test-operator-token")
        server = create_server(db_path=tmp_path / "hub.db")
        with TestClient(server.streamable_http_app()) as c:
            yield c

    H = {"Authorization": "Bearer test-operator-token"}

    def test_register_then_resolve_round_trips(self, client):
        r = client.post("/api/v1/feature-sets", headers=self.H, json={
            "key": "spike", "document": _doc("f1"),
        })
        assert r.status_code == 201, r.text
        listed = client.get("/api/v1/feature-sets", headers=self.H).json()
        assert [s["key"] for s in listed["feature_sets"]] == ["spike"]

    def test_rule_6_reaches_the_route_as_a_422(self, client):
        r = client.post("/api/v1/feature-sets", headers=self.H, json={
            "key": "spike", "document": _doc("f1", "f1"),
        })
        assert r.status_code == 422
        assert "rule 6" in r.json()["detail"]

    def test_no_token_no_registration(self, client):
        assert client.post("/api/v1/feature-sets", json={
            "key": "x", "document": _doc("f1"),
        }).status_code == 401
