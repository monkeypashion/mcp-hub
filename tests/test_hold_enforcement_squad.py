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


def harness(tmp_path, *, held, agent="lane-a", args="--continue",
            running=True, boundary=False, stopped_flag=False):
    """Lay out a HOME, a roster and a mirror, then run one snippet."""
    home = tmp_path
    (home / ".mcp-hub").mkdir(parents=True, exist_ok=True)
    conf = home / "squad.conf"
    conf.write_text(f"{agent}|{home}||{args}|squad\n", encoding="utf-8")

    heldf = home / "held.json"
    heldf.write_text(json.dumps({"generated": time.time(),
                                 "held": held or {}}), encoding="utf-8")

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
             "MCP_HUB_HOLD_BOUNDARY_DIR": str(bdir)},
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
