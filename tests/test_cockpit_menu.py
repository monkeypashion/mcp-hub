"""The cockpit's squad menu, reconciled against what it can actually do.

Operator, 2026-08-08: *"reconcile the squad context menu against all the new
functionality (and preexisting functionality)… some of the options may assume
that one agent has been selected — either make all options compatible on
multi-select, or filter the options if more than one seat is selected."*

🔴 **FILTERING IS NOT POSSIBLE, and that decides the whole design.** A `when`
clause can only read a context key; a context key can only be refreshed from
an event; and VSCode exposes **no terminal-selection API and no
selection-change event**. The extension learns the selection only when a
command fires (`args[1]`). Any `squad.multiSelect` key would therefore be
stale at the moment the menu is drawn.

⇒ So every menu item must be SAFE under multi-select — either it fans out over
the whole selection, or it is singular by nature and NAMES the tabs it
ignored. There is no third option, and specifically there is no "hide it".

These tests read the real `package.json` and the real `extension.js`, because
the failures worth catching here are wiring failures: a command contributed and
never registered is invisible until someone clicks it, and a menu item added
without a `when` shows up on the operator's own shell tab.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

EXT = Path(__file__).resolve().parents[1] / "squad" / "vscode-squad-terminals"

pytestmark = pytest.mark.skipif(
    not (EXT / "package.json").exists(), reason="cockpit extension not present"
)


@pytest.fixture(scope="module")
def pkg() -> dict:
    return json.loads((EXT / "package.json").read_text())


@pytest.fixture(scope="module")
def src() -> str:
    return (EXT / "extension.js").read_text()


def _menu_entries(pkg: dict):
    for menu, items in pkg["contributes"]["menus"].items():
        for it in items:
            yield menu, it


# ------------------------------------------------------------------ wiring


def test_every_menu_command_is_contributed(pkg):
    declared = {c["command"] for c in pkg["contributes"]["commands"]}
    missing = sorted({
        it["command"] for _m, it in _menu_entries(pkg)
        if "command" in it and it["command"] not in declared
    })
    assert not missing, f"in a menu but never contributed: {missing}"


def test_every_menu_submenu_exists(pkg):
    declared = {s["id"] for s in pkg["contributes"]["submenus"]}
    missing = sorted({
        it["submenu"] for _m, it in _menu_entries(pkg)
        if "submenu" in it and it["submenu"] not in declared
    })
    assert not missing, f"referenced but not declared: {missing}"


def test_every_contributed_command_is_REGISTERED(src, pkg):
    """A command in package.json with no registerCommand is invisible until
    someone clicks it and gets 'command not found'.

    ⚠️ Two registration styles, and the naive check only sees one. Most use a
    string literal; the variant families (model/effort/voice/focus, and the
    comms/resume pairs) are registered from loops over template literals, and
    the answer trio from a loop over an array of names. Rather than loosen the
    match into something that would pass for anything `squad.*` — which would
    make this test vacuous exactly where it matters — the loop families are
    named here and their MEMBERSHIP is checked instead: every contributed
    command must be either literally registered or a member of a family whose
    loop exists.
    """
    registered = set(re.findall(r'registerCommand\(\s*"([^"]+)"', src))
    families = {
        "squad.model.": r'registerCommand\(\s*`squad\.model\.\$',
        "squad.effort.": r'registerCommand\(\s*`squad\.effort\.\$',
        "squad.voice.": r'registerCommand\(\s*`squad\.voice\.\$',
        "squad.focus.": r'registerCommand\(\s*`squad\.focus\.\$',
        "squad.comms.": r'registerCommand\(\s*`squad\.\$\{verb\}',
        "squad.resume.": r'registerCommand\(\s*`squad\.\$\{verb\}',
        "squad.answer": r'\["squad\.answerYes", "yes"\]',
    }
    live = {p for p, pat in families.items() if re.search(pat, src)}
    missing = [
        c for c in (x["command"] for x in pkg["contributes"]["commands"])
        if c not in registered and not any(c.startswith(p) for p in live)
    ]
    assert not missing, f"contributed but never registered: {sorted(missing)}"


def test_the_registration_check_can_actually_FAIL(src, pkg):
    """The control for the test above: if every command matched some family
    prefix, that test would pass against an extension that registered nothing.
    A name outside every family must be detected as missing."""
    registered = set(re.findall(r'registerCommand\(\s*"([^"]+)"', src))
    assert "squad.notARealCommand" not in registered
    assert not any("squad.notARealCommand".startswith(p) for p in (
        "squad.model.", "squad.effort.", "squad.voice.", "squad.focus.",
        "squad.comms.", "squad.resume.", "squad.answer"))


# --------------------------------------------------- multi-select safety


# Singular BY NATURE — one courier, one seat to read, one thing to copy.
# These must route through withOneAgent, which names the ignored tabs.
SINGULAR = {
    "squad.broadcast",      # one sender, or the squad gets N copies
    "squad.dmVia",          # one courier
    "squad.seatLogs",       # one seat's output
    "squad.seatRebrief",    # one brief
    "squad.seatClone",      # one source, one new identity
}


def _handler_of(src: str, command: str) -> str:
    m = re.search(
        r'registerCommand\(\s*"' + re.escape(command) + r'"\s*,(.{0,900})',
        src, re.S)
    return m.group(1) if m else ""


@pytest.mark.parametrize("command", sorted(SINGULAR))
def test_singular_actions_use_withOneAgent(src, command):
    """🔴 `squad.broadcast` used to fan out over the selection, so three
    highlighted tabs sent the SAME broadcast three times — and a shared squad
    received three copies of one message. Fanning out a singular action is not
    "doing it for each agent", it is duplicating it."""
    body = _handler_of(src, command)
    assert body, f"{command} is not registered"
    assert "withOneAgent" in body, (
        f"{command} is singular by nature but does not use withOneAgent — "
        "it will either fan out or silently pick one of the selection")


def test_withOneAgent_NAMES_what_it_ignored(src):
    """Silently using one of several highlighted tabs is the kind of thing an
    operator notices only after the wrong agent has spoken."""
    m = re.search(r"function withOneAgent\(.*?\n\}", src, re.S)
    assert m, "withOneAgent not found"
    body = m.group(0)
    assert "others" in body and "shortLabel" in body, (
        "withOneAgent must name the ignored tabs, not just count them")


def test_no_menu_item_is_gated_on_a_SELECTION_derived_key(pkg):
    """🔴 THE DEFECT THIS FILE EXISTS FOR. `squad.hasComms`/`squad.hasResume`
    were computed from the ACTIVE terminal and gated the launch toggles, so
    selecting agent A (comms on) with agent B (comms off) hid the very action
    B needed while the visible one fanned out to both.

    A key that cannot be refreshed when the selection changes is a key that
    lies — and VSCode gives no selection event to refresh it from. Both halves
    of each toggle are shown instead; the CLI is idempotent and toasts what it
    did.
    """
    banned = ("squad.hasComms", "squad.hasResume")
    offenders = [
        (m, it.get("command") or it.get("submenu"), it["when"])
        for m, it in _menu_entries(pkg)
        if "when" in it and any(b in it["when"] for b in banned)
    ]
    assert not offenders, (
        f"gated on a stale, selection-blind key: {offenders}")


def test_the_dead_context_keys_are_no_longer_SET(src):
    """Setting a key nothing reads is how the next reader concludes it matters
    and re-introduces the gate."""
    for key in ("squad.hasComms", "squad.hasResume"):
        assert f'"{key}"' not in src, f"{key} is still being set"


def test_isAgent_survives_because_it_is_a_property_of_the_TAB(src, pkg):
    """The positive control. Removing all gating would put agent actions on
    the operator's own board and shell tabs — `isAgent` is per-tab, not
    per-selection, so it is the one key that stays honest."""
    assert '"squad.isAgent"' in src
    agent_items = [
        it for it in pkg["contributes"]["menus"]["squad.agentMenu"]
        if (it.get("command") or "").startswith("squad.answer")
    ]
    assert agent_items and all(
        it.get("when") == "squad.isAgent" for it in agent_items)


# ------------------------------------------- agent scope vs workspace scope


AGENT_SUBMENUS = ["squad.moveMenu", "squad.enrolMenu", "squad.seatMenu",
                  "squad.launchMenu", "squad.focusMenu",
                  "squad.messagingMenu"]


@pytest.mark.parametrize("menu", AGENT_SUBMENUS)
def test_agent_submenus_only_contain_agent_gated_items(pkg, menu):
    """Every entry in an agent submenu must answer "do this to THIS agent".
    An ungated item there shows on a shell tab and acts on something else."""
    loose = [it.get("command") for it in pkg["contributes"]["menus"][menu]
             if it.get("when") != "squad.isAgent"]
    assert not loose, f"{menu} has non-agent items: {loose}"


def test_workspace_actions_left_the_agent_menus(pkg):
    """🔴 `teardownWorkspace`, `addFolder`, `transportAll` and friends sat in
    an AGENT submenu with no `when` at all — so a right-click on the board tab
    offered to tear the workspace down. They are workspace-scoped: same menu,
    own section, never pretending to be per-agent."""
    ws = {"squad.addFolder", "squad.addFromGitHub", "squad.capsuleAttach",
          "squad.transportAll", "squad.transportWorkspace",
          "squad.teardownWorkspace"}
    in_workspace_menu = {
        it["command"] for it in pkg["contributes"]["menus"]["squad.workspaceMenu"]
    }
    assert ws == in_workspace_menu, "workspace actions are not all in one place"
    for menu in AGENT_SUBMENUS:
        strays = ws & {it.get("command") for it in
                       pkg["contributes"]["menus"][menu]}
        assert not strays, f"{menu} still holds workspace actions: {strays}"


# ------------------------------------------ reconciliation with the CLI


def test_the_new_per_seat_verbs_reached_the_cockpit(pkg):
    """The reconcile half of the ask: a CLI verb with no cockpit affordance is
    half-delivered (operator's standing rule)."""
    declared = {c["command"] for c in pkg["contributes"]["commands"]}
    for c in ("squad.seatLogs", "squad.seatRebrief", "squad.seatClone",
              "squad.manage"):
        assert c in declared, f"{c} missing"
        assert any(it.get("command") == c for _m, it in _menu_entries(pkg)), (
            f"{c} is declared but reachable from no menu — implemented and "
            "undiscoverable is a shape this repo has shipped before")


def test_fleet_management_is_NOT_in_the_per_agent_groups(pkg):
    """create/fork/merge are about the TEAM, not the right-clicked tab.
    Keeping them out is what keeps the agent menu answerable."""
    for menu in AGENT_SUBMENUS:
        assert not any(it.get("command") == "squad.manage"
                       for it in pkg["contributes"]["menus"][menu])


def test_the_fork_quickpick_uses_the_MEMBERS_FLAG_not_positionals(src):
    """⚠️ argparse cannot bind a trailing nargs="*" positional that appears
    after an option, so `squads fork dt --to x alice bob` dies with
    "unrecognized arguments" — measured 2026-08-08. The cockpit must not
    rebuild that footgun."""
    m = re.search(r'registerCommand\(\s*"squad\.manage".*?\n  \)', src, re.S)
    assert m, "squad.manage not found"
    body = m.group(0)
    assert "--members" in body, "fork must pass members as a flag"
