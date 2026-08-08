"""Driving the fleet from ANY node: seats, placements, and dropping a definition.

The management surface has had a write side on the server since it shipped and
no client for it — 2 machines enrolled, 7 workspaces registered, **0 seats and
0 placements**, because nothing could create one. These are the verbs that make
the hub's desired state writable from wherever you happen to be sitting.

The property under test throughout is that they tell the truth about what has
NOT happened: writing a placement schedules nothing, it records an intent that
the named machine's `edge apply` may or may not have acted on yet. A verb that
printed "started" here would be describing a thing it did not do.
"""

from __future__ import annotations

import argparse

import pytest

from mcp_hub import cli
from mcp_hub.operator_api import ApiUnavailable


class FakeApi:
    def __init__(self, seats=None, placements=None, workspaces=None, fail=None):
        self._seats = list(seats or [])
        self._placements = list(placements or [])
        self._workspaces = list(workspaces or [])
        self._fail = fail
        self.calls: list[tuple] = []

    def _maybe_fail(self):
        if self._fail:
            raise ApiUnavailable(self._fail)

    def list_seats(self):
        self._maybe_fail()
        return self._seats

    def create_seat(self, repo, machine, folder, identity="", launch_args="",
                    klass="squad", spec=None):
        self._maybe_fail()
        self.calls.append(("create_seat", repo, machine, folder, identity,
                           launch_args, klass, spec or {}))
        return {"identity": identity or f"{repo.rsplit('/', 1)[-1]}-{machine}",
                "machine": machine}

    def delete_seat(self, identity):
        self._maybe_fail()
        self.calls.append(("delete_seat", identity))
        return {"identity": identity, "archived": True}

    def list_placements(self):
        self._maybe_fail()
        return self._placements

    def create_placement(self, seat, machine, substrate="worktree",
                         desired="running"):
        self._maybe_fail()
        self.calls.append(("create_placement", seat, machine, substrate, desired))
        return {"id": "pl-1", "seat": seat, "machine": machine,
                "desired": desired}

    def set_placement(self, pid, desired):
        self._maybe_fail()
        self.calls.append(("set_placement", pid, desired))
        return {"id": pid, "seat": "s", "machine": "m", "desired": desired}

    def reclaim_placement(self, pid):
        self._maybe_fail()
        self.calls.append(("reclaim_placement", pid))
        return {"id": pid, "reclaim": "requested"}

    def list_workspaces(self):
        self._maybe_fail()
        return self._workspaces

    def delete_workspace(self, wid):
        self._maybe_fail()
        self.calls.append(("delete_workspace", wid))
        return {"id": wid, "removed": True}


def _args(**kw):
    base = dict(hub_url="http://h/mcp", json=False, dry_run=False, yes=False,
                machine=None, identity=None, repo="", folder="",
                want_identity="", launch_args="", klass="squad",
                target=None, desired=None, seat="", substrate="worktree",
                paths=[], all=False, squad="", scan_dir=None,
                image="", env=None, port=None, volume=None, network="",
                memory_volume="", command="", env_from_host=None)
    base.update(kw)
    return argparse.Namespace(**base)


# ---- reachability ----------------------------------------------------------

def test_the_new_verbs_are_reachable_through_the_entry_point():
    """`server._CLI_SUBCOMMANDS` is a SECOND registry — a verb absent from it
    is parsed and then never dispatched. That has bitten `workspaces` once."""
    from mcp_hub.server import _CLI_SUBCOMMANDS

    for verb in ("seats", "placements"):
        assert verb in _CLI_SUBCOMMANDS, verb
        assert cli.build_parser().parse_args([verb, "list"]).subcommand == verb


# ---- seats -----------------------------------------------------------------

def test_listing_no_seats_says_why_that_matters(capsys):
    assert cli.seats_command(_args(action="list"), api=FakeApi()) == 0
    assert "nothing can be placed" in capsys.readouterr().out


