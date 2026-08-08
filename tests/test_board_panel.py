"""The board half of the Squad Board panel, driven by clicking (Pilot), and
the data/theme layers under it.

Same discipline as test_settings_app.py: every previous presentation was
"tested" by rendering it and looking, which is how six of them shipped defects
the first click found.
"""
from __future__ import annotations

import json
import stat
import time

import pytest
from textual.widgets import Select

from mcp_hub import board_data
from mcp_hub.settings_app import SettingsApp

US = "\x1f"

AGENTS = [
    {"agent": "alpha", "worktree": "/a", "klass": "squad"},
    {"agent": "beta", "worktree": "/b", "klass": "squad"},
    {"agent": "gamma", "worktree": "/g", "klass": "faculty"},
]

MODEL = {
    "agent": "x",
    "sections": [{"title": "LAUNCH", "note": "", "rows": [
        {"label": "Comms", "value": "off", "source": "set on this agent",
         "edit": {"choices": ["on", "off"], "bin": "squad",
                  "argv": ["comms", "{}", "x"], "applies": "next restart"}}]}],
}


def _model_for(cwd):
    return {**MODEL, "agent": cwd.strip("/")}


def _snapshot(state_beta="waiting"):
    return {
        "agents": {
            "alpha": {"agent": "alpha", "state": "working", "hub": "⚡",
                      "model": "Fable", "ctx": "19%", "waiting_seconds": 0,
                      "action": "merging the branch", "question": "",
                      "dirty": 0, "unpushed": 2, "branch": "main",
                      "usage_today": 1200000, "usage_hour": 9000,
                      "wakeable": True, "next": None},
            "beta": {"agent": "beta", "state": state_beta, "hub": "⚡",
                     "model": "Opus", "ctx": "57%", "waiting_seconds": 134,
                     "action": "", "question": "Do you want to run rm -rf?",
                     "dirty": 3, "unpushed": 0, "branch": "fix",
                     "usage_today": 0, "usage_hour": 0, "wakeable": True,
                     "next": {"source": "hub", "age": "5m", "hand": True,
                              "text": "DECISION ASK: deploy?", "ask": "deploy?",
                              "net": 5}},
        },
        "order": ["alpha", "beta"],
        "counts": {"waiting": 1, "working": 1, "idle": 0, "down": 0, "hands": 1},
        "error": None,
    }


def _app(board=None, ran=None, dark=None):
    app = SettingsApp(AGENTS, scoped_to=None, model_for=_model_for,
                      squad_bin="/usr/bin/SQUAD", hub_bin="/usr/bin/HUB",
                      board_for=board, dark=dark, poll_seconds=3600,
                      this_machine="thisbox")
    if ran is not None:
        def apply(exe, argv, label, value):
            ran.append((exe, argv))
            app.call_from_thread(app._after_apply, f"{label} → {value}")
        app._apply = apply
    return app


def _label(app, agent: str) -> str:
    """One seat's rendered row in the tree, as the operator reads it."""
    for node in app._agent_nodes():
        if (node.data or {}).get("agent") == agent:
            return node.label.plain
    raise AssertionError(f"{agent} is not in the tree")


async def _goto(app, pilot, agent: str) -> None:
    from textual.widgets import Tree
    for node in app._agent_nodes():
        if (node.data or {}).get("agent") == agent:
            app.query_one("#fleet", Tree).move_cursor(node)
            await pilot.pause()
            await pilot.pause()
            return
    raise AssertionError(f"{agent} is not in the tree")


# ---- the live roster and detail ----

@pytest.mark.asyncio
async def test_the_tree_wears_the_board_state():
    app = _app(board=_snapshot)
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        await pilot.pause()
        assert "19%" in _label(app, "alpha")
        assert "working" in _label(app, "alpha")
        # The model name left the label when it outgrew the panel — it must
        # still be readable, one pane over.
        detail = " ".join(str(w.render())
                          for w in app.query_one("#detail").walk_children())
        assert "Fable" in detail
        assert "waiting" in _label(app, "beta") and "2m" in _label(app, "beta")
        assert "🙋" in _label(app, "beta")           # the hand rides the row
        # an agent the scan doesn't know says so; it does not borrow a state
        assert "○" in _label(app, "gamma")
        assert "waiting" not in _label(app, "gamma")


@pytest.mark.asyncio
async def test_a_local_seat_shows_its_wake_marker_like_a_remote_one_does():
    """The regression the operator caught: `hub` was dropped from the label
    when it was trimmed to fit the panel, so remote seats kept their ⚡ and
    local ones lost it — the tree read as "remote agents are instrumented,
    local ones aren't"."""
    app = _app(board=_snapshot)
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        await pilot.pause()
        assert "⚡" in _label(app, "alpha"), _label(app, "alpha")
        # …and an agent with no pane claims no wake state at all
        assert "⚡" not in _label(app, "gamma")


@pytest.mark.asyncio
async def test_every_seat_wears_a_state_glyph_none_are_bare():
    """`idle` and `no record` both used to render as a bare `·`, which is not
    a mark. Six of eight real local seats looked like nothing at all."""
    app = _app(board=_snapshot)
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        await pilot.pause()
        marks = {"🔴", "▶", "💤", "✖", "○", "⚠"}
        for agent in ("alpha", "beta", "gamma"):
            label = _label(app, agent)
            assert label[0] in marks, f"{agent} starts bare: {label!r}"


@pytest.mark.asyncio
async def test_the_name_column_does_not_jitter_between_states():
    """🔴 and 💤 are two cells wide, ▶ and ○ are one. Unpadded, the name
    shifts sideways as an agent changes state.

    The pair here is chosen to make padding LOAD-BEARING: one wide glyph, one
    narrow, everything else identical. An earlier version of this test compared
    ▶ against ○ — both narrow — so it passed with the padding removed entirely
    (caught by mutating `_cell2` to a no-op; it survived).
    """
    from rich.cells import cell_len

    def snap():
        s = _snapshot()
        # alpha: idle → 💤 (TWO cells).  beta: down → ✖ (ONE cell).
        # Both wakeable, and NEITHER raises a hand — `waiting` always does, by
        # construction, so a waiting agent can never be half of this pair.
        s["agents"]["alpha"]["state"] = "idle"
        s["agents"]["beta"].update(state="down", next=None)
        return s

    app = _app(board=snap)
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        await pilot.pause()
        wide, narrow = _label(app, "alpha"), _label(app, "beta")
        assert wide.startswith("💤") and narrow.startswith("✖"), (wide, narrow)
        assert "🙋" not in wide and "🙋" not in narrow, "a hand skews the prefix"
        assert cell_len(wide.split("alpha")[0]) \
            == cell_len(narrow.split("beta")[0]), (wide, narrow)


