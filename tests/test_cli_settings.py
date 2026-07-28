"""`mcp-hub settings` — the read-only model behind the cockpit's settings panel.

The panel's whole claim is that every value names its SOURCE, because these
settings differ in scope: a squad usually comes from a workspace, comms is per
agent, the hub URL is per machine. A value without its source cannot answer the
only question worth asking before changing one — does this affect this agent, or
every agent on the box? So the tests here assert on sources at least as hard as
on values.
"""
from __future__ import annotations

import json

import pytest

from mcp_hub import cli
from mcp_hub.server import _CLI_SUBCOMMANDS


def test_every_cli_subcommand_is_reachable_through_the_entry_point():
    """Two registries, no coupling: `mcp-hub` is server:main, which forwards to
    the CLI only for names in an ALLOWLIST. A subcommand added to the CLI parser
    alone is fully implemented, fully tested and completely unreachable — it
    fails as `unrecognized arguments`, which reads like a typo rather than a
    missing wire. Caught exactly that way adding `settings`.
    """
    parser_names = set(cli.build_parser()._subparsers._group_actions[0].choices)
    assert parser_names, "found no subcommands at all — introspection broke"
    assert not (parser_names - _CLI_SUBCOMMANDS), (
        "CLI subcommand(s) the entry point will never forward: "
        f"{sorted(parser_names - _CLI_SUBCOMMANDS)}"
    )
    assert not (_CLI_SUBCOMMANDS - parser_names), (
        "allowlisted name(s) with no CLI parser — forwarded then rejected: "
        f"{sorted(_CLI_SUBCOMMANDS - parser_names)}"
    )


