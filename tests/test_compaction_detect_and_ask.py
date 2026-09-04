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

# well under / well over the cap. CAP is the DEFAULT the script ships, and it
# is asserted rather than duplicated as a literal: it moved 150000 -> 138111
# on a measurement (deputy's #327 ruling, 2026-09-04) and will move again the
# next time the climb is re-measured.
CAP = 138_111
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


def boundary(pre, post, *, offset=60):
    """The record the CLIENT writes when it really compacts. `/compact` that
    executes leaves this behind carrying the exact pre/post; the one typed
    into cockpit and squad-proxy on 2026-09-03 left nothing, which is how we
    know it never ran."""
    return {"type": "system", "subtype": "compact_boundary",
            "timestamp": stamp(offset),
            "compactMetadata": {"trigger": "manual", "preTokens": pre,
                                "postTokens": post, "durationMs": 118_000}}


def transcript(home: Path, worktree: Path, *, tokens=None, replies=(),
               compacted=None):
    """Write the lane's Claude Code transcript the way the client encodes it:
    ~/.claude/projects/<worktree with / -> ->/<session>.jsonl.

    Every assistant turn carries the same reading, because a lane that
    answers the ask has not thereby shrunk — the reading only moves once
    something is actually executed."""
    d = home / ".claude" / "projects" / str(worktree).replace("/", "-")
    d.mkdir(parents=True, exist_ok=True)
    rows = [assistant("2026-09-02T18:00:00.000Z", "working", tokens)]
    rows += [assistant(ts, text, tokens) for ts, text in replies]
    if compacted:
        # (pre, post) or (pre, post, offset) — the third element puts the
        # boundary BEFORE the ask, which is how "an older compaction is not
        # this cycle's" gets tested at all.
        rows.append(boundary(compacted[0], compacted[1],
                             offset=compacted[2] if len(compacted) > 2 else 60))
    f = d / "05a50d0c-1111-2222-3333-444455556666.jsonl"
    f.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return f


def run(tmp_path, snippet, *, tokens=OVER, state_lines=None, agent="lane-a",
        replies=(), env=None, ctx="16", jitter=False, klass="squad",
        compacted=None, pane=None, pane_after=None):
    home = tmp_path
    (home / ".mcp-hub").mkdir(parents=True, exist_ok=True)
    conf = home / "squad.conf"
    conf.write_text(f"{agent}|{home}||--continue|{klass}\n", encoding="utf-8")

    transcript(home, home, tokens=tokens, replies=replies, compacted=compacted)

    bin_ = home / "bin"
    bin_.mkdir(exist_ok=True)

    # The pane: a statusline carrying ctx (decoration now, not the trigger),
    # plus whatever state chrome the test wants classify_text to read.
    if pane is None:
        pane = (f"⚡ 8/11 · Opus high · ctx [||||] {ctx}%\n" if ctx
                else "no statusline\n")
        pane += (state_lines or "")
    (bin_ / "tmux").write_text(
        "#!/bin/bash\n"
        f'echo "$@" >> {home}/tmux.log\n'
        # the literal landing is what "a dialog appeared afterwards" means
        f'case "$*" in *"send-keys -l"*) touch {home}/literal_sent ;; esac\n'
        'for a in "$@"; do\n'
        '  case "$a" in\n'
        f'    has-session) exit 0 ;;\n'
        f'    capture-pane) if [ -f {home}/pane2.txt ] && [ -f {home}/literal_sent ];'
    f' then cat {home}/pane2.txt; else cat {home}/pane.txt; fi; exit 0 ;;\n'
        f'    display-message) echo 12345; exit 0 ;;\n'
        '  esac\n'
        'done\n'
        'exit 0\n'
    )
    (bin_ / "tmux").chmod(0o755)
    (home / "pane.txt").write_text(pane)
    if pane_after is not None:
        (home / "pane2.txt").write_text(pane_after)

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


def commands_typed(tmp_path):
    """The slash commands typed as their OWN literal, in order.

    🔴 Substring-matching the tmux log stopped being able to answer this the
    moment the ask itself started naming `/compact` and `/clear` (card #405).
    A test that cannot tell the ASK from the KEYSTROKE would report "nothing
    was typed" while the keystroke went in — the exact shape of failure this
    file exists to catch."""
    out = []
    for ln in keys(tmp_path).splitlines():
        if "send-keys -l" not in ln:
            continue
        last = ln.rsplit(" ", 1)[-1].strip()
        if last in ("/compact", "/clear"):
            out.append(last)
    return out


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
    assert f"ctx=16% tok={OVER} thr={CAP}" in rows(tmp_path)


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
    assert f"{CAP}-token cap" in typed


