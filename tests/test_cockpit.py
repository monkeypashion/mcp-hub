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
           answers=None, wsfile: str = "", terminal: str = "",
           exec_out: str = "", exec_fail: str = "", fire_roster: bool = False) -> dict:
    env = dict(
        os.environ,
        HOME=str(home),
        PATH="/nonexistent",          # no tailscale ⇒ no machine-picker step
        HARNESS_ANSWERS=json.dumps(answers or []),
    )
    if exec_out:
        env["HARNESS_EXEC_OUT"] = exec_out
    if exec_fail:
        env["HARNESS_EXEC_FAIL"] = exec_fail
    if fire_roster:
        env["HARNESS_FIRE_ROSTER"] = "1"
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
    assert out["sent"] == [] and out["execs"] == [], "declining must not run anything"
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
    "squad.duplicate",
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


# ---- the destructive one, and the two everyday ones ----------------------

def test_retire_asks_before_removing_an_agent_everywhere(box):
    """`retire` is the one that unenrols everywhere, not just from a workspace.

    Two entries with two consequences: "Remove from this workspace" is reversible
    and local; this one is neither. It must ask.
    """
    declined = _drive(box, "run", "squad.retire", terminal="demo · idle")
    # BOTH channels: retire runs in the BACKGROUND, so asserting only on the
    # terminal text passed happily against a mutant that retired without asking.
    assert declined["sent"] == [] and declined["execs"] == [], \
        "no confirmation ⇒ nothing runs, by either route"
    assert declined["shown"], "and it must have asked"

    ok = _drive(box, "run", "squad.retire", answers=["Retire"], terminal="demo · idle")
    assert any("rm demo" in s for s in ok["execs"]), ok


def test_clone_from_github_cancelled_enrols_nothing(box):
    """The picker's own cancel path: an abandoned dialog must leave no trace."""
    out = _drive(box, "run", "squad.addFromGitHub", answers=[])
    assert out["sent"] == []


@pytest.mark.parametrize("command,expect", [
    ("squad.stop", "stop"),
    ("squad.restartResume", "restart"),
])
def test_everyday_lifecycle_commands_target_the_clicked_agent(box, command, expect):
    """Whatever else changes, these must act on the tab you right-clicked and
    name that agent explicitly — never the active terminal by accident."""
    out = _drive(box, "run", command, terminal="demo · idle")
    ran = out["sent"] + out["execs"]
    assert ran, f"{command} did nothing at all"
    assert any(f"{expect} demo" in s for s in ran), out


def test_bulk_transport_requires_confirmation(box):
    """A bulk clone is expensive and partly irreversible: ask, then act."""
    declined = _drive(box, "run", "squad.transportAll",
                      answers=["New workspace", "bulk"], terminal="demo · idle")
    assert declined["sent"] == [] and declined["execs"] == [], "no confirmation ⇒ nothing runs"
    assert any("bulk" in s for s in declined["shown"]), declined

    accepted = _drive(box, "run", "squad.transportAll",
                      answers=["New workspace", "bulk", "Transport"], terminal="demo · idle")
    assert len(accepted["sent"]) == 1, accepted
    assert "transport all --to" in accepted["sent"][0]


def test_duplicate_targets_this_workspace_with_no_picker(box, tmp_path):
    """Duplicate's whole point is that there is nothing to choose.

    Transport asks WHERE; a duplicate lands in the workspace you are already
    looking at, so a target picker here would be a question with one answer. It
    must also never type into the agent's own tab — that tab may hold a live
    claude, where a shell command becomes a prompt.
    """
    ws = tmp_path / "here.code-workspace"
    ws.write_text('{"folders":[],"settings":{}}', encoding="utf-8")
    out = _drive(box, "run", "squad.duplicate", answers=[], wsfile=str(ws),
                 terminal="demo \u00b7 idle",
                 exec_out="OK\n  dest   : /tmp/landing/demo-here\n  id-sfx : here\n")
    assert len(out["sent"]) == 1, out
    cmd = out["sent"][0]
    assert "duplicate demo" in cmd, cmd
    assert str(ws) in cmd, "a duplicate must name the workspace it joins"
    assert "--host" not in cmd, cmd
    # answers=[] means every dialog cancels; it still ran, so it asked nothing
    assert not out["shown"], f"duplicate should need no dialog: {out['shown']}"


def test_duplicate_leaves_the_open_workspace_file_alone(box, tmp_path):
    """VSCode reloads the window when a .code-workspace it has OPEN is edited
    externally, which bins the terminal panel you are watching the copy appear
    in. The cockpit's own add/remove paths already avoid that by going through
    updateWorkspaceFolders; duplicate always targets the open workspace, so it
    would hit the heuristic every single time."""
    ws = tmp_path / "here.code-workspace"
    ws.write_text('{"folders":[],"settings":{}}', encoding="utf-8")
    out = _drive(box, "run", "squad.duplicate", answers=[], wsfile=str(ws),
                 terminal="demo \u00b7 idle",
                 exec_out="OK\n  dest   : /tmp/landing/demo-here\n")
    assert "--no-folder-entry" in out["sent"][0], out["sent"][0]
    # and it asked squad where the copy lands BEFORE starting it
    assert any("duplicate demo" in e and "--dry-run" in e for e in out["execs"]), out["execs"]