@pytest.mark.asyncio
async def test_the_boards_own_hub_phrase_survives_in_the_detail_pane():
    """A ⚡-or-nothing column cannot say `✖ REGISTER`."""
    def snap():
        s = _snapshot()
        s["agents"]["alpha"]["hub"] = "✖ REGISTER"
        return s
    app = _app(board=snap)
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        await pilot.pause()
        await _goto(app, pilot, "alpha")
        detail = " ".join(str(w.render())
                          for w in app.query_one("#detail").walk_children())
        assert "✖ REGISTER" in detail


@pytest.mark.asyncio
async def test_a_stopped_seat_says_so_and_names_its_class():
    """`faculty` left the label; it must land somewhere, or the tree simply
    lost the fact that some seats are deliberately never auto-started."""
    app = _app(board=_snapshot)
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        await _goto(app, pilot, "gamma")
        detail = " ".join(str(w.render())
                          for w in app.query_one("#detail").walk_children())
        assert "not running" in detail
        assert "faculty" in detail
        # the fleet summary took over the subtitle
        assert "need you" in app.sub_title


@pytest.mark.asyncio
async def test_the_live_section_shows_the_blocking_question_with_answers():
    app = _app(board=_snapshot)
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        await _goto(app, pilot, "beta")                # the waiting one
        texts = " ".join(str(w.render()) for w in app.query("#live Static"))
        assert "rm -rf" in texts                       # the question, verbatim
        assert "deploy?" in texts                      # the open card's ask
        labels = [str(b.label) for b in app.query("#live Button")]
        assert labels == ["yes", "no", "always"]


@pytest.mark.asyncio
async def test_answer_buttons_run_squad_answer_fail_closed_verb():
    ran: list = []
    app = _app(board=_snapshot, ran=ran)
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        await _goto(app, pilot, "beta")
        await pilot.click(app.query("#live Button").first())
        await pilot.pause()
        await pilot.pause()
    assert ran == [("/usr/bin/SQUAD", ["answer", "beta", "yes"])], ran


@pytest.mark.asyncio
async def test_a_working_agent_gets_no_answer_buttons():
    """`squad answer` is fail-closed anyway, but a button that can only fail
    is UI noise claiming an action exists."""
    app = _app(board=lambda: _snapshot(state_beta="working"))
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        await _goto(app, pilot, "beta")
        assert not list(app.query("#live Button"))


@pytest.mark.asyncio
async def test_a_broken_collector_degrades_to_a_settings_panel():
    """The board half is an instrument; the settings half is the tool. A
    collector that raises must cost the LIVE section, never the app."""
    def boom():
        raise RuntimeError("squad exploded")
    app = _app(board=boom)
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        await pilot.pause()
        assert app.sub_title == "live data unavailable"
        # ⚠️ `assert sel is not None` used to stand here and could NEVER fail:
        # DOMQuery.first() raises NoMatches on an empty result rather than
        # returning None, so the assertion asserted nothing and the `.first()`
        # call was doing all the work silently.
        #
        # "Settings still editable" means the control is POPULATED, not merely
        # mounted — a Select whose value is still Select.NULL is exactly the
        # blank control this degradation is supposed to avoid. Waits on that
        # condition rather than on a frame count: measured, this app mounts and
        # populates in 1 pause while the test allowed 2, so the margin here was
        # 1 where test_settings_app's was 0 (which is why that file flaked on a
        # loaded box at 3/20 and this one did not). Same shape, more headroom.
        for _ in range(200):
            sels = app.query("Select")
            if sels and not any(s.value is Select.NULL for s in sels):
                break
            await pilot.pause()
        sel = app.query("Select").first()             # settings still editable
        assert sel.value is not Select.NULL, "settings degraded to a blank control"


@pytest.mark.asyncio
async def test_n_jumps_to_the_agent_that_needs_you():
    app = _app(board=_snapshot)
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        await pilot.pause()
        assert app.selected["agent"] == "alpha"
        await pilot.press("n")
        await pilot.pause()
        assert app.selected["agent"] == "beta"


# ---- theme ----

@pytest.mark.asyncio
async def test_detected_dark_terminal_gets_the_dark_theme_and_t_flips_it():
    app = _app(dark=True)
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        assert app.theme == "claude-dark"
        await pilot.press("t")
        assert app.theme == "squad-light"
        await pilot.press("t")
        assert app.theme == "claude-dark"


@pytest.mark.asyncio
async def test_undetected_terminal_stays_light_as_it_always_was():
    app = _app(dark=None)
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        assert app.theme == "squad-light"


def test_theme_detection_order_env_osc_colorfgbg():
    dark_reply = "\x1b]11;rgb:1a1a/1a1a/1919\x1b\\"
    light_reply = "\x1b]11;rgb:ffff/ffff/ffff\x07"
    # the terminal's own answer
    assert board_data.terminal_prefers_dark({}, query=lambda: dark_reply) is True
    assert board_data.terminal_prefers_dark({}, query=lambda: light_reply) is False
    # explicit override beats the terminal
    assert board_data.terminal_prefers_dark(
        {"SQUAD_THEME": "light"}, query=lambda: dark_reply) is False
    # no answer -> rxvt convention
    assert board_data.terminal_prefers_dark(
        {"COLORFGBG": "15;0"}, query=lambda: None) is True
    assert board_data.terminal_prefers_dark(
        {"COLORFGBG": "0;15"}, query=lambda: None) is False
    # nothing knows -> None, caller defaults
    assert board_data.terminal_prefers_dark({}, query=lambda: None) is None


# ---- the collector ----

def _fake_squad(tmp_path, payload):
    """A stand-in `squad` that prints canned board --json."""
    exe = tmp_path / "squad"
    exe.write_text("#!/bin/sh\ncat <<'EOF'\n" + json.dumps(payload) + "\nEOF\n")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return str(exe)


def _scan(*rows):
    return {"generated": "now", "agents": [
        {"agent": a, "state": st, "hub": "⚡", "model": "Opus", "ctx": "10%",
         "waiting_seconds": 0, "action": "", "question": "", "dirty": 0,
         "unpushed": 0, "branch": "main"} for a, st in rows]}


