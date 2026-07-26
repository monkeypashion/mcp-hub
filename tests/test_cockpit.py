"""The cockpit's menu path — the only way the operator actually drives any of this.

Everything about transport and teardown was exercised from the command line while
the menu path had never run once. `node --check` proves the file parses, which is
a different claim from "this menu entry reaches a command" or "this command
builds the shell line you think it does".

These drive the real extension against a stubbed VSCode API (tests/cockpit_harness.js):
activate it, then invoke a command and assert what it sent to a terminal.

PATH is deliberately emptied so host enumeration (`tailscale status`) finds
nothing and the machine-picker step is skipped. Otherwise every expectation here
would depend on whether a tailnet peer happened to be awake.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
HARNESS = ROOT / "tests" / "cockpit_harness.js"
EXT = ROOT / "squad" / "vscode-squad-terminals"

pytestmark = pytest.mark.skipif(
    not HARNESS.exists() or not (EXT / "extension.js").exists()
    or subprocess.run(["sh", "-c", "command -v node"], capture_output=True).returncode != 0,
    reason="cockpit extension or node not present",
)


@pytest.fixture
def box(tmp_path):
    """A sandboxed HOME with one roster agent called 'demo'."""
    home = tmp_path / "home"
    (home / ".config" / "squad").mkdir(parents=True)
    (home / "Projects").mkdir(parents=True)
    (home / ".config" / "squad" / "squad.conf").write_text(
        f"demo|{home}/Projects/demo|||\n", encoding="utf-8")
    return home


def _drive(home: pathlib.Path, mode: str, target: str = "",
           answers=None, wsfile: str = "", terminal: str = "") -> dict:
    env = dict(
        os.environ,
        HOME=str(home),
        PATH="/nonexistent",          # no tailscale ⇒ no machine-picker step
        HARNESS_ANSWERS=json.dumps(answers or []),
    )
    if wsfile:
        env["HARNESS_WSFILE"] = wsfile
    if terminal:
        env["HARNESS_TERMINAL"] = terminal
    node = subprocess.run(["sh", "-c", "command -v node"], capture_output=True, text=True)
    cmd = [node.stdout.strip(), str(HARNESS), mode] + ([target] if target else [])
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=env)
    assert res.stdout.strip(), f"harness produced nothing (stderr: {res.stderr[-800:]})"
    return json.loads(res.stdout.strip().splitlines()[-1])


# ---- manifest consistency -------------------------------------------------

def test_every_menu_entry_reaches_a_real_command(box):
    """A menu entry naming a command that isn't registered throws ON CLICK.

    Registration is partly dynamic (model/effort/voice/slash/comms/resume are
    registered in loops), so grepping for registerCommand("literal") misses 31 of
    52 and reports false orphans — measured. Only activating the extension gives
    the true list.
    """
    pkg = json.loads((EXT / "package.json").read_text())
    in_menus = {i["command"] for items in pkg["contributes"]["menus"].values()
                for i in items if "command" in i}
    registered = set(_drive(box, "commands")["registered"])
    assert not (in_menus - registered), \
        f"menu entries with no implementation: {sorted(in_menus - registered)}"


def test_every_declared_command_is_implemented_and_reachable(box):
    pkg = json.loads((EXT / "package.json").read_text())
    declared = {c["command"] for c in pkg["contributes"]["commands"]}
    in_menus = {i["command"] for items in pkg["contributes"]["menus"].values()
                for i in items if "command" in i}
    registered = set(_drive(box, "commands")["registered"])
    assert not (declared - registered), \
        f"declared but not implemented: {sorted(declared - registered)}"
    orphans = sorted(registered - declared)
    assert not orphans, f"implemented but undeclared (palette cannot reach): {orphans}"
    assert not (declared - in_menus), \
        f"declared but in no menu (right-click cannot reach it): {sorted(declared - in_menus)}"


# ---- teardown, the destructive one ---------------------------------------

def test_teardown_keeps_code_unless_deletion_is_chosen(box, tmp_path):
    ws = tmp_path / "demo.code-workspace"
    ws.write_text('{"folders":[],"settings":{}}', encoding="utf-8")
    out = _drive(box, "run", "squad.teardownWorkspace",
                 answers=["keep every folder on disk", "Tear down"], wsfile=str(ws))
    assert len(out["sent"]) == 1, out
    assert "teardown workspace" in out["sent"][0]
    assert "--delete-worktrees" not in out["sent"][0], \
        "the keep-code choice must never pass the deletion flag"
    assert "--yes" in out["sent"][0]


def test_teardown_delete_choice_passes_both_flags(box, tmp_path):
    ws = tmp_path / "demo.code-workspace"
    ws.write_text('{"folders":[],"settings":{}}', encoding="utf-8")
    out = _drive(box, "run", "squad.teardownWorkspace",
                 answers=["delete the transported clones", "Tear down and delete"],
                 wsfile=str(ws))
    assert len(out["sent"]) == 1, out
    assert "--delete-worktrees" in out["sent"][0]
    assert "--yes" in out["sent"][0]


def test_teardown_declined_at_the_confirm_sends_nothing(box, tmp_path):
    """The confirm is the last thing between a click and a deleted worktree."""
    ws = tmp_path / "demo.code-workspace"
    ws.write_text('{"folders":[],"settings":{}}', encoding="utf-8")
    out = _drive(box, "run", "squad.teardownWorkspace",
                 answers=["delete the transported clones"], wsfile=str(ws))
    assert out["sent"] == [], "declining must not run anything"
    assert any("Tear down" in s for s in out["shown"]), "but it must have asked"


def test_teardown_without_a_workspace_warns_instead_of_acting(box):
    out = _drive(box, "run", "squad.teardownWorkspace", answers=[])
    assert out["sent"] == []
    assert any("code-workspace" in s for s in out["shown"]), out


# ---- transport -----------------------------------------------------------

def test_transport_can_create_a_brand_new_workspace(box, tmp_path):
    """The operator's opening move, from the menu: there is no workspace yet.

    This branch used to be unreachable — an empty list dead-ended in a warning.
    """
    out = _drive(box, "run", "squad.transport",
                 answers=["New workspace", "sidecar"], terminal="demo · idle")
    assert len(out["sent"]) == 1, out
    cmd = out["sent"][0]
    assert "transport demo --to" in cmd
    assert "sidecar.code-workspace" in cmd
    assert "--host" not in cmd, "this machine ⇒ no host flag"


def test_transport_with_no_agent_selected_warns(box):
    out = _drive(box, "run", "squad.transport", answers=[])
    assert out["sent"] == []
    assert any("no squad agent" in s for s in out["shown"]), out


def test_transport_workspace_scopes_to_this_workspace_and_confirms(box, tmp_path):
    """Use case 2's entry point: move THIS workspace's agents, not the machine's."""
    ws = tmp_path / "here.code-workspace"
    ws.write_text('{"folders":[],"settings":{}}', encoding="utf-8")
    out = _drive(box, "run", "squad.transportWorkspace",
                 answers=["New workspace", "newbox", "Transport"], wsfile=str(ws))
    assert len(out["sent"]) == 1, out
    cmd = out["sent"][0]
    assert "transport workspace" in cmd and str(ws) in cmd, cmd
    assert "newbox.code-workspace" in cmd