def test_the_ask_demands_a_flush_before_the_answer(tmp_path):
    """`/clear` destroys context; the flush is what makes it survivable."""
    run(tmp_path, "compaction_one lane-a")
    typed = keys(tmp_path)
    assert "flush anything worth keeping to memory" in typed
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
    assert commands_typed(tmp_path) == []
    assert " answer " not in rows(tmp_path)
    assert " exec " not in rows(tmp_path)
    assert p.returncode == 0


def test_a_reply_that_names_neither_word_is_not_an_answer(tmp_path):
    run(tmp_path, "compaction_one lane-a\n" * 3, env=ARMED,
        replies=answered("Acknowledged, I will look at this shortly."))
    assert commands_typed(tmp_path) == []
    assert " answer " not in rows(tmp_path)


def test_prose_is_not_a_verdict(tmp_path):
    """Word-bounded and uppercase: `unclear` and `compaction` are ordinary
    words a lane writes constantly."""
    run(tmp_path, "compaction_one lane-a\n" * 3, env=ARMED,
        replies=answered("The compaction story is still unclear to me."))
    assert " answer " not in rows(tmp_path)
    assert commands_typed(tmp_path) == []


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
    assert commands_typed(tmp_path) == []
    assert " answer " not in rows(tmp_path)


def test_both_words_in_one_reply_is_UNRESOLVED_and_fails_closed(tmp_path):
    p = run(tmp_path, "compaction_one lane-a\n" * 3, env=ARMED,
            replies=answered("CLEAR or COMPACT, whichever you prefer."))
    assert "no action (fail closed)" in p.stdout
    assert "AMBIGUOUS" in rows(tmp_path)
    assert "NOT EXECUTED" in rows(tmp_path)
    assert commands_typed(tmp_path) == []


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
    assert commands_typed(tmp_path) == []


@pytest.mark.parametrize("verdict,cmd,other", [
    ("COMPACT", "/compact", "/clear"),
    ("CLEAR", "/clear", "/compact"),
])
def test_an_armed_leg_types_the_command_the_lane_chose(tmp_path, verdict, cmd, other):
    p = run(tmp_path, "compaction_one lane-a\n" * 2, env=ARMED,
            replies=answered(f"{verdict} — flushed to memory first."))
    assert f"typed {cmd}" in p.stdout, p.stdout + p.stderr
    assert commands_typed(tmp_path) == [cmd], f"other={other} leaked"
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
    assert commands_typed(tmp_path) == []
    # the verdict is banked, so waiting costs nothing
    assert "lane answered CLEAR" in rows(tmp_path)


def test_the_command_is_typed_once_however_many_passes_run(tmp_path):
    p = run(tmp_path, "compaction_one lane-a\n" * 6, env=ARMED,
            replies=answered("COMPACT"))
    assert commands_typed(tmp_path) == ["/compact"], p.stdout


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
            replies=answered("COMPACT"), tokens=40_000,
            compacted=(OVER, 40_000))
    body = rows(tmp_path)
    assert " net " in body, body
    assert f"recovered {OVER - 40_000} tokens" in body


# ---------------------------------------------------------------------------
# ONCE PER CROSSING, NOT ONCE PER SESSION (2026-09-02)
#
# `/compact` rewrites the conversation IN PLACE — same transcript id, same
# jsonl — so the session-keyed flag left `.closed` standing and the lane was
# never asked again however far it climbed. Measured on the author's own seat:
# 176,714 -> 87,054 at 20:42, then back to 379,280 with the cycle closed and
# no ask. Every test until now drove ONE crossing, which is exactly why this
# was invisible.
# ---------------------------------------------------------------------------

SHRUNK = 60_000        # post-compact, below the cap
CLIMB = 200_000        # a genuine second climb
STILL_OVER = 160_000   # a compact that lands above the cap


def _full_cycle(tmp_path):
    """Drive fire -> ask -> answer -> exec, then a shrunk reading so the NET
    row lands and the cycle closes with a measured floor."""
    run(tmp_path, "compaction_one lane-a\n" * 3, env=ARMED,
        replies=answered("COMPACT"))
    run(tmp_path, "compaction_one lane-a\n", tokens=SHRUNK, env=ARMED,
        replies=answered("COMPACT"), compacted=(OVER, SHRUNK))


def test_a_second_climb_in_the_SAME_session_is_asked_again(tmp_path):
    _full_cycle(tmp_path)
    assert " net " in rows(tmp_path), "the cycle must close with a measurement"
    assert rows(tmp_path).count(" ask ") == 1

    # same session id throughout — the transcript filename never changes
    run(tmp_path, "compaction_one lane-a\n" * 3, tokens=CLIMB, env=ARMED,
        replies=answered("COMPACT"))

    assert rows(tmp_path).count(" fire ") == 2, rows(tmp_path)
    assert rows(tmp_path).count(" ask ") == 2, rows(tmp_path)
    sessions = {f.name.split("compaction-lane-a-")[1].split(".")[0]
                for f in (tmp_path / ".mcp-hub").glob("compaction-lane-a-*")}
    assert len(sessions) == 1, f"re-arm must reuse the session key, got {sessions}"


