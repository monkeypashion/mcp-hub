"""Guests — a claude reachable only over ssh, as a tab in an existing workspace.

Operator, 2026-08-13: *"could we add it to a workspace as a lightweight guest
without the full integration? it's not in tailscale — it's on local network —
but I just need to fire up claude… it would be nice if I saw it in an existing
workspace so that it does not have a completely different workflow to access
it."*

The enabling observation is that the cockpit's own BOARD tab already proves a
tab needs neither a roster row nor a workspace folder: it is a named terminal
running a command. A guest is that shape, pointed at ssh.

⚠️ **The boundary is the design.** Without tmux on the far side there is no
`capture-pane` and no `send-keys`, so:

  possible   typing, slash commands, interrupt — the VSCode terminal IS the
             pane, so `sendText` reaches claude with no tmux involved. And
             `--continue` moves persistence from the session to the
             CONVERSATION, which is what makes a tmux-less guest usable at all.
  impossible anything that READS the far screen: state glyphs, ⚡, waiting
             time, and `answer` (which is fail-closed precisely because it
             parses the visible options first).

So a guest must never be offered an agent verb. Every one of them is tmux on
THIS box or a hub identity the guest does not have, and offering them would be
the "delivered live" mistake in a new costume.
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


GUEST_COMMANDS = {
    "squad.guest.connect", "squad.guest.resume", "squad.guest.send",
    "squad.guest.slash", "squad.guest.key", "squad.guest.interrupt",
}


# --------------------------------------------------------------- the wiring


def test_every_guest_command_is_contributed_and_registered(pkg, src):
    declared = {c["command"] for c in pkg["contributes"]["commands"]}
    registered = set(re.findall(r'registerCommand\(\s*"([^"]+)"', src))
    for cid in sorted(GUEST_COMMANDS):
        assert cid in declared, f"{cid} not in package.json"
        assert cid in registered, f"{cid} contributed but never registered"


def test_the_guest_submenu_exists_and_is_gated_on_isGuest(pkg):
    ids = {s["id"] for s in pkg["contributes"]["submenus"]}
    assert "squad.guestMenu" in ids
    items = pkg["contributes"]["menus"]["squad.guestMenu"]
    assert items, "the guest submenu is empty"
    for it in items:
        assert it.get("when") == "squad.isGuest", (
            f"{it.get('command')} is not gated on squad.isGuest")


def test_the_guest_submenu_is_reachable_from_the_tab_menu(pkg):
    top = pkg["contributes"]["menus"]["terminal/title/context"]
    entry = [x for x in top if x.get("submenu") == "squad.guestMenu"]
    assert entry, "the guest submenu never appears on a terminal tab"
    assert entry[0].get("when") == "squad.isGuest", (
        "the guest submenu shows on every tab, including real agents")


# ------------------------------------------- 🔴 guests get NO agent verbs


def test_a_guest_is_NOT_in_agentOf(src):
    """The load-bearing separation. `squad.isAgent` is derived from agentOf, so
    a guest landing in that map would light up every tmux verb — answer,
    restart, stop, focus, transport — each of which would fail at the far end
    or, worse, act on the wrong machine."""
    assert "const guestOf = new Map()" in src, "guests share agentOf"
    m = re.search(r"for \(const g of guestSpecs\(\)\).*?\n  \}", src, re.S)
    assert m, "the guest tab loop was not found"
    body = m.group(0)
    assert "guestOf.set(" in body
    assert "agentOf.set(" not in body, (
        "a guest terminal is registered in agentOf — it would be offered every "
        "per-agent verb, all of which are tmux on THIS box")


def test_isGuest_and_isAgent_are_both_set_from_the_active_terminal(src):
    m = re.search(r"function refreshLaunchContext\(\).*?\n  \}", src, re.S)
    assert m, "refreshLaunchContext not found"
    body = m.group(0)
    assert '"squad.isAgent"' in body and '"squad.isGuest"' in body, (
        "isGuest is not refreshed alongside isAgent, so the guest menu would "
        "be stale on tab focus change")


def test_no_guest_command_claims_to_ANSWER(pkg):
    """🔴 `squad answer` is fail-closed: it parses the visible options and
    refuses when its choice is not on screen. Over ssh there is no
    capture-pane, so a guest 'Answer yes' could only blind-press a digit while
    implying it had read a dialog. The honest verb is 'Send keypress'."""
    titles = {c["command"]: c["title"].lower()
              for c in pkg["contributes"]["commands"]
              if c["command"] in GUEST_COMMANDS}
    for cid, title in titles.items():
        assert "answer" not in title, (
            f"{cid} is titled {title!r} — a guest cannot read the far screen, "
            "so it must not offer to answer a dialog")


def test_the_keypress_verb_says_it_is_BLIND(src):
    """A raw keypress is legitimate; implying it was aimed is not."""
    m = re.search(r'registerCommand\(\s*"squad\.guest\.key".*?\n    \)', src, re.S)
    assert m, "squad.guest.key is not registered"
    assert "blind" in m.group(0).lower(), (
        "the keypress picker does not tell the operator it cannot see the "
        "far screen")


# ------------------------------------------------------------- the spec read


def _guest_specs(raw: list) -> list:
    """Drive the real guestSpecs()/guestSsh() through node."""
    import subprocess
    src = (EXT / "extension.js").read_text()
    fns = []
    for name in ("function guestSpecs()", "function guestSsh("):
        i = src.index(name)
        j = src.index("\n}\n", i)
        fns.append(src[i:j + 2])
    shim = (
        "const vscode = { workspace: { getConfiguration: () => ({ get: () => "
        + json.dumps(raw)
        + " }) } };\n" + "\n".join(fns)
        + "\nconsole.log(JSON.stringify(guestSpecs().map("
          "g => [g.label, guestSsh(g), guestSsh(g, '--continue')])));"
    )
    out = subprocess.run(["node", "-e", shim], capture_output=True,
                         text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_a_guest_needs_a_host_and_defaults_its_label():
    got = _guest_specs([{"host": "box-a"}, {"label": "", "dir": "/x"}])
    assert [g[0] for g in got] == ["box-a"], (
        "a guest with no host was accepted — there is nothing to ssh to")


def test_duplicate_labels_are_dropped_not_silently_merged():
    """The assumes-unique-that-isn't shape. Two guests sharing a label would
    make the tab dedup adopt the first and drop the second without a word."""
    got = _guest_specs([
        {"label": "sam", "host": "a"}, {"label": "sam", "host": "b"}])
    assert len(got) == 1


def test_the_ssh_command_forces_a_pty():
    """Without -t claude gets no terminal and renders nothing interactive,
    which reads as a hang rather than a missing flag."""
    (_label, cmd, _resume), = _guest_specs([{"host": "box-a"}])
    assert cmd.startswith("ssh -t "), cmd


def test_a_dir_starts_claude_there_and_cmd_overrides_outright():
    got = _guest_specs([
        {"label": "d", "host": "h", "dir": "c:/Users/monke"},
        {"label": "c", "host": "h2", "dir": "/ignored",
         "cmd": "powershell -c claude"},
    ])
    by = {g[0]: g[1] for g in got}
    assert "c:/Users/monke" in by["d"] and "claude" in by["d"]
    assert "powershell -c claude" in by["c"]
    assert "/ignored" not in by["c"], (
        "cmd must override outright — the far end may not be a POSIX shell, "
        "which is the whole reason it exists")


def test_resume_passes_continue_so_a_slept_laptop_keeps_its_thread():
    """No tmux means no session persistence, so persistence has to move to the
    CONVERSATION — this is what makes a tmux-less guest usable at all."""
    (_label, _cmd, resume), = _guest_specs([{"host": "box-a"}])
    assert "--continue" in resume


def test_the_user_field_reaches_the_ssh_target():
    (_label, cmd, _r), = _guest_specs([{"host": "h", "user": "monke"}])
    assert "monke@h" in cmd


# ------------------------------------------------------------ tab behaviour


def test_a_guest_tab_does_NOT_dial_out_on_window_open(src):
    """Agent tabs attach with --no-start precisely so opening a workspace
    launches nothing. A guest auto-connecting would be worse: it wakes someone
    else's machine, and an asleep laptop fills the panel with ssh errors on
    every window restore."""
    m = re.search(r"for \(const g of guestSpecs\(\)\).*?\n  \}", src, re.S)
    body = m.group(0)
    assert "sendWhenReady" in body
    # The hint is PRINTED, never executed: printf, not a bare ssh line.
    assert "printf" in body, "the guest tab does not print a hint"
    assert not re.search(r"sendWhenReady\(\s*t,\s*`?ssh ", body), (
        "the guest tab runs ssh at window-open")


def test_unreachability_is_reported_before_the_ssh_is_run(src):
    """A guest is expected to be absent much of the time, so ssh's own error
    text would read as a broken feature rather than a sleeping laptop."""
    m = re.search(r"const guestConnect = async .*?\n  \};", src, re.S)
    assert m, "guestConnect not found"
    body = m.group(0)
    assert "guestReachable" in body
    assert body.index("guestReachable") < body.index("sendText"), (
        "the reachability probe must run BEFORE the ssh is typed")


def test_the_reachability_probe_cannot_hang_the_ui(src):
    """BatchMode so it never sits at a password prompt, ConnectTimeout so a
    black-holed host fails fast."""
    m = re.search(r"const guestReachable = .*?\n    \}\);", src, re.S)
    assert m, "guestReachable not found"
    body = m.group(0)
    assert "BatchMode=yes" in body, "the probe can block on a password prompt"
    assert "ConnectTimeout" in body, "the probe has no connect timeout"


def test_the_settings_schema_documents_the_windows_escape_hatch(pkg):
    props = pkg["contributes"]["configuration"]["properties"]
    assert "squad.guests" in props, "squad.guests is not a declared setting"
    cmd = props["squad.guests"]["items"]["properties"]["cmd"]
    text = json.dumps(cmd).lower()
    assert "powershell" in text or "posix" in text, (
        "the cmd override does not say why it exists — a Windows host is the "
        "case that needs it and the one the operator has")
