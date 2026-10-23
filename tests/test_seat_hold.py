"""Bar 14/42 stop lever: a hold that actually holds, and releases itself.

Three legs, and the middle one is the whole point:
  1. the hub records the hold (`until` and `release_condition` both required);
  2. squad's sweeps HONOUR it — lane lifecycle is squad's, `heal` runs every
     ~2 min and `up` starts any down squad-class lane, so a hold the sweeps
     ignore is undone inside two minutes while the console goes on announcing
     the lane as stopped;
  3. the edge mirrors the hub's holds to a local file so leg 2 needs no
     network on its hot path.

The direction of failure is fixed and deliberate: squad FAILS OPEN. A missing
or unreadable mirror means nothing is held, because an enforcement mechanism
that fails closed converts its own outage into an outage of the whole fleet.
The cost is that a dead edge un-holds lanes, which is why every entry also
carries its own expiry.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from mcp_hub import edge

SQUAD = Path(__file__).resolve().parents[1] / "squad" / "squad"


def _squad(tmp_path) -> Path:
    """squad, copied into tmp_path (declared base test edit, v2f r10 m3).

    `hub_bin` prefers `<script dir>/../.venv/bin/mcp-hub`, so run from a checkout that has a .venv, `squad start`
    called the REAL cli's `placements list` / `placements set`, which talk to the live hub. A copy has no .venv
    beside it, and `_stub_bin` puts a stub `mcp-hub` on PATH that only records its arguments."""
    dst = tmp_path / "checkout" / "squad" / "squad"
    if not dst.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(SQUAD, dst)
    return dst


# --- leg 3: the edge's view of "is this seat held" -------------------------

def act(id_, kind, status="done", until=None, cond="window resets at 18:00"):
    args = {}
    if kind == "hold":
        args = {"until": until, "release_condition": cond, "reason": "over share"}
    return {"id": id_, "kind": kind, "status": status, "args": args}


def test_a_done_hold_in_the_future_is_live():
    st = edge._hold_state([act(1, "hold", until=time.time() + 600)], time.time())
    assert st and st["release_condition"] == "window resets at 18:00"


def test_an_expired_hold_has_released_itself():
    assert edge._hold_state(
        [act(1, "hold", until=time.time() - 1)], time.time()
    ) is None


def test_a_later_release_ends_an_earlier_hold():
    actions = [act(1, "hold", until=time.time() + 600), act(2, "release")]
    assert edge._hold_state(actions, time.time()) is None


def test_a_later_hold_outranks_an_earlier_release():
    actions = [act(1, "release"), act(2, "hold", until=time.time() + 600)]
    assert edge._hold_state(actions, time.time()) is not None


@pytest.mark.parametrize("status", ["pending", "failed", "refused", "superseded"])
def test_only_a_hold_the_edge_CARRIED_OUT_counts(status):
    # A pending hold is an ASK. Enforcing an ask would stop a lane nobody has
    # confirmed stopping — and the console would announce it as held.
    assert edge._hold_state(
        [act(1, "hold", status=status, until=time.time() + 600)], time.time()
    ) is None


def test_the_mirror_is_rebuilt_not_merged(tmp_path, monkeypatch):
    # Release must be expressible. If the file accumulated, a lifted hold
    # would persist forever — a hold nothing can end.
    f = tmp_path / "held.json"
    monkeypatch.setenv("MCP_HUB_HELD_FILE", str(f))
    edge.write_held_mirror({"lane-a": {"until": time.time() + 60}})
    assert "lane-a" in json.loads(f.read_text())["held"]
    edge.write_held_mirror({})
    assert json.loads(f.read_text())["held"] == {}


def test_the_mirror_write_is_atomic(tmp_path, monkeypatch):
    # squad may read at any instant; a half-written file is a vanished hold.
    f = tmp_path / "held.json"
    monkeypatch.setenv("MCP_HUB_HELD_FILE", str(f))
    assert edge.write_held_mirror({"lane-a": {"until": time.time() + 60}})
    assert json.loads(f.read_text())["held"]["lane-a"]
    assert not list(tmp_path.glob("*.tmp")), "temp file left behind"


def test_an_unwritable_mirror_reports_false(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_HUB_HELD_FILE", str(tmp_path / "nope" / "x" / "h.json"))
    monkeypatch.setattr(Path, "mkdir", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    assert edge.write_held_mirror({}) is False


# --- leg 2: squad honours it (the leg that makes the stop real) ------------

def _stub_bin(tmp_path):
    """A fake `tmux` on PATH, so a start that is NOT held stays inert.

    🔴 These tests run `squad start` for real, and four of them exist
    precisely to prove the hold does NOT block — so squad went on to launch.
    `tm` resolves tmux through PATH and $SOCK defaults to the shared `squad`
    socket, so every such run created a REAL tmux session named `lane-a`,
    beside the live fleet, holding a `claude --continue` sitting on the
    trust-this-folder prompt in a pytest tmpdir. The operator's deputy killed
    one twice on 2026-09-02 before it was traced here; its cwd was by then a
    DELETED tmpdir, which is how a passing suite left a stray seat behind.

    has-session exits 1 so squad believes the lane is down and takes the
    start path under test; everything else succeeds and does nothing.
    """
    bindir = tmp_path / "stubbin"
    bindir.mkdir(exist_ok=True)
    stub = bindir / "tmux"
    stub.write_text(
        '#!/bin/sh\n'
        'for a in "$@"; do\n'
        '  [ "$a" = "has-session" ] && exit 1\n'
        'done\n'
        'exit 0\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)
    hub = bindir / "mcp-hub"
    hub.write_text(f'#!/bin/sh\necho "$@" >> {tmp_path}/hub-calls.log\nexit 0\n', encoding="utf-8")
    hub.chmod(0o755)
    return bindir


def _env(tmp_path, conf, heldf):
    return {"PATH": f"{_stub_bin(tmp_path)}:/usr/bin:/bin", "HOME": str(tmp_path),
            "SQUAD_CONF": str(conf), "MCP_HUB_HELD_FILE": str(heldf)}


def run_squad(tmp_path, held: dict | None, agent="lane-a", verb="start"):
    conf = tmp_path / "squad.conf"
    conf.write_text(f"{agent}|{tmp_path}||--continue|squad\n", encoding="utf-8")
    heldf = tmp_path / "held.json"
    if held is not None:
        heldf.write_text(json.dumps({"generated": time.time(), "held": held}),
                         encoding="utf-8")
    return subprocess.run(
        ["bash", str(_squad(tmp_path)), verb, agent],
        capture_output=True, text=True,
        env=_env(tmp_path, conf, heldf),
    )


# --- a hand may start a held lane; heal may not (declared base test edit, v2f r10)
#
# His ruling, 2026-09-14: a hold "should not prevent claude from starting, just
# stop lanes from working". The test this replaces asserted the OLD refusal and
# went stale when e42c80e implemented the ruling; mcp-hub ruled 2026-10-18 that
# the code wins. The exit code is not asserted: in this stubbed environment an
# UNHELD start exits 1 as well, so it measures the stub, not the hold.

@pytest.mark.skipif(not Path("/usr/bin/jq").exists(), reason="needs jq")
def test_a_hand_start_of_a_held_lane_says_so_and_proceeds(tmp_path):
    until = time.time() + 3600
    p = run_squad(tmp_path, {"lane-a": {
        "until": until, "release_condition": "window resets at 18:00"}})
    assert "HELD" in p.stdout, p.stdout
    assert "window resets at 18:00" in p.stdout, "the release condition must be shown"
    assert "starting it anyway" in p.stdout, p.stdout
    # ...and it PROCEEDS: the announcement alone, followed by a return, is
    # the old refusal with a kinder sentence.
    assert "started lane-a" in p.stdout, p.stdout
    marker = tmp_path / ".mcp-hub" / "hold-hand-started-lane-a"
    assert marker.exists(), "the hand's start left no marker"
    stored = marker.read_text().strip()
    # THIS hold's expiry, not a bare flag: a flag would disarm every later
    # hold on the lane, the expiry dies with the hold it was written against.
    assert abs(float(stored) - until) < 1e-3, (stored, until)


def _hold_harness():
    # The sourcing harness of the hold-enforcement suite: squad's helpers in
    # scope with a recording tmux, so heal's door and the enforcer can be
    # called directly. Imported, not copied, so the two files cannot drift.
    from tests.test_hold_enforcement_squad import call, harness, held_entry
    return call, harness, held_entry


def test_heal_still_may_not_relaunch_a_held_lane(tmp_path):
    """The contrast: the hand's door opened, heal's did not. `relaunch_agent`
    is heal's restart; a held lane must stay down under it."""
    call, harness, held_entry = _hold_harness()
    h = harness(tmp_path, held={"lane-a": held_entry()}, running=False)
    p = call(*h, "relaunch_agent lane-a")
    assert "heal will not relaunch it" in p.stdout, p.stdout
    assert "starting it anyway" not in p.stdout, p.stdout
    log = tmp_path / "tmux.log"
    assert "respawn-pane" not in (log.read_text() if log.exists() else "")
    assert not (tmp_path / ".mcp-hub" / "hold-hand-started-lane-a").exists()