def test_a_compact_that_lands_ABOVE_the_cap_does_not_immediately_re_ask(tmp_path):
    """The anti-thrash guard. Right after the exec tok == the measured floor,
    so 'still over the cap' must NOT be enough on its own — re-arming waits
    for growth PAST that floor, or the lane is asked again the moment it
    finishes answering."""
    run(tmp_path, "compaction_one lane-a\n" * 3, env=ARMED,
        replies=answered("COMPACT"))
    run(tmp_path, "compaction_one lane-a\n", tokens=STILL_OVER, env=ARMED,
        replies=answered("COMPACT"), compacted=(OVER, STILL_OVER))
    assert " net " in rows(tmp_path)
    before = rows(tmp_path).count(" ask ")

    run(tmp_path, "compaction_one lane-a\n" * 3, tokens=STILL_OVER, env=ARMED,
        replies=answered("COMPACT"))
    assert rows(tmp_path).count(" ask ") == before, "re-asked without new growth"


def test_a_cycle_closed_WITHOUT_a_measurement_never_re_arms(tmp_path):
    """Exec disarmed: the answer is recorded and nothing is typed, so there is
    no measured floor. That cycle is done for the session — re-arming it would
    re-ask a lane about a climb it was already asked about."""
    run(tmp_path, "compaction_one lane-a\n" * 3, replies=answered("COMPACT"))
    assert "exec leg OFF" in rows(tmp_path) or " exec " in rows(tmp_path)
    before = rows(tmp_path).count(" ask ")

    run(tmp_path, "compaction_one lane-a\n" * 3, tokens=CLIMB,
        replies=answered("COMPACT"))
    assert rows(tmp_path).count(" ask ") == before, \
        "a cycle with no measured net must not re-arm"


def test_a_LEGACY_cycle_re_arms_off_the_exec_reading(tmp_path):
    """Cycles closed by the previous version wrote an EMPTY marker — that is
    every lane compacted on 2026-09-02, i.e. precisely the ones this change
    protects. They fall back to the exec's BEFORE reading, a STRICTER bar:
    the lane must climb back above where it stood before compacting."""
    run(tmp_path, "compaction_one lane-a\n" * 3, env=ARMED,
        replies=answered("COMPACT"))
    run(tmp_path, "compaction_one lane-a\n", tokens=SHRUNK, env=ARMED,
        replies=answered("COMPACT"), compacted=(OVER, SHRUNK))
    flag = next((tmp_path / ".mcp-hub").glob("compaction-lane-a-*.closed"))
    flag.write_text("")                      # what the old code left behind
    before = rows(tmp_path).count(" ask ")

    # below the pre-compact reading: still the residue of the old climb
    run(tmp_path, "compaction_one lane-a\n" * 3, tokens=160_000, env=ARMED,
        replies=answered("COMPACT"))
    assert rows(tmp_path).count(" ask ") == before, "legacy bar is the exec reading"

    # above it: an unambiguous new climb
    run(tmp_path, "compaction_one lane-a\n" * 3, tokens=300_000, env=ARMED,
        replies=answered("COMPACT"))
    assert rows(tmp_path).count(" ask ") == before + 1, rows(tmp_path)


# AN AMBIGUOUS ANSWER MUST NOT DISARM THE LANE FOR THE REST OF ITS SESSION
#
# Failing closed is about the KEYSTROKE: an unresolved verdict types nothing.
# It used to also write an EMPTY `.closed`, and since nothing was typed there
# is no `.exec` for the legacy fallback to read — so compaction_recross could
# never re-arm that session id and only a relaunch fixed it. Measured on
# vps-hetzner-dev-vm-1 2026-09-03: answered AMBIGUOUS 07:21:41Z, then ran 362
# turns at ~326k avg (2.2x the cap) with no further ask.
# ---------------------------------------------------------------------------

BOTH = "CLEAR or COMPACT, whichever you prefer."


def _ambiguous_cycle(tmp_path):
    """fire -> ask -> answer AMBIGUOUS -> closed, with nothing typed."""
    p = run(tmp_path, "compaction_one lane-a\n" * 3, env=ARMED,
            replies=answered(BOTH))
    assert "no action (fail closed)" in p.stdout
    assert commands_typed(tmp_path) == []


