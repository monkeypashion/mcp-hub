"""`squad teardown workspace <ws>` — close a cloned squad down.

The inverse of `transport workspace`. The operator's ephemeral case (spin a
parallel squad up for a side project, then close it down) needs the closing move
to be as cheap as the opening one.

THE SAFETY PROPERTY, which most of this file exists to pin down: deleting a
worktree is gated on the agent having a transport-registered identity suffix in
`~/.mcp-hub/config.json`. Only transport writes one. Pointed at the MAIN
workspace, an ungated teardown would delete the primary repos — they are clean
and pushed exactly like clones are, so "is it safe to delete" cannot tell an
original from a copy. Provenance can, and provenance is the only thing that can.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess

import pytest

SQUAD = pathlib.Path(__file__).resolve().parents[1] / "squad" / "squad"

pytestmark = pytest.mark.skipif(not SQUAD.exists(), reason="squad script not present")


@pytest.fixture
def env(tmp_path):
    home = tmp_path / "home"
    (home / ".config" / "squad").mkdir(parents=True)
    (home / ".mcp-hub").mkdir()
    conf = home / ".config" / "squad" / "squad.conf"
    conf.write_text("", encoding="utf-8")
    e = dict(os.environ, HOME=str(home), SQUAD_CONF=str(conf), SQUAD_SOCKET="tdtest2")
    return e, conf


def _run(env, *args) -> subprocess.CompletedProcess:
    e, _ = env
    return subprocess.run(["bash", str(SQUAD), *args],
                          capture_output=True, text=True, timeout=90, env=e)


def _git(path: pathlib.Path, *args):
    subprocess.run(["git", *args], cwd=path, capture_output=True, check=True)


def _pushed_repo(tmp_path: pathlib.Path, org: str, repo: str) -> pathlib.Path:
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


def _workspace(tmp_path: pathlib.Path, *folders: pathlib.Path) -> pathlib.Path:
    ws = tmp_path / "side.code-workspace"
    entries = ",\n".join(
        '    {\n      "name": "%s",\n      "path": "%s"\n    }' % (f.name, f) for f in folders)
    ws.write_text('{\n  "folders": [\n%s\n  ],\n  "settings": {}\n}\n' % entries, encoding="utf-8")
    return ws


def _enrol(conf: pathlib.Path, name: str, worktree: pathlib.Path):
    with conf.open("a", encoding="utf-8") as fh:
        fh.write(f"{name}|{worktree}|||\n")


def _register_suffix(home: str, worktree: pathlib.Path, suffix: str):
    """What transport writes to mark a worktree as one of its clones."""
    cfg = pathlib.Path(home) / ".mcp-hub" / "config.json"
    data = json.loads(cfg.read_text()) if cfg.exists() else {}
    data.setdefault("workspaces", {})[str(worktree)] = suffix
    cfg.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def test_an_original_is_never_deleted(env, tmp_path):
    """THE property. No registered suffix ⇒ unenrol, never delete.

    This is the case that would otherwise wipe the operator's primary repos:
    the worktree is clean and pushed, so every "safe to delete?" check passes.
    Only provenance distinguishes it.
    """
    e, conf = env
    work = _pushed_repo(tmp_path, "monkeypashion", "demo")
    _enrol(conf, "demo-box", work)
    ws = _workspace(tmp_path, work)

    res = _run(env, "teardown", "workspace", str(ws), "--delete-worktrees", "--yes")
    assert res.returncode == 0, res.stderr
    assert "not a transported clone" in res.stdout
    assert work.is_dir(), "an original must survive an explicit --delete-worktrees"
    assert (work / "README.md").exists()
    assert "demo-box" not in conf.read_text(), "but it should still be unenrolled"


def test_a_transported_clone_is_deleted(env, tmp_path):
    e, conf = env
    work = _pushed_repo(tmp_path, "monkeypashion", "demo")
    _enrol(conf, "demo-box", work)
    _register_suffix(e["HOME"], work, "side")
    ws = _workspace(tmp_path, work)

    res = _run(env, "teardown", "workspace", str(ws), "--delete-worktrees", "--yes")
    assert res.returncode == 0, res.stderr
    assert not work.exists(), "a registered clone should be removed"
    cfg = json.loads((pathlib.Path(e["HOME"]) / ".mcp-hub" / "config.json").read_text())
    assert str(work) not in (cfg.get("workspaces") or {}), "its suffix must be unregistered too"


def test_a_clone_with_unpushed_work_is_kept_and_named(env, tmp_path):
    """Teardown must never be the thing that loses work."""
    e, conf = env
    work = _pushed_repo(tmp_path, "monkeypashion", "demo")
    (work / "scratch.txt").write_text("unsaved\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "local only")
    _enrol(conf, "demo-box", work)
    _register_suffix(e["HOME"], work, "side")
    ws = _workspace(tmp_path, work)

    res = _run(env, "teardown", "workspace", str(ws), "--delete-worktrees", "--yes")
    assert res.returncode == 0, res.stderr
    assert work.is_dir(), "unpushed commits must survive"
    assert "unpushed" in res.stdout


def test_default_keeps_all_code(env, tmp_path):
    """Without --delete-worktrees, teardown only unenrols."""
    e, conf = env
    work = _pushed_repo(tmp_path, "monkeypashion", "demo")
    _enrol(conf, "demo-box", work)
    _register_suffix(e["HOME"], work, "side")
    ws = _workspace(tmp_path, work)

    res = _run(env, "teardown", "workspace", str(ws))
    assert res.returncode == 0, res.stderr
    assert work.is_dir()
    assert "demo-box" not in conf.read_text()
    assert str(work) not in ws.read_text(), "the workspace folder entry should be gone"


def test_destructive_run_refuses_without_yes_and_shows_the_plan(env, tmp_path):
    e, conf = env
    work = _pushed_repo(tmp_path, "monkeypashion", "demo")
    _enrol(conf, "demo-box", work)
    _register_suffix(e["HOME"], work, "side")
    ws = _workspace(tmp_path, work)

    res = _run(env, "teardown", "workspace", str(ws), "--delete-worktrees")
    assert res.returncode != 0
    assert "DELETE" in res.stdout, "the plan must be shown before the refusal"
    assert "--yes" in res.stderr
    assert work.is_dir(), "a refusal must not delete anything"
    assert "demo-box" in conf.read_text(), "nor unenrol anything"


def test_dry_run_writes_nothing(env, tmp_path):
    e, conf = env
    work = _pushed_repo(tmp_path, "monkeypashion", "demo")
    _enrol(conf, "demo-box", work)
    ws = _workspace(tmp_path, work)
    before = ws.read_text()

    res = _run(env, "teardown", "workspace", str(ws), "--dry-run")
    assert res.returncode == 0, res.stderr
    assert "dry run" in res.stdout
    assert conf.read_text().strip() != "", "roster untouched"
    assert ws.read_text() == before, "workspace untouched"
    assert work.is_dir()


def test_hub_optin_survives_when_another_agent_shares_the_project(env, tmp_path):
    """Tearing a clone down must not silence the hooks for the original.

    Both rows derive the same org/repo, so a blanket opt-out would take the
    surviving agent's hub comms with it.
    """
    e, conf = env
    original = _pushed_repo(tmp_path, "monkeypashion", "demo")
    clone = tmp_path / "clone" / "demo"
    clone.parent.mkdir(parents=True)
    subprocess.run(["git", "clone", "-q", str(tmp_path / "remotes" / "demo.git"), str(clone)],
                   check=True, capture_output=True)
    _git(clone, "remote", "set-url", "origin", "git@github-monkeypashion:monkeypashion/demo.git")
    _enrol(conf, "demo-orig", original)
    _enrol(conf, "demo-clone", clone)
    cfg = pathlib.Path(e["HOME"]) / ".mcp-hub" / "config.json"
    cfg.write_text(json.dumps({"projects": ["monkeypashion/demo"]}, indent=2), encoding="utf-8")
    ws = _workspace(tmp_path, clone)

    res = _run(env, "teardown", "workspace", str(ws))
    assert res.returncode == 0, res.stderr
    assert "monkeypashion/demo" in json.loads(cfg.read_text()).get("projects", []), \
        "the surviving original still needs its hub opt-in"


def test_workspace_file_removal_is_opt_in(env, tmp_path):
    e, conf = env
    work = _pushed_repo(tmp_path, "monkeypashion", "demo")
    _enrol(conf, "demo-box", work)
    ws = _workspace(tmp_path, work)

    assert _run(env, "teardown", "workspace", str(ws)).returncode == 0
    assert ws.is_file(), "the workspace file survives a plain teardown"

    _enrol(conf, "demo-box", work)
    ws2 = _workspace(tmp_path, work)
    res = _run(env, "teardown", "workspace", str(ws2), "--remove-workspace", "--yes")
    assert res.returncode == 0, res.stderr
    assert not ws2.exists()


def _claude_json(home: str, *paths: pathlib.Path):
    """What transport's trust-seeding leaves behind, per destination path."""
    p = pathlib.Path(home) / ".claude.json"
    p.write_text(json.dumps({"projects": {
        **{str(d): {"hasTrustDialogAccepted": True, "enabledMcpjsonServers": ["hub"]}
           for d in paths},
        "/home/someone/unrelated": {"hasTrustDialogAccepted": True},
    }}, indent=2), encoding="utf-8")
    return p


