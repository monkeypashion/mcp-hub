"""Ctrl+P — every verb the board can run, reachable by typing.

The list is built by the app rather than by the provider, so it can be checked
without opening a palette. The property that matters is not "the command
exists" but "the command is only offered where it can actually be performed":
a palette entry that shells `squad answer` at a seat on another machine is a
button that lies, and the panel's whole rule is that it can never offer an edit
the underlying verb cannot make.
"""

from __future__ import annotations

import pytest
from textual.widgets import Tree

from mcp_hub.settings_app import SettingsApp, SquadCommands

AGENTS = [
    {"agent": "alpha-here", "worktree": "/code/alpha", "klass": "squad"},
    {"agent": "beta-here", "worktree": "/code/beta", "klass": "squad"},
]

FLEET = {
    "ts": 4_000_000_000.0,
    "agents": [
        {"name": "pm-dev-vm-1", "project": "org/pm", "wakeable": True,
         "idle": True, "sessions": 1, "next": ""},
    ],
}

WS_ROWS = [
    {"name": "feral", "machine": "here",
     "path": "/home/me/Projects/feral.code-workspace", "folders": 1,
     "error": "", "on_disk": True, "open_now": False, "registered": False,
     "squad": "", "listings": []},
    {"name": "remote-feral", "machine": "dev-vm-1",
     "path": "/home/me/Projects/remote-feral.code-workspace", "folders": 1,
     "error": "", "on_disk": True, "open_now": False, "registered": False,
     "squad": "", "listings": []},
]


def _snapshot(state="working"):
    return {
        "agents": {
            "alpha-here": {"agent": "alpha-here", "state": state, "hub": "⚡",
                           "model": "Opus", "ctx": "12%", "waiting_seconds": 90,
                           "question": "wipe it?", "action": "", "dirty": 0,
                           "unpushed": 0, "branch": "main", "usage_today": 0,
                           "usage_hour": 0, "wakeable": True, "next": None},
        },
        "order": ["alpha-here"],
        "counts": {"waiting": 0, "working": 1, "idle": 0, "down": 0, "hands": 0},
        "error": None,
    }


def _app(ran=None, state="working"):
    app = SettingsApp(
        AGENTS, scoped_to=None, model_for=lambda c: {"agent": "x", "sections": []},
        squad_bin="/usr/bin/SQUAD", hub_bin="/usr/bin/HUB",
        board_for=lambda: _snapshot(state), poll_seconds=3600,
        this_machine="here", fleet_for=lambda: FLEET,
        workspaces_for=lambda: {
            "hub_reachable": True, "note": "", "rows": WS_ROWS,
            "this_machine": "here", "machines": ["here", "dev-vm-1"],
        },
        now=lambda: 4_000_000_000.0,
    )
    if ran is not None:
        def apply(exe, argv, label, value):
            ran.append((exe, argv))
            app.call_from_thread(app._after_apply, f"{label} → {value}")
        app._apply = apply
    return app


async def _ready(app, pilot):
    for _ in range(40):
        if app.workspaces.get("rows"):
            await pilot.pause()
            await pilot.pause()
            return
        await pilot.pause()
    raise AssertionError("the workspace poll never delivered any rows")


async def _select(app, pilot, key: str):
    """Navigate the way the app itself does — remote branches are folded by
    default, so a raw move_cursor would silently land nowhere."""
    for node in app._all_nodes():
        if (node.data or {}).get("key") == key:
            app._move_to(node)
            await pilot.pause()
            await pilot.pause()
            return node
    raise AssertionError(f"no node keyed {key}: "
                         f"{[(n.data or {}).get('key') for n in app._all_nodes()]}")


def _titles(app):
    return [t for t, _help, _run in app.palette_commands()]


def _run_named(app, title):
    for t, _help, run in app.palette_commands():
        if t == title:
            return run()
    raise AssertionError(f"no palette command titled {title!r}: {_titles(app)}")


# ---- focus -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_focus_is_offered_for_the_selected_seat_and_names_it():
    app = _app()
    async with app.run_test(size=(120, 34)) as pilot:
        await _ready(app, pilot)
        await _select(app, pilot, "a:here/beta-here")
        titles = _titles(app)
        assert "Focus 1h — beta-here" in titles
        assert "Focus off — beta-here" in titles
        # …and not for the seat that merely happens to be first in the roster
        assert "Focus 1h — alpha-here" not in titles


@pytest.mark.asyncio
async def test_running_focus_shells_the_hub_binary_with_that_agent():
    ran: list = []
    app = _app(ran)
    async with app.run_test(size=(120, 34)) as pilot:
        await _ready(app, pilot)
        await _select(app, pilot, "a:here/beta-here")
        _run_named(app, "Focus 2h — beta-here")
        await pilot.pause()
        await pilot.pause()
    assert ran == [("/usr/bin/HUB",
                    ["focus", "120", "--agent", "beta-here"])], ran


@pytest.mark.asyncio
async def test_focus_off_sends_off_not_zero_minutes():
    ran: list = []
    app = _app(ran)
    async with app.run_test(size=(120, 34)) as pilot:
        await _ready(app, pilot)
        await _select(app, pilot, "a:here/alpha-here")
        _run_named(app, "Focus off — alpha-here")
        await pilot.pause()
        await pilot.pause()
    assert ran == [("/usr/bin/HUB",
                    ["focus", "--off", "--agent", "alpha-here"])], ran


