"""The workspace manager's RENDERING — now the middle level of the fleet tree.

It used to be a separate `w` view: a flat list of every workspace on every
machine, one Static per row. The rows had already been through one geometry
defect (a fixed 24-cell Label beside a 1fr Static, which overran its own row by
exactly the label's width) and were fixed by measuring the text before mounting
it. The tree removes the class of defect instead: a label is one string, and
the path — the thing that made rows wrap — moved to the detail pane, which has
room for it.

Every guarantee the `w` view made is re-asserted here against the tree, because
a guarantee that quietly stops being checked when its surface moves was never a
guarantee. Drift still says what it IS in words, `? hub` is still not an
accusation, and drift still outranks both open states.
"""

from __future__ import annotations

import pathlib

import pytest
from textual.widgets import Tree

from mcp_hub.settings_app import SettingsApp

AGENTS = [{"agent": "alpha", "worktree": "/a", "klass": "squad"}]


def _row(name, machine, **kw):
    base = {
        "name": name, "machine": machine,
        # Under the REAL home, not a literal one: the detail pane
        # shortens $HOME -> ~, and a hardcoded /home/monke asserted an
        # environment coincidence — green on the dev box, red on the
        # first bare CI runner (HOME=/home/runner), while testing
        # nothing about the mechanism either way.
        "path": f"{pathlib.Path.home()}/Projects/{name}.code-workspace",
        "folders": 1, "error": "", "on_disk": True, "open_now": False,
        "registered": True, "squad": "", "listings": [],
    }
    base.update(kw)
    return base


def _app(rows, note="", reachable=True, this_machine="here", scoped_to=None):
    return SettingsApp(
        AGENTS, scoped_to=scoped_to,
        model_for=lambda c: {"agent": "x", "sections": []},
        squad_bin="/s", hub_bin="/h", board_for=None, poll_seconds=3600,
        this_machine=this_machine,
        workspaces_for=lambda: {
            "hub_reachable": reachable, "note": note, "rows": rows,
            "this_machine": this_machine, "machines": [],
        },
    )


async def _ready(app, pilot):
    """Pump until the registry poll has landed — bounded, so a broken poll
    fails the test rather than hanging it."""
    for _ in range(40):
        if app.workspaces.get("rows"):
            await pilot.pause()
            return
        await pilot.pause()
    raise AssertionError("the workspace poll never delivered any rows")


def _nodes(app, kind):
    return [n for n in app._all_nodes() if (n.data or {}).get("kind") == kind]


def _ws(app, name):
    for n in _nodes(app, "workspace"):
        if n.data["name"] == name:
            return n
    raise AssertionError(f"no workspace node named {name}")


def _styles(node) -> set[str]:
    """The colours actually applied to a label, as the terminal will show it."""
    return {str(span.style) for span in node.label.spans}


async def _detail_of(app, pilot, node) -> str:
    app.query_one("#fleet", Tree).move_cursor(node)
    await pilot.pause()
    await pilot.pause()
    pane = app.query_one("#detail")
    return " ".join(str(w.render()) for w in pane.walk_children())


@pytest.mark.asyncio
async def test_workspaces_are_grouped_under_one_node_per_machine():
    rows = [
        _row("showcase", "here"), _row("windows", "here"),
        _row("general", "dev-vm-1"), _row("runtime", "dev-vm-1"),
    ]
    app = _app(rows)
    async with app.run_test(size=(120, 30)) as pilot:
        await _ready(app, pilot)
        heads = [n.label.plain.split("  ·")[0] for n in _nodes(app, "machine")]
        # One node per machine, not one per row — and this box comes first.
        assert heads == ["here", "dev-vm-1"], heads
        assert len(_nodes(app, "workspace")) == 4


