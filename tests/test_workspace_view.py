"""The workspace manager's RENDERING — grouped by machine, nothing overflowing.

The previous shape reused the settings rows' geometry: a fixed 24-cell Label
beside a 1fr Static. Measured, the Static was sized to the full row width
while starting after the label, so it overran its own row by exactly the
label's width, and a row whose second line was a 45-character absolute path
(indented 24) overran again. On the operator's screen one workspace read as
three wrapped lines.

So: one Static per row, text composed and measured before it is mounted, and
a heading per machine — which is also what makes the fleet view legible once
other machines report.
"""

from __future__ import annotations

import pytest
from textual.containers import Horizontal
from textual.widgets import Static

from mcp_hub.settings_app import SettingsApp

AGENTS = [{"agent": "alpha", "worktree": "/a", "klass": "squad"}]


def _row(name, machine, **kw):
    base = {
        "name": name, "machine": machine,
        "path": f"/home/monke/Projects/{name}.code-workspace",
        "folders": 1, "error": "", "on_disk": True, "open_now": False,
        "registered": True, "squad": "",
    }
    base.update(kw)
    return base


def _app(rows, note="", reachable=True, this_machine="here", scoped_to=None):
    return SettingsApp(
        AGENTS, scoped_to=scoped_to,
        model_for=lambda c: {"agent": "x", "sections": []},
        squad_bin="/s", hub_bin="/h", board_for=None, poll_seconds=3600,
        workspaces_for=lambda: {
            "hub_reachable": reachable, "note": note, "rows": rows,
            "this_machine": this_machine,
        },
    )


async def _texts(app, cls):
    det = app.query_one("#detail")
    return [str(n.content) for n in det.query(Static) if cls in n.classes]


async def _headings(app):
    """Machine headings in document order, local or remote alike."""
    det = app.query_one("#detail")
    return [
        str(n.content) for n in det.query(Static)
        if {"ws-machine", "ws-machine-remote"} & set(n.classes)
    ]


@pytest.mark.asyncio
async def test_rows_are_grouped_under_one_heading_per_machine():
    rows = [
        _row("showcase", "fireblade-wsl"), _row("windows", "fireblade-wsl"),
        _row("general", "dev-vm-1"), _row("runtime", "dev-vm-1"),
    ]
    app = _app(rows)
    async with app.run_test(size=(120, 30)) as pilot:
        app.show_workspaces = True
        await app.refresh_detail()
        await pilot.pause()
        heads = await _headings(app)
        # One heading per machine, not one per row.
        assert [h.split("  ·")[0] for h in heads] == ["fireblade-wsl", "dev-vm-1"]
        body = await _texts(app, "ws-row")
        assert len(body) == 4


@pytest.mark.asyncio
async def test_no_row_widget_is_a_multi_child_line_anymore():
    """The overflow came from Label+Static inside a Horizontal. If that shape
    returns, the geometry bug returns with it."""
    app = _app([_row("solo", "here")])
    async with app.run_test(size=(120, 30)) as pilot:
        app.show_workspaces = True
        await app.refresh_detail()
        await pilot.pause()
        det = app.query_one("#detail")
        # The live board section also uses .row-line; scope to what the
        # workspace view mounted by checking none carry workspace text.
        for h in det.query(Horizontal):
            joined = " ".join(str(getattr(c, "content", "")) for c in h.children)
            assert "code-workspace" not in joined
            assert "hub" not in joined or "disk" not in joined


@pytest.mark.asyncio
async def test_every_rendered_row_fits_the_pane_it_is_mounted_in():
    """The whole point. A row wider than its pane is the defect."""
    rows = [_row("showcase", "fireblade-wsl"), _row("runtime", "dev-vm-1",
                                                    open_now=True, squad="runtime")]
    app = _app(rows)
    async with app.run_test(size=(120, 30)) as pilot:
        app.show_workspaces = True
        await app.refresh_detail()
        await pilot.pause()
        width = app.query_one("#detail").region.width
        for text in await _texts(app, "ws-row"):
            assert len(text) <= width, f"row overflows by {len(text) - width}: {text}"


@pytest.mark.asyncio
async def test_a_feral_file_is_named_in_words_and_wears_the_drift_class():
    app = _app([_row("feral", "here", registered=False)])
    async with app.run_test(size=(120, 30)) as pilot:
        app.show_workspaces = True
        await app.refresh_detail()
        await pilot.pause()
        drift = await _texts(app, "ws-row-drift")
        assert len(drift) == 1
        assert "✗ hub" in drift[0]
        assert "not registered" in drift[0]
        assert await _texts(app, "ws-row") == []


