"""#318 legs 3/4 in squad: stopping a RUNNING held lane, and releasing it.

squad owns lane lifecycle, so squad does the stopping — the lane's own Stop
hook cannot (it runs inside the process it would kill) and the edge must not
(its lane leg exists precisely so lanes have no second lifecycle owner).

Driven by SOURCING the real script through its `SQUAD_SOURCE_ONLY` seam and
calling the functions with tmux stubbed, so these exercise the shipped code
rather than a transcription of it.
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

SQUAD = Path(__file__).resolve().parents[1] / "squad" / "squad"
jq_missing = not Path("/usr/bin/jq").exists()
pytestmark = pytest.mark.skipif(jq_missing, reason="needs jq")


def pace_body(used, elapsed):
    return json.dumps({"checks": [{"used_percentage": used,
                                   "window_elapsed_percentage": elapsed,
                                   "margin": 0.0, "ok": used <= elapsed}]})


def harness(tmp_path, *, held, agent="lane-a", args="--continue",
            running=True, boundary=False, stopped_flag=False,
            pace=pace_body(10, 90)):
    """Lay out a HOME, a roster, a mirror and a pace, then run one snippet.

    ⚠️ `pace` is not decoration. `hold_release_pass` now reads the week pace
    before it restarts anything, and the default URL is the live console on
    this machine — so a harness that did not pin it would send every release
    test to a real service whose answer changes hourly, and the suite would
    pass or fail on the fleet's actual burn. Default is FAR under the line, so
    every pre-existing test means what it did before. `None` writes no file at
    all, which is the unreadable case.
    """
    home = tmp_path
    (home / ".mcp-hub").mkdir(parents=True, exist_ok=True)
    conf = home / "squad.conf"
    conf.write_text(f"{agent}|{home}||{args}|squad\n", encoding="utf-8")

    heldf = home / "held.json"
    heldf.write_text(json.dumps({"generated": time.time(),
                                 "held": held or {}}), encoding="utf-8")

    if pace is not None:
        (home / "pace.json").write_text(pace, encoding="utf-8")

    bdir = home / "boundary"
    bdir.mkdir(exist_ok=True)
    if boundary:
        (bdir / f"{agent}.json").write_text('{"reached_at": 1}')
    if stopped_flag:
        (home / ".mcp-hub" / f"hold-stopped-{agent}").write_text("")

    # tmux stub: records every call, and answers has-session per `running`.
    bin_ = home / "bin"
    bin_.mkdir(exist_ok=True)
    (bin_ / "tmux").write_text(
        "#!/bin/bash\n"
        f'echo "$@" >> {home}/tmux.log\n'
        'for a in "$@"; do\n'
        '  if [ "$a" = "has-session" ]; then\n'
        f'    exit {0 if running else 1}\n'
        '  fi\n'
        # pane_alive asks for the pane's current command and treats a shell
        # (or nothing) as dead. A stub that answered neither made every
        # enforcement test pass vacuously by returning before it acted.
        '  if [ "$a" = "display-message" ]; then\n'
        f'    {"echo node" if running else "true"}; exit 0\n'
        '  fi\n'
        'done\n'
        'exit 0\n'
    )
    (bin_ / "tmux").chmod(0o755)
    return home, conf, heldf, bdir, bin_


def call(home, conf, heldf, bdir, bin_, snippet):
    """Run `snippet` with squad's helpers in scope, dispatch excluded.

    Same extraction as test_squad_comms, and written to a FILE for the same
    reason: a single argv entry is capped at 128 KiB on Linux, and squad's
    helper region is already over it. `bash -c <the whole region>` fails with
    "Argument list too long", which reads as a broken harness.
    """
    head = SQUAD.read_text(encoding="utf-8").split(
        '\ncase "${1:-help}" in', 1)[0]
    assert "hold_enforce_one()" in head, "extraction boundary moved"
    script = home / "_squad_head.sh"
    script.write_text(head + "\n" + snippet, encoding="utf-8")
    return subprocess.run(
        ["bash", str(script)],
        capture_output=True, text=True,
        env={"PATH": f"{bin_}:/usr/bin:/bin", "HOME": str(home),
             "SQUAD_CONF": str(conf), "MCP_HUB_HELD_FILE": str(heldf),
             "MCP_HUB_HOLD_BOUNDARY_DIR": str(bdir),
             # Never the live console. See `harness`.
             "MCP_HUB_PACE_URL": f"file://{home}/pace.json"},
    )


def held_entry(until_in=3600.0, held_ago=0.0, cond="window resets at 18:00"):
    return {"until": time.time() + until_in,
            "held_at": time.time() - held_ago,
            "reason": "over 1.5x fair share",
            "release_condition": cond}


# --- the clean stop: at a turn boundary -----------------------------------

def test_a_held_lane_that_reached_a_boundary_is_stopped(tmp_path):
    h = harness(tmp_path, held={"lane-a": held_entry()}, boundary=True)
    p = call(*h, "hold_enforce_one lane-a")
    assert "stopping at its turn boundary" in p.stdout
    assert "kill-session" in (tmp_path / "tmux.log").read_text()


def test_the_clean_stop_never_claims_a_turn_was_lost(tmp_path):
    """Nothing is in flight at a boundary — saying otherwise would make the
    hard stop's warning meaningless."""
    h = harness(tmp_path, held={"lane-a": held_entry()}, boundary=True)
    p = call(*h, "hold_enforce_one lane-a")
    assert "LOST" not in (p.stdout + p.stderr)


