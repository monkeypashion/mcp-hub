"""Three defects found by DRIVING the placement loop against production.

Operator, 2026-08-09, planning a web front end to manage agents/workspaces/
squads. Proving the placement→edge→`squad start` loop first surfaced these,
with the whole suite green throughout — every one of them is a thing no test
was asking about.

1. **`seats add --dry-run` WROTE the seat**, and printed the same success line
   a real add prints. `seats rm --dry-run` DELETED, by the identical omission:
   the branch simply never consulted the flag. A dry run that performs the
   real act is the worst of both, so an action that does not implement it now
   REFUSES — fail-closed, so a future write verb cannot inherit the bug.
2. **`placements set --seat X --machine Y ran`** either errored or silently
   bound `ran` to the placement id, dropping `--seat`/`--machine` without a
   word — which made `ran` unreachable at creation. Third instance of the
   trailing-positional-after-an-option class (see `squads fork`).
3. **A placement could not be UNPLACED.** DELETE meant *reclaim* — harvest,
   verify, destroy → `squad rm` — so the only way to stop the hub scheduling a
   seat was to demolish the agent behind it.

The tests below are written against the real handlers, and each mutation named
in a docstring was verified to fail its test.
"""
from __future__ import annotations

import argparse

import pytest

from mcp_hub import cli
from mcp_hub.operator_api import ApiUnavailable


class FakeApi:
    """Records every write. The assertion that matters is almost always that
    a list stayed EMPTY — 'it printed the right words' is what let the
    silent-write bug live, since the dry run printed the right words too."""

    def __init__(self, placements=None, fail=None):
        self._placements = list(placements or [])
        self._fail = fail
        self.calls: list[tuple] = []

    def _maybe_fail(self):
        if self._fail:
            raise ApiUnavailable(self._fail)

    def create_seat(self, repo, machine, folder, identity="", launch_args="",
                    klass="squad", spec=None):
        self._maybe_fail()
        self.calls.append(("create_seat", repo, machine, folder))
        return {"identity": identity or f"{repo.rsplit('/', 1)[-1]}-{machine}",
                "machine": machine}

    def delete_seat(self, identity):
        self._maybe_fail()
        self.calls.append(("delete_seat", identity))
        return {"identity": identity}

    def list_seats(self):
        self._maybe_fail()
        return []

    def create_capsule(self, squad):
        self._maybe_fail()
        self.calls.append(("create_capsule", squad))
        return {"id": "cap-1", "squad": squad, "manifest": {"seats": []}}

    def create_api_squad(self, name):
        self._maybe_fail()
        self.calls.append(("create_api_squad", name))
        return {"name": name}

    def place_capsule(self, cid, machine, as_label=""):
        self._maybe_fail()
        self.calls.append(("place_capsule", cid, machine, as_label))
        return {"placements": ["pl-1"], "seats": ["s-1"]}

    def list_capsules(self):
        self._maybe_fail()
        return []

    def list_placements(self):
        self._maybe_fail()
        return self._placements

    def create_placement(self, seat, machine, substrate="worktree",
                         desired="running"):
        self._maybe_fail()
        self.calls.append(("create_placement", seat, machine, substrate,
                           desired))
        return {"id": "pl-1", "seat": seat, "machine": machine,
                "desired": desired}

    def set_placement(self, pid, desired):
        self._maybe_fail()
        self.calls.append(("set_placement", pid, desired))
        return {"id": pid, "seat": "s", "machine": "m", "desired": desired}

    def reclaim_placement(self, pid):
        self._maybe_fail()
        self.calls.append(("reclaim_placement", pid))
        return {"id": pid}

    def unplace_placement(self, pid):
        self._maybe_fail()
        self.calls.append(("unplace_placement", pid))
        return {"id": pid, "purged": True}


def _args(**kw):
    base = dict(hub_url="http://h/mcp", json=False, dry_run=False, yes=False,
                machine=None, identity=None, repo="", folder="",
                want_identity="", launch_args="", klass="squad",
                target=None, desired=None, desired_flag=None, seat="",
                substrate="worktree", paths=[], all=False, squad="",
                scan_dir=None, image="", env=None, port=None, volume=None,
                network="", memory_volume="", command="", env_from_host=None,
                register=False, as_label="", mode="", prompt="", timeout=None,
                agent=None, pod_squad="", brief="", input=None,
                clone_suffix="")
    base.update(kw)
    return argparse.Namespace(**base)


