"""W2.2 — `mcp-hub placements move`, the verb that moves a seat between boxes.

`machine` is immutable on a placement, so the only way to "move" one used to
be create-the-same-seat-on-B — which is not a move at all but TWO live
placements for one identity, both registering, the last one silently owning
the wake binding. That is the collision `capsules place` refuses by name, and
nothing stopped an operator reaching it one placement at a time.

The bar (docs/verification/wave-2.md, W2.2):
  B1 positive control, then refuse a second live placement for the seat
  B2 refuse a docker seat with no memory_volume unless --no-harvest
  B3 refuse a machine whose edge is not reporting
  B4 wait for `reclaim.destroy == done` before creating on B; --timeout
     exits RESUMABLY, naming the manual two-phase path
  B5 leftovers suppressed except the machine-A roster row
  B6 the harvest-does-not-gate-destroy limitation is NAMED in the output
"""

from __future__ import annotations

import argparse
import time
from unittest.mock import patch

import pytest

from mcp_hub import cli

# Sentinel, resolved to time.time() INSIDE _machine. 🔴 Not a module-level
# constant: `EDGE_STALE_SECONDS` is 120s and the full suite runs ~7 minutes, so
# a timestamp baked at import is stale by the time these tests execute — they
# passed alone and failed 14-of-23 in the suite. A fixture that decays is an
# instrument that reports the clock instead of the code.
FRESH = object()


def _machine(name, *, last_run=FRESH, result="ok"):
    now = time.time()
    return {"name": name, "os": "linux", "capabilities": {},
            "last_seen": now,
            "edge_last_run": now if last_run is FRESH else last_run,
            "edge_result": {"result": result} if result else None}


def _stale():
    return time.time() - 999_999


def _placement(pid="pl-a", seat="x-a", machine="box-a", substrate="worktree",
               desired="running", reclaim=None):
    row = {"id": pid, "seat": seat, "machine": machine, "substrate": substrate,
           "desired": desired, "observed": {"state": "running"},
           "status": "converged"}
    if reclaim is not None:
        row["reclaim"] = reclaim
    return row


class MoveApi:
    """Fake with a RECLAIM THAT PROGRESSES — the wait-gate is the whole verb,
    so a fake that reports `done` on the first poll would test nothing."""

    def __init__(self, placements=None, seats=None, machines=None,
                 destroy_after=1):
        self._placements = list(placements or [])
        self._seats = list(seats or [])
        self._machines = list(machines or [_machine("box-a"), _machine("box-b")])
        self._destroy_after = destroy_after
        self._polls = 0
        self.calls: list[tuple] = []
        self.create_at_poll: int | None = None

    def list_machines(self):
        return self._machines

    def list_seats(self):
        return self._seats

    def list_placements(self):
        # Only count polls AFTER a reclaim was requested — the pre-flight
        # reads must not advance the clock the gate is measuring.
        if any(c[0] == "reclaim_placement" for c in self.calls):
            self._polls += 1
            if self._polls >= self._destroy_after:
                for p in self._placements:
                    if p.get("reclaim"):
                        p["reclaim"] = {"harvest": p["reclaim"].get("harvest",
                                                                   "done"),
                                        "verify": "done", "destroy": "done"}
        return self._placements

    def reclaim_placement(self, pid):
        self.calls.append(("reclaim_placement", pid))
        for p in self._placements:
            if p["id"] == pid:
                p["desired"] = "reclaimed"
                p.setdefault("reclaim", {"harvest": "pending",
                                         "verify": "pending",
                                         "destroy": "pending"})
        return {"id": pid}

    def create_placement(self, seat, machine, substrate="worktree",
                         desired="running"):
        self.calls.append(("create_placement", seat, machine, substrate,
                           desired))
        self.create_at_poll = self._polls
        return {"id": "pl-b", "seat": seat, "machine": machine,
                "desired": desired}

    def get_registry(self):
        return {"definitions": []}