# --- mid-turn: wait, then his ten-minute hard stop -------------------------

def test_a_mid_turn_lane_is_left_alone_inside_the_grace(tmp_path):
    h = harness(tmp_path, held={"lane-a": held_entry(held_ago=60)})
    p = call(*h, "hold_enforce_one lane-a")
    assert "waiting for its turn boundary" in p.stdout
    assert "kill-session" not in (tmp_path / "tmux.log").read_text()


def test_a_mid_turn_lane_past_ten_minutes_is_hard_stopped(tmp_path):
    h = harness(tmp_path, held={"lane-a": held_entry(held_ago=700)})
    p = call(*h, "hold_enforce_one lane-a")
    assert "HARD-STOPPED" in p.stderr
    assert "kill-session" in (tmp_path / "tmux.log").read_text()


def test_the_hard_stop_says_the_in_flight_turn_is_lost(tmp_path):
    """His words, and the notice MUST carry them: a stop announced as clean
    when a turn died in it teaches the operator to distrust every other line."""
    h = harness(tmp_path, held={"lane-a": held_entry(held_ago=700)})
    p = call(*h, "hold_enforce_one lane-a")
    assert "IN-FLIGHT TURN IS LOST" in p.stderr


def test_an_entry_with_no_held_at_never_hard_stops(tmp_path):
    """A mirror-format change must not be able to take out the fleet."""
    entry = held_entry(held_ago=99999)
    del entry["held_at"]
    h = harness(tmp_path, held={"lane-a": entry})
    p = call(*h, "hold_enforce_one lane-a")
    assert "hard stop cannot be timed" in p.stdout
    assert "kill-session" not in (tmp_path / "tmux.log").read_text()


# --- what must NOT be stopped ---------------------------------------------

def test_an_unheld_lane_is_untouched(tmp_path):
    h = harness(tmp_path, held={}, boundary=True)
    p = call(*h, "hold_enforce_one lane-a")
    assert p.stdout.strip() == ""
    assert not (tmp_path / "tmux.log").exists()


def test_an_expired_hold_stops_nothing(tmp_path):
    h = harness(tmp_path, held={"lane-a": held_entry(until_in=-5)},
                boundary=True)
    call(*h, "hold_enforce_one lane-a")
    assert not (tmp_path / "tmux.log").exists()


def test_a_lane_that_is_already_down_is_not_killed_again(tmp_path):
    h = harness(tmp_path, held={"lane-a": held_entry()}, boundary=True,
                running=False)
    call(*h, "hold_enforce_one lane-a")
    assert "kill-session" not in (tmp_path / "tmux.log").read_text()


def test_a_missing_mirror_holds_nobody(tmp_path):
    """FAILS OPEN, in this direction only. A dead edge must not freeze the
    fleet; the price is that it un-holds lanes, which the expiry covers."""
    home, conf, heldf, bdir, bin_ = harness(tmp_path, held={})
    heldf.unlink()
    p = call(home, conf, heldf, bdir, bin_, "hold_enforce_pass")
    assert p.stdout.strip() == ""


# --- release: the lane comes back, with its conversation -------------------

def test_a_released_lane_is_restarted_with_continue(tmp_path):
    h = harness(tmp_path, held={}, running=False, stopped_flag=True)
    p = call(*h, "hold_release_pass")
    assert "RELEASED" in p.stdout
    assert "--continue" in p.stdout


