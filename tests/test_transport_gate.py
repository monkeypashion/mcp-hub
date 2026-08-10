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
import shutil
import subprocess

import pytest

SQUAD = pathlib.Path(__file__).resolve().parents[1] / "squad" / "squad"

pytestmark = pytest.mark.skipif(not SQUAD.exists(), reason="squad script not present")

# The remote-leg tests drive the REAL preflight against a shimmed far
# host — which is this machine. On a box without Claude Code the
# preflight HONESTLY refuses ("✗ claude — agents land but cannot
# start"), which is the behavior working, not failing. Skipped rather
# than stubbed: a preflight taught to ignore a missing claude in tests
# is a preflight nobody can trust. Found by the first bare-runner CI.
_needs_claude = pytest.mark.skipif(
    shutil.which("claude") is None,
    reason="remote leg needs Claude Code on the (shimmed) far host",
)

FAKE_SSH = """#!/bin/sh
# Pretends to be ssh: drops the host argument and runs the command locally.
shift
exec /bin/sh -c "$*"
"""


def _make_ready(home: pathlib.Path) -> None:
    """The readiness markers `squad preflight` looks for.

    Transport now REFUSES a destination that cannot host an agent, before any
    bytes move — so a remote-leg test has to stand up a destination that passes,
    exactly as an operator would with `squad bootstrap`. git/python3/mcp-hub/squad
    come from the inherited PATH; these are the per-home pieces.
    """
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    (home / ".claude" / "settings.json").write_text(
        '{"hooks":{"Stop":[{"matcher":"*","hooks":['
        '{"type":"command","command":"mcp-hub stop-hook"}]}]}}', encoding="utf-8")
    (home / ".claude.json").write_text('{"projects":{}}', encoding="utf-8")
    (home / ".mcp-hub").mkdir(parents=True, exist_ok=True)
    (home / ".mcp-hub" / "config.json").write_text('{"projects":[]}', encoding="utf-8")


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

@_needs_claude
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
    _make_ready(pathlib.Path(e["HOME"]))
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
    # This used to be caught LATE, by transport-recv, after the repo and memory
    # had already been shipped — "cannot derive the agent name". Preflight now
    # catches it before any bytes move, which is strictly better: the operator
    # gets a machine that is untouched rather than one holding a half-wired
    # agent. Assert the EARLY refusal, and that nothing landed.
    assert "nothing transported" in out, f"refused for some other reason:\n{out[-600:]}"
    assert "mcp-hub" in out
    dest_root = pathlib.Path(e["HOME"]) / "Projects" / "code"
    assert not dest_root.exists(), "bytes moved despite the refusal"
    conf_text = conf.read_text()
    assert conf_text.count("demo-box") == 1, "a refusal must not add a roster row"


# ---- the clone knows it is a clone ---------------------------------------

def _marker_dir(home: str, dest: pathlib.Path) -> pathlib.Path:
    enc = str(dest).replace("/", "-").rstrip("-")
    return pathlib.Path(home) / ".claude" / "projects" / enc / "memory"


def test_a_clone_is_told_that_it_is_a_clone(env, tmp_path):
    """The highest-value gap that was left, and a clone itself reported it.

    The identity suffix makes a clone distinguishable to US — roster, statusline,
    hub binding — and not to ITSELF. It resumes carrying the original's sense of
    standing, and none of that authority travelled with the memory.
    """
    e, conf = env
    work = _pushed_repo(tmp_path, rewrite_origin=False)
    _enrol(conf, "demo-box", work)
    res = _run(env, "transport", "demo-box", "--to", str(_ws(tmp_path, "cl")))
    assert res.returncode == 0, res.stdout + res.stderr
    dest = pathlib.Path(e["HOME"]) / "Projects" / "code" / "remotes" / "demo"
    mem = _marker_dir(e["HOME"], dest)
    marker = mem / "000_you_are_a_transported_clone.md"
    assert marker.exists(), f"no marker written:\n{res.stdout}"
    text = marker.read_text()
    assert "demo-box" in text, "the marker must name what this is a copy OF"
    # and it must be the FIRST thing in the index, since MEMORY.md is what loads
    first = (mem / "MEMORY.md").read_text().splitlines()[0]
    assert "000_you_are_a_transported_clone.md" in first, f"not indexed first: {first}"


