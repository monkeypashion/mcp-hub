"""The settings panel, driven the way the operator drives it: by clicking.

Every previous presentation was "tested" by rendering it and looking, which is
how six of them shipped defects the first click found. Textual's Pilot sends
real clicks and key presses through the real app, so these fail for the same
reasons the operator's session fails.

The left panel is now a tree, so navigation here is arrow keys through the real
widget rather than clicks on list rows — still real input, still the same app.
"""
from __future__ import annotations

import asyncio
import time

import pytest
from textual.widgets import Select, Tree

from mcp_hub.settings_app import SettingsApp

WITH_SETTINGS = {
    "agent": "real",
    "sections": [
        {"title": "SQUADS", "note": "who hears it", "rows": [
            {"label": "dreamteam", "value": "hearing", "source": "set on this agent",
             "edit": {"choices": ["hearing", "muted"], "bin": "mcp-hub",
                      "applies": "immediately",
                      "argv": ["mute", "--agent", "real", "--squad", "dreamteam",
                               "--state", "{}"]}}]},
        {"title": "LAUNCH", "note": "", "rows": [
            {"label": "Comms", "value": "off", "source": "set on this agent",
             "edit": {"choices": ["on", "off"], "bin": "squad",
                      "argv": ["comms", "{}", "real"], "applies": "next restart"}},
            {"label": "Worktree", "value": "/real", "source": "roster"}]},
    ],
}

# Two agents with NO settings, adjacent. `squad add-folder` enrols plain
# directories deliberately, so this is the ordinary case, not a contrivance.
AGENTS = [
    {"agent": "scratch-one", "worktree": "/scratch/one", "klass": "faculty"},
    {"agent": "scratch-two", "worktree": "/scratch/two", "klass": "faculty"},
    {"agent": "real", "worktree": "/real", "klass": "squad"},
    {"agent": "real-two", "worktree": "/real2", "klass": "squad"},
]


def _model_for(cwd):
    if cwd.startswith("/scratch"):
        return None
    return {**WITH_SETTINGS, "agent": cwd.strip("/")}


def _app(ran=None, fail=False):
    app = SettingsApp(AGENTS, scoped_to="/home/me/team.code-workspace",
                      # DISTINCT paths: with both set to the same binary no
                      # assertion here can tell which one the model asked for,
                      # and a mutant hardcoding one of them passes.
                      model_for=_model_for, squad_bin="/usr/bin/SQUAD",
                      hub_bin="/usr/bin/HUB", this_machine="thisbox")
    if ran is not None:
        def apply(exe, argv, label, value):
            ran.append((exe, argv))
            app.call_from_thread(app._after_apply,
                                 "failed" if fail else f"{label} → {value}")
        app._apply = apply
    return app


async def _goto(app, pilot, agent: str) -> None:
    """Move the cursor onto one seat through the real widget."""
    for node in app._agent_nodes():
        if (node.data or {}).get("agent") == agent:
            app.query_one("#fleet", Tree).move_cursor(node)
            await pilot.pause()
            await pilot.pause()
            return
    raise AssertionError(f"{agent} is not in the tree")


def _detail_text(app) -> str:
    pane = app.query_one("#detail")
    return " ".join(str(w.render()) for w in pane.walk_children())


# How long "nothing happened" has to be observed before it counts. Only the
# NEGATIVE assertions need this — see the note there for why a number is
# defensible in that one place and nowhere else.
_SETTLE = 25

# Positive waits are WALL-CLOCK, not frame counts. `pilot.pause()` on an idle
# message pump returns in well under a millisecond, so `for _ in range(200)`
# is ~0.1s of real time — while the thing being waited on is a WORKER THREAD,
# which this box (ambient load ~42: 16 tmux agents, seat containers, rclone)
# can starve for whole seconds. Frames are cheapest exactly when the box is
# busiest, so a frame budget SHRINKS under the load that needs it most:
# measured 2026-08-12, file-scope runs failed 2/3 on this box — identically
# on a tree predating that day's changes, so the frame-count gate, not any
# code change, was the defect. The sleep in each turn of the loop is what
# actually spends time; the pause keeps the pump serviced meanwhile.
_WAIT_WALL_SECONDS = 30.0


