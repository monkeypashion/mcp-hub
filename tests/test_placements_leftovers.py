"""`placements reclaim` must NAME the records that outlive the container.

🔴 Operator, 2026-08-08, after four containers were destroyed and the board
still showed them: *"Why didn't they just disappear and clean themselves up?"*

Destroying the substrate removes ONE of five records. The seat declaration, the
roster row, the workspace registration and the workspace file all survive — by
design — and nothing said so, so "delete this seat" was a five-step ritual you
had to already know.

⚠️ The fix is NOT a cascade. A seat outliving its placement is the point of
splitting them, and the roster row and workspace file belong to that machine
and to the operator. Every layer keeps its autonomy; only the surprise goes.
"""
from __future__ import annotations

import argparse

from mcp_hub import cli


class _Api:
    def __init__(self, seats=None, defs=None):
        self._seats = seats if seats is not None else [{"identity": "seat-a"}]
        self._defs = defs if defs is not None else []
        self.reclaimed = []

    def list_placements(self):
        return [{"id": "pl-1", "seat": "seat-a", "machine": "boxy"}]

    def reclaim_placement(self, pid):
        self.reclaimed.append(pid)

    def list_seats(self):
        return self._seats

    def get_registry(self):
        return {"definitions": self._defs, "discovered": []}


def _run(api, capsys):
    args = argparse.Namespace(action="reclaim", target="pl-1", dry_run=False,
                              yes=True, seat=None, machine=None,
                              substrate=None, json=False, hub_url=None)
    rc = cli.placements_command(args, api=api)
    return rc, capsys.readouterr().out


def test_it_names_the_seat_declaration_that_outlives_the_container(capsys):
    api = _Api()
    rc, out = _run(api, capsys)
    assert rc == 0 and api.reclaimed == ["pl-1"]
    assert "seats rm seat-a" in out, out


def test_it_names_the_roster_row_even_though_it_CANNOT_see_it(capsys):
    """The roster lives on the other machine. Saying nothing would read as
    'nothing left', which is the exact wrong inference — and is what actually
    happened."""
    rc, out = _run(_Api(), capsys)
    assert "roster row" in out and "boxy" in out, out


def test_it_names_a_workspace_that_still_lists_the_seat(capsys):
    api = _Api(defs=[{"name": "capsule", "machine": "boxy",
                      "listings": ["/home/me/work/seat-a"]}])
    _rc, out = _run(api, capsys)
    assert "workspaces remove capsule" in out, out


def test_a_seat_ALREADY_archived_is_not_offered_again(capsys):
    """The positive control: it must report what is actually there, not a
    fixed checklist. A hardcoded list would pass every test above."""
    api = _Api(seats=[])
    _rc, out = _run(api, capsys)
    assert "seats rm" not in out, out
    assert "roster row" in out, "dropped the advice it cannot verify"


def test_advice_failure_never_fails_the_reclaim(capsys):
    """The reclaim already happened. Advice is a courtesy and must not turn a
    completed destructive action into a non-zero exit."""
    api = _Api()
    api.list_seats = lambda: (_ for _ in ()).throw(RuntimeError("hub down"))
    rc, out = _run(api, capsys)
    assert rc == 0 and api.reclaimed == ["pl-1"]
    assert "roster row" in out
