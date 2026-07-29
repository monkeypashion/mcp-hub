"""The board half of the Squad Board panel, driven by clicking (Pilot), and
the data/theme layers under it.

Same discipline as test_settings_app.py: every previous presentation was
"tested" by rendering it and looking, which is how six of them shipped defects
the first click found.
"""
from __future__ import annotations

import json
import stat

import pytest

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
                      board_for=board, dark=dark, poll_seconds=3600)
    if ran is not None:
        def apply(exe, argv, label, value):
            ran.append((exe, argv))
            app.call_from_thread(app._after_apply, f"{label} → {value}")
        app._apply = apply
    return app


# ---- the live roster and detail ----

@pytest.mark.asyncio
async def test_the_roster_wears_the_board_state():
    app = _app(board=_snapshot)
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        await pilot.pause()
        line0 = app.query_one("#live-0").render()
        line1 = app.query_one("#live-1").render()
        assert "Fable" in str(line0) and "working" in str(line0)
        assert "waiting" in str(line1) and "2m" in str(line1)
        assert "🙋" in str(line1)                       # the hand rides the roster
        # an agent the scan doesn't know keeps its class line, not garbage
        assert str(app.query_one("#live-2").render()) == "faculty"
        # the fleet summary took over the subtitle
        assert "need you" in app.sub_title


@pytest.mark.asyncio
async def test_the_live_section_shows_the_blocking_question_with_answers():
    app = _app(board=_snapshot)
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        items = app.query("#agents > ListItem")
        await pilot.click(items[1])                    # beta, the waiting one
        await pilot.pause()
        await pilot.pause()
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
        items = app.query("#agents > ListItem")
        await pilot.click(items[1])
        await pilot.pause()
        await pilot.pause()
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
        items = app.query("#agents > ListItem")
        await pilot.click(items[1])
        await pilot.pause()
        await pilot.pause()
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
        sel = app.query("Select").first()             # settings still editable
        assert sel is not None


@pytest.mark.asyncio
async def test_n_jumps_to_the_agent_that_needs_you():
    app = _app(board=_snapshot)
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        await pilot.pause()
        assert app.agent_ix == 0
        await pilot.press("n")
        await pilot.pause()
        assert app.agents[app.agent_ix]["agent"] == "beta"


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
        # click is pure scheduling — and it is the difference between the live
        # section rendering None and rendering beta, i.e. a legitimately
        # different render key. Racing it made this test fail ~50% of runs.
        for _ in range(50):
            await pilot.pause()
            if "beta" in (app.board.get("agents") or {}):
                break
        assert "beta" in (app.board.get("agents") or {}), "board data never arrived"
        items = app.query("#agents > ListItem")
        await pilot.click(items[1])                    # beta, the waiting one
        await pilot.pause()
        await pilot.pause()
        key_after_select = app._live_key
        assert key_after_select is not None, "live section never rendered beta"
        for _ in range(6):                             # several poll applications
            app._apply_board(snapshot())
            await pilot.pause()
        assert app.is_running                          # the crash, pinned
        buttons = list(app.query("Button"))
        assert len(buttons) == 3, f"{len(buttons)} buttons — duplicate mounts survived"
        # The RENDER KEY, not the _gen counter. _gen is bumped by every
        # rebuild from any source, including the mount-time poll whose worker
        # lands at a nondeterministic point relative to the click — keying the
        # assertion on it made this test fail ~50% of runs on the very commit
        # it protects, and I nearly blamed an innocent merge for it
        # (2026-07-29 night). The invariant that actually matters is that a
        # sub-minute tick does not CHANGE WHAT WOULD BE RENDERED, so nothing
        # under the operator's pointer is torn down.
        assert app._live_key == key_after_select, \
            "sub-minute timer ticks changed the live render key"


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
        await pilot.click(app.query("#agents > ListItem")[1])   # beta, waiting
        await pilot.pause()
        for _ in range(8):
            app._apply_board(snapshot())
            await pilot.pause()
            await pilot.pause()
        assert app.is_running, "the panel died while rebuilding the live section"
        buttons = [str(b.label) for b in app.query("Button")]
        assert buttons == ["yes", "no", "always"], \
            f"{buttons} — duplicate or lost mounts across rebuilds"
