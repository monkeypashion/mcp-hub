"""v2-fleet r19 (F60, T75; j6-a-key-header-reads-the-sessions-folder): the
key header reads the session's folder from its own Claude process.

Claude Code runs a user-scope connection's headersHelper with cwd = its
config folder, not the session's folder, so `mcp-hub agent-key header` found
no identity and printed {}. THE RULE: with no --name and only when
CLAUDE_CODE_MCP_SERVER_NAME is set, the header walks its parent chain (ppid
from /proc/<pid>/stat after the LAST ')'), passing only shells (sh, bash,
dash, zsh by comm or argv[0], a login '-' stripped), at most 8 steps; if the
first process that is not a shell is Claude (comm or argv[0]'s basename is
'claude'), the folder is that process's /proc cwd; otherwise os.getcwd().
CLAUDE_PID never chooses a folder. F58's line gains cwd_source (ancestor or
getcwd), ancestor (the escaped comm of the first non-shell process reached,
cut to 15 characters; null with no walk) and claude_pid (unset, match or
mismatch).

A STAND-IN CLAUDE is a real process exec'd through a symlink named claude
(so its comm and argv[0] are both 'claude'), with its cwd a lane folder. It
starts the helper as Claude Code does: `/bin/sh -c '<helper>'` with cwd H (a
folder with neither git nor marker), stdin at EOF, no --name,
CLAUDE_CODE_MCP_SERVER_NAME=hub, and CLAUDE_PID as each case says. Linux only
(/proc).
FALSIFIERS (red on the parent): S1 to S7 (the parent has no walk and no
cwd_source in its line). S8 is a guard. The carried tests are guards, in the
same command (they set no CLAUDE_CODE_MCP_SERVER_NAME).
"""

from __future__ import annotations

import json
import os
import secrets
import shlex
import subprocess
import sys
import time
from pathlib import Path

import pytest

from mcp_hub import cli

pytestmark = pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="needs /proc")

HEADER = "x-mcp-hub-agent-key"
KEY_A = secrets.token_hex(32)
KEY_B = secrets.token_hex(32)
SRC = str(Path(cli.__file__).resolve().parents[1])
HELPER = f"{shlex.quote(sys.executable)} -m mcp_hub.cli agent-key header"

# The stand-in's own code: run the command it is given (cwd, env), stdin at
# EOF, and print {rc, out, err} as JSON. Stdlib only.
STANDIN = r"""
import json, subprocess, sys
spec = json.loads(sys.argv[1])
p = subprocess.run(spec["cmd"], cwd=spec["cwd"], env=spec["env"], stdin=subprocess.DEVNULL,
                   capture_output=True, text=True, timeout=60)
print(json.dumps({"rc": p.returncode, "out": p.stdout, "err": p.stderr}))
"""
# A non-shell wrapper between the stand-in and the helper (S4).
WRAPPER = r"""
import json, subprocess, sys
spec = json.loads(sys.argv[1])
p = subprocess.run(spec["cmd"], cwd=spec["cwd"], env=spec["env"], stdin=subprocess.DEVNULL,
                   capture_output=True, text=True, timeout=60)
comm = open("/proc/self/comm").read().strip()
print(json.dumps({"rc": p.returncode, "out": p.stdout, "err": p.stderr, "comm": comm}))
"""


@pytest.fixture
def world(tmp_path, monkeypatch):
    state = tmp_path / "state" / "hub"
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("MCP_HUB_STATE_DIR", str(state))
    keys = state / "agent-keys"
    keys.mkdir(parents=True)
    folders = {}
    for lane, key in (("lane-a", KEY_A), ("lane-b", KEY_B)):
        f = tmp_path / "work" / lane
        (f / ".claude").mkdir(parents=True)
        (f / ".claude" / "hub-agent.json").write_text(json.dumps({"name": lane, "project": "p"}))
        (keys / f"{lane}.key").write_text(key + "\n")
        folders[lane] = f
    h = tmp_path / "dot-claude"
    h.mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    claude = bindir / "claude"
    claude.symlink_to(sys.executable)
    w = {"A": folders["lane-a"], "B": folders["lane-b"], "H": h, "state": state,
         "claude": str(claude), "bin": bindir, "procs": []}
    yield w
    for p in w["procs"]:
        if p.poll() is None:
            p.kill()
            p.wait(timeout=10)


