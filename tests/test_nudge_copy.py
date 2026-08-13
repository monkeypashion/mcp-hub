"""heal's deaf-agent nudge: which advice fires, asserted per branch.

The 2026-08-12 overnight incident: the one-copy nudge prescribed relaunches
to healthy seats for a FLEET-WIDE self-clearing condition (idle wake-streams
dying everywhere at once), and the prescribed repair was unfalsifiable — a
fresh session is active by definition and gets ⚡ immediately, so "relaunch
fixed it" could never test false. pm/RA/fo dismantled it between midnight
and dawn; `nudge_copy` is the rewrite, extracted pure (heal_action pattern)
so every branch is assertable without a hub, a daemon, or a fleet.
"""

from __future__ import annotations

import pathlib
import subprocess

import pytest

SQUAD = pathlib.Path(__file__).resolve().parents[1] / "squad" / "squad"

pytestmark = pytest.mark.skipif(not SQUAD.exists(),
                                reason="squad script not present")


def _copy(state: str, churn: int, fw: str = "", ft: str = "") -> str:
    res = subprocess.run(
        ["bash", "-c",
         'source "$1" help >/dev/null 2>&1; nudge_copy "$2" "$3" "$4" "$5"',
         "_", str(SQUAD), state, str(churn), fw, ft],
        capture_output=True, text=True, timeout=60,
    )
    assert res.returncode == 0, res.stderr
    return res.stdout


def test_hub_restart_churn_wins_over_everything():
    """A fresh reconnect stamp means the hub itself just restarted — that
    explanation outranks even a fleet-wide reading, and the copy must not
    breathe the word relaunch."""
    out = _copy("offline", 1, "0", "14")
    assert "just restarted" in out
    assert "relaunch" not in out.lower().replace("no relaunch needed", "")


def test_a_fleet_wide_outage_reads_environmental_and_forbids_relaunch():
    """3 of 14 ⚡ (and 0 of 14): a dead per-session stream cannot explain
    eleven agents in lockstep. Per-session advice here is a wrong-repair
    with a ~14x multiplier."""
    for fw in ("3", "0"):
        out = _copy("offline", 0, fw, "14")
        assert "ENVIRONMENTAL" in out
        assert "do NOT relaunch" in out
        assert "a relaunch will" not in out


def test_a_healthy_fleet_gets_the_per_seat_advice_made_falsifiable():
    """12 of 14 ⚡: the seat may genuinely be dead — the old advice stands,
    but judged by consequence (queued items), gated on recurrence while the
    fleet stays ⚡, and bounded to a quiet window."""
    out = _copy("offline", 0, "12", "14")
    assert "CONSEQUENCE" in out
    assert "QUIET window" in out
    assert "recurs while the rest of the fleet stays" in out


def test_missing_counts_fall_through_to_per_seat_not_environmental():
    """An absent instrument is not evidence of 'environmental' — no counts
    means the stale-cache case, and the copy must neither claim the fleet
    is healthy nor that it is down."""
    out = _copy("offline", 0, "", "")
    assert "ENVIRONMENTAL" not in out
    assert "mostly ⚡" not in out          # no unmeasured fleet claim
    assert "QUIET window" in out


def test_a_small_fleet_never_triggers_the_environmental_read():
    """1 of 3 ⚡ is one seat's weather, not a front — below 5 seats the
    ratio is noise and the per-seat copy must stand."""
    out = _copy("offline", 0, "1", "3")
    assert "ENVIRONMENTAL" not in out


def test_the_threshold_is_one_third():
    """Boundary: 5 of 15 fires (exactly a third); 6 of 15 does not."""
    assert "ENVIRONMENTAL" in _copy("offline", 0, "5", "15")
    assert "ENVIRONMENTAL" not in _copy("offline", 0, "6", "15")
