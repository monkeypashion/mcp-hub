"""Every CLI verb must be reachable from the console script.

There are TWO registries and they are not the same thing:
  · `cli.build_parser()` knows how to PARSE a verb;
  · `server._CLI_SUBCOMMANDS` decides whether `mcp-hub <verb>` DELEGATES to
    the client CLI at all.

A verb in the parser but not the set is unreachable: the server's own parser
handles the argv instead and answers "unrecognized arguments", naming neither
the real cause nor the file to fix. Measured live 2026-09-01 while adding
`send` — the parser was correct, `--help` rendered, and the console script
still refused it.

⭐ Same shape as the `hold` verb the hub could write and the edge could not
execute, found the same day: one vocabulary, two registries, nothing
reporting the gap. The fix in both places is a test that fails when they
drift, rather than a convention that they should not.
"""
from __future__ import annotations

from mcp_hub.cli import build_parser
from mcp_hub.server import _CLI_SUBCOMMANDS


def parser_verbs() -> set[str]:
    for action in build_parser()._actions:
        if getattr(action, "choices", None) and action.dest == "subcommand":
            return set(action.choices)
    raise AssertionError("no subcommand action found — build_parser changed")


def test_every_parsed_verb_is_dispatchable():
    """The direction that actually bites: parseable but unreachable."""
    missing = parser_verbs() - _CLI_SUBCOMMANDS
    assert not missing, (
        f"{sorted(missing)} are in cli.build_parser() but not in "
        "server._CLI_SUBCOMMANDS, so `mcp-hub <verb>` will answer "
        "'unrecognized arguments' instead of running them"
    )


def test_every_dispatchable_verb_is_parseable():
    """The other direction: dispatched into a CLI that cannot parse it."""
    unknown = _CLI_SUBCOMMANDS - parser_verbs()
    assert not unknown, (
        f"{sorted(unknown)} are dispatched to the client CLI but its parser "
        "does not define them"
    )


def test_the_check_is_not_vacuous():
    """A guard that examines nothing passes everything."""
    verbs = parser_verbs()
    assert len(verbs) > 10, f"only found {len(verbs)} verbs — extraction broke"
    assert "send" in verbs and "send" in _CLI_SUBCOMMANDS