def test_the_marker_describes_rather_than_forbidding(env, tmp_path):
    """A blanket "defer on everything" produces a uselessly timid agent.

    The line that matters: your own worktree is yours, shared things need
    authorisation addressed to YOU. Both halves have to be present, or the marker
    either does nothing or over-fires.
    """
    e, conf = env
    work = _pushed_repo(tmp_path, rewrite_origin=False)
    _enrol(conf, "demo-box", work)
    _run(env, "transport", "demo-box", "--to", str(_ws(tmp_path, "cl")))
    dest = pathlib.Path(e["HOME"]) / "Projects" / "code" / "remotes" / "demo"
    text = (_marker_dir(e["HOME"], dest) / "000_you_are_a_transported_clone.md").read_text()
    # collapse whitespace: this is prose and it hard-wraps, so a phrase can span
    # a newline ("...addressed\nto *you*") and never match as a literal substring
    low = " ".join(text.lower().split())
    assert "your own worktree is yours" in low, "must permit its own work"
    assert "authorisation addressed to" in low, "must gate SHARED resources"
    assert "mcp-hub identity" in low, "must tell the clone how to CHECK, not just assert"


def test_the_marker_never_breaks_a_transport(env, tmp_path):
    """Fail-soft: an unwritable memory dir is not a reason to fail the whole move."""
    e, conf = env
    work = _pushed_repo(tmp_path, rewrite_origin=False)
    _enrol(conf, "demo-box", work)
    dest = pathlib.Path(e["HOME"]) / "Projects" / "code" / "remotes" / "demo"
    blocked = _marker_dir(e["HOME"], dest).parent
    blocked.parent.mkdir(parents=True, exist_ok=True)
    blocked.write_text("not a directory", encoding="utf-8")   # mkdir will fail
    res = _run(env, "transport", "demo-box", "--to", str(_ws(tmp_path, "cl")))
    assert res.returncode == 0, f"a marker failure must not fail the transport:\n{res.stdout}"
    assert (dest / "README.md").exists(), "the clone still has to land"


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

@_needs_claude
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
    _make_ready(pathlib.Path(e["HOME"]))
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


def _behind_origin(work: pathlib.Path) -> str:
    """Advance origin past the source, the way a twin's push does.

    Returns the source's own HEAD — the commit the clone must come out on.
    """
    src_head = subprocess.run(["git", "-C", str(work), "rev-parse", "HEAD"],
                              capture_output=True, text=True, check=True).stdout.strip()
    # a second checkout pushes one more commit, leaving `work` behind its remote
    url = subprocess.run(["git", "-C", str(work), "remote", "get-url", "origin"],
                         capture_output=True, text=True, check=True).stdout.strip()
    other = work.parent / (work.name + "-twin")
    subprocess.run(["git", "clone", "-q", url, str(other)], check=True, capture_output=True)
    _git(other, "config", "user.email", "t@t")
    _git(other, "config", "user.name", "t")
    (other / "LATER.md").write_text("pushed after the source stopped looking\n", encoding="utf-8")
    _git(other, "add", "-A")
    _git(other, "commit", "-qm", "newer than the source")
    _git(other, "push", "-q", "origin", "HEAD")
    return src_head


