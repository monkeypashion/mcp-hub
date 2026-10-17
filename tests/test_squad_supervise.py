"""A faculty lane that stops comes back without a keystroke — and only then.

Bar 414 (goal 84, deputy-accepted 2026-09-22 under #303, hub.msg 31377). The
squad-resourcing planner is roster class `faculty`, so `squad up` skips it by
design, and `squad heal` demands a live tmux session before it judges anything
— a seat that has STOPPED is invisible to both, while heal still reports "all
up agents healthy". The sampler self-heals from an @reboot crontab and the
console from a systemd unit; this lane had nothing.

The fix is PER-ROW, never per class: teaching `up` to start faculty would
revive fourteen seats to bring back one (his ruling relayed 20:32 2026-09-02,
faculty seats were 58% of one hour's burn). Promoting the row to squad class
is HIS call and deliberately not taken here.

⚠️ The hard part is not starting the lane, it is the four cases where starting
it would be WRONG, and each one has a test below: a hand's stop must stick, a
hold must stand, a pace-gate refusal must not be overturned, and a placed seat
already has the edge. A supervisor that ignored the first would rebuild the
bug the LIFECYCLE INTENT block exists for — "every time I find you are running
again" — this time with no placement to blame.

Same head-extraction harness as test_squad_placement_intent, so these run the
shipped code rather than a transcription of it.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

SQUAD = Path(__file__).resolve().parents[1] / "squad" / "squad"

AGENT = "planner-dev-vm-1"
ROW_RESUME = f"{AGENT}|/tmp/planner||--continue --model fable|faculty"
ROW_NO_RESUME = f"{AGENT}|/tmp/planner||--model fable|faculty"


def head() -> str:
    h = SQUAD.read_text(encoding="utf-8").split('\ncase "${1:-help}" in', 1)[0]
    assert "supervise_pass()" in h, "extraction boundary moved"
    return h


def run(tmp_path, snippet, *, row=ROW_RESUME, stubs="", stub_up_one=True):
    home = tmp_path
    (home / ".mcp-hub").mkdir(parents=True, exist_ok=True)
    conf = home / "squad.conf"
    conf.write_text(row + "\n", encoding="utf-8")

    default_stubs = """
placement_id()  { return 1; }
held_until()    { return 1; }
tm()            { [ "$1" = has-session ] && return 1; return 0; }
"""
    # The door test below exercises the REAL up_one, so the logging stub has
    # to be optional — a test that asserts on a stub asserts on itself.
    if stub_up_one:
        default_stubs += 'up_one() { echo "UP_ONE $1" >> "$HOME/calls.log"; }\n'
    script = home / "_squad_head.sh"
    script.write_text(head() + default_stubs + stubs + "\n" + snippet, encoding="utf-8")
    proc = subprocess.run(
        ["bash", str(script)],
        capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(home), "SQUAD_CONF": str(conf)},
    )
    calls = (home / "calls.log").read_text() if (home / "calls.log").exists() else ""
    return proc, calls, home


def supervise(home):
    (home / ".mcp-hub" / f"supervise-{AGENT}").touch()


# --- the bar's own clause: it comes back with no keystroke -----------------

def test_supervised_and_down_is_started_with_the_resume_flag(tmp_path):
    proc, calls, home = run(tmp_path, "true")
    supervise(home)
    proc, calls, _ = run(tmp_path, "supervise_pass")
    assert f"UP_ONE {AGENT}" in calls
    assert "--continue" in proc.stdout


def test_a_row_nobody_opted_in_is_left_alone(tmp_path):
    """Per-row opt-in. Without the marker this pass does nothing at all —
    which is what keeps `up`'s deliberate faculty skip intact."""
    proc, calls, _ = run(tmp_path, "supervise_pass")
    assert calls == ""
    assert proc.stdout.strip() == ""


# --- the four cases where starting it would be wrong ----------------------

def test_a_hands_stop_sticks(tmp_path):
    proc, calls, home = run(tmp_path, "true")
    supervise(home)
    (home / ".mcp-hub" / f"want-down-{AGENT}").touch()
    proc, calls, _ = run(tmp_path, "supervise_pass")
    assert calls == "", "a supervisor that restarts a hand's stop is the bug, not the fix"
    assert "stopped by hand" in proc.stdout


