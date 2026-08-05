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


def test_a_running_container_seat_is_not_reported_DOWN(env, tmp_path):
    """MEASURED: `squad ls` said `down` for a seat that was running and ⚡ on
    the hub — because squad looked for a tmux session on the HOST while the
    session lives inside the container.

    That is the "delivered live" class of falsehood: a status line asserting
    something it never checked. Liveness for a container row must ask
    DOCKER, which is where the process actually is.
    """
    work = tmp_path / "w"
    work.mkdir()
    # A fake docker on PATH that reports the container as running — the real
    # one is never invoked, so this can run anywhere.
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "docker").write_text(
        "#!/bin/sh\n"
        'case "$*" in *"ps"*) echo up-container ;; esac\nexit 0\n',
        encoding="utf-8",
    )
    (bindir / "docker").chmod(0o755)

    e, conf = env
    e = dict(e, PATH=f"{bindir}:{e['PATH']}")
    _run((e, conf), "add-container", "s1", str(work), "up-container")
    r = subprocess.run(
        ["bash", str(SQUAD), "ls"],
        capture_output=True, text=True, timeout=60, env=e,
    )
    assert r.returncode == 0, r.stderr
    line = [ln for ln in r.stdout.splitlines() if "s1" in ln]
    assert line, r.stdout
    assert "down" not in line[0], line[0]


def test_a_stopped_container_seat_IS_reported_down(env, tmp_path):
    """The other half: if the container is not running, saying so is the
    whole point. A check that always says 'up' is not a check."""
    work = tmp_path / "w"
    work.mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    # `docker ps` lists nothing — the container is absent.
    (bindir / "docker").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (bindir / "docker").chmod(0o755)

    e, conf = env
    e = dict(e, PATH=f"{bindir}:{e['PATH']}")
    _run((e, conf), "add-container", "s1", str(work), "gone-container")
    r = subprocess.run(
        ["bash", str(SQUAD), "ls"],
        capture_output=True, text=True, timeout=60, env=e,
    )
    line = [ln for ln in r.stdout.splitlines() if "s1" in ln]
    assert line and "down" in line[0], r.stdout


def test_a_container_seat_counts_as_having_comms(env, tmp_path):
    """The last visible difference in `squad ls`: the HUB column showed `·`
    (no comms) for a seat that was ⚡ on the hub, because has_comms greps the
    ARGS field for the channels flag and a container row's args field holds
    the marker instead.

    The flag lives inside the container — seat-entry launches claude with
    it. Reporting "no comms" for an agent that is demonstrably on the hub is
    the instrument being wrong about the thing it is pointed at.
    """
    work = tmp_path / "w"
    work.mkdir()
    _run(env, "add-container", "s1", str(work), "c1")
    r = subprocess.run(
        ["bash", "-c",
         f'. {SQUAD} 2>/dev/null; has_comms s1 && echo YES || echo NO'],
        capture_output=True, text=True, timeout=60, env=env[0],
    )
    # Sourcing squad runs its dispatch; fall back to the observable surface.
    if "YES" not in r.stdout and "NO" not in r.stdout:
        r = subprocess.run(["bash", str(SQUAD), "ls"], capture_output=True,
                           text=True, timeout=60, env=env[0])
        line = [ln for ln in r.stdout.splitlines() if "s1" in ln][0]
        # `·` is the "no comms, nothing to say" marker.
        assert " · " not in line, line
    else:
        assert "YES" in r.stdout


def _fake_docker(bindir, container, pane_text):
    """A docker that reports `container` running and serves a pane capture."""
    bindir.mkdir(exist_ok=True)
    (bindir / "docker").write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        f'  *ps*) echo {container} ;;\n'
        f'  *capture-pane*) printf "%s\\n" "{pane_text}" ;;\n'
        "esac\nexit 0\n",
        encoding="utf-8",
    )
    (bindir / "docker").chmod(0o755)


def test_a_running_container_seat_APPEARS_on_the_board(env, tmp_path):
    """The operator's report: it was not on the squad board at all.

    board_scan gates on a HOST tmux session, and a container seat has none —
    so it fell into the `down` branch and, being faculty, was dropped
    entirely. Invisible, while running and ⚡. The board must read a
    container row's state from the container.
    """
    import json

    work = tmp_path / "w"
    work.mkdir()
    _fake_docker(tmp_path / "bin", "c1", "esc to interrupt")
    e, conf = env
    e = dict(e, PATH=f"{tmp_path / 'bin'}:{e['PATH']}")
    _run((e, conf), "add-container", "s1", str(work), "c1")

    r = subprocess.run(["bash", str(SQUAD), "board", "--json"],
                       capture_output=True, text=True, timeout=60, env=e)
    assert r.returncode == 0, r.stderr
    names = [a["agent"] for a in json.loads(r.stdout).get("agents", [])]
    assert "s1" in names, names


def test_the_board_reads_a_container_seats_REAL_state(env, tmp_path):
    """Not merely present — present with a state derived from its own pane,
    like any other agent. A row that is always 'idle' would be decoration."""
    import json

    work = tmp_path / "w"
    work.mkdir()
    # "esc to interrupt" is claude's mid-turn chrome — classify_text calls
    # that WORKING, and the text comes from inside the container.
    _fake_docker(tmp_path / "bin", "c1", "esc to interrupt")
    e, conf = env
    e = dict(e, PATH=f"{tmp_path / 'bin'}:{e['PATH']}")
    _run((e, conf), "add-container", "s1", str(work), "c1")

    r = subprocess.run(["bash", str(SQUAD), "board", "--json"],
                       capture_output=True, text=True, timeout=60, env=e)
    row = [a for a in json.loads(r.stdout)["agents"] if a["agent"] == "s1"][0]
    assert row["state"] == "working", row


def test_a_STOPPED_container_seat_stays_hidden_like_other_faculty(env, tmp_path):
    """The existing rule is kept deliberately: permanent grey `down` rows
    bury the squad signal, which is why faculty is hidden when not running.
    A container seat is on-demand by nature, so it follows that rule."""
    import json

    work = tmp_path / "w"
    work.mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "docker").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (bindir / "docker").chmod(0o755)
    e, conf = env
    e = dict(e, PATH=f"{bindir}:{e['PATH']}")
    _run((e, conf), "add-container", "s1", str(work), "c1")

    r = subprocess.run(["bash", str(SQUAD), "board", "--json"],
                       capture_output=True, text=True, timeout=60, env=e)
    names = [a["agent"] for a in json.loads(r.stdout).get("agents", [])]
    assert "s1" not in names, names