def test_a_still_held_lane_is_not_released(tmp_path):
    h = harness(tmp_path, held={"lane-a": held_entry()}, running=False,
                stopped_flag=True)
    p = call(*h, "hold_release_pass")
    assert "RELEASED" not in p.stdout
    assert (tmp_path / ".mcp-hub" / "hold-stopped-lane-a").exists()


def test_a_stale_mirror_restarts_nobody(tmp_path):
    """The 01:00:44Z shape, inverted. Release FAILS CLOSED where enforcement
    fails open: an unreadable or stale mirror is not evidence that a hold
    ended, and restarting on it is how a held lane comes back burning its
    share while the pass reports a clean release."""
    home, conf, heldf, bdir, bin_ = harness(
        tmp_path, held={}, running=False, stopped_flag=True)
    heldf.write_text(json.dumps({"generated": time.time() - 3600,
                                 "held": {}}), encoding="utf-8")
    p = call(home, conf, heldf, bdir, bin_, "hold_release_pass")
    assert "RELEASED" not in p.stdout
    assert "stale" in p.stderr
    assert (tmp_path / ".mcp-hub" / "hold-stopped-lane-a").exists(), \
        "the flag must survive — this DEFERS the release, never cancels it"


def test_a_missing_mirror_restarts_nobody(tmp_path):
    home, conf, heldf, bdir, bin_ = harness(
        tmp_path, held={}, running=False, stopped_flag=True)
    heldf.unlink()
    p = call(home, conf, heldf, bdir, bin_, "hold_release_pass")
    assert "RELEASED" not in p.stdout
    assert (tmp_path / ".mcp-hub" / "hold-stopped-lane-a").exists()


def test_a_mirror_with_no_generated_stamp_is_unknown_not_fresh(tmp_path):
    """Absent is UNKNOWN. A writer too old to stamp the snapshot is exactly
    the one whose freshness nobody can vouch for."""
    home, conf, heldf, bdir, bin_ = harness(
        tmp_path, held={}, running=False, stopped_flag=True)
    heldf.write_text(json.dumps({"held": {}}), encoding="utf-8")
    p = call(home, conf, heldf, bdir, bin_, "hold_release_pass")
    assert "RELEASED" not in p.stdout


def test_a_fresh_mirror_still_releases(tmp_path):
    """The positive control. Without it the three above pass for a gate that
    refuses everything, which is not a tightening but a break."""
    h = harness(tmp_path, held={}, running=False, stopped_flag=True)
    p = call(*h, "hold_release_pass")
    assert "RELEASED" in p.stdout
    assert "stale" not in p.stderr


def test_a_lane_without_the_resume_flag_is_named_not_restarted_blank(tmp_path):
    """Coming back with no conversation is a loss reported as a release."""
    h = harness(tmp_path, held={}, args="", running=False, stopped_flag=True)
    p = call(*h, "hold_release_pass")
    assert "NOT restarting" in p.stderr
    assert "kill-session" not in (tmp_path / "tmux.log").read_text() \
        if (tmp_path / "tmux.log").exists() else True


def test_a_flag_for_a_retired_agent_resurrects_nothing(tmp_path):
    h = harness(tmp_path, held={}, running=False)
    (tmp_path / ".mcp-hub" / "hold-stopped-ghost-lane").write_text("")
    p = call(*h, "hold_release_pass")
    assert "no longer on the roster" in p.stderr
    assert not (tmp_path / ".mcp-hub" / "hold-stopped-ghost-lane").exists()


def test_a_lane_already_back_up_just_clears_its_flag(tmp_path):
    h = harness(tmp_path, held={}, running=True, stopped_flag=True)
    p = call(*h, "hold_release_pass")
    assert "RELEASED" not in p.stdout
    assert not (tmp_path / ".mcp-hub" / "hold-stopped-lane-a").exists()


def test_no_flags_at_all_is_a_quiet_no_op(tmp_path):
    """The glob must not match its own literal when nothing is there."""
    h = harness(tmp_path, held={}, running=False)
    p = call(*h, "hold_release_pass")
    assert p.stdout.strip() == "" and p.stderr.strip() == ""


# --- the week pace: a release is a restart, and a restart costs a lane -----
#
# His word 2026-09-11: while the week's used% is over its elapsed%, nobody
# restarts a lane. Overnight the release pass spent five restarts on one lane
# at each hour boundary while the week was already over the line.

