"""What `squad heal` decides to do about one unhealthy agent.

The rule used to live inside heal's escalation loop, reachable only by a live
squad with real tmux sessions, a real claude process and a real status cache —
so it was never asserted at all. `heal_action` is that rule, extracted and
pure, on the same argument `fleet_tree.py` makes about the join: the rule is
the interesting part.

The defect these were written for (recorded 2026-07-25, unfixed until now, and
described in a comment at the top of squad itself): an agent that is
wake-capable but NOT resume-capable fell into the `nudge` branch forever. Every
two minutes it was told to re-register — and told that if the problem recurred
a relaunch would fix it and would resume its conversation. For that agent all
three claims are false. It had already re-registered and it had not worked,
heal was never going to relaunch it (rightly — that would destroy the
conversation), and a restart would not have resumed anything.

So the properties here are about a tool not lying to the agent it is helping,
and about every OTHER verdict surviving the extraction unchanged.
"""

from __future__ import annotations

import pathlib
import subprocess

import pytest

SQUAD = pathlib.Path(__file__).resolve().parents[1] / "squad" / "squad"
RESUME = "--continue"
COMMS = "--dangerously-load-development-channels server:hub"

pytestmark = pytest.mark.skipif(not SQUAD.exists(), reason="squad script not present")


def _action(state: str, struck: int, args: str, idle: int) -> str:
    """One verdict, from the real script.

    Sourced with `help` so the dispatch runs a branch that only prints usage —
    the functions are then defined and callable without a live machine.
    """
    res = subprocess.run(
        ["bash", "-c",
         'source "$1" help >/dev/null 2>&1; heal_action "$2" "$3" "$4" "$5"',
         "_", str(SQUAD), state, str(struck), args, str(idle)],
        capture_output=True, text=True, timeout=60,
    )
    assert res.returncode == 0, res.stderr
    return res.stdout.strip()


# ---- the defect ------------------------------------------------------------

def test_a_struck_agent_without_the_resume_flag_is_BLOCKED_not_nudged():
    """The whole point. A restart is the cure and the roster forbids it, so the
    one thing heal must not do is tell the agent to re-register again."""
    assert _action("offline", 1, COMMS, 1) == "blocked"


def test_the_same_agent_WITH_the_resume_flag_is_relaunched():
    """`blocked` and `relaunch` differ by exactly one flag — that is the whole
    distinction, and it must not have picked up a second condition."""
    assert _action("offline", 1, f"{RESUME} {COMMS}", 1) == "relaunch"


def test_an_unwakeable_agent_is_judged_the_same_way_as_an_offline_one():
    """Both mean 'the hub cannot reach it'. A rule that fired for one and not
    the other would leave half the population in the old forever-nudge."""
    assert _action("unwakeable", 1, COMMS, 1) == "blocked"
    assert _action("unwakeable", 1, f"{RESUME} {COMMS}", 1) == "relaunch"


# ---- the first strike is still worth taking --------------------------------

def test_an_unstruck_agent_is_still_nudged_even_without_the_resume_flag():
    """Re-registering genuinely fixes a stale binding, and that is most of what
    heal sees after a redeploy. Suppressing the FIRST nudge for every
    resume-less agent would break real recovery to fix a repetition problem."""
    assert _action("offline", 0, COMMS, 1) == "nudge"


def test_a_resume_capable_agent_is_nudged_before_it_is_restarted():
    """Escalation order survives: the cheap intervention comes first."""
    assert _action("offline", 0, f"{RESUME} {COMMS}", 1) == "nudge"


# ---- verdicts that must survive the extraction unchanged -------------------

def test_no_signal_respawns_the_daemon_first():
    """A dead heartbeat daemon costs the agent nothing to fix, so it is tried
    before anything that spends a turn."""
    assert _action("no-signal", 0, f"{RESUME} {COMMS}", 1) == "daemon"


