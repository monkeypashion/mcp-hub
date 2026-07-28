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
    assert squads["dreamteam"]["value"] == "hearing it"
    assert squads["dreamteam"]["source"] == "set on this agent — no workspace declares it"
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
    assert squads["dreamteam"]["source"] == "from team.code-workspace", squads
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
    assert squads["dreamteam"]["value"] == "muted"


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


# ---- editability: which rows can be changed, and by what -------------------

def test_model_and_effort_are_read_from_the_launch_args(box):
    """They were session-only, which made the panel lie by calling them
    settings. Now they persist as --model/--effort and the panel reads back
    what will actually be used at next start."""
    conf = box["home"] / ".config" / "squad" / "squad.conf"
    conf.write_text(conf.read_text().rstrip("\n").replace(
        "|squad", "|squad").replace("server:hub", "server:hub --model opus --effort high"),
        encoding="utf-8")
    launch = _rows(cli._settings_model(str(box["work"])), "LAUNCH")
    assert launch["Model"]["value"] == "opus"
    assert launch["Effort"]["value"] == "high"
    assert launch["Model"]["source"] == "set on this agent"


def test_an_unset_model_names_whose_default_it_is(box):
    launch = _rows(cli._settings_model(str(box["work"])), "LAUNCH")
    assert launch["Model"]["value"] == "default"
    assert "claude's own default" in launch["Model"]["source"]


def test_every_editable_row_carries_a_runnable_command(box):
    """The extension shells out; it does not know what a squad or a launch flag
    is. So an edit descriptor has to be complete on its own — choices to offer,
    argv to run, and which binary runs it."""
    (box["home"] / ".mcp-hub" / "status-widget-box.json").write_text(
        json.dumps({"squads": ["dreamteam"], "muted": []}), encoding="utf-8")
    model = cli._settings_model(str(box["work"]))
    edits = [(r["label"], r["edit"]) for s in model["sections"] for r in s["rows"]
             if "edit" in r]
    assert edits, "nothing is editable at all"
    for label, edit in edits:
        assert edit["choices"], label
        assert edit["bin"] in ("squad", "mcp-hub"), label
        assert "{}" in edit["argv"], f"{label}: no placeholder for the chosen value"
        assert edit["applies"] in ("next restart", "immediately"), label


def test_derived_values_are_not_offered_for_editing(box):
    """Name, project and worktree are COMPUTED. Offering an edit implies a
    stored value that could be changed, and there isn't one."""
    ident = _rows(cli._settings_model(str(box["work"])), "IDENTITY")
    for label in ("Name", "Project", "Worktree", "Workspaces"):
        assert "edit" not in ident[label], f"{label} was offered as editable"


def test_squad_membership_offers_mute_but_never_join_or_leave(box):
    """Membership derives from declaring a workspace as a squad. A second way to
    set it here would disagree with the workspace eventually — which is the
    exact failure the derived model exists to prevent. Attention is per agent;
    membership is not."""
    (box["home"] / ".mcp-hub" / "status-widget-box.json").write_text(
        json.dumps({"squads": ["dreamteam"], "muted": []}), encoding="utf-8")
    squads = _rows(cli._settings_model(str(box["work"])), "SQUADS")
    edit = squads["dreamteam"]["edit"]
    assert edit["choices"] == ["hear", "mute"], edit
    assert "mute" in edit["argv"] and edit["bin"] == "mcp-hub"
    assert not any(w in " ".join(edit["argv"]) for w in ("join", "leave", "set_squads"))
    assert "edit" not in squads["Would derive as"], \
        "offered to edit the OUTPUT of a derivation"


def test_a_mute_applies_immediately_and_a_launch_flag_does_not(box):
    """Not interchangeable: a launch flag does nothing until the agent restarts,
    a mute lands on the hub at once. One word for both would be wrong half the
    time about whether the change is in force."""
    (box["home"] / ".mcp-hub" / "status-widget-box.json").write_text(
        json.dumps({"squads": ["dreamteam"], "muted": []}), encoding="utf-8")
    model = cli._settings_model(str(box["work"]))
    squads = _rows(model, "SQUADS")
    launch = _rows(model, "LAUNCH")
    assert squads["dreamteam"]["edit"]["applies"] == "immediately"
    assert launch["Comms (hub wake)"]["edit"]["applies"] == "next restart"
    assert launch["Model"]["edit"]["applies"] == "next restart"


def test_the_edit_command_names_the_agent_it_will_change(box):
    """The panel is opened per agent and the command runs detached from it. An
    argv that relied on cwd would edit whichever agent the extension host
    happened to be sitting in."""
    launch = _rows(cli._settings_model(str(box["work"])), "LAUNCH")
    assert "widget-box" in launch["Comms (hub wake)"]["edit"]["argv"]
    assert "widget-box" in launch["Model"]["edit"]["argv"]


# ---- `mcp-hub mute`: the cli half of it ------------------------------------
#
# The hub tool itself is covered in test_broadcast_scope (6 tests). What was
# untested is the part written for the cockpit: turning the operator's word into
# the tool's boolean, and what happens to the cached copy afterwards.

