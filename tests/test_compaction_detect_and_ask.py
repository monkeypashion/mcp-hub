"""Goal 81, bars 50/51/52: detect on TOKENS, ASK the lane, act only on its answer.

His words: "some automatic detection that doesn't auto compact or auto clear.
That's not what I want." So the properties under test are mostly NEGATIVE —
what this must refuse to do:

  · never types into a lane that is not idle (working, waiting, unknown);
  · never fires on a lane whose transcript cannot be read (unmeasured != 0);
  · never asks twice in one session;
  · never types a slash command without a verdict the LANE wrote;
  · never types one when the exec leg is disarmed;
  · never resolves a reply that names both words.

The trip point is ABSOLUTE TOKENS off the lane's own transcript, not the
statusline percent — a ~1M-window lane sat at 169,255 tokens showing 16%, so
every percent mark this fleet had would have let it sail past a token cap
unasked. These tests therefore write a real transcript fixture and assert on
the KEYSTROKES, because the failure that matters is a keystroke that should
not have been sent.
"""
from __future__ import annotations

import datetime
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

SQUAD = Path(__file__).resolve().parents[1] / "squad" / "squad"

# well under / well over the 150k cap
UNDER, OVER = 40_000, 169_255


def assistant(ts, text, tokens):
    """One assistant record. Usage is SPLIT across the three fields the
    reading sums, so a version that counted only input_tokens would read a
    169k lane as 2."""
    msg = {"content": [{"type": "text", "text": text}]}
    if tokens is not None:
        msg["usage"] = {"input_tokens": 2,
                        "cache_read_input_tokens": tokens - 2_043,
                        "cache_creation_input_tokens": 2_041}
    return {"type": "assistant", "timestamp": ts, "message": msg}


def transcript(home: Path, worktree: Path, *, tokens=None, replies=()):
    """Write the lane's Claude Code transcript the way the client encodes it:
    ~/.claude/projects/<worktree with / -> ->/<session>.jsonl.

    Every assistant turn carries the same reading, because a lane that
    answers the ask has not thereby shrunk — the reading only moves once
    something is actually executed."""
    d = home / ".claude" / "projects" / str(worktree).replace("/", "-")
    d.mkdir(parents=True, exist_ok=True)
    rows = [assistant("2026-09-02T18:00:00.000Z", "working", tokens)]
    rows += [assistant(ts, text, tokens) for ts, text in replies]
    f = d / "05a50d0c-1111-2222-3333-444455556666.jsonl"
    f.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return f


def run(tmp_path, snippet, *, tokens=OVER, state_lines=None, agent="lane-a",
        replies=(), env=None, ctx="16", jitter=False, klass="squad"):
    home = tmp_path
    (home / ".mcp-hub").mkdir(parents=True, exist_ok=True)
    conf = home / "squad.conf"
    conf.write_text(f"{agent}|{home}||--continue|{klass}\n", encoding="utf-8")

    transcript(home, home, tokens=tokens, replies=replies)

    bin_ = home / "bin"
    bin_.mkdir(exist_ok=True)

    # The pane: a statusline carrying ctx (decoration now, not the trigger),
    # plus whatever state chrome the test wants classify_text to read.
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
    # `jitter` reproduces the real clock: etimes is TRUNCATED whole seconds,
    # so (now - etimes) alternates by one between passes and every other pass
    # computes a different session key for the same session.
    if jitter:
        (bin_ / "ps").write_text(
            "#!/bin/bash\n"
            f'n=$(cat {home}/pscalls 2>/dev/null || echo 0)\n'
            f'echo $(( n + 1 )) > {home}/pscalls\n'
            "b=$(( $(date +%s) - 1788000000 ))\n"
            '[ $(( n % 2 )) -eq 1 ] && b=$(( b - 1 ))\n'
            "echo $b\n")
    else:
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
    e = {"PATH": f"{bin_}:/usr/bin:/bin", "HOME": str(home),
         "SQUAD_CONF": str(conf)}
    e.update(env or {})
    return subprocess.run(["bash", str(script)], capture_output=True,
                          text=True, env=e)


