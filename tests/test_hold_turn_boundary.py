"""#318 leg 2/3: a held lane stops at a TURN BOUNDARY, not mid-turn.

The operator's ruling has three parts this file pins:
  · a hold is applied at the seat's next turn boundary;
  · if the lane is still mid-turn ten minutes later it is HARD-STOPPED, and
    the notice must say the in-flight turn is lost;
  · nothing is stopped by the hook itself — it runs inside the process it
    would be killing, so it observes and stamps, and squad acts.

Direction of failure is fixed: everything here FAILS OPEN. An unreadable
mirror, a malformed entry, an unwritable stamp — all mean NOT HELD. A hold
that cannot be read must never block a turn end.
"""
from __future__ import annotations

import json
import time

import pytest

from mcp_hub import hold


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_HUB_HELD_FILE", str(tmp_path / "held-lanes.json"))
    monkeypatch.setenv("MCP_HUB_HOLD_BOUNDARY_DIR", str(tmp_path / "boundary"))
    return tmp_path


def write_mirror(tmp_path, agent="lane-1", until_in=3600.0, held_ago=0.0,
                 **extra):
    payload = {"generated": time.time(), "held": {agent: {
        "until": time.time() + until_in,
        "held_at": time.time() - held_ago,
        "reason": "over 1.5x fair share",
        "release_condition": "usage window resets at 18:00",
        **extra,
    }}}
    (tmp_path / "held-lanes.json").write_text(json.dumps(payload))


# --- reading the hold ------------------------------------------------------

def test_a_live_hold_is_seen(_isolated):
    write_mirror(_isolated)
    assert hold.held_entry("lane-1")


def test_an_expired_hold_reads_as_released(_isolated):
    write_mirror(_isolated, until_in=-1)
    assert hold.held_entry("lane-1") is None


def test_the_expiry_is_rechecked_not_trusted(_isolated):
    """The mirror can be minutes old; an expired hold must read released
    everywhere it is read, not only where it was written."""
    write_mirror(_isolated, until_in=30)
    assert hold.held_entry("lane-1") is not None
    assert hold.held_entry("lane-1", now=time.time() + 31) is None


def test_another_lanes_hold_does_not_leak(_isolated):
    write_mirror(_isolated, agent="lane-2")
    assert hold.held_entry("lane-1") is None


@pytest.mark.parametrize("body", ["", "{ not json", '{"held": "nope"}',
                                  '{"held": {"lane-1": "nope"}}'])
def test_an_unreadable_mirror_fails_open(_isolated, body):
    (_isolated / "held-lanes.json").write_text(body)
    assert hold.held_entry("lane-1") is None


def test_no_mirror_at_all_fails_open(_isolated):
    assert hold.held_entry("lane-1") is None


# --- the boundary stamp ----------------------------------------------------

def test_a_boundary_is_stamped_once_and_keeps_its_first_moment(_isolated):
    """squad needs the FIRST boundary after the hold. Re-stamping every Stop
    would push the moment forward on an already-stoppable lane."""
    assert hold.stamp_boundary("lane-1", now=1000.0)
    assert hold.stamp_boundary("lane-1", now=2000.0)
    assert hold.boundary_reached_at("lane-1") == 1000.0


def test_a_stamp_is_cleared_when_the_hold_goes(_isolated):
    """A stamp outliving its hold would make the NEXT hold look already
    boundaried — stopping a lane mid-turn under the rule against it."""
    hold.stamp_boundary("lane-1")
    hold.clear_boundary("lane-1")
    assert hold.boundary_reached_at("lane-1") is None


def test_clearing_a_stamp_that_is_not_there_is_quiet(_isolated):
    hold.clear_boundary("never-held")  # must not raise


def test_an_unwritable_stamp_reports_false(_isolated, monkeypatch):
    monkeypatch.setenv("MCP_HUB_HOLD_BOUNDARY_DIR", "/proc/nope/boundary")
    assert hold.stamp_boundary("lane-1") is False


# --- the ten-minute hard stop ---------------------------------------------