def _mute_args(**kw):
    import argparse
    base = dict(agent="widget-box", squad="dreamteam", state="mute",
                hub_url="http://hub.invalid/mcp")
    base.update(kw)
    return argparse.Namespace(**base)


@pytest.mark.parametrize("state,expected", [("mute", True), ("hear", False)])
def test_the_operators_word_maps_to_the_tools_boolean(box, monkeypatch, state, expected):
    """`--state hear|mute` reads correctly at the command line; `muted=` reads
    correctly at the tool. The mapping between them is one negation away from
    silencing a squad when asked to unsilence it, and the two spellings mean the
    round trip cannot be checked by eye."""
    seen = {}

    async def fake(hub_url, name, squad, muted):
        seen.update(hub_url=hub_url, name=name, squad=squad, muted=muted)
        return "ok"

    monkeypatch.setattr(cli, "_mute_squad", fake)
    assert cli.mute_command(_mute_args(state=state)) == 0
    assert seen["muted"] is expected, seen
    assert seen["name"] == "widget-box" and seen["squad"] == "dreamteam"


def test_a_successful_mute_is_RECORDED_in_the_snapshot_not_erased(box, monkeypatch):
    """This REPLACES "a successful mute drops the cached snapshot".

    Deleting looked principled — the daemon is the honest author of that file,
    so don't hand-edit it. But the daemon rewrites it about once a minute, so
    between the write and the next beat every reader reported the squad as
    `unknown`. The operator changed a value and watched it become unknown
    (2026-07-28). "We do not know" is strictly worse than the state we just
    successfully applied.
    """
    snap = box["home"] / ".mcp-hub" / "status-widget-box.json"
    snap.write_text(json.dumps({"squads": ["dreamteam"], "muted": []}), encoding="utf-8")

    async def fake(*a, **k):
        return "ok"

    monkeypatch.setattr(cli, "_mute_squad", fake)
    assert cli.mute_command(_mute_args(state="mute")) == 0
    after = json.loads(snap.read_text())
    assert after["muted"] == ["dreamteam"], after
    assert after["squads"] == ["dreamteam"], "membership was disturbed by a mute"


def test_unmuting_removes_it_again(box, monkeypatch):
    snap = box["home"] / ".mcp-hub" / "status-widget-box.json"
    snap.write_text(json.dumps({"squads": ["dreamteam"], "muted": ["dreamteam"]}),
                    encoding="utf-8")

    async def fake(*a, **k):
        return "ok"

    monkeypatch.setattr(cli, "_mute_squad", fake)
    assert cli.mute_command(_mute_args(state="hear")) == 0
    assert json.loads(snap.read_text())["muted"] == []


def test_muting_twice_does_not_duplicate_the_entry(box, monkeypatch):
    snap = box["home"] / ".mcp-hub" / "status-widget-box.json"
    snap.write_text(json.dumps({"squads": ["dreamteam"], "muted": ["dreamteam"]}),
                    encoding="utf-8")

    async def fake(*a, **k):
        return "ok"

    monkeypatch.setattr(cli, "_mute_squad", fake)
    cli.mute_command(_mute_args(state="mute"))
    assert json.loads(snap.read_text())["muted"] == ["dreamteam"]


def test_no_snapshot_yet_is_left_for_the_daemon(box, monkeypatch):
    """Inventing a cache file here would publish a snapshot with no online or
    wakeable fields, which the statusline reads as an agent that is down."""
    snap = box["home"] / ".mcp-hub" / "status-widget-box.json"

    async def fake(*a, **k):
        return "ok"

    monkeypatch.setattr(cli, "_mute_squad", fake)
    assert cli.mute_command(_mute_args()) == 0
    assert not snap.exists()


def test_a_failed_mute_reports_and_keeps_the_snapshot(box, monkeypatch):
    """Nothing changed on the hub, so the cached copy is still ACCURATE.
    Dropping it there would replace a correct value with 'unknown' — turning a
    failed write into a second, invented, symptom."""
    snap = box["home"] / ".mcp-hub" / "status-widget-box.json"
    snap.write_text(json.dumps({"squads": ["dreamteam"], "muted": []}), encoding="utf-8")

    async def boom(*a, **k):
        raise RuntimeError("hub unreachable")

    monkeypatch.setattr(cli, "_mute_squad", boom)
    assert cli.mute_command(_mute_args()) == 1
    assert json.loads(snap.read_text())["muted"] == [], \
        "recorded a mute that never reached the hub"


def test_the_settings_edit_and_the_mute_command_agree_on_spelling(box):
    """The panel builds `--state {}` from the row's choices, so the choices ARE
    the accepted values. A rename on either side leaves a dropdown whose entries
    the command rejects — and the panel would report the refusal as if the hub
    had said no."""
    (box["home"] / ".mcp-hub" / "status-widget-box.json").write_text(
        json.dumps({"squads": ["dreamteam"], "muted": []}), encoding="utf-8")
    squads = _rows(cli._settings_model(str(box["work"])), "SQUADS")
    choices = squads["dreamteam"]["edit"]["choices"]
    parser = cli.build_parser()
    accepted = set(
        parser._subparsers._group_actions[0]
        .choices["mute"]._option_string_actions["--state"].choices
    )
    assert set(choices) == accepted, f"panel offers {choices}, cli accepts {accepted}"