def keys(tmp_path):
    log = tmp_path / "tmux.log"
    return log.read_text() if log.exists() else ""


def rows(tmp_path):
    f = tmp_path / "rows.txt"
    return f.read_text() if f.exists() else ""


def stamp(offset):
    """An ISO timestamp `offset` seconds from now. The ask records the REAL
    clock, so a fixture pinned to a literal date is in the past by the time
    the suite runs and no reply is ever attributable to it."""
    return (datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(seconds=offset)).isoformat().replace(
                "+00:00", "Z")


def answered(text, *, offset=60):
    """A reply the lane writes into its own transcript AFTER the ask."""
    return ((stamp(offset), text),)


ARMED = {"MCP_HUB_COMPACTION_EXEC": "1"}


# --- the instrument ---------------------------------------------------------

def test_the_reading_sums_all_three_usage_fields(tmp_path):
    """input + cache_read + cache_creation — the same three the board sums.
    Counting only input_tokens reads a 169k lane as 2."""
    p = run(tmp_path, "compaction_tokens lane-a", tokens=OVER)
    assert p.stdout.strip() == str(OVER)


def test_a_lane_under_the_cap_is_left_alone(tmp_path):
    p = run(tmp_path, "compaction_one lane-a", tokens=UNDER)
    assert p.stdout.strip() == ""
    assert "send-keys" not in keys(tmp_path)
    assert rows(tmp_path) == ""


def test_the_percent_does_not_decide(tmp_path):
    """169,255 tokens on a ~1M lane renders 16%. Bars 50/51 tripped on the
    percent and would never have fired here — that lane was this one."""
    p = run(tmp_path, "compaction_one lane-a", tokens=OVER, ctx="16")
    assert "compaction ask sent at 169255 tokens" in p.stdout


def test_an_unreadable_transcript_is_UNMEASURED_not_zero(tmp_path):
    """A lane we cannot measure must never present as an empty context —
    that would make a blind instrument look like a healthy one."""
    p = run(tmp_path, "compaction_one lane-a", tokens=None)
    assert p.stdout.strip() == ""
    assert rows(tmp_path) == ""
    assert "send-keys" not in keys(tmp_path)


# --- the ask ----------------------------------------------------------------

def test_an_idle_lane_over_the_cap_is_asked(tmp_path):
    p = run(tmp_path, "compaction_one lane-a")
    assert "compaction ask sent at" in p.stdout
    assert "send-keys -l" in keys(tmp_path), "the ask text was never typed"
    assert "COMPACTION lane-a ask " in rows(tmp_path)


def test_every_row_carries_the_tokens_and_the_cap(tmp_path):
    """`ctx=` keeps its position for the door's existing parse; `tok=`/`thr=`
    carry the quantity that actually decided."""
    run(tmp_path, "compaction_one lane-a")
    assert f"ctx=16% tok={OVER} thr=150000" in rows(tmp_path)


def test_an_unreadable_pane_renders_ctx_as_a_question_mark(tmp_path):
    """Absence is not zero here either — ctx=0% would read as an empty lane."""
    run(tmp_path, "compaction_one lane-a", ctx="")
    assert "ctx=?%" in rows(tmp_path)


def test_the_fire_row_precedes_the_ask_row(tmp_path):
    """`fire` is detection; `ask` is delivery. An ask with no fire would hide
    the detections that never reached a lane."""
    run(tmp_path, "compaction_one lane-a")
    body = rows(tmp_path)
    assert body.index(" fire ") < body.index(" ask ")


def test_the_ask_names_the_real_reading_and_the_cap(tmp_path):
    run(tmp_path, "compaction_one lane-a")
    typed = keys(tmp_path)
    assert f"at {OVER} tokens" in typed
    assert "150000-token cap" in typed


