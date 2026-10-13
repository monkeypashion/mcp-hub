"""F6 (j6-conversation-exemption): a mechanism does not stop a lane mid-talk.

A hold placed by a MECHANISM — a brake, the hibernation scanner — must not
stop a lane in the middle of a conversation with Tim. He asks a question, the
boundary stop lands on the answer, and the pane he is typing into is gone,
with the thing that killed it being the only party that could have told him.

⭐ ONLY A HOLD WITH AN `owner` DEFERS, and the asymmetry is the whole rule.
An `owner` names the mechanism that placed the hold. A hold with none was
placed BY HAND — that is his own act, and deferring it would be the tool
overruling the person it is protecting him from.

🔴 AN INJECTED TURN IS NOT A CONVERSATION. A `squad cmd` line and a heal
nudge land in a transcript in exactly the shape of a line Tim typed, so
without F4's ledger the fleet's own nudges would read as him talking — and a
lane could keep ITSELF exempt from the brake by being nudged. That is the
coupling between the two features, and it is the one worth pinning.

⚠️ NO MECHANISM PLACES HOLDS TODAY (the hibernation timer is inactive; the
share brake has been off since 2026-09-23). This is a guard built before it
is needed, so the first mechanism to place one is not what discovers the gap.

The r5 falsifiers drive squad's real enforcement pass; this file pins the
question of fact and the record the stop hook reads.
"""
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mcp_hub import conversation, hold, injections

ROOT = Path(__file__).resolve().parents[1]
WINDOW = conversation.DEFAULT_WINDOW_SECONDS


def _iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z")


def _user(epoch, text, kind="human"):
    return json.dumps({"type": "user", "timestamp": _iso(epoch),
                       "message": {"role": "user", "content": text},
                       "origin": {"kind": kind}})


def _assistant(epoch, text="ok"):
    return json.dumps({"type": "assistant", "timestamp": _iso(epoch),
                       "message": {"role": "assistant",
                                   "content": [{"type": "text", "text": text}]}})


class Rig:
    """One lane, its worktree, its transcript dir and the F4 ledger."""

    def __init__(self, tmp, lane="lane-a"):
        self.lane = lane
        self.home = tmp / "home"
        self.worktree = self.home / "w" / lane
        self.worktree.mkdir(parents=True)
        self.dir = (self.home / ".claude" / "projects"
                    / str(self.worktree).replace("/", "-"))
        self.dir.mkdir(parents=True)
        self.ledger = tmp / "injections.jsonl"

    def transcript(self, lines, name="s1.jsonl", mtime=None):
        path = self.dir / name
        path.write_text("\n".join(lines) + "\n")
        if mtime is not None:
            import os
            os.utime(path, (mtime, mtime))
        return path

    def inject(self, epoch, text):
        injections.record(self.lane, "nudge", text, now=epoch, path=self.ledger)

    def read(self, now=None):
        rows = conversation.read([(self.lane, str(self.worktree))],
                                 now=now, home=self.home,
                                 ledger_path=self.ledger)
        return rows[0]


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


# --- the question of fact --------------------------------------------------

def test_a_lane_typed_to_inside_the_window_is_in_conversation(rig):
    now = time.time()
    rig.transcript([_user(now - 300, "what about F6?"), _assistant(now - 280)])
    v = rig.read(now=now)
    assert v["in_conversation"] is True
    assert v["conversation_end"] == pytest.approx(now - 300 + WINDOW, abs=1)


def test_the_conversations_end_is_the_last_typed_turn_plus_the_window(rig):
    """What the hard-stop clock runs from. A lane held at 12:00 that he then
    talked to until 12:20 has not spent twenty minutes ignoring the hold."""
    now = time.time()
    rig.transcript([_user(now - WINDOW - 60, "earlier"), _assistant(now - WINDOW - 40)])
    v = rig.read(now=now)
    assert v["in_conversation"] is False, "60 s past the window is past it"
    assert v["conversation_end"] == pytest.approx(now - 60, abs=1)


def test_a_turn_the_ledger_marks_injected_is_not_conversation(rig):
    """Without this a lane could keep itself exempt from the brake by being
    nudged: a heal nudge is indistinguishable from a typed line in the
    transcript, and it arrives exactly when the lane is in trouble."""
    now = time.time()
    nudge = "hub: your session shows offline. Call register() now"
    rig.inject(now - 302, nudge)
    rig.transcript([_user(now - WINDOW - 600, "an old question"),
                    _assistant(now - WINDOW - 580),
                    _user(now - 300, nudge), _assistant(now - 280)])
    v = rig.read(now=now)
    assert v["in_conversation"] is False, (
        "a squad-typed nudge kept the lane exempt from its own brake")
    assert v["conversation_end"] == pytest.approx(now - WINDOW - 600 + WINDOW, abs=1)