def test_collect_merges_scan_with_every_cache(tmp_path):
    home = tmp_path / "home"
    mh = home / ".mcp-hub"
    mh.mkdir(parents=True)
    (mh / "board-usage.cache").write_text(f"alpha{US}12000{US}500\n")
    (mh / "board-recap.cache").write_text(f"beta{US}100{US}1{US}Waiting on you.\n")
    (mh / "fleet-board.json").write_text(json.dumps(
        {"ts": 0, "agents": [{"name": "alpha", "wakeable": False, "next": ""}]}))
    (mh / "decisions-open.json").write_text(json.dumps(
        {"ts": 1000, "cards": [{"agent": "alpha", "raw": "DECISION ASK: x?",
                                "ask": "x?", "net_score": 3,
                                "submitted_at": 900}]}))
    exe = _fake_squad(tmp_path, _scan(("alpha", "working"), ("beta", "idle")))
    snap = board_data.collect(exe, home=home, now=1100.0)
    assert snap["error"] is None
    a, b = snap["agents"]["alpha"], snap["agents"]["beta"]
    assert (a["usage_today"], a["usage_hour"]) == (12000, 500)
    assert a["wakeable"] is False
    # fresh decisions cache: the card IS the hand, and it wins over everything
    assert a["next"]["source"] == "hub" and a["next"]["hand"] is True
    assert a["next"]["net"] == 3
    # recap hand: flagged AND idle -> a real hand... but the fresh decisions
    # cache is authoritative — no card for beta means NO hand, however loudly
    # the recap prose reads as waiting (squad's 2026-07-26 rule)
    assert b["next"]["source"] == "recap" and b["next"]["hand"] is False
    assert snap["counts"]["hands"] == 1


def test_collect_recap_hand_counts_when_decisions_cache_is_stale(tmp_path):
    home = tmp_path / "home"
    mh = home / ".mcp-hub"
    mh.mkdir(parents=True)
    (mh / "board-recap.cache").write_text(f"beta{US}100{US}1{US}Waiting on you.\n")
    # decisions cache STALE (ts far in the past): applies no labels either way
    (mh / "decisions-open.json").write_text(json.dumps({"ts": 0, "cards": []}))
    exe = _fake_squad(tmp_path, _scan(("beta", "idle")))
    snap = board_data.collect(exe, home=home, now=100000.0)
    assert snap["agents"]["beta"]["next"]["hand"] is True


def test_collect_survives_missing_squad_and_missing_caches(tmp_path):
    home = tmp_path / "empty-home"
    home.mkdir()
    snap = board_data.collect(str(tmp_path / "no-such-squad"), home=home)
    assert snap["agents"] == {} and "squad not found" in snap["error"]


def test_collect_recap_hand_needs_idle(tmp_path):
    home = tmp_path / "home"
    mh = home / ".mcp-hub"
    mh.mkdir(parents=True)
    (mh / "board-recap.cache").write_text(f"beta{US}100{US}1{US}Waiting on you.\n")
    exe = _fake_squad(tmp_path, _scan(("beta", "working")))
    snap = board_data.collect(exe, home=home, now=100000.0)
    assert snap["agents"]["beta"]["next"]["hand"] is False


@pytest.mark.asyncio
async def test_polling_a_waiting_agent_does_not_crash_or_churn_its_buttons():
    """The operator's first real click on a waiting agent took the app down:
    waiting_seconds ticks on every scan, so the live section — and the answer
    buttons' container, which carried a FIXED id — was torn down and remounted
    every poll, and remove_children() is asynchronous, so the previous
    container was still in the tree when the next one mounted: DuplicateIds,
    app dead ("as soon as it loaded, the page crashed", 2026-07-29).

    Two properties pinned: many polls over a waiting agent crash nothing, and
    a tick that does not change the DISPLAYED minute does not rebuild the
    section at all (a rebuild under the pointer eats the click it was built
    to receive)."""
    tick = {"n": 0}

    def snapshot():
        tick["n"] += 1
        snap = _snapshot()
        # SUB-MINUTE ticks: seconds advance every scan but stay inside the
        # same minute, so the rendered "2m" never changes. Wrapping is
        # deliberate — an unbounded 134+n crosses 180s and the displayed
        # minute becomes "3m", which SHOULD rebuild, so the fixture would be
        # constructing the opposite of the condition under test. It did, and
        # the test failed ~50% of runs while the code was correct.
        # % 45 (fb's bound, adopted for convergence): 134..178 — 45 distinct
        # values that all render "2m", so ANY number of ticks proves no
        # rebuild, rather than only the few that fit a narrow window.
        snap["agents"]["beta"]["waiting_seconds"] = 134 + (tick["n"] % 45)
        return snap

    app = _app(board=snapshot)
    async with app.run_test(size=(120, 34)) as pilot:
        # WAIT for the mount-time poll to be APPLIED before touching anything.
        # It runs on a worker thread, so whether it lands before or after the
        # selection is pure scheduling — and it is the difference between the
        # live section rendering None and rendering beta, i.e. a legitimately
        # different render key. Racing it made this test fail ~50% of runs.
        for _ in range(50):
            await pilot.pause()
            if "beta" in (app.board.get("agents") or {}):
                break
        assert "beta" in (app.board.get("agents") or {}), "board data never arrived"
        await _goto(app, pilot, "beta")                # the waiting one
        key_after_select = app._live_key
        assert key_after_select is not None, "live section never rendered beta"
        gen_after_select = app._gen
        for _ in range(6):                             # several poll applications
            app._apply_board(snapshot())
            await pilot.pause()
        assert app.is_running                          # the crash, pinned
        buttons = list(app.query("Button"))
        assert len(buttons) == 3, f"{len(buttons)} buttons — duplicate mounts survived"
        # BOTH the render key AND the rebuild counter, because each is blind
        # where the other sees. The key alone cannot detect a lost dedup — a
        # rebuild with an unchanged key leaves _live_key equal, so deleting
        # the early-out sailed through a key-only assertion (measured
        # 2026-08-04). _gen alone was flaky when the mount-time poll's worker
        # raced the selection and bumped it legitimately (2026-07-29 night —
        # ~50% failures on the very commit it protects). The wait-for-data
        # loop above ends that race: after selection, a sub-minute tick must
        # neither change what would be rendered nor rebuild the section that
        # renders it.
        assert app._live_key == key_after_select, \
            "sub-minute timer ticks changed the live render key"
        assert app._gen == gen_after_select, \
            "sub-minute timer ticks rebuilt the live section under the pointer"


@pytest.mark.asyncio
async def test_rebuilding_a_waiting_agents_live_section_repeatedly_does_not_crash():
    """THE CRASH ITSELF — the operator's ("as soon as it loaded, the page
    crashed", 2026-07-29). Its sibling above pins the no-churn property, and
    de-flaking that one silently cost this coverage: with a stable render key
    the section is never rebuilt, so a fixed id has nothing to collide with and
    the crash mutant PASSED it. A property test and a crash test need
    different fixtures — this one forces a rebuild on every tick by crossing
    the displayed minute, which is when the answer buttons are actually torn
    down and remounted.
    """
    tick = {"n": 0}

    def snapshot():
        tick["n"] += 1
        snap = _snapshot()
        # a whole minute per tick: the rendered value changes every time, so
        # the live section — buttons and all — is genuinely rebuilt
        snap["agents"]["beta"]["waiting_seconds"] = 134 + 60 * tick["n"]
        return snap

    app = _app(board=snapshot)
    async with app.run_test(size=(120, 34)) as pilot:
        for _ in range(50):
            await pilot.pause()
            if "beta" in (app.board.get("agents") or {}):
                break
        await _goto(app, pilot, "beta")                # beta, waiting
        await pilot.pause()
        for _ in range(8):
            app._apply_board(snapshot())
            await pilot.pause()
            await pilot.pause()
        assert app.is_running, "the panel died while rebuilding the live section"
        buttons = [str(b.label) for b in app.query("Button")]
        assert buttons == ["yes", "no", "always"], \
            f"{buttons} — duplicate or lost mounts across rebuilds"