def test_the_ask_demands_a_flush_before_the_answer(tmp_path):
    """`/clear` destroys context; the flush is what makes it survivable."""
    run(tmp_path, "compaction_one lane-a")
    typed = keys(tmp_path)
    assert "Flush anything worth keeping to memory" in typed
    assert "CLEAR" in typed and "COMPACT" in typed


def test_the_text_and_the_Enter_are_separate_sends(tmp_path):
    """`send-keys "<text>" Enter` makes tmux read the literal as a KEY NAME
    wherever it matches one — the bug that typed `-tmcp-hub-dev-vm-1` into two
    lanes on 2026-08-28."""
    run(tmp_path, "compaction_one lane-a")
    lines = [ln for ln in keys(tmp_path).splitlines() if "send-keys" in ln]
    assert any("send-keys -l" in ln for ln in lines), lines
    assert any(ln.strip().endswith("Enter") and "send-keys -l" not in ln
               for ln in lines), lines


@pytest.mark.parametrize("chrome,why", [
    ("Nucleating… (1m 55s · ↓ 6.4k tokens)\n", "working"),
    ("❯ 1. Yes\n  2. No\n", "waiting on a dialog"),
])
def test_a_lane_that_is_not_idle_is_never_typed_into(tmp_path, chrome, why):
    """The whole safety property. A send-keys into a working lane appends to
    its input box; into a dialog it answers a question that is the
    operator's."""
    p = run(tmp_path, "compaction_one lane-a", state_lines=chrome)
    assert "not asking this pass (idle only)" in p.stdout, why
    assert "send-keys" not in keys(tmp_path)


def test_a_busy_lane_STILL_records_the_detection(tmp_path):
    """Detection and delivery are separate facts — a fire with no ask is
    exactly the row that shows the ask never landed."""
    run(tmp_path, "compaction_one lane-a",
        state_lines="Nucleating… (1m 55s · ↓ 6.4k tokens)\n")
    assert " fire " in rows(tmp_path)
    assert " ask " not in rows(tmp_path)


def test_a_busy_lane_fires_ONCE_not_every_sweep(tmp_path):
    """FIRE is a CROSSING, not a per-scan level log; the door counts a repeat
    at the same threshold as REPEAT."""
    run(tmp_path, "compaction_one lane-a\n" * 4,
        state_lines="Nucleating… (1m 55s · ↓ 6.4k tokens)\n")
    assert rows(tmp_path).count(" fire ") == 1


def test_the_same_session_is_not_asked_twice(tmp_path):
    run(tmp_path, "compaction_one lane-a\n" * 4)
    assert keys(tmp_path).count("send-keys -l") == 1


def test_the_session_key_does_not_move_when_the_clock_does(tmp_path):
    """THE repeat bug. The first key was COMPUTED as (now - etimes), and
    `ps -o etimes=` is truncated whole seconds — so it alternated by one
    between passes, minting a fresh flag and asking the lane AGAIN every
    other sweep. The console flagged that as REPEAT on reliable-ai, and the
    live box was left holding two keys one second apart for one session.

    The key is now READ off the transcript (its session id), so a jittering
    clock cannot move it. `jitter` drives the stub the way the real one
    behaves."""
    run(tmp_path, "compaction_one lane-a\n" * 6, jitter=True)
    assert keys(tmp_path).count("send-keys -l") == 1
    assert rows(tmp_path).count(" fire ") == 1
    assert rows(tmp_path).count(" ask ") == 1


def test_a_new_session_re_arms_the_ask(tmp_path):
    """Stability must not become stickiness: a relaunch is a NEW transcript
    file, and that lane has a fresh context worth asking about. This is the
    property the old start-time key bought, and it survives the change."""
    run(tmp_path, "compaction_one lane-a\n" * 3)
    d = tmp_path / ".claude" / "projects" / str(tmp_path).replace("/", "-")
    src = next(d.glob("*.jsonl"))
    later = d / "99999999-aaaa-bbbb-cccc-dddddddddddd.jsonl"
    later.write_bytes(src.read_bytes())
    os.utime(later, (time.time() + 10, time.time() + 10))
    run(tmp_path, "compaction_one lane-a\n" * 3)
    assert keys(tmp_path).count("send-keys -l") == 2