def test_no_signal_is_checked_BEFORE_idleness_because_it_never_touches_the_pane():
    """The daemon respawn is the one intervention with no pane involvement, so
    it is deliberately not gated on idle. Moving it behind the idle check would
    leave a busy agent's dead daemon unrevived for as long as it stayed busy."""
    assert _action("no-signal", 0, f"{RESUME} {COMMS}", 0) == "daemon"


def test_a_busy_agent_is_never_touched():
    """Both remaining interventions touch the pane: the nudge TYPES text and
    Enter — at a '1. Yes / 2. No' dialog that answers it on the operator's
    behalf — and the restart kills an in-flight turn."""
    assert _action("offline", 0, f"{RESUME} {COMMS}", 0) == "defer"
    assert _action("offline", 1, f"{RESUME} {COMMS}", 0) == "defer"


def test_a_busy_agent_is_deferred_even_when_it_would_otherwise_be_BLOCKED():
    """`blocked` writes no pane, but it is still a verdict about what to do
    now, and 'busy' is the more urgent fact: report it when there is something
    to report about, not while the agent is mid-turn."""
    assert _action("offline", 1, COMMS, 0) == "defer"


# ---- the flag is matched as a WORD ----------------------------------------

def test_the_resume_flag_is_matched_as_a_whole_word():
    """`has_resume` has always matched with word boundaries; the loop this was
    extracted from used a bare substring test. A future `--continue-on-error`
    would have satisfied the substring version and earned a relaunch that
    started the agent blank."""
    assert _action("offline", 1, "--continue-on-error", 1) == "blocked"


def test_the_flag_is_found_when_it_is_the_only_argument():
    """The word-boundary test wraps the field in spaces; an args field of
    exactly `--continue` has neither a leading nor a trailing one."""
    assert _action("offline", 1, RESUME, 1) == "relaunch"


def test_an_empty_args_field_is_resume_less():
    """A faculty row enrolled before the flag became the default."""
    assert _action("offline", 1, "", 1) == "blocked"


# ---- the rule and its call site must not drift apart ----------------------

def test_every_verdict_has_an_arm_in_the_pass_that_consumes_it():
    """`heal_action` and the `case` that acts on it are in different parts of
    the file and nothing but this test couples them.

    A verdict with no arm matches nothing, so the pass does NOTHING for that
    agent and still counts it as healed — silence dressed as a repair, which is
    strictly worse than the forever-nudge being fixed here. The script also
    carries a loud `*)` arm for this; the test is what fails in CI rather than
    at 3am on somebody's box.
    """
    body = SQUAD.read_text(encoding="utf-8")
    fn = body.split("heal_action() {", 1)[1].split("\n}", 1)[0]
    verdicts = {ln.split("echo", 1)[1].split(";")[0].strip()
                for ln in fn.splitlines() if "echo " in ln}
    assert verdicts == {"daemon", "defer", "relaunch", "blocked", "nudge"}, verdicts
    consumer = body.split('case "$(heal_action', 1)[1].split("\n              esac", 1)[0]
    for v in verdicts:
        assert f"\n              {v})" in consumer, \
            f"heal_action can return {v!r} and the pass has no arm for it"


def test_the_advice_heal_prints_names_verbs_that_actually_EXIST():
    """Both resume-less messages tell the operator to run `resume on` and
    `restart`. Advice naming a verb that does not exist is worse than no
    advice: it is confidently wrong at the moment somebody is already stuck,
    and nothing else in the tree couples the message to the dispatch.
    """
    body = SQUAD.read_text(encoding="utf-8")
    advice = [ln for ln in body.splitlines() if "Fix: $CMD resume on" in ln]
    assert len(advice) == 2, \
        f"expected the deaf-sweep and the escalation message, got {len(advice)}"
    dispatch = body.split('case "${1:-help}" in', 1)[1]
    for verb in ("resume", "restart"):
        assert f"\n  {verb})" in dispatch, \
            f"heal tells the operator to run `squad {verb}`, which is not a verb"