async def _ready(pilot, app, timeout: float = _WAIT_WALL_SECONDS) -> None:
    """Wait until the controls are POPULATED — not until a frame has elapsed.

    🔴 One `await pilot.pause()` after `run_test()` is a fixed frame count
    standing in for a condition: **the same substitution `_ran` exists to
    remove, one step earlier, in the setup every test here shares.** A `Select`
    is constructed with `value=` and `allow_blank=False`, but the reactive is
    only applied once the widget MOUNTS — so under load `.value` still reads
    `Select.NULL` and the test either compares NULL against a real value or
    assigns to a control that is not ready. No wait placed *after* that point
    can repair it.

    Found by mcp-hub-dev-vm-1-general 2026-08-08: **3 failures in 20 runs** on a
    box whose *ambient* load average is 42 (16 tmux agents, four seat
    containers, rclone, squad heal). It does NOT reproduce on an 8-core box
    under 8 CPU burners (0/25 measured), so this is not a bound anyone can tune
    by watching it pass locally — hence a condition rather than a bigger number.

    ⭐ **The old single pause had ZERO margin, measured.** Instrumented on an
    idle 8-core box, this loop consumes **exactly 1 pause, 15 runs out of 15** —
    the condition is unmet on entry and met after one. So `await pilot.pause()`
    was not comfortably sufficient, it was *exactly* sufficient, and any load
    that pushes the requirement to 2 breaks it. That is why the failure looks
    machine-specific rather than rare: it isn't a long tail, it's a boundary.

    ⚠️ **Not a product defect.** The panel would show a blank control for a
    frame, and `_on_select_changed` refuses `Select.NULL` anyway because it is
    never in the row's `choices`.

    ⚠️ **This fix cannot be mutation-verified here.** Reverting the loop to a
    single pause still passes 8/8 locally, because the box that reproduces the
    failure is not this one. The local evidence is the zero-margin measurement
    above, not a red test — say so rather than implying it was proven here.

    Compares against `Select.NULL` by identity: in textual 8.2.8 `Select.BLANK`
    is an unrelated plain `False`, so both `str(v) == "Select.NULL"` and
    `Select.BLANK` would be wrong here in different ways.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        sels = app.query("Select")
        if sels and not any(s.value is Select.NULL for s in sels):
            return
        await pilot.pause()
        await asyncio.sleep(0.02)
    raise AssertionError(
        "controls never populated — every Select still reads Select.NULL")


async def _ran(pilot, ran, timeout: float = _WAIT_WALL_SECONDS) -> list:
    """Wait for the command to be RECORDED, not for a fixed number of frames.

    ⚠️ `_apply` is dispatched with `run_worker(thread=True)`, so the append
    happens on a WORKER THREAD. Two `await pilot.pause()` calls — the shape
    these tests used to have — are a SCHEDULE standing in for a CONDITION:
    they pass on an idle box and can lose the race when the whole suite is
    loading the machine, which is how this file produced a test that passed
    in isolation and failed in the suite (mcp-hub-dev-vm-1-general, 2026-08-08).

    Not claimed as a proven cure: that failure has not been reproduced here
    (40 targeted runs under load and a full green suite). It is the weakness
    that was actually demonstrable by reading, fixed on its own merits.

    2026-08-12: the condition loop alone was NOT the cure — bounded by a
    FRAME count it still lost the race 2/3 file-scope runs on this box (and
    identically on a pre-change tree). See _WAIT_WALL_SECONDS: the bound is
    now wall-clock, and each turn of the loop spends real time.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ran:
            return ran
        await pilot.pause()
        await asyncio.sleep(0.02)
    return ran


@pytest.mark.asyncio
async def test_walking_the_whole_tree_does_not_crash():
    """THE crash the operator hit: moving from one no-settings agent to another
    mounted a second widget with the same id, and DuplicateIds took the whole
    app down. remove_children() is asynchronous — the mounts were racing it.
    """
    app = _app()
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        for _ in range(12):                      # to the very top
            await pilot.press("up")
            await pilot.pause()
        for _ in range(12):                      # and all the way back down
            await pilot.press("down")
            await pilot.pause()
        assert app.selected["agent"] == "real-two", app.selected