def test_a_faculty_lane_is_swept_too(tmp_path):
    """Grouping decides who hears a broadcast; it must not decide who gets
    MEASURED. squad_agents() excludes class `faculty`, which hid squad-proxy
    and every ad-hoc spike from a sweep whose whole subject is what a pane is
    burning (his ruling, relayed 2026-09-02 20:32)."""
    p = run(tmp_path, "compaction_pass\n", klass="faculty")
    assert "compaction ask sent" in p.stdout, p.stdout + p.stderr
    assert "send-keys -l" in keys(tmp_path)


# --- bar 52: no answer = no action, absolutely -------------------------------

def test_an_unanswered_ask_never_reaches_send_keys(tmp_path):
    """THE safety property, with the leg ARMED. The lane was asked and said
    nothing; nothing may be typed, forever."""
    p = run(tmp_path, "compaction_one lane-a\n" * 5, env=ARMED, replies=())
    typed = keys(tmp_path)
    assert "/clear" not in typed and "/compact" not in typed
    assert " answer " not in rows(tmp_path)
    assert " exec " not in rows(tmp_path)
    assert p.returncode == 0


def test_a_reply_that_names_neither_word_is_not_an_answer(tmp_path):
    run(tmp_path, "compaction_one lane-a\n" * 3, env=ARMED,
        replies=answered("Acknowledged, I will look at this shortly."))
    typed = keys(tmp_path)
    assert "/clear" not in typed and "/compact" not in typed
    assert " answer " not in rows(tmp_path)


def test_prose_is_not_a_verdict(tmp_path):
    """Word-bounded and uppercase: `unclear` and `compaction` are ordinary
    words a lane writes constantly."""
    run(tmp_path, "compaction_one lane-a\n" * 3, env=ARMED,
        replies=answered("The compaction story is still unclear to me."))
    assert " answer " not in rows(tmp_path)
    assert "/clear" not in keys(tmp_path)


def test_the_ask_itself_is_never_read_back_as_the_answer(tmp_path):
    """The ask names both words and is typed into the lane. It arrives as a
    USER turn, so only assistant turns are scanned — otherwise every ask
    would answer itself AMBIGUOUS on the next pass."""
    run(tmp_path, "compaction_one lane-a\n" * 3, env=ARMED)
    assert "AMBIGUOUS" not in rows(tmp_path)
    assert " answer " not in rows(tmp_path)


def test_a_reply_from_BEFORE_the_ask_is_not_an_answer(tmp_path):
    """Attribution is what the ask timestamp buys: a lane that happened to
    write CLEAR an hour earlier has not answered this ask."""
    run(tmp_path, "compaction_one lane-a\n" * 3, env=ARMED,
        replies=answered("CLEAR", offset=-3600))
    assert "/clear" not in keys(tmp_path)
    assert " answer " not in rows(tmp_path)


def test_both_words_in_one_reply_is_UNRESOLVED_and_fails_closed(tmp_path):
    p = run(tmp_path, "compaction_one lane-a\n" * 3, env=ARMED,
            replies=answered("CLEAR or COMPACT, whichever you prefer."))
    assert "no action (fail closed)" in p.stdout
    assert "AMBIGUOUS" in rows(tmp_path)
    assert "NOT EXECUTED" in rows(tmp_path)
    assert "/clear" not in keys(tmp_path) and "/compact" not in keys(tmp_path)


# --- bar 52: the exec leg, disarmed and armed --------------------------------

def test_the_exec_leg_is_off_by_default(tmp_path):
    p = run(tmp_path, 'echo "exec=$COMPACT_EXEC"')
    assert "exec=0" in p.stdout


