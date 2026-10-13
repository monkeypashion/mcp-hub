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
    live_a-d + live_guard (FIX PASS): the same, on the LIVE layout (ledger names agents and tmux
       sessions, the meter keys transcript folders); mapping and cases in the block above them.

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
import fcntl
import hashlib
import io
import json
import os
import pty
import random
import re
import resource
import select
import shutil
import signal
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

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


# ======================================================================================
# F4: load-meter with the injection ledger
# ======================================================================================

LM_START, LM_END, LM_NOW = "2026-10-01", "2026-10-08", "2026-10-09T12:00:00Z"
TYPED_TEXTS = ("F4 typed one", "F4 typed two", "F4 typed three")
INJECTED_TEXTS = ("hub: your session shows offline. Call register() now", "F4 squad cmd line")
LM_DAY = datetime(2026, 10, 3, 10, 0, tzinfo=timezone.utc).timestamp()


def _user(epoch, text, kind="human"):
    return json.dumps({"type": "user", "timestamp": _iso(epoch), "message": {"role": "user", "content": text}, "origin": {"kind": kind}})


def _assistant(epoch, text="ok", tokens=None):
    msg = {"role": "assistant", "content": [{"type": "text", "text": text}]}
    if tokens is not None:
        msg["usage"] = {"input_tokens": 2, "cache_read_input_tokens": tokens - 2043, "cache_creation_input_tokens": 2041}
    return json.dumps({"type": "assistant", "timestamp": _iso(epoch), "message": msg})


def _ledger_row(epoch, lane, text, door="nudge", by="squad"):
    return {"ts": epoch, "lane": lane, "door": door, "by": by, "chars": len(text), "sha256": _sha(text)}


@pytest.fixture
def lm(tmp_path):
    db = tmp_path / "hub.db"
    c = sqlite3.connect(db)
    c.executescript("CREATE TABLE messages (id INTEGER PRIMARY KEY, ts REAL, from_agent TEXT, to_agent TEXT, channel TEXT);")
    c.commit()
    c.close()
    snap = tmp_path / "snap.json"
    snap.write_text(json.dumps({"source": "fixture", "taken_at": LM_NOW, "since": "2026-09-26T00:00Z",
                                "columns": ["hour_utc", "recipient", "reason", "held_max", "count"], "rows": []}))
    tr = tmp_path / "transcripts"
    lines, rows, t = [], [], LM_DAY
    for i in range(5):
        if i in (1, 3):
            text = INJECTED_TEXTS[0 if i == 1 else 1]
            rows.append(_ledger_row(t - 2, "lane-a", text, door="nudge" if i == 1 else "cmd", by="squad" if i == 1 else "hand"))
        else:
            text = TYPED_TEXTS[{0: 0, 2: 1, 4: 2}[i]]
        lines += [_user(t, text), _assistant(t + 20)]
        t += 600
    (tr / "lane-a").mkdir(parents=True)
    (tr / "lane-a" / "s1.jsonl").write_text("\n".join(lines) + "\n")
    ledger = tmp_path / "injections.jsonl"
    ledger.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return {"tmp": tmp_path, "db": db, "snap": snap, "tr": tr, "ledger": ledger, "rows": rows}


def _meter(lm, injections, *, cpu=False, tr=None):
    out = lm["tmp"] / "reading.json"
    if out.exists():
        out.unlink()
    cmd = [sys.executable, "-m", "mcp_hub.cli", "load-meter", "--hub-db", str(lm["db"]), "--snapshot", str(lm["snap"]),
           "--transcripts", str(tr or lm["tr"]), "--out", str(out), "--baseline-start", LM_START, "--baseline-end", LM_END,
           "--now", LM_NOW, "--hub-db-taken-at", LM_NOW, "--injections", str(injections)]
    r0 = resource.getrusage(resource.RUSAGE_CHILDREN)
    p = subprocess.run(cmd, capture_output=True, text=True, cwd=lm["tmp"], env=CHILD_ENV, timeout=300)
    r1 = resource.getrusage(resource.RUSAGE_CHILDREN)
    assert p.returncode == 0, f"load-meter --injections exited {p.returncode}: {p.stderr[-600:]}"
    r = json.loads(out.read_text())
    errors = list(Draft202012Validator(SCHEMA).iter_errors(r))
    assert not errors, errors[0].message
    if cpu:
        return r, (r1.ru_utime - r0.ru_utime) + (r1.ru_stime - r0.ru_stime)
    return r


def test_r5_f4_2_injected_turns_are_counted_apart_from_typed(lm):
    r = _meter(lm, lm["ledger"])
    m = r["measures"]
    assert m["typed_turns"]["pane"]["value"] == 3, f"typed_turns {m['typed_turns']['pane']['value']} (2 of 5 were squad's, ledgered)"
    inj = m.get("injected_turns")
    assert inj is not None, "no injected_turns measure"
    assert inj["value"] == 2, inj
    assert str(lm["ledger"]) in inj["source"], f"injected_turns' source does not name the ledger: {inj['source']}"
    assert inj.get("per_lane") == {"lane-a": 2}, inj


def test_r5_f4_2_without_the_ledger_injected_is_missing_not_zero(lm):
    r = _meter(lm, lm["tmp"] / "no-such-ledger.jsonl")
    m = r["measures"]
    assert m["typed_turns"]["pane"]["value"] == 5
    inj = m.get("injected_turns")
    assert inj is not None and inj["value"] is None and inj.get("missing"), f"no ledger must read null + missing, never 0: {inj}"


def test_r5_f4_3_an_unmatched_ledger_row_counts_nothing_and_is_flagged(lm):
    rows = list(lm["rows"])
    rows.append(_ledger_row(LM_DAY + 3000, "lane-a", "text that was never typed", door="cmd", by="hand"))
    rows.append(_ledger_row(LM_DAY - 30, "lane-a", TYPED_TEXTS[0], door="cmd", by="hand"))   # right text, 30 s early
    lm["ledger"].write_text("".join(json.dumps(r) + "\n" for r in rows))
    r = _meter(lm, lm["ledger"])
    m = r["measures"]
    assert m["typed_turns"]["pane"]["value"] == 3, m["typed_turns"]["pane"]
    assert m["injected_turns"]["value"] == 2, m["injected_turns"]
    flags = [f for f in r["flags"] if f["code"] == "injection_unmatched"]
    assert flags, f"an unmatched ledger row was not flagged: {r['flags']}"
    assert any("lane-a" in f["detail"] for f in flags)


def test_r5_f4_5_guard_schema_declares_injected_turns_and_carries_r2_to_r4():
    Draft202012Validator.check_schema(SCHEMA)
    assert "injected_turns" in SCHEMA["$defs"]["measure_set"]["properties"]
    assert "injection_unmatched" in SCHEMA["properties"]["flags"]["items"]["properties"]["code"]["enum"]
    for n in (2, 3, 4):
        assert (ROOT / "tests" / f"test_v2fleet_r{n}_falsifiers.py").is_file()


def test_r5_f4_7_load_meter_with_a_ledger_costs_under_2s_cpu_at_46_lanes(lm):
    tr = lm["tmp"] / "busy"
    rows = []
    day = datetime(2026, 10, 8, 6, 0, tzinfo=timezone.utc).timestamp()   # the closed day after the baseline
    for n in range(46):
        lane = f"lane{n:02d}"
        lines = []
        t = day
        for k in range(100):
            text = f"{lane} turn {k}"
            if k % 10 == 0:
                rows.append(_ledger_row(t - 1, lane, text))
            lines += [_user(t, text), _assistant(t + 5), _user(t + 60, "", kind="channel"), _assistant(t + 65)]
            t += 300
        (tr / lane).mkdir(parents=True)
        (tr / lane / "s.jsonl").write_text("\n".join(lines) + "\n")
    led = lm["tmp"] / "busy-injections.jsonl"
    led.write_text("".join(json.dumps(r) + "\n" for r in rows))
    r, cpu = _meter(lm, led, cpu=True, tr=tr)
    day_m = {d["day"]: d["measures"] for d in r["daily"]}["2026-10-08"]
    assert day_m["injected_turns"]["value"] == 460, day_m["injected_turns"]
    assert cpu <= 2.0, f"load-meter used {cpu:.2f} s CPU for 46 lanes x 1 day with a ledger (limit 2 s)"