@pytest.mark.asyncio
async def test_jumping_repaints_the_detail_pane_not_just_the_cursor():
    """A defect that shipped and was caught by a later test asserting on the
    RENDERED pane instead of on `selected`.

    `_move_to` sets the selection eagerly so callers see it at once, then
    defers the cursor move a frame (a Tree maps nodes to lines only after
    layout). When the deferred move finally fired its NodeHighlighted,
    `_node_changed` saw a key that already matched and returned early — so `n`
    and every "Go to" moved the cursor while the right-hand pane went on
    describing the seat you had just left.
    """
    app = _app(board=_snapshot)
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        await pilot.pause()
        await _goto(app, pilot, "alpha")
        assert "merging the branch" in _detail_text(app)

        await pilot.press("n")                     # jumps to beta, the waiting one
        await pilot.pause()
        await pilot.pause()
        assert app.selected["agent"] == "beta"
        text = _detail_text(app)
        assert "rm -rf" in text, "the pane still shows the seat we left"
        assert "merging the branch" not in text


def _detail_text(app) -> str:
    return " ".join(str(w.render())
                    for w in app.query_one("#detail").walk_children())


# ---- the roster is re-read, not snapshotted ----
#
# `squad add-container` enrolled a seat and the board that was already open
# never showed it — twice, on the same evening. The roster was read once at
# construction while the board, the workspaces and the fleet all refreshed on
# timers, so the one instrument the operator was actually looking at was the
# one that could not see the change. "Restart the tab" is not an answer: a
# missing row and a broken seat look identical from that chair.

def _roster_app(roster_for, board=None):
    """Same panel, with the roster INJECTED as a callable rather than a list.

    The opening list is deliberately what the callable says at construction —
    the real caller reads it the same way, so a test cannot pass by starting
    from a roster the reader never produced.
    """
    return SettingsApp(roster_for(), scoped_to=None, model_for=_model_for,
                       squad_bin="/usr/bin/SQUAD", hub_bin="/usr/bin/HUB",
                       board_for=board, dark=None, poll_seconds=3600,
                       this_machine="thisbox", roster_for=roster_for)


def _tree_agents(app) -> list[str]:
    return [(n.data or {}).get("agent") for n in app._agent_nodes()]


@pytest.mark.asyncio
async def test_a_seat_enrolled_after_the_board_opened_appears_in_it():
    rows = list(AGENTS)
    app = _roster_app(lambda: list(rows), board=_snapshot)
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        await pilot.pause()
        assert "delta" not in _tree_agents(app)
        rows.append({"agent": "delta", "worktree": "/d", "klass": "faculty"})
        app._poll_board()                       # the tick, not a restart
        await pilot.pause()
        await pilot.pause()
        assert "delta" in _tree_agents(app), _tree_agents(app)


@pytest.mark.asyncio
async def test_a_seat_retired_after_the_board_opened_leaves_it():
    rows = list(AGENTS)
    app = _roster_app(lambda: list(rows), board=_snapshot)
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        await pilot.pause()
        rows.remove(AGENTS[2])                  # gamma retired
        app._poll_board()
        await pilot.pause()
        await pilot.pause()
        assert "gamma" not in _tree_agents(app), _tree_agents(app)


@pytest.mark.asyncio
async def test_the_cursor_holds_its_seat_when_the_roster_shifts_under_it():
    """A new row at the FRONT renumbers every seat behind it. The detail pane
    resolves the selection through `roster_ix`, so a stale index does not blank
    the pane — it quietly describes the WRONG agent, which is worse.
    """
    rows = list(AGENTS)
    app = _roster_app(lambda: list(rows), board=_snapshot)
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        await pilot.pause()
        await _goto(app, pilot, "beta")
        assert "rm -rf" in _detail_text(app)
        rows.insert(0, {"agent": "aaa-new", "worktree": "/n", "klass": "squad"})
        app._poll_board()
        await pilot.pause()
        await pilot.pause()
        assert app.selected["agent"] == "beta"
        # the exact expression refresh_detail uses to find the seat's worktree
        assert app.agents[app.selected["roster_ix"]]["agent"] == "beta"
        assert "rm -rf" in _detail_text(app), "the pane followed the index, not the seat"


@pytest.mark.asyncio
async def test_a_roster_we_could_not_read_leaves_the_tree_alone():
    """None (the read failed) and [] (nothing is enrolled) are different
    answers. Collapsing them would let one unreadable tick empty the board."""
    def boom():
        raise OSError("squad.conf is busy")

    app = SettingsApp(list(AGENTS), scoped_to=None, model_for=_model_for,
                      squad_bin="/usr/bin/SQUAD", hub_bin="/usr/bin/HUB",
                      board_for=_snapshot, dark=None, poll_seconds=3600,
                      this_machine="thisbox", roster_for=boom)
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        await pilot.pause()
        assert app.is_running
        assert _tree_agents(app) == ["alpha", "beta", "gamma"], _tree_agents(app)


@pytest.mark.asyncio
async def test_an_emptied_roster_is_reported_rather_than_ignored():
    rows = list(AGENTS)
    app = _roster_app(lambda: list(rows), board=_snapshot)
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        await pilot.pause()
        rows.clear()
        app._poll_board()
        await pilot.pause()
        await pilot.pause()
        assert _tree_agents(app) == [], _tree_agents(app)
        assert app.is_running


@pytest.mark.asyncio
async def test_the_tick_runs_for_the_roster_alone_with_no_live_scan():
    """`board_for` is optional. Gating the tick on it alone would leave the
    roster frozen on any board opened without a scan.

    Nothing here calls `_poll_board()` — that is the whole point. The panel
    opens on a stale list and only the tick the app schedules for ITSELF can
    reconcile it, so a gate that skips scheduling fails here. The first version
    of this test pressed the poll by hand and passed with the gate reverted:
    a tick you fire yourself proves nothing about when the app fires one.
    """
    rows = [*AGENTS, {"agent": "delta", "worktree": "/d", "klass": "faculty"}]
    app = SettingsApp(list(AGENTS), scoped_to=None, model_for=_model_for,
                      squad_bin="/usr/bin/SQUAD", hub_bin="/usr/bin/HUB",
                      board_for=None, dark=None, poll_seconds=3600,
                      this_machine="thisbox", roster_for=lambda: list(rows))
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        await pilot.pause()
        assert "delta" in _tree_agents(app), _tree_agents(app)


