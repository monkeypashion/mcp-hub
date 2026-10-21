"""v2-fleet r11 F45 / T59 v8 (j6-inboxed-ask-and-nudge-held-once): an ask or nudge left in the lane
inbox is put there once, and a pane a person is in gets no 'typing' row.

Frozen falsifiers, cut on r10's built head b-132 5d5f9b51 (= merge-prep 7d7346bc plus stages/).
ALREADY MET there and kept as GUARDS (1)-(3): the ask door writes $flag and one ask row naming the
inbox, so later passes do not ask again and the lane's reply to the inboxed ask is read as its answer;
compaction_type inboxes only an ask, never a slash command. STILL OPEN, the FALSIFIERS:

  (4) the exec leg, armed with the pane in use, writes 'typing /clear' before compaction_type finds the
      pane in use, and past the deadline with the lane busy first writes 'DEADLINE: ... typing anyway';
  (5) heal's nudge door puts on every pass while the pane is in use (no flag, deliberately not charged);
  (6) `python -m mcp_hub.lane_inbox put` has no dedupe: a second put for the same lane and door writes a
      second file, exit 0, saying nothing.

The fix: `lane_inbox put` refuses a second item from the same door for the same lane (exit 0, no new
file, and says so); the exec leg checks pane_in_use before any row and, on a pane in use, writes one
'NOT EXECUTED: pane in use' row only, and tries again on a later pass.

pane_in_use is made TRUE here the way squad reads it: `tmux list-clients` answers an attached client
active this second. The compaction harness is tests/test_compaction_detect_and_ask.py's `run`; heal's
nudge door runs on the r5 scratch fleet (a real private tmux socket and an attached pty client).
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from mcp_hub import lane_inbox
from tests.test_compaction_detect_and_ask import (
    BUSY, PINNED_NOW, SAFE_PANE, answered, commands_typed, keys, pinned_now, rows, run,
)
from tests.test_v2fleet_r5_falsifiers import IDLE_SCREEN, NUDGE_MARK, Fleet, Hook

ROOT = Path(__file__).resolve().parents[1]
SRC = str(ROOT / "src")
EXEC_ON = {"MCP_HUB_COMPACTION_EXEC": "1"}
DEADLINE_NOW = {"MCP_HUB_COMPACTION_EXEC": "1", "MCP_HUB_COMPACT_EXEC_DEADLINE": "0"}


# --- harness ---------------------------------------------------------------------------------

def harness(tmp_path, snippet, *, in_use=True, env=None, **kw):
    """`run` with two additions: a `python` beside the stub `mcp-hub` (squad's hub_python finds it
    there) that can import mcp_hub, so lane_inbox_put really writes; and a tmux in front of the
    harness's own that answers `list-clients` with a client active NOW while `in_use` holds."""
    bin_ = tmp_path / "bin"
    bin_.mkdir(parents=True, exist_ok=True)
    py = bin_ / "python"
    py.write_text(f"#!/bin/bash\nexport PYTHONPATH={SRC}\nexec {sys.executable} \"$@\"\n")
    py.chmod(0o755)
    over = tmp_path / "over"
    over.mkdir(exist_ok=True)
    flag = tmp_path / "in_use"
    (over / "tmux").write_text(
        "#!/bin/bash\n"
        'case " $* " in\n'
        f'  *" list-clients "*) [ -f {flag} ] && echo "$(/usr/bin/date +%s) /dev/pts/9"; exit 0 ;;\n'
        "esac\n"
        f'exec {bin_}/tmux "$@"\n')
    (over / "tmux").chmod(0o755)
    if in_use:
        flag.touch()
    else:
        flag.unlink(missing_ok=True)
    e = {"PATH": f"{over}:{bin_}:/usr/bin:/bin"}
    e.update(env or {})
    return run(tmp_path, snippet, env=e, **kw)


def _door(name):
    """The door of an inbox file: lane_inbox names it <time>-<door>-<pid>[-n].txt."""
    m = re.match(r"^[0-9.]+-(.+?)-[0-9]+(?:-[0-9]+)?\.txt$", name)
    return m.group(1) if m else None


def inbox(home, lane="lane-a"):
    """[(name, body)] waiting for `lane`, body without the HEADER line."""
    d = home / ".mcp-hub" / "lane-inbox" / lane
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.iterdir()):
        if p.is_file():
            text = p.read_text(encoding="utf-8", errors="replace")
            body = text.split("\n", 1)[1] if text.startswith(lane_inbox.HEADER) else text
            out.append((p.name, body.strip()))
    return out