# ======================================================================================
# A real scratch fleet (F4 doors, F5): private tmux socket, panes that look like claude
# ======================================================================================

FAKE_CLAUDE = (
    "import pathlib, subprocess, sys, time\n"
    "subprocess.Popen(['bash', '-c', 'exec -a claude-fake sleep 3600'])\n"
    "sys.stdout.write(pathlib.Path(sys.argv[1]).read_text()); sys.stdout.flush()\n"
    "while True:\n    time.sleep(3600)\n"
)
IDLE_SCREEN = "● ready\n\n❯ \n  ⏵⏵ accept edits on (shift+tab to cycle)\n"
DIALOG_SCREEN = "● Bash(ls)\n\nDo you want to proceed?\n❯ 1. Yes\n  2. No, and tell Claude what to do differently\n"
NUDGE_MARK = "Call register() now"
ASK_MARK = "tokens, over the"
OVER_CAP = 169_255


class Client:
    """A real tmux client attached to one session, under a pty (an 'attached, active' viewer)."""

    def __init__(self, fleet, lane):
        self.fleet, self.lane = fleet, lane
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            try:
                fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 120, 0, 0))
                os.execve(fleet.real_tmux, [fleet.real_tmux, "-L", fleet.sock, "attach", "-t", lane], fleet.tenv)
            finally:
                os._exit(127)
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 120, 0, 0))
        self._stop = False
        self._t = threading.Thread(target=self._drain, daemon=True)
        self._t.start()
        deadline = time.time() + 10
        while time.time() < deadline:
            if self.tty():
                break
            time.sleep(0.1)
        assert self.tty(), "the scratch tmux client never attached"
        time.sleep(0.5)

    def _drain(self):
        while not self._stop:
            try:
                r, _, _ = select.select([self.fd], [], [], 0.1)
                if r:
                    os.read(self.fd, 65536)
            except OSError:
                return

    def tty(self):
        out = subprocess.run([self.fleet.real_tmux, "-L", self.fleet.sock, "list-clients", "-t", self.lane, "-F", "#{client_tty}"],
                             capture_output=True, text=True, env=self.fleet.tenv).stdout.strip()
        return out

    def detach(self):
        subprocess.run([self.fleet.real_tmux, "-L", self.fleet.sock, "detach-client", "-s", self.lane], env=self.fleet.tenv, capture_output=True)
        deadline = time.time() + 5
        while time.time() < deadline:
            pid, _ = os.waitpid(self.pid, os.WNOHANG)
            if pid:
                self.pid = 0
                break
            time.sleep(0.05)
        self.close()

    def close(self):
        self._stop = True
        if self.pid:
            with contextlib.suppress(OSError):
                os.kill(self.pid, signal.SIGKILL)
            with contextlib.suppress(ChildProcessError):
                os.waitpid(self.pid, 0)
            self.pid = 0
        with contextlib.suppress(OSError):
            os.close(self.fd)


class Fleet:
    def __init__(self, tmp, lanes, *, members=None):
        _need("tmux")
        self.real_tmux = shutil.which("tmux")
        self.tmp = Path(tmp)
        self.home = self.tmp / "home"
        self.state = self.home / ".mcp-hub"
        self.state.mkdir(parents=True)
        self.work = self.tmp / "w"
        self.lanes = dict(lanes)
        rows = []
        for a in self.lanes:
            (self.work / a).mkdir(parents=True)
            rows.append(f"{a}|{self.work / a}||{COMMS} --continue|squad\n")
        self.conf = self.home / "squad.conf"
        self.conf.write_text("".join(rows))
        self.log = self.tmp / "tmux.log"
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        ledger = self.state / "injections.jsonl"
        (self.bin / "tmux").write_text(
            "#!/bin/bash\n"
            'case " $* " in\n'
            f'  *" send-keys "*) n=0; [ -f {ledger} ] && n=$(wc -l < {ledger}); echo "SEND $n $*" >> {self.log} ;;\n'
            f'  *" list-clients "*) echo "LC $*" >> {self.log} ;;\n'
            f'  *" respawn-pane "*|*" kill-session "*|*" kill-server "*) echo "KILL $*" >> {self.log} ;;\n'
            "esac\n"
            f'exec {self.real_tmux} "$@"\n')
        mem = list(self.lanes) if members is None else list(members)
        (self.bin / "mcp-hub").write_text(
            "#!/bin/bash\n"
            'if [ "$1" = squads ] && [ "$2" = members ]; then\n' + "".join(f"  echo {m}\n" for m in mem) + "fi\nexit 0\n")
        for f in ("tmux", "mcp-hub"):
            (self.bin / f).chmod(0o755)
        (self.bin / "python").symlink_to(sys.executable)
        sq = self.tmp / "sq" / "squad"
        sq.mkdir(parents=True)
        shutil.copy2(SQUAD, sq / "squad")
        self.squad_path = sq / "squad"
        self.tdir = Path(tempfile.mkdtemp(prefix="v2f5-"))
        self.sock = f"r5-{uuid.uuid4().hex[:8]}"
        env = {k: v for k, v in os.environ.items() if not k.startswith("TMUX")}
        for k in ("SQUAD_IN_USE_SECONDS", "SQUAD_BY_HAND", "MCP_HUB_COMPACT_ASK", "MCP_HUB_COMPACT_ASK_TOKENS",
                  "MCP_HUB_COMPACTION_EXEC", "MCP_HUB_CONVERSATION_SECONDS", "SQUAD_HEAL_MAX_NUDGES"):
            env.pop(k, None)
        env.update(HOME=str(self.home), SQUAD_CONF=str(self.conf), SQUAD_SOCKET=self.sock, TMUX_TMPDIR=str(self.tdir),
                   PATH=f"{self.bin}:{os.environ['PATH']}", MCP_HUB_STATE_DIR=str(self.state), SQUAD_HEAL_GRACE_SECONDS="0",
                   MCP_HUB_HELD_FILE=str(self.home / "held-lanes.json"), MCP_HUB_HOLD_BOUNDARY_DIR=str(self.home / "hold-boundary"),
                   MCP_HUB_PACE_URL=f"file://{self.home}/pace.json", LANG="C.UTF-8", PYTHONPATH=CHILD_ENV["PYTHONPATH"])
        self.env = env
        self.tenv = {**env, "PATH": os.environ["PATH"]}
        self.clients = []
        fake = self.tmp / "fake_claude.py"
        fake.write_text(FAKE_CLAUDE)
        for a, screen in self.lanes.items():
            sf = self.tmp / f"screen-{a}.txt"
            sf.write_text(screen)
            subprocess.run([self.real_tmux, "-L", self.sock, "new-session", "-d", "-s", a, "-x", "120", "-y", "30",
                            f"exec {sys.executable} {fake} {sf}"], check=True, env=self.tenv)
            self.status(a, online=True)
        deadline = time.time() + 15
        for a, screen in self.lanes.items():
            last = [ln for ln in screen.splitlines() if ln.strip()][-1].strip()
            while last not in self.capture(a):
                assert time.time() < deadline, f"scratch pane {a} never drew its screen"
                time.sleep(0.1)
        time.sleep(1.2)   # the fake claude child must exist for agent_started

    def status(self, a, *, online):
        (self.state / f"status-{a}.json").write_text(json.dumps({"online": online, "wakeable": online, "focus_until": 0}))

    def capture(self, a):
        return subprocess.run([self.real_tmux, "-L", self.sock, "capture-pane", "-p", "-t", a], capture_output=True, text=True, env=self.tenv).stdout

    def attach(self, a):
        c = Client(self, a)
        self.clients.append(c)
        return c

    def squad(self, *args, env=None, timeout=180):
        e = dict(self.env)
        e.update(env or {})
        return subprocess.run(["bash", str(self.squad_path), *args], capture_output=True, text=True, env=e, timeout=timeout)

    def mark(self):
        return len(self.log.read_text().splitlines()) if self.log.exists() else 0

    def lines(self, since=0, kind=None, lane=None):
        out = self.log.read_text().splitlines()[since:] if self.log.exists() else []
        if kind:
            out = [ln for ln in out if ln.startswith(kind + " ")]
        if lane:
            out = [ln for ln in out if f" -t {lane} " in ln + " " or f" -t ={lane} " in ln + " "]
        return out

    def ledger(self):
        p = self.state / "injections.jsonl"
        return [json.loads(ln) for ln in p.read_text().splitlines() if ln.strip()] if p.exists() else []

    def inbox_text(self):
        d = self.state / "lane-inbox"
        if not d.is_dir():
            return ""
        return "".join(p.read_text(errors="replace") for p in sorted(d.rglob("*")) if p.is_file())

    def transcript(self, a, tokens):
        d = self.home / ".claude" / "projects" / str(self.work / a).replace("/", "-")
        d.mkdir(parents=True, exist_ok=True)
        (d / "05a50d0c-1111-2222-3333-444455556666.jsonl").write_text(_assistant(time.time() - 120, "working", tokens) + "\n")

    def close(self):
        for c in self.clients:
            c.close()
        subprocess.run([self.real_tmux, "-L", self.sock, "kill-server"], env=self.tenv, capture_output=True)
        shutil.rmtree(self.tdir, ignore_errors=True)