def test_an_AMBIGUOUS_cycle_re_arms_on_genuine_growth(tmp_path):
    """The regression this fix exists for. The floor is the reading at the
    moment of the ambiguous close, so a real climb past it is asked about
    again — under the SAME session id, with no relaunch."""
    _ambiguous_cycle(tmp_path)
    before = rows(tmp_path).count(" ask ")

    run(tmp_path, "compaction_one lane-a\n" * 3, tokens=CLIMB, env=ARMED,
        replies=answered("COMPACT"))

    assert rows(tmp_path).count(" ask ") == before + 1, rows(tmp_path)
    assert commands_typed(tmp_path) == ["/compact"], "the re-armed cycle must reach exec"
    sessions = {f.name.split("compaction-lane-a-")[1].split(".")[0]
                for f in (tmp_path / ".mcp-hub").glob("compaction-lane-a-*")}
    assert len(sessions) == 1, f"re-arm must reuse the session key, got {sessions}"


def test_an_AMBIGUOUS_cycle_does_not_re_ask_at_the_SAME_reading(tmp_path):
    """The anti-repeat half. Still over the cap is not new growth — otherwise
    an unresolved lane is re-asked every single pass, which is the thrash the
    empty marker was there to prevent."""
    _ambiguous_cycle(tmp_path)
    before = rows(tmp_path).count(" ask ")

    run(tmp_path, "compaction_one lane-a\n" * 3, tokens=OVER, env=ARMED,
        replies=answered(BOTH))

    assert rows(tmp_path).count(" ask ") == before, \
        "an ambiguous cycle re-asked without new growth"


def test_an_AMBIGUOUS_close_records_its_reading_as_the_floor(tmp_path):
    """The marker itself, not just the behaviour: an empty one is the bug."""
    _ambiguous_cycle(tmp_path)
    flag = next((tmp_path / ".mcp-hub").glob("compaction-lane-a-*.closed"))
    assert flag.read_text().strip() == str(OVER), \
        f"AMBIGUOUS must close with a measured floor, got {flag.read_text()!r}"


# ---------------------------------------------------------------------------
# THE EXEC THAT DID NOTHING (2026-09-03)
#
# exec typed `/compact` into operator-cockpit-ui-agent (08:16:49) and
# squad-proxy (08:16:46) while both were idle. NEITHER transcript carries a
# compact_boundary after it: both show `user "/compact"` answered by the model
# with "No response requested." — the TUI never parsed the leading slash and
# the line went to the API as an ordinary prompt. A `/compact` that really
# runs also writes `<command-name>/compact</command-name>` and then the
# boundary; dreamteam's 13:03:50 exec has all of them (170527 -> 9731).
#
# The close leg only ever closed on `tok < before`. With no compaction that
# comparison is never true, so the cycle stayed open, `.closed` was never
# written, recross could never re-arm, and both lanes climbed to ~400k over
# five hours with NOT ONE ROW saying anything. Every test above drove an exec
# that WORKED, which is exactly why this was invisible.
# ---------------------------------------------------------------------------

LANDED_NOW = {"MCP_HUB_COMPACTION_EXEC": "1", "MCP_HUB_COMPACT_LANDED_AFTER": "0"}


def _typed(tmp_path, **kw):
    """fire -> ask -> answer COMPACT -> exec, with the command typed."""
    p = run(tmp_path, "compaction_one lane-a\n" * 3, replies=answered("COMPACT"),
            **kw)
    assert "typed /compact" in p.stdout, p.stdout
    return p


def test_the_net_is_read_from_the_clients_own_compact_boundary(tmp_path):
    """The exact pre/post, from the process that did the compacting — not a
    reading this sweep happened to catch. The lane's CURRENT reading here is
    still above `before`, so the old sampled close would have seen nothing."""
    _typed(tmp_path, env=ARMED)
    run(tmp_path, "compaction_one lane-a", env=ARMED, tokens=OVER + 5_000,
        replies=answered("COMPACT"), compacted=(170_527, 9_731))

    body = rows(tmp_path)
    assert "recovered 160796 tokens: 170527 -> 9731 (compact_boundary)" in body, body
    flag = next((tmp_path / ".mcp-hub").glob("compaction-lane-a-*.closed"))
    assert flag.read_text().strip() == "9731", flag.read_text()


def test_nothing_is_concluded_while_the_compaction_is_still_running(tmp_path):
    """Real ones take 98-148s. Inside the window a missing boundary is not a
    failed keystroke, and saying so would libel every slow compact."""
    _typed(tmp_path, env=ARMED)
    p = run(tmp_path, "compaction_one lane-a", env=ARMED,
            replies=answered("COMPACT"))

    assert "NOT COMPACTED" not in rows(tmp_path), rows(tmp_path)
    assert "re-arming" not in p.stdout, p.stdout
    assert not list((tmp_path / ".mcp-hub").glob("compaction-lane-a-*.closed"))


