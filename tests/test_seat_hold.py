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
import subprocess
import time
from pathlib import Path

import pytest

from mcp_hub import edge

SQUAD = Path(__file__).resolve().parents[1] / "squad" / "squad"


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

def run_squad(tmp_path, held: dict | None, agent="lane-a", verb="start"):
    conf = tmp_path / "squad.conf"
    conf.write_text(f"{agent}|{tmp_path}||--continue|squad\n", encoding="utf-8")
    heldf = tmp_path / "held.json"
    if held is not None:
        heldf.write_text(json.dumps({"generated": time.time(), "held": held}),
                         encoding="utf-8")
    return subprocess.run(
        ["bash", str(SQUAD), verb, agent],
        capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
             "SQUAD_CONF": str(conf), "MCP_HUB_HELD_FILE": str(heldf)},
    )


@pytest.mark.skipif(not Path("/usr/bin/jq").exists(), reason="needs jq")
def test_squad_refuses_to_start_a_held_lane(tmp_path):
    p = run_squad(tmp_path, {"lane-a": {
        "until": time.time() + 3600,
        "release_condition": "window resets at 18:00"}})
    assert "HELD" in p.stdout
    assert "window resets at 18:00" in p.stdout, "the release condition must be shown"
    assert p.returncode == 0, "a deliberate hold is not an error"


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
        ["bash", str(SQUAD), "start", "lane-a"],
        capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
             "SQUAD_CONF": str(conf), "MCP_HUB_HELD_FILE": str(heldf)},
    )
    assert "HELD" not in p.stdout