def _args(**kw):
    base = dict(hub_url="http://h/mcp", json=False, dry_run=False, yes=True,
                action="move", target="pl-a", to="box-b", no_harvest=False,
                timeout=300, machine=None, seat="", substrate="worktree",
                desired=None, desired_flag=None)
    base.update(kw)
    return argparse.Namespace(**base)


def _move(api, **kw):
    with patch.object(time, "sleep"):
        return cli.placements_command(_args(**kw), api=api)


# ---------------------------------------------------------------------------
# B1 — positive control, then the collision
# ---------------------------------------------------------------------------


class TestCollision:
    def test_positive_control_an_ordinary_move_SUCCEEDS(self, capsys):
        """If this fails, every refusal below is an instrument failure rather
        than a contract verdict."""
        api = MoveApi(placements=[_placement()])
        assert _move(api) == 0
        assert ("create_placement", "x-a", "box-b", "worktree", "running") \
            in api.calls
        assert "moved x-a: box-a -> box-b" in capsys.readouterr().out

    def test_a_second_live_placement_is_REFUSED(self, capsys):
        """🔴 The collision the verb exists to make unreachable.

        Mutation: drop the `others` check -> this fails.
        """
        api = MoveApi(placements=[
            _placement(),
            _placement(pid="pl-c", machine="box-c"),
        ])
        assert _move(api) == 1
        err = capsys.readouterr().err
        assert "another live placement" in err
        assert "pl-c on box-c" in err  # NAMED, not merely counted
        assert api.calls == []  # nothing written

    def test_an_ALREADY_RECLAIMED_placement_does_not_block_a_move(self):
        """A reclaimed row is not a live placement; treating it as one would
        make a seat unmovable forever after its first move."""
        api = MoveApi(placements=[
            _placement(),
            _placement(pid="pl-old", machine="box-c", desired="reclaimed"),
        ])
        assert _move(api) == 0

    def test_moving_to_the_machine_it_is_already_on_is_refused(self, capsys):
        api = MoveApi(placements=[_placement()])
        assert _move(api, to="box-a") == 1
        assert "already placed on box-a" in capsys.readouterr().err
        assert api.calls == []


# ---------------------------------------------------------------------------
# B2 — a move that silently loses memory
# ---------------------------------------------------------------------------


class TestHarvest:
    def test_a_docker_seat_with_no_memory_volume_is_REFUSED(self, capsys):
        """Reclaim harvests from the volume; with none there is nothing to
        harvest, so the move destroys everything the agent learned.

        Mutation: drop the memory_volume check -> this fails.
        """
        api = MoveApi(
            placements=[_placement(substrate="docker")],
            seats=[{"identity": "x-a", "spec": {"image": "img"}}],
        )
        assert _move(api) == 1
        err = capsys.readouterr().err
        assert "no memory_volume" in err
        assert "--no-harvest" in err  # the exit is NAMED
        assert api.calls == []

    def test_no_harvest_accepts_the_loss_deliberately(self):
        api = MoveApi(
            placements=[_placement(substrate="docker")],
            seats=[{"identity": "x-a", "spec": {"image": "img"}}],
        )
        assert _move(api, no_harvest=True) == 0

    def test_a_docker_seat_WITH_a_volume_moves(self, capsys):
        """Positive control for the refusal above."""
        api = MoveApi(
            placements=[_placement(substrate="docker")],
            seats=[{"identity": "x-a", "spec": {"memory_volume": "mem-x"}}],
        )
        assert _move(api) == 0
        # memory is staged hub-side by PROJECT, so the re-attach is named
        assert "memory-import" in capsys.readouterr().out

    def test_a_WORKTREE_seat_with_no_volume_is_not_blocked(self):
        """memory_volume is a docker concept — applying the gate to worktree
        seats would refuse every ordinary move."""
        api = MoveApi(placements=[_placement(substrate="worktree")], seats=[])
        assert _move(api) == 0


