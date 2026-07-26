"""`squad add-folder` — enrol an EXISTING folder as an agent.

The pull to `transport`'s push: nothing is cloned, copied or re-keyed. This is
how the operator's scratch agents were made by hand (plain directories, no git,
faculty class), so the feature is that same act without editing a config file.

Deliberately incurious about the folder — git is a bonus (it yields a real hub
identity and comms), never a gate.
"""
from __future__ import annotations

import os
import pathlib
import subprocess

import pytest

SQUAD = pathlib.Path(__file__).resolve().parents[1] / "squad" / "squad"

pytestmark = pytest.mark.skipif(not SQUAD.exists(), reason="squad script not present")


@pytest.fixture
def env(tmp_path):
    """Own HOME and own roster, so a test can never touch the real fleet."""
    home = tmp_path / "home"
    (home / ".config" / "squad").mkdir(parents=True)
    (home / ".mcp-hub").mkdir()
    conf = home / ".config" / "squad" / "squad.conf"
    conf.write_text("", encoding="utf-8")
    return dict(os.environ, HOME=str(home), SQUAD_CONF=str(conf)), conf


def _add(env_conf, folder) -> subprocess.CompletedProcess:
    env, _ = env_conf
    return subprocess.run(
        ["bash", str(SQUAD), "add-folder", str(folder)],
        capture_output=True, text=True, timeout=60, env=env,
    )


def _rows(env_conf) -> list[list[str]]:
    _, conf = env_conf
    return [line.split("|") for line in conf.read_text().splitlines() if line.strip()]


def _git(path: pathlib.Path, origin: str) -> pathlib.Path:
    path.mkdir(parents=True, exist_ok=True)
    def run(*a):
        subprocess.run(a, cwd=path, capture_output=True, check=True)
    run("git", "init", "-q")
    run("git", "remote", "add", "origin", origin)
    return path


def test_plain_folder_becomes_a_faculty_agent(env, tmp_path):
    folder = tmp_path / "notes"
    folder.mkdir()
    res = _add(env, folder)
    assert res.returncode == 0, res.stderr
    row = _rows(env)[0]
    assert row[1] == str(folder)
    assert row[4] == "faculty", "an added folder is on-demand by nature"
    # --continue matters: heal REFUSES to relaunch an agent without it, so an
    # agent enrolled without it is detected-deaf and then unrecoverable.
    assert "--continue" in row[3]
    # ...but NOT the comms flag: it is inert for a folder with no hub identity,
    # and implying comms it cannot have would be a lie in the roster.
    assert "channels" not in row[3]


def test_no_git_means_no_opt_in(env, tmp_path):
    folder = tmp_path / "notes"
    folder.mkdir()
    _add(env, folder)
    cfg = pathlib.Path(env[0]["HOME"]) / ".mcp-hub" / "config.json"
    assert not cfg.exists() or "projects" not in cfg.read_text() or "[]" in cfg.read_text()


def test_git_folder_gets_comms_too(env, tmp_path):
    folder = _git(tmp_path / "repo", "git@github-monkeypashion:monkeypashion/demo-x.git")
    res = _add(env, folder)
    assert res.returncode == 0, res.stderr
    row = _rows(env)[0]
    assert "--continue" in row[3] and "channels" in row[3]
    assert row[0].startswith("demo-x-"), f"identity should derive from the REMOTE, got {row[0]}"


def test_enrolling_twice_is_idempotent(env, tmp_path):
    folder = tmp_path / "notes"
    folder.mkdir()
    assert _add(env, folder).returncode == 0
    second = _add(env, folder)
    assert second.returncode == 0
    assert "already enrolled" in second.stdout
    assert len(_rows(env)) == 1, "one worktree must never get two rows"


def test_refuses_when_the_derived_name_is_taken_by_another_folder(env, tmp_path):
    """Two folders deriving one name would make field() pick arbitrarily."""
    origin = "git@github-monkeypashion:monkeypashion/demo-y.git"
    first = _git(tmp_path / "a" / "demo-y", origin)
    second = _git(tmp_path / "b" / "demo-y", origin)   # same repo, same host
    assert _add(env, first).returncode == 0
    res = _add(env, second)
    assert res.returncode != 0
    assert "already names a different worktree" in res.stderr
    assert len(_rows(env)) == 1, "a refusal must not add a row"


def test_missing_directory_is_refused(env, tmp_path):
    res = _add(env, tmp_path / "nope")
    assert res.returncode != 0
    assert "no such directory" in res.stderr
    assert _rows(env) == []


def test_relative_path_is_stored_absolute(env, tmp_path):
    """The roster is read by other processes; a relative path is meaningless."""
    folder = tmp_path / "notes"
    folder.mkdir()
    env_, conf = env
    res = subprocess.run(
        ["bash", str(SQUAD), "add-folder", "notes"],
        capture_output=True, text=True, timeout=60, env=env_, cwd=str(tmp_path),
    )
    assert res.returncode == 0, res.stderr
    assert _rows(env)[0][1] == str(folder.resolve())


def test_already_enrolled_still_lists_the_folder_in_a_workspace(env, tmp_path):
    """The asymmetry the operator found by trying the CLI.

    Enrolment and workspace membership are INDEPENDENT — `ws-remove` leaves an
    agent enrolled but absent from a workspace. `add-folder` used to return early
    on "already enrolled" and never restore the folder entry, so the CLI could not
    undo what ws-remove did. The cockpit path only worked because the extension
    added the folder itself afterwards.
    """
    env_, conf = env
    folder = tmp_path / "notes"
    folder.mkdir()
    ws = tmp_path / "t.code-workspace"
    ws.write_text('{\n  "folders": [],\n  "settings": {}\n}\n', encoding="utf-8")

    first = subprocess.run(["bash", str(SQUAD), "add-folder", str(folder), "--to", str(ws)],
                           capture_output=True, text=True, timeout=60, env=env_)
    assert first.returncode == 0, first.stderr
    assert str(folder) in ws.read_text(), "fresh enrolment must list the folder"

    # simulate ws-remove: folder entry gone, agent still enrolled
    ws.write_text('{\n  "folders": [],\n  "settings": {}\n}\n', encoding="utf-8")
    assert "notes-" in conf.read_text()

    second = subprocess.run(["bash", str(SQUAD), "add-folder", str(folder), "--to", str(ws)],
                            capture_output=True, text=True, timeout=60, env=env_)
    assert second.returncode == 0, second.stderr
    assert "already enrolled" in second.stdout
    assert str(folder) in ws.read_text(), "already-enrolled must STILL restore the folder"
    assert len(_rows(env)) == 1, "and must not duplicate the roster row"
