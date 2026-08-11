"""W2.4 — shadow-mode diagnostic for the Stop hook's compact rendering.

The hub's "already delivered live" line is an INFERENCE
(`pushed_gen == gen_now and not wake_render_unproven`). Nothing has ever
checked it against what the agent's context actually received. Shadow mode
observes the other side from the transcript and records disagreements.

The bar (docs/verification/wave-2.md, W2.4):
  D1 ZERO behaviour change — rendering byte-identical with shadow on.
  D2 a synthetic disagreement is captured; the log caps.
  D3 no hub write, no new network call.

Record shapes below are FAITHFUL — measured from this machine's transcripts
on 2026-08-11, where a `<channel …>` tag appears in three record types:
`queue-operation` (8), `user` (6), `attachment` (1). Only `user` is a render;
the others prove the notification reached the client, which is the very thing
in question.
"""

from __future__ import annotations

import argparse
import json
import socket
from pathlib import Path
from unittest.mock import patch

from mcp_hub import shadow
from mcp_hub.cli import stop_hook_command

BODY = "Sync report + handback ack. Pre-pull state was NOT 2 weeks stale."


def _render_record(agent: str, body: str) -> str:
    """A `type: "user"` record — the channel push as it actually reached the
    conversation. Keys mirror a measured record."""
    return json.dumps({
        "type": "user",
        "parentUuid": "p", "isSidechain": False, "sessionId": "s",
        "cwd": "/repo", "gitBranch": "master", "promptSource": "channel",
        "message": {
            "role": "user",
            "content": (
                f'<channel source="hub" from_agent="{agent}" kind="dm" '
                f'priority="low" drain_batch="false">\n'
                f"[15:39:03] DM from {agent} [low]: {body}"
            ),
        },
    })


def _queue_record(agent: str, body: str) -> str:
    """A `type: "queue-operation"` record — the notification was ENQUEUED.
    Carries identical content and is not evidence the agent saw anything."""
    return json.dumps({
        "type": "queue-operation",
        "operation": "enqueue",
        "timestamp": "2026-08-11T15:39:03.808Z",
        "sessionId": "s",
        "content": (
            f'<channel source="hub" from_agent="{agent}" kind="dm" '
            f'priority="low" drain_batch="false">\n'
            f"[15:39:03] DM from {agent} [low]: {body}"
        ),
    })


def _assistant_quoting_record(agent: str, body: str) -> str:
    """A `type: "assistant"` record that QUOTES a channel tag — the agent
    writing about a message rather than receiving one. Common in this repo,
    whose transcripts are full of its own source and pasted tags."""
    return json.dumps({
        "type": "assistant",
        "parentUuid": "p", "isSidechain": False, "sessionId": "s",
        "message": {
            "role": "assistant",
            "content": [{
                "type": "text",
                "text": (
                    "Looking at what arrived: "
                    f'<channel source="hub" from_agent="{agent}" kind="dm" '
                    f'priority="low">\n[15:39:03] DM from {agent} [low]: {body}'
                ),
            }],
        },
    })


def _transcript(tmp_path: Path, *records: str) -> str:
    tmp_path.mkdir(parents=True, exist_ok=True)
    p = tmp_path / "transcript.jsonl"
    p.write_text("\n".join(records) + "\n", encoding="utf-8")
    return str(p)


def _claimed_live(agent: str, handle: str) -> str:
    """The hub's compact render for a message it believes surfaced live."""
    return f"[15:39:03] **{agent}** [low]: (already delivered live — {handle})"


# ---------------------------------------------------------------------------
# The parse halves
# ---------------------------------------------------------------------------