def _env(w, *, server_name: bool = True, claude_pid: int | None = None) -> dict:
    env = dict(os.environ)
    for k in ("CLAUDE_PID", "CLAUDE_CODE_MCP_SERVER_NAME", "MCP_HUB_URL"):
        env.pop(k, None)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (SRC, env.get("PYTHONPATH", "")) if p)
    env["PWD"] = str(w["H"])
    if server_name:
        env["CLAUDE_CODE_MCP_SERVER_NAME"] = "hub"
    if claude_pid is not None:
        env["CLAUDE_PID"] = str(claude_pid)
    return env


def _log(w) -> list[dict]:
    path = w["state"] / "agent-key-header.jsonl"
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


def _standin(w, cwd: Path, cmd: list[str], env: dict, code: str = STANDIN) -> dict:
    """Run `cmd` (cwd H) as the child of a stand-in Claude whose cwd is `cwd`;
    return {rc, out, err, line} with the one new log line."""
    before = len(_log(w))
    spec = json.dumps({"cmd": cmd, "cwd": str(w["H"]), "env": env})
    p = subprocess.run([w["claude"], "-c", code, spec], cwd=str(cwd), env=env,
                       stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=90)
    assert p.returncode == 0, f"setup: the stand-in failed: {p.stderr[-400:]}"
    res = json.loads(p.stdout)
    lines = _log(w)[before:]
    assert len(lines) == 1, f"one new log line per run, got {len(lines)}: {res['err'][-300:]}"
    res["line"] = lines[0]
    return res


