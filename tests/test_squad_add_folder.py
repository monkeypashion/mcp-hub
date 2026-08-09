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


# --------------------------------------------- --hub: a folder ON the hub
#
# 🔴 THE GAP (operator, 2026-08-09): a plain folder could be started and
# stopped through the API but never MESSAGED by it. Hub identity is derived
# from the git remote, so no remote meant no project, so `hub_optedin` refused,
# so `arm_comms` never armed the channels flag. On dev-vm-1 that was 13 of the
# 15 on-demand agents — i.e. half the roster was schedulable but unaddressable.
#
# ⚠️ The marker stays DEPRECATED for git repos: a committed marker is shared by
# every clone, so they collapse into one hub identity. That needs a repo to
# happen. A folder with no git has no clones and nothing to commit, and if it
# later gains a remote, derived identity wins by resolution order.


def _add_hub(env_conf, folder, *extra) -> subprocess.CompletedProcess:
    env, _ = env_conf
    return subprocess.run(
        ["bash", str(SQUAD), "add-folder", str(folder), "--hub", *extra],
        capture_output=True, text=True, timeout=60, env=env,
    )


def _marker(folder: pathlib.Path) -> dict:
    import json
    return json.loads((folder / ".claude" / "hub-agent.json").read_text())


def _optins(env_conf) -> list[str]:
    import json
    env, _ = env_conf
    cfg = pathlib.Path(env["HOME"]) / ".mcp-hub" / "config.json"
    if not cfg.exists():
        return []
    return json.loads(cfg.read_text()).get("projects", [])


def test_hub_gives_a_remoteless_folder_ALL_THREE_legs(env, tmp_path):
    """Marker, opt-in AND comms flag. Any one alone is not enough: without the
    marker there is no identity to register; without the opt-in `arm_comms`
    refuses; and without the channels flag the binding is not push-deliverable,
    so the heartbeat's deliverability check drops it after 3 beats and the
    agent flickers offline."""
    folder = tmp_path / "notes"
    folder.mkdir()
    res = _add_hub(env, folder)
    assert res.returncode == 0, res.stderr

    m = _marker(folder)
    assert m["name"] == _rows(env)[0][0]        # marker agrees with the roster
    assert m["project"] == "folder/notes"       # default, from the basename
    assert "folder/notes" in _optins(env)
    assert "server:hub" in _rows(env)[0][3], "comms flag not armed"


def test_the_project_can_be_NAMED(env, tmp_path):
    """With no git remote there is nothing forcing two machines to agree on a
    project string, so anything meant to pair across machines must say so."""
    folder = tmp_path / "notes"
    folder.mkdir()
    assert _add_hub(env, folder, "--project", "team/shared").returncode == 0
    assert _marker(folder)["project"] == "team/shared"
    assert "team/shared" in _optins(env)


def test_without_hub_a_plain_folder_stays_OFF_the_hub(env, tmp_path):
    """The control. --hub is opt-in: `add-folder` on its own must keep the old
    behaviour, or every scratch directory silently joins the fleet."""
    folder = tmp_path / "notes"
    folder.mkdir()
    assert _add(env, folder).returncode == 0
    assert not (folder / ".claude" / "hub-agent.json").exists()
    assert _optins(env) == []
    assert "server:hub" not in _rows(env)[0][3]


def test_hub_is_IGNORED_where_a_git_remote_already_derives_one(env, tmp_path):
    """Derived identity wins, so a marker here would be inert — and it would
    put the deprecated committed-marker shape inside a git repo, which is the
    collision the deprecation exists to prevent."""
    folder = _git(tmp_path / "repo", "git@github-x:acme/widget.git")
    res = _add_hub(env, folder, "--project", "should/be-ignored")
    assert res.returncode == 0, res.stderr
    assert not (folder / ".claude" / "hub-agent.json").exists()
    assert "acme/widget" in _optins(env)
    assert "should/be-ignored" not in _optins(env)
    assert "git remote" in res.stdout


def test_rm_removes_the_marker_so_the_agent_really_goes_QUIET(env, tmp_path):
    """🔴 `_resolve_agent_identity` reads the marker with NO opt-in gate — that
    gate applies to DERIVED identity only. So a folder left holding a marker
    keeps registering, heartbeating and answering DMs after `squad rm` reported
    it removed: a silent disconnect in the direction nobody checks."""
    env_, _conf = env
    folder = tmp_path / "notes"
    folder.mkdir()
    _add_hub(env, folder)
    agent = _rows(env)[0][0]

    res = subprocess.run(["bash", str(SQUAD), "rm", agent],
                         capture_output=True, text=True, timeout=60, env=env_)
    assert res.returncode == 0, res.stderr
    assert not (folder / ".claude" / "hub-agent.json").exists()
    assert _optins(env) == [], "opt-in survived the removal"


