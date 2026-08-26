"""`mcp-hub squads` — the verb that did not exist.

🔴 THE GAP (operator, 2026-08-08). Five of the seven team-assembly scenarios
needed to read or change squad membership and NONE of them could. The REST
routes had been complete since the runtime shipped; the only CLI door was a
side-effect flag on `capsules compose --register`. Membership was otherwise
something an agent did TO ITSELF via the MCP `set_squads`, so the operator's
own team structure was the one thing the operator's CLI could not touch.

⭐ These tests drive `squads_command` with a double, so they assert what the
verb ASKS THE HUB TO DO. A test that only checked printed output would pass
against a command that printed a convincing summary and called nothing —
which, for a verb whose entire job is remote bookkeeping, is the failure mode
worth guarding.
"""
from __future__ import annotations

import argparse

import pytest

from mcp_hub import cli


class FakeApi:
    """Tracks calls and keeps enough state for fork/merge to be meaningful."""

    def __init__(self, members=None):
        self.calls: list[tuple] = []
        self.members: dict[str, list[str]] = members or {}

    def list_api_squads(self):
        return [{"name": n, "member_count": len(m), "description": ""}
                for n, m in sorted(self.members.items())]

    def create_api_squad(self, name, description=""):
        self.calls.append(("create", name, description))
        self.members.setdefault(name, [])
        return {"name": name}

    def delete_api_squad(self, name, purge=False):
        self.calls.append(("delete", name, purge))
        self.members.pop(name, None)
        return {"name": name, "archived": True}

    def rename_api_squad(self, name, new_name):
        self.calls.append(("rename", name, new_name))
        return {"name": new_name}

    def list_squad_members(self, name):
        return [{"seat": s, "muted": False, "source": "api", "expires": 0}
                for s in self.members.get(name, [])]

    def add_squad_member(self, name, seat, expires=0.0, source="api"):
        self.calls.append(("add", name, seat, expires, source))
        self.members.setdefault(name, []).append(seat)
        return {"seat": seat, "squad": name}

    def remove_squad_member(self, name, seat):
        self.calls.append(("remove", name, seat))
        self.members.get(name, []).remove(seat)
        return {}


def _args(**kw) -> argparse.Namespace:
    base = dict(action="list", name=None, members=[], to="", into="",
                description="", until="", purge=False, keep_source=False,
                dry_run=False, hub_url="http://hub", json=False)
    base.update(kw)
    return argparse.Namespace(**base)


# ------------------------------------------------------------------ the basics


def test_add_and_remove_reach_the_hub(capsys):
    api = FakeApi({"dreamteam": []})
    assert cli.squads_command(
        _args(action="add", name="dreamteam", members=["alice", "bob"]),
        api=api) == 0
    assert [c[:3] for c in api.calls] == [
        ("add", "dreamteam", "alice"), ("add", "dreamteam", "bob")]

    api.calls.clear()
    assert cli.squads_command(
        _args(action="remove", name="dreamteam", members=["alice"]),
        api=api) == 0
    assert api.calls == [("remove", "dreamteam", "alice")]


def test_add_with_until_marks_it_a_LOAN_and_says_the_deadline_is_enforced(
        capsys):
    """A deadline the operator believes is merely recorded gets diarised by
    hand — which is the bookkeeping this exists to remove."""
    api = FakeApi({"spike": []})
    cli.squads_command(
        _args(action="add", name="spike", members=["alice"], until="+7d"),
        api=api)
    _verb, _squad, _seat, expires, source = api.calls[0]
    assert expires > 0 and source == "loan"
    out = capsys.readouterr().out
    assert "ends by itself" in out


def test_a_malformed_until_FAILS_rather_than_joining_permanently(capsys):
    """The dangerous direction. Treating an unreadable duration as "no
    deadline" turns a typo into a permanent membership, silently."""
    api = FakeApi({"spike": []})
    rc = cli.squads_command(
        _args(action="add", name="spike", members=["alice"], until="7 days"),
        api=api)
    assert rc == 1
    assert api.calls == [], "joined the squad anyway, with no deadline at all"


def test_dry_run_writes_nothing(capsys):
    api = FakeApi({"spike": []})
    assert cli.squads_command(
        _args(action="add", name="spike", members=["alice"], dry_run=True),
        api=api) == 0
    assert api.calls == []


# -------------------------------------------------------------------- the fork