def _sleeper(w, cwd: Path) -> subprocess.Popen:
    """A second live stand-in Claude, idle, with its cwd a lane folder."""
    p = subprocess.Popen([w["claude"], "-c", "import time; time.sleep(120)"], cwd=str(cwd),
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    w["procs"].append(p)
    for _ in range(100):
        try:
            if Path(f"/proc/{p.pid}/cwd").resolve() == cwd.resolve():
                break
        except OSError:
            pass
        time.sleep(0.02)
    return p


def _no_key_text(res: dict, w) -> None:
    text = res["err"] + "\n" + (w["state"] / "agent-key-header.jsonl").read_text()
    for key in (KEY_A, KEY_B):
        for k in (key, key.upper()):
            for i in range(len(k) - 7):
                assert k[i:i + 8] not in text, "an 8-character run of a key is in a line or stderr"


def _a_key(res: dict) -> None:
    assert res["rc"] == 0, res
    assert res["out"] == json.dumps({HEADER: KEY_A}) + "\n", f"not A's key: {res['out']!r}"


def _empty(res: dict) -> None:
    assert res["rc"] == 0, res
    assert res["out"] == "{}\n", f"expected {{}}: {res['out']!r}"


def _same(path, folder: Path) -> bool:
    return path is not None and Path(path).resolve() == folder.resolve()


# =============================================================================
# S1 THE SESSION'S FOLDER
# =============================================================================


def test_s1_a_helper_under_claude_in_a_reads_as(world):
    w = world
    res = _standin(w, w["A"], ["/bin/sh", "-c", HELPER], _env(w))
    _a_key(res)
    ln = res["line"]
    assert _same(ln.get("cwd"), w["A"]), ln
    assert ln.get("cwd_source") == "ancestor", ln
    assert ln.get("ancestor") == "claude", ln
    assert ln.get("claude_pid") == "unset", ln
    assert (ln.get("source"), ln.get("name"), ln.get("reason")) == ("marker", "lane-a", "key"), ln
    _no_key_text(res, w)


# =============================================================================
# S2 NO SHELL BETWEEN
# =============================================================================


def test_s2_the_helper_started_directly_reads_as(world):
    w = world
    res = _standin(w, w["A"], shlex.split(HELPER), _env(w))
    _a_key(res)
    ln = res["line"]
    assert _same(ln.get("cwd"), w["A"]) and ln.get("cwd_source") == "ancestor", ln
    assert ln.get("ancestor") == "claude", ln
    _no_key_text(res, w)


# =============================================================================
# S3 CLAUDE_PID NEVER CHOOSES
# =============================================================================


def test_s3a_claude_pid_naming_another_claude_never_gives_its_key(world):
    w = world
    other = _sleeper(w, w["B"])
    res = _standin(w, w["A"], ["/bin/sh", "-c", HELPER], _env(w, claude_pid=other.pid))
    _a_key(res)
    assert res["line"].get("claude_pid") == "mismatch", res["line"]
    _no_key_text(res, w)


def test_s3b_claude_pid_naming_its_own_claude_is_a_match(world):
    w = world
    # The stand-in's pid is only known once it runs: it passes its own pid on.
    own = 'env={**spec["env"], "CLAUDE_PID": str(__import__("os").getpid())}'
    code = STANDIN.replace('env=spec["env"]', own)
    res = _standin(w, w["A"], ["/bin/sh", "-c", HELPER], _env(w), code=code)
    _a_key(res)
    assert res["line"].get("claude_pid") == "match", res["line"]
    _no_key_text(res, w)


# =============================================================================
# S4 ONLY SHELLS ARE PASSED
# =============================================================================


def test_s4_a_non_shell_between_stops_the_walk(world):
    w = world
    env = _env(w)
    inner = json.dumps({"cmd": ["/bin/sh", "-c", HELPER], "cwd": str(w["H"]), "env": env})
    res = _standin(w, w["A"], [sys.executable, "-c", WRAPPER, inner], env)
    wrapped = json.loads(res["out"])
    res = {**res, "out": wrapped["out"], "rc": wrapped["rc"], "err": res["err"] + wrapped["err"]}
    _empty(res)
    ln = res["line"]
    assert ln.get("reason") == "no identity", ln
    assert _same(ln.get("cwd"), w["H"]) and ln.get("cwd_source") == "getcwd", ln
    assert ln.get("ancestor") == wrapped["comm"][:15], (ln, wrapped["comm"])
    _no_key_text(res, w)


# =============================================================================
# S5 NO CLAUDE
# =============================================================================


def test_s5_no_claude_ancestor_and_claude_pid_naming_one_in_a(world):
    w = world
    standin = _sleeper(w, w["A"])
    before = len(_log(w))
    p = subprocess.run(["/bin/sh", "-c", HELPER], cwd=str(w["H"]),
                       env=_env(w, claude_pid=standin.pid), stdin=subprocess.DEVNULL,
                       capture_output=True, text=True, timeout=60)
    lines = _log(w)[before:]
    assert len(lines) == 1, f"one new log line per run, got {len(lines)}"
    res = {"rc": p.returncode, "out": p.stdout, "err": p.stderr, "line": lines[0]}
    _empty(res)
    ln = res["line"]
    assert ln.get("reason") == "no identity" and ln.get("cwd_source") == "getcwd", ln
    _no_key_text(res, w)


# =============================================================================
# S6 ONLY A HELPER RUN WALKS
# =============================================================================


def test_s6_without_the_server_name_there_is_no_walk(world):
    w = world
    res = _standin(w, w["A"], ["/bin/sh", "-c", HELPER], _env(w, server_name=False))
    _empty(res)
    ln = res["line"]
    assert ln.get("reason") == "no identity", ln
    assert _same(ln.get("cwd"), w["H"]), ln
    assert ln.get("cwd_source") == "getcwd", ln
    assert "ancestor" in ln and ln["ancestor"] is None, ln
    _no_key_text(res, w)


# =============================================================================
# S7 A NAME WITH PARENTHESES
# =============================================================================


def test_s7_a_shell_named_with_parentheses_is_passed(world):
    w = world
    script = w["bin"] / "w) (x"
    script.write_text(f"#!/bin/sh\n{HELPER}\nrc=$?\nexit $rc\n")
    script.chmod(0o755)
    res = _standin(w, w["A"], [str(script)], _env(w))
    _a_key(res)
    ln = res["line"]
    assert _same(ln.get("cwd"), w["A"]) and ln.get("cwd_source") == "ancestor", ln
    assert ln.get("ancestor") == "claude", ln
    _no_key_text(res, w)


# =============================================================================
# S8 --NAME STILL WINS (guard)
# =============================================================================


def test_s8_name_still_wins_under_the_chain(world):
    w = world
    res = _standin(w, w["A"], ["/bin/sh", "-c", HELPER + " --name lane-b"], _env(w))
    assert res["rc"] == 0 and res["out"] == json.dumps({HEADER: KEY_B}) + "\n", res
    ln = res["line"]
    assert (ln.get("source"), ln.get("name"), ln.get("reason")) == ("--name", "lane-b", "key"), ln
    _no_key_text(res, w)
