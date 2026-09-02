"""Goal 81, bars 50/51: detect at 50%, ASK the lane, never act alone.

His words: "some automatic detection that doesn't auto compact or auto clear.
That's not what I want." So the properties under test are mostly NEGATIVE —
what this must refuse to do:

  · never types into a lane that is not idle (working, waiting, unknown);
  · never fires on a lane whose ctx cannot be read (unmeasured != 0%);
  · never asks a third time;
  · the exec leg (bar 52) is OFF and stays off until it is built.

ctx% is not derived here: Claude Code hands the statusline
`context_window.used_percentage` and the pane renders it. These stub the pane
and assert on the KEYSTROKES, because the failure that matters is a keystroke
that should not have been sent.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

SQUAD = Path(__file__).resolve().parents[1] / "squad" / "squad"


def run(tmp_path, snippet, *, ctx="55", state_lines=None, agent="lane-a"):
    home = tmp_path
    (home / ".mcp-hub").mkdir(parents=True, exist_ok=True)
    conf = home / "squad.conf"
    conf.write_text(f"{agent}|{home}||--continue|squad\n", encoding="utf-8")

    bin_ = home / "bin"
    bin_.mkdir(exist_ok=True)

    # The pane: a statusline carrying ctx, plus whatever state chrome the
    # test wants classify_text to read.
    pane = f"⚡ 8/11 · Opus high · ctx [||||] {ctx}%\n" if ctx else "no statusline\n"
    pane += (state_lines or "")
    (bin_ / "tmux").write_text(
        "#!/bin/bash\n"
        f'echo "$@" >> {home}/tmux.log\n'
        'for a in "$@"; do\n'
        '  case "$a" in\n'
        f'    has-session) exit 0 ;;\n'
        f'    capture-pane) cat {home}/pane.txt; exit 0 ;;\n'
        f'    display-message) echo 12345; exit 0 ;;\n'
        '  esac\n'
        'done\n'
        'exit 0\n'
    )
    (bin_ / "tmux").chmod(0o755)
    (home / "pane.txt").write_text(pane)

    # pgrep/ps make agent_started answer, so the once-per-session flag has a key
    (bin_ / "pgrep").write_text("#!/bin/bash\necho 999\n")
    (bin_ / "pgrep").chmod(0o755)
    # etimes must TRACK the clock: agent_started is (now - etimes), so a
    # constant here makes the session key drift a second at a time and
    # every call reads as a fresh session. Cost me two false failures.
    (bin_ / "ps").write_text(
        "#!/bin/bash\necho $(( $(date +%s) - 1788000000 ))\n")
    (bin_ / "ps").chmod(0o755)
    # the door: record rows instead of sending them
    (bin_ / "mcp-hub").write_text(
        f'#!/bin/bash\ncat >> {home}/rows.txt\necho "$@" >> {home}/rowargs.txt\n')
    (bin_ / "mcp-hub").chmod(0o755)

    head = SQUAD.read_text(encoding="utf-8").split(
        '\ncase "${1:-help}" in', 1)[0]
    assert "compaction_one()" in head, "extraction boundary moved"
    script = home / "_head.sh"
    script.write_text(head + "\n" + snippet, encoding="utf-8")
    return subprocess.run(
        ["bash", str(script)], capture_output=True, text=True,
        env={"PATH": f"{bin_}:/usr/bin:/bin", "HOME": str(home),
             "SQUAD_CONF": str(conf)},
    )


def keys(tmp_path):
    log = tmp_path / "tmux.log"
    return log.read_text() if log.exists() else ""


def rows(tmp_path):
    f = tmp_path / "rows.txt"
    return f.read_text() if f.exists() else ""


# --- the ask, when it is allowed --------------------------------------------

def test_an_idle_lane_over_the_threshold_is_asked(tmp_path):
    p = run(tmp_path, "compaction_one lane-a", ctx="55")
    assert "compaction ask sent at 55%" in p.stdout
    assert "send-keys -l" in keys(tmp_path), "the ask text was never typed"
    assert "COMPACTION lane-a ask ctx=55%" in rows(tmp_path)


def test_the_fire_row_precedes_the_ask_row(tmp_path):
    """`fire` is detection; `ask` is delivery. An ask with no fire would hide
    the detections that never reached a lane."""
    run(tmp_path, "compaction_one lane-a", ctx="55")
    body = rows(tmp_path)
    assert body.index("fire ctx=55%") < body.index("ask ctx=55%")


def test_the_ask_names_the_real_percentage(tmp_path):
    run(tmp_path, "compaction_one lane-a", ctx="57")
    assert "57% of your context window" in keys(tmp_path)


def test_the_ask_demands_a_flush_before_the_answer(tmp_path):
    """`/clear` destroys context; the flush is what makes it survivable."""
    run(tmp_path, "compaction_one lane-a", ctx="55")
    typed = keys(tmp_path)
    assert "Flush anything worth keeping to memory" in typed
    assert "CLEAR" in typed and "COMPACT" in typed


def test_the_text_and_the_Enter_are_separate_sends(tmp_path):
    """`send-keys "<text>" Enter` makes tmux read the literal as a KEY NAME
    wherever it matches one — the bug that typed `-tmcp-hub-dev-vm-1` into two
    lanes on 2026-08-28."""
    run(tmp_path, "compaction_one lane-a", ctx="55")
    lines = [ln for ln in keys(tmp_path).splitlines() if "send-keys" in ln]
    assert any("send-keys -l" in ln for ln in lines), lines
    assert any(ln.strip().endswith("Enter") and "send-keys -l" not in ln
               for ln in lines), lines


# --- the ask, when it must NOT happen ---------------------------------------

@pytest.mark.parametrize("chrome,why", [
    ("Nucleating… (1m 55s · ↓ 6.4k tokens)\n", "working"),
    ("❯ 1. Yes\n  2. No\n", "waiting on a dialog"),
])
def test_a_lane_that_is_not_idle_is_never_typed_into(tmp_path, chrome, why):
    """The whole safety property. A send-keys into a working lane appends to
    its input box; into a dialog it answers a question that is the
    operator's."""
    p = run(tmp_path, "compaction_one lane-a", ctx="55", state_lines=chrome)
    assert "not asking this pass (idle only)" in p.stdout, why
    assert "send-keys" not in keys(tmp_path)


