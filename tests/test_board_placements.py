"""Driving another machine's seat from this board.

The payoff of the seat/placement split: a placement is a HUB write, so the
board can start or stop a seat on a box it is not sitting at. What it must
never do is imply that anything has happened. `pending-edge` means no edge pass
has reported since the write — and until a machine's `mcp-hub edge apply` runs
on a timer, that is its permanent state.

So the properties here are about the gap between asked and observed staying
visible: pending and diverged wear the drift colour, they say which machine to
go and check, and a seat with no placement says it is not scheduled rather than
looking merely idle.
"""

from __future__ import annotations

import pytest

from mcp_hub.fleet_tree import build_tree
from mcp_hub.settings_app import SettingsApp

NOW = 4_000_000_000.0

AGENTS = [{"agent": "alpha-here", "worktree": "/code/alpha", "klass": "squad"}]

FLEET = {"ts": NOW, "agents": [
    {"name": "pm-dev-vm-1", "project": "org/pm", "wakeable": True,
     "idle": True, "sessions": 1, "next": ""},
]}

WS_ROWS = [
    {"name": "team", "machine": "here",
     "path": "/home/me/Projects/team.code-workspace", "folders": 1,
     "error": "", "on_disk": True, "open_now": False, "registered": True,
     "squad": "", "listings": []},
]


def _placement(seat, machine, desired="running", status="converged",
               observed="running", pid="pl-1"):
    return {"id": pid, "seat": seat, "machine": machine, "substrate": "worktree",
            "desired": desired, "observed": {"state": observed, "at": NOW,
                                             "enumeration": {}},
            "status": status}