def test_the_search_walks_back_past_injected_turns_to_a_real_one(rig):
    now = time.time()
    nudge = "hub: your session shows offline. Call register() now"
    rig.inject(now - 122, nudge)
    rig.transcript([_user(now - 400, "a real question"), _assistant(now - 380),
                    _user(now - 120, nudge), _assistant(now - 100)])
    v = rig.read(now=now)
    assert v["in_conversation"] is True
    assert v["conversation_end"] == pytest.approx(now - 400 + WINDOW, abs=1)


def test_a_ledger_row_after_its_turn_does_not_mark_it_injected(rig):
    """A row is written BEFORE the keystroke. One that follows the turn
    describes a different event, however well the text agrees."""
    now = time.time()
    rig.inject(now - 290, "what about F6?")        # 10 s AFTER the turn
    rig.transcript([_user(now - 300, "what about F6?"), _assistant(now - 280)])
    assert rig.read(now=now)["in_conversation"] is True


def test_without_a_ledger_every_turn_reads_as_typed(rig, tmp_path):
    """🔴 Fails towards KEEPING THE LANE UP. Nothing can be shown to be
    injected, so the lane stays in conversation and the mechanism waits —
    the safe way round for an exemption."""
    now = time.time()
    rig.ledger = tmp_path / "absent.jsonl"
    rig.transcript([_user(now - 300, "a nudge, for all we know"),
                    _assistant(now - 280)])
    assert rig.read(now=now)["in_conversation"] is True


def test_a_hub_wake_is_not_a_conversation(rig):
    """origin.kind=channel is the hub talking, not Tim."""
    now = time.time()
    rig.transcript([_user(now - 300, "queued DM", kind="channel"),
                    _assistant(now - 280)])
    v = rig.read(now=now)
    assert v["in_conversation"] is False and v["conversation_end"] == 0


def test_a_lane_with_no_transcript_at_all_is_not_in_conversation(rig):
    v = rig.read()
    assert v["in_conversation"] is False
    assert v["conversation_end"] == 0, (
        "0 means no typed turn, so the clock falls back to the hold's own "
        "held_at — exactly as before this feature")


def test_the_newest_transcript_is_the_one_modified_last(rig):
    """Session ids are uuids and sort arbitrarily, so a name order would read
    a resumed lane's oldest session as its current one."""
    now = time.time()
    rig.transcript([_user(now - 300, "the live session"), _assistant(now - 280)],
                   name="zzz.jsonl", mtime=now)
    rig.transcript([_user(now - WINDOW - 600, "an abandoned session"),
                    _assistant(now - WINDOW - 580)],
                   name="aaa.jsonl", mtime=now - 90000)
    assert rig.read(now=now)["in_conversation"] is True