class TestParsing:
    def test_a_live_claim_and_a_full_body_are_told_apart(self):
        text = "\n".join([
            _claimed_live("bob", BODY),
            f"[15:40:00] **carol**: {BODY}",
        ])
        parsed = shadow.parse_rendered_messages(text)
        assert [p["claimed_live"] for p in parsed] == [True, False]
        assert [p["agent"] for p in parsed] == ["bob", "carol"]

    def test_the_footer_is_not_mistaken_for_a_message(self):
        """The compact footer and the gap notice are not `**name**:` lines;
        counting them as messages would invent disagreements out of chrome."""
        text = "\n".join([
            _claimed_live("bob", BODY),
            "(1 already surfaced live — shortened to save context, and now "
            "marked read. Full text: get_history('alice'))",
        ])
        assert len(shadow.parse_rendered_messages(text)) == 1

    def test_a_user_record_counts_as_a_render(self, tmp_path):
        path = _transcript(tmp_path, _render_record("bob", BODY))
        observed = shadow.observed_renders(path)
        assert len(observed) == 1
        assert observed[0]["agent"] == "bob"

    def test_queue_plumbing_is_NOT_evidence_of_a_render(self, tmp_path):
        """🔴 The load-bearing distinction. A `queue-operation` record means
        the notification arrived at the client, NOT that it rendered. Counting
        it would make a queued-but-never-seen message look delivered — exactly
        the false-compaction case shadow mode exists to catch, so the
        instrument would be blind to its own subject.

        Rejected TWICE over — by the type check and, independently, by the
        message-SHAPE check (these records carry `content` at top level, never
        `message.content`). So no single mutation kills this test, and it is
        recorded that way in the ledger rather than credited to either check.
        The assistant-quote test below is what pins the type check alone.

        Mutation: drop the type check AND accept a top-level `content` key
        (both together) -> this fails.
        """
        path = _transcript(tmp_path, _queue_record("bob", BODY))
        assert shadow.observed_renders(path) == []

    def test_an_ASSISTANT_quoting_a_channel_tag_is_not_a_render(self, tmp_path):
        """🔴 What the type check actually buys. An agent that writes ABOUT a
        message — quoting the tag, pasting the push code — produces a record
        with a real `message.content` carrying `<channel …>`. Counting it
        would let the agent's own words vouch for delivery it never got.

        Not hypothetical: the first hand-grep during this build matched this
        repo's own `push_channel` source instead of any live push.

        Mutation: drop the `rec.get("type") != "user"` check -> this fails.
        """
        path = _transcript(tmp_path, _assistant_quoting_record("bob", BODY))
        assert shadow.observed_renders(path) == []

    def test_positive_control_the_scan_CAN_see_a_render(self, tmp_path):
        """Both negatives above are only worth reading if the scanner can
        find a real render in a file that also contains the decoys."""
        path = _transcript(
            tmp_path,
            _queue_record("bob", BODY),
            _assistant_quoting_record("bob", BODY),
            _render_record("bob", BODY),
        )
        assert len(shadow.observed_renders(path)) == 1


# ---------------------------------------------------------------------------
# D2 — disagreements are captured
# ---------------------------------------------------------------------------


class TestDisagreements:
    def test_positive_control_agreement_records_NOTHING(self, tmp_path):
        """Before trusting any capture below: a claim BACKED by a render must
        produce no entry. If this fails, every capture is an instrument
        artefact rather than a finding."""
        path = _transcript(tmp_path, _render_record("bob", BODY))
        log = tmp_path / "shadow.jsonl"
        entries = shadow.run_shadow("alice", _claimed_live("bob", BODY), path,
                                    path=log)
        assert entries == []
        assert not log.exists()

    def test_a_live_claim_with_no_render_is_captured(self, tmp_path):
        """FALSE COMPACTION — the harmful direction: the body was reduced to
        one line AND marked read, so the full text now lives only in history.

        Mutation: drop the `false_compaction` branch in compare() -> fails.
        """
        path = _transcript(tmp_path, _queue_record("bob", BODY))
        log = tmp_path / "shadow.jsonl"
        entries = shadow.run_shadow("alice", _claimed_live("bob", BODY), path,
                                    path=log)
        assert [e["kind"] for e in entries] == ["false_compaction"]
        assert entries[0]["agent"] == "bob"
        assert entries[0]["agent_name"] == "alice"
        assert json.loads(log.read_text().splitlines()[0])["kind"] == (
            "false_compaction"
        )

    def test_a_full_reprint_of_a_rendered_message_is_captured(self, tmp_path):
        """DOUBLE SURFACE — the context tax. Predicted by the parked
        2026-08-09 investigation (a resume mints a new binding generation).

        Mutation: drop the `double_surface` branch -> this fails.
        """
        path = _transcript(tmp_path, _render_record("bob", BODY))
        entries = shadow.run_shadow(
            "alice", f"[15:39:03] **bob** [low]: {BODY}", path,
            path=tmp_path / "shadow.jsonl",
        )
        assert [e["kind"] for e in entries] == ["double_surface"]

    def test_a_handle_too_short_to_match_abstains(self, tmp_path):
        """A two-word handle cannot distinguish a match from a coincidence.
        The diagnostic records `unmatchable` rather than inventing a verdict —
        a shadow mode that guesses would poison the log it exists to fill."""
        path = _transcript(tmp_path, _render_record("bob", BODY))
        entries = shadow.run_shadow("alice", _claimed_live("bob", "ok"), path,
                                    path=tmp_path / "shadow.jsonl")
        assert [e["kind"] for e in entries] == ["unmatchable"]

    def test_the_log_caps(self, tmp_path):
        """Runs at every Stop of every agent on the box; unbounded it is a
        slow disk leak nobody watches.

        Mutation: remove the trim in record() -> this fails.
        """
        log = tmp_path / "shadow.jsonl"
        for i in range(40):
            shadow.record([{"kind": "false_compaction", "n": i}], log, cap=10)
        lines = log.read_text().splitlines()
        assert len(lines) == 10
        # the SURVIVORS are the most recent, not the oldest
        assert json.loads(lines[-1])["n"] == 39


# ---------------------------------------------------------------------------
# D3 — no hub write, no new network call
# ---------------------------------------------------------------------------