def test_duplicate_reports_a_refusal_instead_of_running_it(box, tmp_path):
    """The dry run is also the gate check. A refusal belongs in a dialog, not
    scrolling past in a terminal the operator may not be looking at."""
    ws = tmp_path / "here.code-workspace"
    ws.write_text('{"folders":[],"settings":{}}', encoding="utf-8")
    out = _drive(box, "run", "squad.duplicate", answers=[], wsfile=str(ws),
                 terminal="demo \u00b7 idle",
                 exec_fail="REFUSED: demo — uncommitted changes")
    assert out["sent"] == [], "a refused duplicate must not run"
    assert any("uncommitted changes" in s for s in out["shown"]), out["shown"]


DEST = "/tmp/landing/demo-here"
DRY = f"OK\n  dest   : {DEST}\n  id-sfx : here\n"


def test_duplicate_adopts_the_folder_once_the_roster_row_appears(box, tmp_path):
    """The copy joins the workspace through the API, not by editing the file."""
    ws = tmp_path / "here.code-workspace"
    ws.write_text('{"folders":[],"settings":{}}', encoding="utf-8")
    conf = box / ".config" / "squad" / "squad.conf"
    # transport-recv writes this row last, and only on success
    conf.write_text(conf.read_text() + f"demo-here|{DEST}|||\n", encoding="utf-8")

    out = _drive(box, "run", "squad.duplicate", answers=[], wsfile=str(ws),
                 terminal="demo \u00b7 idle", exec_out=DRY, fire_roster=True)
    added = [p for op in out["folderOps"] for p in op["add"]]
    assert DEST in added, f"the copy never joined the workspace: {out['folderOps']}"


def test_duplicate_adds_no_folder_when_the_copy_never_landed(box, tmp_path):
    """THE safety property. transport-recv writes the roster row last and only
    after every step succeeded, so the row is the success signal. Adopting on
    anything weaker — a timer, the path existing — leaves a phantom folder
    pointing at nothing whenever a duplicate fails partway."""
    ws = tmp_path / "here.code-workspace"
    ws.write_text('{"folders":[],"settings":{}}', encoding="utf-8")
    # roster deliberately NOT updated: the duplicate died before writing its row
    out = _drive(box, "run", "squad.duplicate", answers=[], wsfile=str(ws),
                 terminal="demo \u00b7 idle", exec_out=DRY, fire_roster=True)
    added = [p for op in out["folderOps"] for p in op["add"]]
    assert added == [], f"adopted a folder for a copy that never landed: {added}"


# ---- operator-editable lists (prompts.txt / slash.txt) --------------------

def _write_list(home: pathlib.Path, name: str, body: str):
    (home / ".config" / "squad" / name).write_text(body, encoding="utf-8")


def test_slash_offers_the_saved_list(box, tmp_path):
    """Adding a slash command must cost one line in a file, not two source edits
    plus a version bump and an ext-align — which is why the built-in list never
    grew past the nine that shipped."""
    _write_list(box, "slash.txt", "# my usual\n\n/review\n/memory-sync\n")
    # A SUBSTRING, deliberately: the quick pick resolves it to the whole entry,
    # while the input box would hand back exactly these characters. Answering
    # with the full string passed even when the saved list was ignored entirely
    # — the assertion could not tell picked-from-list from typed-by-hand.
    out = _drive(box, "run", "squad.slash.custom", answers=["memory-sync"],
                 terminal="demo · idle")
    assert any("cmd demo /memory-sync" in e for e in out["execs"]), out


def test_slash_with_no_file_behaves_exactly_as_before(box):
    """A feature you haven't opted into must not make the old path longer.

    No file ⇒ no quick pick, no toast: straight to the input box, one step, the
    way it has always worked.
    """
    out = _drive(box, "run", "squad.slash.custom", answers=["/doctor"],
                 terminal="demo · idle")
    assert any("cmd demo /doctor" in e for e in out["execs"]), out
    assert not out["shown"], f"it should not have announced anything: {out['shown']}"


def test_slash_list_still_lets_you_type_one(box):
    """The saved list must not remove the escape hatch — same single entry,
    'type one' first, the shape the workspace picker already uses."""
    _write_list(box, "slash.txt", "/review\n")
    out = _drive(box, "run", "squad.slash.custom", answers=["Type one", "/adhoc"],
                 terminal="demo · idle")
    assert any("cmd demo /adhoc" in e for e in out["execs"]), out


def test_a_line_missing_its_slash_is_normalised_visibly(box):
    """A missing leading / is a typo, not a different intent. Normalise it, and
    show the normalised form so the list says what will actually be sent."""
    _write_list(box, "slash.txt", "review\n")
    # again a substring, so only a normalised LIST ENTRY can produce the slash
    out = _drive(box, "run", "squad.slash.custom", answers=["review"],
                 terminal="demo · idle")
    assert any("cmd demo /review" in e for e in out["execs"]), out


def test_cancelling_the_saved_list_sends_nothing(box):
    _write_list(box, "slash.txt", "/review\n")
    out = _drive(box, "run", "squad.slash.custom", answers=[], terminal="demo · idle")
    assert out["execs"] == [] and out["sent"] == [], out


def test_both_lists_share_one_reader(box):
    """prompts.txt and slash.txt are documented as having the same rules, so
    they must be PARSED by the same code — otherwise the claim decays silently.
    Comments and blank lines, proven on both.
    """
    _write_list(box, "prompts.txt", "# c\n\n  status please  \n")
    _write_list(box, "slash.txt", "# c\n\n  /status  \n")
    p = _drive(box, "run", "squad.stockPrompt", answers=["status ple"],
               terminal="demo · idle")
    s = _drive(box, "run", "squad.slash.custom", answers=["stat"],
               terminal="demo · idle")
    assert any("cmd demo status please" in e for e in p["execs"]), p
    assert any("cmd demo /status" in e for e in s["execs"]), s