def test_a_hold_stands(tmp_path):
    proc, calls, home = run(tmp_path, "true")
    supervise(home)
    proc, calls, _ = run(
        tmp_path, "supervise_pass",
        stubs='held_until() { echo 99999999999; return 0; }\n')
    assert calls == "", "a held lane is one somebody deliberately wants down"


def test_a_pace_gate_refusal_is_not_overturned(tmp_path):
    """The one that bites. A lane the pace gate refused is no longer held and
    has no session — it waits behind its hold-stopped flag. Restarting it here
    would silently overturn the console's brake, which this pass cannot read."""
    proc, calls, home = run(tmp_path, "true")
    supervise(home)
    (home / ".mcp-hub" / f"hold-stopped-{AGENT}").touch()
    proc, calls, _ = run(tmp_path, "supervise_pass")
    assert calls == ""


def test_a_placed_seat_is_left_to_the_edge(tmp_path):
    proc, calls, home = run(tmp_path, "true")
    supervise(home)
    proc, calls, _ = run(
        tmp_path, "supervise_pass",
        stubs='placement_id() { echo pl-aaaa000000000001; return 0; }\n')
    assert calls == "", "two supervisors racing one seat is worse than none"
    assert "edge reconciler" in proc.stdout


# --- the rest of the guards ----------------------------------------------

def test_a_live_session_is_not_restarted(tmp_path):
    proc, calls, home = run(tmp_path, "true")
    supervise(home)
    proc, calls, _ = run(tmp_path, "supervise_pass",
                         stubs='tm() { return 0; }\n')
    assert calls == ""


def test_a_row_without_the_resume_flag_is_refused_loudly(tmp_path):
    """Starting it blank would be a lost conversation reported as a recovery."""
    proc, calls, home = run(tmp_path, "true", row=ROW_NO_RESUME)
    supervise(home)
    proc, calls, _ = run(tmp_path, "supervise_pass", row=ROW_NO_RESUME)
    assert calls == ""
    assert "--continue" in proc.stderr and "NOT starting" in proc.stderr


def test_a_failed_launch_is_not_retried_every_pass(tmp_path):
    """The loop bound: if claude cannot boot, a 2-minute timer must not
    respawn it forever."""
    proc, calls, home = run(tmp_path, "true")
    supervise(home)
    proc, calls, _ = run(tmp_path, "supervise_pass")
    assert calls.count("UP_ONE") == 1
    proc, calls, _ = run(tmp_path, "supervise_pass")
    assert calls.count("UP_ONE") == 1, "second pass inside grace must not retry"
    assert "within grace" in proc.stdout


def test_a_marker_outliving_its_roster_row_starts_nothing(tmp_path):
    """safe_name() is one-way, so the pass is driven off the roster. A retired
    lane's leftover marker must not resurrect it."""
    proc, calls, home = run(tmp_path, "true")
    (home / ".mcp-hub" / "supervise-a-lane-that-was-removed").touch()
    proc, calls, _ = run(tmp_path, "supervise_pass")
    assert calls == ""


# --- intent at both doors -------------------------------------------------

def test_the_hands_start_clears_the_want_down_marker(tmp_path):
    """Write intent at both doors or neither: a start that cleared only the
    placement would leave the local marker still saying "stay down"."""
    proc, calls, home = run(tmp_path, "true")
    flag = home / ".mcp-hub" / f"want-down-{AGENT}"
    flag.touch()
    run(tmp_path, f'''
set_placement_intent() {{ :; }}
launch_agent_cmd()     {{ :; }}
wait_shell_ready()     {{ :; }}
untilde()              {{ printf '%s' "$1"; }}
tm()                   {{ [ "$1" = has-session ] && return 1; return 0; }}
mkdir -p /tmp/planner
up_one {AGENT}
''', stub_up_one=False)
    assert not flag.exists(), "up_one is the hand's door and must clear it"