def test_an_exec_that_never_compacted_is_re_armed_and_says_so(tmp_path):
    """The backstop. No boundary and no drop, past the window: the keystroke
    did not take, and the crossing is still owed an ask."""
    _typed(tmp_path, env=ARMED)
    asks = rows(tmp_path).count(" ask ")

    p = run(tmp_path, "compaction_one lane-a", env=LANDED_NOW,
            replies=answered("COMPACT"))
    assert "NOT COMPACTED" in rows(tmp_path), rows(tmp_path)
    assert "the keystroke did not take (attempt 1 of 2)" in rows(tmp_path)
    assert "re-arming" in p.stdout, p.stdout

    # re-armed means ASKED AGAIN, under the same session key
    run(tmp_path, "compaction_one lane-a\n" * 2, env=LANDED_NOW,
        replies=answered("COMPACT"))
    assert rows(tmp_path).count(" ask ") == asks + 1, rows(tmp_path)
    sessions = {f.name.split("compaction-lane-a-")[1].split(".")[0]
                for f in (tmp_path / ".mcp-hub").glob("compaction-lane-a-*")}
    assert len(sessions) == 1, f"re-arm must reuse the session key, got {sessions}"


def test_a_failed_exec_is_not_retried_for_ever(tmp_path):
    """A command that will not parse will not start parsing. After the cap the
    leg stops TYPING — and keeps WATCHING: the floor is the measured reading,
    so a genuine climb re-arms, exactly as after an AMBIGUOUS close."""
    for _ in range(4):
        run(tmp_path, "compaction_one lane-a\n" * 3, env=LANDED_NOW,
            replies=answered("COMPACT"))

    body = rows(tmp_path)
    assert "GIVING UP on typing for this session after 2 failed execs" in body, body
    flag = next((tmp_path / ".mcp-hub").glob("compaction-lane-a-*.closed"))
    assert flag.read_text().strip() == str(OVER), flag.read_text()
    assert body.count("attempt 1 of 2") == 1, "the cap must not reset itself"

    asks = body.count(" ask ")
    run(tmp_path, "compaction_one lane-a\n" * 3, env=LANDED_NOW,
        replies=answered("COMPACT"))
    assert rows(tmp_path).count(" ask ") == asks, "re-asked at the same reading"

    run(tmp_path, "compaction_one lane-a\n" * 3, tokens=CLIMB, env=LANDED_NOW,
        replies=answered("COMPACT"))
    assert rows(tmp_path).count(" ask ") == asks + 1, "a real climb must re-arm"


def test_a_dip_with_no_compaction_in_the_transcript_is_not_a_saving(tmp_path):
    """⚠️ The false measurement. squad-proxy's reading fell 3,222 below its
    `before` for SIX SECONDS at 08:17:06 on ordinary variation, with no
    compaction anywhere in its transcript. A sampled close would have written
    a `net` row claiming a saving for an act that never happened."""
    _typed(tmp_path, env=ARMED)
    run(tmp_path, "compaction_one lane-a", env=ARMED, tokens=OVER - 3_222,
        replies=answered("COMPACT"))

    assert " net " not in rows(tmp_path), rows(tmp_path)
    assert not list((tmp_path / ".mcp-hub").glob("compaction-lane-a-*.closed"))


def test_a_readable_transcript_outranks_the_sampled_drop(tmp_path):
    """The ordering, stated on its own: when the client reports a compaction,
    its numbers are the record — not whatever this sweep's reading happens to
    be at the moment the row is written."""
    _typed(tmp_path, env=ARMED)
    run(tmp_path, "compaction_one lane-a", env=ARMED, tokens=61_234,
        replies=answered("COMPACT"), compacted=(170_527, 9_731))

    body = rows(tmp_path)
    assert "170527 -> 9731 (compact_boundary)" in body, body
    assert "61234" not in body, "the sample must not be reported as the net"


# --- the dialog guard (card #381) -------------------------------------------
#
# 2026-09-03 11:01:47.714: compaction_type's Enter answered vps-hetzner's open
# AskUserQuestion — "Do you authorise the EXPAND leg of #371 on prod-1 now?"
# -> "Yes — apply the expand leg", recorded +0.009s later — and vps applied it
# to prod-1. The lane was maximally `idle` by classify_text, because a lane
# blocked on a dialog is not generating and the classifier's fall-through is
# `else idle`: its permissive branch is also its blind branch.
#
# ⚠️ vps's actual rendered screen is NOT recoverable (its pane history had
# rolled, and it declined to reconstruct one from memory). So none of the
# fixtures below claims to BE that screen. They stand for the category the
# allowlist exists for: a dialog matching no pattern anyone has written down.

UNKNOWN_DIALOG = """\
╭──────────────────────────────────────────────╮
│ Which deployment window should I use?        │
│                                              │
│    Tonight 22:00                             │
│    Tomorrow 06:00                            │
╰──────────────────────────────────────────────╯
"""

# The real bypass dialog, copied from tests/test_seat.py — the denylist DOES
# catch this one. It is here to prove the new guard did not lose ground that
# the old classifier already held.
BYPASS_DIALOG = """\
  WARNING: Claude Code running in Bypass Permissions mode

  ❯ 1. No, exit
    2. Yes, I accept

  Enter to confirm - Esc to cancel
"""