@pytest.mark.asyncio
async def test_reload_re_reads_the_roster_too():
    """`r` is what the operator reaches for when the board looks wrong. It
    refreshed the scan and the registry but not the roster, so the one key that
    means "show me what is actually there" was the one that could not."""
    rows = list(AGENTS)
    app = SettingsApp(list(rows), scoped_to=None, model_for=_model_for,
                      squad_bin="/usr/bin/SQUAD", hub_bin="/usr/bin/HUB",
                      board_for=None, dark=None, poll_seconds=3600,
                      this_machine="thisbox", roster_for=lambda: list(rows))
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        await pilot.pause()
        rows.append({"agent": "delta", "worktree": "/d", "klass": "faculty"})
        await pilot.press("r")
        await pilot.pause()
        await pilot.pause()
        assert "delta" in _tree_agents(app), _tree_agents(app)


# ---- containers in the tree ------------------------------------------------

CONTAINER_SEATS = [{
    "identity": "mcp-hub-seat-thisbox", "machine": "thisbox", "repo": "",
    "folder": "", "launch_args": "", "class": "squad", "cloned_from": "",
    "spec": {"image": "mcp-hub-seat:latest",
             "volumes": ["/home/me/work/seat:/home/seat/work"],
             "memory_volume": "seat-memory:/home/seat/.claude"},
}]

SEAT_ROSTER = [
    {"agent": "alpha", "worktree": "/a", "klass": "squad"},
    {"agent": "mcp-hub-seat-thisbox", "worktree": "/home/me/work/seat",
     "klass": "faculty"},
]


# The ORDINARY case: the container is there and doing what was asked. Without
# a placement every row would wear `no placement`, which is a real state but
# not the one most of these tests are about.
HEALTHY_PLACEMENT = [{
    "id": "pl-ok", "seat": "mcp-hub-seat-thisbox", "machine": "thisbox",
    "substrate": "docker", "desired": "running", "status": "converged",
    "observed": {"state": "running", "at": 1.0,
                 "enumeration": {"container": "mcp-hub-seat-thisbox",
                                 "alive": True, "exists": True,
                                 "image_matches": True}},
}]


def _seat_app(seats=CONTAINER_SEATS, roster=SEAT_ROSTER,
              placements=HEALTHY_PLACEMENT):
    return SettingsApp(list(roster), scoped_to=None, model_for=_model_for,
                       squad_bin="/usr/bin/SQUAD", hub_bin="/usr/bin/HUB",
                       board_for=None, dark=None, poll_seconds=3600,
                       this_machine="thisbox",
                       workspaces_for=lambda: {"rows": [], "machines": ["thisbox"],
                                               "this_machine": "thisbox", "note": ""},
                       seats_for=lambda: list(seats),
                       placements_for=lambda: list(placements))


def _node_for(app, pred):
    for n in app._all_nodes():
        if pred(n.data or {}):
            return n
    return None


@pytest.mark.asyncio
async def test_a_container_gets_its_own_row_marked_as_a_container():
    app = _seat_app()
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        await pilot.pause()
        node = _node_for(app, lambda d: d.get("kind") == "container")
        assert node is not None, "no container row in the tree"
        label = node.label.plain
        assert "🐳" in label and "container" in label, label
        # The IMAGE is NOT on the row: it costs 20 cells in a 52-cell panel and
        # a Tree clips silently. It belongs to the detail pane, which the
        # container-detail test asserts.
        assert "mcp-hub-seat:latest" not in label, label


@pytest.mark.asyncio
async def test_the_container_row_does_not_claim_to_be_a_machine():
    """It has no token, no edge and no ssh. A row reading `· remote` like a box
    would invite `transport --host <the container>`, which can never work."""
    app = _seat_app()
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        await pilot.pause()
        label = _node_for(app, lambda d: d.get("kind") == "container").label.plain
        assert "this machine" not in label and "remote" not in label, label


@pytest.mark.asyncio
async def test_selecting_a_container_describes_it_rather_than_saying_nothing():
    """Every new node kind that `refresh_detail` does not know falls through to
    'nothing selected', which reads as a broken row."""
    from textual.widgets import Tree
    app = _seat_app()
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        await pilot.pause()
        node = _node_for(app, lambda d: d.get("kind") == "container")
        app.query_one("#fleet", Tree).move_cursor(node)
        await pilot.pause()
        await pilot.pause()
        text = _detail_text(app)
        assert "nothing selected" not in text, text
        assert "CONTAINER" in text
        assert "mcp-hub-seat:latest" in text
        # the way IN is the point: there is no sshd, so say what does work
        assert "docker exec" in text, text
        assert "/home/me/work/seat" in text, "the work mount is not shown"


@pytest.mark.asyncio
async def test_a_container_selection_survives_a_restructure():
    """`_identity` falls through to the machine case for any kind it does not
    name, so a container would be identified AS its host — and a restructure
    would move the cursor up a level while the pane kept describing a box."""
    app = _seat_app()
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        await pilot.pause()
        node = _node_for(app, lambda d: d.get("kind") == "container")
        app._move_to(node)
        await pilot.pause()
        ident = app._identity(app.selected)
        assert ident[0] == "container", ident
        assert ident != app._identity(
            _node_for(app, lambda d: d.get("kind") == "machine").data)


@pytest.mark.asyncio
async def test_a_seat_with_no_image_grows_no_container_row():
    plain = [{**CONTAINER_SEATS[0], "spec": {}}]
    app = _seat_app(seats=plain)
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        await pilot.pause()
        assert _node_for(app, lambda d: d.get("kind") == "container") is None