def test_a_hand_start_under_an_earlier_hold_does_not_cover_a_new_one(tmp_path):
    """The contrast that keeps one `up` from disarming every future brake: a
    lane hand-started under hold A, then held again (hold B, a different
    expiry), is stopped by the enforcer at its boundary as if never started."""
    call, harness, held_entry = _hold_harness()
    h = harness(tmp_path, held={"lane-a": held_entry(until_in=1800)},
                running=True, boundary=True)
    call(*h, "up_one lane-a")                      # the hand, under hold A
    heldf = tmp_path / "held.json"
    heldf.write_text(json.dumps({"generated": time.time(),
                                 "held": {"lane-a": held_entry(until_in=3600)}}))
    p = call(*h, "hold_enforce_one lane-a")        # hold B is now the hold
    assert "left up" not in p.stdout, p.stdout
    assert "kill-session" in (tmp_path / "tmux.log").read_text(), p.stdout


@pytest.mark.skipif(not Path("/usr/bin/jq").exists(), reason="needs jq")
def test_an_expired_hold_does_not_stop_a_start(tmp_path):
    p = run_squad(tmp_path, {"lane-a": {
        "until": time.time() - 5, "release_condition": "gone"}})
    assert "HELD" not in p.stdout


@pytest.mark.skipif(not Path("/usr/bin/jq").exists(), reason="needs jq")
def test_a_hold_on_a_DIFFERENT_lane_does_not_leak(tmp_path):
    p = run_squad(tmp_path, {"someone-else": {
        "until": time.time() + 3600, "release_condition": "x"}})
    assert "HELD" not in p.stdout