def test_adding_a_seat_needs_the_fields_that_make_it_materializable(capsys):
    rc = cli.seats_command(_args(action="add", repo="org/x"), api=FakeApi())
    assert rc == 1
    assert "--folder" in capsys.readouterr().err


def test_a_declared_seat_says_it_is_not_yet_running_anywhere(capsys):
    api = FakeApi()
    rc = cli.seats_command(
        _args(action="add", repo="org/x", folder="/srv/x", machine="box"),
        api=api)
    assert rc == 0
    out = capsys.readouterr().out
    # The trap this closes: "seat declared" reads as "seat started".
    assert "will not run until it is PLACED" in out
    assert "placements set --seat x-box" in out
    assert api.calls[0][:4] == ("create_seat", "org/x", "box", "/srv/x")


def test_a_docker_unit_needs_no_folder_because_it_has_an_image(capsys):
    """An nginx container has no worktree and never will. Demanding one would
    make every non-agent unit lie about itself."""
    api = FakeApi()
    rc = cli.seats_command(
        _args(action="add", repo="org/site", machine="box", image="nginx:alpine",
              port=["8080:80"], env=["TZ=UTC"], volume=["/srv:/usr/share/nginx"]),
        api=api)
    assert rc == 0
    spec = api.calls[0][7]
    assert spec == {"image": "nginx:alpine", "env": {"TZ": "UTC"},
                    "ports": ["8080:80"], "volumes": ["/srv:/usr/share/nginx"]}
    assert "docker (nginx:alpine)" in capsys.readouterr().out


def test_a_worktree_unit_still_demands_its_folder(capsys):
    rc = cli.seats_command(_args(action="add", repo="org/x", machine="box"),
                           api=FakeApi())
    assert rc == 1
    assert "--folder" in capsys.readouterr().err


def test_memory_volume_is_what_separates_a_seat_from_a_service(capsys):
    """Its presence is the whole agent-vs-service distinction: reclaim
    harvests before destroying only when there is something to preserve."""
    api = FakeApi()
    cli.seats_command(
        _args(action="add", repo="org/pm", machine="box", image="mcp-hub-seat",
              memory_volume="pm-memory"), api=api)
    assert api.calls[0][7]["memory_volume"] == "pm-memory"


def test_headless_is_a_flag_not_tribal_env_knowledge(capsys):
    """--mode headless assembles the same spec env a hand-rolled
    `--env SEAT_MODE=headless` would — the affordance IS the feature; an
    operator should not need to know the env var names."""
    api = FakeApi()
    rc = cli.seats_command(
        _args(action="add", repo="org/e", machine="box", image="mcp-hub-seat",
              mode="headless", prompt="do the thing", timeout=600,
              memory_volume="e-mem"), api=api)
    assert rc == 0
    env = api.calls[0][7]["env"]
    assert env["SEAT_MODE"] == "headless"
    assert env["SEAT_PROMPT"] == "do the thing"
    assert env["SEAT_TIMEOUT"] == "600"


@pytest.mark.parametrize("kw,expect", [
    # No instruction: a one-shot with nothing to do reads as a crash.
    (dict(image="i", memory_volume="m"), "--prompt or --brief"),
    # No volume: the result would provably die with the container.
    (dict(image="i", prompt="go"), "--memory-volume"),
    # No image: SEAT_MODE means nothing to a worktree seat.
    (dict(repo="org/x", folder="/srv/x", prompt="go", memory_volume="m"),
     "--image"),
    # A pod: SEAT_PROMPT is single-valued, a pod has several agents.
    (dict(image="i", prompt="go", memory_volume="m",
          agent=["a=org/a", "b=org/b"]), "1:1"),
])
def test_headless_declaration_refuses_at_the_earliest_gate(capsys, kw, expect):
    """The same rules seat-entry's door and the edge enforce, surfaced at
    DECLARATION — where the fix is one flag away instead of a dead container
    two minutes later."""
    rc = cli.seats_command(
        _args(action="add", machine="box", mode="headless", **kw),
        api=FakeApi())
    assert rc == 1
    assert expect in capsys.readouterr().err