@pytest.fixture
def fleet_factory(tmp_path):
    made = []

    def make(lanes, **kw):
        f = Fleet(tmp_path / f"f{len(made)}", lanes, **kw)
        made.append(f)
        return f

    yield make
    for f in made:
        f.close()


def _one_new_row(fleet, before_rows, mark, lane, door, text=None):
    rows = fleet.ledger()[len(before_rows):]
    assert len(rows) == 1, f"{door}: {len(rows)} ledger rows for one send: {rows}"
    row = rows[0]
    assert row.get("door") == door and row.get("lane") == lane, row
    assert set(row) >= {"ts", "lane", "door", "by", "chars", "sha256"}, row
    assert isinstance(row["chars"], int) and re.fullmatch(r"[0-9a-f]{64}", str(row["sha256"])), row
    if door in HAND_DOORS:
        assert row["by"] == "hand", row
    else:
        assert row["by"] != "hand", row
    if text is not None:
        assert row["chars"] == len(text) and row["sha256"] == _sha(text), row
    sends = fleet.lines(mark, "SEND", lane)
    assert sends, f"{door}: nothing was sent"
    first_n = int(sends[0].split()[1])
    assert first_n == len(before_rows) + 1, f"{door}: the ledger row was not written before the first keystroke ({sends[0]})"
    return row


def test_r5_f4_1_every_squad_door_writes_one_row_before_its_send(fleet_factory):
    f = fleet_factory({"lane-a": IDLE_SCREEN, "lane-b": IDLE_SCREEN, "lane-c": IDLE_SCREEN, "lane-d": DIALOG_SCREEN})
    cases = [
        (("cmd", "lane-a", "F4 cmd text"), "lane-a", "cmd", "F4 cmd text", {}),
        (("key", "lane-a", "C-l"), "lane-a", "key", None, {}),
        (("model", "lane-a", "sonnet"), "lane-a", "model", "/model sonnet", {}),
        (("answer", "lane-d", "no"), "lane-d", "answer", None, {}),
    ]
    for args, lane, door, text, env in cases:
        rows, mark = f.ledger(), f.mark()
        p = f.squad(*args, env=env)
        assert p.returncode == 0, f"squad {' '.join(args)}: {p.returncode} {p.stderr[-400:]}"
        _one_new_row(f, rows, mark, lane, door, text)
    # heal's nudge: lane-b offline
    f.status("lane-b", online=False)
    rows, mark = f.ledger(), f.mark()
    p = f.squad("heal")
    assert p.returncode == 0, p.stderr[-400:]
    _one_new_row(f, rows, mark, "lane-b", "nudge")
    # the compaction ask: lane-c over the cap, armed
    f.status("lane-b", online=True)
    f.transcript("lane-c", OVER_CAP)
    rows, mark = f.ledger(), f.mark()
    p = f.squad("heal", env={"MCP_HUB_COMPACT_ASK": "1"})
    assert p.returncode == 0, p.stderr[-400:]
    _one_new_row(f, rows, mark, "lane-c", "compaction_type")


class SeatRunner:
    """The edge's injected runner. list-clients is answered for any -F format."""

    def __init__(self, clients=(), ledger=None):
        self.calls, self.clients, self.ledger = [], list(clients), ledger
        self.rows_at_first_send = None

    def __call__(self, cmd, cwd=None):  # noqa: ARG002
        cmd = list(cmd)
        self.calls.append(cmd)
        if "list-clients" in cmd:
            fmt = cmd[cmd.index("-F") + 1] if "-F" in cmd else "#{client_name}: #{session_name}"
            out = []
            for c in self.clients:
                s = fmt
                for k, v in {"client_activity": str(int(c["activity"])), "client_tty": c["tty"], "client_name": c["tty"],
                             "client_session": "lane-a", "session_name": "lane-a", "client_created": str(int(c["activity"]))}.items():
                    s = s.replace("#{" + k + "}", v)
                out.append(re.sub(r"#\{[^}]*\}", "", s))
            return 0, "".join(ln + "\n" for ln in out)
        if "send-keys" in cmd and self.rows_at_first_send is None and self.ledger is not None:
            self.rows_at_first_send = len(self.ledger.read_text().splitlines()) if self.ledger.exists() else 0
        if "capture-pane" in cmd:
            return 0, "● ready\n> F4 edge text\n❯ \n"
        return 0, ""

    def sent(self):
        return [c for c in self.calls if "send-keys" in c]


def test_r5_f4_1_the_edge_prompt_writes_one_row_before_its_send():
    from mcp_hub.edge import realize_seat_action

    ledger = Path(os.environ["MCP_HUB_STATE_DIR"]) / "injections.jsonl"
    runner = SeatRunner(ledger=ledger)
    out = realize_seat_action({"kind": "prompt", "args": {"text": "F4 edge text"}}, {"session": "lane-a"}, runner, pause=lambda _s: None)
    assert runner.sent(), out
    rows = [json.loads(ln) for ln in ledger.read_text().splitlines()] if ledger.exists() else []
    assert len(rows) == 1, f"edge prompt: {len(rows)} ledger rows"
    assert rows[0]["door"] == "edge_prompt" and rows[0]["lane"] == "lane-a" and rows[0]["by"] != "hand", rows[0]
    assert rows[0]["sha256"] == _sha("F4 edge text") and rows[0]["chars"] == len("F4 edge text")
    assert runner.rows_at_first_send == 1, "the ledger row was not written before the first keystroke"


def test_r5_f4_4_the_ledger_never_stores_the_typed_text(fleet_factory):
    f = fleet_factory({"lane-a": IDLE_SCREEN})
    secret = "ZQX-R5-SECRET-4412 do not store me"
    assert f.squad("cmd", "lane-a", secret).returncode == 0
    raw = (f.state / "injections.jsonl").read_text() if (f.state / "injections.jsonl").exists() else ""
    assert raw, "no ledger written"
    assert "ZQX-R5-SECRET" not in raw, "the ledger stored typed text"
    row = json.loads(raw.splitlines()[-1])
    assert row["chars"] == len(secret) and row["sha256"] == _sha(secret)


def test_r5_f4_6_the_ledger_is_pruned_at_14_days(fleet_factory):
    f = fleet_factory({"lane-a": IDLE_SCREEN})
    now = time.time()
    old = _ledger_row(now - 15 * 86400, "lane-a", "fifteen days old")
    kept = _ledger_row(now - 13 * 86400, "lane-a", "thirteen days old")
    (f.state / "injections.jsonl").write_text(json.dumps(old) + "\n" + json.dumps(kept) + "\n")
    assert f.squad("cmd", "lane-a", "prune trigger").returncode == 0
    rows = f.ledger()
    assert old not in rows, "a row older than 14 days survived an append"
    assert kept in rows, "a 13-day row was pruned"
    assert rows[-1]["sha256"] == _sha("prune trigger")