def _placement(pid="pl-1", seat="s", machine="box", observed=None,
               desired="running"):
    return {"id": pid, "seat": seat, "machine": machine, "desired": desired,
            "substrate": "worktree", "status": "converged",
            "observed": {"state": observed}}


# ------------------------------------------------------- 1. the silent write


def test_seats_add_dry_run_WRITES_NOTHING(capsys):
    """🔴 THE DEFECT. Measured against production: the seat list was empty,
    `--dry-run` reported, the seat EXISTED, and the real add then returned
    409. Nothing in the dry run's output distinguished it from a real one.

    Mutation: delete the `if args.dry_run` block in the add branch → this
    fails on `api.calls`, NOT on the printed words.
    """
    api = FakeApi()
    rc = cli.seats_command(
        _args(action="add", repo="org/x", folder="/srv/x", machine="box",
              dry_run=True),
        api=api)
    assert rc == 0
    assert api.calls == [], f"dry run performed real writes: {api.calls}"
    assert "would declare" in capsys.readouterr().out


def test_seats_rm_dry_run_DELETES_NOTHING(capsys):
    """A dry run that deletes is the worst instance of the class — the same
    omission as `add`, on a destructive verb."""
    api = FakeApi()
    rc = cli.seats_command(
        _args(action="rm", identity="x-box", dry_run=True), api=api)
    assert rc == 0
    assert api.calls == [], f"dry run deleted: {api.calls}"
    assert "would archive" in capsys.readouterr().out


def test_the_dry_run_output_cannot_be_mistaken_for_a_real_one(capsys):
    """What made the production defect invisible: both paths printed
    'seat … declared'. The two outputs must not be confusable."""
    real = FakeApi()
    cli.seats_command(_args(action="add", repo="org/x", folder="/srv/x",
                            machine="box"), api=real)
    real_out = capsys.readouterr().out
    cli.seats_command(_args(action="add", repo="org/x", folder="/srv/x",
                            machine="box", dry_run=True), api=FakeApi())
    dry_out = capsys.readouterr().out
    assert real_out != dry_out
    assert "would" in dry_out and "would" not in real_out


@pytest.mark.parametrize("action,extra", [
    ("compose", {"squad": "dt"}),
    ("place", {"target": "cap-1", "machine": "box"}),
])
def test_capsules_refuses_dry_run_it_cannot_honour(capsys, action, extra):
    """compose/place cannot be previewed without asking the hub what they
    would freeze or place, and a local guess would be a second implementation.
    So they REFUSE — `capsules compose --dry-run` used to compose for real.

    Mutation: add "compose"/"place" to the honoured tuple → these fail,
    because the guard stops refusing and the real call goes through.
    """
    api = FakeApi()
    rc = cli.capsules_command(_args(action=action, dry_run=True, **extra),
                              api=api)
    assert rc == 1
    assert api.calls == [], f"refused but still wrote: {api.calls}"
    assert "not implemented" in capsys.readouterr().err


def test_the_guard_is_FAIL_CLOSED_for_an_action_nobody_thought_about():
    """The property that makes this a fix and not a patch: a NEW write verb
    added later refuses --dry-run until it implements it, rather than
    inheriting the silent write."""
    assert cli.refuse_unhonoured_dry_run(
        _args(action="some-future-verb", dry_run=True), ("list",)) is True


def test_the_guard_does_not_fire_when_the_action_HONOURS_the_flag():
    """The positive control: a guard that refused everything would pass every
    test above while breaking every working dry run."""
    assert cli.refuse_unhonoured_dry_run(
        _args(action="update", dry_run=True), ("update", "clone")) is False
    assert cli.refuse_unhonoured_dry_run(
        _args(action="add", dry_run=False), ("update",)) is False