@pytest.mark.asyncio
async def test_machine_nodes_say_which_box_is_local():
    """Which machine a workspace lives on decides whether you can open it —
    it should never have to be inferred from the hostname."""
    app = _app([_row("a", "here"), _row("b", "dev-vm-1")])
    async with app.run_test(size=(120, 30)) as pilot:
        await _ready(app, pilot)
        heads = [n.label.plain for n in _nodes(app, "machine")]
        assert heads[0].startswith("here  · this machine"), heads
        assert heads[1].startswith("dev-vm-1  · remote"), heads


@pytest.mark.asyncio
async def test_every_label_fits_the_panel_it_is_mounted_in():
    """The original defect, pinned at the new surface. A label wider than the
    tree is a wrapped row again."""
    rows = [_row("showcase", "here"),
            _row("runtime", "dev-vm-1", open_now=True, squad="runtime")]
    app = _app(rows)
    async with app.run_test(size=(120, 30)) as pilot:
        await _ready(app, pilot)
        width = app.query_one("#fleet", Tree).region.width
        for node in app._all_nodes():
            text = node.label.plain
            assert len(text) <= width, \
                f"label overflows by {len(text) - width}: {text!r}"


@pytest.mark.asyncio
async def test_a_feral_file_is_named_in_words_and_wears_the_drift_colour():
    app = _app([_row("feral", "here", registered=False)])
    async with app.run_test(size=(120, 30)) as pilot:
        await _ready(app, pilot)
        node = _ws(app, "feral")
        assert "not registered" in node.label.plain
        assert app._palette()["warning"] in " ".join(_styles(node))
        # and the detail pane names the fix, not just the fault
        text = await _detail_of(app, pilot, node)
        assert "workspaces register" in text


@pytest.mark.asyncio
async def test_a_ghost_definition_says_it_has_no_file():
    # Deliberately NOT named "ghost": an earlier version of this test asserted
    # `"ghost" in ...` against a row of that name, so it passed with the drift
    # text emptied entirely. The row name must not contain any word the
    # assertion is looking for.
    app = _app([_row("defonly", "here", on_disk=False, path="")])
    async with app.run_test(size=(120, 30)) as pilot:
        await _ready(app, pilot)
        node = _ws(app, "defonly")
        assert "ghost — registered, no file" in node.label.plain
        assert app._palette()["warning"] in " ".join(_styles(node))


@pytest.mark.asyncio
async def test_unknown_registration_is_a_question_not_an_accusation():
    """When the hub cannot answer, the row must not say the workspace is
    unregistered — that would accuse every workspace on the machine."""
    app = _app([_row("solo", "here", registered=None)],
               note="the hub's management API is disabled — local scan only",
               reachable=False)
    async with app.run_test(size=(120, 30)) as pilot:
        await _ready(app, pilot)
        node = _ws(app, "solo")
        assert "registration unknown" in node.label.plain
        assert "not registered" not in node.label.plain
        text = await _detail_of(app, pilot, node)
        assert "not an accusation" in text
        # …and the offer to fix it is withheld, because there is nothing
        # yet to say is broken.
        assert "workspaces register" not in text


@pytest.mark.asyncio
async def test_the_path_moves_to_the_detail_pane_home_shortened():
    """The path is what made rows wrap. It is still shown — just where there
    is room for it."""
    app = _app([_row("solo", "here")])
    async with app.run_test(size=(120, 30)) as pilot:
        await _ready(app, pilot)
        node = _ws(app, "solo")
        assert "/home/" not in node.label.plain
        text = await _detail_of(app, pilot, node)
        assert "~/Projects" in text


@pytest.mark.asyncio
async def test_the_workspace_this_board_is_looking_at_is_marked_HERE():
    rows = [_row("mine", "here", open_now=True), _row("other", "here", open_now=True)]
    app = _app(rows, scoped_to=f"{pathlib.Path.home()}/Projects/mine.code-workspace")
    async with app.run_test(size=(120, 30)) as pilot:
        await _ready(app, pilot)
        mine, other = _ws(app, "mine"), _ws(app, "other")
        assert mine.label.plain.startswith("◉ mine")
        assert app._palette()["accent"] in " ".join(_styles(mine))
        # The other open workspace is still marked open, just not as yours.
        assert other.label.plain.startswith("● other")
        assert app._palette()["success"] in " ".join(_styles(other))