def test_a_busy_lane_STILL_records_the_detection(tmp_path):
    """Detection and delivery are separate facts — a fire with no ask is
    exactly the row that shows the ask never landed."""
    run(tmp_path, "compaction_one lane-a", ctx="55",
        state_lines="Nucleating… (1m 55s · ↓ 6.4k tokens)\n")
    assert "fire ctx=55%" in rows(tmp_path)
    assert "ask ctx=" not in rows(tmp_path)


def test_a_lane_below_the_threshold_is_left_alone(tmp_path):
    p = run(tmp_path, "compaction_one lane-a", ctx="34")
    assert p.stdout.strip() == ""
    assert "send-keys" not in keys(tmp_path)
    assert rows(tmp_path) == ""


def test_an_unreadable_ctx_is_UNMEASURED_not_zero(tmp_path):
    """A pane we cannot read must never present as an empty context — that
    would make a blind instrument look like a healthy one."""
    p = run(tmp_path, "compaction_one lane-a", ctx="")
    assert p.stdout.strip() == ""
    assert rows(tmp_path) == ""


# --- once per session, then the re-ask, then silence ------------------------

def test_the_same_session_is_not_asked_twice_at_the_same_mark(tmp_path):
    run(tmp_path, "compaction_one lane-a\ncompaction_one lane-a", ctx="55")
    assert keys(tmp_path).count("send-keys -l") == 1


def test_it_re_asks_once_at_sixty(tmp_path):
    p = run(tmp_path, "compaction_one lane-a\ncompaction_one lane-a", ctx="62")
    assert keys(tmp_path).count("send-keys -l") == 2, p.stdout


def test_there_is_never_a_third_ask(tmp_path):
    """Nagging past the re-ask is the 'auto' behaviour he ruled out, one step
    removed."""
    run(tmp_path, "compaction_one lane-a\n" * 4, ctx="70")
    assert keys(tmp_path).count("send-keys -l") == 2


# --- bar 52 is not built ----------------------------------------------------

def test_the_exec_leg_is_off_by_default(tmp_path):
    p = run(tmp_path, 'echo "exec=$COMPACT_EXEC"', ctx="55")
    assert "exec=0" in p.stdout


def test_nothing_types_a_slash_command(tmp_path):
    """bar 52 is unbuilt: no /clear or /compact may reach a pane."""
    run(tmp_path, "compaction_one lane-a", ctx="55")
    typed = keys(tmp_path)
    assert "/clear" not in typed and "/compact" not in typed