def row_list(tmp_path):
    """[(step, body)] for each COMPACTION row the door received, in order."""
    out = []
    for chunk in rows(tmp_path).split("COMPACTION ")[1:]:
        head, _, body = chunk.partition("\n")
        parts = head.split()
        out.append((parts[1] if len(parts) > 1 else "", chunk))
    return out


# --- (1) (2) the ask door: one item, one row, and a flag with the ask time ----------------------

def test_r11_t59_1_four_passes_leave_one_ask_in_the_inbox_and_one_ask_row(tmp_path):
    p = harness(tmp_path, "compaction_one lane-a\n" * 4, pane=SAFE_PANE)
    items = [n for n, _b in inbox(tmp_path) if _door(n) == "compaction_type"]
    assert len(items) == 1, f"{len(items)} compaction asks in the lane inbox after 4 passes:\n{p.stdout}"
    asks = [b for s, b in row_list(tmp_path) if s == "ask"]
    assert len(asks) == 1 and "left in the lane inbox" in asks[0], f"ask rows: {asks}"
    assert "send-keys" not in keys(tmp_path), "the ask was typed into a pane in use"


def test_r11_t59_2_the_flag_holds_the_ask_time(tmp_path):
    p = harness(tmp_path, "compaction_one lane-a", pane=SAFE_PANE)
    flags = [f for f in (tmp_path / ".mcp-hub").glob("compaction-lane-a-*") if "." not in f.name]
    assert len(flags) == 1 and flags[0].is_file() and flags[0].read_text().strip(), (
        f"no flag with the ask time after the ask went to the inbox: {flags}\n{p.stdout}")
    at = int(flags[0].read_text().strip())
    assert PINNED_NOW - 5 <= at <= pinned_now() + 5, f"the flag holds {at}, not the ask time"


# --- (3) the inboxed ask can be answered ---------------------------------------------------------

def test_r11_t59_3_a_reply_to_an_inboxed_ask_is_read_as_the_answer(tmp_path):
    harness(tmp_path, "compaction_one lane-a\n" * 3, pane=SAFE_PANE,
            replies=answered("CLEAR because the work is parked and flushed."))
    steps = row_list(tmp_path)
    assert any(s == "answer" and "lane answered CLEAR" in b for s, b in steps), \
        f"the reply to the inboxed ask was never read: {steps}"
    assert any(s == "exec" and "exec leg disarmed" in b for s, b in steps), steps
    assert "send-keys" not in keys(tmp_path), "something was typed with the exec leg off"


# --- (4) the exec leg never inboxes a slash command, and writes no 'typing' row ------------------

def _no_slash_item(tmp_path):
    bodies = [b for _n, b in inbox(tmp_path)]
    assert not any(b in ("/clear", "/compact") for b in bodies), f"a slash command in the inbox: {bodies}"


def test_r11_t59_4_an_armed_exec_on_a_pane_in_use_is_not_executed_and_retried(tmp_path):
    """Three armed passes, the pane in use throughout: the ask goes to the inbox, the reply is read,
    and every exec pass after it writes NOT EXECUTED, never 'typing /clear'. Then, the pane free, a
    later pass types it."""
    harness(tmp_path, "compaction_one lane-a\n" * 3, in_use=True, pane=SAFE_PANE, env=EXEC_ON,
            replies=answered("CLEAR because the work is parked and flushed."))
    _no_slash_item(tmp_path)
    steps = row_list(tmp_path)
    assert any(s == "answer" and "lane answered CLEAR" in b for s, b in steps), steps
    assert not any("typing /clear" in b for _s, b in steps), f"a 'typing /clear' row on a pane in use: {steps}"
    execs = [b for s, b in steps if s == "exec"]
    assert execs and all("NOT EXECUTED: pane in use" in b for b in execs), f"exec rows: {execs}"
    assert commands_typed(tmp_path) == []
    # ...and a later pass, the pane free, types it.
    harness(tmp_path, "compaction_one lane-a", in_use=False, pane=SAFE_PANE, env=EXEC_ON,
            replies=answered("CLEAR because the work is parked and flushed."))
    assert commands_typed(tmp_path) == ["/clear"], keys(tmp_path)


