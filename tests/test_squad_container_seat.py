"""A container seat should be INDISTINGUISHABLE from a normal squad agent.

Operator's bar, 2026-08-05: it appears on the board, its folder is in the
`.code-workspace`, and opening it is the same act as opening any other
agent — Remote-SSH to the machine, open the workspace, click the tab.

The only honest difference is WHERE the process runs, so the only thing
that changes is what the tab types: `docker exec -it <container> tmux
attach` instead of `claude <args>`. Everything else — roster row, folder
entry, cockpit tab, statusline — is the ordinary path.

The marker lives in the ARGS field (`@docker:<container>`) rather than in a
new roster column: the roster is a 5-field pipe format that several readers
parse, and adding a column would silently shift every field after it.
"""
from __future__ import annotations

import os
import pathlib
import subprocess

import pytest

SQUAD = pathlib.Path(__file__).resolve().parents[1] / "squad" / "squad"

pytestmark = pytest.mark.skipif(
    not SQUAD.exists(), reason="squad script not present"
)


@pytest.fixture
def env(tmp_path):
    """Own HOME and own roster — a test must never touch the real fleet."""
    home = tmp_path / "home"
    (home / ".config" / "squad").mkdir(parents=True)
    (home / ".mcp-hub").mkdir()
    conf = home / ".config" / "squad" / "squad.conf"
    conf.write_text("", encoding="utf-8")
    return dict(os.environ, HOME=str(home), SQUAD_CONF=str(conf)), conf


def _run(env_conf, *argv) -> subprocess.CompletedProcess:
    e, _ = env_conf
    return subprocess.run(
        ["bash", str(SQUAD), *argv],
        capture_output=True, text=True, timeout=60, env=e,
    )


def test_add_container_enrols_a_row_the_cockpit_can_show(env, tmp_path):
    """The folder is a REAL directory on this machine (bind-mounted into
    the container), which is what lets the workspace file list it and the
    extension give it a tab."""
    work = tmp_path / "seatwork"
    work.mkdir()
    r = _run(env, "add-container", "claude-seat-box", str(work),
             "claude-seat-box")
    assert r.returncode == 0, r.stderr
    row = env[1].read_text(encoding="utf-8").strip()
    fields = row.split("|")
    assert fields[0] == "claude-seat-box"
    assert fields[1] == str(work)
    assert fields[3] == "@docker:claude-seat-box"
    # faculty: the EDGE owns a container's lifecycle, so `up` must never
    # start one — that would race the placement it is realizing.
    assert fields[4] == "faculty"


def test_add_container_refuses_a_missing_folder(env, tmp_path):
    r = _run(env, "add-container", "s1", str(tmp_path / "nope"), "c1")
    assert r.returncode != 0
    assert "folder" in (r.stderr + r.stdout).lower()


def test_add_container_refuses_a_duplicate_name(env, tmp_path):
    work = tmp_path / "w"
    work.mkdir()
    assert _run(env, "add-container", "s1", str(work), "c1").returncode == 0
    r = _run(env, "add-container", "s1", str(work), "c1")
    assert r.returncode != 0
    assert "already" in (r.stderr + r.stdout).lower()


def test_the_launch_line_attaches_instead_of_running_claude(env, tmp_path):
    """The whole difference in one line. Running `claude` on the host for a
    container seat would put a SECOND claude on the same worktree — two
    agents, one identity, both writing.
    """
    work = tmp_path / "w"
    work.mkdir()
    _run(env, "add-container", "s1", str(work), "my-container")
    r = _run(env, "launch-cmd", "s1")
    assert r.returncode == 0, r.stderr
    out = r.stdout.strip()
    assert "docker exec -it my-container" in out
    assert "tmux attach" in out
    assert not out.startswith("claude ")


def test_a_normal_agent_still_launches_claude(env, tmp_path):
    """The guard against fixing containers by breaking everything else."""
    work = tmp_path / "w"
    work.mkdir()
    conf = env[1]
    conf.write_text(f"plain-1|{work}||--continue|squad\n", encoding="utf-8")
    r = _run(env, "launch-cmd", "plain-1")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().startswith("claude ")


def test_a_diagnostic_never_mutates_the_roster(env, tmp_path):
    """`launch-cmd` prints; it must not arm, rename or rewrite anything.

    ⚠️ HONEST LIMIT, stated rather than faked: the up/restart path calls
    arm_comms, which appends the channels flag to field 4 — the field that
    holds the container MARKER. That path needs a live tmux server, so no
    automated test here exercises it. It is made safe STRUCTURALLY instead:
    launch_agent_cmd returns for a container row BEFORE arm_comms is
    reached, and arm_comms carries its own container guard as belt and
    braces. An earlier version of this test claimed to cover that and
    covered nothing — the mutation survived, and the probe that exposed it
    showed launch-cmd never calls arm_comms at all.
    """
    work = tmp_path / "w"
    work.mkdir()
    _run(env, "add-container", "s1", str(work), "c1")
    before = env[1].read_text(encoding="utf-8")
    _run(env, "launch-cmd", "s1")
    assert env[1].read_text(encoding="utf-8") == before