def test_only_the_tail_is_read_and_a_torn_first_line_is_skipped(rig):
    """A long-running lane's session file reaches tens of megabytes; a
    bounded read is what keeps a 46-lane pass inside its CPU budget."""
    now = time.time()
    filler = [_assistant(now - 7200, "x" * 900)
              for _ in range(conversation.TAIL_BYTES // 900 + 40)]
    rig.transcript([*filler, _user(now - 300, "the newest line"),
                    _assistant(now - 280)])
    v = rig.read(now=now)
    assert v["in_conversation"] is True
    path = rig.dir / "s1.jsonl"
    assert path.stat().st_size > conversation.TAIL_BYTES
    lines = conversation.tail_lines(path)
    assert len("\n".join(lines)) <= conversation.TAIL_BYTES
    assert all(json.loads(ln) for ln in lines), "a torn line survived the tail"


def test_a_tool_result_is_not_a_typed_turn(rig):
    now = time.time()
    rig.transcript([json.dumps({
        "type": "user", "timestamp": _iso(now - 300), "origin": {"kind": "human"},
        "toolUseResult": {"stdout": "ok"},
        "message": {"role": "user", "content": "tool output"}})])
    assert rig.read(now=now)["in_conversation"] is False


def test_the_window_is_configurable_and_decides_the_verdict(rig):
    now = time.time()
    rig.transcript([_user(now - 900, "fifteen minutes ago"), _assistant(now - 880)])
    assert conversation.read([(rig.lane, str(rig.worktree))], now=now,
                             window=600, home=rig.home,
                             ledger_path=rig.ledger)[0]["in_conversation"] is False
    assert conversation.read([(rig.lane, str(rig.worktree))], now=now,
                             window=1200, home=rig.home,
                             ledger_path=rig.ledger)[0]["in_conversation"] is True


# --- the shape squad reads -------------------------------------------------

def test_the_cli_prints_one_flat_line_per_lane_asked(rig):
    """squad reads this with `read` in a while loop and must not need jq to
    learn whether Tim is talking."""
    now = time.time()
    rig.transcript([_user(now - 300, "hello"), _assistant(now - 280)])
    p = subprocess.run(
        [sys.executable, "-m", "mcp_hub.conversation",
         f"{rig.lane}={rig.worktree}", "lane-b=/nowhere",
         "--injections", str(rig.ledger)],
        capture_output=True, text=True,
        env={"PYTHONPATH": str(ROOT / "src"), "HOME": str(rig.home),
             "PATH": "/usr/bin:/bin"})
    assert p.returncode == 0, p.stderr
    lines = p.stdout.strip().splitlines()
    assert len(lines) == 2, p.stdout
    lane, inc, end = lines[0].split()
    assert lane == "lane-a" and inc == "1" and float(end) > now
    assert lines[1].split()[:2] == ["lane-b", "0"], (
        "a lane with no transcript must still get a line — a missing row "
        "would read as 'not asked' and squad would scan it per lane")


def test_there_is_no_new_hub_verb_for_this(rig):
    """server.py is frozen for r5, and nothing here needs the hub: squad
    reaches `python -m mcp_hub.conversation` directly."""
    squad = (ROOT / "squad" / "squad").read_text(encoding="utf-8")
    assert "-m mcp_hub.conversation" in squad
    assert "mcp-hub conversation" not in squad
    from mcp_hub import server
    assert not hasattr(server, "conversation")


def test_squad_asks_once_per_pass_not_once_per_lane():
    """At 46 held lanes a call per lane would put 46 python forks inside a
    two-minute timer, which is the cost this feature is bounded on."""
    squad = (ROOT / "squad" / "squad").read_text(encoding="utf-8")
    import re
    pass_body = re.search(r"\nhold_enforce_pass\(\) \{(.*?)\n\}", squad, re.S)
    assert pass_body, "no hold_enforce_pass"
    assert "conversation_scan" in pass_body.group(1)
    one = re.search(r"\nhold_enforce_one\(\) \{(.*?)\n\}", squad, re.S)
    assert one and "in_conversation" in one.group(1)
    # Asked for the OWNER-held lanes only: a fleet held entirely by hand
    # costs the pass nothing, because the exemption is bounded on what it adds.
    assert "held_owner" in pass_body.group(1)


# --- the record the stop hook reads ---------------------------------------

def test_the_deferral_is_a_deadline_not_a_flag():
    """A squad that stops running must not silence the 'being stopped'
    notice forever — the same reason focus mode stores an expiry."""
    hold.record_deferral("lane-a", until=1.0, owner="brake")
    assert hold.deferred("lane-a") is True
    hold.record_deferral("lane-a", until=1.0, owner="brake",
                         now=time.time() - hold.DEFERRAL_TTL_SECONDS - 1)
    assert hold.deferred("lane-a") is False, "a lapsed deferral stayed silent"


def test_clearing_the_deferral_lets_the_notice_through_again():
    hold.record_deferral("lane-a", until=1.0, owner="brake")
    hold.clear_deferral("lane-a")
    assert hold.deferred("lane-a") is False
    hold.clear_deferral("lane-a")          # idempotent: no record is not an error


def test_an_unreadable_deferral_record_fails_open():
    """Fails open as NOT deferred: the lane hears the notice, which is the
    direction that loses nothing."""
    path = hold.deferral_path("lane-a")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ this is not json")
    assert hold.deferred("lane-a") is False


def test_squad_and_the_hook_agree_on_the_records_filename():
    """squad writes `$a.json` with the roster's spelling of the lane. Two
    spellings of one filename is a record that reads as absent."""
    squad = (ROOT / "squad" / "squad").read_text(encoding="utf-8")
    assert "hold-deferred" in squad
    assert str(hold.deferral_path("lane-a")).endswith(
        "hold-deferred/lane-a.json")