def _app(ran=None, placements=()):
    app = SettingsApp(
        AGENTS, scoped_to=None, model_for=lambda c: {"agent": "x", "sections": []},
        squad_bin="/usr/bin/SQUAD", hub_bin="/usr/bin/HUB",
        board_for=lambda: {"agents": {}, "counts": {}, "error": None},
        poll_seconds=3600, this_machine="here", fleet_for=lambda: FLEET,
        workspaces_for=lambda: {
            "hub_reachable": True, "note": "", "rows": WS_ROWS,
            "this_machine": "here", "machines": ["here", "dev-vm-1"]},
        placements_for=lambda: list(placements),
        now=lambda: NOW,
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


async def _select(app, pilot, key):
    for node in app._all_nodes():
        if (node.data or {}).get("key") == key:
            app._move_to(node)
            await pilot.pause()
            await pilot.pause()
            return node
    raise AssertionError(f"no node keyed {key}")


def _label(app, agent):
    for n in app._agent_nodes():
        if (n.data or {}).get("agent") == agent:
            return n.label.plain
    raise AssertionError(f"{agent} not in the tree")


def _node(app, agent):
    for n in app._agent_nodes():
        if (n.data or {}).get("agent") == agent:
            return n
    raise AssertionError(f"{agent} not in the tree")


def _titles(app):
    return [t for t, _h, _r in app.palette_commands()]


def _run_named(app, title):
    for t, _h, run in app.palette_commands():
        if t == title:
            return run()
    raise AssertionError(f"no command {title!r}: {_titles(app)}")


def _detail(app):
    return " ".join(str(w.render())
                    for w in app.query_one("#detail").walk_children())


# ---- the join --------------------------------------------------------------

def test_a_placement_attaches_to_its_seat_by_name():
    """Seat identity IS the agent name — the hub assigns `<repo>-<machine>`,
    the same rule derived identity uses — so no second mapping exists to drift
    out of step."""
    tree = build_tree(
        roster=[], board={"agents": {}},
        workspaces={"rows": [], "machines": ["dev-vm-1"], "this_machine": "here"},
        fleet=FLEET, this_machine="here",
        placements=[_placement("pm-dev-vm-1", "dev-vm-1")], now=NOW)
    seat = [m for m in tree["machines"] if m["machine"] == "dev-vm-1"][0]["loose"][0]
    assert seat["placement"]["id"] == "pl-1"


def test_a_seat_with_no_placement_carries_none_rather_than_a_blank():
    tree = build_tree(
        roster=[], board={"agents": {}},
        workspaces={"rows": [], "machines": ["dev-vm-1"], "this_machine": "here"},
        fleet=FLEET, this_machine="here", placements=[], now=NOW)
    seat = [m for m in tree["machines"] if m["machine"] == "dev-vm-1"][0]["loose"][0]
    assert seat["placement"] is None


# ---- what the row says -----------------------------------------------------

@pytest.mark.asyncio
async def test_pending_says_no_edge_has_reported_and_wears_the_drift_colour():
    """The state every placement is in until that machine's timer is wired —
    so it must not read as 'running'."""
    app = _app(placements=[_placement("pm-dev-vm-1", "dev-vm-1",
                                      status="pending-edge", observed=None)])
    async with app.run_test(size=(120, 34)) as pilot:
        await _ready(app, pilot)
        label = _label(app, "pm-dev-vm-1")
        assert "no edge yet" in label, label
        styles = " ".join(str(sp.style) for sp in _node(app, "pm-dev-vm-1").label.spans)
        assert app._palette()["warning"] in styles


@pytest.mark.asyncio
async def test_diverged_is_not_softened_into_pending():
    """One is a delay, the other is a disagreement."""
    app = _app(placements=[_placement("pm-dev-vm-1", "dev-vm-1",
                                      desired="running", status="diverged",
                                      observed="stopped")])
    async with app.run_test(size=(120, 34)) as pilot:
        await _ready(app, pilot)
        assert "DIVERGED" in _label(app, "pm-dev-vm-1")


@pytest.mark.asyncio
async def test_the_pending_detail_names_the_machine_to_go_and_check():
    app = _app(placements=[_placement("pm-dev-vm-1", "dev-vm-1",
                                      status="pending-edge", observed=None)])
    async with app.run_test(size=(120, 34)) as pilot:
        await _ready(app, pilot)
        await _select(app, pilot, "a:dev-vm-1/pm-dev-vm-1")
        text = _detail(app)
        assert "mcp-hub-edge.timer" in text
        assert "dev-vm-1" in text
        assert "before suspecting the hub" in text


@pytest.mark.asyncio
async def test_an_unscheduled_seat_says_so_rather_than_looking_idle():
    app = _app(placements=[])
    async with app.run_test(size=(120, 34)) as pilot:
        await _ready(app, pilot)
        await _select(app, pilot, "a:dev-vm-1/pm-dev-vm-1")
        text = _detail(app)
        assert "not scheduled" in text
        # …and names the two-step, because a placement needs a seat and a seat
        # needs a folder this machine cannot know for another box.
        assert "seats add" in text and "placements set" in text


# ---- driving it ------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_remote_seat_can_be_started_from_here():
    app = _app(placements=[_placement("pm-dev-vm-1", "dev-vm-1",
                                      desired="stopped", observed="stopped")])
    async with app.run_test(size=(120, 34)) as pilot:
        await _ready(app, pilot)
        await _select(app, pilot, "a:dev-vm-1/pm-dev-vm-1")
        titles = _titles(app)
        assert "Start on dev-vm-1 — pm-dev-vm-1" in titles
        # The state it is already in is not offered — that would be noise.
        assert "Stop on dev-vm-1 — pm-dev-vm-1" not in titles


@pytest.mark.asyncio
async def test_starting_writes_desired_state_and_nothing_else():
    ran: list = []
    app = _app(ran, placements=[_placement("pm-dev-vm-1", "dev-vm-1",
                                           desired="stopped", observed="stopped")])
    async with app.run_test(size=(120, 34)) as pilot:
        await _ready(app, pilot)
        await _select(app, pilot, "a:dev-vm-1/pm-dev-vm-1")
        _run_named(app, "Start on dev-vm-1 — pm-dev-vm-1")
        await pilot.pause()
        await pilot.pause()
    # NOT `squad start` — that would be tmux on THIS box for a seat on another.
    assert ran == [("/usr/bin/HUB", ["placements", "set", "pl-1", "running"])], ran


@pytest.mark.asyncio
async def test_the_offer_says_convergence_is_not_immediate():
    app = _app(placements=[_placement("pm-dev-vm-1", "dev-vm-1",
                                      desired="stopped", observed="stopped")])
    async with app.run_test(size=(120, 34)) as pilot:
        await _ready(app, pilot)
        await _select(app, pilot, "a:dev-vm-1/pm-dev-vm-1")
        helps = {t: h for t, h, _r in app.palette_commands()}
        h = helps["Start on dev-vm-1 — pm-dev-vm-1"]
        assert "edge realizes it" in h and "next pass" in h


@pytest.mark.asyncio
async def test_a_seat_with_no_placement_is_not_offered_start_or_stop():
    """There is nothing to set. Offering it would produce a hub 404 named
    after a seat the operator never declared."""
    app = _app(placements=[])
    async with app.run_test(size=(120, 34)) as pilot:
        await _ready(app, pilot)
        await _select(app, pilot, "a:dev-vm-1/pm-dev-vm-1")
        assert not [t for t in _titles(app) if t.startswith(("Start on", "Stop on"))]


@pytest.mark.asyncio
async def test_a_failing_placements_fetch_does_not_take_the_board_down():
    def boom():
        raise RuntimeError("placements exploded")
    app = _app()
    app._placements_for = boom
    async with app.run_test(size=(120, 34)) as pilot:
        await _ready(app, pilot)
        assert app.is_running
        assert app.placements == []