def test_the_clone_lands_on_the_sources_commit_not_origins_tip(env, tmp_path):
    """A copy must not come out NEWER than the agent it copied.

    Measured on the first real transport of this repo: the source was several
    commits behind origin, and `git clone --branch master` gave a clone at the
    remote tip. Nothing was lost — the gate guarantees that — but "move this
    agent" then handed back something the agent had never been. Being a few
    commits behind a remote is the ordinary case, so this cannot be fixed by
    refusing it.
    """
    e, conf = env
    work = _pushed_repo(tmp_path, rewrite_origin=False)
    src_head = _behind_origin(work)
    _enrol(conf, "demo-box", work)

    res = _run(env, "transport", "demo-box", "--to", str(_ws(tmp_path)))
    assert res.returncode == 0, res.stdout + res.stderr
    dest = pathlib.Path(e["HOME"]) / "Projects" / "code" / "remotes" / "demo"
    landed = subprocess.run(["git", "-C", str(dest), "rev-parse", "HEAD"],
                            capture_output=True, text=True, check=True).stdout.strip()
    assert landed == src_head, "the clone is not on the source's commit"
    assert not (dest / "LATER.md").exists(), "it picked up a commit the source never had"
    # the branch NAME must survive, or the clone is detached and its next commit
    # goes nowhere
    branch = subprocess.run(["git", "-C", str(dest), "branch", "--show-current"],
                            capture_output=True, text=True, check=True).stdout.strip()
    assert branch, f"clone left on a detached HEAD: {res.stdout}"
    assert "pinned to the source's commit" in res.stdout, "it must say it moved the clone back"


def test_a_source_level_with_origin_is_not_reported_as_pinned(env, tmp_path):
    """The ordinary case must stay quiet — a note that always fires says nothing."""
    e, conf = env
    work = _pushed_repo(tmp_path, rewrite_origin=False)
    _enrol(conf, "demo-box", work)
    res = _run(env, "transport", "demo-box", "--to", str(_ws(tmp_path)))
    assert res.returncode == 0, res.stdout + res.stderr
    assert "pinned" not in res.stdout, res.stdout


@_needs_claude
def test_the_remote_leg_pins_the_clone_too(env, tmp_path):
    """Same rule across the network — and this branch is the one quoting breaks.

    The pin runs inside a double-quoted ssh command, so the source's sha has to
    expand HERE while the destination's HEAD expands THERE. Get it backwards and
    the comparison silently compares a string to itself.
    """
    e, conf = env
    work = _pushed_repo(tmp_path, rewrite_origin=False)
    src_head = _behind_origin(work)
    _enrol(conf, "demo-box", work)
    _make_ready(pathlib.Path(e["HOME"]))
    shim = tmp_path / "fake-ssh"
    shim.write_text(FAKE_SSH, encoding="utf-8")
    shim.chmod(0o755)

    res = _run(env, "transport", "demo-box", "--to", str(_ws(tmp_path, "far")),
               "--host", "pretend-host", "--rsh", str(shim))
    assert res.returncode == 0, res.stdout + res.stderr
    dest = pathlib.Path(e["HOME"]) / "Projects" / "code" / "remotes" / "demo"
    landed = subprocess.run(["git", "-C", str(dest), "rev-parse", "HEAD"],
                            capture_output=True, text=True, check=True).stdout.strip()
    assert landed == src_head, f"remote clone not pinned: {res.stdout}"
    assert not (dest / "LATER.md").exists()


TAB_TITLE = '"terminal.integrated.tabs.title": "${sequence}"'


def _valid_jsonc(path: pathlib.Path) -> dict:
    """The file must still be a workspace VSCode can load."""
    return json.loads(path.read_text(encoding="utf-8"))