def test_every_honoured_action_named_by_a_guard_is_a_REAL_action():
    """A typo in an honoured tuple silently turns a working dry run into a
    refusal — the failure would look like a deliberate design choice."""
    parser = cli.build_parser()
    choices = {}
    for act in parser._subparsers._group_actions:          # noqa: SLF001
        for name, sub in act.choices.items():
            for a in sub._actions:                          # noqa: SLF001
                if a.dest == "action" and a.choices:
                    choices[name] = set(a.choices)
    for verb, honoured in (
        ("seats", ("list", "logs", "add", "rm", "update", "clone")),
        ("capsules", ("list", "rm", "attach")),
        ("placements", ("list", "set", "reclaim", "unplace")),
        ("workspaces", ("list", "register", "remove")),
    ):
        assert set(honoured) <= choices[verb], (
            f"{verb}: {set(honoured) - choices[verb]} is not a real action")


# ------------------------------------------------- 2. the argparse mis-bind


def test_desired_is_reachable_as_a_FLAG_when_creating(capsys):
    """`ran` — the headless terminal state — could not be set at creation at
    all: the positional binds to `target`, and there is no way to skip a
    positional. So a headless placement had to be created as `running` (which
    a reconciler acts on) and corrected afterwards."""
    api = FakeApi()
    rc = cli.placements_command(
        _args(action="set", seat="s", machine="box", desired_flag="ran"),
        api=api)
    assert rc == 0
    assert ("create_placement", "s", "box", "worktree", "ran") in api.calls


def test_a_desired_state_in_the_ID_slot_is_REFUSED_not_obeyed(capsys):
    """🔴 `placements set running --seat X --machine Y` bound "running" to the
    placement id and silently ignored --seat/--machine — it read as "amend the
    placement whose id is 'running'".

    Mutation: drop the `args.target in (...)` check → this fails, and
    set_placement("running", "running") is called.
    """
    api = FakeApi()
    rc = cli.placements_command(
        _args(action="set", target="running", seat="s", machine="box"),
        api=api)
    assert rc == 1
    assert api.calls == []
    err = capsys.readouterr().err
    assert "not a placement id" in err and "--desired running" in err


def test_an_id_and_a_seat_together_are_REFUSED(capsys):
    """Two different modes — amend an existing row vs create one. The id used
    to win silently, so --seat/--machine were dropped without a word."""
    api = FakeApi()
    rc = cli.placements_command(
        _args(action="set", target="pl-9", seat="s", machine="box"), api=api)
    assert rc == 1
    assert api.calls == []
    assert "not both" in capsys.readouterr().err


def test_contradicting_spellings_are_refused_rather_than_ranked(capsys):
    """Precedence would silently write one of two states the operator named.
    Refusing is the only answer that cannot be wrong."""
    api = FakeApi()
    rc = cli.placements_command(
        _args(action="set", target="pl-9", desired="running",
              desired_flag="stopped"), api=api)
    assert rc == 1
    assert api.calls == []
    assert "contradicts" in capsys.readouterr().err


def test_the_positional_still_works_for_an_existing_placement():
    """The old spelling is the documented one and must not break."""
    api = FakeApi()
    assert cli.placements_command(
        _args(action="set", target="pl-9", desired="stopped"), api=api) == 0
    assert ("set_placement", "pl-9", "stopped") in api.calls


def test_the_parser_ACTUALLY_accepts_the_flag_form():
    """Through build_parser(), because the bug being fixed is an argparse
    binding bug — a hand-built Namespace cannot see it. This is the shape that
    let `squads fork --to x alice bob` ship broken with the suite green."""
    p = cli.build_parser()
    ns = p.parse_args(["placements", "set", "--seat", "s", "--machine", "box",
                       "--desired", "ran"])
    assert ns.desired_flag == "ran" and ns.seat == "s" and ns.target is None


# ------------------------------------------------------------- 3. unplacing


def test_unplace_drops_the_row_and_NEVER_reclaims():
    """🔴 reclaim → `squad rm` → the agent is unenrolled and its repo opted
    out of the hub. Confusing the two verbs destroys real estate config to
    tidy a bookkeeping row.

    Mutation: point `_placements_unplace` at reclaim_placement → this fails.
    """
    api = FakeApi(placements=[_placement(observed="stopped")])
    rc = cli.placements_command(_args(action="unplace", target="pl-1"),
                                api=api)
    assert rc == 0
    assert ("unplace_placement", "pl-1") in api.calls
    assert not any(c[0] == "reclaim_placement" for c in api.calls)


