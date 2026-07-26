"""Where a transported clone LANDS.

The operator's account->location ruleset (org_alias/pull_local) already says
where a repo belongs on a machine: `~/Projects/code/<org>/<repo>`. Transport
used to invent `<workspace-dir>/<label>/<repo>` instead, which is how moving
mcp-hub into the general workspace produced a stray `~/Projects/general/mcp-hub`
— a directory named after a WORKSPACE sitting in a tree organised by ACCOUNT.

Canonical when free, `<repo>-<label>` when taken, numbered after that. The
identity suffix moves in lockstep: two clones of one repo on distinct paths but
sharing a suffix would derive the same agent name and silently share identity,
which is the whole reason the suffix exists.
"""
from __future__ import annotations

import os
import pathlib
import subprocess

import pytest

SQUAD = pathlib.Path(__file__).resolve().parents[1] / "squad" / "squad"

pytestmark = pytest.mark.skipif(not SQUAD.exists(), reason="squad script not present")


def _run(env_conf, *args) -> subprocess.CompletedProcess:
    env, _ = env_conf
    return subprocess.run(
        ["bash", str(SQUAD), *args],
        capture_output=True, text=True, timeout=90, env=env,
    )


@pytest.fixture
def env(tmp_path):
    home = tmp_path / "home"
    (home / ".config" / "squad").mkdir(parents=True)
    (home / ".mcp-hub").mkdir()
    conf = home / ".config" / "squad" / "squad.conf"
    conf.write_text("", encoding="utf-8")
    e = dict(os.environ, HOME=str(home), SQUAD_CONF=str(conf), SQUAD_SOCKET="tdtest")
    return e, conf


def _git(path: pathlib.Path, *args, **kw):
    subprocess.run(["git", *args], cwd=path, capture_output=True, check=True, **kw)