def test_a_generated_workspace_is_born_with_the_settings_the_cockpit_needs(env, tmp_path):
    """Transport creates workspaces, so it must not create ones the cockpit warns about.

    tabs.title = ${sequence} is load-bearing: under VSCode's default every
    glyph/model/ctx% the painter pushes is silently discarded and tabs read
    "tmux <something>". That cost a day of ghost-chasing once already, the
    extension warns about it — and then transport started generating
    `"settings": {}` and became a second source of the same fault.
    """
    e, conf = env
    work = _pushed_repo(tmp_path, rewrite_origin=False)
    _enrol(conf, "demo-box", work)
    ws = tmp_path / "brandnew.code-workspace"
    assert not ws.exists()

    res = _run(env, "transport", "demo-box", "--to", str(ws))
    assert res.returncode == 0, res.stdout + res.stderr
    text = ws.read_text(encoding="utf-8")
    assert TAB_TITLE in text, f"generated workspace lacks the painter's setting:\n{text}"
    settings = _valid_jsonc(ws)["settings"]
    assert settings["terminal.integrated.tabs.description"] == "${progress}"
    # squad owns the tmux lifecycle; VSCode restoring its own idea of a pane on
    # top of that is a second, stale truth
    assert settings["terminal.integrated.enablePersistentSessions"] is False
    # and the folder still landed — the settings must not have displaced it
    assert _valid_jsonc(ws)["folders"], "no folder entry in the generated workspace"


def test_an_existing_workspace_is_told_about_it_never_rewritten(env, tmp_path):
    """A hand-kept file is the operator's. Report, don't 'help'."""
    e, conf = env
    work = _pushed_repo(tmp_path, rewrite_origin=False)
    _enrol(conf, "demo-box", work)
    ws = tmp_path / "mine.code-workspace"
    original = ('{\n  // my ordering, my comments\n  "folders": [],\n'
                '  "settings": { "terminal.integrated.fontSize": 13 }\n}\n')
    ws.write_text(original, encoding="utf-8")

    res = _run(env, "transport", "demo-box", "--to", str(ws))
    assert res.returncode == 0, res.stdout + res.stderr
    assert "tabs.title" in res.stdout, f"said nothing about it:\n{res.stdout}"
    text = ws.read_text(encoding="utf-8")
    assert "// my ordering, my comments" in text, "comments destroyed"
    assert '"terminal.integrated.fontSize": 13' in text, "settings rewritten behind the operator"
    assert TAB_TITLE not in text, "it edited settings it was only asked to report on"


@_needs_claude
def test_the_far_side_generates_the_same_settings(env, tmp_path):
    """transport-recv creates the workspace on the REMOTE, from its own copy of
    the skeleton — two writers, one convention, and only a test keeps them in
    step."""
    e, conf = env
    work = _pushed_repo(tmp_path, rewrite_origin=False)
    _enrol(conf, "demo-box", work)
    _make_ready(pathlib.Path(e["HOME"]))
    shim = tmp_path / "fake-ssh"
    shim.write_text(FAKE_SSH, encoding="utf-8")
    shim.chmod(0o755)
    ws = tmp_path / "faraway.code-workspace"
    assert not ws.exists()

    res = _run(env, "transport", "demo-box", "--to", str(ws),
               "--host", "pretend-host", "--rsh", str(shim))
    assert res.returncode == 0, res.stdout + res.stderr
    assert ws.is_file(), f"far side created no workspace: {res.stdout}"
    assert TAB_TITLE in ws.read_text(encoding="utf-8")
    assert _valid_jsonc(ws)["folders"], "folder entry missing on the far side"


# ---- duplicate: a second seat, same workspace ------------------------------

def _dup_env(env, tmp_path, ws_name="side"):
    """An enrolled, pushed repo that IS a folder of a workspace under $HOME.

    `squad duplicate` finds the workspace by scanning ~/Projects and ~, so the
    file has to live where the real thing looks — a fixture that puts it in
    tmp_path would exercise a lookup the operator never performs.
    """
    e, conf = env
    work = _pushed_repo(tmp_path, rewrite_origin=False)
    _enrol(conf, "demo-box", work)
    home = pathlib.Path(e["HOME"])
    (home / "Projects").mkdir(parents=True, exist_ok=True)
    ws = home / "Projects" / f"{ws_name}.code-workspace"
    ws.write_text('{\n  "folders": [\n    {\n      "name": "%s",\n      "path": "%s"\n    }\n  ],\n'
                  '  "settings": {}\n}\n' % (work.name, work), encoding="utf-8")
    return work, ws