def test_over_the_line_defers_the_restart_and_keeps_the_flag(tmp_path):
    """The defect itself. Over the line, a released lane is NOT restarted —
    and the flag survives, so this defers the release rather than cancelling
    it, exactly as the stale-mirror gate does."""
    h = harness(tmp_path, held={}, running=False, stopped_flag=True,
                pace=pace_body(61, 51.6))
    p = call(*h, "hold_release_pass")
    assert "RELEASED" not in p.stdout
    assert "over the line" in p.stderr
    assert "not restarting, flag kept" in p.stderr
    assert "lane-a" in p.stderr, "the line must name the lane it held back"
    assert (tmp_path / ".mcp-hub" / "hold-stopped-lane-a").exists()


def test_under_the_line_restarts_exactly_as_before(tmp_path):
    """MUST-FIRE, and the positive control for all three gates below: without
    it they pass for a pace read that refuses everything, which is a break
    wearing a tightening's clothes."""
    h = harness(tmp_path, held={}, running=False, stopped_flag=True,
                pace=pace_body(40, 51.6))
    p = call(*h, "hold_release_pass")
    assert "RELEASED" in p.stdout
    assert "--continue" in p.stdout
    assert "over the line" not in p.stderr


def test_exactly_on_the_line_still_restarts(tmp_path):
    """His rule is used% > elapsed%, margin 0. Equal is ON the line, and a
    lane held back there would never be released by a pace that only ever
    touches its own line from above."""
    h = harness(tmp_path, held={}, running=False, stopped_flag=True,
                pace=pace_body(51.6, 51.6))
    p = call(*h, "hold_release_pass")
    assert "RELEASED" in p.stdout


def test_an_unreadable_pace_restarts_nobody(tmp_path):
    """Fail closed, the same shape as the stale mirror: a pace nobody can read
    is not evidence that we are under the line. `None` writes no file, so the
    real curl fails the way an unreachable console does."""
    h = harness(tmp_path, held={}, running=False, stopped_flag=True,
                pace=None)
    p = call(*h, "hold_release_pass")
    assert "RELEASED" not in p.stdout
    assert "unreadable" in p.stderr
    assert (tmp_path / ".mcp-hub" / "hold-stopped-lane-a").exists()


def test_a_non_numeric_pace_is_unreadable_not_zero(tmp_path):
    """awk reads "n/a" as 0, which would report the fleet comfortably under a
    line it never measured — the most expensive way for this gate to fail."""
    h = harness(tmp_path, held={}, running=False, stopped_flag=True,
                pace=json.dumps({"checks": [{"used_percentage": "n/a",
                                             "window_elapsed_percentage": 51.6}]}))
    p = call(*h, "hold_release_pass")
    assert "RELEASED" not in p.stdout
    assert "unreadable" in p.stderr


def test_a_pace_payload_with_no_checks_is_unreadable(tmp_path):
    """A shape change at the console must not read as `under`."""
    h = harness(tmp_path, held={}, running=False, stopped_flag=True,
                pace=json.dumps({"checks": [], "ok": True}))
    p = call(*h, "hold_release_pass")
    assert "RELEASED" not in p.stdout
    assert "unreadable" in p.stderr


def test_the_pace_is_not_consulted_when_there_is_nothing_to_release(tmp_path):
    """Only a waiting release pays for the read. An unreachable console must
    not make an empty pass noisy every two minutes."""
    h = harness(tmp_path, held={}, running=False, pace=None)
    p = call(*h, "hold_release_pass")
    assert p.stdout.strip() == "" and p.stderr.strip() == ""


def test_a_retired_agents_flag_is_still_cleared_over_the_line(tmp_path):
    """The pace gate guards the RESTART, not the bookkeeping. A ghost flag
    that outlives its agent starts nothing, so holding it back over the line
    would only make it immortal."""
    h = harness(tmp_path, held={}, running=False, pace=pace_body(61, 51.6))
    (tmp_path / ".mcp-hub" / "hold-stopped-ghost-lane").write_text("")
    p = call(*h, "hold_release_pass")
    assert "no longer on the roster" in p.stderr
    assert not (tmp_path / ".mcp-hub" / "hold-stopped-ghost-lane").exists()


def test_a_still_held_lane_is_not_reported_as_over_the_line(tmp_path):
    """It is not being released at all — naming it here would teach the
    operator that the pace is what is keeping a held lane down."""
    h = harness(tmp_path, held={"lane-a": held_entry()}, running=False,
                stopped_flag=True, pace=pace_body(61, 51.6))
    p = call(*h, "hold_release_pass")
    assert "over the line" not in p.stderr
