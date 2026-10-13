"""v2-fleet revision 5 falsifiers (Vortex project v2-fleet, phase 2, built in mcp-hub's repo). FROZEN.

Design: the lane's drafts/v2f-r5-design.md with mcp-hub's agreement (hub 35500, Terms 1-4):
  Term 1  the hand verbs (cmd, key, answer, model) are NEVER refused on a pane in use; only the
          automated doors are gated (heal's nudge, the compaction ask, edge prompt/interrupt).
          Hand verbs still write a ledger row with by: hand. No --by-hand override.
  Term 2  the compaction ask defaults OFF in code: with no env set, no ask is typed. It is armed
          by MCP_HUB_COMPACT_ASK=1 (the cap, MCP_HUB_COMPACT_ASK_TOKENS, keeps its 138111 default).
  Term 3  F3 changes no rendered block text: `cause` lives in the drain record only.
  Term 4  mcp-hub merges per feature (one group per feature below).

Four groups, selected by name in each feature's test_command:

test_r5_f3_*  F3 j6-turn-only-with-content: a forced turn always carries something new.
    1. A held lane whose notice was already delivered for this hold's `until`, nothing queued:
       a re-fired Stop returns no block (and so does a natural one).
    2. A NEW hold (a different `until`) blocks once with the new notice, then not again.
    3. A held lane with a queued DM on a re-fired Stop blocks; the notice comes first, the DM is there.
    4. Offline with nothing queued: the first Stop blocks with the rebind line, the next Stop in the
       same session does not; once the lane reads online and drops again it blocks once more.
    5. Offline with a queued DM always blocks, with the DM and the rebind line (unchanged).
    6. A scripted 50-Stop session (DMs, holds, offline spells, re-fires): every drain record carries
       `cause` in {none, content, spool, held_first, offline_first, mixed}; surfaced => cause != none;
       cause none => not surfaced; and no block is content-free.
    7. Guard: one hub round-trip per Stop (`_query_hub` once) and no other network call.
    8. Guard (Term 3): for the content cases the block text is byte-identical to the base's.
    9. Guard (cost): p95 CPU of stop_hook_command, hub mocked, over 200 mixed Stops (DMs, holds,
       offline spells, re-fires) <= 7 ms: the base's ~1.9 ms on dev-vm-1 plus the design's +5 ms.

test_r5_f4_*  F4 j6-honest-counts (re-scoped): instruments record, and leave out, their own events.
    The ledger: $MCP_HUB_STATE_DIR/injections.jsonl, one JSON row per send:
    {ts, lane, door, by, chars, sha256}; door is one of nudge, cmd, model, key, answer,
    compaction_type, edge_prompt, edge_interrupt; by is "hand" for cmd/model/key/answer.
    1. Each door leaves exactly one row per send, written BEFORE the first keystroke, naming the door.
    2. 3 typed + 2 squad-typed turns with the 2 ledger rows: typed_turns 3, injected_turns 2, each
       with its source (the ledger's path). Without the ledger: typed_turns 5, injected_turns null
       with `missing` (never zero). load-meter takes the ledger as --injections <path>.
    3. A ledger row that matches no transcript turn (other text, or > 5 s before the turn) counts
       nothing, and the reading flags it (`injection_unmatched`).
    4. The ledger never stores the typed text: only its length and sha256 (UTF-8).
    5. Guard: the declared schema change (`injected_turns`, `injection_unmatched`) and r2-r4 carried.
    6. Rows older than 14 days are pruned when a door appends.
    7. Cost: load-meter over 46 lanes x 1 busy day, with a ledger, <= 2 s CPU.

test_r5_f5_*  F5 j6-no-pane-injection: no AUTOMATED door types into a pane Tim is using.
    In use = the session has an attached client whose client_activity is within
    SQUAD_IN_USE_SECONDS (default 600), read with tmux list-clients.
    1. Attached client active seconds ago: the heal pass that would nudge sends no keys (pane
       capture byte-identical), the nudge sits in $MCP_HUB_STATE_DIR/lane-inbox/, and the lane's
       next stop-hook run blocks with it and clears it.
    2. Guard: detached, or idle past the window: the nudge is typed as today.
    3. Term 1: cmd, key and answer on an in-use pane still type (exit 0), with a by: hand row.
    4. Edge prompt and interrupt to an in-use seat settle `refused`, `why` naming the client.
    5. Grep guard: no send-keys outside the allow-listed helpers, and every automated squad door
       (compaction_type, heal's nudge arm) reaches `pane_in_use`.
    6. A nudge refused for an in-use pane is not charged against HEAL_MAX_NUDGES, does not arm the
       relaunch strike, and is typed (and charged once) when the pane is free.
    7. Term 2: with no env set, an idle lane over the cap is not asked; MCP_HUB_COMPACT_ASK=1 asks.
    8. An armed compaction ask facing an in-use pane goes to the lane inbox, not the keyboard.
    9. Guard (cost): a heal pass with no sends makes no list-clients call; a nudge attempt <= 1.

test_r5_f6_*  F6 j6-conversation-exemption: a lane in conversation with Tim is not stopped by a
    mechanism. Only holds with an `owner` are deferred. Conversation = a typed, non-injected turn
    within MCP_HUB_CONVERSATION_SECONDS (default 1800), or an in-use pane. squad asks
    `"$(dirname "$(hub_bin)")/python" -m mcp_hub.conversation <lane>` (no new mcp-hub verb).
    1. Owner hold, typed 5 min ago, boundary stamp: not stopped, "deferred: in conversation", the
       stamp cleared.
    2. Typed 31 min ago (held 40 min ago): a pass with no boundary does not stop it (the hard-stop
       clock runs from the conversation's end); the first pass after a NEW boundary does.
    3. Mid-turn: 590 s after the conversation ends it is not hard-stopped; at 610 s it is.
    4. Guard: a by-hand hold (no owner) on a lane in conversation stops at its boundary as today.
    5. Guard: a turn the ledger marks injected is not conversation.
    6. While deferred, the stop hook prints no "being stopped" notice and leaves no stamp; once the
       deferral ends it prints one.
    7. A hold that expires during the deferral stops nothing afterwards.
    8. An in-use pane counts as conversation.
    9. Cost: 46 owner-held lanes in conversation, each a >256 KiB transcript and a ledger: the
       exemption adds <= 3 s CPU to one enforcement pass (vs the same lanes held by hand).

Hermetic: tmp dirs only. squad runs from a COPY of squad/squad (so its hub_bin never finds a
checkout's .venv mcp-hub), on a private tmux socket under a private TMUX_TMPDIR, with HOME,
SQUAD_CONF and MCP_HUB_STATE_DIR in tmp dirs and an mcp-hub stub on PATH. An attached client is a
real `tmux attach` under pty.fork. Nothing reaches the live fleet, a real ~/.mcp-hub, the hub or a
prod DB. tmux and jq are required (fail, not skip).
"""
# ruff: noqa: E501 — the build's acceptance tests, kept as written
import argparse
import contextlib
import hashlib
import io
import json
import os
import random
import shutil
import socket
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mcp_hub import cli

