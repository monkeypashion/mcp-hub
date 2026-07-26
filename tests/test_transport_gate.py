"""The transport gate — the whole safety property, previously untested.

Transport refuses unless the source is reconstructible FROM ITS REMOTE: a git
repo with an origin, nothing uncommitted, nothing unpushed, no untracked files.
What travels is what is *pushed*, so the destination is provably identical rather
than approximately so.

Every one of those branches was only ever exercised incidentally (a live dry run
happened to show "not a git repo" and "uncommitted changes"). Nothing asserted
that unpushed commits are refused, and nothing asserted the ONE exemption —
`.mcp.json`, which never travels by git and is regenerated at the destination, so
a copied one would carry the wrong hub URL to a box on another network.

Also covered here: the remote leg. It had only ever run against a real second
machine, which meant it could not be tested. `fake-ssh` drops the host argument
and runs the command locally, so the remote branch — remote existence probes, the
remote clone, the memory rsync, the staged-history rsync and the transport-recv
pipe — all execute on one box.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess

import pytest

SQUAD = pathlib.Path(__file__).resolve().parents[1] / "squad" / "squad"

pytestmark = pytest.mark.skipif(not SQUAD.exists(), reason="squad script not present")

FAKE_SSH = """#!/bin/sh
# Pretends to be ssh: drops the host argument and runs the command locally.
shift
exec /bin/sh -c "$*"
"""


@pytest.fixture
def env(tmp_path):
    home = tmp_path / "home"
    (home / ".config" / "squad").mkdir(parents=True)
    (home / ".mcp-hub").mkdir()
    (home / "Projects").mkdir()
    conf = home / ".config" / "squad" / "squad.conf"
    conf.write_text("", encoding="utf-8")
    e = dict(os.environ, HOME=str(home), SQUAD_CONF=str(conf), SQUAD_SOCKET="gatetest")
    return e, conf


def _run(env, *args, timeout=180) -> subprocess.CompletedProcess:
    e, _ = env
    return subprocess.run(["bash", str(SQUAD), *args],
                          capture_output=True, text=True, timeout=timeout, env=e)


def _git(path: pathlib.Path, *args):
    subprocess.run(["git", *args], cwd=path, capture_output=True, check=True)


def _pushed_repo(tmp_path: pathlib.Path, repo="demo", org="monkeypashion",
                 rewrite_origin=True) -> pathlib.Path:
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
    if rewrite_origin:
        _git(work, "remote", "set-url", "origin", f"git@github-{org}:{org}/{repo}.git")
    return work


def _enrol(conf: pathlib.Path, name: str, worktree: pathlib.Path, args="", cls=""):
    with conf.open("a", encoding="utf-8") as fh:
        fh.write(f"{name}|{worktree}||{args}|{cls}\n")


def _ws(tmp_path: pathlib.Path, name="side", settings="{}") -> pathlib.Path:
    ws = tmp_path / f"{name}.code-workspace"
    ws.write_text('{\n  "folders": [],\n  "settings": %s\n}\n' % settings, encoding="utf-8")
    return ws


# ---- the four refusal branches -------------------------------------------

def test_a_folder_with_no_git_is_refused(env, tmp_path):
    e, conf = env
    plain = tmp_path / "plain"
    plain.mkdir()
    _enrol(conf, "plain-box", plain)
    res = _run(env, "transport", "plain-box", "--to", str(_ws(tmp_path)), "--dry-run")
    assert res.returncode != 0
    assert "not a git repo" in (res.stdout + res.stderr)


def test_no_origin_remote_is_refused(env, tmp_path):
    """Nothing to reconstruct from: a clone would have no source of truth."""
    e, conf = env
    work = tmp_path / "orphan"
    work.mkdir()
    _git(work, "init", "-q")
    _enrol(conf, "orphan-box", work)
    res = _run(env, "transport", "orphan-box", "--to", str(_ws(tmp_path)), "--dry-run")
    assert res.returncode != 0
    assert "origin" in (res.stdout + res.stderr)


def test_uncommitted_changes_are_refused(env, tmp_path):
    e, conf = env
    work = _pushed_repo(tmp_path)
    (work / "README.md").write_text("edited but not committed\n", encoding="utf-8")
    _enrol(conf, "demo-box", work)
    res = _run(env, "transport", "demo-box", "--to", str(_ws(tmp_path)), "--dry-run")
    assert res.returncode != 0
    assert "uncommitted" in (res.stdout + res.stderr)


def test_unpushed_commits_are_refused(env, tmp_path):
    """THE branch nothing asserted. Transport moves what is PUSHED — an unpushed
    commit would simply not exist in the clone, silently."""
    e, conf = env
    work = _pushed_repo(tmp_path)
    (work / "later.txt").write_text("local only\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "not pushed")
    _enrol(conf, "demo-box", work)
    res = _run(env, "transport", "demo-box", "--to", str(_ws(tmp_path)), "--dry-run")
    assert res.returncode != 0
    assert "unpushed" in (res.stdout + res.stderr)


def test_untracked_files_are_refused(env, tmp_path):
    e, conf = env
    work = _pushed_repo(tmp_path)
    (work / "scratch.txt").write_text("untracked\n", encoding="utf-8")
    _enrol(conf, "demo-box", work)
    res = _run(env, "transport", "demo-box", "--to", str(_ws(tmp_path)), "--dry-run")
    assert res.returncode != 0
    assert "untracked" in (res.stdout + res.stderr)


def test_an_untracked_mcp_json_is_the_one_exemption(env, tmp_path):
    """.mcp.json never travels by git and is GENERATED at the destination — a
    copied one would carry this network's hub URL to a box on another. So it must
    not block, while any other untracked file must."""
    e, conf = env
    work = _pushed_repo(tmp_path)
    (work / ".mcp.json").write_text('{"mcpServers":{}}', encoding="utf-8")
    _enrol(conf, "demo-box", work)
    res = _run(env, "transport", "demo-box", "--to", str(_ws(tmp_path)), "--dry-run")
    assert res.returncode == 0, res.stdout + res.stderr
    # NB: assert on the refusal, not the word "untracked" — pytest's tmp_path
    # contains this test's own name, which contains that word.
    assert "REFUSED" not in res.stdout, res.stdout


# ---- destination safety --------------------------------------------------

def test_a_non_empty_destination_is_refused(env, tmp_path):
    """--dest at an occupied path must refuse rather than clone into it."""
    e, conf = env
    work = _pushed_repo(tmp_path)
    _enrol(conf, "demo-box", work)
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "someone-elses-file").write_text("keep me\n", encoding="utf-8")
    res = _run(env, "transport", "demo-box", "--to", str(_ws(tmp_path)), "--dest", str(occupied))
    assert res.returncode != 0
    assert "not empty" in (res.stdout + res.stderr)
    assert (occupied / "someone-elses-file").exists(), "a refusal must not disturb it"


# ---- the workspace's own declaration ------------------------------------

def test_a_workspace_declaring_comms_off_lands_the_clone_without_them(env, tmp_path):
    """dev-vm-1's general workspace states "no comms" in a COMMENT, which nothing
    can read, so a comms-armed agent transported there arrived comms-armed and
    contradicted the file's own intent. The machine-readable marker is honoured."""
    e, conf = env
    # keep the real bare-repo origin: this test performs an ACTUAL clone, so the
    # remote has to be reachable (a github- SSH alias is not).
    work = _pushed_repo(tmp_path, rewrite_origin=False)
    _enrol(conf, "demo-box", work,
           args="--continue --dangerously-load-development-channels server:hub")
    ws = _ws(tmp_path, "quiet", settings='{"squad.comms": false}')
    shim = tmp_path / "fake-ssh"
    shim.write_text(FAKE_SSH, encoding="utf-8")
    shim.chmod(0o755)
    res = _run(env, "transport", "demo-box", "--to", str(ws),
               "--host", "pretend-host", "--rsh", str(shim))
    assert res.returncode == 0, res.stdout + res.stderr
    assert "comms OFF" in res.stdout, res.stdout
    row = [ln for ln in conf.read_text().splitlines() if ln.startswith("demo-")][-1]
    assert "channels" not in row, f"clone kept the comms flag: {row}"
    assert "--continue" in row, "but it must keep the rest of its launch args"


