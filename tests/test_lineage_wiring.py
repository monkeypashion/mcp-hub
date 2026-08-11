"""W3.1 — the lineage graph wired into the hub's verbs.

Bar items pinned here: A2 (positive control + incident suite untouched — the
27 broadcast-scope tests run unedited elsewhere), A3 (auto edges per type),
A6 (refs visible where messages are read; a copied ref works as in_reply_to),
A10 (NO inference — the load-bearing refusal), plus the declared-path
refusals: a malformed or nonexistent reply target refuses the SEND, loudly,
before anything is stored.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import patch

import pytest

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
    return str(result)


async def _setup(server, *names):
    for n in names:
        await _call(server, "register", {"name": n, "project": "p"})


async def _lineage(server, ref, **kw):
    return json.loads(await _call(server, "get_lineage", {"ref": ref, **kw}))


REF_RE = re.compile(r"⟨(hub\.msg/1\?id=\d+)⟩")


class TestAutoEdges:
    @pytest.mark.anyio
    async def test_positive_control_a_send_still_delivers_and_gains_edges(
        self, server
    ):
        """A2/A3 — delivery is unchanged AND the hub's own acts are recorded:
        authorship and routing are facts it just performed."""
        await _setup(server, "alice", "bob")
        out = await _call(server, "send", {
            "from_agent": "alice", "to": "bob", "message": "hello",
        })
        assert "queued" in out or "sent" in out  # delivery verdict unchanged
        g = await _lineage(server, "hub.msg/1?id=1")
        preds = {(e["predicate"], e["source"]) for e in g["edges"]}
        assert ("authored-by", "auto") in preds
        assert ("addressed-to", "auto") in preds
        assert "hub.agent/1?name=alice" in g["nodes"]
        assert "hub.agent/1?name=bob" in g["nodes"]

    @pytest.mark.anyio
    async def test_a_post_is_addressed_to_its_channel(self, server):
        await _setup(server, "alice")
        await _call(server, "create_channel", {
            "name": "dev", "created_by": "alice",
        })
        await _call(server, "post", {
            "from_agent": "alice", "channel": "dev", "message": "x",
            "priority": "low",
        })
        g = await _lineage(server, "hub.channel/1?name=dev", direction="in")
        assert any(e["predicate"] == "addressed-to" for e in g["edges"])

    @pytest.mark.anyio
    async def test_a_squad_broadcast_is_addressed_to_the_squad(self, server):
        await _call(server, "register", {
            "name": "alice", "project": "p", "squads": "spike",
        })
        await _call(server, "broadcast", {
            "from_agent": "alice", "message": "x", "priority": "low",
        })
        g = await _lineage(server, "hub.squad/1?name=spike", direction="in")
        assert any(e["predicate"] == "addressed-to" for e in g["edges"])

    @pytest.mark.anyio
    async def test_the_card_lifecycle_records_supersede_and_resolve(
        self, server
    ):
        """A3 — the hub OWNS the card lifecycle, so these edges are its own
        acts: a different ask supersedes the open card; a DECIDED resolves."""
        await _setup(server, "alice")
        card1 = "**DECISION**\n**ASK:** deploy the thing now?\n**WHY:** x"
        card2 = ("**DECISION**\n**ASK:** rotate every credential on prod\n"
                 "**WHY:** y")
        await _call(server, "decision_put", {
            "from_agent": "alice", "card": card1,
        })
        await _call(server, "decision_put", {
            "from_agent": "alice", "card": card2,
        })
        g = await _lineage(server, "hub.decision/1?card=2")
        assert any(
            e["predicate"] == "supersedes"
            and e["object"] == "hub.decision/1?card=1"
            for e in g["edges"]
        )
        await _call(server, "decision_resolve", {
            "from_agent": "alice", "verdict": "approved",
        })
        g = await _lineage(server, "hub.decision/1?card=2", direction="in")
        assert any(
            e["predicate"] == "resolves"
            and e["subject"] == "hub.agent/1?name=alice"
            for e in g["edges"]
        )


class TestDeclaredReplies:
    @pytest.mark.anyio
    async def test_A6_a_ref_copied_from_the_rendered_surface_works(
        self, server
    ):
        """🔴 THE adoption test. An agent can only reply to a ref it has
        SEEN — so this test copies the ref out of the rendered get_messages
        text (never out of the database) and uses it. If the surfaces stop
        printing refs, this fails; if the printed form stops parsing, this
        fails. Either failure means the declared path is unusable and the
        graph goes sparse BY CONSTRUCTION.

        Mutation: drop the ⟨ref⟩ from the get_messages render -> fails.
        """
        await _setup(server, "alice", "bob")
        await _call(server, "send", {
            "from_agent": "alice", "to": "bob", "message": "question?",
        })
        rendered = await _call(server, "get_messages", {"agent_name": "bob"})
        m = REF_RE.search(rendered)
        assert m, f"no ⟨ref⟩ in the rendered surface: {rendered!r}"
        copied_ref = m.group(1)

        await _call(server, "send", {
            "from_agent": "bob", "to": "alice", "message": "answer.",
            "in_reply_to": copied_ref,
        })
        g = await _lineage(server, copied_ref, direction="in")
        replies = [e for e in g["edges"] if e["predicate"] == "replies-to"]
        assert replies and replies[0]["source"] == "declared"

    @pytest.mark.anyio
    async def test_a_malformed_reply_ref_refuses_the_send_LOUDLY(self, server):
        """A silently-dropped edge is a lineage record that lies by
        omission — so a bad ref refuses the whole send, and nothing lands."""
        await _setup(server, "alice", "bob")
        out = await _call(server, "send", {
            "from_agent": "alice", "to": "bob", "message": "x",
            "in_reply_to": "not-a-ref",
        })
        assert "in_reply_to refused" in out
        inbox = await _call(server, "get_messages", {"agent_name": "bob"})
        assert "x" not in inbox, "the refused send was stored anyway"

    @pytest.mark.anyio
    async def test_a_reply_to_a_message_that_never_existed_is_refused(
        self, server
    ):
        """An invented parent is an invented fact."""
        await _setup(server, "alice", "bob")
        out = await _call(server, "send", {
            "from_agent": "alice", "to": "bob", "message": "x",
            "in_reply_to": "hub.msg/1?id=99999",
        })
        assert "never existed" in out

    @pytest.mark.anyio
    async def test_a_reply_ref_of_the_wrong_scheme_is_refused(self, server):
        await _setup(server, "alice", "bob")
        out = await _call(server, "send", {
            "from_agent": "alice", "to": "bob", "message": "x",
            "in_reply_to": "hub.agent/1?name=bob",
        })
        assert "replies target messages" in out


class TestNeverInfer:
    @pytest.mark.anyio
    async def test_A10_consecutive_DMs_produce_NO_parent_edge(self, server):
        """🔴 The load-bearing refusal. bob answers alice's DM immediately —
        the exact state a 'probably answers the previous message' heuristic
        would edge — and NO reply edge may exist, because the hub cannot know
        and a guessed causal edge is a mirror of a plausible story.

        Mutation: infer parentage from the previous counterparty message ->
        this fails.
        """
        await _setup(server, "alice", "bob")
        await _call(server, "send", {
            "from_agent": "alice", "to": "bob", "message": "question?",
        })
        await _call(server, "send", {
            "from_agent": "bob", "to": "alice", "message": "an answer",
        })
        for mid in (1, 2):
            g = await _lineage(server, f"hub.msg/1?id={mid}")
            assert not any(
                e["predicate"] == "replies-to" for e in g["edges"]
            ), "an inferred reply edge exists — the hub guessed"


class TestSurfacesAndTool:
    @pytest.mark.anyio
    async def test_refs_render_in_history_and_broadcasts(self, server):
        await _call(server, "register", {
            "name": "alice", "project": "p", "squads": "spike",
        })
        await _call(server, "broadcast", {
            "from_agent": "alice", "message": "news", "priority": "low",
        })
        hist = await _call(server, "get_history",
                           {"agent_or_channel": "#general"})
        assert REF_RE.search(hist), hist
        feed = await _call(server, "get_broadcasts", {})
        assert REF_RE.search(feed), feed

    @pytest.mark.anyio
    async def test_an_edgeless_ref_reads_lineage_blind(self, server):
        """A3/A7 through the tool: nothing recorded ≠ a root."""
        g = await _lineage(server, "hub.msg/1?id=424242")
        assert g["lineage_blind"] is True

    @pytest.mark.anyio
    async def test_the_tool_refuses_an_unknown_scheme_naming_the_registered(
        self, server
    ):
        out = await _call(server, "get_lineage", {"ref": "no.such/1?id=1"})
        assert out.startswith("REFUSED:") and "hub.msg/1" in out

    @pytest.mark.anyio
    async def test_lineage_failure_never_breaks_delivery(self, server):
        """The fail-soft contract: the message is already committed when
        edges are written, so a lineage fault costs the graph an edge and
        the recipient NOTHING."""
        await _setup(server, "alice", "bob")
        with patch("mcp_hub.server.lineage.write_edge",
                   side_effect=RuntimeError("edge store on fire")):
            out = await _call(server, "send", {
                "from_agent": "alice", "to": "bob", "message": "still lands",
            })
        assert "refused" not in out.lower()
        assert "still lands" in await _call(
            server, "get_messages", {"agent_name": "bob"},
        )
