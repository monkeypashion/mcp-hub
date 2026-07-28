"""The settings TUI's state machine — navigation, selection and what a keypress
means — tested without a terminal.

Everything here is deliberately reachable with no curses: a TUI whose logic is
entangled with its drawing is one that gets "tested" by looking at it, which is
how the four preceding presentations each shipped something that only looked
right. The curses layer draws what these objects say and reads keys into them;
it decides nothing.
"""
from __future__ import annotations

import json

import pytest

from mcp_hub import cli

SECTIONS = [
    # IDENTITY FIRST and entirely read-only, exactly as production emits it.
    # Without it "first selectable" and "first editable" coincide and no test
    # here can tell them apart — which is how the cursor came to open on a wall
    # of derived fields and the panel looked unable to edit anything.
    {"title": "IDENTITY", "note": "", "rows": [
        {"label": "Name", "value": "a1", "source": "derived from repo + hostname"},
        {"label": "Project", "value": "acme/a1", "source": "derived from git remote"}]},
    {"title": "SQUADS", "note": "who hears its broadcasts", "rows": [
        {"label": "dreamteam", "value": "hearing it", "source": "set on this agent",
         "edit": {"kind": "mute", "choices": ["hear", "mute"], "bin": "mcp-hub",
                  "argv": ["mute", "--agent", "a1", "--squad", "dreamteam",
                           "--state", "{}"], "applies": "immediately"}}]},
    {"title": "LAUNCH", "note": "", "rows": [
        {"label": "Comms (hub wake)", "value": "off", "source": "set on this agent",
         "edit": {"kind": "comms", "choices": ["on", "off"], "bin": "squad",
                  "argv": ["comms", "{}", "a1"], "applies": "next restart"}},
        {"label": "Worktree", "value": "/tmp/a1", "source": "roster"}]},
]


@pytest.fixture
def tui(monkeypatch):
    agents = [{"agent": "a1", "worktree": "/tmp/a1", "args": "", "klass": "squad"},
              {"agent": "a2", "worktree": "/tmp/a2", "args": "", "klass": "faculty"}]
    monkeypatch.setattr(cli, "_settings_model",
                        lambda cwd: {"agent": cwd.rsplit("/", 1)[-1],
                                     "sections": SECTIONS})
    return cli.SettingsTui(agents, scoped_to="/home/me/Projects/team.code-workspace")


# ---- navigation -----------------------------------------------------------

def test_the_cursor_never_lands_on_a_section_heading(tui):
    """A cursor that can sit on a heading is a cursor that appears stuck: the
    key repeats and nothing seems to move, because the row it selected has
    nothing to show or do."""
    seen = set()
    for _ in range(20):
        tui.move_row(1)
        seen.add(tui.row_ix)
    headers = {i for i, r in enumerate(tui.rows()) if "header" in r}
    assert not (seen & headers), f"landed on heading row(s) {seen & headers}"


def test_moving_down_past_the_end_stops_rather_than_wrapping(tui):
    """Wrapping in a two-pane list throws the eye to the far end of the screen
    for what felt like one step down."""
    for _ in range(50):
        tui.move_row(1)
    last = tui.selectable()[-1]
    assert tui.row_ix == last
    tui.move_row(1)
    assert tui.row_ix == last


def test_moving_up_past_the_start_stops(tui):
    tui.move_row(-50)
    assert tui.row_ix == tui.selectable()[0]


def test_changing_agent_reloads_and_resets_the_row(tui):
    """The rows belong to the agent. Keeping the index across a switch points
    the cursor at whatever happens to sit there in the NEW agent's list — which
    is how an edit lands on a setting the operator never selected."""
    tui.move_row(1)
    tui.move_row(1)
    moved = tui.row_ix
    tui.move_agent(1)
    assert tui.agent_ix == 1
    assert tui.model["agent"] == "a2"
    # back to the first EDITABLE row — not row 0 (a heading), and not the
    # first selectable one either, which in production is a derived field
    assert tui.row_ix == tui.first_editable()
    assert tui.row_ix != moved


def test_agent_selection_clamps_at_both_ends(tui):
    tui.move_agent(-5)
    assert tui.agent_ix == 0
    tui.move_agent(50)
    assert tui.agent_ix == len(tui.agents) - 1


def test_clicking_an_agent_selects_it(tui):
    """Mouse support is the whole reason this shape was chosen over a list —
    "see the agents, click on them"."""
    tui.select_agent(1)
    assert tui.agent_ix == 1 and tui.model["agent"] == "a2"


def test_clicking_a_heading_selects_nothing(tui):
    headers = [i for i, r in enumerate(tui.rows()) if "header" in r]
    before = tui.row_ix
    tui.select_row(headers[0])
    assert tui.row_ix == before


# ---- editing --------------------------------------------------------------

def _row_index(tui, label):
    return next(i for i, r in enumerate(tui.rows()) if r.get("label") == label)