@pytest.mark.asyncio
async def test_no_tree_row_overflows_the_panel_it_is_drawn_in():
    """A Tree CLIPS rather than wraps, so an overlong label loses its tail with
    no sign that anything is missing — the operator read `container · mcp-hub`
    and could not know `-seat:latest` had been cut off.

    Asserted against the panel's real width and the real indent, for EVERY row
    the tree draws, so the next thing anyone hangs on a label is measured
    rather than eyeballed.
    """
    from rich.cells import cell_len
    from textual.widgets import Tree

    # A LOOSE ambiguous row is the widest thing the tree draws and was not in
    # this fixture: the guard passed while `💤⚡ mcp-hub-dev-vm-1  workspace
    # unknown · clones` overflowed on the real board. A width guard that never
    # sees the widest row is not a width guard.
    app = SettingsApp(
        list(SEAT_ROSTER), scoped_to=None, model_for=_model_for,
        squad_bin="/usr/bin/SQUAD", hub_bin="/usr/bin/HUB",
        board_for=None, dark=None, poll_seconds=3600, this_machine="thisbox",
        workspaces_for=lambda: {
            "machines": ["thisbox", "dev-vm-1"], "this_machine": "thisbox",
            "note": "", "rows": [
                {"name": "squad", "machine": "dev-vm-1",
                 "path": "/f/squad.code-workspace", "folders": 1, "error": "",
                 "on_disk": True, "open_now": False, "registered": True,
                 "squad": "", "listings": ["/code/org/mcp-hub"]},
                {"name": "general", "machine": "dev-vm-1",
                 "path": "/f/general.code-workspace", "folders": 1, "error": "",
                 "on_disk": True, "open_now": False, "registered": True,
                 "squad": "", "listings": ["/general/mcp-hub"]},
            ]},
        seats_for=lambda: list(CONTAINER_SEATS),
        placements_for=lambda: list(HEALTHY_PLACEMENT),
        fleet_for=lambda: {"ts": time.time(), "agents": [
            # The real ambiguous agent, at its real length. A synthetic longer
            # name would fail this guard for a row the fleet does not have,
            # and the honest limit is stated in the assertion below.
            {"name": "mcp-hub-dev-vm-1", "project": "org/mcp-hub",
             "wakeable": True, "idle": True, "sessions": 1, "next": ""}]},
    )
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        await pilot.pause()
        assert any("workspace unknown" in n.label.plain for n in app._all_nodes()), \
            "the widest row is not in this fixture"
        width = app.query_one("#fleet", Tree).size.width
        assert width > 0
        for node in app._all_nodes():
            # depth * guide_depth, plus the expand arrow the Tree draws itself
            depth, parent = 0, node.parent
            while parent is not None:
                depth += 1
                parent = parent.parent
            indent = node.tree.guide_depth * max(depth - 1, 0) + 2
            used = cell_len(node.label.plain) + indent
            assert used <= width, (
                f"{node.label.plain!r} needs {used} cells in {width}: "
                "the tail is silently clipped")


# ---- staleness must not leave a live claim standing ------------------------

def _stale_remote_app():
    """A remote agent the snapshot calls WAKEABLE, from a snapshot old enough
    to be not-reporting. Both facts come from the same file."""
    return SettingsApp(
        [], scoped_to=None, model_for=_model_for,
        squad_bin="/usr/bin/SQUAD", hub_bin="/usr/bin/HUB",
        board_for=None, dark=None, poll_seconds=3600, this_machine="thisbox",
        workspaces_for=lambda: {"rows": [], "machines": ["thisbox", "farbox"],
                                "this_machine": "thisbox", "note": ""},
        fleet_for=lambda: {"ts": 1.0, "agents": [        # ts=1 → ancient
            {"name": "pm-farbox", "project": "org/pm", "wakeable": True,
             "idle": True, "sessions": 1, "next": ""}]},
    )


@pytest.mark.asyncio
async def test_a_not_reporting_row_drops_its_wake_marker():
    """`⚠ ⚡ dreamteam-dev-vm-1  not reporting` said two contradictory things
    at once: we cannot see this agent, AND it is wakeable. The wake flag is
    read from the very snapshot the row has just called stale, so it is not a
    second source — it is the same dead cache asserting liveness.
    """
    app = _stale_remote_app()
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        await pilot.pause()
        row = _label(app, "pm-farbox")
        assert "not reporting" in row, row
        assert "⚡" not in row, f"a stale row still claims wakeable: {row}"


@pytest.mark.asyncio
async def test_a_FRESH_remote_row_keeps_its_wake_marker():
    """The guard above must not simply delete ⚡ from remote rows — that would
    pass the test by removing theclaim rather than by qualifying it."""
    app = SettingsApp(
        [], scoped_to=None, model_for=_model_for,
        squad_bin="/usr/bin/SQUAD", hub_bin="/usr/bin/HUB",
        board_for=None, dark=None, poll_seconds=3600, this_machine="thisbox",
        workspaces_for=lambda: {"rows": [], "machines": ["thisbox", "farbox"],
                                "this_machine": "thisbox", "note": ""},
        fleet_for=lambda: {"ts": time.time(), "agents": [
            {"name": "pm-farbox", "project": "org/pm", "wakeable": True,
             "idle": True, "sessions": 1, "next": ""}]},
    )
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        await pilot.pause()
        row = _label(app, "pm-farbox")
        assert "not reporting" not in row, row
        assert "⚡" in row, row


# ---- the panel is the operator's to size -----------------------------------

@pytest.mark.asyncio
async def test_the_tree_panel_widens_and_narrows_on_keys():
    from textual.widgets import Tree
    app = _seat_app()
    async with app.run_test(size=(160, 34)) as pilot:
        await pilot.pause()
        start = app.query_one("#fleet", Tree).size.width
        await pilot.press("]")
        await pilot.pause()
        wider = app.query_one("#fleet", Tree).size.width
        assert wider > start, (start, wider)
        await pilot.press("[")
        await pilot.press("[")
        await pilot.pause()
        assert app.query_one("#fleet", Tree).size.width < wider


@pytest.mark.asyncio
async def test_resizing_starts_from_the_width_the_panel_really_has():
    """The CSS width is a PERCENTAGE until the first keypress. Resizing from a
    remembered constant would make the first `]` on a wide terminal shrink the
    panel — the opposite of the key's name."""
    from textual.widgets import Tree
    app = _seat_app()
    async with app.run_test(size=(200, 34)) as pilot:
        await pilot.pause()
        # OUTER width throughout: that is what the resize writes, and mixing
        # it with `size` (the content box) is the drift this test exists for.
        start = app.query_one("#fleet", Tree).outer_size.width
        assert start > 52, f"a 200-col terminal should beat the floor: {start}"
        await pilot.press("]")
        await pilot.pause()
        assert app.query_one("#fleet", Tree).outer_size.width \
            == start + app.TREE_STEP, "the step drifted by the border/padding"


@pytest.mark.asyncio
async def test_a_wide_terminal_gives_the_tree_more_room_than_a_narrow_one():
    from textual.widgets import Tree
    widths = {}
    for cols in (120, 200):
        app = _seat_app()
        async with app.run_test(size=(cols, 34)) as pilot:
            await pilot.pause()
            widths[cols] = app.query_one("#fleet", Tree).size.width
    assert widths[200] > widths[120], widths
    # `size.width` is the CONTENT box; the CSS min-width of 52 is the border
    # box, so the floor shows up here as 52 minus border and padding.
    assert widths[120] >= 49, f"the old fixed width is the FLOOR: {widths}"


# ---- a container row reports what was OBSERVED, not what was declared -------
#
# The board drew `edge-probe-dev-vm-1` as a live substrate while the hub held
# `desired: reclaimed · observed: reclaimed · exists: false · converged` for
# it. The container node is built from the SEAT record — what may run — and the
# placement carrying the observation was attached to the node and never read.

def _placement(seat, desired="running", status="converged", state="running",
               **enum):
    e = {"container": seat, "alive": True, "exists": True}
    e.update(enum)
    return {"id": f"pl-{seat}", "seat": seat, "machine": "thisbox",
            "substrate": "docker", "desired": desired, "status": status,
            "observed": {"state": state, "at": 1.0, "enumeration": e}}


