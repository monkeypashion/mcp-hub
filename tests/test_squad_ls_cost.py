"""`squad ls` must answer the same, and cost a run, not a run per agent.

dev-vm-1, 2026-10-09: with 46 agents one `squad ls` took ~9s and 5.5
CPU-seconds, and the cockpit extension ran it every 8s in every VSCode
window — three windows held ~3.3 cores to learn which agents were up. Most of
it was per-agent forks: a grep+awk for every roster lookup, three tmux calls
and a docker call per agent.

What must hold, each with a cheaper wrong version:
  THE ANSWER IS UNCHANGED — up / DEGRADED / down per substrate, exactly as
  before; a faster `ls` that misreports one seat is the bug, not the fix.
  ONE LISTING PER SUBSTRATE — tmux and docker are each asked once per run,
  whatever the roster size.
  `--up` IS LIVENESS ONLY — it never asks for hub state, which is what made
  the remaining cost (a docker exec per container seat).

Runs on a PRIVATE tmux socket with a fake docker: never the live fleet.
"""
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import uuid

import pytest

SQUAD = pathlib.Path(__file__).resolve().parents[1] / "squad" / "squad"

pytestmark = pytest.mark.skipif(
    not SQUAD.exists() or not shutil.which("tmux"),
    reason="squad script or tmux not present",
)


@pytest.fixture
def fleet(tmp_path):
    home = tmp_path / "home"
    (home / ".config" / "squad").mkdir(parents=True)
    (home / ".mcp-hub").mkdir()
    work = tmp_path / "w"
    work.mkdir()
    conf = home / ".config" / "squad" / "squad.conf"
    conf.write_text(
        f"alpha|{work}||--continue\n"
        f"beta|{work}||--continue\n"
        f"gamma|{work}||--continue\n"
        f"delta|{work}||@docker:c-delta\n"
        f"eps|{work}||@docker:c-eps\n",
        encoding="utf-8",
    )
    bindir = tmp_path / "bin"
    bindir.mkdir()
    calls = tmp_path / "calls"
    real_tmux = shutil.which("tmux")
    # Count every tmux and docker the script runs. docker has only c-delta
    # running, and honours `--filter name=^X$` the way the real one does — a
    # fake that ignored it would report every seat up and pass the old code
    # for the wrong reason.
    (bindir / "tmux").write_text(
        f'#!/bin/sh\necho tmux >> {calls}\nexec {real_tmux} "$@"\n')
    (bindir / "docker").write_text(
        f'#!/bin/sh\necho "docker $1" >> {calls}\n'
        '[ "$1" = ps ] || exit 0\n'
        'case "$*" in *"name=^"*) case "$*" in *"name=^c-delta\\$"*) ;; *) exit 0 ;; esac ;; esac\n'
        'echo c-delta\n')
    for f in ("tmux", "docker"):
        (bindir / f).chmod(0o755)
    sock = f"lscost-{uuid.uuid4().hex[:8]}"
    env = dict(os.environ, HOME=str(home), SQUAD_CONF=str(conf),
               SQUAD_SOCKET=sock, PATH=f"{bindir}:{os.environ['PATH']}")
    t = [real_tmux, "-L", sock]
    # alpha runs a program (up); beta's pane is a bare shell (DEGRADED);
    # gamma has no session at all (down).
    subprocess.run(t + ["new-session", "-d", "-s", "alpha", "sleep 120"], check=True)
    subprocess.run(t + ["new-session", "-d", "-s", "beta", "bash --norc"], check=True)
    try:
        yield env, calls
    finally:
        subprocess.run(t + ["kill-server"], capture_output=True)


def _ls(env, *extra):
    r = subprocess.run(["bash", str(SQUAD), "ls", *extra], capture_output=True,
                       text=True, timeout=60, env=env)
    assert r.returncode == 0, r.stderr
    return {ln.split()[0]: ln.split()[1:]
            for ln in r.stdout.splitlines()[1:] if ln.strip()}


def _counts(calls):
    lines = calls.read_text().splitlines() if calls.exists() else []
    return (sum(1 for x in lines if x == "tmux"),
            [x for x in lines if x.startswith("docker")])


def test_ls_reports_each_substrate_as_before(fleet):
    env, _ = fleet
    rows = _ls(env)
    assert rows["alpha"][0] == "up" and "(sleep)" in rows["alpha"]
    assert rows["beta"][0] == "DEGRADED"
    assert rows["gamma"][0] == "down"
    assert rows["delta"][0] == "up" and "c-delta)" in rows["delta"]
    assert rows["eps"][0] == "down"


def test_ls_asks_tmux_and_docker_once_per_run(fleet):
    env, calls = fleet
    _ls(env)
    tmux, docker = _counts(calls)
    # Before: three tmux calls per host agent and a docker ps per seat.
    assert tmux == 1, f"tmux ran {tmux} times for 5 agents"
    assert docker.count("docker ps") == 1, docker


def test_ls_up_is_liveness_only(fleet):
    env, calls = fleet
    full = _ls(env)
    calls.unlink(missing_ok=True)
    up = _ls(env, "--up")
    assert {a: r[0] for a, r in up.items()} == {a: r[0] for a, r in full.items()}
    # No hub column was computed, so no docker exec into a seat for it.
    _, docker = _counts(calls)
    assert docker == ["docker ps"], docker
    assert all(r[1] == "-" for r in up.values()), up