def test_unplacing_a_RUNNING_seat_is_refused_without_yes(capsys):
    """Unplace removes the policy, not the process. Abandoning a live agent
    is a legitimate thing to want and never a thing to do by accident."""
    api = FakeApi(placements=[_placement(observed="running")])
    rc = cli.placements_command(_args(action="unplace", target="pl-1"),
                                api=api)
    assert rc == 1
    assert not any(c[0] == "unplace_placement" for c in api.calls)
    err = capsys.readouterr().err
    assert "RUNNING" in err and "--yes" in err


def test_unplacing_a_running_seat_with_yes_says_what_was_ABANDONED(capsys):
    api = FakeApi(placements=[_placement(observed="running", seat="a-box")])
    rc = cli.placements_command(
        _args(action="unplace", target="pl-1", yes=True), api=api)
    assert rc == 0
    assert ("unplace_placement", "pl-1") in api.calls
    out = capsys.readouterr().out
    assert "unmanaged" in out and "squad stop a-box" in out


def test_unplace_dry_run_writes_nothing(capsys):
    api = FakeApi(placements=[_placement(observed="stopped")])
    rc = cli.placements_command(
        _args(action="unplace", target="pl-1", dry_run=True), api=api)
    assert rc == 0
    assert not any(c[0] == "unplace_placement" for c in api.calls)
    assert "would unplace" in capsys.readouterr().out


def test_unplace_without_a_target_is_refused(capsys):
    api = FakeApi()
    assert cli.placements_command(_args(action="unplace"), api=api) == 1
    assert "name the placement" in capsys.readouterr().err


def test_unplace_is_reachable_through_the_parser():
    ns = cli.build_parser().parse_args(["placements", "unplace", "pl-1"])
    assert ns.action == "unplace" and ns.target == "pl-1"


# ----------------------------------------- 4. repo-less folders are placeable
#
# 🔴 `mindconnect-iot2050` — a plain folder, already a roster agent, sitting in
# the operator's cockpit — could not be woken through the API at all, because
# declaring a seat demanded a git remote. Measured on dev-vm-1 2026-08-09:
# 13 of 15 FACULTY (on-demand) agents are plain folders, i.e. the API could
# start 2 of the 15 agents most worth starting from a UI.
#
# `repo` is now one thing only, in both branches: the source of a derived NAME.


def test_a_plain_folder_seat_is_accepted_when_NAMED(capsys):
    api = FakeApi()
    rc = cli.seats_command(
        _args(action="add", folder="/home/x/Projects/mindconnect",
              want_identity="mindconnect-iot2050-dev-vm-1",
              machine="dev-vm-1", klass="faculty"),
        api=api)
    assert rc == 0
    assert any(c[0] == "create_seat" and c[1] == "" for c in api.calls), (
        f"a repo-less seat was not created: {api.calls}")


def test_a_plain_folder_seat_with_NO_name_is_refused_not_guessed(capsys):
    """The basename is NOT a safe fallback: deriving identity from it while
    the cli derives from the git remote is what makes a clone's statusline
    read `hub ?`. Refuse and say what to pass."""
    api = FakeApi()
    rc = cli.seats_command(
        _args(action="add", folder="/home/x/Projects/mindconnect",
              machine="dev-vm-1"),
        api=api)
    assert rc == 1
    assert api.calls == []
    err = capsys.readouterr().err
    assert "--repo or --identity" in err
    assert "derived NAME" in err, "the refusal must say WHY repo is optional"


def test_folder_is_STILL_required_for_a_worktree_seat(capsys):
    """The control: relaxing repo must not relax everything. Without a folder
    there is nothing on disk to enrol."""
    api = FakeApi()
    rc = cli.seats_command(
        _args(action="add", repo="org/x", machine="box"), api=api)
    assert rc == 1
    assert api.calls == []
    assert "--folder" in capsys.readouterr().err