@pytest.mark.asyncio
async def test_a_remote_seat_can_be_focused_but_not_driven():
    """Focus is a hub fact and works by name from anywhere. `answer`,
    `restart`, `stop` and `start` are tmux on THIS box, so offering them for
    another machine's seat would be an action that cannot happen."""
    app = _app(state="waiting")
    async with app.run_test(size=(120, 34)) as pilot:
        await _ready(app, pilot)
        await _select(app, pilot, "a:dev-vm-1/pm-dev-vm-1")
        titles = _titles(app)
        assert "Focus 1h — pm-dev-vm-1" in titles
        for forbidden in ("Answer yes — pm-dev-vm-1",
                          "Restart agent — pm-dev-vm-1",
                          "Stop agent — pm-dev-vm-1"):
            assert forbidden not in titles, forbidden


# ---- answer ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_answer_is_offered_only_while_the_agent_is_waiting():
    working = _app(state="working")
    async with working.run_test(size=(120, 34)) as pilot:
        await _ready(working, pilot)
        await _select(working, pilot, "a:here/alpha-here")
        assert "Answer yes — alpha-here" not in _titles(working)

    waiting = _app(state="waiting")
    async with waiting.run_test(size=(120, 34)) as pilot:
        await _ready(waiting, pilot)
        await _select(waiting, pilot, "a:here/alpha-here")
        titles = _titles(waiting)
        assert "Answer yes — alpha-here" in titles
        assert "Answer always — alpha-here" in titles


@pytest.mark.asyncio
async def test_answer_runs_the_fail_closed_squad_verb():
    ran: list = []
    app = _app(ran, state="waiting")
    async with app.run_test(size=(120, 34)) as pilot:
        await _ready(app, pilot)
        await _select(app, pilot, "a:here/alpha-here")
        _run_named(app, "Answer no — alpha-here")
        await pilot.pause()
        await pilot.pause()
    assert ran == [("/usr/bin/SQUAD", ["answer", "alpha-here", "no"])], ran


@pytest.mark.asyncio
async def test_restart_goes_to_squad_with_the_selected_agent():
    ran: list = []
    app = _app(ran)
    async with app.run_test(size=(120, 34)) as pilot:
        await _ready(app, pilot)
        await _select(app, pilot, "a:here/beta-here")
        _run_named(app, "Restart agent — beta-here")
        await pilot.pause()
        await pilot.pause()
    assert ran == [("/usr/bin/SQUAD", ["restart", "beta-here"])], ran


# ---- workspaces ------------------------------------------------------------

@pytest.mark.asyncio
async def test_registering_is_offered_for_a_local_feral_workspace_only():
    """Registering another machine's file would name a path this box cannot
    verify, so the offer is withheld there."""
    app = _app()
    async with app.run_test(size=(120, 34)) as pilot:
        await _ready(app, pilot)
        await _select(app, pilot, "w:here/feral")
        assert "Register workspace — feral" in _titles(app)
        await _select(app, pilot, "w:dev-vm-1/remote-feral")
        assert "Register workspace — remote-feral" not in _titles(app)


@pytest.mark.asyncio
async def test_registering_shells_the_hub_verb_with_the_files_path():
    ran: list = []
    app = _app(ran)
    async with app.run_test(size=(120, 34)) as pilot:
        await _ready(app, pilot)
        await _select(app, pilot, "w:here/feral")
        _run_named(app, "Register workspace — feral")
        await pilot.pause()
        await pilot.pause()
    assert ran == [("/usr/bin/HUB", [
        "workspaces", "register", "/home/me/Projects/feral.code-workspace"])], ran


# ---- navigation ------------------------------------------------------------

@pytest.mark.asyncio
async def test_go_to_moves_the_cursor_to_that_seat():
    app = _app()
    async with app.run_test(size=(120, 34)) as pilot:
        await _ready(app, pilot)
        await _select(app, pilot, "a:here/alpha-here")
        _run_named(app, "Go to pm-dev-vm-1")
        await pilot.pause()
        await pilot.pause()
        assert app.selected["agent"] == "pm-dev-vm-1"
        # …including opening the remote machine node, which is folded by
        # default — a jump that lands on a hidden row moves nothing.
        assert app.query_one("#fleet", Tree).cursor_node.data["agent"] \
            == "pm-dev-vm-1"


@pytest.mark.asyncio
async def test_every_seat_and_workspace_is_reachable_by_name():
    app = _app()
    async with app.run_test(size=(120, 34)) as pilot:
        await _ready(app, pilot)
        titles = _titles(app)
        for agent in ("alpha-here", "beta-here", "pm-dev-vm-1"):
            assert f"Go to {agent}" in titles
        for ws in ("feral", "remote-feral"):
            assert f"Go to workspace {ws}" in titles


# ---- the provider itself ---------------------------------------------------

@pytest.mark.asyncio
async def test_the_provider_serves_the_apps_list_not_its_own():
    """Two lists would drift, and the palette's would be the one nobody
    notices is stale."""
    app = _app()
    async with app.run_test(size=(120, 34)) as pilot:
        await _ready(app, pilot)
        await _select(app, pilot, "a:here/alpha-here")
        provider = SquadCommands(app.screen)
        hits = [h async for h in provider.search("focus")]
        assert hits, "typing `focus` found nothing"
        found = {str(h.match_display) for h in hits}
        assert any("Focus 1h — alpha-here" in f for f in found), found
        # Discovery shows the same commands, not a hand-picked second list.
        discovered = [d async for d in provider.discover()]
        assert {str(d.display) for d in discovered} <= set(_titles(app))


@pytest.mark.asyncio
async def test_a_search_that_matches_nothing_yields_nothing():
    app = _app()
    async with app.run_test(size=(120, 34)) as pilot:
        await _ready(app, pilot)
        provider = SquadCommands(app.screen)
        assert [h async for h in provider.search("zzzznotacommand")] == []