def test_transport_workspace_declined_sends_nothing(box, tmp_path):
    ws = tmp_path / "here.code-workspace"
    ws.write_text('{"folders":[],"settings":{}}', encoding="utf-8")
    out = _drive(box, "run", "squad.transportWorkspace",
                 answers=["New workspace", "newbox"], wsfile=str(ws))
    assert out["sent"] == [], "no confirmation ⇒ nothing runs"


@pytest.mark.parametrize("command", [
    "squad.transportWorkspace",
    "squad.addFolder",
    "squad.wsRemove",
    "squad.teardownWorkspace",
])
def test_workspace_commands_refuse_without_a_workspace_open(box, command):
    """The cockpit shows tabs by FOLDER MEMBERSHIP, so every one of these is
    meaningless outside a .code-workspace. Each must say so rather than act on a
    guess."""
    out = _drive(box, "run", command, answers=[])
    assert out["sent"] == [], f"{command} acted with no workspace open"
    assert out["shown"], f"{command} failed silently — no message at all"


def test_add_folder_cancelled_dialog_changes_nothing(box, tmp_path):
    """The stub's file dialog always cancels, which is the case worth pinning:
    an abandoned picker must not enrol anything."""
    ws = tmp_path / "here.code-workspace"
    ws.write_text('{"folders":[],"settings":{}}', encoding="utf-8")
    out = _drive(box, "run", "squad.addFolder", answers=[], wsfile=str(ws))
    assert out["sent"] == []


# ---- Start & attach: a regression that already happened once -------------

@pytest.mark.parametrize("command,mode", [
    ("squad.startAttach", "--resume"),
    ("squad.startAttachFresh", "--fresh"),
])
def test_start_and_attach_actually_attaches(box, command, mode):
    """This broke in production today and the operator caught it, not a test.

    It was rewired to start the agent via a background exec, which left the tab a
    bare shell — so "Start & attach" only started. Attaching is a property of THIS
    terminal, so the command must be TYPED INTO THE TAB, and it must contain the
    attach as well as the restart. Both halves asserted, because the bug was the
    presence of one without the other.
    """
    out = _drive(box, "run", command, terminal="demo · idle")
    assert len(out["sent"]) == 1, out
    cmd = out["sent"][0]
    assert "squad restart demo" in cmd and mode in cmd, cmd
    assert "squad attach demo" in cmd, f"started without attaching: {cmd}"


def test_start_and_attach_on_a_non_agent_tab_warns(box):
    out = _drive(box, "run", "squad.startAttach")
    assert out["sent"] == []
    assert any("no squad agent" in s for s in out["shown"]), out


def test_bulk_transport_requires_confirmation(box):
    """A bulk clone is expensive and partly irreversible: ask, then act."""
    declined = _drive(box, "run", "squad.transportAll",
                      answers=["New workspace", "bulk"], terminal="demo · idle")
    assert declined["sent"] == [], "no confirmation ⇒ nothing runs"
    assert any("bulk" in s for s in declined["shown"]), declined

    accepted = _drive(box, "run", "squad.transportAll",
                      answers=["New workspace", "bulk", "Transport"], terminal="demo · idle")
    assert len(accepted["sent"]) == 1, accepted
    assert "transport all --to" in accepted["sent"][0]