class TestFork:
    """Scenario 1 — pull two or three out of a squad onto a question."""

    def test_it_COPIES_leaving_the_source_intact(self, capsys):
        """The load-bearing property. A fork that also removed the members
        would make 'lend three people to a spike' inexpressible, and that is
        the far more common need."""
        api = FakeApi({"dreamteam": ["alice", "bob", "carol"]})
        rc = cli.squads_command(
            _args(action="fork", name="dreamteam", to="spike-x",
                  members=["alice", "bob"]), api=api)
        assert rc == 0
        assert api.members["spike-x"] == ["alice", "bob"]
        assert api.members["dreamteam"] == ["alice", "bob", "carol"], (
            "forking moved members out of the source squad")
        assert not any(c[0] == "remove" for c in api.calls)

    def test_naming_no_members_forks_the_WHOLE_squad(self):
        """The other real case: a squad splitting in two."""
        api = FakeApi({"dreamteam": ["alice", "bob"]})
        cli.squads_command(
            _args(action="fork", name="dreamteam", to="spike-x"), api=api)
        assert api.members["spike-x"] == ["alice", "bob"]

    def test_a_MISTYPED_member_is_refused_rather_than_quietly_dropped(
            self, capsys):
        """🔴 The one that would bite hardest in practice. Forking a subset of
        what was asked for produces a spike team silently missing the one
        person it was assembled for, and nothing would ever say so."""
        api = FakeApi({"dreamteam": ["alice", "bob"]})
        rc = cli.squads_command(
            _args(action="fork", name="dreamteam", to="spike-x",
                  members=["alice", "carol"]), api=api)
        assert rc == 1
        assert "carol" in capsys.readouterr().err
        assert "spike-x" not in api.members, (
            "created the fork anyway, one member short")

    def test_a_forked_member_can_be_a_loan(self):
        api = FakeApi({"dreamteam": ["alice"]})
        cli.squads_command(
            _args(action="fork", name="dreamteam", to="spike-x",
                  until="+3d"), api=api)
        add = next(c for c in api.calls if c[0] == "add")
        assert add[3] > 0

    def test_forking_an_empty_squad_is_refused(self, capsys):
        api = FakeApi({"dreamteam": []})
        assert cli.squads_command(
            _args(action="fork", name="dreamteam", to="spike-x"), api=api) == 1

    def test_it_requires_a_destination(self, capsys):
        api = FakeApi({"dreamteam": ["alice"]})
        assert cli.squads_command(
            _args(action="fork", name="dreamteam"), api=api) == 1
        assert "--to" in capsys.readouterr().err


# ------------------------------------------------------------------- the merge


class TestMerge:
    """Scenario 6 — two threads converge."""

    def test_members_move_and_the_source_is_archived(self, capsys):
        api = FakeApi({"a": ["alice", "bob"], "b": ["carol"]})
        rc = cli.squads_command(
            _args(action="merge", name="a", into="b"), api=api)
        assert rc == 0
        assert sorted(api.members["b"]) == ["alice", "bob", "carol"]
        assert ("delete", "a", True) in api.calls, (
            "left the source squad alive — the fleet keeps broadcasting to a "
            "squad nobody remembers is still there")

    def test_keep_source_leaves_it_alone(self):
        api = FakeApi({"a": ["alice"], "b": []})
        cli.squads_command(
            _args(action="merge", name="a", into="b", keep_source=True),
            api=api)
        assert not any(c[0] == "delete" for c in api.calls)
        assert api.members["a"] == ["alice"]

    def test_an_overlapping_member_is_not_added_twice(self, capsys):
        api = FakeApi({"a": ["alice"], "b": ["alice", "carol"]})
        cli.squads_command(_args(action="merge", name="a", into="b"), api=api)
        assert api.members["b"].count("alice") == 1

    def test_merged_members_arrive_PERMANENT_even_if_they_were_on_loan(self):
        """A loan that survived the merge would end inside the merged squad,
        silently removing someone from a team they were merged into rather
        than lent to."""
        api = FakeApi({"a": ["alice"], "b": []})
        cli.squads_command(_args(action="merge", name="a", into="b"), api=api)
        add = next(c for c in api.calls if c[0] == "add")
        assert add[3] == 0.0

    def test_merging_a_squad_into_itself_is_refused(self, capsys):
        api = FakeApi({"a": ["alice"]})
        assert cli.squads_command(
            _args(action="merge", name="a", into="a"), api=api) == 1

    def test_it_requires_a_survivor(self, capsys):
        api = FakeApi({"a": ["alice"]})
        assert cli.squads_command(_args(action="merge", name="a"), api=api) == 1
        assert "--into" in capsys.readouterr().err


# --------------------------------------------------------------- discoverable


class TestBothSpellingsOfTheMemberList:
    """🔴 Found by SMOKE-TESTING the verb against a live hub, with 1543 unit
    tests green. Every test above passes a Namespace straight in, so none of
    them exercises argparse — and argparse cannot bind a trailing `nargs="*"`
    positional that appears after an option. `fork dt --to spike alice bob`,
    the order that reads most naturally and the one the docs gave first, died
    with "unrecognized arguments".

    ⭐ The lesson is about the harness, not the code: a test that constructs
    the parsed object can never find a parsing bug. These go through
    `build_parser()` for exactly that reason.
    """

    def _parse(self, argv):
        return cli.build_parser().parse_args(argv)

    def test_positional_members_before_the_flags(self):
        a = self._parse(["squads", "fork", "dt", "alice", "bob",
                         "--to", "spike"])
        assert a.members == ["alice", "bob"] and a.to == "spike"

    def test_members_flag_works_after_other_flags(self):
        a = self._parse(["squads", "fork", "dt", "--to", "spike",
                         "--members", "alice,bob"])
        assert a.members_flag == "alice,bob"

    def test_the_two_spellings_reach_the_hub_identically(self):
        got = []
        for argv in (["squads", "fork", "dt", "alice", "bob", "--to", "spike"],
                     ["squads", "fork", "dt", "--to", "spike",
                      "--members", "alice,bob"]):
            api = FakeApi({"dt": ["alice", "bob", "carol"]})
            cli.squads_command(self._parse(argv), api=api)
            got.append(api.members["spike"])
        assert got[0] == got[1] == ["alice", "bob"]

    def test_a_seat_named_twice_is_added_once(self):
        api = FakeApi({"dt": ["alice"]})
        cli.squads_command(
            self._parse(["squads", "fork", "dt", "alice", "--to", "spike",
                         "--members", "alice"]), api=api)
        assert api.members["spike"] == ["alice"]


