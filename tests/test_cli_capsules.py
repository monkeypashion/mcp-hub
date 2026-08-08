"""`mcp-hub capsules` — starting a whole SQUAD on docker, from any node.

The capsule machinery has existed on the hub since the runtime shipped
(compose, download, place-one-placement-per-seat) but had no verb: the only
way to start a squad on docker was curl against /api/v1 with the operator
token. A capability reachable only by curl is half-delivered — the operator
drives the fleet from the board and the CLI, not from a REST client.

These tests drive the CLI against a FakeApi, so they prove what the client
SENDS. They cannot prove the hub accepts it — that is the lesson from the
seats 500 (a fake at the client boundary tests the client), so the live
path is exercised separately against the real hub.
"""

from __future__ import annotations

import argparse

from mcp_hub import cli


class FakeApi:
    def __init__(self, capsules=None, fail=None):
        self._capsules = list(capsules or [])
        self._fail = fail
        self.calls: list[tuple] = []

    def list_capsules(self):
        self.calls.append(("list_capsules",))
        return self._capsules

    def create_capsule(self, squad):
        self.calls.append(("create_capsule", squad))
        return {
            "id": "cap-abc123",
            "squad": squad,
            "manifest": {"squad": squad, "seats": [
                {"identity": "a-1", "spec": {"image": "mcp-hub-seat:latest"}},
                {"identity": "b-1", "spec": {"image": "mcp-hub-seat:latest"}},
            ]},
        }

    def place_capsule(self, cid, machine, as_label=""):
        # Signature tracks OperatorApi.place_capsule deliberately: a double
        # that lags the real method turns a caller change into a test-only
        # TypeError, which reads as a bug in the caller.
        self.calls.append(("place_capsule", cid, machine, as_label))
        return {"placements": ["pl-1", "pl-2"],
                "seats": ["seat-a", "seat-b"]}


def _args(**kw):
    base = {"action": "list", "target": None, "squad": None, "machine": None,
            "hub_url": "http://hub", "json": False, "register": False}
    base.update(kw)
    return argparse.Namespace(**base)


def test_the_verb_is_reachable_through_the_entry_point():
    """server._CLI_SUBCOMMANDS is a SECOND registry — a verb absent from it
    parses and is then never dispatched, failing as 'unrecognized
    arguments', which reads like a typo rather than a missing wire."""
    from mcp_hub.server import _CLI_SUBCOMMANDS

    assert "capsules" in _CLI_SUBCOMMANDS
    assert cli.build_parser().parse_args(["capsules", "list"]).subcommand == \
        "capsules"


def test_listing_nothing_says_what_a_capsule_is_for(capsys):
    assert cli.capsules_command(_args(action="list"), api=FakeApi()) == 0
    out = capsys.readouterr().out
    assert "no capsules" in out.lower()


def test_composing_a_capsule_names_the_seats_it_froze(capsys):
    """A capsule is a SNAPSHOT — saying how many seats it captured is the
    only way an operator can tell it caught the squad they meant."""
    api = FakeApi()
    rc = cli.capsules_command(_args(action="compose", squad="dreamteam"), api=api)
    assert rc == 0
    assert api.calls[0] == ("create_capsule", "dreamteam")
    out = capsys.readouterr().out
    assert "cap-abc123" in out
    assert "2 seat" in out


def test_composing_says_plainly_that_nothing_is_running_yet(capsys):
    """Same trap as `seats add`: 'capsule composed' reads as 'squad
    started'. It is inert until placed, and then still inert until an edge
    pass realizes it."""
    cli.capsules_command(_args(action="compose", squad="dreamteam"),
                         api=FakeApi())
    out = capsys.readouterr().out
    assert "not running" in out.lower() or "nothing is running" in out.lower()
    assert "capsules place" in out


def test_placing_a_capsule_reports_one_placement_per_seat(capsys):
    api = FakeApi()
    rc = cli.capsules_command(
        _args(action="place", target="cap-abc123", machine="dev-vm-1"), api=api)
    assert rc == 0
    # Empty label = place the squad ITSELF, not a second copy of it.
    assert api.calls[0] == ("place_capsule", "cap-abc123", "dev-vm-1", "")
    out = capsys.readouterr().out
    assert "2 placement" in out
    # The honest caveat the whole runtime is built on.
    assert "edge" in out.lower()


def test_compose_without_a_squad_refuses_and_names_the_flag(capsys):
    rc = cli.capsules_command(_args(action="compose"), api=FakeApi())
    assert rc == 1
    assert "--squad" in capsys.readouterr().err


def test_place_without_a_machine_refuses_and_names_the_flag(capsys):
    rc = cli.capsules_command(_args(action="place", target="cap-1"),
                              api=FakeApi())
    assert rc == 1
    assert "--machine" in capsys.readouterr().err


def test_place_without_a_capsule_id_refuses(capsys):
    rc = cli.capsules_command(_args(action="place", machine="box"),
                              api=FakeApi())
    assert rc == 1
    assert "capsule" in capsys.readouterr().err.lower()


def test_register_first_is_explicit_never_silent():
    """A squad can exist for MESSAGING and be unknown to the MANAGEMENT
    registry — composing then 404s with the members sitting right there.
    Registering is a real act, so it takes a real flag rather than
    happening quietly inside compose."""
    api = FakeApi()
    api.create_api_squad = lambda name: api.calls.append(("create_api_squad", name))
    cli.capsules_command(_args(action="compose", squad="runtime", register=True),
                         api=api)
    assert api.calls[0] == ("create_api_squad", "runtime")
    assert api.calls[1] == ("create_capsule", "runtime")


def test_compose_without_register_does_not_touch_the_registry():
    api = FakeApi()
    api.create_api_squad = lambda name: api.calls.append(("create_api_squad", name))
    cli.capsules_command(_args(action="compose", squad="runtime"), api=api)
    assert not any(c[0] == "create_api_squad" for c in api.calls)