def _pushed_repo(tmp_path: pathlib.Path, org: str, repo: str) -> pathlib.Path:
    """A worktree that PASSES transport_gate, with a controllable org/repo.

    Clone from a bare repo so the upstream ref genuinely exists and
    `rev-list @{u}..HEAD` is 0, then rewrite origin's URL so the derived project
    is `<org>/<repo>`. The gate only reads the URL, so it still passes.
    """
    bare = tmp_path / "remotes" / f"{repo}.git"
    bare.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True, capture_output=True)
    work = tmp_path / "work" / repo
    work.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "clone", "-q", str(bare), str(work)], check=True, capture_output=True)
    _git(work, "config", "user.email", "t@t")
    _git(work, "config", "user.name", "t")
    (work / "README.md").write_text("x\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "init")
    _git(work, "push", "-q", "origin", "HEAD")
    _git(work, "remote", "set-url", "origin", f"git@github-{org}:{org}/{repo}.git")
    return work


def _enrol(conf: pathlib.Path, name: str, worktree: pathlib.Path):
    with conf.open("a", encoding="utf-8") as fh:
        fh.write(f"{name}|{worktree}|||\n")


def test_default_destination_follows_the_account_ruleset(env, tmp_path):
    e, conf = env
    work = _pushed_repo(tmp_path, "monkeypashion", "demo")
    _enrol(conf, "demo-box", work)
    ws = tmp_path / "side.code-workspace"
    ws.write_text('{\n  "folders": [],\n  "settings": {}\n}\n', encoding="utf-8")

    res = _run(env, "transport", "demo-box", "--to", str(ws), "--dry-run")
    assert res.returncode == 0, res.stderr
    expected = f"{e['HOME']}/Projects/code/monkeypashion/demo"
    assert expected in res.stdout, res.stdout
    # the old behaviour, explicitly gone: no workspace-named directory
    assert "/side/demo" not in res.stdout


def test_taken_canonical_falls_back_to_repo_dash_label(env, tmp_path):
    """Second clone of one repo must not be offered a path that already exists."""
    e, conf = env
    work = _pushed_repo(tmp_path, "monkeypashion", "demo")
    _enrol(conf, "demo-box", work)
    canonical = pathlib.Path(e["HOME"]) / "Projects" / "code" / "monkeypashion" / "demo"
    canonical.mkdir(parents=True)
    ws = tmp_path / "side.code-workspace"
    ws.write_text('{\n  "folders": [],\n  "settings": {}\n}\n', encoding="utf-8")

    res = _run(env, "transport", "demo-box", "--to", str(ws), "--dry-run")
    assert res.returncode == 0, res.stderr
    assert f"{canonical}-side" in res.stdout, res.stdout


def test_suffix_does_not_inherit_the_path_attempt_number(env, tmp_path):
    """Path and identity suffix are resolved from DIFFERENT things.

    Tying the suffix to the path attempt looks tidier and is wrong: transporting
    on the same machine always finds the canonical path occupied — by the
    original — so every same-machine clone came out named `...-<label>-2` with
    nothing for the 2 to distinguish it from. The suffix counts clones of this
    repo in this operation; the path counts what is free on disk.
    """
    e, conf = env
    work = _pushed_repo(tmp_path, "monkeypashion", "demo")
    _enrol(conf, "demo-box", work)
    (pathlib.Path(e["HOME"]) / "Projects" / "code" / "monkeypashion" / "demo").mkdir(parents=True)
    ws = tmp_path / "side.code-workspace"
    ws.write_text('{\n  "folders": [],\n  "settings": {}\n}\n', encoding="utf-8")

    res = _run(env, "transport", "demo-box", "--to", str(ws), "--dry-run")
    assert res.returncode == 0, res.stderr
    assert "id-sfx : side" in res.stdout, res.stdout
    assert "side-2" not in res.stdout, "a lone clone has nothing to be the 2nd of"


def test_missing_target_workspace_is_reported_as_creatable(env, tmp_path):
    """The operator's opening move is an empty workspace that doesn't exist yet."""
    e, conf = env
    work = _pushed_repo(tmp_path, "monkeypashion", "demo")
    _enrol(conf, "demo-box", work)
    ws = tmp_path / "brand-new.code-workspace"

    res = _run(env, "transport", "demo-box", "--to", str(ws), "--dry-run")
    assert res.returncode == 0, res.stderr
    assert "does not exist yet" in res.stdout
    assert not ws.exists(), "a dry run must not create it"


def test_dest_override_is_refused_for_a_set(env, tmp_path):
    """--dest names ONE directory; for a set every agent would land on it."""
    e, conf = env
    work = _pushed_repo(tmp_path, "monkeypashion", "demo")
    _enrol(conf, "demo-box", work)
    ws = tmp_path / "side.code-workspace"
    ws.write_text('{\n  "folders": [],\n  "settings": {}\n}\n', encoding="utf-8")

    res = _run(env, "transport", "all", "--to", str(ws), "--dest", str(tmp_path / "x"))
    assert res.returncode != 0
    assert "cannot be used with 'all'" in res.stderr


def test_ws_new_creates_an_empty_workspace_and_never_clobbers(env, tmp_path):
    ws = tmp_path / "fresh.code-workspace"
    res = _run(env, "ws-new", str(ws))
    assert res.returncode == 0, res.stderr
    assert ws.is_file()
    assert '"folders"' in ws.read_text()

    ws.write_text('{\n  "folders": [],\n  "settings": {"squad.comms": false}\n}\n',
                  encoding="utf-8")
    before = ws.read_text()
    assert _run(env, "ws-new", str(ws)).returncode == 0
    assert ws.read_text() == before, "an existing workspace must be left exactly as it is"


def test_ws_new_appends_the_extension_when_missing(env, tmp_path):
    res = _run(env, "ws-new", str(tmp_path / "noext"))
    assert res.returncode == 0, res.stderr
    assert (tmp_path / "noext.code-workspace").is_file()
