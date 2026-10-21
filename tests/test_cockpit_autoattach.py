"""Auto-attach: tabs pick up agents started from OUTSIDE this window.

Operator, 2026-08-09, on wiring a web front end to the hub: *"autoattach is a
must too"*.

🔴 THE GAP. An agent can be started by anything — `squad start` over ssh, a
cron, or the hub's edge realizing a placement a web app wrote. None of that
touches the VSCode window, and the extension only attaches at WINDOW-OPEN, so
the tab for a freshly-woken agent sits as a bare shell while the agent runs
happily behind it. The operator sees nothing happen.

These drive the real `planAutoAttach` through the stubbed-VSCode harness, not
a regex over the source. The rules being tested all have a way of being
subtly, invisibly wrong:

  · fire on the DOWN -> UP TRANSITION, not continuously — an operator who
    pressed Ctrl-b d meant it, and a loop that re-attached 8s later would be
    unusable;
  · wait out the startup-dialog dance — tmux sizes a session to its SMALLEST
    client, so attaching mid-launch narrows the pane while squad is grepping
    it for dialogs AND sending keystrokes at it (the 2026-07-25 re-wrap bug);
  · attach nothing on the FIRST pass — window-open already did that;
  · "could not look" is never "nothing is up".
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
EXT = ROOT / "squad" / "vscode-squad-terminals"
HARNESS = ROOT / "tests" / "cockpit_harness.js"

_NODE = subprocess.run(["sh", "-c", "command -v node"],
                       capture_output=True, text=True).stdout.strip()

pytestmark = pytest.mark.skipif(
    not HARNESS.exists() or not (EXT / "extension.js").exists() or not _NODE,
    reason="cockpit extension or node not present",
)

SETTLE = 30000


def _passes(passes, settle=SETTLE):
    """Run a scripted sequence of polls through the real planner."""
    env = dict(os.environ,
               HARNESS_PASSES=json.dumps(passes),
               HARNESS_SETTLE_MS=str(settle))
    out = subprocess.run([_NODE, str(HARNESS), "autoattach"],
                         capture_output=True, text=True, timeout=60, env=env)
    assert out.returncode == 0, out.stderr or out.stdout
    return json.loads(out.stdout)["passes"]


TAB = [{"agent": "mindconnect-dev-vm-1"}]


def test_an_agent_ALREADY_UP_at_startup_is_never_auto_attached():
    """Window-open already ran `squad attach --no-start` for every tab.

    ⚠️ THIS TEST WAS VACUOUS AND HID A REAL BUG. It first asserted only that
    the FIRST pass returns nothing — which a first pass always does, because a
    newly-seen agent's stamp is `now`, so the settle check alone suppresses
    it. Deleting the whole seed branch left it green (mutation MA3,
    2026-08-09), and the code really did attach startup agents ~30s later:
    skipping the first PASS is not the same as skipping the agents that pass
    saw. The second pass below is what makes the distinction — settle has
    elapsed, so only the preexisting-set can be suppressing it.
    """
    a = "mindconnect-dev-vm-1"
    got = _passes([
        {"up": [a], "now": 0, "tabs": TAB},              # seed: already up
        {"up": [a], "now": SETTLE + 8000, "tabs": TAB},  # settle long gone
    ])
    assert got == [[], []], "attached an agent that was already up at startup"


def test_a_startup_agent_IS_attached_once_it_restarts():
    """The control for the rule above: 'leave startup agents alone' must not
    become 'ignore this agent forever'. Once it goes down and comes back, it
    is an ordinary transition."""
    a = "mindconnect-dev-vm-1"
    got = _passes([
        {"up": [a], "now": 0, "tabs": TAB},              # seed: already up
        {"up": [], "now": 50000, "tabs": TAB},           # stopped
        {"up": [a], "now": 60000, "tabs": TAB},          # started again
        {"up": [a], "now": 60000 + SETTLE, "tabs": TAB},
    ])
    assert got == [[], [], [], [a]]


def test_an_agent_woken_ELSEWHERE_is_attached_after_the_settle():
    """The whole point: nothing in this window started it."""
    got = _passes([
        {"up": [], "now": 0, "tabs": TAB},                       # seed: down
        {"up": ["mindconnect-dev-vm-1"], "now": 8000, "tabs": TAB},
        {"up": ["mindconnect-dev-vm-1"], "now": 8000 + SETTLE, "tabs": TAB},
    ])
    assert got == [[], [], ["mindconnect-dev-vm-1"]]


def test_it_does_NOT_attach_during_the_startup_dialog_dance():
    """🔴 tmux sizes a session to its SMALLEST attached client. Attaching
    mid-launch narrows the pane while squad greps it for dialogs and sends
    keystrokes — the 2026-07-25 bug where a 35-col client re-wrapped a dialog
    mid-word and blinded the match, looping dreamteam through 3 relaunches."""
    got = _passes([
        {"up": [], "now": 0, "tabs": TAB},
        {"up": ["mindconnect-dev-vm-1"], "now": 1000, "tabs": TAB},
        {"up": ["mindconnect-dev-vm-1"], "now": 1000 + SETTLE - 1, "tabs": TAB},
    ])
    assert got == [[], [], []], "attached before the dance could finish"


def test_it_attaches_ONCE_not_on_every_poll():
    """The detach-respecting rule. Without it, Ctrl-b d would be undone
    within one poll and the tab would be impossible to leave."""
    late = 1000 + SETTLE
    got = _passes([
        {"up": [], "now": 0, "tabs": TAB},
        {"up": ["mindconnect-dev-vm-1"], "now": 1000, "tabs": TAB},
        {"up": ["mindconnect-dev-vm-1"], "now": late, "tabs": TAB},
        {"up": ["mindconnect-dev-vm-1"], "now": late + 8000, "tabs": TAB},
        {"up": ["mindconnect-dev-vm-1"], "now": late + 16000, "tabs": TAB},
    ])
    assert got == [[], [], ["mindconnect-dev-vm-1"], [], []]


def test_a_RESTART_is_a_new_transition_and_attaches_again():
    """The control for the rule above: "once" must mean once per time the
    agent comes up, not once ever. A restarted agent is a new session with new
    scrollback, and its tab is stale."""
    a = "mindconnect-dev-vm-1"
    got = _passes([
        {"up": [], "now": 0, "tabs": TAB},
        {"up": [a], "now": 1000, "tabs": TAB},
        {"up": [a], "now": 1000 + SETTLE, "tabs": TAB},          # attach
        {"up": [], "now": 100000, "tabs": TAB},                  # stopped
        {"up": [a], "now": 200000, "tabs": TAB},                 # up again
        {"up": [a], "now": 200000 + SETTLE, "tabs": TAB},        # attach again
    ])
    assert got == [[], [], [a], [], [], [a]]


def test_an_agent_with_no_tab_in_this_window_is_ignored():
    """The roster is machine-wide; a workspace shows a subset. Attaching for
    an agent this window has no tab for is meaningless."""
    got = _passes([
        {"up": [], "now": 0, "tabs": TAB},
        {"up": ["some-other-agent"], "now": 1000, "tabs": TAB},
        {"up": ["some-other-agent"], "now": 1000 + SETTLE, "tabs": TAB},
    ])
    assert got == [[], [], []]


def test_a_tab_already_attached_for_this_up_is_not_attached_again():
    """The caller records the stamp it attached for; passing it back must
    suppress a repeat even on a fresh pass."""
    a = "mindconnect-dev-vm-1"
    got = _passes([
        {"up": [], "now": 0, "tabs": TAB},
        {"up": [a], "now": 1000, "tabs": TAB},
        # attachedStamp pinned to the up-stamp: the tab is already watching.
        {"up": [a], "now": 1000 + SETTLE,
         "tabs": [{"agent": a, "attachedStamp": 1000}], "noRecord": True},
    ])
    assert got == [[], [], []]


# ------------------------------------------------------------ the ls parser


def _parse(ls: str):
    out = subprocess.run([_NODE, str(HARNESS), "parseup"],
                         capture_output=True, text=True, timeout=60,
                         env=dict(os.environ, HARNESS_LS=ls))
    assert out.returncode == 0, out.stderr or out.stdout
    return set(json.loads(out.stdout)["up"])


def test_only_UP_rows_count_and_the_header_is_not_an_agent():
    """Real `squad ls` output, header and all. A parser that took the first
    column of every line would treat AGENT as an agent, and `down` rows as
    live ones."""
    ls = (
        "AGENT                      TMUX       HUB         \n"
        "mindconnect-iot2050-dev-vm-1 down       ·         \n"
        "mcp-hub-dev-vm-1           up         ⚡ online  (claude)\n"
        "weather-comp-dev-vm-1      down       ·         \n"
    )
    assert _parse(ls) == {"mcp-hub-dev-vm-1"}


def test_empty_output_yields_no_agents_rather_than_throwing():
    assert _parse("") == set()
