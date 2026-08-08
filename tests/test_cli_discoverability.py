"""Every dispatched subcommand must be FINDABLE from `mcp-hub --help`.

🔴 Operator, 2026-08-08, asking whether the container shapes still needed live
examples: *"is it all captured in cli args with proper help?"* It was not — not
because the help was bad, but because it was unreachable.

The console script is `mcp_hub.server:main`, so `mcp-hub --help` renders the
SERVER's parser. The 24 utility verbs are dispatched by an `argv[1] in
_CLI_SUBCOMMANDS` check *before* argparse ever runs, so argparse had never
heard of them and listed none. Each verb had good `--help` and you could only
reach it by already knowing its name.

⭐ The failure is a shape worth naming: **a thing can be fully implemented,
fully documented AND undiscoverable**, and the tests for the first two pass
while the third is what the user actually hits. `test_the_verb_is_reachable_
through_both_registries` in test_cli_machines.py already guards *dispatch* —
this guards *advertisement*, which is a different claim.
"""
from __future__ import annotations

import pytest

from mcp_hub import cli
from mcp_hub.server import _CLI_SUBCOMMANDS


def _help_text(capsys, monkeypatch) -> str:
    """⚠️ `server.main()` takes NO argv — it reads `sys.argv` directly, both for
    the subcommand dispatch and for `parse_args()`. The first version of this
    helper called `main(["--help"])`, which argparse simply ignored: the tests
    failed identically before and after the mutation, which is the signature of
    a harness that never reached the code it names.
    """
    import sys

    from mcp_hub.server import main as server_main
    monkeypatch.setattr(sys, "argv", ["mcp-hub", "--help"])
    with pytest.raises(SystemExit) as e:
        server_main()
    assert e.value.code == 0
    out = capsys.readouterr().out
    assert "--transport" in out, "this is not the server parser's help at all"
    return out


def test_every_dispatched_subcommand_is_listed_in_the_top_level_help(
        capsys, monkeypatch):
    """The one that would have caught it. Built from _CLI_SUBCOMMANDS rather
    than a hand-written list, so adding a verb to the dispatch set and not to
    the help is a failing test rather than a silently invisible feature."""
    out = _help_text(capsys, monkeypatch)
    missing = sorted(v for v in _CLI_SUBCOMMANDS if v not in out)
    assert not missing, (
        f"dispatched but not advertised in `mcp-hub --help`: {missing} — "
        "reachable only by someone who already knows the name")


def test_the_help_says_a_bare_invocation_runs_the_SERVER(capsys, monkeypatch):
    """`mcp-hub` with no verb starts a server and blocks. Someone exploring the
    CLI needs to know that before they type it, not after."""
    out = _help_text(capsys, monkeypatch).lower()
    assert "server" in out and "subcommand" in out


def test_the_advertised_verbs_actually_dispatch(capsys):
    """Advertisement without dispatch would be the same bug wearing the other
    face — a help listing verbs that do not run. Checks a sample across both
    registries rather than every verb, since several are long-running daemons.
    """
    parser_names = set(cli.build_parser()._subparsers._group_actions[0].choices)
    for verb in ("workspaces", "seats", "capsules", "placements", "identity"):
        assert verb in _CLI_SUBCOMMANDS, f"{verb} is not dispatched"
        assert verb in parser_names, f"{verb} has no parser"
        with pytest.raises(SystemExit) as e:
            cli.main([verb, "--help"])
        assert e.value.code == 0, f"{verb} --help did not succeed"
        capsys.readouterr()