def test_a_read_only_row_offers_no_choices(tui):
    tui.select_row(_row_index(tui, "Worktree"))
    assert tui.choices() == []
    assert tui.command_for("anything") is None


def test_an_editable_row_offers_its_choices_and_builds_the_command(tui):
    tui.select_row(_row_index(tui, "Comms (hub wake)"))
    assert tui.choices() == ["on", "off"]
    assert tui.command_for("on") == ("squad", ["comms", "on", "a1"])


def test_the_value_already_set_builds_no_command(tui):
    """Every verb here is idempotent, so this is not about safety — it is about
    not reporting a change that did not happen."""
    tui.select_row(_row_index(tui, "Comms (hub wake)"))
    assert tui.command_for("off") is None


def test_a_value_that_was_never_offered_is_refused(tui):
    """The choices ARE the contract. Accepting anything else would let a
    mistyped or stale value reach a command that writes the roster."""
    tui.select_row(_row_index(tui, "Comms (hub wake)"))
    assert tui.command_for("maybe") is None


def test_the_mute_row_targets_the_hub_not_squad(tui):
    """Two binaries, and the model says which. Sending a mute to `squad` would
    fail confusingly rather than obviously."""
    tui.select_row(_row_index(tui, "dreamteam"))
    binary, argv = tui.command_for("mute")
    assert binary == "mcp-hub"
    assert argv == ["mute", "--agent", "a1", "--squad", "dreamteam",
                    "--state", "mute"]


# ---- scope ----------------------------------------------------------------

def test_the_header_names_the_workspace_it_is_scoped_to(tui):
    assert "team.code-workspace" in tui.header()
    assert "2 agent(s)" in tui.header()


def test_an_unscoped_run_says_so_rather_than_looking_scoped(monkeypatch):
    """A whole-machine list that LOOKS workspace-scoped is worse than either:
    the operator reads five agents as "my workspace" when it is really the box."""
    monkeypatch.setattr(cli, "_settings_model", lambda cwd: {"agent": "x",
                                                            "sections": SECTIONS})
    t = cli.SettingsTui([{"agent": "a1", "worktree": "/tmp/a1"}], scoped_to=None)
    assert "no workspace open" in t.header()


def test_no_agents_at_all_does_not_crash(monkeypatch):
    monkeypatch.setattr(cli, "_settings_model", lambda cwd: None)
    t = cli.SettingsTui([], scoped_to=None)
    assert t.rows() == [] and t.choices() == []
    t.move_agent(1)
    t.move_row(1)          # must not raise on an empty panel
    assert t.current() is None


def test_an_unreadable_agent_reports_instead_of_taking_the_panel_down(monkeypatch):
    """This runs inside a redraw loop. An exception over one un-derivable agent
    would kill the whole view rather than that row."""
    def boom(cwd):
        raise RuntimeError("no git remote")

    monkeypatch.setattr(cli, "_settings_model", boom)
    t = cli.SettingsTui([{"agent": "a1", "worktree": "/tmp/a1"}], scoped_to=None)
    assert t.model is None
    assert "no git remote" in t.status
    assert t.rows() == []


# ---- workspace scoping ----------------------------------------------------

def test_agents_are_scoped_by_folder_membership(tmp_path, monkeypatch):
    """The SAME rule squad's ws_agents() and the cockpit's tab list use. A third
    spelling is how teardown and the tab list come to disagree about what they
    are acting on."""
    conf = tmp_path / "squad.conf"
    conf.write_text(
        f"a1|{tmp_path}/one||--continue|squad\n"
        f"a2|{tmp_path}/two||--continue|faculty\n", encoding="utf-8")
    monkeypatch.setattr(cli, "SQUAD_CONF", conf)
    ws = tmp_path / "team.code-workspace"
    ws.write_text(json.dumps({"folders": [{"path": str(tmp_path / "one")}]}),
                  encoding="utf-8")
    (tmp_path / "one").mkdir()
    (tmp_path / "two").mkdir()

    scoped = cli._agents_in_workspace(str(ws))
    assert [r["agent"] for r in scoped] == ["a1"]


def test_no_workspace_means_the_whole_roster(tmp_path, monkeypatch):
    conf = tmp_path / "squad.conf"
    conf.write_text(f"a1|{tmp_path}/one|||\na2|{tmp_path}/two|||\n", encoding="utf-8")
    monkeypatch.setattr(cli, "SQUAD_CONF", conf)
    assert [r["agent"] for r in cli._agents_in_workspace(None)] == ["a1", "a2"]


def test_a_workspace_listing_no_roster_folder_scopes_to_nothing(tmp_path, monkeypatch):
    """Empty is the honest answer. Falling back to the whole roster HERE would
    silently widen the panel to the machine — the opposite of what the operator
    asked for — and look identical to a correctly scoped one."""
    conf = tmp_path / "squad.conf"
    conf.write_text(f"a1|{tmp_path}/one|||\n", encoding="utf-8")
    monkeypatch.setattr(cli, "SQUAD_CONF", conf)
    ws = tmp_path / "empty.code-workspace"
    ws.write_text(json.dumps({"folders": [{"path": str(tmp_path / "nowhere")}]}),
                  encoding="utf-8")
    assert cli._agents_in_workspace(str(ws)) == []