def test_archiving_a_seat_with_placements_explains_the_refusal(capsys):
    api = FakeApi(fail="hub API error 409 on /api/v1/seats/x: active placements")
    rc = cli.seats_command(_args(action="rm", identity="x-box"), api=api)
    assert rc == 1
    assert "reclaim them first" in capsys.readouterr().err


# ---- placements ------------------------------------------------------------

def test_placing_a_seat_says_nothing_has_happened_yet(capsys):
    """The whole honesty contract of the verb."""
    api = FakeApi()
    rc = cli.placements_command(
        _args(action="set", seat="x-box", machine="dev-vm-1"), api=api)
    assert rc == 0
    out = capsys.readouterr().out
    assert "Nothing has happened yet" in out
    assert "edge apply" in out
    assert api.calls == [("create_placement", "x-box", "dev-vm-1",
                          "worktree", "running")]


def test_setting_an_existing_placement_patches_it_rather_than_creating(capsys):
    api = FakeApi()
    assert cli.placements_command(
        _args(action="set", target="pl-1", desired="stopped"), api=api) == 0
    assert api.calls == [("set_placement", "pl-1", "stopped")]


def test_reclaim_is_not_reachable_through_set(capsys):
    """It harvests then DESTROYS. A destroy you can reach by typing a word
    into a state field is a destroy that happens by accident."""
    api = FakeApi()
    rc = cli.placements_command(
        _args(action="set", target="pl-1", desired="reclaimed"), api=api)
    assert rc == 1
    assert "reclaim is its own verb" in capsys.readouterr().err
    assert api.calls == []


def test_reclaim_refuses_without_yes(capsys):
    api = FakeApi()
    rc = cli.placements_command(_args(action="reclaim", target="pl-1"), api=api)
    assert rc == 1
    assert "DESTROYS" in capsys.readouterr().err
    assert api.calls == []


def test_reclaim_dry_run_writes_nothing_and_still_says_what_it_would_do(capsys):
    api = FakeApi()
    assert cli.placements_command(
        _args(action="reclaim", target="pl-1", dry_run=True), api=api) == 0
    assert "would reclaim" in capsys.readouterr().out
    assert api.calls == []


def test_pending_placements_name_the_likeliest_cause(capsys):
    """`edge apply` is a one-shot and nothing schedules it by default, so
    "pending forever" is the expected first symptom of the whole design."""
    api = FakeApi(placements=[
        {"id": "pl-1", "seat": "a-box", "machine": "box", "desired": "running",
         "observed": {"state": None}, "status": "pending-edge"},
        {"id": "pl-2", "seat": "b-box", "machine": "box", "desired": "running",
         "observed": {"state": "running"}, "status": "converged"},
    ])
    assert cli.placements_command(_args(action="list"), api=api) == 0
    out = capsys.readouterr().out
    assert "1 pending-edge" in out
    assert "actually runs on those machines" in out


def test_a_converged_fleet_is_not_nagged_about_edges(capsys):
    api = FakeApi(placements=[
        {"id": "pl-2", "seat": "b-box", "machine": "box", "desired": "running",
         "observed": {"state": "running"}, "status": "converged"},
    ])
    assert cli.placements_command(_args(action="list"), api=api) == 0
    assert "pending-edge" not in capsys.readouterr().out


# ---- workspaces remove -----------------------------------------------------

WS = [
    {"id": 1, "name": "xport", "machine": "here", "listings": [{"path": "/a"}]},
    {"id": 2, "name": "dup", "machine": "here", "listings": []},
    {"id": 3, "name": "dup", "machine": "dev-vm-1", "listings": []},
]


def _ws_args(**kw):
    return _args(action="remove", **kw)