@pytest.fixture
def box(tmp_path, monkeypatch):
    """A worktree with a derived identity, a roster row and a workspace."""
    home = tmp_path / "home"
    (home / ".config" / "squad").mkdir(parents=True)
    (home / "Projects").mkdir(parents=True)
    work = home / "Projects" / "code" / "acme" / "widget"
    work.mkdir(parents=True)

    monkeypatch.setattr(cli.pathlib.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(cli, "SQUAD_CONF", home / ".config" / "squad" / "squad.conf")
    monkeypatch.setattr(cli, "_HUB_CONFIG_PATH", home / ".mcp-hub" / "config.json")
    monkeypatch.setattr(cli, "_derive_agent_identity",
                        lambda cwd: ("widget-box", "acme/widget"))
    monkeypatch.setattr(cli, "_state_dir", lambda: home / ".mcp-hub")
    (home / ".mcp-hub").mkdir(parents=True)
    (home / ".mcp-hub" / "config.json").write_text(
        json.dumps({"projects": ["acme/widget"]}), encoding="utf-8")
    (home / ".config" / "squad" / "squad.conf").write_text(
        f"widget-box|{work}||--continue --dangerously-load-development-channels server:hub|squad\n",
        encoding="utf-8")
    (home / "Projects" / "team.code-workspace").write_text(
        json.dumps({"folders": [{"path": str(work)}]}), encoding="utf-8")
    return {"home": home, "work": work}


def _rows(model, title):
    section = next(s for s in model["sections"] if s["title"] == title)
    return {r["label"]: r for r in section["rows"]}


def test_the_model_reports_launch_flags_from_the_roster(box):
    model = cli._settings_model(str(box["work"]))
    launch = _rows(model, "LAUNCH")
    assert launch["Comms (hub wake)"]["value"] == "on"
    assert launch["Resume on restart"]["value"] == "on"
    assert launch["Comms (hub wake)"]["source"] == "set on this agent"


def test_comms_needs_the_hub_not_just_the_channels_flag(box):
    """--channels pointed at some other server is not hub wake. Two conditions,
    and a single-condition read reports an unreachable agent as reachable."""
    conf = box["home"] / ".config" / "squad" / "squad.conf"
    conf.write_text(conf.read_text().replace("server:hub", "server:other"),
                    encoding="utf-8")
    launch = _rows(cli._settings_model(str(box["work"])), "LAUNCH")
    assert launch["Comms (hub wake)"]["value"] == "off"


def test_a_squad_nothing_derives_is_named_as_such(box):
    """THE row this panel exists for. The hub says dreamteam; no workspace says
    anything. Both facts are true and only showing the first is how a membership
    that nothing regenerates goes unnoticed indefinitely — register() treats an
    empty squads argument as "preserve", so a hand-set value survives every
    reconnect looking exactly like a derived one.
    """
    (box["home"] / ".mcp-hub" / "status-widget-box.json").write_text(
        json.dumps({"squads": ["dreamteam"], "muted": [], "online": True}),
        encoding="utf-8")
    squads = _rows(cli._settings_model(str(box["work"])), "SQUADS")
    assert squads["Squads"]["value"] == "dreamteam"
    assert squads["Squads"]["source"] == "set on this agent — no workspace declares it"
    assert squads["Would derive as"]["value"] == "— none —"


def test_a_derived_squad_names_the_workspace_it_came_from(box):
    (box["home"] / ".mcp-hub" / "config.json").write_text(json.dumps({
        "projects": ["acme/widget"],
        "squad_workspaces": {
            str(box["home"] / "Projects" / "team.code-workspace"): "dreamteam"},
    }), encoding="utf-8")
    (box["home"] / ".mcp-hub" / "status-widget-box.json").write_text(
        json.dumps({"squads": ["dreamteam"], "muted": []}), encoding="utf-8")
    squads = _rows(cli._settings_model(str(box["work"])), "SQUADS")
    assert squads["Squads"]["source"] == "from team.code-workspace", squads
    assert squads["Would derive as"]["value"] == "dreamteam"


def test_no_snapshot_says_unknown_rather_than_none(box):
    """"No squad" and "we have not heard" are different facts with the same
    look. Reporting the second as the first invents a membership change."""
    squads = _rows(cli._settings_model(str(box["work"])), "SQUADS")
    assert squads["Squads"]["value"] == "unknown", squads
    assert "snapshot" in squads["Squads"]["source"]


def test_an_empty_squad_list_is_reported_as_none(box):
    (box["home"] / ".mcp-hub" / "status-widget-box.json").write_text(
        json.dumps({"squads": [], "muted": []}), encoding="utf-8")
    squads = _rows(cli._settings_model(str(box["work"])), "SQUADS")
    assert squads["Squads"]["value"] == "— none —"


def test_a_muted_squad_is_marked(box):
    (box["home"] / ".mcp-hub" / "status-widget-box.json").write_text(
        json.dumps({"squads": ["dreamteam"], "muted": ["dreamteam"]}), encoding="utf-8")
    squads = _rows(cli._settings_model(str(box["work"])), "SQUADS")
    assert squads["Squads"]["value"] == "dreamteam (muted)"


def test_identity_names_the_workspaces_this_worktree_appears_in(box):
    ident = _rows(cli._settings_model(str(box["work"])), "IDENTITY")
    assert ident["Workspaces"]["value"] == "team.code-workspace"
    assert ident["Workspaces"]["source"] == "appears in 1"


def test_a_worktree_in_no_workspace_says_so(box, tmp_path):
    other = box["home"] / "Projects" / "code" / "acme" / "other"
    other.mkdir(parents=True)
    conf = box["home"] / ".config" / "squad" / "squad.conf"
    conf.write_text(f"widget-box|{other}||--continue|squad\n", encoding="utf-8")
    ident = _rows(cli._settings_model(str(other)), "IDENTITY")
    assert ident["Workspaces"]["value"] == "— none —"
    assert ident["Workspaces"]["source"] == "appears in 0"


def test_the_hub_url_names_which_source_won(box, monkeypatch):
    # This box exports MCP_HUB_URL, so without this the "default" case never
    # ran and the assertion passed only against the env it happened to inherit.
    monkeypatch.delenv("MCP_HUB_URL", raising=False)
    monkeypatch.setattr(cli, "DEFAULT_HUB_URL", cli.BUILTIN_HUB_URL)
    machine = _rows(cli._settings_model(str(box["work"])), "THIS MACHINE")
    assert machine["Hub URL"]["value"] == cli.BUILTIN_HUB_URL
    assert machine["Hub URL"]["source"] == "built-in default"


def test_a_url_inherited_from_the_environment_at_import_is_not_called_default(box, monkeypatch):
    """DEFAULT_HUB_URL is `os.environ.get("MCP_HUB_URL", <literal>)`, evaluated
    at IMPORT. So a process started with the override and then examined after it
    was cleared still holds the env value — and reading "is the var set now?"
    would report that env-derived URL as the built-in default.

    Provenance is the entire point of this panel; a row that misreports its own
    source is worse than one that shows nothing.
    """
    monkeypatch.delenv("MCP_HUB_URL", raising=False)          # cleared AFTER import
    monkeypatch.setattr(cli, "DEFAULT_HUB_URL", "http://elsewhere.invalid/mcp")
    machine = _rows(cli._settings_model(str(box["work"])), "THIS MACHINE")
    assert machine["Hub URL"]["value"] == "http://elsewhere.invalid/mcp"
    assert machine["Hub URL"]["source"] == "MCP_HUB_URL", machine["Hub URL"]
    monkeypatch.setenv("MCP_HUB_URL", "http://example.invalid/mcp")
    machine = _rows(cli._settings_model(str(box["work"])), "THIS MACHINE")
    assert machine["Hub URL"]["value"] == "http://example.invalid/mcp"
    assert machine["Hub URL"]["source"] == "MCP_HUB_URL"


def test_every_row_everywhere_carries_a_source(box):
    """The panel's one structural promise. A row without a source is a value the
    operator cannot reason about, and it is one careless append away."""
    model = cli._settings_model(str(box["work"]))
    for section in model["sections"]:
        assert section["rows"], f"empty section: {section['title']}"
        for row in section["rows"]:
            assert row.get("source"), f"{section['title']}/{row['label']} has no source"
            assert row.get("value") not in (None, ""), \
                f"{section['title']}/{row['label']} has no value"


def test_no_identity_is_a_failure_not_an_empty_panel(box, monkeypatch):
    monkeypatch.setattr(cli, "_derive_agent_identity", lambda cwd: (None, None))
    assert cli._settings_model(str(box["work"])) is None


def test_a_missing_roster_row_still_renders(box):
    """An agent can be opted in without being enrolled with squad. The panel
    must say so rather than crash — it is a diagnostic, so the states worth
    diagnosing are exactly the incomplete ones."""
    (box["home"] / ".config" / "squad" / "squad.conf").unlink()
    ident = _rows(cli._settings_model(str(box["work"])), "IDENTITY")
    assert ident["Worktree"]["source"] == "not enrolled with squad"


def test_json_output_round_trips(box, capsys):
    import argparse
    rc = cli.settings_command(argparse.Namespace(cwd=str(box["work"]), json=True))
    assert rc == 0
    model = json.loads(capsys.readouterr().out)
    assert model["agent"] == "widget-box"
    assert [s["title"] for s in model["sections"]] == [
        "IDENTITY", "SQUADS", "LAUNCH", "THIS MACHINE"]
