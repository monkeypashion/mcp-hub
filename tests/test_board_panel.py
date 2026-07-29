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
        # Seconds advance every scan; the rendered "2m" must not. MODULO is
        # load-bearing: `134 + tick["n"]` crossed 180s → "3m" once ~46 ticks
        # accumulated, which the 0.05s timer reaches on a loaded box but not
        # a fast one — the section then rebuilt LEGITIMATELY and the test
        # called it churn (7/12 failures on dev-vm-1, 0/12 here, same
        # commit; measured 2026-07-30). Staying inside one displayed minute
        # makes the assertion stronger too: ANY number of ticks now proves
        # no rebuild, instead of only the few that fit in the window.
        snap["agents"]["beta"]["waiting_seconds"] = 134 + (tick["n"] % 45)
        return snap

    app = _app(board=snapshot)
    app._poll_seconds = 0.05
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        items = app.query("#agents > ListItem")
        await pilot.click(items[1])                    # beta, the waiting one
        await pilot.pause()
        gen_after_select = app._gen
        for _ in range(6):                             # several poll applications
            app._apply_board(snapshot())
            await pilot.pause()
        assert app.is_running                          # the crash, pinned
        buttons = list(app.query("Button"))
        assert len(buttons) == 3, f"{len(buttons)} buttons — duplicate mounts survived"
        assert app._gen == gen_after_select, \
            "sub-minute timer ticks rebuilt the live section under the pointer"