def _placed_app(placements, seats=CONTAINER_SEATS):
    return SettingsApp(
        list(SEAT_ROSTER), scoped_to=None, model_for=_model_for,
        squad_bin="/usr/bin/SQUAD", hub_bin="/usr/bin/HUB",
        board_for=None, dark=None, poll_seconds=3600, this_machine="thisbox",
        workspaces_for=lambda: {"rows": [], "machines": ["thisbox"],
                                "this_machine": "thisbox", "note": ""},
        seats_for=lambda: list(seats),
        placements_for=lambda: list(placements),
    )


def _container_row(app) -> str:
    for n in app._all_nodes():
        if (n.data or {}).get("kind") == "container":
            return n.label.plain
    raise AssertionError("no container row")


@pytest.mark.asyncio
async def test_a_reclaimed_container_does_not_read_as_a_live_one():
    seat = CONTAINER_SEATS[0]["identity"]
    app = _placed_app([_placement(seat, desired="reclaimed",
                                  state="reclaimed", exists=False, alive=False)])
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        await pilot.pause()
        row = _container_row(app)
        assert "reclaimed" in row, row
        assert "gone" in row, row


@pytest.mark.asyncio
async def test_a_container_nothing_has_scheduled_says_so():
    """A seat with no placement is a declaration nobody has acted on. Drawing
    it identically to a running one is the same lie in a quieter voice."""
    app = _placed_app([])
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        await pilot.pause()
        assert "no placement" in _container_row(app)


@pytest.mark.asyncio
async def test_a_container_running_the_wrong_image_says_stale_image():
    seat = CONTAINER_SEATS[0]["identity"]
    app = _placed_app([_placement(seat, image_matches=False)])
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        await pilot.pause()
        assert "stale image" in _container_row(app)


@pytest.mark.asyncio
async def test_a_container_asked_to_run_that_is_not_running_says_so():
    seat = CONTAINER_SEATS[0]["identity"]
    app = _placed_app([_placement(seat, desired="running", state="stopped")])
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        await pilot.pause()
        assert "not running" in _container_row(app)


@pytest.mark.asyncio
async def test_a_healthy_container_stays_QUIET():
    """Silence has to mean "seen, and as intended" — if a converged container
    also wore a qualifier, none of the qualifiers above would carry weight."""
    seat = CONTAINER_SEATS[0]["identity"]
    app = _placed_app([_placement(seat)])
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        await pilot.pause()
        row = _container_row(app)
        assert row.endswith("container"), row


@pytest.mark.asyncio
async def test_an_edge_that_never_reported_exists_is_not_called_reclaimed():
    """`exists` absent means the edge did not report the field; `exists: False`
    means it reported it absent. Truthiness collapses the two and would call an
    unreported container reclaimed."""
    seat = CONTAINER_SEATS[0]["identity"]
    pl = _placement(seat)
    del pl["observed"]["enumeration"]["exists"]
    app = _placed_app([pl])
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        await pilot.pause()
        assert "reclaimed" not in _container_row(app)


# ---- out-of-scope workspaces -----------------------------------------------

@pytest.mark.asyncio
async def test_a_workspace_this_board_is_not_scoped_to_says_so():
    app = SettingsApp(
        [], scoped_to="/w/windows.code-workspace", model_for=_model_for,
        squad_bin="/usr/bin/SQUAD", hub_bin="/usr/bin/HUB",
        board_for=None, dark=None, poll_seconds=3600, this_machine="thisbox",
        workspaces_for=lambda: {
            "machines": ["thisbox"], "this_machine": "thisbox", "note": "",
            "rows": [
                {"name": "windows", "machine": "thisbox",
                 "path": "/w/windows.code-workspace", "folders": 1, "error": "",
                 "on_disk": True, "open_now": False, "registered": True,
                 "squad": "", "listings": ["/w/win"]},
                {"name": "xport", "machine": "thisbox",
                 "path": "/w/xport.code-workspace", "folders": 1, "error": "",
                 "on_disk": True, "open_now": False, "registered": True,
                 "squad": "", "listings": ["/w/xp"]},
            ]},
    )
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        await pilot.pause()
        rows = {(n.data or {}).get("name"): n.label.plain
                for n in app._all_nodes() if (n.data or {}).get("kind") == "workspace"}
        assert "not in this board's scope" in rows["xport"], rows
        assert "not in this board's scope" not in rows["windows"], rows


@pytest.mark.asyncio
async def test_the_container_pane_explains_the_state_the_row_abbreviates():
    """The row says `reclaimed · gone` because that is all 49 cells hold. The
    pane is where the operator finds out what that means and who observed it."""
    from textual.widgets import Tree
    seat = CONTAINER_SEATS[0]["identity"]
    app = _placed_app([_placement(seat, desired="reclaimed", state="reclaimed",
                                  exists=False, alive=False)])
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        await pilot.pause()
        node = _node_for(app, lambda d: d.get("kind") == "container")
        app.query_one("#fleet", Tree).move_cursor(node)
        await pilot.pause()
        await pilot.pause()
        text = _detail_text(app)
        assert "enumerated" in text and "did not find" in text, text


@pytest.mark.asyncio
async def test_out_of_scope_does_not_steal_the_open_now_mark():
    """A workspace someone ELSE has open, that this board is not scoped to, is
    both things at once. Written first as a branch in the label's cascade, the
    scope note replaced the open-now colour — so a workspace with an operator
    in it read as an unremarkable one. Caught by the existing workspace-view
    suite, not by anything I wrote."""
    app = SettingsApp(
        [], scoped_to="/w/windows.code-workspace", model_for=_model_for,
        squad_bin="/usr/bin/SQUAD", hub_bin="/usr/bin/HUB",
        board_for=None, dark=None, poll_seconds=3600, this_machine="thisbox",
        workspaces_for=lambda: {
            "machines": ["thisbox"], "this_machine": "thisbox", "note": "",
            "rows": [
                {"name": "windows", "machine": "thisbox",
                 "path": "/w/windows.code-workspace", "folders": 1, "error": "",
                 "on_disk": True, "open_now": True, "registered": True,
                 "squad": "", "listings": ["/w/win"]},
                {"name": "elsewhere", "machine": "thisbox",
                 "path": "/w/elsewhere.code-workspace", "folders": 1,
                 "error": "", "on_disk": True, "open_now": True,
                 "registered": True, "squad": "", "listings": ["/w/el"]},
            ]},
    )
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        await pilot.pause()
        node = next(n for n in app._all_nodes()
                    if (n.data or {}).get("name") == "elsewhere")
        label = node.label.plain
        assert label.startswith("● elsewhere"), label     # still marked OPEN
        assert "not in this board's scope" in label, label
        styles = {s.style for s in node.label.spans if s.style}
        assert any(app._palette()["success"] in str(s) for s in styles), styles