@pytest.mark.asyncio
async def test_a_ghost_definition_says_it_has_no_file():
    # Deliberately NOT named "ghost": an earlier version of this test asserted
    # `"ghost" in drift[0]` against a row of that name, so it passed with the
    # drift text emptied entirely. The row name must not contain any word the
    # assertion is looking for.
    app = _app([_row("defonly", "here", on_disk=False, path="")])
    async with app.run_test(size=(120, 30)) as pilot:
        app.show_workspaces = True
        await app.refresh_detail()
        await pilot.pause()
        drift = await _texts(app, "ws-row-drift")
        assert "✗ disk" in drift[0]
        assert "ghost — registered, no file" in drift[0]


@pytest.mark.asyncio
async def test_unknown_registration_renders_as_a_question_not_an_accusation():
    """When the hub cannot answer, `? hub` — never `✗ hub`, which would
    accuse every workspace on the machine of being feral."""
    app = _app([_row("solo", "here", registered=None)],
               note="the hub's management API is disabled — local scan only",
               reachable=False)
    async with app.run_test(size=(120, 30)) as pilot:
        app.show_workspaces = True
        await app.refresh_detail()
        await pilot.pause()
        text = (await _texts(app, "ws-row"))[0]
        assert "? hub" in text
        assert "✗ hub" not in text


@pytest.mark.asyncio
async def test_paths_are_home_shortened_so_the_row_stays_short():
    app = _app([_row("solo", "here")])
    async with app.run_test(size=(120, 30)) as pilot:
        app.show_workspaces = True
        await app.refresh_detail()
        await pilot.pause()
        text = (await _texts(app, "ws-row"))[0]
        assert "~/Projects" in text
        assert "/home/" not in text


@pytest.mark.asyncio
async def test_machine_headings_say_which_box_is_local():
    """Which machine a workspace lives on decides whether you can open it —
    it should never have to be inferred from the hostname."""
    rows = [_row("a", "here"), _row("b", "dev-vm-1")]
    app = _app(rows, this_machine="here")
    async with app.run_test(size=(120, 30)) as pilot:
        app.show_workspaces = True
        await app.refresh_detail()
        await pilot.pause()
        local = await _texts(app, "ws-machine")
        remote = await _texts(app, "ws-machine-remote")
        assert local == ["here  · this machine"]
        assert remote == ["dev-vm-1  · remote"]


@pytest.mark.asyncio
async def test_the_workspace_this_board_is_looking_at_is_marked_HERE():
    rows = [_row("mine", "here", open_now=True), _row("other", "here", open_now=True)]
    app = _app(rows, scoped_to="/home/monke/Projects/mine.code-workspace")
    async with app.run_test(size=(120, 30)) as pilot:
        app.show_workspaces = True
        await app.refresh_detail()
        await pilot.pause()
        mine = await _texts(app, "ws-row-here")
        others = await _texts(app, "ws-row-open")
        assert len(mine) == 1 and "mine" in mine[0] and "◉ here" in mine[0]
        # The other open workspace is still marked open, just not as yours.
        assert len(others) == 1 and "other" in others[0] and "● open" in others[0]


@pytest.mark.asyncio
async def test_an_unscoped_board_marks_nothing_as_here():
    """No --workspace means the board is not standing in one."""
    app = _app([_row("a", "here", open_now=True)], scoped_to=None)
    async with app.run_test(size=(120, 30)) as pilot:
        app.show_workspaces = True
        await app.refresh_detail()
        await pilot.pause()
        assert await _texts(app, "ws-row-here") == []
        assert len(await _texts(app, "ws-row-open")) == 1


@pytest.mark.asyncio
async def test_drift_outranks_both_open_states():
    """A workspace can be open AND feral. It must read as feral — attention
    beats status, or the colour that means 'fix me' is the one you lose."""
    app = _app(
        [_row("mine", "here", open_now=True, registered=False)],
        scoped_to="/home/monke/Projects/mine.code-workspace",
    )
    async with app.run_test(size=(120, 30)) as pilot:
        app.show_workspaces = True
        await app.refresh_detail()
        await pilot.pause()
        assert await _texts(app, "ws-row-here") == []
        assert await _texts(app, "ws-row-open") == []
        drift = await _texts(app, "ws-row-drift")
        assert len(drift) == 1
        assert "◉ here" in drift[0]      # still marked, just not coloured as ok
        assert "not registered" in drift[0]


def test_collect_returns_the_machine_it_was_asked_about(tmp_path):
    """The view must not re-derive the local machine name — a second
    derivation is a second chance to disagree."""
    from mcp_hub.workspace_data import collect_workspaces

    class _Api:
        def get_registry(self):
            return {"definitions": [], "discovered": []}

    out = collect_workspaces(_Api(), [tmp_path], "box-9")
    assert out["this_machine"] == "box-9"


def test_short_dir_leaves_paths_outside_home_alone():
    assert SettingsApp._short_dir("/opt/x/a.code-workspace") == "/opt/x"
    assert SettingsApp._short_dir("") == "definition only — nothing materialized"