# ---- unreachable far end -------------------------------------------------

def test_an_unreachable_host_refuses_with_advice(env, tmp_path):
    """/bin/false as the remote shell: every probe fails, nothing is attempted."""
    e, conf = env
    work = _pushed_repo(tmp_path, rewrite_origin=False)
    _enrol(conf, "demo-box", work)
    res = _run(env, "transport", "demo-box", "--to", str(_ws(tmp_path)),
               "--host", "nowhere", "--rsh", "/bin/false")
    assert res.returncode != 0
    out = res.stdout + res.stderr
    assert "cannot reach" in out, out
    assert "--rsh" in out, "the refusal should name the escape hatch"


def test_a_dry_run_against_an_unreachable_host_says_it_is_guessing(env, tmp_path):
    """A preview it could not verify must SAY so, not quietly print a plan.

    This is the same class as the two preview bugs found by running the real use
    cases: a plan the operator confirms has to be a plan that was checked.
    """
    e, conf = env
    work = _pushed_repo(tmp_path, rewrite_origin=False)
    b = tmp_path / "work" / "demo-two"
    subprocess.run(["git", "clone", "-q", str(tmp_path / "remotes" / "demo.git"), str(b)],
                   check=True, capture_output=True)
    _enrol(conf, "demo-a", work)
    _enrol(conf, "demo-b", b)
    src = tmp_path / "src.code-workspace"
    src.write_text(json.dumps({"folders": [{"path": str(work)}, {"path": str(b)}]}),
                   encoding="utf-8")
    res = _run(env, "transport", "workspace", str(src), "--to", str(_ws(tmp_path)),
               "--host", "nowhere", "--rsh", "/bin/false", "--dry-run")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "UNCHECKED" in res.stdout, res.stdout