SAFE_PANE = """\
────────────────────────────────────────────────
❯
────────────────────────────────────────────────
  ⚡ 8/11 · Opus high · ctx [||||] 16%
  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents
"""


def test_an_unrecognised_dialog_is_never_typed_into(tmp_path):
    """THE regression. This screen matches none of classify_text's dialog
    patterns, so the old leg read it as idle and would have typed the ask and
    pressed Enter into it — selecting whatever row was default. The guard is
    an ALLOWLIST for exactly this reason: a denylist is only ever as good as
    the dialog shape somebody already met."""
    p = run(tmp_path, "compaction_one lane-a", pane=UNKNOWN_DIALOG)
    assert "send-keys" not in keys(tmp_path), "a keystroke reached a dialog"
    assert "NOT typing" in p.stderr


def test_the_crossing_survives_a_refusal(tmp_path):
    """Refusing to type must not consume the ask. No marker means the next
    sweep asks again — a lane silenced by one badly-timed dialog would be the
    same silent stall this whole leg exists to remove."""
    run(tmp_path, "compaction_one lane-a", pane=UNKNOWN_DIALOG)
    flags = list((tmp_path / ".mcp-hub").glob("compaction-lane-a-*"))
    assert not any(f.suffix == "" and f.stat().st_size for f in flags), \
        "the ask time was recorded for an ask that was never delivered"


def test_the_known_bypass_dialog_is_still_refused(tmp_path):
    """Ground the old classifier already held, held again."""
    run(tmp_path, "compaction_one lane-a", pane=BYPASS_DIALOG)
    assert "send-keys" not in keys(tmp_path)


def test_a_pane_that_cannot_be_read_is_not_typed_into(tmp_path):
    """Unreadable is not safe. The blind branch must not be the permissive
    one — that equivalence is the whole defect."""
    run(tmp_path, "compaction_one lane-a", pane="")
    assert "send-keys" not in keys(tmp_path)


def test_a_normal_idle_pane_is_still_typed_into(tmp_path):
    """The guard has to let the ordinary case through, or it is just an
    outage with a good excuse."""
    p = run(tmp_path, "compaction_one lane-a", pane=SAFE_PANE)
    assert "send-keys -l" in keys(tmp_path)
    assert "compaction ask sent at" in p.stdout


def test_the_enter_is_withheld_when_a_dialog_appears_after_the_literal(tmp_path):
    """The gap between the literal and the Enter is ~1s of real time, and the
    Enter is the half that ANSWERS a dialog. Checking once at the top would
    leave that window open. An unsent line is visible and harmless; a pressed
    default is neither."""
    p = run(tmp_path, "compaction_one lane-a", pane=SAFE_PANE,
            pane_after=UNKNOWN_DIALOG)
    log = keys(tmp_path)
    assert "send-keys -l" in log, "the literal should have been sent"
    assert "Enter" not in log, "Enter was pressed into a dialog"
    assert "Enter NOT sent" in p.stderr


def test_the_exec_slash_command_goes_through_the_same_funnel(tmp_path):
    """/compact is typed by a different leg. One guarded path and one
    unguarded path is how a guard gets trusted and still loses."""
    run(tmp_path, "compaction_one lane-a", pane=UNKNOWN_DIALOG,
        replies=answered("COMPACT"), env=ARMED)
    assert "send-keys" not in keys(tmp_path)
    assert commands_typed(tmp_path) == []


# --- the answer->exec deadline (deputy's #327 ruling, 2026-09-04) -----------
#
# The idle guard above is right in the common case and catastrophic in the
# tail: vps-hetzner-dev-vm-1 fired ON TIME at 151,191 on 2026-09-03 10:47Z
# and still peaked at 455,166, because it answered and then never went idle
# for 12,603s. No threshold reaches that — the ask does not fire twice — so
# the WAIT is what gets bounded, and only after the lane has answered.

DEADLINE_NOW = {"MCP_HUB_COMPACTION_EXEC": "1", "MCP_HUB_COMPACT_EXEC_DEADLINE": "0"}
BUSY = "Nucleating… (1m 55s · ↓ 6.4k tokens)\n"