def test_deleting_a_clone_removes_its_trust_entry(env, tmp_path):
    """Found by RUNNING a teardown, not by a test — nothing looked at this file.

    Transport seeds folder-trust + hub-MCP approval so a clone's first launch
    doesn't hang on dialogs, and its stated justification is that seeding makes
    trust "an explicit act by whoever authorised the transport". An entry that
    outlives the agent breaks exactly that: the destination path is derived from
    repo + label, so a LATER transport to the same place arrives pre-trusted on
    an authorisation granted for a different, deleted agent.
    """
    e, conf = env
    work = _pushed_repo(tmp_path, "monkeypashion", "demo")
    _enrol(conf, "demo-box", work)
    _register_suffix(e["HOME"], work, "side")
    cj = _claude_json(e["HOME"], work)
    ws = _workspace(tmp_path, work)

    res = _run(env, "teardown", "workspace", str(ws), "--delete-worktrees", "--yes")
    assert res.returncode == 0, res.stderr
    projects = json.loads(cj.read_text())["projects"]
    assert str(work) not in projects, "the deleted clone's trust must not outlive it"
    assert "/home/someone/unrelated" in projects, "and nothing else may be touched"


def test_unenrolling_KEEPS_the_trust_entry(env, tmp_path):
    """The inverse case, and the reason this isn't unconditional.

    An unenrolled agent keeps its code and can be re-enrolled. Revoking trust it
    was legitimately granted would re-prompt it for no reason — so only an
    actually-deleted worktree loses its entry.
    """
    e, conf = env
    work = _pushed_repo(tmp_path, "monkeypashion", "demo")
    _enrol(conf, "demo-box", work)          # no registered suffix -> unenrol only
    cj = _claude_json(e["HOME"], work)
    ws = _workspace(tmp_path, work)

    res = _run(env, "teardown", "workspace", str(ws), "--delete-worktrees", "--yes")
    assert res.returncode == 0, res.stderr
    assert work.is_dir(), "precondition: this agent is kept, not deleted"
    assert str(work) in json.loads(cj.read_text())["projects"], \
        "code kept ⇒ trust kept, or the agent gets re-prompted for nothing"


def test_unreadable_claude_json_does_not_fail_the_teardown(env, tmp_path):
    """Fail-open, same as the seeding side: never block on this file."""
    e, conf = env
    work = _pushed_repo(tmp_path, "monkeypashion", "demo")
    _enrol(conf, "demo-box", work)
    _register_suffix(e["HOME"], work, "side")
    (pathlib.Path(e["HOME"]) / ".claude.json").write_text("{ not json", encoding="utf-8")
    ws = _workspace(tmp_path, work)

    res = _run(env, "teardown", "workspace", str(ws), "--delete-worktrees", "--yes")
    assert res.returncode == 0, res.stderr
    assert not work.exists(), "the worktree deletion must still happen"


def test_unknown_workspace_is_refused(env, tmp_path):
    res = _run(env, "teardown", "workspace", str(tmp_path / "nope.code-workspace"))
    assert res.returncode != 0
    assert "no such workspace file" in res.stderr