# ---- partial failure in a fan-out ---------------------------------------

def test_one_agent_failing_does_not_abandon_the_rest(env, tmp_path):
    """A squad clone that dies halfway and says nothing is the worst outcome.

    The first agent's destination is pre-occupied so its transport fails; the
    second must still go, and the summary must count both honestly.
    """
    e, conf = env
    a = _pushed_repo(tmp_path, repo="demo", rewrite_origin=False)
    b = _pushed_repo(tmp_path, repo="other", rewrite_origin=False)
    _enrol(conf, "demo-a", a)
    _enrol(conf, "other-b", b)
    # Exhaust the destination search for 'demo' only. resolve_dest walks 20
    # candidates (demo, demo-far, demo-far-2 … demo-far-19), so occupying three
    # merely pushes it to the fourth — measured on the first attempt at this
    # test, which passed for the wrong reason.
    occupied = ["demo", "demo-far"] + [f"demo-far-{i}" for i in range(2, 20)]
    for n in occupied:
        d = pathlib.Path(e["HOME"]) / "Projects" / "code" / "remotes" / n
        d.mkdir(parents=True)
        (d / "occupied").write_text("x", encoding="utf-8")
    src = tmp_path / "src.code-workspace"
    src.write_text(json.dumps({"folders": [{"path": str(a)}, {"path": str(b)}]}),
                   encoding="utf-8")

    res = _run(env, "transport", "workspace", str(src), "--to", str(_ws(tmp_path, "far")))
    out = res.stdout + res.stderr
    landed = pathlib.Path(e["HOME"]) / "Projects" / "code" / "remotes" / "other"
    assert (landed / "README.md").exists(), f"the healthy agent was abandoned:\n{out}"
    assert "1 transported" in out, out
    assert "FAIL" in out, f"the failure must be NAMED, not silently counted:\n{out}"
    for d in occupied:
        p = pathlib.Path(e["HOME"]) / "Projects" / "code" / "remotes" / d
        assert (p / "occupied").exists(), "an occupied directory must be left alone"


# ---- destination toolchain ------------------------------------------------

def test_a_destination_without_mcp_hub_refuses_rather_than_guessing(env, tmp_path):
    """The condition really hit on dev-vm-1, and it is easy to hit.

    Identity is derived at the destination. For the mcp-hub repo itself the
    freshly-cloned cli.py can do it, but for ANY OTHER repo the destination needs
    `mcp-hub` on PATH — and a non-login ssh shell does not get ~/.local/bin, so a
    box that plainly has it can still fail to find it. Getting a name wrong here
    would silently give the clone the wrong identity, so it must refuse.
    """
    e, conf = env
    work = _pushed_repo(tmp_path, repo="demo", rewrite_origin=False)
    _enrol(conf, "demo-box", work)
    shim = tmp_path / "fake-ssh"
    shim.write_text(FAKE_SSH, encoding="utf-8")
    shim.chmod(0o755)
    # Keep the REAL PATH and drop only the directories that actually contain
    # mcp-hub. Hand-building a minimal PATH looks tidier and made this test
    # vacuous: it died at exit 127 on a missing `basename`, long before the
    # identity step, and still "passed" because the word mcp-hub appears in the
    # squad script's own path.
    kept = [d for d in e["PATH"].split(os.pathsep)
            if d and not os.path.exists(os.path.join(d, "mcp-hub"))]
    e2 = dict(e, PATH=os.pathsep.join(kept))
    assert subprocess.run(["sh", "-c", "command -v basename"], env=e2,
                          capture_output=True).returncode == 0, "PATH lost the toolchain"
    assert subprocess.run(["sh", "-c", "command -v mcp-hub"], env=e2,
                          capture_output=True).returncode != 0, "mcp-hub still reachable"
    res = subprocess.run(
        ["bash", str(SQUAD), "transport", "demo-box", "--to", str(_ws(tmp_path, "far")),
         "--host", "pretend-host", "--rsh", str(shim)],
        capture_output=True, text=True, timeout=180, env=e2)
    out = res.stdout + res.stderr
    assert res.returncode != 0, f"should have refused:\n{out}"
    assert "127" not in out, f"died on a missing tool, not the intended refusal:\n{out}"
    # Match the REFUSAL text specifically. "identity" alone also matches the
    # successful "identity suffix:" line printed just above it, which would let
    # this pass on the wrong evidence — the same mistake twice in one test.
    assert "cannot derive the agent name" in out, \
        f"refused for some other reason:\n{out[-600:]}"
    conf_text = conf.read_text()
    assert conf_text.count("demo-box") == 1, "a refusal must not add a roster row"