# ======================================================================================
# F5: nothing automated types into a pane in use
# ======================================================================================

@pytest.fixture
def hook_in(monkeypatch, utc):
    def make(state, tmp):
        monkeypatch.setenv("MCP_HUB_STATE_DIR", str(state))
        return Hook(tmp, monkeypatch)
    return make


def test_r5_f5_1_an_in_use_pane_gets_the_nudge_at_its_stop_not_its_keyboard(fleet_factory, hook_in, tmp_path):
    f = fleet_factory({"lane-a": IDLE_SCREEN})
    f.attach("lane-a")
    f.status("lane-a", online=False)
    before = f.capture("lane-a")
    mark = f.mark()
    p = f.squad("heal")
    assert p.returncode == 0, p.stderr[-400:]
    sent = f.lines(mark, "SEND", "lane-a")
    assert sent == [], f"heal typed into a pane in use: {sent[:2]}"
    assert f.capture("lane-a") == before, "the in-use pane changed"
    assert NUDGE_MARK in f.inbox_text(), "the refused nudge is not in the lane inbox"
    h = hook_in(f.state, tmp_path / "hook")
    text, _ = h.stop(online=True)
    assert text is not None and NUDGE_MARK in text, f"the next Stop did not carry the inbox: {text!r}"
    assert NUDGE_MARK not in f.inbox_text(), "the inbox was not cleared after its Stop"
    again, _ = h.stop(online=True)
    assert again is None, "the inbox was delivered twice"


def test_r5_f5_2_guard_a_detached_or_idle_pane_is_nudged_as_today(fleet_factory):
    f = fleet_factory({"lane-a": IDLE_SCREEN, "lane-b": IDLE_SCREEN})
    c = f.attach("lane-b")
    time.sleep(2.5)
    f.status("lane-a", online=False)
    f.status("lane-b", online=False)
    mark = f.mark()
    p = f.squad("heal", env={"SQUAD_IN_USE_SECONDS": "1"})
    assert p.returncode == 0, p.stderr[-400:]
    for lane in ("lane-a", "lane-b"):
        lit = [ln for ln in f.lines(mark, "SEND", lane) if " -l " in ln]
        assert lit and NUDGE_MARK in lit[0], f"{lane} (detached, or a client idle past the window) was not nudged"
    c.detach()


def test_r5_f5_3_hand_verbs_still_type_on_a_pane_in_use(fleet_factory):
    f = fleet_factory({"lane-a": IDLE_SCREEN, "lane-d": DIALOG_SCREEN})
    f.attach("lane-a")
    f.attach("lane-d")
    for args, lane, door in ((("cmd", "lane-a", "hand typed line"), "lane-a", "cmd"), (("key", "lane-a", "C-l"), "lane-a", "key"),
                             (("answer", "lane-d", "no"), "lane-d", "answer")):
        rows, mark = f.ledger(), f.mark()
        p = f.squad(*args)
        assert p.returncode == 0, f"Term 1: squad {args[0]} on a pane in use exited {p.returncode}: {p.stderr[-300:]}"
        assert "in use" not in (p.stdout + p.stderr), p.stdout + p.stderr
        row = _one_new_row(f, rows, mark, lane, door)
        assert row["by"] == "hand"


@pytest.mark.parametrize("kind", ["prompt", "interrupt"])
def test_r5_f5_4_the_edge_refuses_an_in_use_seat_naming_the_client(kind):
    from mcp_hub.edge import realize_seat_action

    runner = SeatRunner(clients=[{"tty": "/dev/pts/42", "activity": time.time() - 10}])
    out = realize_seat_action({"kind": kind, "args": {"text": "edge text"}}, {"session": "lane-a"}, runner, pause=lambda _s: None)
    assert out["status"] == "refused", f"edge {kind} into a pane in use settled {out['status']}"
    assert "/dev/pts/42" in str(out["observed"].get("why", "")), out["observed"]
    assert runner.sent() == [], "keys were sent to a pane in use"


def test_r5_f5_4_guard_an_idle_client_does_not_refuse_the_edge():
    from mcp_hub.edge import realize_seat_action

    runner = SeatRunner(clients=[{"tty": "/dev/pts/42", "activity": time.time() - 3600}])
    out = realize_seat_action({"kind": "interrupt", "args": {}}, {"session": "lane-a"}, runner, pause=lambda _s: None)
    assert out["status"] == "done", out
    assert runner.sent()


ALLOWED_SEND_KEYS = {"type_line", "compaction_type", "wait_shell_ready", "launch_agent_cmd", "arm:key", "arm:answer"}