def test_duplicate_finds_its_workspace_and_takes_the_first_free_number(env, tmp_path):
    e, conf = env
    work, ws = _dup_env(env, tmp_path)
    res = _run(env, "duplicate", "demo-box", "--dry-run")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "id-sfx : side" in res.stdout, res.stdout
    assert "id-sfx : side-2" not in res.stdout, "nothing is taken yet"


def _register_suffix(home: str, worktree: pathlib.Path, suffix: str):
    """What transport writes to mark a worktree as one of its clones."""
    cfg = pathlib.Path(home) / ".mcp-hub" / "config.json"
    data = json.loads(cfg.read_text()) if cfg.exists() else {}
    data.setdefault("workspaces", {})[str(worktree)] = suffix
    cfg.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def test_duplicate_skips_a_suffix_already_spoken_for(env, tmp_path):
    """Taken means "a clone of THIS repo still holds that suffix on disk".

    Read from the identity registry rather than the roster, because `squad rm`
    drops the row but leaves both the repo and its registry entry — and handing
    that number out again would leave two worktrees deriving ONE agent name.
    """
    e, conf = env
    work, ws = _dup_env(env, tmp_path)
    # a real second clone of the same origin, registered but NOT enrolled: this
    # is precisely the post-`squad rm` state a roster-based check misses
    url = subprocess.run(["git", "-C", str(work), "remote", "get-url", "origin"],
                         capture_output=True, text=True, check=True).stdout.strip()
    orphan = work.parent / "demo-side"
    subprocess.run(["git", "clone", "-q", url, str(orphan)], check=True, capture_output=True)
    _register_suffix(e["HOME"], orphan, "side")

    res = _run(env, "duplicate", "demo-box", "--dry-run")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "id-sfx : side-2" in res.stdout, res.stdout


def test_duplicate_reuses_a_number_whose_worktree_is_gone(env, tmp_path):
    """"Next FREE", not "one past the count".

    Otherwise numbers drift upward forever and leave gaps nobody can explain.
    Teardown deletes the worktree and unregisters, so the number must come back —
    and a stale entry pointing at nothing must not pin one.
    """
    e, conf = env
    work, ws = _dup_env(env, tmp_path)
    url = subprocess.run(["git", "-C", str(work), "remote", "get-url", "origin"],
                         capture_output=True, text=True, check=True).stdout.strip()
    orphan = work.parent / "demo-side"
    subprocess.run(["git", "clone", "-q", url, str(orphan)], check=True, capture_output=True)
    _register_suffix(e["HOME"], orphan, "side")
    assert "id-sfx : side-2" in _run(env, "duplicate", "demo-box", "--dry-run").stdout

    shutil.rmtree(orphan)          # what teardown does to the bytes
    out = _run(env, "duplicate", "demo-box", "--dry-run").stdout
    assert "id-sfx : side" in out and "id-sfx : side-2" not in out, \
        f"a freed number must come back, got:\n{out}"


def test_duplicate_refuses_to_guess_between_two_workspaces(env, tmp_path):
    """Guessing would put the copy in a window the operator wasn't looking at —
    and the NAME it gets depends on which workspace it joins."""
    e, conf = env
    work, ws = _dup_env(env, tmp_path, ws_name="alpha")
    second = pathlib.Path(e["HOME"]) / "Projects" / "beta.code-workspace"
    second.write_text('{\n  "folders": [\n    {\n      "path": "%s"\n    }\n  ],\n'
                      '  "settings": {}\n}\n' % work, encoding="utf-8")
    res = _run(env, "duplicate", "demo-box", "--dry-run")
    assert res.returncode != 0
    both = res.stdout + res.stderr
    assert "alpha.code-workspace" in both and "beta.code-workspace" in both, both
    assert "--to" in both, "it must say how to resolve it"