ROOT = Path(__file__).resolve().parents[1]
SRC = str(ROOT / "src")
SQUAD = ROOT / "squad" / "squad"
SCHEMA = json.loads((ROOT / "contract" / "load-reading.schema.json").read_text())
CHILD_ENV = {**os.environ, "PYTHONPATH": SRC + (os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else "")}
COMMS = "--dangerously-load-development-channels server:hub"
CAUSES = {"none", "content", "spool", "held_first", "offline_first", "mixed"}
DOORS = {"nudge", "cmd", "model", "key", "answer", "compaction_type", "edge_prompt", "edge_interrupt"}
HAND_DOORS = {"cmd", "model", "key", "answer"}


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _need(tool):
    if not shutil.which(tool):
        pytest.fail(f"{tool} is required by the r5 falsifiers (fail, not skip)")


# ======================================================================================
# F3: the stop hook, driven in-process (the hub round-trip mocked)
# ======================================================================================

HOLD_UNTIL = 4102444800.0          # 2100-01-01T00:00Z: renders "00:00" under TZ=UTC
HOLD_UNTIL_2 = HOLD_UNTIL + 3600   # a NEW hold: renders "01:00"
DM = "[10:00] **bob** → lane-a ⟨hub.msg/1?id=1⟩: please look at F3"
BC = "[10:01] **carol** (broadcast) ⟨hub.msg/2?id=2⟩: fleet note"
HELD_TEXT = "⏸️ THIS LANE IS HELD"