def test_the_deadline_lifts_a_guard_that_the_PANE_guard_RE_IMPOSES(tmp_path):
    """⭐⭐ THE DEADLINE IS INERT BY CONSTRUCTION, and this test exists so
    nobody reads that as a bug OR as load-bearing.

    MEASURED 2026-09-04, and it is stronger than "not enough on its own":
    ``classify_agent`` IS ``classify_text(pane)``, and
    ``compaction_safe_to_type`` requires chrome AND
    ``classify_text(pane) == idle``. So guard 2 is a STRICT SUBSET of guard 1,
    and every state in which the deadline lifts guard 1 is a state guard 2
    then re-imposes. The set of keystrokes this deadline can buy is EMPTY.
    Empirically the same: 0 deadline fires ever, and 20/20 exec-side blocks in
    the heal journal were state `working`, never `waiting`, max 600s, all
    self-resolved.

    Guard 2 stays unweakened — it is what stops a /clear landing in a live
    generation. The real residual is one guard UPSTREAM: the ASK is idle-gated
    too (squad/squad:1370), so a lane that is never idle is never asked, never
    answers, and nothing down here ever runs for it.

    There are TWO guards between a verdict and a keystroke:

      1. `classify_agent` != idle   — state chrome. THIS is what the
         deadline bounds, because waiting on it forever is the 455k case.
      2. `compaction_safe_to_type`  — refuses any pane that is not claude's
         own idle chrome. THIS IS NOT BOUNDED AND MUST NOT BE. It is the
         guard that stops a blind keypress landing in a dialog, and `/clear`
         into a pane mid-generation is the worst keystroke in this file.

    So a lane that is genuinely mid-turn is STILL not typed into after the
    deadline: the deadline is reached, the attempt is made, and the pane
    guard refuses it out loud. A deadline that could type into a working
    pane would not be a deadline, it would be the thing every negative test
    in this file was written to prevent."""
    p = run(tmp_path,
            "compaction_one lane-a\n"
            f'echo "{BUSY.strip()}" >> "$HOME/pane.txt"\n'
            "compaction_one lane-a\n",
            env=DEADLINE_NOW, replies=answered("COMPACT"))
    assert "deadline reached, typing" in p.stdout, p.stdout + p.stderr
    assert "DEADLINE: answered COMPACT" in rows(tmp_path)
    # ...and the second guard still holds.
    assert commands_typed(tmp_path) == [], "the pane guard was bypassed"
    assert "not claude's idle chrome" in p.stderr, p.stderr


def test_the_deadline_row_says_the_lane_was_not_idle(tmp_path):
    """A keystroke into a busy pane must never look like an ordinary one in
    the record — the row carries the wait and the state that was overridden."""
    run(tmp_path,
        "compaction_one lane-a\n"
        f'echo "{BUSY.strip()}" >> "$HOME/pane.txt"\n'
        "compaction_one lane-a\n",
        env=DEADLINE_NOW, replies=answered("COMPACT"))
    body = rows(tmp_path)
    assert "typing anyway at the 0s bound" in body
    assert "has not been idle since" in body


def test_a_busy_lane_INSIDE_the_deadline_is_still_left_alone(tmp_path):
    """The default 900s must not become 'type immediately'. Same setup, real
    deadline: nothing is typed and the wait is reported."""
    p = run(tmp_path,
            "compaction_one lane-a\n"
            f'echo "{BUSY.strip()}" >> "$HOME/pane.txt"\n'
            "compaction_one lane-a\n",
            env=ARMED, replies=answered("COMPACT"))
    assert "not typing this pass (idle only)" in p.stdout
    assert "deadline reached" not in p.stdout
    assert commands_typed(tmp_path) == []
    assert "of 900s" in p.stdout, "the wait is not reported"


def test_a_lane_that_NEVER_ANSWERED_is_never_exec_d_however_long_it_waits(tmp_path):
    """🔴 THE RESIDUAL THE DEPUTY WOULD NOT MOVE. The deadline starts at the
    ANSWER. 'No answer = no action' is his own g81 wording, so a silent lane
    is re-asked for ever and never typed into — even with the deadline at 0,
    which would fire instantly if it were measured from the ask."""
    p = run(tmp_path,
            "compaction_one lane-a\n"
            f'echo "{BUSY.strip()}" >> "$HOME/pane.txt"\n'
            "compaction_one lane-a\n",
            env=DEADLINE_NOW)          # no replies => never answered
    assert "deadline reached" not in p.stdout
    assert commands_typed(tmp_path) == []
    assert commands_typed(tmp_path) == []


def test_the_shipped_default_deadline_is_900_seconds(tmp_path):
    """900s is the p90 of the MEASURED fire->exec lag. Pinned because a
    default that drifts silently is how the tail came back."""
    assert 'MCP_HUB_COMPACT_EXEC_DEADLINE:-900' in SQUAD.read_text(encoding="utf-8")


def test_the_shipped_default_cap_is_the_measured_one(tmp_path):
    assert f'MCP_HUB_COMPACT_ASK_TOKENS:-{CAP}' in SQUAD.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# THE BOUNDED FLUSH-AND-PARK — card #405, HIS PRESS 2026-09-04 22:00:45Z