def test_a_verdict_types_nothing_while_the_leg_is_disarmed(tmp_path):
    """Disarmed still RECORDS the answer — the Ledger can see what lanes
    chose long before anyone lets the keystroke through."""
    p = run(tmp_path, "compaction_one lane-a\n" * 3,
            replies=answered("COMPACT — the thread is still live."))
    assert "exec leg OFF" in p.stdout
    assert "lane answered COMPACT" in rows(tmp_path)
    assert "MCP_HUB_COMPACTION_EXEC=0" in rows(tmp_path)
    assert "/compact" not in keys(tmp_path)


@pytest.mark.parametrize("verdict,cmd,other", [
    ("COMPACT", "/compact", "/clear"),
    ("CLEAR", "/clear", "/compact"),
])
def test_an_armed_leg_types_the_command_the_lane_chose(tmp_path, verdict, cmd, other):
    p = run(tmp_path, "compaction_one lane-a\n" * 2, env=ARMED,
            replies=answered(f"{verdict} — flushed to memory first."))
    assert f"typed {cmd}" in p.stdout, p.stdout + p.stderr
    typed = keys(tmp_path)
    assert cmd in typed
    assert other not in typed
    assert f"lane answered {verdict}" in rows(tmp_path)
    assert f"typing {cmd}" in rows(tmp_path)


def test_the_slash_command_is_typed_as_two_sends(tmp_path):
    run(tmp_path, "compaction_one lane-a\n" * 2, env=ARMED,
        replies=answered("COMPACT"))
    lines = [ln for ln in keys(tmp_path).splitlines() if "send-keys" in ln]
    assert any("send-keys -l" in ln and "/compact" in ln for ln in lines), lines


def test_the_exec_row_precedes_the_keystroke(tmp_path):
    """If the send goes wrong the record still says what was about to happen
    and at what reading."""
    run(tmp_path, "compaction_one lane-a\n" * 2, env=ARMED,
        replies=answered("COMPACT"))
    assert "typing /compact at" in rows(tmp_path)


def test_an_armed_leg_still_refuses_a_lane_that_is_not_idle(tmp_path):
    """`/clear` into a working pane is the worst keystroke in this file. The
    lane is asked while idle and picks work up before the verdict is acted
    on — the exec guard has to be checked at EXEC time, not inherited from
    whatever the pane looked like at ask time."""
    p = run(tmp_path,
            "compaction_one lane-a\n"
            'echo "Nucleating… (1m 55s · ↓ 6.4k tokens)" >> "$HOME/pane.txt"\n'
            "compaction_one lane-a\n"
            "compaction_one lane-a\n",
            env=ARMED, replies=answered("CLEAR"))
    assert "not typing this pass (idle only)" in p.stdout, p.stdout
    assert "/clear" not in keys(tmp_path)
    # the verdict is banked, so waiting costs nothing
    assert "lane answered CLEAR" in rows(tmp_path)


def test_the_command_is_typed_once_however_many_passes_run(tmp_path):
    p = run(tmp_path, "compaction_one lane-a\n" * 6, env=ARMED,
            replies=answered("COMPACT"))
    assert keys(tmp_path).count("/compact") == 1, p.stdout


def test_the_net_row_is_measured_after_the_fact_never_predicted(tmp_path):
    """The saving is not knowable before the next turn lands, so the row is
    written only once the transcript has actually moved."""
    p = run(tmp_path, "compaction_one lane-a\n" * 3, env=ARMED,
            replies=answered("COMPACT"))
    assert "typed /compact" in p.stdout, p.stdout
    assert " net " not in rows(tmp_path), "a net row before the reading moved"
    # HOME (and so the flag set) survives; the lane's next turn now reads
    # lower, which is the only thing that makes the saving measurable.
    p = run(tmp_path, "compaction_one lane-a", env=ARMED,
            replies=answered("COMPACT"), tokens=40_000)
    body = rows(tmp_path)
    assert " net " in body, body
    assert f"recovered {OVER - 40_000} tokens" in body