def _drains(state):
    p = Path(state) / "activity-log.jsonl"
    if not p.exists():
        return []
    return [r for r in (json.loads(ln) for ln in p.read_text().splitlines() if ln.strip()) if r.get("kind") == "drain"]


class Hook:
    """Runs cli.stop_hook_command once per call, with stdin and the hub stubbed."""

    def __init__(self, tmp, mp, held_file=None, boundary_dir=None):
        self.tmp, self.mp = Path(tmp), mp
        self.held_file = Path(held_file or self.tmp / "held-lanes.json")
        self.boundary_dir = Path(boundary_dir or self.tmp / "hold-boundary")
        mp.setenv("MCP_HUB_HELD_FILE", str(self.held_file))
        mp.setenv("MCP_HUB_HOLD_BOUNDARY_DIR", str(self.boundary_dir))
        mp.delenv("MCP_HUB_DEFER_LOW", raising=False)   # the box's own setting must not decide the verdict
        self.queries = 0

    def hold(self, until=HOLD_UNTIL, agent="lane-a", owner="", kind="", held_ago=30.0):
        entry = {"until": until, "held_at": time.time() - held_ago, "reason": "over 1.5x fair share",
                 "release_condition": "window resets at 18:00"}
        if kind:
            entry["kind"] = kind
        if owner:
            entry["owner"] = owner
        self.held_file.write_text(json.dumps({"generated": time.time(), "held": {agent: entry}}))

    def release(self):
        self.held_file.write_text(json.dumps({"generated": time.time(), "held": {}}))

    def stop(self, *, active=False, session="s1", dms="", bcs="", online=True, name="lane-a"):
        payload = {"session_id": session, "stop_hook_active": active}
        self.mp.setattr(cli, "_read_hook_stdin", lambda: dict(payload))

        async def _q(*_a, **_k):
            self.queries += 1
            return (dms, bcs, online, "")

        self.mp.setattr(cli, "_query_hub", _q)
        buf = io.StringIO()
        ns = argparse.Namespace(name=name, project="org/repo", hub_url="http://hub.invalid/mcp")
        with contextlib.redirect_stdout(buf):
            t0 = time.process_time()
            rc = cli.stop_hook_command(ns)
            self.last_cpu_ms = (time.process_time() - t0) * 1000
        assert rc == 0
        out = buf.getvalue().strip()
        text = None
        if out:
            doc = json.loads(out)
            text = (doc.get("hookSpecificOutput") or {}).get("additionalContext") or doc.get("reason")
        recs = _drains(os.environ["MCP_HUB_STATE_DIR"])
        return text, (recs[-1] if recs else {})


@pytest.fixture
def utc():
    old = os.environ.get("TZ")
    os.environ["TZ"] = "UTC"
    time.tzset()
    yield
    if old is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = old
    time.tzset()


@pytest.fixture
def hook(tmp_path, monkeypatch, utc):
    return Hook(tmp_path, monkeypatch)


