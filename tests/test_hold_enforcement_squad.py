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


def pace_body(mode, used=84, elapsed=76.5):
    """The console's real /ceiling/pace shape, with the WEEK fields deliberately
    left OVER the line (84 > 76.5 — the live reading at 00:19Z 13 Sep).

    That is the instrument, not decoration: under his revised rule the served
    `mode` and the week comparison DISAGREE, so a body whose week fields agreed
    with its mode could not tell a gate reading `mode` from one still reading
    used% vs elapsed%. Pass `mode=None` for a payload that carries no verdict.
    """
    week_ok = used <= elapsed
    body = {"checks": [{"used_percentage": used,
                        "window_elapsed_percentage": elapsed,
                        "margin": 0.0, "ok": week_ok,
                        "daily": {"share": 9.6, "spent_today": 0,
                                  "ok": mode == "TURBO",
                                  "basis": "00:00Z reading"}}],
            "ok": week_ok,
            "asserts": "used% <= window-elapsed% + margin, per live credential"}
    if mode is not None:
        body["mode"] = mode
    return json.dumps(body)


def harness(tmp_path, *, held, agent="lane-a", args="--continue",
            running=True, boundary=False, stopped_flag=False,
            pace=pace_body("TURBO"), members=None):
    """Lay out a HOME, a roster, a mirror and a pace, then run one snippet.

    ⚠️ `pace` is not decoration. `hold_release_pass` now reads the week pace
    before it restarts anything, and the default URL is the live console on
    this machine — so a harness that did not pin it would send every release
    test to a real service whose answer changes hourly, and the suite would
    pass or fail on the fleet's actual burn. Default is FAR under the line, so
    every pre-existing test means what it did before — the default is TURBO,
    the console's "under today's share". `None` writes no file at all, which is
    the unreachable case.
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

    # mcp-hub stub: the SQUAD MEMBERSHIP the controls are bounded by.
    #
    # ⚠️ Not decoration, and not optional. Since hub.msg 26539 the stop and
    # the relaunch are gated on membership, and that gate FAILS CLOSED — so a
    # harness with no stub would answer "membership unreadable", refuse every
    # stop, and the suite would report the bound working when what it had
    # actually proven is that an unreadable hub stops nothing.
    #
    # Default: the harness agent IS a member, so every pre-existing test still
    # means exactly what it meant before the bound existed. `members=[]` makes
    # the list UNREADABLE (squad's loader treats empty output as "!"), and
    # `members=["someone-else"]` is readable-but-excluded — the two cases a
    # single "no members" fixture would silently conflate.
    hub_members = [agent] if members is None else list(members)
    (bin_ / "mcp-hub").write_text(
        "#!/bin/bash\n"
        'if [ "$1" = "squads" ] && [ "$2" = "members" ]; then\n'
        + "".join(f'  echo "{m}"\n' for m in hub_members)
        + "  exit 0\n"
        "fi\n"
        "exit 0\n"
    )
    (bin_ / "mcp-hub").chmod(0o755)
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


# --- the pace gate: a release is a restart, and a restart costs a lane -----
#
# His word 2026-09-11: while we are over the line, nobody restarts a lane. The
# release pass had spent five restarts on one lane at each hour boundary.
#
# ⭐ His rule REVISED 2026-09-12 20:3xZ: the week line (used% vs elapsed%) is a
# TARGET; the brake is a DAILY share, and the console serves that verdict as a
# top-level `mode` — TURBO under, SILENT over. This gate reads `mode` and does
# not recompute the line, so every body below carries week fields that are OVER
# (84 > 76.5) while its mode varies. A gate still reading the old predicate
# fails every TURBO case here; one reading `mode` passes them all.

def test_silent_defers_the_restart_and_keeps_the_flag(tmp_path):
    """MUST-FIRE. Over the day's share, a released lane is NOT restarted — and
    the flag survives, so this defers the release rather than cancelling it,
    exactly as the stale-mirror gate does."""
    h = harness(tmp_path, held={}, running=False, stopped_flag=True,
                pace=pace_body("SILENT"))
    p = call(*h, "hold_release_pass")
    assert "RELEASED" not in p.stdout
    assert "over the line" in p.stderr
    assert "not restarting, flag kept" in p.stderr
    assert "lane-a" in p.stderr, "the line must name the lane it held back"
    assert (tmp_path / ".mcp-hub" / "hold-stopped-lane-a").exists()


def test_turbo_restarts_the_lane(tmp_path):
    """MUST-FIRE, and the positive control for every gate below: without it
    they all pass for a pace read that refuses everything, which is a break
    wearing a tightening's clothes.

    It is also the DEFECT this commit fixes. The week fields in this body are
    over the line (84 > 76.5) — the live 00:19Z reading — so the old predicate
    parked this lane while the served mode was TURBO."""
    h = harness(tmp_path, held={}, running=False, stopped_flag=True,
                pace=pace_body("TURBO"))
    p = call(*h, "hold_release_pass")
    assert "RELEASED" in p.stdout
    assert "--continue" in p.stdout
    assert "over the line" not in p.stderr


def test_the_week_line_no_longer_decides_anything(tmp_path):
    """The week fields are now a TARGET. Stated separately from the must-fire
    above because it is a distinct claim: not merely that TURBO restarts, but
    that the strongest possible week-line objection — `checks[0].ok` false,
    top-level `ok` false, used% seven points over — cannot override it."""
    h = harness(tmp_path, held={}, running=False, stopped_flag=True,
                pace=pace_body("TURBO", used=99, elapsed=10))
    p = call(*h, "hold_release_pass")
    assert "RELEASED" in p.stdout


def test_an_unreachable_console_restarts_nobody(tmp_path):
    """Fail closed, the same shape as the stale mirror: a pace nobody can read
    is not evidence that we are under the line. `None` writes no file, so the
    real curl fails the way an unreachable console does."""
    h = harness(tmp_path, held={}, running=False, stopped_flag=True,
                pace=None)
    p = call(*h, "hold_release_pass")
    assert "RELEASED" not in p.stdout
    assert "unreadable" in p.stderr
    assert (tmp_path / ".mcp-hub" / "hold-stopped-lane-a").exists()


def test_a_payload_with_no_mode_is_unknown_not_under(tmp_path):
    """MUST-FIRE. The console before 22:08Z 12 Sep served exactly this shape,
    and so does any rollback to it. A missing verdict must read UNKNOWN — the
    absent field defaulting to "" and falling through to `under` is the whole
    failure mode this case exists to pin."""
    h = harness(tmp_path, held={}, running=False, stopped_flag=True,
                pace=pace_body(None))
    p = call(*h, "hold_release_pass")
    assert "RELEASED" not in p.stdout
    assert "unreadable" in p.stderr
    assert (tmp_path / ".mcp-hub" / "hold-stopped-lane-a").exists()


def test_an_unrecognised_mode_is_unknown_not_under(tmp_path):
    """Only the two named values decide. A third mode invented next month must
    not be able to start a lane by being unfamiliar — an allow-list, not a
    SILENT-check with everything else passing."""
    h = harness(tmp_path, held={}, running=False, stopped_flag=True,
                pace=pace_body("PAUSED"))
    p = call(*h, "hold_release_pass")
    assert "RELEASED" not in p.stdout
    assert "unreadable" in p.stderr


def test_the_mode_is_read_case_insensitively(tmp_path):
    """Normalising case is not a fail-open: the allow-list still admits only
    the two values after it. Pinned so a lowercase serve does not park the
    fleet on a cosmetic difference."""
    h = harness(tmp_path, held={}, running=False, stopped_flag=True,
                pace=pace_body("turbo"))
    p = call(*h, "hold_release_pass")
    assert "RELEASED" in p.stdout


def test_a_non_json_body_is_unknown(tmp_path):
    """A console serving an error page reads as no verdict at all."""
    h = harness(tmp_path, held={}, running=False, stopped_flag=True,
                pace="<html>502 Bad Gateway</html>")
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
    h = harness(tmp_path, held={}, running=False, pace=pace_body("SILENT"))
    (tmp_path / ".mcp-hub" / "hold-stopped-ghost-lane").write_text("")
    p = call(*h, "hold_release_pass")
    assert "no longer on the roster" in p.stderr
    assert not (tmp_path / ".mcp-hub" / "hold-stopped-ghost-lane").exists()


def test_a_still_held_lane_is_not_reported_as_over_the_line(tmp_path):
    """It is not being released at all — naming it here would teach the
    operator that the pace is what is keeping a held lane down."""
    h = harness(tmp_path, held={"lane-a": held_entry()}, running=False,
                stopped_flag=True, pace=pace_body("SILENT"))
    p = call(*h, "hold_release_pass")
    assert "over the line" not in p.stderr


# --- the squad bound on the controls (his ruling, hub.msg 26539) -----------
#
# "any controls like that should only apply to dreamteam" — the heal script's
# hold/stop/restart pass bounded by squad membership, "a lane outside it is
# never stopped, flagged or restarted, whatever held-lanes.json says".
#
# Measured cause, 2026-09-13: the pass had a scope gate on the ASK leg only.
# It ran 363 times that day and named hub-voice-dev-vm-1 out-of-squad every
# time, while the restart path started that same lane 81 times — 84 executed
# starts of out-of-scope lanes in one day. The gate was not missing; it was
# missing from the routes that ACT.


def test_an_out_of_squad_lane_is_not_stopped_however_held_it_is(tmp_path):
    """The held entry is fully valid and the boundary is reached: everything
    except membership says stop. Only the bound prevents it."""
    h = harness(tmp_path, held={"lane-a": held_entry()}, boundary=True,
                members=["someone-else"])
    p = call(*h, "hold_enforce_one lane-a")
    assert "kill-session" not in (tmp_path / "tmux.log").read_text()
    assert "NOT stopped" in p.stdout


def test_an_out_of_squad_lane_is_not_relaunched(tmp_path):
    """The other acting route. Gated at its own door, because heal reaches a
    restart from two loops and guarding one is the 'four of five routes' bug."""
    h = harness(tmp_path, held={}, members=["someone-else"])
    p = call(*h, "relaunch_agent lane-a")
    log = tmp_path / "tmux.log"
    assert "respawn-pane" not in (log.read_text() if log.exists() else "")
    assert "NOT relaunched" in p.stdout


def test_the_refusal_names_the_lane_and_says_why(tmp_path):
    """'log it and move on' — a bound that drops lanes silently is
    indistinguishable from a heal that has stopped working."""
    h = harness(tmp_path, held={"lane-a": held_entry()}, boundary=True,
                members=["someone-else"])
    p = call(*h, "hold_enforce_one lane-a")
    assert "lane-a" in p.stdout
    assert "squad-bounded" in p.stdout


def test_unreadable_membership_stops_nobody(tmp_path):
    """FAIL CLOSED. When the hub is unreachable every lane reads offline, so
    an ungated pass would judge the whole fleet broken at once. Distinct from
    the excluded case above: here membership was never read at all."""
    h = harness(tmp_path, held={"lane-a": held_entry()}, boundary=True,
                members=[])
    p = call(*h, "hold_enforce_one lane-a")
    assert "kill-session" not in (tmp_path / "tmux.log").read_text()
    assert "NOT stopped" in p.stdout


def test_unreadable_membership_relaunches_nobody(tmp_path):
    h = harness(tmp_path, held={}, members=[])
    p = call(*h, "relaunch_agent lane-a")
    log = tmp_path / "tmux.log"
    assert "respawn-pane" not in (log.read_text() if log.exists() else "")
    assert "NOT relaunched" in p.stdout


# --- MUST-FIRE: the bound must not become a way of never acting -----------
#
# His words: "a dreamteam lane held is still stopped at its boundary as
# today." Without these two, every assertion above is satisfied by a gate
# that refuses everything, which is the failure mode a fail-closed change is
# most likely to ship.


def test_must_fire_an_in_squad_lane_is_still_stopped_at_its_boundary(tmp_path):
    h = harness(tmp_path, held={"lane-a": held_entry()}, boundary=True,
                members=["lane-a", "someone-else"])
    p = call(*h, "hold_enforce_one lane-a")
    assert "kill-session" in (tmp_path / "tmux.log").read_text()
    assert "stopping at its turn boundary" in p.stdout
    assert "NOT stopped" not in p.stdout


def test_must_fire_an_in_squad_lane_is_still_relaunched(tmp_path):
    h = harness(tmp_path, held={}, members=["lane-a", "someone-else"])
    p = call(*h, "relaunch_agent lane-a")
    assert "respawn-pane" in (tmp_path / "tmux.log").read_text()
    assert "NOT relaunched" not in p.stdout