@pytest.mark.asyncio
async def test_an_ambiguous_remote_row_says_WHY_it_has_no_workspace():
    """Moving the agent out of the workspaces is only half the fix. A row that
    simply appears under the machine looks like an agent nobody filed, not one
    whose workspace cannot be known — so the reason rides the row.

    (The refusal itself is tested in test_fleet_tree; this is the sentence.)
    """
    def _wsrow(name, path, listing):
        return {"name": name, "machine": "farbox", "path": path, "folders": 1,
                "error": "", "on_disk": True, "open_now": False,
                "registered": True, "squad": "", "listings": [listing]}

    app = SettingsApp(
        [], scoped_to=None, model_for=_model_for,
        squad_bin="/usr/bin/SQUAD", hub_bin="/usr/bin/HUB",
        board_for=None, dark=None, poll_seconds=3600, this_machine="thisbox",
        workspaces_for=lambda: {
            "machines": ["thisbox", "farbox"], "this_machine": "thisbox",
            "note": "", "rows": [
                _wsrow("squad", "/f/squad.code-workspace", "/code/org/mcp-hub"),
                _wsrow("general", "/f/general.code-workspace", "/general/mcp-hub"),
            ]},
        fleet_for=lambda: {"ts": time.time(), "agents": [
            {"name": "mcp-hub-farbox", "project": "org/mcp-hub",
             "wakeable": True, "idle": True, "sessions": 1, "next": ""}]},
    )
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        await pilot.pause()
        row = _label(app, "mcp-hub-farbox")
        assert "workspace unknown" in row, row
        assert "hub only" not in row, "the reason was overwritten by the state"


# ---- off-hub rows: which of them actually MATTER --------------------------
#
# Making a roster-only agent visible turned every enrolled folder on a box into
# a warning: dev-vm-1 raised twenty, of which exactly one was actionable. A
# warning on twenty rows is a warning on none — the same defect as the row
# being invisible, only louder.

def _offhub_app(agents):
    return SettingsApp(
        [], scoped_to=None, model_for=_model_for,
        squad_bin="/usr/bin/SQUAD", hub_bin="/usr/bin/HUB",
        board_for=None, dark=None, poll_seconds=3600, this_machine="thisbox",
        workspaces_for=lambda: {
            "machines": ["thisbox", "farbox"], "this_machine": "thisbox",
            "note": "", "rows": []},
        fleet_for=lambda: {"ts": time.time(), "agents": []},
        machine_agents_for=lambda: {"farbox": agents},
    )


async def test_a_comms_agent_that_is_UP_but_off_the_hub_warns():
    """The one actionable shape: launched to reach the hub, no longer reaching
    it. That is what a redeploy leaves behind."""
    app = _offhub_app([{"agent": "armed-farbox", "worktree": "/w/a",
                        "comms": True, "running": True}])
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        await pilot.pause()
        row = _label(app, "armed-farbox")
        assert "running, not on hub" in row, row
        assert "⚠" in row, row


async def test_a_STOPPED_comms_agent_does_not_warn():
    """Stopped is not a fault. It is the ordinary state of most of a box."""
    app = _offhub_app([{"agent": "down-farbox", "worktree": "/w/d",
                        "comms": True, "running": False}])
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        await pilot.pause()
        row = _label(app, "down-farbox")
        assert "stopped" in row, row
        assert "⚠" not in row, row


async def test_a_SCRATCH_folder_never_warns_even_while_running():
    """No comms flag means it was never going to be on the hub — `squad
    add-folder` omits the flag deliberately, because it is inert without a hub
    identity. Warning about its absence would be warning about a decision."""
    app = _offhub_app([{"agent": "scratch-farbox", "worktree": "/w/s",
                        "comms": False, "running": True}])
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        await pilot.pause()
        row = _label(app, "scratch-farbox")
        assert "⚠" not in row, row
        assert "not on hub" not in row, row


async def test_an_edge_that_never_reported_liveness_keeps_the_weaker_line():
    """Absent `running` is UNKNOWN. Reading it as either state would be a
    claim about a box whose edge has not answered yet."""
    app = _offhub_app([{"agent": "old-farbox", "worktree": "/w/o",
                        "comms": True}])
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        await pilot.pause()
        row = _label(app, "old-farbox")
        assert "not on hub" in row and "running," not in row, row


# ---- a workspace whose seats are all in containers -------------------------

def _ws_label(app, name: str) -> str:
    """One workspace's rendered row, as the operator reads it."""
    for node in app._all_nodes():
        data = node.data or {}
        if data.get("kind") == "workspace" and data.get("name") == name:
            return node.label.plain
    raise AssertionError(f"workspace {name} is not in the tree")


def _capsule_app():
    """`capsule`: one workspace, one containerized seat inside its folders.

    The container correctly claims the agent, so before the note this row
    drew bare — a workspace that looks like it holds nothing while its seat
    is one node below it on the same board.
    """
    return SettingsApp(
        list(SEAT_ROSTER), scoped_to=None, model_for=_model_for,
        squad_bin="/usr/bin/SQUAD", hub_bin="/usr/bin/HUB",
        board_for=None, dark=None, poll_seconds=3600, this_machine="thisbox",
        workspaces_for=lambda: {
            "machines": ["thisbox"], "this_machine": "thisbox", "note": "",
            "rows": [
                {"name": "capsule", "machine": "thisbox",
                 "path": "/f/capsule.code-workspace", "folders": 1, "error": "",
                 "on_disk": True, "open_now": False, "registered": True,
                 "squad": "", "listings": ["/home/me/work"]},
                {"name": "plain", "machine": "thisbox",
                 "path": "/f/plain.code-workspace", "folders": 1, "error": "",
                 "on_disk": True, "open_now": False, "registered": True,
                 "squad": "", "listings": ["/a"]},
            ]},
        seats_for=lambda: list(CONTAINER_SEATS),
        fleet_for=lambda: {"ts": time.time(), "agents": []},
    )


@pytest.mark.asyncio
async def test_a_workspace_says_how_many_of_its_seats_are_in_containers():
    """The `capsule` row. Its seat runs in a container and is shown there —
    correctly — which left this row with no children at all. An empty row is
    read as "no agents", and that is a measurement nobody took."""
    app = _capsule_app()
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        await pilot.pause()
        row = _ws_label(app, "capsule")
        assert "1 in containers" in row, row


@pytest.mark.asyncio
async def test_a_workspace_with_no_containers_says_nothing_about_them():
    """The note appears only where it is true. A count of zero on every
    ordinary workspace is noise that trains the operator to skip the tail."""
    app = _capsule_app()
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        await pilot.pause()
        row = _ws_label(app, "plain")
        assert "in containers" not in row, row