def test_the_roster_order_is_preserved(tmp_path, monkeypatch):
    """File order is the order squad lists agents and the order the tabs appear.
    Re-sorting here would disagree with both for no reason."""
    conf = tmp_path / "squad.conf"
    conf.write_text(f"zeta|{tmp_path}/z|||\nalpha|{tmp_path}/a|||\n", encoding="utf-8")
    monkeypatch.setattr(cli, "SQUAD_CONF", conf)
    assert [r["agent"] for r in cli._roster_all()] == ["zeta", "alpha"]


# ---- reachability: the two defects the operator hit ------------------------

def test_the_cursor_opens_on_an_EDITABLE_row(tui):
    """IDENTITY comes first and every row in it is derived, so "first
    selectable" opened the panel on a wall of read-only fields: press enter,
    get "read-only", conclude the panel cannot edit anything. Which is exactly
    what happened — "I am not able to update the settings... how do I do an
    edit?" (2026-07-28)."""
    assert tui.current().get("edit"), tui.current()
    assert tui.choices(), "opened on a row with nothing to choose"


def test_an_agent_with_nothing_editable_still_opens_somewhere_sensible(monkeypatch):
    monkeypatch.setattr(cli, "_settings_model", lambda cwd: {"agent": "x", "sections": [
        {"title": "IDENTITY", "note": "", "rows": [
            {"label": "Name", "value": "x", "source": "derived"}]}]})
    t = cli.SettingsTui([{"agent": "x", "worktree": "/tmp/x"}], scoped_to=None)
    assert t.current()["label"] == "Name"       # first selectable, not row 0
    assert t.choices() == []


def test_rows_below_the_fold_scroll_into_view(tui):
    """Without a viewport the rows past the screen are not merely unseen, they
    are UNREACHABLE: the cursor moves onto them and disappears. In a terminal
    panel that is most of the panel."""
    height = 3
    for _ in range(20):
        tui.move_row(1)
    first, last = tui.visible(height)
    assert first <= tui.row_ix < last, \
        f"cursor {tui.row_ix} outside the drawn window {first}:{last}"


def test_the_viewport_never_runs_past_either_end(tui):
    total = len(tui.rows())
    for _ in range(30):
        tui.move_row(-1)
    first, last = tui.visible(3)
    assert first == 0 and last <= total
    for _ in range(30):
        tui.move_row(1)
    first, last = tui.visible(3)
    assert last == total and first >= 0


def test_a_pane_taller_than_the_list_shows_all_of_it(tui):
    assert tui.visible(500) == (0, len(tui.rows()))


def test_a_zero_height_pane_asks_for_nothing(tui):
    assert tui.visible(0) == (0, 0)


def test_it_opens_on_an_agent_that_HAS_settings(monkeypatch):
    """`squad add-folder` enrols plain directories on purpose, and those have no
    derived identity — so no settings at all. Four of the six agents in the
    operator's own workspace are exactly that, and opening on the first ROSTER
    row showed a blank panel that looked like a broken feature.
    """
    def model(cwd):
        return None if "scratch" in cwd else {"agent": "real", "sections": SECTIONS}

    monkeypatch.setattr(cli, "_settings_model", model)
    t = cli.SettingsTui([{"agent": "s1", "worktree": "/tmp/scratch/a"},
                         {"agent": "s2", "worktree": "/tmp/scratch/b"},
                         {"agent": "real", "worktree": "/tmp/real"}], scoped_to=None)
    assert t.agent_ix == 2, "opened on an agent with nothing to show"
    assert t.model is not None


def test_an_agent_with_no_identity_says_why_rather_than_showing_nothing(monkeypatch):
    """An empty pane reads as broken. This is not a failure — it is a folder
    that deliberately has no hub identity — so it has to say so."""
    monkeypatch.setattr(cli, "_settings_model", lambda cwd: None)
    t = cli.SettingsTui([{"agent": "s1", "worktree": "/tmp/scratch/a"}], scoped_to=None)
    assert t.rows() == []
    assert "no hub identity" in t.reason
    assert "add-folder" in t.reason, "does not name the thing that created it"


def test_the_reason_clears_when_moving_to_an_agent_that_has_settings(monkeypatch):
    """Stale explanatory text under a populated panel is worse than none."""
    def model(cwd):
        return None if "scratch" in cwd else {"agent": "real", "sections": SECTIONS}

    monkeypatch.setattr(cli, "_settings_model", model)
    t = cli.SettingsTui([{"agent": "real", "worktree": "/tmp/real"},
                         {"agent": "s1", "worktree": "/tmp/scratch/a"}], scoped_to=None)
    t.move_agent(1)
    assert t.reason
    t.move_agent(-1)
    assert not t.reason, "kept the no-identity note over an agent that has one"