# (hand=operator, said_at=press, verified, operator-token; executor
# mcp-hub-dev-vm-1). The ask and the keystroke are ALIKE idle-gated, so a
# lane that answers and then stays busy is reachable by neither. The lane is
# the only actor that can stop a busy lane without anyone typing into it, so
# the ask now instructs it to stop ITSELF — bounded: finish the step, flush,
# park, then act. Guard 2 (never type into a live generation) is untouched.
# ---------------------------------------------------------------------------

def _ask_text(tmp_path):
    """The ask exactly as it was typed into the pane."""
    for line in keys(tmp_path).splitlines():
        if "send-keys -l" in line and "context is at" in line:
            return line
    raise AssertionError(f"no ask was typed:\n{keys(tmp_path)}")


def test_the_ask_carries_the_bounded_flush_and_park(tmp_path):
    run(tmp_path, "compaction_one lane-a")
    ask = _ask_text(tmp_path)
    for clause in ("finish the step you are on",
                   "flush anything worth keeping to memory",
                   "park", "run /compact or /clear yourself"):
        assert clause in ask, f"{clause!r} missing from the ask:\n{ask}"


def test_the_bound_comes_FIRST_or_it_is_an_instruction_to_abandon_work(
        tmp_path):
    """BOUNDED is the load-bearing word. 'Compact yourself' with no bound
    tells a lane halfway through a write to drop it."""
    ask = (run(tmp_path, "compaction_one lane-a"), _ask_text(tmp_path))[1]
    order = [ask.index("finish the step you are on"),
             ask.index("flush anything worth keeping"),
             ask.index("park"),
             ask.index("run /compact or /clear yourself")]
    assert order == sorted(order), f"the four steps are out of order:\n{ask}"
    assert "do not abandon work mid-edit" in ask


def test_the_asks_own_slash_commands_are_lowercase(tmp_path):
    """The parser reads CLEAR/COMPACT uppercase and word-bounded. If the ask
    capitalised its own commands, an echo of the ask would be a verdict."""
    ask = (run(tmp_path, "compaction_one lane-a"), _ask_text(tmp_path))[1]
    assert "/compact" in ask and "/clear" in ask
    assert "/COMPACT" not in ask and "/CLEAR" not in ask


def test_a_lane_that_compacted_ITSELF_is_never_typed_into(tmp_path):
    """🔴 The race the approved instruction creates. The lane obeys and
    compacts; the keystroke would then land on a session that no longer
    holds what we measured, and `/clear` would destroy the context the lane
    had just chosen to keep."""
    run(tmp_path, "compaction_one lane-a")                    # the ask
    p = run(tmp_path, "compaction_one lane-a", env=ARMED,
            replies=answered("CLEAR"), tokens=SHRUNK,
            compacted=(OVER, SHRUNK))
    assert "compacted itself on the ask" in p.stdout, p.stdout
    assert "typed /clear" not in p.stdout, "the lane was typed into anyway"
    body = rows(tmp_path)
    assert "THE LANE DID IT ITSELF" in body, body
    assert f"recovered {OVER - SHRUNK} tokens" in body
    assert not list((tmp_path / ".mcp-hub").glob("*.exec")), \
        "an exec marker was written for a keystroke that never happened"


def test_the_lane_needs_no_REPLY_to_have_obeyed(tmp_path):
    """A lane that parks and compacts without replying has done the thing
    that was asked. Refusing to notice for want of a reply would re-type a
    command into a lane that already obeyed."""
    run(tmp_path, "compaction_one lane-a")
    p = run(tmp_path, "compaction_one lane-a", env=ARMED,
            tokens=SHRUNK, compacted=(OVER, SHRUNK))
    assert "compacted itself on the ask" in p.stdout, p.stdout
    assert " answer " not in rows(tmp_path), "a verdict was invented"


def test_a_self_compaction_still_records_the_floor_so_growth_re_arms(
        tmp_path):
    """Same measured floor as every other close — a later climb has to be
    tellable from the residue of this one."""
    run(tmp_path, "compaction_one lane-a")
    run(tmp_path, "compaction_one lane-a", env=ARMED,
        tokens=SHRUNK, compacted=(OVER, SHRUNK))
    p = run(tmp_path, "compaction_one lane-a", env=ARMED, tokens=CLIMB,
            compacted=(OVER, SHRUNK))
    assert "re-arming" in p.stdout, p.stdout


def test_a_boundary_from_BEFORE_the_ask_is_not_the_lane_obeying(tmp_path):
    """The lane's own act has to be attributable to the ask. An older
    compaction sitting in the transcript must not close a cycle opened after
    it — that would leave a lane over the cap believing it had complied."""
    run(tmp_path, "compaction_one lane-a")
    p = run(tmp_path, "compaction_one lane-a", env=ARMED,
            replies=answered("CLEAR"), compacted=(OVER, SHRUNK, -3_600))
    assert "compacted itself on the ask" not in p.stdout, p.stdout
    assert "typed /clear" in p.stdout, p.stdout