def test_no_mirror_file_means_nothing_is_held(tmp_path):
    # FAILS OPEN. A dead edge must not be able to freeze the fleet.
    p = run_squad(tmp_path, None)
    assert "HELD" not in p.stdout


@pytest.mark.skipif(not Path("/usr/bin/jq").exists(), reason="needs jq")
def test_a_corrupt_mirror_means_nothing_is_held(tmp_path):
    conf = tmp_path / "squad.conf"
    conf.write_text(f"lane-a|{tmp_path}||--continue|squad\n", encoding="utf-8")
    heldf = tmp_path / "held.json"
    heldf.write_text("{not json at all", encoding="utf-8")
    p = subprocess.run(
        ["bash", str(_squad(tmp_path)), "start", "lane-a"],
        capture_output=True, text=True,
        env=_env(tmp_path, conf, heldf),
    )
    assert "HELD" not in p.stdout


# --- leg 0: the hop that never happened ------------------------------------
#
# Everything above builds actions with `status="done"` by hand, which is the
# state the mirror keys on — so the suite could be fully green while the one
# transition that produces that state did not exist. It did not: the hub
# wrote `hold` as PENDING, the edge's verb set was ("interrupt", "prompt"),
# so `realize_seat_action` refused it as unrecognised, it never settled, and
# `_hold_state` (which counts only `done`) therefore never reported a hold.
# The mirror was rebuilt empty every pass and squad held nothing, on every
# lane, from the moment 6490b1a shipped.
#
# The tests below exercise the real transition end to end, and pin the two
# verb sets together so the vocabularies cannot drift apart again in silence.

def test_the_hub_and_the_edge_agree_on_the_verb_set():
    """A verb the hub can WRITE and the edge cannot EXECUTE is inert.

    The two guards stay independent — that is deliberate, and this does not
    merge them. But the vocabulary is one fact, and when it split, `hold`
    was accepted, validated, stored, and silently never carried out.
    """
    from mcp_hub.api_v1 import SEAT_PHASE1_VERBS

    assert set(SEAT_PHASE1_VERBS) == set(edge._SEAT_PHASE1_VERBS)


@pytest.mark.parametrize("kind", ["hold", "release"])
def test_a_control_verb_settles_done_through_the_edge(kind):
    """The transition the mirror depends on, taken through the real code."""
    action = {"id": 7, "kind": kind, "status": "pending",
              "args": {"until": time.time() + 3600,
                       "release_condition": "window resets at 18:00"}}
    report = edge.realize_seat_action(
        action, {"identity": "lane-1", "session": "lane-1"},
        lambda argv: (0, "pane"), pause=lambda _s: None,
    )
    assert report["status"] == "done", report["observed"]