# ---------------------------------------------------------------------------
# B3 — a move is only as real as the edge that realizes it
# ---------------------------------------------------------------------------


class TestEdgeHealth:
    @pytest.mark.parametrize("state", ["stale", "never", "absent"])
    def test_a_DESTINATION_whose_edge_is_not_reporting_is_refused(
        self, capsys, state
    ):
        """Mutation: drop the destination edge check -> this fails.

        Machines are built HERE, not in the parametrize list — that list is
        evaluated at collection time, which is minutes before these tests run
        in a full suite, and every `fresh` timestamp in it would be stale.
        """
        machines = {
            "stale": lambda: [_machine("box-a"),
                              _machine("box-b", last_run=_stale())],
            "never": lambda: [_machine("box-a"),
                              _machine("box-b", last_run=None)],
            "absent": lambda: [_machine("box-a")],
        }[state]()
        api = MoveApi(placements=[_placement()], machines=machines)
        assert _move(api) == 1
        err = capsys.readouterr().err
        assert "destination edge is not reporting" in err
        assert "mcp-hub-edge.timer" in err  # where the fault nearly always is
        assert api.calls == []

    def test_a_SOURCE_whose_edge_is_not_reporting_is_refused(self, capsys):
        """🔴 The bar names the DESTINATION; the source is what actually hangs
        the wait — machine A offline means the reclaim is never observed
        complete and the move can only time out, after destroying nothing and
        creating nothing. Checking only the destination would satisfy the
        letter of B3 and still strand the operator mid-move.

        Mutation: check only the destination -> this fails.
        """
        api = MoveApi(placements=[_placement()],
                      machines=[_machine("box-a", last_run=_stale()),
                                _machine("box-b")])
        assert _move(api) == 1
        assert "source edge is not reporting" in capsys.readouterr().err
        assert api.calls == []

    def test_a_FAILING_edge_warns_but_proceeds(self, capsys):
        """`failed` is a measurement; `stale` is blindness. Refusing a
        reporting-but-failing edge would make the fleet unmovable exactly when
        an operator most needs to move something off a sick box."""
        api = MoveApi(placements=[_placement()],
                      machines=[_machine("box-a"),
                                _machine("box-b", result="failed")])
        assert _move(api) == 0
        assert "edge is FAILING on box-b" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# B4 — the wait-gate
# ---------------------------------------------------------------------------


class TestWaitGate:
    def test_B_is_created_ONLY_after_destroy_is_observed_done(self, capsys):
        """🔴 The heart of the verb. Creating on B before A's substrate is
        gone is the double-placement collision by another route — so the gate
        is what makes the collision impossible BY CONSTRUCTION rather than by
        a refusal someone can be talked past.

        Mutation: create on B immediately after requesting the reclaim ->
        this fails (create lands at poll 1, before destroy is done).
        """
        api = MoveApi(placements=[_placement()], destroy_after=3)
        assert _move(api) == 0
        assert [c[0] for c in api.calls] == ["reclaim_placement",
                                             "create_placement"]
        # it WAITED — the create happened on the poll that saw destroy done,
        # not on the first one
        assert api.create_at_poll == 3
        assert "reclaim in progress" in capsys.readouterr().out

    def test_a_timeout_exits_RESUMABLY_and_creates_NOTHING(self, capsys):
        """A move that timed out is half-done by design: A is reclaimed, B is
        untouched. Creating on B anyway would be the collision; leaving the
        operator without the two-phase path would be abandonment.

        Mutation: create on B after the timeout -> this fails.
        """
        api = MoveApi(placements=[_placement()], destroy_after=10**6)
        rc = _move(api, timeout=0)
        assert rc == 2  # distinct from 1 (refused) and 0 (moved)
        assert [c[0] for c in api.calls] == ["reclaim_placement"]
        err = capsys.readouterr().err
        assert "timed out" in err
        assert "resumable, not" in err
        # the manual path, spelled out
        assert "placements set --seat x-a --machine box-b" in err

    def test_a_vanished_placement_row_counts_as_complete(self, capsys):
        """Nothing on A still claims the seat, which is the condition the
        create needs — waiting for a row that no longer exists would time out
        on a reclaim that actually finished."""
        api = MoveApi(placements=[_placement()], destroy_after=10**6)

        real = api.list_placements

        def _vanish():
            rows = real()
            if any(c[0] == "reclaim_placement" for c in api.calls):
                return []
            return rows

        api.list_placements = _vanish
        assert _move(api) == 0
        assert "treating the reclaim as complete" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# B5 / B6 — what survives, and what is not gated
