"""The cockpit's stop must outlive the next edge pass.

`squad stop` killed tmux and retired the daemon, both purely local, and left
the hub's placement at desired=running — so edge.py plan() re-planned an
`op: start` on its next pass (30s+jitter, or instantly via the edge-watch
doorbell) and the seat was back before the operator saw it go. Their words:
"I keep shutting you down via squad cockpit context command but every time I
find you are running again." An imperative kill wired into a declarative
system can never mean "stay down".

Diagnosed by mindconnect-iot2050-dev-vm-1 and routed here 2026-09-17.

⚠️ The write leg is exercised ONLY against a stubbed cli. `placements set` on
a real row stops a real seat on somebody else's lane, so these tests must
never reach a live hub — the stub records the argv instead, which is the thing
worth asserting anyway: that the RIGHT id gets the RIGHT desired state.

Same `SQUAD_SOURCE_ONLY`-style extraction as test_hold_enforcement_squad, so
these run the shipped code rather than a transcription of it.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

SQUAD = Path(__file__).resolve().parents[1] / "squad" / "squad"

# One row per (id, seat) — the real `placements list` column order, which is
# what placement_id's awk depends on. A change to that print() must break a
# test here, not a stop button in the cockpit.
ROWS = [
    ("pl-aaaa000000000001", "lane-with-placement-dev-vm-1", "running"),
    ("pl-bbbb000000000002", "other-lane-dev-vm-1", "running"),
    ("pl-cccc000000000003", "twinned-lane-dev-vm-1", "running"),
    ("pl-dddd000000000004", "twinned-lane-dev-vm-1", "running"),  # same seat!
]


def harness(tmp_path, *, set_fails=False):
    home = tmp_path
    (home / ".mcp-hub").mkdir(parents=True, exist_ok=True)
    bin_ = home / "bin"
    bin_.mkdir(exist_ok=True)

    listing = "".join(
        f'  echo "{pid} {seat:<30} dev-vm-1         '
        f'want {desired:<9} saw {desired:<9} converged"\n'
        for pid, seat, desired in ROWS
    )
    (bin_ / "mcp-hub").write_text(
        "#!/bin/bash\n"
        f'echo "$@" >> {home}/hub.log\n'
        'if [ "$1" = "placements" ] && [ "$2" = "list" ]; then\n'
        + listing
        + "  exit 0\n"
        "fi\n"
        'if [ "$1" = "placements" ] && [ "$2" = "set" ]; then\n'
        f'  exit {1 if set_fails else 0}\n'
        "fi\n"
        "exit 0\n"
    )
    (bin_ / "mcp-hub").chmod(0o755)
    return home, bin_


def call(home, bin_, snippet):
    head = SQUAD.read_text(encoding="utf-8").split(
        '\ncase "${1:-help}" in', 1)[0]
    assert "set_placement_intent()" in head, "extraction boundary moved"
    script = home / "_squad_head.sh"
    script.write_text(head + "\n" + snippet, encoding="utf-8")
    return subprocess.run(
        ["bash", str(script)],
        capture_output=True, text=True,
        env={"PATH": f"{bin_}:/usr/bin:/bin", "HOME": str(home)},
    )


def hub_calls(home):
    log = home / "hub.log"
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def test_stop_writes_stopped_for_a_placed_lane(tmp_path):
    """The whole point: the hub is told to WANT it down, by id."""
    home, bin_ = harness(tmp_path)
    r = call(home, bin_,
             'set_placement_intent lane-with-placement-dev-vm-1 stopped')
    assert r.returncode == 0, r.stderr
    assert "placements set pl-aaaa000000000001 stopped" in hub_calls(home)
    assert "want=stopped" in r.stdout


def test_start_writes_running_so_the_fix_is_not_mirrored(tmp_path):
    """A stop that wrote `stopped` while start left the row alone would not
    fix the bug, it would mirror it — the next hand-start comes up locally
    against a placement still reading `stopped` and the reconciler plans
    `op: stop` against the seat the operator just started."""
    home, bin_ = harness(tmp_path)
    r = call(home, bin_,
             'set_placement_intent lane-with-placement-dev-vm-1 running')
    assert r.returncode == 0, r.stderr
    assert "placements set pl-aaaa000000000001 running" in hub_calls(home)


def test_a_lane_with_no_placement_is_a_silent_no_op(tmp_path):
    """Most local lanes have no placement. Writing intent for them must not
    fail, and must not invent a row."""
    home, bin_ = harness(tmp_path)
    r = call(home, bin_, 'set_placement_intent ordinary-local-lane stopped')
    assert r.returncode == 0, r.stderr
    assert not any(c.startswith("placements set") for c in hub_calls(home))


def test_an_ambiguous_seat_refuses_rather_than_guessing(tmp_path):
    """Two rows, one seat name. Picking either would write lifecycle intent to
    an arbitrary machine's seat — so it writes NOTHING and says why."""
    home, bin_ = harness(tmp_path)
    r = call(home, bin_, 'set_placement_intent twinned-lane-dev-vm-1 stopped')
    assert r.returncode == 0, r.stderr
    assert not any(c.startswith("placements set") for c in hub_calls(home))
    assert "2 placements match" in r.stderr


def test_a_failed_set_is_loud_but_never_fatal(tmp_path):
    """The local kill is the part the operator is watching, so a hub failure
    must not abort it — but a stop that silently failed to stick is the bug
    being fixed, not a quieter version of it."""
    home, bin_ = harness(tmp_path, set_fails=True)
    r = call(home, bin_,
             'set_placement_intent lane-with-placement-dev-vm-1 stopped'
             '; echo "CALLER SURVIVED"')
    assert r.returncode == 0, r.stderr
    assert "CALLER SURVIVED" in r.stdout
    assert "NOT set to stopped" in r.stderr
    assert "will restart it" in r.stderr


def test_heals_door_does_not_write_intent(tmp_path):
    """`relaunch_agent` is heal's door. Heal resurrecting a lane is not the
    operator saying "I want this running", and wiring it here would let heal
    overturn a stop the operator had just made — the same override this
    change exists to remove, one layer down."""
    body = SQUAD.read_text(encoding="utf-8").split(
        "\nrelaunch_agent() {", 1)[1].split("\n}\n", 1)[0]
    assert "set_placement_intent" not in body