# ---- explicit overrides ---------------------------------------------------

def test_dest_override_lands_exactly_where_told(env, tmp_path):
    """--dest bypasses the derived location, for the one-off case."""
    e, conf = env
    work = _pushed_repo(tmp_path, rewrite_origin=False)
    _enrol(conf, "demo-box", work)
    where = tmp_path / "somewhere" / "else"
    res = _run(env, "transport", "demo-box", "--to", str(_ws(tmp_path)), "--dest", str(where))
    assert res.returncode == 0, res.stdout + res.stderr
    assert (where / "README.md").exists(), res.stdout


def test_the_clone_inherits_the_sources_class(env, tmp_path):
    """Lifecycle must travel: a squad-class agent cloned as faculty would stop
    being swept by `up`, and vice versa would auto-start something on-demand."""
    e, conf = env
    work = _pushed_repo(tmp_path, rewrite_origin=False)
    _enrol(conf, "demo-box", work, args="--continue", cls="squad")
    res = _run(env, "transport", "demo-box", "--to", str(_ws(tmp_path, "cls")))
    assert res.returncode == 0, res.stdout + res.stderr
    rows = [ln for ln in conf.read_text().splitlines() if ln.startswith("demo-")]
    assert len(rows) == 2, rows
    assert rows[-1].split("|")[4] == "squad", f"class did not travel: {rows[-1]}"


# ---- the remote leg, exercised locally ----------------------------------

def test_the_whole_remote_leg_runs_end_to_end(env, tmp_path):
    """Previously only reachable with a second machine, i.e. untestable.

    Exercises the remote branch: existence probes over the shell, the remote
    clone, the memory rsync, the staged-history rsync, and piping transport-recv
    to the far end.
    """
    e, conf = env
    # real clone ⇒ the origin must be reachable, so keep the bare-repo path.
    # That makes the derived project "remotes/demo", which is fine: what matters
    # is that the destination is derived from it at all.
    work = _pushed_repo(tmp_path, rewrite_origin=False)
    _enrol(conf, "demo-box", work, args="--continue")
    # give it memory to carry, keyed the way Claude keys it
    enc = str(work).replace("/", "-")
    mem = pathlib.Path(e["HOME"]) / ".claude" / "projects" / enc / "memory"
    mem.mkdir(parents=True)
    (mem / "a.md").write_text("remembered\n", encoding="utf-8")
    shim = tmp_path / "fake-ssh"
    shim.write_text(FAKE_SSH, encoding="utf-8")
    shim.chmod(0o755)

    res = _run(env, "transport", "demo-box", "--to", str(_ws(tmp_path, "far")),
               "--host", "pretend-host", "--rsh", str(shim))
    assert res.returncode == 0, res.stdout + res.stderr
    dest = pathlib.Path(e["HOME"]) / "Projects" / "code" / "remotes" / "demo"
    assert (dest / "README.md").exists(), f"clone did not land: {res.stdout}"
    assert (dest / ".mcp.json").exists(), "the destination generates its own .mcp.json"
    landed = pathlib.Path(e["HOME"]) / ".claude" / "projects" / str(dest).replace("/", "-")
    assert (landed / "memory" / "a.md").exists(), "memory did not rsync to the far end"
    cfg = json.loads((pathlib.Path(e["HOME"]) / ".mcp-hub" / "config.json").read_text())
    assert str(dest) in (cfg.get("workspaces") or {}), "no identity suffix registered"
    assert "STOPPED" in res.stdout, "a transported agent must never land running"