@pytest.mark.asyncio
async def test_an_unscoped_board_marks_nothing_as_here():
    """No --workspace means the board is not standing in one."""
    app = _app([_row("a", "here", open_now=True)], scoped_to=None)
    async with app.run_test(size=(120, 30)) as pilot:
        await _ready(app, pilot)
        node = _ws(app, "a")
        assert "◉" not in node.label.plain
        assert node.label.plain.startswith("● a")


@pytest.mark.asyncio
async def test_drift_outranks_both_open_states():
    """A workspace can be open AND feral. It must read as feral — attention
    beats status, or the colour that means 'fix me' is the one you lose."""
    app = _app(
        [_row("mine", "here", open_now=True, registered=False)],
        scoped_to=f"{pathlib.Path.home()}/Projects/mine.code-workspace",
    )
    async with app.run_test(size=(120, 30)) as pilot:
        await _ready(app, pilot)
        node = _ws(app, "mine")
        styles = " ".join(_styles(node))
        assert app._palette()["warning"] in styles
        assert app._palette()["success"] not in styles
        assert app._palette()["accent"] not in styles
        assert node.label.plain.startswith("◉ mine")   # still marked, just not coloured ok
        assert "not registered" in node.label.plain


@pytest.mark.asyncio
async def test_the_selection_survives_the_registry_restructuring_the_tree():
    """Found by rendering the real board, not by any unit test.

    At mount the registry has not answered yet, so the seat hangs loose under
    its machine. A second later the poll lands, the seat moves INSIDE a
    workspace, and its node key changes with it — a key carries its parent.
    Re-acquiring by key alone lost the selection every single time the board
    was opened, and the detail pane went blank.
    """
    # The registry answers NOTHING first, exactly as it does before the first
    # poll returns — otherwise the seat is already attributed by the time the
    # test looks and the restructure never happens.
    answered = {"yet": False}

    def registry():
        rows = [_row("team", "here", listings=["/a"])] if answered["yet"] else []
        return {"hub_reachable": True, "note": "", "rows": rows,
                "this_machine": "here", "machines": []}

    app = _app([])
    app._workspaces_for = registry
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()
        assert app.selected["agent"] == "alpha"
        loose_key = app.selected["key"]
        assert loose_key == "a:here/alpha", loose_key      # hanging loose

        answered["yet"] = True
        app._poll_workspaces()
        await _ready(app, pilot)

        assert app.selected is not None, "the selection was dropped"
        assert app.selected["agent"] == "alpha"
        assert app.selected["key"] != loose_key, \
            "the seat never moved, so this proves nothing"
        assert app.selected["key"] == "a:w:here/team/alpha"


@pytest.mark.asyncio
async def test_a_failing_registry_is_named_and_the_tree_survives():
    app = _app([])
    def boom():
        raise RuntimeError("registry exploded")
    app._workspaces_for = boom
    async with app.run_test(size=(120, 30)) as pilot:
        for _ in range(20):
            await pilot.pause()
        assert app.is_running
        assert "registry exploded" in app.workspaces.get("note", "")


def test_collect_returns_the_machine_it_was_asked_about(tmp_path):
    """The view must not re-derive the local machine name — a second
    derivation is a second chance to disagree."""
    from mcp_hub.workspace_data import collect_workspaces

    class _Api:
        def get_registry(self):
            return {"definitions": [], "discovered": []}

    out = collect_workspaces(_Api(), [tmp_path], "box-9")
    assert out["this_machine"] == "box-9"
    assert "box-9" in out["machines"]


def test_short_dir_leaves_paths_outside_home_alone():
    assert SettingsApp._short_dir("/opt/x/a.code-workspace") == "/opt/x"
    assert SettingsApp._short_dir("") == "definition only — nothing materialized"