def test_a_fresh_hold_is_not_yet_hard_stopped(_isolated):
    write_mirror(_isolated, held_ago=60)
    assert hold.hard_stop_due(hold.held_entry("lane-1"), "lane-1") is False


def test_a_hold_past_ten_minutes_with_no_boundary_is_hard_stopped(_isolated):
    write_mirror(_isolated, held_ago=601)
    assert hold.hard_stop_due(hold.held_entry("lane-1"), "lane-1") is True


def test_a_boundary_cancels_the_hard_stop_however_old_the_hold(_isolated):
    """Reaching a boundary means there is no in-flight turn to lose."""
    write_mirror(_isolated, held_ago=99999)
    hold.stamp_boundary("lane-1")
    assert hold.hard_stop_due(hold.held_entry("lane-1"), "lane-1") is False


def test_the_deadline_runs_from_the_ASK_not_from_the_mirror(_isolated):
    """Measuring from the mirror's own age would restart the clock on every
    edge pass and the hard stop would never arrive."""
    write_mirror(_isolated, held_ago=700)
    entry = hold.held_entry("lane-1")
    assert hold.hard_stop_due(entry, "lane-1") is True
    entry_without = {k: v for k, v in entry.items() if k != "held_at"}
    assert hold.hard_stop_due(entry_without, "lane-1") is False


def test_an_entry_that_cannot_say_when_it_started_never_hard_stops(_isolated):
    """A mirror-format change must not be able to hard-stop the fleet."""
    for bad in ({}, {"held_at": 0}, {"held_at": "soon"}, {"held_at": None}):
        assert hold.hard_stop_due(bad, "lane-1") is False


# --- what the agent is told ------------------------------------------------

def test_the_notice_names_the_release_condition_and_the_expiry(_isolated):
    write_mirror(_isolated)
    text = hold.hook_notice("lane-1", hold.held_entry("lane-1"))
    assert "usage window resets at 18:00" in text
    assert "over 1.5x fair share" in text


def test_the_notice_says_stopping_not_stopped(_isolated):
    """squad acts within one heal pass, so the lane is still up while this
    is read. Announcing a completed stop that has not happened is the
    'delivered live' mistake in a new costume."""
    write_mirror(_isolated)
    text = hold.hook_notice("lane-1", hold.held_entry("lane-1")).lower()
    assert "being stopped" in text
    assert "has been stopped" not in text


# --- the hook's own gate: a held lane must SURFACE ---------------------------
#
# Being stopped is the one thing an agent must not learn by having its pane
# disappear. So the notice has to beat every path that would return None.

from mcp_hub.cli import build_hook_response  # noqa: E402


def base(**kw):
    args = dict(agent_name="lane-1", project="org/repo", messages_text="",
                broadcasts_text="", is_online=True)
    args.update(kw)
    return build_hook_response(**args)


def test_the_happy_path_still_stays_quiet_when_nothing_is_held():
    """Negative control — the steady state must not start blocking."""
    assert base() is None


def test_a_held_lane_blocks_even_with_an_empty_inbox():
    out = base(held_notice="⏸️ THIS LANE IS HELD")
    assert out is not None
    assert "HELD" in out["reason"]


def test_a_held_lane_blocks_even_on_a_re_fired_stop():
    """The loop backstop exists to stop content-less re-blocks. A lane about
    to be stopped is not content-less."""
    assert base(held_notice="⏸️ HELD", stop_hook_active=True) is not None


def test_the_hold_notice_comes_first():
    out = base(held_notice="⏸️ HELD", messages_text="[..] someone: hi")
    assert out["reason"].startswith("⏸️ HELD")


def test_a_held_lanes_traffic_is_never_deferred_into_the_low_spool(monkeypatch):
    """bar 47's low-only deferral returns None. On a held lane that would
    swallow the last warning it gets."""
    out = base(held_notice="⏸️ HELD", defer_low=True,
               messages_text="[12:00] a → lane-1 ⟨hub.msg/1?id=1⟩ [low]: fyi")
    assert out is not None
    assert "HELD" in out["reason"]