def test_a_control_verb_sends_no_keystrokes():
    """A hold must not type into the lane it is stopping."""
    calls = []

    def runner(argv):
        calls.append(argv)
        return 0, "pane"

    edge.realize_seat_action(
        {"id": 8, "kind": "hold", "status": "pending",
         "args": {"until": time.time() + 3600, "release_condition": "x"}},
        {"identity": "lane-1", "session": "lane-1"}, runner,
        pause=lambda _s: None,
    )
    assert calls == []


def test_a_hold_survives_a_pane_that_cannot_be_read():
    """The wedged lane is the one most worth holding.

    Fail-closed guards a BLIND KEYSTROKE. A control verb sends none, so
    gating it on a readable pane would refuse a hold exactly when the lane
    is stuck — which is when the ceiling watcher reaches for it.
    """
    report = edge.realize_seat_action(
        {"id": 9, "kind": "hold", "status": "pending",
         "args": {"until": time.time() + 3600, "release_condition": "x"}},
        {"identity": "lane-1", "session": "lane-1"},
        lambda argv: (1, "no server running"), pause=lambda _s: None,
    )
    assert report["status"] == "done"


def test_a_keystroke_verb_still_fails_closed_on_an_unreadable_pane():
    """Negative control: the split must not have loosened the older rule."""
    report = edge.realize_seat_action(
        {"id": 10, "kind": "prompt", "status": "pending",
         "args": {"text": "hello"}},
        {"identity": "lane-1", "session": "lane-1"},
        lambda argv: (1, "no server running"), pause=lambda _s: None,
    )
    assert report["status"] == "refused"


def test_a_pending_hold_reaches_the_mirror_in_one_pass(tmp_path, monkeypatch):
    """End to end: what the hub actually writes, through to what squad reads.

    This is the test whose absence let an inert hold ship green.
    """
    mirror = tmp_path / "held-lanes.json"
    monkeypatch.setenv("MCP_HUB_HELD_FILE", str(mirror))
    until = time.time() + 3600

    class FakeApi:
        def __init__(self):
            # exactly what the hub stores on POST .../actions {kind: hold}
            self.actions = [{"id": 1, "kind": "hold", "status": "pending",
                             "args": {"until": until,
                                      "release_condition": "window resets 18:00",
                                      "reason": "over 1.5x fair share"}}]

        def pull_seat_actions(self, seat):
            return self.actions

        def report_seat_action(self, seat, action_id, report):
            for a in self.actions:
                if a["id"] == action_id:
                    a["status"] = report["status"]

        def seat_watched(self, seat):
            return False

    out = edge.seat_control_pass(
        FakeApi(), [{"seat": "lane-1", "desired": "running"}],
        lambda argv: (0, "pane"),
    )

    assert out["errors"] == []
    assert out["held"] == ["lane-1"]
    written = json.loads(mirror.read_text())
    assert written["held"]["lane-1"]["release_condition"] == "window resets 18:00"


def test_a_pending_release_lifts_the_hold_in_the_same_pass(tmp_path, monkeypatch):
    """Release has to settle too, or the only way out is the expiry."""
    mirror = tmp_path / "held-lanes.json"
    monkeypatch.setenv("MCP_HUB_HELD_FILE", str(mirror))
    until = time.time() + 3600

    class FakeApi:
        def __init__(self):
            self.actions = [
                {"id": 1, "kind": "hold", "status": "done",
                 "args": {"until": until, "release_condition": "c"}},
                {"id": 2, "kind": "release", "status": "pending", "args": {}},
            ]

        def pull_seat_actions(self, seat):
            return self.actions

        def report_seat_action(self, seat, action_id, report):
            for a in self.actions:
                if a["id"] == action_id:
                    a["status"] = report["status"]

        def seat_watched(self, seat):
            return False

    out = edge.seat_control_pass(
        FakeApi(), [{"seat": "lane-1", "desired": "running"}],
        lambda argv: (0, "pane"),
    )
    assert out["held"] == []
    assert json.loads(mirror.read_text())["held"] == {}


def test_squad_start_reaches_only_the_stub_hub(tmp_path):
    """Hermetic guard (declared base test edit, v2f r10 m3): `squad start` asks the hub for this seat's
    placement. It must ask the stub on PATH, never a cli next to the script."""
    run_squad(tmp_path, None)
    log = tmp_path / "hub-calls.log"
    assert log.exists() and "placements list" in log.read_text(), "squad start did not reach the stub hub"
    assert not (_squad(tmp_path).parents[1] / ".venv").exists()
