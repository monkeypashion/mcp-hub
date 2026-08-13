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
    "squad.copyId",         # one clipboard — N names would silently become 1
}


def _handler_of(src: str, command: str, window: int = 900) -> str:
    m = re.search(
        r'registerCommand\(\s*"' + re.escape(command) + r'"\s*,(.{0,%d})' % window,
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


# --------------------------------------------------- copyId: the RIGHT name


def test_copyId_copies_the_RAW_name_never_the_stripped_label(src):
    """🔴 The whole reason this verb exists.

    `shortLabel()` strips THIS MACHINE'S hostname, so
    `mcp-hub-fireblade-wsl` is displayed on every tab as `mcp-hub`. The
    hub's derived identity IS `<repo>-<host>`, so the stripped form is not
    addressable: `send(to="mcp-hub")` fails to route while looking exactly
    right on screen. An operator retyping what they can SEE gets the wrong
    string, which is precisely why copying is worth a menu item.

    Mutation: `writeText(shortLabel(agent))` → this fails.
    """
    body = _handler_of(src, "squad.copyId")
    assert body, "squad.copyId is not registered"
    m = re.search(r"writeText\(([^)]*)\)", body)
    assert m, "copyId does not write to the clipboard"
    written = m.group(1).strip()
    assert written == "agent", (
        f"copyId writes {written!r} — it must write the RAW agent name; "
        "shortLabel() strips the hostname and the result is unaddressable")


def test_copyId_shows_the_operator_what_landed_on_the_clipboard(src):
    """A clipboard write is invisible. Confirming with the stripped label
    would tell the operator they copied something they did not."""
    body = _handler_of(src, "squad.copyId")
    m = re.search(r"showInformationMessage\((.{0,200})", body, re.S)
    assert m, "copyId gives no feedback that anything was copied"
    assert "shortLabel" not in m.group(1), (
        "the toast must echo the FULL name that was copied, not the "
        "stripped label")


def test_copyId_is_TOP_LEVEL_and_not_under_send_to_others(pkg):
    """🔴 It was first placed in `squad.messagingMenu`, chosen by reading the
    INTERNAL ID. That submenu's visible LABEL is "Send to others", and it
    holds broadcast and message-via — acts of SENDING. Copying a name to the
    clipboard sends nothing, so it read as a category error to the one person
    who matters, who looked and did not find it.

    ⭐ The lesson is about menus generally: place by the LABEL the operator
    reads, never by the identifier the code uses. They diverge silently.

    Top level, beside "Settings…" — both are things you READ about an agent
    rather than acts performed on it, and a one-item utility needs no submenu
    to hide in.
    """
    agent_menu = pkg["contributes"]["menus"]["squad.agentMenu"]
    assert "squad.copyId" in [i.get("command") for i in agent_menu], (
        "copyId must be top-level in the Squad menu — a utility buried in a "
        "submenu whose label describes a different act is not discoverable")
    for menu, items in pkg["contributes"]["menus"].items():
        if menu == "squad.agentMenu":
            continue
        assert "squad.copyId" not in [i.get("command") for i in items], (
            f"squad.copyId also appears in {menu} — one home per verb")


def test_no_command_sits_in_a_submenu_whose_label_contradicts_it(pkg):
    """The general form of the mistake above, as a standing guard.

    "Send to others" is a VERB group: everything in it must be an act of
    sending. A copy/show/read verb landing there is the category error that
    made copyId unfindable.
    """
    titles = {c["command"]: c["title"] for c in pkg["contributes"]["commands"]}
    sending = pkg["contributes"]["menus"].get("squad.messagingMenu", [])
    for it in sending:
        title = titles.get(it.get("command"), "")
        first = title.split()[0].lower() if title else ""
        assert first not in {"copy", "show", "open", "read", "clone"}, (
            f'"{title}" is in the "Send to others" submenu but its verb is '
            f'"{first}" — it does not send anything')


# --------------------------------------- what the surfaces SAY about the act
#
# 🔴 Both found 2026-08-13 by RUNNING the verbs against a scratch roster, in
# the functional half of the capability audit. Everything above proves the
# wiring; neither of these is a wiring failure, and neither was visible to any
# static check — the item is registered, contributed, gated and singular, and
# still tells the operator the wrong thing.


def _code_only(js: str) -> str:
    """Strip `//` line comments.

    Both tests below FAILED on their own explanatory comments the first time —
    the comment names the bug, so a naive substring check reads the comment and
    passes (or, for the negative assertions, fails) regardless of the code.
    Vacuous-test shape #4: assert on the thing under test, not on prose that
    happens to sit beside it.
    """
    return "\n".join(re.sub(r"^\s*//.*$", "", ln) for ln in js.splitlines())


def test_the_retire_dialog_names_the_SESSION_KILL(src):
    """`rm_agent` runs `tm kill-session` FIRST — verified by retiring a live
    agent on a scratch tmux socket. That is the only consequence here that is
    immediate and destroys work in flight, and the dialog enumerated the other
    four precisely while omitting it.

    Nothing else can tell the operator: the CLI line that does name it
    ("session, roster, hub opt-in, daemon") goes to stdout, and squadExec
    surfaces stderr only.
    """
    body = _handler_of(_code_only(src), "squad.retire")
    assert body.strip(), "squad.retire is not registered"
    m = re.search(r"detail:(.{0,600}?)\},", body, re.S)
    assert m, "the retire confirmation has no detail text"
    detail = m.group(1).lower()
    assert any(w in detail for w in ("stops the agent", "kills", "session")), (
        "the Retire dialog does not say the agent is stopped — retiring a "
        "running agent ends its turn and the operator is not told")


def test_the_duplicate_refusal_keeps_the_REASON(src):
    """Two bugs in one line, both measured:

    - `e.stdout || e.stderr` picked ONE stream, and `squad duplicate` refuses
      on stdout for the gate checks and on stderr for the no-git case.
    - `.split("\\n").pop()` took the LAST line, which on a two-line refusal is
      the footnote. A plain folder toasted "cannot duplicate x —
      Duplicating clones from the remote, like transport does." with the
      actual reason ("has no git origin") dropped — and plain-folder agents
      are most of the roster (13 of 15 on dev-vm-1).
    """
    body = _handler_of(_code_only(src), "squad.duplicate", window=2000)
    assert body.strip(), "squad.duplicate is not registered"
    m = re.search(r"catch \(e\)(.{0,900}?)return;", body, re.S)
    assert m, "the duplicate dry-run has no refusal branch"
    branch = m.group(1)
    assert "e.stdout || e.stderr" not in branch, (
        "the refusal reads ONE stream — squad duplicate refuses on both")
    assert "e.stdout" in branch and "e.stderr" in branch, (
        "the refusal must read both streams")
    assert ".pop()" not in branch, (
        "the refusal still shows only the last line, which is the footnote "
        "rather than the reason")
