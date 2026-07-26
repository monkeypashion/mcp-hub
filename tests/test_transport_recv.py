"""transport-recv — destination-side wiring, and the identity-collapse guard.

The guard exists because the failure it prevents is SILENT and looks like
success. Two clones of one repo transported into one workspace derive the SAME
agent name (suffix defaults to the workspace label, which they share). The old
code saw the name already in the roster, said "row already present", and exited
0 — so you asked for two agents and got one, sharing a hub binding, an inbox, a
heartbeat pidfile and a status cache.

`squad transport all` passes a per-destination suffix to avoid this. These tests
cover the caller who doesn't know the trap exists: the wall has to be in
transport-recv, not in remembering to pass an argument.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess

import pytest

RECV = pathlib.Path(__file__).resolve().parents[1] / "squad" / "transport-recv"
ORIGIN = "git@github-monkeypashion:monkeypashion/mcp-hub.git"

pytestmark = pytest.mark.skipif(not RECV.exists(), reason="transport-recv not present")


def _repo(path: pathlib.Path) -> pathlib.Path:
    """A git repo whose origin decides the derived name."""
    path.mkdir(parents=True)
    def run(*a):
        subprocess.run(a, cwd=path, capture_output=True, check=True)
    run("git", "init", "-q")
    run("git", "remote", "add", "origin", ORIGIN)
    return path


def _recv(home: pathlib.Path, dest: pathlib.Path, ws: pathlib.Path, suffix: str):
    env = dict(os.environ, HOME=str(home))
    return subprocess.run(
        ["bash", str(RECV), str(dest), str(ws), suffix,
         "monkeypashion/mcp-hub", "--continue", "faculty", "http://hub.test/mcp"],
        capture_output=True, text=True, timeout=120, env=env,
    )


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    (h / ".config" / "squad").mkdir(parents=True)
    ws = h / "target.code-workspace"
    ws.write_text(json.dumps({"folders": [], "settings": {}}), encoding="utf-8")
    return h, ws


def _roster(home: pathlib.Path) -> list[str]:
    conf = home / ".config" / "squad" / "squad.conf"
    return [line for line in conf.read_text().splitlines() if line.strip()] if conf.exists() else []


def test_wires_up_a_destination(home, tmp_path):
    h, ws = home
    dest = _repo(tmp_path / "a" / "mcp-hub")
    res = _recv(h, dest, ws, "target")
    assert res.returncode == 0, res.stdout + res.stderr
    # roster row carries args AND class — an agent without them is
    # operationally a different agent (can't be healed, can't hear)
    row = _roster(h)[0].split("|")
    assert row[1] == str(dest) and row[3] == "--continue" and row[4] == "faculty"
    # hub connection file is generated, not copied
    assert json.loads((dest / ".mcp.json").read_text())["mcpServers"]["hub"]["url"] \
        == "http://hub.test/mcp"
    # trust + MCP pre-approved, or first launch blocks on dialogs forever
    entry = json.loads((h / ".claude.json").read_text())["projects"][str(dest)]
    assert entry["hasTrustDialogAccepted"] is True
    assert entry["enabledMcpjsonServers"] == ["hub"]
    # identity suffix registered, project opted in
    cfg = json.loads((h / ".mcp-hub" / "config.json").read_text())
    assert cfg["workspaces"][str(dest)] == "target"
    assert "monkeypashion/mcp-hub" in cfg["projects"]
    # and it appears in the workspace, or no terminal ever opens for it
    assert str(dest) in (ws.read_text())


def test_same_destination_twice_is_idempotent(home, tmp_path):
    h, ws = home
    dest = _repo(tmp_path / "a" / "mcp-hub")
    assert _recv(h, dest, ws, "target").returncode == 0
    second = _recv(h, dest, ws, "target")
    assert second.returncode == 0, second.stdout + second.stderr
    assert "idempotent" in second.stdout
    assert len(_roster(h)) == 1, "must not duplicate the row"
    assert ws.read_text().count(str(dest)) == 1, "must not duplicate the folder"


def test_refuses_identity_collapse_when_suffix_is_reused(home, tmp_path):
    """The trap: same repo, same suffix, DIFFERENT worktree -> same derived name.

    This is what happens when a caller fans out over two clones of one repo and
    forgets a per-destination suffix. It must be a wall, not a shrug.
    """
    h, ws = home
    first = _repo(tmp_path / "a" / "mcp-hub")
    second = _repo(tmp_path / "b" / "mcp-hub")
    assert _recv(h, first, ws, "target").returncode == 0

    res = _recv(h, second, ws, "target")          # same suffix, different dest
    assert res.returncode == 5, res.stdout + res.stderr
    assert "REFUSING" in res.stdout
    assert str(first) in res.stdout and str(second) in res.stdout
    # the first agent must be untouched — a refusal may not corrupt what exists
    assert len(_roster(h)) == 1
    assert _roster(h)[0].split("|")[1] == str(first)


def test_distinct_suffixes_give_two_independent_agents(home, tmp_path):
    """What `squad transport all` actually does: <label>, <label>-2, ..."""
    h, ws = home
    first = _repo(tmp_path / "a" / "mcp-hub")
    second = _repo(tmp_path / "b" / "mcp-hub")
    assert _recv(h, first, ws, "target").returncode == 0
    assert _recv(h, second, ws, "target-2").returncode == 0
    rows = _roster(h)
    assert len(rows) == 2
    names = [r.split("|")[0] for r in rows]
    assert len(set(names)) == 2, f"identities must differ, got {names}"
    assert names[1].endswith("-target-2")


def test_refusal_seeds_nothing_for_the_rejected_destination(home, tmp_path):
    """A refusal must not leave the rejected destination half-wired.

    Caught in review by mcp-hub-dev-vm-1: the check used to sit beside the
    roster append, so by the time it fired, steps 3-4 had already written
    .mcp.json and the trust/MCP approval for a destination we then declined to
    enrol. "Changes nothing" was overstated. The check now runs before any
    seeding, and rolls back the one thing written ahead of it.
    """
    h, ws = home
    first = _repo(tmp_path / "a" / "mcp-hub")
    second = _repo(tmp_path / "b" / "mcp-hub")
    assert _recv(h, first, ws, "target").returncode == 0

    res = _recv(h, second, ws, "target")
    assert res.returncode == 5

    assert not (second / ".mcp.json").exists(), "no hub config for a rejected dest"
    claude = json.loads((h / ".claude.json").read_text())
    assert str(second) not in claude.get("projects", {}), "no trust seeding either"
    cfg = json.loads((h / ".mcp-hub" / "config.json").read_text())
    assert str(second) not in cfg.get("workspaces", {}), "suffix must be rolled back"
    assert str(second) not in ws.read_text(), "no workspace folder entry"
    # and the accepted agent is still intact
    assert len(_roster(h)) == 1 and _roster(h)[0].split("|")[1] == str(first)
