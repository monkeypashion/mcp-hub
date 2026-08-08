"""`capsules rm` — the one registry that could only ever GROW.

🔴 Found by the operator asking the right question rather than by a failure:
"through the CLI, can we support any container or workspace or squad
scenario?" Seats archive, placements reclaim, workspaces remove — capsules had
list/compose/place/attach and no way back. The server had
`DELETE /api/v1/capsules/{id}` all along; nothing reached it.

⚠️ Deliberately no cascade and no "placements first" gate, unlike `seats rm`.
A capsule is a SNAPSHOT: `place` copies the manifest into per-seat placements
and nothing refers back afterwards. Removing one takes away the ability to
re-place that snapshot and changes nothing about what is already running.
"""
from __future__ import annotations

import argparse

from mcp_hub import cli


class _Api:
    def __init__(self):
        self.deleted: list[str] = []

    def list_capsules(self):
        return [{"id": "cap-1", "squad": "alpha", "manifest": {"seats": [{}]}}]

    def delete_capsule(self, cid):
        self.deleted.append(cid)
        return {"id": cid, "removed": True}


def _run(api, capsys, target="cap-1", dry=False):
    args = argparse.Namespace(action="rm", target=target, dry_run=dry,
                              squad=None, machine=None, workspace=None,
                              register=False, json=False, hub_url=None)
    rc = cli.capsules_command(args, api=api)
    return rc, capsys.readouterr()


def test_it_removes_the_capsule(capsys):
    api = _Api()
    rc, out = _run(api, capsys)
    assert rc == 0 and api.deleted == ["cap-1"], out.out


def test_dry_run_writes_NOTHING(capsys):
    api = _Api()
    rc, out = _run(api, capsys, dry=True)
    assert rc == 0 and api.deleted == [], "dry run deleted it anyway"
    assert "would remove" in out.out


def test_it_refuses_without_a_target(capsys):
    api = _Api()
    rc, out = _run(api, capsys, target=None)
    assert rc == 1 and api.deleted == []
    assert "name the capsule" in out.err


def test_it_SAYS_that_running_placements_are_unaffected(capsys):
    """The natural fear is that this tears down a live squad, and that fear is
    what would stop someone tidying. The opposite is true, so say it."""
    _rc, out = _run(_Api(), capsys)
    low = out.out.lower()
    assert "keeps running" in low or "untouched" in low, out.out


def test_the_verb_is_reachable_through_both_registries():
    """Same guard that caught `workspaces` and `voice-client` shipping
    unreachable: the parser and the dispatch set must BOTH know it."""
    from mcp_hub.server import _CLI_SUBCOMMANDS

    choices = cli.build_parser()._subparsers._group_actions[0].choices
    assert "capsules" in choices and "capsules" in _CLI_SUBCOMMANDS
    assert "rm" in choices["capsules"]._actions[1].choices, \
        "capsules rm is implemented but not offered by the parser"