def _squad_structure(src):
    lines = src.splitlines()
    funcs, owner = {}, [None] * len(lines)
    i = 0
    while i < len(lines):
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\(\)\s*\{(.*)$", lines[i])
        if m:
            name = m.group(1)
            if m.group(2).rstrip().endswith("}"):
                funcs[name] = m.group(2)
                owner[i] = name
                i += 1
                continue
            j = i + 1
            while j < len(lines) and not re.match(r"^\}\s*(#.*)?$", lines[j]):
                j += 1
            funcs[name] = "\n".join(lines[i + 1:j])
            for k in range(i, min(j + 1, len(lines))):
                owner[k] = name
            i = j + 1
            continue
        i += 1
    dispatch = next(n for n, ln in enumerate(lines) if ln.startswith('case "${1:-help}" in'))
    arm = None
    for n in range(dispatch, len(lines)):
        m = re.match(r"^  ([a-z][a-z0-9_|-]*)\)", lines[n])
        if m:
            arm = m.group(1)
        if owner[n] is None:
            owner[n] = f"arm:{arm}" if arm else None
    return lines, funcs, owner


def _reaches(text, funcs, target, depth=3, seen=None):
    if re.search(rf"\b{target}\b", text):
        return True
    if depth == 0:
        return False
    seen = seen or set()
    for name, body in funcs.items():
        if name not in seen and re.search(rf"(^|[\s;&|(`$]){re.escape(name)}($|[\s;&|)`])", text):
            seen.add(name)
            if _reaches(body, funcs, target, depth - 1, seen):
                return True
    return False


def test_r5_f5_5_every_automated_door_goes_through_pane_in_use():
    src = SQUAD.read_text(encoding="utf-8")
    lines, funcs, owner = _squad_structure(src)
    stray = []
    send = re.compile(r"(?<![\w-])(tm|tmt)\s+send-keys\b|(?<![\w-])tmux\s+-L\s+\S+\s+send-keys\b")
    for n, ln in enumerate(lines):
        if not ln.lstrip().startswith("#"):
            code = ln.split(" #", 1)[0]
            if send.search(code) and owner[n] not in ALLOWED_SEND_KEYS:
                stray.append(f"{n + 1}: {owner[n]}: {ln.strip()[:90]}")
    assert not stray, "send-keys outside the allow-listed helpers:\n" + "\n".join(stray)
    assert "pane_in_use" in funcs, "squad has no pane_in_use door"
    assert _reaches(funcs["compaction_type"], funcs, "pane_in_use"), "compaction_type does not go through pane_in_use"
    heal_start = next(n for n, ln in enumerate(lines) if re.match(r"^  heal\)", ln))
    nudge_at = next(n for n in range(heal_start, len(lines)) if re.match(r"^\s+nudge\)\s*$", lines[n]))
    nudge_end = next(n for n in range(nudge_at, len(lines)) if lines[n].strip() == ";;")
    assert _reaches("\n".join(lines[nudge_at:nudge_end]), funcs, "pane_in_use"), "heal's nudge arm does not go through pane_in_use"


def test_r5_f5_6_a_refused_nudge_is_not_charged_and_arms_no_relaunch(fleet_factory):
    f = fleet_factory({"lane-a": IDLE_SCREEN})
    c = f.attach("lane-a")
    f.status("lane-a", online=False)
    ncnt = f.state / "heal-nudges-lane-a"
    mark = f.mark()
    for _ in range(3):
        p = f.squad("heal")
        assert p.returncode == 0, p.stderr[-400:]
        f.status("lane-a", online=False)
    assert f.lines(mark, "SEND", "lane-a") == [], "heal typed into a pane in use"
    assert f.lines(mark, "KILL") == [], f"heal relaunched a lane in use: {f.lines(mark, 'KILL')[:1]}"
    assert (ncnt.read_text().strip() if ncnt.exists() else "0") in ("", "0"), f"a refused nudge was charged: {ncnt.read_text()!r}"
    c.detach()
    mark = f.mark()
    p = f.squad("heal")
    assert p.returncode == 0, p.stderr[-400:]
    lit = [ln for ln in f.lines(mark, "SEND", "lane-a") if " -l " in ln]
    assert lit and NUDGE_MARK in lit[0], "the nudge was not typed once the pane was free"
    assert ncnt.read_text().strip() == "1", f"the delivered nudge was charged {ncnt.read_text()!r}"


def test_r5_f5_7_the_compaction_ask_is_off_by_default(fleet_factory):
    f = fleet_factory({"lane-c": IDLE_SCREEN})
    f.transcript("lane-c", OVER_CAP)
    mark = f.mark()
    p = f.squad("heal")
    assert p.returncode == 0, p.stderr[-400:]
    asked = [ln for ln in f.lines(mark, "SEND", "lane-c") if ASK_MARK in ln]
    assert asked == [], "Term 2: with no env set, the compaction ask was typed"
    mark = f.mark()
    p = f.squad("heal", env={"MCP_HUB_COMPACT_ASK": "1"})
    assert p.returncode == 0, p.stderr[-400:]
    assert [ln for ln in f.lines(mark, "SEND", "lane-c") if ASK_MARK in ln], "MCP_HUB_COMPACT_ASK=1 did not arm the ask"


def test_r5_f5_8_an_armed_ask_to_an_in_use_pane_goes_to_the_inbox(fleet_factory):
    f = fleet_factory({"lane-c": IDLE_SCREEN})
    f.transcript("lane-c", OVER_CAP)
    f.attach("lane-c")
    mark = f.mark()
    p = f.squad("heal", env={"MCP_HUB_COMPACT_ASK": "1"})
    assert p.returncode == 0, p.stderr[-400:]
    assert f.lines(mark, "SEND", "lane-c") == [], "the compaction ask was typed into a pane in use"
    assert ASK_MARK in f.inbox_text(), "the refused ask is not in the lane inbox"


def test_r5_f5_9_guard_list_clients_only_per_attempted_send(fleet_factory):
    f = fleet_factory({"lane-a": IDLE_SCREEN, "lane-b": IDLE_SCREEN, "lane-c": IDLE_SCREEN})
    f.attach("lane-c")
    mark = f.mark()
    assert f.squad("heal").returncode == 0
    assert f.lines(mark, "LC") == [], f"a heal pass with no sends called list-clients: {f.lines(mark, 'LC')[:2]}"
    f.status("lane-a", online=False)
    mark = f.mark()
    assert f.squad("heal").returncode == 0
    assert len(f.lines(mark, "LC")) <= 1, f"one nudge attempt made {len(f.lines(mark, 'LC'))} list-clients calls"


# ======================================================================================
# F6: the conversation exemption, on squad's sourced helpers (tmux stubbed)
# ======================================================================================

class Rig:
    def __init__(self, tmp, lanes=("lane-a",)):
        _need("jq")
        self.tmp = Path(tmp)
        self.home = self.tmp / "home"
        self.state = self.home / ".mcp-hub"
        self.state.mkdir(parents=True)
        self.lanes = list(lanes)
        self.conf = self.home / "squad.conf"
        self.conf.write_text("".join(f"{a}|{self.home / 'w' / a}||--continue|squad\n" for a in self.lanes))
        self.held = self.home / "held-lanes.json"
        self.bdir = self.home / "hold-boundary"
        self.bdir.mkdir()
        (self.home / "pace.json").write_text(json.dumps({"mode": "TURBO", "ok": True, "checks": []}))
        self.bin = self.home / "bin"
        self.bin.mkdir()
        self.log = self.home / "tmux.log"
        self.inuse = self.home / "INUSE"
        (self.bin / "tmux").write_text(
            "#!/bin/bash\n"
            f'echo "$@" >> {self.log}\n'
            'for a in "$@"; do\n'
            '  case "$a" in\n'
            "    has-session) exit 0 ;;\n"
            "    display-message) echo node; exit 0 ;;\n"
            "    list-clients)\n"
            f"      [ -f {self.inuse} ] || exit 0\n"
            '      fmt="#{client_tty}"; prev=""\n'
            '      for b in "$@"; do [ "$prev" = "-F" ] && fmt="$b"; prev="$b"; done\n'
            '      now="$(date +%s)"\n'
            '      fmt="${fmt//"#{client_activity}"/$now}"; fmt="${fmt//"#{client_created}"/$now}"\n'
            '      fmt="${fmt//"#{client_tty}"//dev/pts/77}"; fmt="${fmt//"#{client_name}"//dev/pts/77}"\n'
            '      fmt="${fmt//"#{client_session}"/lane-a}"; fmt="${fmt//"#{session_name}"/lane-a}"\n'
            "      printf '%s\\n' \"$fmt\" | sed 's/#{[^}]*}//g'; exit 0 ;;\n"
            "  esac\n"
            "done\n"
            "exit 0\n")
        (self.bin / "mcp-hub").write_text(
            "#!/bin/bash\n"
            'if [ "$1" = squads ] && [ "$2" = members ]; then\n' + "".join(f"  echo {a}\n" for a in self.lanes) + "fi\nexit 0\n")
        for f in ("tmux", "mcp-hub"):
            (self.bin / f).chmod(0o755)
        (self.bin / "python").symlink_to(sys.executable)
        head = SQUAD.read_text(encoding="utf-8").split('\ncase "${1:-help}" in', 1)[0]
        assert "hold_enforce_one()" in head, "extraction boundary moved"
        self.script = self.home / "_squad_head.sh"
        self.script.write_text(head)

    def hold(self, *, owner="brake", held_ago=60.0, until_in=3600.0, lanes=None):
        held = {}
        for a in lanes or self.lanes:
            e = {"until": time.time() + until_in, "held_at": time.time() - held_ago, "reason": "over 1.5x fair share",
                 "release_condition": "window resets at 18:00"}
            if owner:
                e["owner"] = owner
                e["kind"] = "brake"
            held[a] = e
        self.held.write_text(json.dumps({"generated": time.time(), "held": held}))

    def stamp(self, a="lane-a"):
        (self.bdir / f"{a}.json").write_text(json.dumps({"reached_at": time.time() - 5, "agent": a}))

    def stamped(self, a="lane-a"):
        return (self.bdir / f"{a}.json").exists()

    def tdir(self, a):
        d = self.home / ".claude" / "projects" / str(self.home / "w" / a).replace("/", "-")
        d.mkdir(parents=True, exist_ok=True)
        return d

    def converse(self, typed_ago, *, a="lane-a", injected_ago=None, pad_kib=0):
        """A transcript whose last typed turn was `typed_ago` s ago (optionally a later injected one)."""
        now = time.time()
        out = []
        if pad_kib:
            filler = "x" * 900
            t0 = now - 7200
            while sum(len(x) + 1 for x in out) < pad_kib * 1024:
                out.append(_assistant(t0, filler))
                t0 += 1
        out += [_user(now - typed_ago, "Tim typed this"), _assistant(now - typed_ago + 20)]
        if injected_ago is not None:
            text = f"squad injected this {a}"
            out += [_user(now - injected_ago, text), _assistant(now - injected_ago + 20)]
            with open(self.state / "injections.jsonl", "a") as fh:
                fh.write(json.dumps(_ledger_row(now - injected_ago - 2, a, text)) + "\n")
        (self.tdir(a) / "s1.jsonl").write_text("\n".join(out) + "\n")

    def env(self):
        return {"PATH": f"{self.bin}:/usr/bin:/bin", "HOME": str(self.home), "SQUAD_CONF": str(self.conf),
                "MCP_HUB_HELD_FILE": str(self.held), "MCP_HUB_HOLD_BOUNDARY_DIR": str(self.bdir),
                "MCP_HUB_STATE_DIR": str(self.state), "MCP_HUB_PACE_URL": f"file://{self.home}/pace.json",
                "PYTHONPATH": CHILD_ENV["PYTHONPATH"], "LANG": "C.UTF-8"}

    def run(self, snippet):
        s = self.home / "_run.sh"
        s.write_text(self.script.read_text() + "\n" + snippet + "\n")
        return subprocess.run(["bash", str(s)], capture_output=True, text=True, env=self.env(), timeout=300)

    def enforce(self, a="lane-a"):
        start = len(self.log.read_text().splitlines()) if self.log.exists() else 0
        p = self.run(f"hold_enforce_one {a}")
        new = self.log.read_text().splitlines()[start:] if self.log.exists() else []
        return p, any("kill-session" in ln for ln in new)


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def test_r5_f6_1_an_owner_hold_defers_while_tim_is_talking(rig):
    rig.hold(owner="brake")
    rig.stamp()
    rig.converse(300)
    p, killed = rig.enforce()
    assert not killed, f"a lane Tim typed to 5 min ago was stopped by an owner hold: {p.stdout}{p.stderr}"
    assert "deferred: in conversation" in p.stdout, p.stdout + p.stderr
    assert not rig.stamped(), "the deferral kept the boundary stamp (it would later stop the lane mid-turn)"


def test_r5_f6_2_after_the_conversation_only_a_new_boundary_stops_it(rig):
    rig.hold(owner="brake", held_ago=2400)
    rig.converse(1860)
    p, killed = rig.enforce()
    assert not killed, f"stopped with no boundary 60 s after the conversation ended (the hard-stop clock ran from held_at): {p.stderr}"
    rig.stamp()
    p, killed = rig.enforce()
    assert killed, f"the first pass after a new boundary did not stop it: {p.stdout}{p.stderr}"


@pytest.mark.parametrize("after_end,stops", [(590, False), (610, True)])
def test_r5_f6_3_the_hard_stop_runs_from_the_conversations_end(rig, after_end, stops):
    rig.hold(owner="brake", held_ago=7200)
    rig.converse(1800 + after_end)
    p, killed = rig.enforce()
    assert killed is stops, f"{after_end} s after the conversation ended: killed={killed} (want {stops}): {p.stdout}{p.stderr}"


def test_r5_f6_4_guard_a_by_hand_hold_stops_as_today(rig):
    rig.hold(owner="")
    rig.stamp()
    rig.converse(300)
    p, killed = rig.enforce()
    assert killed, f"a by-hand hold (no owner) was deferred: {p.stdout}"


def test_r5_f6_5_guard_an_injected_turn_is_not_conversation(rig):
    rig.hold(owner="brake")
    rig.stamp()
    rig.converse(2400, injected_ago=300)
    p, killed = rig.enforce()
    assert killed, f"a turn the ledger marks injected kept the lane in conversation: {p.stdout}"


def test_r5_f6_6_no_stopping_notice_while_deferred_one_after(rig, monkeypatch, utc):
    monkeypatch.setenv("MCP_HUB_STATE_DIR", str(rig.state))
    h = Hook(rig.home, monkeypatch, held_file=rig.held, boundary_dir=rig.bdir)
    rig.hold(owner="brake")
    rig.converse(300)
    p, killed = rig.enforce()
    assert not killed
    text, _ = h.stop()
    assert text is None or "being stopped" not in text, f"a deferred lane was told it is being stopped: {text!r}"
    assert not rig.stamped(), "the hook stamped a boundary while the lane was deferred"
    rig.converse(2400)
    p, killed = rig.enforce()
    assert not killed, f"stopped on a stamp left from the deferral: {p.stdout}"
    text, _ = h.stop()
    assert text is not None and "being stopped" in text, f"the deferral ended but no notice: {text!r}"


def test_r5_f6_7_a_hold_that_expires_during_the_deferral_stops_nothing(rig):
    rig.hold(owner="brake")
    rig.stamp()
    rig.converse(300)
    p, killed = rig.enforce()
    assert not killed, p.stdout + p.stderr
    rig.hold(owner="brake", held_ago=7200, until_in=-60)
    rig.converse(2400)
    rig.stamp()
    p, killed = rig.enforce()
    assert not killed, f"an expired hold stopped the lane after its deferral: {p.stdout}{p.stderr}"


def test_r5_f6_8_an_in_use_pane_counts_as_conversation(rig):
    rig.hold(owner="brake")
    rig.stamp()
    rig.converse(2400)
    rig.inuse.write_text("")
    p, killed = rig.enforce()
    assert not killed, f"an owner hold stopped a lane whose pane is in use: {p.stdout}"
    assert "deferred: in conversation" in p.stdout


def test_r5_f6_9_the_exemption_adds_under_3s_cpu_to_a_pass_over_46_held_lanes(tmp_path):
    """F6's own cost at its worst case: every lane owner-held and in conversation, each a
    >256 KiB transcript and ledger rows. Measured as the pass's CPU minus a control pass over
    the same 46 lanes held BY HAND and waiting for a boundary (today's cheapest held path),
    so the bound is on what the exemption adds, not on hold enforcement as a whole."""
    lanes = [f"lane{n:02d}" for n in range(46)]

    def one_pass(r):
        r0 = resource.getrusage(resource.RUSAGE_CHILDREN)
        p = r.run("hold_enforce_pass")
        r1 = resource.getrusage(resource.RUSAGE_CHILDREN)
        assert p.returncode == 0, p.stderr[-400:]
        return p, (r1.ru_utime - r0.ru_utime) + (r1.ru_stime - r0.ru_stime)

    ctl = Rig(tmp_path / "ctl", lanes=lanes)
    ctl.hold(owner="", held_ago=60, lanes=lanes)
    for a in lanes:
        ctl.converse(300, a=a, injected_ago=100, pad_kib=300)
    pc, base_cpu = one_pass(ctl)
    assert pc.stdout.count("waiting for its turn boundary") == 46, pc.stdout[-600:]
    r = Rig(tmp_path / "f6", lanes=lanes)
    r.hold(owner="brake", held_ago=60, lanes=lanes)
    for a in lanes:
        r.stamp(a)
        r.converse(300, a=a, injected_ago=100, pad_kib=300)
    p, cpu = one_pass(r)
    assert p.stdout.count("deferred: in conversation") == 46, p.stdout[-600:]
    assert cpu - base_cpu <= 3.0, f"the exemption added {cpu - base_cpu:.2f} s CPU to one pass over 46 owner-held lanes (pass {cpu:.2f} s, control {base_cpu:.2f} s; limit 3 s)"


# ---------------------------------------------------------------------------
# test_r5_f4_live_*  F4 FIX PASS: honest counts on the LIVE layout.
# v2-fleet r5 F4 FIX PASS falsifiers: honest counts on the LIVE layout. FROZEN.
#
# Why these exist (features-json's independent check, hub 35649, T17): b-44's F4 matched a
# ledger row to a turn only when the row's `lane` and the transcript FOLDER were the same string.
# Live, they never are:
#   - the meter keys a lane by its transcript folder under ~/.claude/projects, which the client
#     names from the lane's worktree, e.g. /home/monke/Projects/code/dreamteam-ai-labs/vortex-driver
#     -> "-home-monke-Projects-code-dreamteam-ai-labs-vortex-driver";
#   - squad's doors write the AGENT name (`ledger_note "$a" ...`), e.g. "vortex-dev-vm-1";
#   - the edge's doors write the tmux SESSION name (`edge._ledger_note`, seat["session"]).
# So injected_turns read 0 and every row was "unmatched". b-44's tests passed only because their
# fixtures used one name on both sides.
#
# ⭐ THE MAPPING (decided here): THE SQUAD ROSTER. A ledger `lane` is a roster agent name; the
# roster row's field 2 is that lane's worktree; the transcript folder is the worktree encoded the
# way the Claude Code client encodes it: `~` expanded against $HOME, then EVERY character that is
# not [A-Za-z0-9] replaced by '-' (so "/home/monke/.mcp-hub/hooks-clone" ->
# "-home-monke--mcp-hub-hooks-clone"; the client's own folders show the doubled '-' for a dot).
# The roster is read from $SQUAD_CONF, default $HOME/.config/squad/squad.conf: the same file and
# the same rule squad itself reads (`rows`, `field "$a" 2`, `compaction_dir`). It is a local file:
# no hub, no tmux, no network.
# The tmux SESSION name needs no second source: squad names each lane's session for its roster
# agent ("agent sessions carry roster names", squad `unmanaged_sessions`; every door types with
# `tm send-keys -t "$a"`), and an enrolled lane seat's session is `spec.session or identity`, the
# same name. So the edge's row resolves through the same roster row. Rejected: the lane's cwd
# (only readable from a live process or tmux) and the tmux session (needs the live tmux server).
# A row whose `lane` already IS a transcript folder name still matches it directly (the guard).
#
# Falsifiers (each red by assertion on b-44's head 3a2affab):
#   (a) live naming: a squad nudge row (agent name) and an edge prompt row (session name) for the
#       same lane, each with a turn of the same sha 0-5 s later in that lane's transcript folder,
#       are counted as injected, not typed; no `injection_unmatched` flag.
#   (b) injected_turns is > 0 there, with the roster found at its DEFAULT path ($HOME/.config/squad/
#       squad.conf, SQUAD_CONF unset), for a worktree whose path holds a dot.
#   (c) key, answer and edge_interrupt rows carry no line of text (a key name, a menu digit,
#       Escape), so they can never match a turn: they are NOT flagged unmatched and change no
#       count; the reading counts them as their own kind, naming each such door in
#       injected_turns' source. A nudge that matched nothing is still flagged.
#   (d) record() appends: with nothing old enough to prune, earlier rows stay byte-identical in the
#       same file (same inode). A rewrite per append loses a row whenever squad and the edge write
#       at once. Pruning may still rewrite, but only when a row is past 14 days.
#   guard: one name on both sides (the b-44 fixtures' layout) matches as today, and a roster
#       mapping never lets a row match a same-sha turn in ANOTHER lane's folder.
#
# Hermetic: tmp dirs only (HOME, SQUAD_CONF, MCP_HUB_STATE_DIR); the rows are written by the real
# writers (squad's `python -m mcp_hub.injections` command line and `edge._ledger_note`).
# ---------------------------------------------------------------------------
from mcp_hub import edge as _live_edge  # noqa: E402
from mcp_hub import injections as _live_injections  # noqa: E402

LIVE_VORTEX_AGENT = "vortex-dev-vm-1"
LIVE_VORTEX_WORKTREE = "/home/monke/Projects/code/dreamteam-ai-labs/vortex-driver"
LIVE_VORTEX_FOLDER = "-home-monke-Projects-code-dreamteam-ai-labs-vortex-driver"
LIVE_HOOKS_AGENT = "mcp-hub-dev-vm-1"
LIVE_HOOKS_WORKTREE = "/home/monke/.mcp-hub/hooks-clone"
LIVE_HOOKS_FOLDER = "-home-monke--mcp-hub-hooks-clone"
LIVE_OTHER_AGENT = "features-json-dev-vm-1"
LIVE_OTHER_WORKTREE = "/home/monke/Projects/code/dreamteam-ai-labs/features-json"
LIVE_OTHER_FOLDER = "-home-monke-Projects-code-dreamteam-ai-labs-features-json"
LIVE_COMMS = "--continue --dangerously-load-development-channels server:hub"

LIVE_NUDGE = "hub: your session shows offline. Call register() now"
LIVE_PROMPT = "operator console: please post your status"
LIVE_TIM = "Tim's own line, typed by hand"


def _live_sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _live_iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _live_user(epoch, text, kind="human"):
    return json.dumps({"type": "user", "timestamp": _live_iso(epoch), "message": {"role": "user", "content": text}, "origin": {"kind": kind}})


def _live_assistant(epoch, text="ok"):
    return json.dumps({"type": "assistant", "timestamp": _live_iso(epoch), "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}})


@pytest.fixture
def live_layout(tmp_path, monkeypatch):
    """The live layout in a tmp HOME: a roster, a state dir, a transcripts root, a hub copy."""
    home = tmp_path / "home"
    state = tmp_path / "state"
    conf = home / ".config" / "squad" / "squad.conf"
    conf.parent.mkdir(parents=True)
    state.mkdir()
    conf.write_text(
        "# ~/.config/squad/squad.conf — this machine's squad roster (one line per agent)\n"
        "# Format:  agent_name | worktree | gh_config_dir | claude_args\n\n"
        f"{LIVE_VORTEX_AGENT}|{LIVE_VORTEX_WORKTREE}||{LIVE_COMMS}\n"
        f"{LIVE_HOOKS_AGENT}|{LIVE_HOOKS_WORKTREE}||{LIVE_COMMS}\n"
        f"{LIVE_OTHER_AGENT}|{LIVE_OTHER_WORKTREE}||{LIVE_COMMS}\n")
    tr = tmp_path / "projects"   # stands in for ~/.claude/projects
    tr.mkdir()
    db = tmp_path / "hub.db"
    c = sqlite3.connect(db)
    c.executescript("CREATE TABLE messages (id INTEGER PRIMARY KEY, ts REAL, from_agent TEXT, to_agent TEXT, channel TEXT);")
    c.commit()
    c.close()
    # The rows are written NOW by the real writers, so the baseline is placed around today.
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    end = today + timedelta(days=1)
    start = end - timedelta(days=7)
    now = (end + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    snap = tmp_path / "snap.json"
    snap.write_text(json.dumps({"source": "fixture", "taken_at": now, "since": start.strftime("%Y-%m-%dT%H:%MZ"),
                                "columns": ["hour_utc", "recipient", "reason", "held_max", "count"], "rows": []}))
    monkeypatch.setenv("MCP_HUB_STATE_DIR", str(state))
    monkeypatch.setenv("HOME", str(home))
    env = {**os.environ, "HOME": str(home), "MCP_HUB_STATE_DIR": str(state), "SQUAD_CONF": str(conf),
           "PYTHONPATH": SRC + (os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else "")}
    return {"tmp": tmp_path, "home": home, "state": state, "conf": conf, "tr": tr, "db": db, "snap": snap,
            "ledger": state / "injections.jsonl", "env": env, "start": start.date().isoformat(),
            "end": end.date().isoformat(), "now": now}


def _live_squad_note(live_layout, lane, door, by, text):
    """squad's `ledger_note "$a" <door> <by> "$text"`, as the script runs it: text on stdin."""
    p = subprocess.run([sys.executable, "-m", "mcp_hub.injections", "--lane", lane, "--door", door, "--by", by],
                       input=text, capture_output=True, text=True, env=live_layout["env"], timeout=60)
    assert p.returncode == 0, p.stderr
    return _live_last_row(live_layout)


def _live_edge_note(live_layout, session, door, text):
    """The edge's own writer, for a lane seat whose tmux session is `session`."""
    _live_edge._ledger_note({"identity": session, "substrate": "lane", "session": session}, door, text)
    return _live_last_row(live_layout)


def _live_last_row(live_layout):
    rows = _live_injections.read_rows(live_layout["ledger"])
    assert rows, "the writer left no row"
    return rows[-1]


def _live_transcript(live_layout, folder, lines, name="0b5c6f3e-1d2a-4c8e-9a1b-2f3e4d5c6b7a.jsonl"):
    d = live_layout["tr"] / folder
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text("\n".join(lines) + "\n")


def _live_meter(live_layout, *, env=None):
    out = live_layout["tmp"] / "reading.json"
    if out.exists():
        out.unlink()
    cmd = [sys.executable, "-m", "mcp_hub.cli", "load-meter", "--hub-db", str(live_layout["db"]), "--snapshot", str(live_layout["snap"]),
           "--transcripts", str(live_layout["tr"]), "--out", str(out), "--baseline-start", live_layout["start"],
           "--baseline-end", live_layout["end"], "--now", live_layout["now"], "--hub-db-taken-at", live_layout["now"],
           "--injections", str(live_layout["ledger"])]
    p = subprocess.run(cmd, capture_output=True, text=True, cwd=live_layout["tmp"], env=env or live_layout["env"], timeout=300)
    assert p.returncode == 0, f"load-meter exited {p.returncode}: {p.stderr[-600:]}"
    r = json.loads(out.read_text())
    errors = list(Draft202012Validator(SCHEMA).iter_errors(r))
    assert not errors, errors[0].message
    return r


def _live_unmatched(r):
    return [f for f in r["flags"] if f["code"] == "injection_unmatched"]


def test_r5_f4_live_a_agent_and_session_rows_count_as_injected_not_typed(live_layout):
    t0 = time.time() - 120
    nudge = _live_squad_note(live_layout, LIVE_VORTEX_AGENT, "nudge", "squad", LIVE_NUDGE)       # squad: the AGENT name
    prompt = _live_edge_note(live_layout, LIVE_VORTEX_AGENT, "edge_prompt", LIVE_PROMPT)        # edge: the tmux SESSION name
    assert nudge["lane"] == LIVE_VORTEX_AGENT and prompt["lane"] == LIVE_VORTEX_AGENT
    _live_transcript(live_layout, LIVE_VORTEX_FOLDER, [
        _live_user(t0, LIVE_TIM), _live_assistant(t0 + 20),
        _live_user(nudge["ts"] + 1, LIVE_NUDGE), _live_assistant(nudge["ts"] + 2),
        _live_user(prompt["ts"] + 1, LIVE_PROMPT), _live_assistant(prompt["ts"] + 2),
    ])
    r = _live_meter(live_layout)
    m = r["measures"]
    assert m["typed_turns"]["pane"]["per_lane"].get(LIVE_VORTEX_FOLDER) == 1, (
        f"the two door turns still count as typed: typed_turns per_lane {m['typed_turns']['pane'].get('per_lane')}")
    assert m["typed_turns"]["pane"]["value"] == 1, m["typed_turns"]["pane"]
    assert m["injected_turns"]["value"] == 2, m["injected_turns"]
    assert not _live_unmatched(r), f"rows for a lane the roster names were flagged unmatched: {_live_unmatched(r)}"


def test_r5_f4_live_b_injected_turns_above_zero_with_the_default_roster_and_a_dotted_worktree(live_layout):
    nudge = _live_squad_note(live_layout, LIVE_HOOKS_AGENT, "nudge", "squad", LIVE_NUDGE)
    cmd = _live_squad_note(live_layout, LIVE_HOOKS_AGENT, "cmd", "hand", "run the r5 check and report")
    _live_transcript(live_layout, LIVE_HOOKS_FOLDER, [
        _live_user(nudge["ts"] + 1, LIVE_NUDGE), _live_assistant(nudge["ts"] + 2),
        _live_user(cmd["ts"] + 1, "run the r5 check and report"), _live_assistant(cmd["ts"] + 3),
    ])
    env = {k: v for k, v in live_layout["env"].items() if k != "SQUAD_CONF"}   # the roster at $HOME/.config/squad/squad.conf
    r = _live_meter(live_layout, env=env)
    inj = r["measures"]["injected_turns"]
    assert inj["value"] is not None and inj["value"] > 0, f"injected_turns {inj['value']} on the live layout"
    assert (inj.get("per_lane") or {}).get(LIVE_HOOKS_FOLDER) == 2, inj.get("per_lane")
    assert r["measures"]["typed_turns"]["pane"]["value"] == 0, r["measures"]["typed_turns"]["pane"]


def test_r5_f4_live_c_rows_with_no_line_to_match_are_their_own_kind_not_unmatched(live_layout):
    t0 = time.time() - 60
    key = _live_squad_note(live_layout, LIVE_VORTEX_AGENT, "key", "hand", "Escape")
    answer = _live_squad_note(live_layout, LIVE_VORTEX_AGENT, "answer", "hand", "2")
    interrupt = _live_edge_note(live_layout, LIVE_VORTEX_AGENT, "edge_interrupt", "Escape")
    stray = _live_squad_note(live_layout, LIVE_OTHER_AGENT, "nudge", "squad", LIVE_NUDGE)       # typed, never landed
    _live_transcript(live_layout, LIVE_VORTEX_FOLDER, [_live_user(t0, LIVE_TIM), _live_assistant(t0 + 30)])
    _live_transcript(live_layout, LIVE_OTHER_FOLDER, [_live_user(t0, "features-json's own line"), _live_assistant(t0 + 30)])
    for row in (key, answer, interrupt, stray):
        assert row["lane"] in (LIVE_VORTEX_AGENT, LIVE_OTHER_AGENT)
    r = _live_meter(live_layout)
    m = r["measures"]
    assert m["typed_turns"]["pane"]["value"] == 2, m["typed_turns"]["pane"]
    assert m["injected_turns"]["value"] == 0, m["injected_turns"]
    flags = _live_unmatched(r)
    assert flags, "the nudge that matched no turn must still be flagged"
    for f in flags:
        assert LIVE_VORTEX_AGENT not in f["detail"] and LIVE_VORTEX_FOLDER not in f["detail"], (
            f"key/answer/edge_interrupt rows carry no line to match and were flagged unmatched: {f['detail']}")
    assert any(LIVE_OTHER_AGENT in f["detail"] or LIVE_OTHER_FOLDER in f["detail"] for f in flags), flags
    src = m["injected_turns"]["source"].replace(str(live_layout["tmp"]), "")   # paths are not an account
    for door in ("key", "answer", "edge_interrupt"):
        assert door in src, f"injected_turns' source does not account for the {door} row(s) as their own kind: {src}"


def test_r5_f4_live_d_record_appends_and_never_rewrites_the_file(live_layout):
    p = live_layout["ledger"]
    for i in range(3):
        assert _live_injections.record(LIVE_VORTEX_AGENT, "nudge", f"row {i}", path=p)
    before = p.read_bytes()
    inode = p.stat().st_ino
    assert _live_injections.record(LIVE_VORTEX_AGENT, "edge_prompt", "one more", by="edge", path=p)
    after = p.read_bytes()
    assert p.stat().st_ino == inode, "record() replaced the ledger file instead of appending to it"
    assert after.startswith(before), "record() rewrote earlier rows instead of appending"
    assert len(_live_injections.read_rows(p)) == 4


def test_r5_f4_live_guard_one_name_on_both_sides_matches_as_today_and_never_across_lanes(live_layout):
    t0 = time.time() - 300
    same = _live_squad_note(live_layout, "lane-a", "nudge", "squad", LIVE_NUDGE)            # b-44's fixture layout
    cross = _live_squad_note(live_layout, LIVE_OTHER_AGENT, "cmd", "hand", LIVE_PROMPT)           # mapped to LIVE_OTHER_FOLDER
    _live_transcript(live_layout, "lane-a", [_live_user(t0, LIVE_TIM), _live_assistant(t0 + 10),
                                 _live_user(same["ts"] + 1, LIVE_NUDGE), _live_assistant(same["ts"] + 2)])
    # The same text, in time, but in a DIFFERENT lane's folder: must not match the features-json row.
    _live_transcript(live_layout, LIVE_VORTEX_FOLDER, [_live_user(cross["ts"] + 1, LIVE_PROMPT), _live_assistant(cross["ts"] + 2)])
    r = _live_meter(live_layout)
    m = r["measures"]
    assert m["injected_turns"]["value"] == 1, m["injected_turns"]
    assert (m["injected_turns"].get("per_lane") or {}).get("lane-a") == 1, m["injected_turns"].get("per_lane")
    assert m["typed_turns"]["pane"]["per_lane"].get(LIVE_VORTEX_FOLDER) == 1, m["typed_turns"]["pane"]
    flags = _live_unmatched(r)
    assert flags and any(LIVE_OTHER_AGENT in f["detail"] or LIVE_OTHER_FOLDER in f["detail"] for f in flags), flags