@pytest.mark.asyncio
async def test_widget_ids_are_never_reused_between_renders():
    """Awaiting remove_children() is the fix; this is the belt to that braces.

    Two populated agents both mount `sel-*` controls, so ANY id scheme fixed by
    position collides the moment a removal lags a mount — the same
    DuplicateIds crash wearing different ids. Asserted as a property rather
    than by trying to provoke the race, because the race is timing-dependent
    and a test that only sometimes reproduces it is not a test.
    """
    app = _app()
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        seen: set[str] = set()
        for _ in range(3):
            for name in ("real", "real-two"):
                await _goto(app, pilot, name)
                await _ready(pilot, app)
                ids = {s.id for s in app.query("Select")}
                assert ids, "controls vanished after switching agents"
                assert not (ids & seen), f"id reused across renders: {ids & seen}"
                seen |= ids


@pytest.mark.asyncio
async def test_it_opens_on_an_agent_that_has_settings():
    """Roster order puts two identity-less scratch folders first. Opening on
    row 0 showed an empty panel that read as a broken feature."""
    app = _app()
    async with app.run_test(size=(110, 30)) as pilot:
        await _ready(pilot, app)
        assert app.selected["agent"] == "real", app.selected
        assert app.query("Select"), "opened on an agent with nothing to change"


@pytest.mark.asyncio
async def test_a_folder_with_no_identity_explains_itself():
    app = _app()
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        await _goto(app, pilot, "scratch-one")
        # Assert on the RENDERED pane, not a widget internal: what matters is
        # that the operator can read it.
        assert "no hub identity" in _detail_text(app).lower()
        assert not app.query("Select"), "offered controls for a folder with none"


@pytest.mark.asyncio
async def test_changing_a_dropdown_runs_the_command_the_model_named():
    """The whole point, and the thing a screenshot cannot show. The cockpit does
    not know what a launch flag is — the model says which binary and argv."""
    ran: list = []
    app = _app(ran)
    async with app.run_test(size=(110, 30)) as pilot:
        await _ready(pilot, app)
        sel = app.query("Select").last()      # Comms, currently "off"
        sel.value = "on"
        await _ran(pilot, ran)
    assert ran == [("/usr/bin/SQUAD", ["comms", "on", "real"])], ran


@pytest.mark.asyncio
async def test_a_mute_goes_to_the_hub_binary_not_squad():
    ran: list = []
    app = _app(ran)
    async with app.run_test(size=(110, 30)) as pilot:
        await _ready(pilot, app)
        sel = app.query("Select").first()     # the dreamteam squad row
        sel.value = "muted"
        await _ran(pilot, ran)
    assert ran and ran[0][0] == "/usr/bin/HUB", ran
    assert ran[0][1][0] == "mute" and ran[0][1][-1] == "muted", ran


@pytest.mark.asyncio
async def test_selecting_the_value_already_set_runs_nothing():
    """Not a safety property — every verb is idempotent. It is about not
    reporting a change that did not happen.

    The second half is the instrument proving ITSELF. An empty `ran` only
    means "nothing ran" if this test would have SEEN something that did, and
    what it is waiting on is a worker thread — so a slow enough box passes the
    first assertion for entirely the wrong reason. Changing the same widget to
    a real value afterwards shows the window was wide enough to catch one.
    """
    ran: list = []
    app = _app(ran)
    async with app.run_test(size=(110, 30)) as pilot:
        await _ready(pilot, app)
        sel = app.query("Select").last()
        sel.value = "off"                     # already off
        for _ in range(_SETTLE):
            await pilot.pause()
        assert ran == [], ran
        sel.value = "on"                      # a real change, same widget
        assert await _ran(pilot, ran), (
            "the settle above could not have seen a command anyway")


@pytest.mark.asyncio
async def test_every_editable_row_shows_its_current_value_as_selected():
    """A control that opens blank has stopped reporting the setting. This is
    what caught the two-vocabulary bug: values said "hearing it" while the
    choices said "hear", so nothing could ever be selected."""
    app = _app()
    async with app.run_test(size=(110, 30)) as pilot:
        await _ready(pilot, app)
        values = sorted(str(s.value) for s in app.query("Select"))
        assert values == ["hearing", "off"], values
