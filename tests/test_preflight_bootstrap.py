"""Can a machine RECEIVE an agent — asked before any bytes move, and fixable.

The operator's near-term case is a brand-new Linux server: say "move that
workspace across" and have it happen. The cockpit offers any online Linux tailnet
peer, so a fresh box is offered before it can host anything — and transport used
to clone the repo and rsync memory across before transport-recv discovered there
was no mcp-hub to derive a name with. That leaves a half-wired agent on a machine
the operator was told was a valid destination.

A destination check that runs after the bytes have moved is a post-mortem.

These tests use a sandbox HOME plus a shim that pretends to be ssh, so the whole
remote path runs locally. Bootstrap's clone+venv steps are skipped by
pre-creating the repo and a stub binary — they need network and ~30s, and what
matters here is the wiring: PATH links, roster, opt-in, hooks and the trust file.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SQUAD = ROOT / "squad" / "squad"
BOOTSTRAP = ROOT / "squad" / "bootstrap-host"

pytestmark = pytest.mark.skipif(
    not SQUAD.exists() or not BOOTSTRAP.exists(), reason="squad scripts not present"
)


def _shim(tmp_path: pathlib.Path, home: pathlib.Path) -> pathlib.Path:
    """Pretends to be ssh: drops the host argument, runs locally under `home`."""
    p = tmp_path / "fake-ssh"
    p.write_text(
        "#!/bin/sh\nshift\n"
        f'exec env HOME="{home}" PATH="$PATH" /bin/sh -c "$*"\n',
        encoding="utf-8",
    )
    p.chmod(0o755)
    return p


def _fresh_home(tmp_path: pathlib.Path, name="home") -> pathlib.Path:
    h = tmp_path / name
    h.mkdir()
    return h


def _prepared_repo(home: pathlib.Path) -> pathlib.Path:
    """A repo + stub binary, so bootstrap skips the slow clone/venv steps."""
    code = home / "Projects" / "code" / "monkeypashion" / "mcp-hub"
    (code / ".venv" / "bin").mkdir(parents=True)
    (code / ".git").mkdir()
    stub = code / ".venv" / "bin" / "mcp-hub"
    stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    stub.chmod(0o755)
    (code / "squad").mkdir()
    (code / "squad" / "squad").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    return code


def _run(args, home: pathlib.Path, extra_env=None):
    env = dict(os.environ, HOME=str(home))
    env.update(extra_env or {})
    return subprocess.run(args, capture_output=True, text=True, timeout=300, env=env)


def test_a_brand_new_machine_reports_not_ready_and_names_the_fix(tmp_path):
    home = _fresh_home(tmp_path)
    res = _run(["bash", str(SQUAD), "preflight", "--host", "newbox",
                "--rsh", str(_shim(tmp_path, home))], tmp_path / "self")
    assert res.returncode != 0, "a fresh box must not report ready"
    out = res.stdout
    assert "NOT READY" in out
    assert "bootstrap" in out, "must name the command that fixes it"
    # the two that silently break a landed agent, rather than failing loudly
    assert "trustfile" in out, "no ~/.claude.json ⇒ the agent hangs on the trust dialog"
    assert "hooks" in out, "no stop-hook ⇒ the agent lands mute on the hub"


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


@_needs_claude
def test_bootstrap_makes_it_ready(tmp_path):
    home = _fresh_home(tmp_path)
    _prepared_repo(home)
    boot = _run(["bash", str(BOOTSTRAP), str(home / "Projects/code/monkeypashion/mcp-hub")], home)
    assert boot.returncode == 0, boot.stdout + boot.stderr
    res = _run(["bash", str(SQUAD), "preflight", "--host", "newbox",
                "--rsh", str(_shim(tmp_path, home))], tmp_path / "self")
    assert res.returncode == 0, res.stdout
    assert "READY" in res.stdout


def test_bootstrap_never_clobbers_existing_hooks(tmp_path):
    """settings.json is the OPERATOR's file. A bootstrap that replaced it would
    silently drop whatever else they run on Stop or SessionStart."""
    home = _fresh_home(tmp_path)
    _prepared_repo(home)
    (home / ".claude").mkdir()
    (home / ".claude" / "settings.json").write_text(json.dumps({
        "hooks": {"Stop": [{"matcher": "*", "hooks": [
            {"type": "command", "command": "/usr/local/bin/my-own-thing"}]}]},
        "somethingElse": {"keep": "me"},
    }), encoding="utf-8")

    assert _run(["bash", str(BOOTSTRAP),
                 str(home / "Projects/code/monkeypashion/mcp-hub")], home).returncode == 0
    data = json.loads((home / ".claude" / "settings.json").read_text())
    cmds = [h["command"] for g in data["hooks"]["Stop"] for h in g["hooks"]]
    assert "/usr/local/bin/my-own-thing" in cmds, "the operator's own hook was dropped"
    assert any("stop-hook" in c for c in cmds), "ours was not added"
    assert data["somethingElse"] == {"keep": "me"}, "unrelated settings were disturbed"


def test_bootstrap_is_idempotent(tmp_path):
    home = _fresh_home(tmp_path)
    _prepared_repo(home)
    repo = str(home / "Projects/code/monkeypashion/mcp-hub")
    first = _run(["bash", str(BOOTSTRAP), repo], home)
    second = _run(["bash", str(BOOTSTRAP), repo], home)
    assert second.returncode == 0, second.stdout + second.stderr
    assert "3 added" in first.stdout
    assert "0 added, 3 already present" in second.stdout, second.stdout
    data = json.loads((home / ".claude" / "settings.json").read_text())
    cmds = [h["command"] for g in data["hooks"]["SessionStart"] for h in g["hooks"]]
    assert len(cmds) == len(set(cmds)), f"hooks duplicated on re-run: {cmds}"


def test_bootstrap_does_not_overwrite_an_existing_trust_file(tmp_path):
    home = _fresh_home(tmp_path)
    _prepared_repo(home)
    (home / ".claude.json").write_text(json.dumps({"projects": {"/somewhere": {"x": 1}}}),
                                       encoding="utf-8")
    assert _run(["bash", str(BOOTSTRAP),
                 str(home / "Projects/code/monkeypashion/mcp-hub")], home).returncode == 0
    data = json.loads((home / ".claude.json").read_text())
    assert data["projects"]["/somewhere"] == {"x": 1}, "an existing trust registry was clobbered"


def _pushed_repo(tmp_path: pathlib.Path, repo="demo") -> pathlib.Path:
    bare = tmp_path / "remotes" / f"{repo}.git"
    bare.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True, capture_output=True)
    work = tmp_path / "work" / repo
    work.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "clone", "-q", str(bare), str(work)], check=True, capture_output=True)
    for k, v in (("user.email", "t@t"), ("user.name", "t")):
        subprocess.run(["git", "config", k, v], cwd=work, check=True, capture_output=True)
    (work / "README.md").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=work, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=work, check=True, capture_output=True)
    subprocess.run(["git", "push", "-q", "origin", "HEAD"], cwd=work, check=True,
                   capture_output=True)
    return work


def test_transport_to_an_unprepared_machine_moves_nothing(tmp_path):
    """THE user-centric fix: find out BEFORE the repo and memory are shipped.

    Previously the clone and the memory rsync happened first, and only then did
    transport-recv discover the box could not derive an agent name — leaving a
    half-wired agent on a machine the cockpit had offered as valid.
    """
    me = _fresh_home(tmp_path, "me")
    (me / ".config" / "squad").mkdir(parents=True)
    conf = me / ".config" / "squad" / "squad.conf"
    work = _pushed_repo(tmp_path)
    conf.write_text(f"demo-box|{work}|||\n", encoding="utf-8")
    far = _fresh_home(tmp_path, "far")            # brand-new, nothing installed
    ws = tmp_path / "t.code-workspace"
    ws.write_text('{"folders":[],"settings":{}}', encoding="utf-8")

    res = _run(["bash", str(SQUAD), "transport", "demo-box", "--to", str(ws),
                "--host", "newbox", "--rsh", str(_shim(tmp_path, far))],
               me, {"SQUAD_CONF": str(conf)})
    out = res.stdout + res.stderr
    assert res.returncode != 0, out
    assert "nothing transported" in out, out
    assert not (far / "Projects" / "code").exists(), "bytes moved despite the refusal"
    assert "demo-box" in conf.read_text() and conf.read_text().count("demo-box") == 1


def test_bootstrap_refuses_without_sudo_rather_than_taking_root(tmp_path):
    """Silently acquiring root on someone's server is not a transport tool's job."""
    home = _fresh_home(tmp_path)
    bin_ = tmp_path / "emptybin"
    bin_.mkdir()
    for t in ("sh", "bash", "printf", "grep", "cat", "mkdir", "ln", "rm", "command"):
        found = subprocess.run(["sh", "-c", f"command -v {t}"],
                               capture_output=True, text=True).stdout.strip()
        if found:
            (bin_ / t).symlink_to(found)
    res = _run(["bash", str(BOOTSTRAP), "/nonexistent"], home, {"PATH": str(bin_)})
    assert res.returncode != 0
    out = res.stdout + res.stderr
    assert "apt-get install" in out, "must print the exact line to run"
    assert "sudo" in out