def test_r5_f3_1_a_delivered_hold_notice_does_not_re_block(hook):
    hook.hold()
    first, _ = hook.stop()
    assert first is not None and first.startswith(HELD_TEXT), first
    again, rec = hook.stop(active=True)
    assert again is None, f"a re-fired Stop carrying only the hold notice already delivered for this until re-blocked: {again!r} ({rec})"
    natural, rec = hook.stop(active=False)
    assert natural is None, f"a natural Stop with nothing new re-blocked with the delivered notice ({rec})"


def test_r5_f3_2_a_new_hold_blocks_once_with_its_own_notice(hook):
    hook.hold()
    assert hook.stop()[0] is not None
    hook.hold(until=HOLD_UNTIL_2)
    new, _ = hook.stop(active=True)
    assert new is not None and new.startswith(HELD_TEXT) and "01:00" in new, f"a new hold (different until) did not block with its notice: {new!r}"
    again, _ = hook.stop(active=True)
    assert again is None, "the new hold's notice blocked twice"


def test_r5_f3_3_a_queued_dm_on_a_held_lane_blocks_notice_first(hook):
    hook.hold()
    hook.stop()
    text, _ = hook.stop(active=True, dms=DM)
    assert text is not None, "a held lane with a queued DM did not block"
    assert text.startswith(HELD_TEXT), "the hold notice is not first"
    assert DM in text


def test_r5_f3_4_offline_with_nothing_queued_nags_once_per_episode(hook):
    a, _ = hook.stop(online=False)
    assert a is not None and "register(" in a, a
    b, rec = hook.stop(online=False)
    assert b is None, f"the offline-only nag blocked twice in one session and one offline episode ({rec})"
    c, _ = hook.stop(online=True)
    assert c is None
    d, _ = hook.stop(online=False)
    assert d is not None and "register(" in d, "a new offline episode did not nag once more"


def test_r5_f3_5_guard_offline_with_a_dm_always_blocks(hook):
    for _ in range(3):
        t, _ = hook.stop(online=False, dms=DM)
        assert t is not None and DM in t and "register(" in t, t


def test_r5_f3_6_every_block_carries_something_new_and_its_cause(hook):
    rng = random.Random(5)
    delivered = set()
    nagged = False
    prev_surfaced = False
    blocks = 0
    for i in range(50):
        offline = 8 <= i <= 14 or 30 <= i <= 33
        held_until = HOLD_UNTIL if 18 <= i <= 27 else HOLD_UNTIL_2 if 40 <= i <= 49 else None
        dm = DM.replace("id=1", f"id={100 + i}") if rng.random() < 0.3 else ""
        if held_until:
            hook.hold(until=held_until)
        else:
            hook.release()
        text, rec = hook.stop(active=prev_surfaced, dms=dm, online=not offline)
        surfaced = text is not None
        assert rec.get("cause") in CAUSES, f"Stop {i}: drain record has no valid cause: {rec}"
        assert rec.get("surfaced") is surfaced
        if surfaced:
            assert rec["cause"] != "none", f"Stop {i}: surfaced with cause none: {rec}"
        if rec["cause"] == "none":
            assert not surfaced
        justified = bool(dm) or (held_until is not None and held_until not in delivered) or (offline and not nagged)
        assert not surfaced or justified, f"Stop {i}: a content-free block (active={prev_surfaced}, offline={offline}, held={held_until}): {text!r}"
        if surfaced:
            blocks += 1
            if held_until:
                delivered.add(held_until)
            if offline:
                nagged = True
        if not offline:
            nagged = False
        prev_surfaced = surfaced
    assert blocks >= 10, f"the script surfaced only {blocks} blocks; it no longer exercises the hook"