def test_duplicate_of_an_agent_in_no_workspace_says_so(env, tmp_path):
    e, conf = env
    work = _pushed_repo(tmp_path, rewrite_origin=False)
    _enrol(conf, "demo-box", work)
    res = _run(env, "duplicate", "demo-box", "--dry-run")
    assert res.returncode != 0
    assert "--to" in (res.stdout + res.stderr)


def test_duplicate_lands_a_real_second_seat(env, tmp_path):
    """End to end: its own worktree, its own identity, the SAME workspace, and a
    marker naming the agent it was copied from."""
    e, conf = env
    work, ws = _dup_env(env, tmp_path)
    enc = str(work).replace("/", "-")
    mem = pathlib.Path(e["HOME"]) / ".claude" / "projects" / enc / "memory"
    mem.mkdir(parents=True)
    (mem / "a.md").write_text("remembered\n", encoding="utf-8")

    res = _run(env, "duplicate", "demo-box")
    assert res.returncode == 0, res.stdout + res.stderr
    rows = [r for r in conf.read_text().splitlines() if r.strip()]
    assert len(rows) == 2, f"expected a second roster row:\n{conf.read_text()}"
    clone = pathlib.Path([r.split("|")[1] for r in rows if not r.startswith("demo-box|")][0])
    assert clone != work and clone.is_dir()
    cfg = json.loads((pathlib.Path(e["HOME"]) / ".mcp-hub" / "config.json").read_text())
    assert cfg["workspaces"][str(clone)] == "side", "the copy needs its own identity"
    # same workspace, now listing both
    folders = ws.read_text(encoding="utf-8")
    assert str(work) in folders and (str(clone) in folders or clone.name in folders), folders
    marker = (pathlib.Path(e["HOME"]) / ".claude" / "projects"
              / str(clone).replace("/", "-") / "memory"
              / "000_you_are_a_transported_clone.md")
    assert marker.is_file(), "a copy that doesn't know it's a copy is the whole hazard"
    assert "demo-box" in marker.read_text(encoding="utf-8"), "the marker must name the parent"
    assert "STOPPED" in res.stdout, "a duplicate must never land running"


def test_duplicate_can_leave_the_workspace_file_untouched(env, tmp_path):
    """--no-folder-entry: the caller owns the folder entry.

    The cockpit needs this because VSCode reloads the window when a workspace it
    has OPEN is edited externally. Everything ELSE must still happen — a flag
    that quietly skipped the roster row or the identity would be far worse than
    the reload it avoids.
    """
    e, conf = env
    work, ws = _dup_env(env, tmp_path)
    before = ws.read_text(encoding="utf-8")

    res = _run(env, "duplicate", "demo-box", "--no-folder-entry")
    assert res.returncode == 0, res.stdout + res.stderr
    assert ws.read_text(encoding="utf-8") == before, "the workspace file was edited"
    assert "left to the caller" in res.stdout, res.stdout
    # everything else still landed
    rows = [r for r in conf.read_text().splitlines() if r.strip()]
    assert len(rows) == 2, f"roster row missing:\n{conf.read_text()}"
    clone = pathlib.Path([r.split("|")[1] for r in rows if not r.startswith("demo-box|")][0])
    assert clone.is_dir(), "clone did not land"
    cfg = json.loads((pathlib.Path(e["HOME"]) / ".mcp-hub" / "config.json").read_text())
    assert cfg["workspaces"][str(clone)] == "side", "identity suffix not registered"


def test_a_plain_transport_still_writes_the_folder_entry(env, tmp_path):
    """The skip is opt-in. A CLI transport into a workspace that is NOT open has
    nobody else to add the folder, so it must keep doing it itself."""
    e, conf = env
    work, ws = _dup_env(env, tmp_path)
    res = _run(env, "duplicate", "demo-box")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "workspace folder added" in res.stdout, res.stdout