# ---------------------------------------------------------------------------


class TestAftermath:
    def test_only_the_source_roster_row_is_reported_as_a_leftover(self, capsys):
        """🔴 B5. A seat declaration OUTLIVING its placement is what makes
        moving machines possible at all — reporting it as a leftover would
        invite the operator to delete the very thing the move depends on.

        Mutation: call _report_leftovers here -> this fails (it names the
        seat declaration and workspace too).
        """
        api = MoveApi(placements=[_placement()],
                      seats=[{"identity": "x-a", "spec": {}}])
        assert _move(api) == 0
        out = capsys.readouterr().out
        assert "roster row   squad rm x-a" in out
        assert "seat declaration" not in out
        assert "seats rm" not in out

    def test_a_failed_harvest_is_NAMED_because_destroy_ran_anyway(self, capsys):
        """🔴 B6, the named limitation. The edge runs harvest -> verify ->
        destroy unconditionally, so a harvest that failed did not stop the
        destroy. Gating that changes reclaim semantics for every caller and is
        deferred — so the move states the risk rather than implying safety.

        Mutation: drop the harvest-state report -> this fails.
        """
        api = MoveApi(
            placements=[_placement(substrate="docker",
                                   reclaim={"harvest": "failed",
                                            "verify": "pending",
                                            "destroy": "pending"})],
            seats=[{"identity": "x-a", "spec": {"memory_volume": "mem-x"}}],
        )
        assert _move(api) == 0
        out = capsys.readouterr().out
        assert "harvest phase reported 'failed'" in out
        assert "unconditionally" in out


# ---------------------------------------------------------------------------
# The guards around a destructive verb
# ---------------------------------------------------------------------------


class TestGuards:
    def test_without_yes_it_refuses_and_writes_nothing(self, capsys):
        api = MoveApi(placements=[_placement()])
        assert _move(api, yes=False) == 1
        assert "RECLAIMS it on box-a first" in capsys.readouterr().err
        assert api.calls == []

    def test_dry_run_writes_nothing_and_names_all_three_steps(self, capsys):
        api = MoveApi(placements=[_placement()])
        assert _move(api, dry_run=True, yes=False) == 0
        out = capsys.readouterr().out
        assert "would move x-a: box-a -> box-b" in out
        assert "reclaim" in out and "wait" in out and "create" in out
        assert api.calls == []

    def test_a_missing_destination_is_refused(self, capsys):
        api = MoveApi(placements=[_placement()])
        assert _move(api, to="") == 1
        assert "--to <machine>" in capsys.readouterr().err

    def test_an_unknown_placement_is_refused(self, capsys):
        api = MoveApi(placements=[_placement()])
        assert _move(api, target="nope") == 1
        assert "no placement 'nope'" in capsys.readouterr().err

    def test_move_is_reachable_through_the_parser(self):
        """A verb absent from the parser's choices is unreachable however
        well it works — the second-registry trap this file already guards."""
        args = cli.build_parser().parse_args(
            ["placements", "move", "pl-a", "--to", "box-b", "--yes"]
        )
        assert args.action == "move" and args.to == "box-b"