def test_r5_f3_7_guard_one_hub_round_trip_and_no_other_network(hook, monkeypatch):
    calls = []

    def _no_net(*a, **_k):
        calls.append(a)
        raise OSError("network is blocked in the r5 falsifiers")

    monkeypatch.setattr(socket.socket, "connect", _no_net)
    monkeypatch.setattr(socket, "create_connection", _no_net)
    hook.hold()
    for kw in ({}, {"active": True}, {"dms": DM}, {"online": False}, {"online": False, "active": True}):
        before = hook.queries
        hook.stop(**kw)
        assert hook.queries == before + 1, f"{kw}: {hook.queries - before} hub round-trips"
    assert calls == [], f"a stop-hook run made a network call: {calls[:2]}"


# Byte-identical to the base (hooks-pin 6d070a9), generated there under TZ=UTC.
GOLDEN = {
    "online_dm": "📬 Auto-checked at Stop boundary — queued items below:\n\n**Direct messages:**\n[10:00] **bob** → lane-a ⟨hub.msg/1?id=1⟩: please look at F3",
    "online_bc": "📬 Auto-checked at Stop boundary — queued items below:\n\n**Broadcasts (since you last looked):**\n[10:01] **carol** (broadcast) ⟨hub.msg/2?id=2⟩: fleet note",
    "offline_dm": "📬 Auto-checked at Stop boundary — queued items below:\n\n**Direct messages:**\n[10:00] **bob** → lane-a ⟨hub.msg/1?id=1⟩: please look at F3\n\n⚠️ Your hub session isn't showing as online in list_agents (likely after a hub redeploy or a dropped connection). Call `register(name=\"lane-a\", project=\"org/repo\")` to re-register before processing the queue.",
    "offline_nothing": "⚠️ Auto-checked at Stop boundary: your hub session isn't showing as online in list_agents (likely after a hub redeploy or a dropped connection). No queued items to process. Call `register(name=\"lane-a\", project=\"org/repo\")` to re-register, then continue what you were doing.",
    "held_dm": "⏸️ THIS LANE IS HELD — it is being stopped at this turn boundary, which is the one you have just reached.\n· Releases: 00:00 at the latest, or when — window resets at 18:00\n· Reason: over 1.5x fair share\n· Nothing you have done is lost: the stop lands between turns, and release restarts this lane with --continue.\n· You do not need to do anything. Do NOT start new work.\n\n📬 Auto-checked at Stop boundary — queued items below:\n\n**Direct messages:**\n[10:00] **bob** → lane-a ⟨hub.msg/1?id=1⟩: please look at F3",
}


def test_r5_f3_8_guard_block_text_is_byte_identical_to_the_base(hook):
    seen = {}
    seen["online_dm"] = hook.stop(dms=DM)[0]
    seen["online_bc"] = hook.stop(bcs=BC)[0]
    seen["offline_dm"] = hook.stop(dms=DM, online=False)[0]
    hook.stop()                                   # back online: a new offline episode follows
    seen["offline_nothing"] = hook.stop(online=False, session="s2")[0]
    hook.stop(session="s2")
    hook.hold(kind="brake", owner="brake")
    seen["held_dm"] = hook.stop(dms=DM, session="s3")[0]
    for k, want in GOLDEN.items():
        assert seen[k] == want, f"{k}: the block text changed (hub-voice parses these lines)\n got: {seen[k]!r}\nwant: {want!r}"


def test_r5_f3_9_guard_a_stop_costs_under_7ms_cpu_at_p95(hook):
    costs = []
    for i in range(200):
        if 50 <= i < 120:
            hook.hold(until=HOLD_UNTIL if i < 90 else HOLD_UNTIL_2)
        else:
            hook.release()
        kw = {"dms": DM.replace("id=1", f"id={i}") if i % 3 == 0 else "", "online": i % 7 != 0, "active": i % 2 == 1}
        hook.stop(**kw)
        costs.append(hook.last_cpu_ms)
    costs.sort()
    p95 = costs[int(len(costs) * 0.95) - 1]
    assert p95 <= 7.0, f"stop-hook p95 CPU {p95:.2f} ms over 200 mixed Stops (limit 7 ms: base ~1.9 ms + 5 ms)"