def test_r11_t59_4_past_the_deadline_a_pane_in_use_gets_no_typing_anyway_row(tmp_path):
    """$flag.answer older than the deadline (0), the lane busy, the pane in use: one NOT EXECUTED row,
    and no 'typing anyway' and no 'typing /clear' row."""
    harness(tmp_path, "compaction_one lane-a", in_use=True, pane=SAFE_PANE, env=DEADLINE_NOW)  # ask inboxed
    harness(tmp_path, "compaction_one lane-a", in_use=True, env=DEADLINE_NOW, state_lines=BUSY,
            replies=answered("CLEAR"))
    steps = row_list(tmp_path)
    assert any(s == "answer" and "lane answered CLEAR" in b for s, b in steps), steps
    assert not any("typing anyway" in b for _s, b in steps), f"a 'typing anyway' row on a pane in use: {steps}"
    _no_slash_item(tmp_path)
    assert not any("typing /clear" in b for _s, b in steps), f"a 'typing /clear' row on a pane in use: {steps}"
    refused = [b for s, b in steps if s == "exec" and "NOT EXECUTED: pane in use" in b]
    assert len(refused) == 1, f"exec rows: {[b for s, b in steps if s == 'exec']}"
    assert commands_typed(tmp_path) == []


# --- (5) heal's nudge door: one item, nothing charged ---------------------------------------------

def test_r11_t59_5_four_heal_passes_on_a_pane_in_use_leave_one_nudge(tmp_path):
    f = Fleet(tmp_path / "fleet", {"lane-a": IDLE_SCREEN})
    try:
        f.attach("lane-a")
        f.status("lane-a", online=False)
        for _ in range(4):
            p = f.squad("heal")
            assert p.returncode == 0, p.stderr[-400:]
        d = f.state / "lane-inbox" / "lane-a"
        nudges = sorted(x.name for x in d.iterdir() if _door(x.name) == "nudge") if d.is_dir() else []
        assert len(nudges) == 1, f"{len(nudges)} nudges in the lane inbox after 4 heal passes"
        assert NUDGE_MARK in f.inbox_text()
        cnt = f.state / "heal-nudges-lane-a"
        assert (cnt.read_text().strip() if cnt.exists() else "0") in ("", "0"), "a refused nudge was charged"
        assert not (f.state / "heal-flag-lane-a").exists(), "a refused nudge armed the strike"
        assert f.lines(0, "SEND", "lane-a") == [], "heal typed into a pane in use"
    finally:
        f.close()


# --- (6) (7) lane_inbox put: same door + same lane once ------------------------------------------

def _put(state, lane, door, text="hello"):
    env = {**os.environ, "MCP_HUB_STATE_DIR": str(state), "PYTHONPATH": SRC}
    return subprocess.run([sys.executable, "-m", "mcp_hub.lane_inbox", "put", lane, door, "-"],
                          input=text, capture_output=True, text=True, env=env)


def _files(state, lane):
    d = state / "lane-inbox" / lane
    return sorted(p.name for p in d.iterdir() if p.is_file()) if d.is_dir() else []


def test_r11_t59_6_a_second_put_from_the_same_door_is_refused_and_says_so(tmp_path):
    state = tmp_path / "state"
    first = _put(state, "lane-a", "compaction_type", "ask one")
    assert first.returncode == 0, first.stderr
    second = _put(state, "lane-a", "compaction_type", "ask two")
    assert second.returncode == 0, f"the refusal is not exit 0: {second.returncode} {second.stderr}"
    assert len(_files(state, "lane-a")) == 1, f"two puts from one door left {_files(state, 'lane-a')}"
    assert (second.stdout + second.stderr).strip(), "the refused put did not say so"


def test_r11_t59_6_a_different_door_or_lane_still_gets_its_own(tmp_path):
    state = tmp_path / "state"
    assert _put(state, "lane-a", "compaction_type").returncode == 0
    assert _put(state, "lane-a", "nudge").returncode == 0
    assert _put(state, "lane-b", "compaction_type").returncode == 0
    assert len(_files(state, "lane-a")) == 2, _files(state, "lane-a")
    assert len(_files(state, "lane-b")) == 1, _files(state, "lane-b")


def test_r11_t59_7_after_a_stop_drain_the_next_put_lands(tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setenv("MCP_HUB_STATE_DIR", str(state))
    assert _put(state, "lane-a", "nudge", "first nudge").returncode == 0
    text, _ = Hook(tmp_path / "hook", monkeypatch).stop(online=True)
    assert text is not None and "first nudge" in text, text
    assert _files(state, "lane-a") == [], "the Stop did not clear the inbox"
    assert _put(state, "lane-a", "nudge", "second nudge").returncode == 0
    assert len(_files(state, "lane-a")) == 1, "after a drain the door could not put again"


# --- CONTROL: the pane free, the ask is typed as on r10 -------------------------------------------

def test_r11_t59_control_with_the_pane_free_the_ask_is_typed(tmp_path):
    p = harness(tmp_path, "compaction_one lane-a", in_use=False, pane=SAFE_PANE)
    assert "compaction ask sent at" in p.stdout, p.stdout + p.stderr
    assert "send-keys -l" in keys(tmp_path)
    assert inbox(tmp_path) == []