class TestNoNetwork:
    def test_the_diagnostic_runs_with_the_network_POISONED(self, tmp_path):
        """Enforcement, not inspection: every socket constructor raises, so a
        hub call added inside run_shadow would throw — and run_shadow swallows
        everything, so the failure surfaces as an EMPTY result rather than an
        error. Asserting the disagreement still lands is what makes this a
        network test rather than a comment.

        Mutation: add any hub round-trip to run_shadow -> this fails.
        """
        path = _transcript(tmp_path, _queue_record("bob", BODY))
        log = tmp_path / "shadow.jsonl"

        def _no_network(*_a, **_k):
            raise AssertionError("shadow mode must make no network call")

        with patch.object(socket, "socket", _no_network), \
             patch.object(socket, "create_connection", _no_network):
            entries = shadow.run_shadow("alice", _claimed_live("bob", BODY),
                                        path, path=log)

        assert [e["kind"] for e in entries] == ["false_compaction"]


# ---------------------------------------------------------------------------
# D1 — zero behaviour change
# ---------------------------------------------------------------------------


class TestZeroBehaviourChange:
    """The hook's one job is surfacing messages. A diagnostic riding along
    must not change a byte of what lands in the agent's context."""

    @staticmethod
    def _run(capsys, tmp_path, *, shadow_on: bool) -> str:
        args = argparse.Namespace(
            name="alice", project="proj", hub_url="http://x/mcp"
        )
        messages = "\n".join([
            _claimed_live("bob", BODY),
            f"[15:41:00] **carol**: {BODY}",
        ])

        async def _fake_query(*_a, **_k):
            return (messages, "", True, "")

        transcript = _transcript(tmp_path, _queue_record("bob", BODY))
        stack = [
            patch("mcp_hub.cli._query_hub", side_effect=_fake_query),
            patch("mcp_hub.cli._read_hook_stdin",
                  return_value={"transcript_path": transcript}),
            patch.object(shadow, "shadow_log_path",
                         return_value=tmp_path / "shadow.jsonl"),
        ]
        if not shadow_on:
            stack.append(patch.object(shadow, "run_shadow", return_value=[]))
        for p in stack:
            p.start()
        try:
            assert stop_hook_command(args) == 0
        finally:
            for p in reversed(stack):
                p.stop()
        return capsys.readouterr().out

    def test_rendering_is_byte_identical_with_shadow_on(self, capsys, tmp_path):
        """Mutation: let run_shadow's return value reach the response (or drop
        its try/except so a bad transcript propagates) -> this fails."""
        off = self._run(capsys, tmp_path / "a", shadow_on=False)
        on = self._run(capsys, tmp_path / "b", shadow_on=True)
        assert on == off
        assert json.loads(on)["decision"] == "block"  # control: it DID render

    def test_a_RAISING_shadow_cannot_break_the_hook(self, capsys, tmp_path):
        """🔴 Fail-open, provoked rather than assumed. The corrupt-transcript
        case below never actually raises (observed_renders already catches
        OSError and ValueError), so it does NOT exercise run_shadow's
        try/except — a mutation removing that guard survived it. Injecting a
        real fault is what pins the contract.

        Mutation: remove run_shadow's try/except -> this fails.
        """
        args = argparse.Namespace(
            name="alice", project="proj", hub_url="http://x/mcp"
        )

        async def _fake_query(*_a, **_k):
            return (_claimed_live("bob", BODY), "", True, "")

        def _boom(*_a, **_k):
            raise RuntimeError("shadow exploded")

        with patch("mcp_hub.cli._query_hub", side_effect=_fake_query), \
             patch("mcp_hub.cli._read_hook_stdin",
                   return_value={"transcript_path": None}), \
             patch.object(shadow, "parse_rendered_messages", _boom):
            assert stop_hook_command(args) == 0

        # the messages still reached the agent — the diagnostic died alone
        out = capsys.readouterr().out
        assert json.loads(out)["decision"] == "block"
        assert BODY[:20] in json.loads(out)["reason"]

    def test_a_corrupt_transcript_is_survivable(self, capsys, tmp_path):
        """Robustness of the reader itself (binary junk, truncated records).
        Deliberately NOT claimed as the try/except test — see above."""
        args = argparse.Namespace(
            name="alice", project="proj", hub_url="http://x/mcp"
        )

        async def _fake_query(*_a, **_k):
            return (_claimed_live("bob", BODY), "", True, "")

        bad = tmp_path / "corrupt.jsonl"
        bad.write_bytes(b"\x00\xff not json at all\n<channel")

        with patch("mcp_hub.cli._query_hub", side_effect=_fake_query), \
             patch("mcp_hub.cli._read_hook_stdin",
                   return_value={"transcript_path": str(bad)}), \
             patch.object(shadow, "shadow_log_path",
                          return_value=tmp_path / "shadow.jsonl"):
            assert stop_hook_command(args) == 0

        assert json.loads(capsys.readouterr().out)["decision"] == "block"

    def test_a_missing_transcript_path_is_survivable(self, tmp_path):
        """The hook payload does not always carry `transcript_path`."""
        assert shadow.observed_renders(None) == []
        assert shadow.run_shadow("alice", _claimed_live("bob", BODY), None,
                                 path=tmp_path / "s.jsonl")