def test_removing_a_definition_refuses_without_yes(capsys):
    api = FakeApi(workspaces=WS)
    rc = cli.workspaces_command(_ws_args(paths=["xport"]), api=api)
    assert rc == 1
    out = capsys.readouterr()
    assert "cannot be undone" in out.err
    # Found live: the preview said "removing: xport" and the refusal followed
    # on the next line, describing an act that was about to not occur.
    assert "would remove: xport" in out.out
    assert "removing: xport" not in out.out
    assert api.calls == []


def test_removing_a_definition_touches_no_disk_and_says_so(capsys):
    api = FakeApi(workspaces=WS)
    rc = cli.workspaces_command(_ws_args(paths=["xport"], yes=True), api=api)
    assert rc == 0
    out = capsys.readouterr().out
    assert "removed definition: xport" in out
    assert "FILES are untouched" in out
    assert "squad teardown workspace" in out          # the other half, named
    assert api.calls == [("delete_workspace", 1)]


def test_a_name_defined_on_two_machines_is_refused_not_guessed(capsys):
    """Deleting the wrong machine's definition is silent, and the fix is one
    flag away — so refuse rather than resolve."""
    api = FakeApi(workspaces=WS)
    rc = cli.workspaces_command(_ws_args(paths=["dup"], yes=True), api=api)
    assert rc == 1
    err = capsys.readouterr().err
    assert "AMBIGUOUS" in err and "--machine" in err
    assert api.calls == []


def test_naming_the_machine_resolves_the_ambiguity(capsys):
    api = FakeApi(workspaces=WS)
    rc = cli.workspaces_command(
        _ws_args(paths=["dup"], yes=True, machine="dev-vm-1"), api=api)
    assert rc == 0
    assert api.calls == [("delete_workspace", 3)]


def test_a_path_is_accepted_where_a_name_is_expected(capsys):
    """`register` takes paths; muscle memory will paste one here."""
    api = FakeApi(workspaces=WS)
    rc = cli.workspaces_command(
        _ws_args(paths=["/home/me/Projects/xport.code-workspace"], yes=True),
        api=api)
    assert rc == 0
    assert api.calls == [("delete_workspace", 1)]


def test_an_unknown_name_fails_loudly_rather_than_reporting_success(capsys):
    api = FakeApi(workspaces=WS)
    rc = cli.workspaces_command(_ws_args(paths=["nope"], yes=True), api=api)
    assert rc == 1
    assert "no definition named nope" in capsys.readouterr().err
    assert api.calls == []


def test_remove_dry_run_writes_nothing(capsys):
    api = FakeApi(workspaces=WS)
    rc = cli.workspaces_command(
        _ws_args(paths=["xport"], yes=True, dry_run=True), api=api)
    assert rc == 0
    assert "would remove" in capsys.readouterr().out
    assert api.calls == []


@pytest.mark.parametrize("action", ["list", "register", "remove"])
def test_every_workspaces_action_parses(action):
    args = cli.build_parser().parse_args(["workspaces", action])
    assert args.action == action


def test_an_image_unit_needs_no_repo_either(capsys):
    """An nginx container has no git remote any more than it has a worktree.
    Demanding one forces the operator to invent a field, and an invented
    field is a lie the roster then carries forever. Measured: declaring the
    first containerized claude seat required passing a repo the container
    never clones."""
    api = FakeApi()
    rc = cli.seats_command(
        _args(action="add", identity="web-1", machine="box",
              image="nginx:alpine"),
        api=api)
    assert rc == 0
    assert api.calls[0][7]["image"] == "nginx:alpine"


def test_a_worktree_unit_still_demands_repo_AND_folder(capsys):
    """The relaxation is for image units ONLY — a tmux seat with neither
    cannot be materialized anywhere."""
    api = FakeApi()
    rc = cli.seats_command(_args(action="add", machine="box"), api=api)
    assert rc == 1
    err = capsys.readouterr().err
    assert "--repo" in err and "--folder" in err