def test_the_verb_is_reachable_through_both_registries():
    """Implemented, documented and unreachable is a shape this repo has
    already shipped once (test_cli_discoverability.py). Dispatch AND parser."""
    from mcp_hub.server import _CLI_SUBCOMMANDS
    assert "squads" in _CLI_SUBCOMMANDS
    names = set(cli.build_parser()._subparsers._group_actions[0].choices)
    assert "squads" in names
    with pytest.raises(SystemExit) as e:
        cli.main(["squads", "--help"])
    assert e.value.code == 0


# --------------------------------------------------- gap 6: reading the result


class LogsApi:
    def __init__(self, placements):
        self._p = placements

    def list_placements(self):
        return self._p


def test_logs_refuses_and_NAMES_THE_MACHINE_when_the_seat_is_elsewhere(capsys):
    """🔴 The honest failure. `docker logs` only works where the container is,
    and returning empty output would read as "the seat printed nothing" — an
    operator who believes that stops looking."""
    args = _args(action="logs", name=None)
    args.identity = "errand-1"
    args.machine = None
    args.tail = "200"
    args.follow = False
    # A name no real box carries: the test ran green for weeks and then
    # failed on dev-vm-1 itself, because the placement said "dev-vm-1" and
    # the premise ("the seat is elsewhere") collapses on the machine whose
    # name it borrowed. Hostname-coupled fixtures are latent until the one
    # box they name runs the suite.
    rc = cli.seats_command(
        args, api=LogsApi([{"seat": "errand-1", "machine": "not-this-box"}]))
    err = capsys.readouterr().err
    assert rc == 1
    assert "not-this-box" in err
    assert "ssh not-this-box" in err, \
        "refused without saying where to go instead"


def test_logs_says_so_when_the_seat_was_never_PLACED(capsys):
    """Declared but never placed means nothing has ever run — a different
    fact from 'it ran and said nothing', and the operator needs to tell them
    apart."""
    args = _args(action="logs", name=None)
    args.identity = "errand-1"
    args.machine = None
    args.tail = "200"
    args.follow = False
    rc = cli.seats_command(args, api=LogsApi([]))
    assert rc == 1
    assert "never placed" in capsys.readouterr().err


# ---------------------------------------------------------------- rm wording


class TestRmSaysWhatItDid:
    """Found live closing #168 (2026-08-26): `squads rm capsule --purge`
    printed "archived — its message history is KEPT", the ARCHIVE message,
    and the operator had to run list_squads to learn the purge had in fact
    worked. A success string that describes a different, weaker act than the
    one performed is the lying-receipt family — the reader either concludes
    the purge failed, or worse, believes the name is still reserved when it
    is free."""

    def test_purge_says_purged_not_archived(self, capsys):
        api = FakeApi({"capsule": ["a", "b"]})
        rc = cli.squads_command(
            _args(action="rm", name="capsule", purge=True), api=api)
        assert rc == 0
        assert api.calls == [("delete", "capsule", True)]
        out = capsys.readouterr().out.lower()
        assert "purged" in out, f"the purge reported itself as: {out!r}"
        assert "archived" not in out, (
            "purge described itself as the weaker act — the operator who "
            f"reads this verifies by hand or believes it failed: {out!r}"
        )
        assert "history" in out, (
            "history survival is the one thing purge does NOT change and "
            f"the reader will worry about first — say it: {out!r}"
        )

    def test_purge_says_the_name_is_free(self, capsys):
        """The 409-reserved trap was the whole reason purge exists; its
        success message should close that loop."""
        api = FakeApi({"capsule": []})
        cli.squads_command(
            _args(action="rm", name="capsule", purge=True), api=api)
        out = capsys.readouterr().out.lower()
        assert "free" in out or "reuse" in out, (
            f"purge did not say the name is reusable again: {out!r}")

    def test_bare_rm_still_says_archived_and_offers_purge(self, capsys):
        api = FakeApi({"capsule": []})
        cli.squads_command(
            _args(action="rm", name="capsule", purge=False), api=api)
        out = capsys.readouterr().out.lower()
        assert "archived" in out
        assert "--purge" in out, (
            "the bare archive must keep pointing at the finishing move, or "
            f"the operator is back in the no-route-to-finish trap: {out!r}"
        )