def test_rm_reads_the_project_OUT_of_the_marker_BEFORE_deleting_it(env, tmp_path):
    """Ordering, which is easy to get wrong and silent when you do: the opt-out
    looks the project up via the marker, so deleting the marker first leaves an
    orphaned entry in config.json forever."""
    folder = tmp_path / "notes"
    folder.mkdir()
    _add_hub(env, folder, "--project", "solo/thing")
    agent = _rows(env)[0][0]
    env_, _conf = env
    subprocess.run(["bash", str(SQUAD), "rm", agent],
                   capture_output=True, text=True, timeout=60, env=env_)
    assert "solo/thing" not in _optins(env)


def test_a_SHARED_marker_project_keeps_its_opt_in_when_one_agent_goes(env, tmp_path):
    """The 2026-08-08 silent-disconnect rule, which must hold for marker
    projects too: opting out is per PROJECT, removal is per AGENT."""
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    _add_hub(env, a, "--project", "team/notes")
    _add_hub(env, b, "--project", "team/notes")
    env_, _conf = env
    agent_a = [r[0] for r in _rows(env) if r[1] == str(a)][0]
    res = subprocess.run(["bash", str(SQUAD), "rm", agent_a],
                         capture_output=True, text=True, timeout=60, env=env_)
    assert "team/notes" in _optins(env), "sibling was silently disconnected"
    assert "KEPT" in res.stdout
    assert not (a / ".claude" / "hub-agent.json").exists()
    assert (b / ".claude" / "hub-agent.json").exists(), "removed the wrong marker"


# ------------------------------------- --hub on a folder ALREADY enrolled
#
# 🔴 The first cut of --hub only ran at FIRST enrolment, so it did nothing for
# every agent already on the roster — i.e. for all 13 of the plain-folder
# agents it was built for. `add-folder` returns early for a folder it already
# knows, and that early return swallowed the whole feature.


def test_hub_UPGRADES_a_folder_that_is_already_enrolled(env, tmp_path):
    """The common case, not the exotic one."""
    folder = tmp_path / "notes"
    folder.mkdir()
    assert _add(env, folder).returncode == 0          # enrolled the old way
    agent = _rows(env)[0][0]
    assert not (folder / ".claude" / "hub-agent.json").exists()

    res = _add_hub(env, folder)
    assert res.returncode == 0, res.stderr
    assert _marker(folder)["name"] == agent, (
        "marker must name the agent `squad start` will launch, not a new one")
    assert _marker(folder)["project"] == "folder/notes"
    assert "folder/notes" in _optins(env)
    assert "server:hub" in _rows(env)[0][3], "comms not armed on upgrade"


def test_the_upgrade_keeps_the_agents_other_launch_args(env, tmp_path):
    """arm_comms is used rather than a second roster writer precisely so the
    rest of field 4 survives — losing --continue would make `heal` refuse to
    restart the agent, silently."""
    folder = tmp_path / "notes"
    folder.mkdir()
    _add(env, folder)
    before = _rows(env)[0][3]
    assert "--continue" in before
    _add_hub(env, folder)
    assert "--continue" in _rows(env)[0][3]


def test_the_upgrade_is_IDEMPOTENT(env, tmp_path):
    """Re-running must not duplicate the flag or the opt-in — squad verbs are
    re-runnable by convention and this one is aimed at a batch of agents."""
    folder = tmp_path / "notes"
    folder.mkdir()
    _add(env, folder)
    _add_hub(env, folder)
    first = _rows(env)[0][3]
    _add_hub(env, folder)
    assert _rows(env)[0][3] == first
    assert _optins(env).count("folder/notes") == 1
    assert len(_rows(env)) == 1, "a second roster row appeared"


def test_upgrading_an_already_enrolled_GIT_folder_is_ignored(env, tmp_path):
    """Derived identity is authoritative there, and a marker inside a git repo
    is the deprecated committed shape."""
    folder = _git(tmp_path / "repo", "git@github-x:acme/widget.git")
    _add(env, folder)
    res = _add_hub(env, folder, "--project", "should/be-ignored")
    assert res.returncode == 0, res.stderr
    assert not (folder / ".claude" / "hub-agent.json").exists()
    assert "should/be-ignored" not in _optins(env)
    assert "git remote" in res.stdout


def test_the_upgrade_can_be_given_an_explicit_project(env, tmp_path):
    folder = tmp_path / "notes"
    folder.mkdir()
    _add(env, folder)
    _add_hub(env, folder, "--project", "team/shared")
    assert _marker(folder)["project"] == "team/shared"
    assert "team/shared" in _optins(env)
