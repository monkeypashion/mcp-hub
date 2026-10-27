"""v2-fleet r16.1 (F55, T70; j6-a-folder-lanes-header-carries-its-key): a
folder lane's connection header carries its key, and a name that is not a
plain lane name never reads or writes a key file.

`mcp-hub agent-key header` (the headersHelper) named the lane with the
git-only _derive_agent_identity, so a lane named by its folder's
.claude/hub-agent.json held a key and connected without it. THE RULE: header
names the lane with _resolve_agent_identity(args, {"cwd": cwd}) (--name, the
derived git identity, then the marker) and never reads stdin; a key file is
only ever a plain name's (non-empty, no leading '-', [a-z0-9_-] only), for
every read and write; header prints a key only when it is 64 lowercase hex.

Off the seat: temporary HOME and MCP_HUB_STATE_DIR; git is never needed (the
git half replaces cli._git_remote_url). Paths opened are recorded with a
Python audit hook ("open" events), never atime.
FALSIFIERS (red on the parent): H1, H3, H4's ensure cases, H5. GUARDS (green
on the parent, which never reads a marker in header): H2, H4's header cases,
H6, (7). The carried key and rebind tests are (8), in the same command.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from mcp_hub import cli

HEADER = "x-mcp-hub-agent-key"
FOLDER_KEY = "cd" * 32
OTHER_KEY = "ef" * 32
BAD_NAMES = ["../../x", "a/b", "-x", "X", ""]

_OPENED: list[str] | None = None


def _audit(event, args):
    if _OPENED is not None and event == "open" and args:
        path = args[0]
        if isinstance(path, (str, bytes, os.PathLike)):
            try:
                _OPENED.append(os.path.realpath(os.fsdecode(path)))
            except Exception:  # noqa: BLE001
                pass


sys.addaudithook(_audit)


@pytest.fixture(autouse=True)
def _private(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("MCP_HUB_STATE_DIR", str(tmp_path / "state" / "hub"))
    monkeypatch.delenv("MCP_HUB_URL", raising=False)
    monkeypatch.setattr(cli, "_git_remote_url", lambda _cwd: None)   # no git
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))


def _hold(name: str, key: str) -> Path:
    path = Path(os.environ["MCP_HUB_STATE_DIR"]) / "agent-keys" / f"{name}.key"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(key + "\n", encoding="utf-8")
    return path


def _folder(tmp_path: Path, name: str | None, sub: str = "lane") -> Path:
    folder = tmp_path / "work" / sub
    folder.mkdir(parents=True)
    if name is not None:
        (folder / ".claude").mkdir()
        (folder / ".claude" / "hub-agent.json").write_text(
            json.dumps({"name": name, "project": "p"}), encoding="utf-8")
    return folder


def _header(folder: Path, monkeypatch, capsys, *extra: str) -> tuple[int, dict]:
    monkeypatch.chdir(folder)
    capsys.readouterr()
    rc = cli.main(["agent-key", "header", *extra])
    out = capsys.readouterr().out.strip()
    return rc, json.loads(out or "null")


# =============================================================================
# H1 A FOLDER LANE PRESENTS ITS KEY
# =============================================================================


def test_h1_a_folder_lane_presents_its_key(tmp_path, monkeypatch, capsys):
    folder = _folder(tmp_path, "folder-lane")
    _hold("folder-lane", FOLDER_KEY)
    rc, out = _header(folder, monkeypatch, capsys)
    assert rc == 0
    assert out == {HEADER: FOLDER_KEY}, out


# =============================================================================
# H2 GIT LANES UNCHANGED (guard)
# =============================================================================


def _git_clone(tmp_path: Path, monkeypatch, marker: str | None) -> tuple[Path, str]:
    clone = _folder(tmp_path, marker, "clone")
    monkeypatch.setattr(cli, "_git_remote_url",
                        lambda cwd: "git@github.com:org/repo.git"
                        if Path(cwd).resolve() == clone.resolve() else None)
    monkeypatch.setattr(cli, "_load_hub_config", lambda: {"projects": ["org/repo"]})
    derived, _project = cli._derive_agent_identity(str(clone))
    assert derived, "setup: no derived identity"
    return clone, derived


def test_h2_a_git_clone_prints_its_derived_lanes_key(tmp_path, monkeypatch, capsys):
    clone, derived = _git_clone(tmp_path, monkeypatch, None)
    _hold(derived, OTHER_KEY)
    rc, out = _header(clone, monkeypatch, capsys)
    assert rc == 0 and out == {HEADER: OTHER_KEY}, out


def test_h2_derived_wins_over_a_marker_in_a_clone(tmp_path, monkeypatch, capsys):
    clone, derived = _git_clone(tmp_path, monkeypatch, "marker-lane")
    _hold(derived, OTHER_KEY)
    _hold("marker-lane", FOLDER_KEY)
    rc, out = _header(clone, monkeypatch, capsys)
    assert rc == 0 and out == {HEADER: OTHER_KEY}, out


# =============================================================================
# H3 NO STDIN
# =============================================================================


def test_h3_header_never_reads_stdin(tmp_path):
    folder = _folder(tmp_path, "folder-lane")
    other = _folder(tmp_path, "other-lane", "other")
    _hold("folder-lane", FOLDER_KEY)
    _hold("other-lane", OTHER_KEY)
    src = str(Path(cli.__file__).resolve().parents[1])
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (src, env.get("PYTHONPATH", "")) if p)
    proc = subprocess.Popen(
        [sys.executable, "-m", "mcp_hub.cli", "agent-key", "header"],
        cwd=str(folder), env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE)
    try:
        # A hook payload naming another lane, and stdin left OPEN.
        proc.stdin.write(json.dumps({"cwd": str(other)}).encode())
        proc.stdin.flush()
        try:
            rc = proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            rc = None
        assert rc is not None, "header waited on its open stdin"
        out = proc.stdout.read().decode().strip()
        assert rc == 0, rc
        assert json.loads(out or "null") == {HEADER: FOLDER_KEY}, out
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
        proc.stdin.close()
        proc.stdout.close()
        proc.stderr.close()


# =============================================================================
# H4 A PATH-SHAPED NAME REACHES NO FILE
# =============================================================================


def _bait(tmp_path: Path) -> list[Path]:
    """A 64-hex key file wherever each bad name's key would resolve."""
    keys = Path(os.environ["MCP_HUB_STATE_DIR"]) / "agent-keys"
    spots = [keys / ".." / ".." / "x.key", keys / "a" / "b.key", keys / "-x.key",
             keys / "X.key", keys / ".key"]
    out = []
    for spot in spots:
        spot = Path(os.path.realpath(spot))
        spot.parent.mkdir(parents=True, exist_ok=True)
        spot.write_text(FOLDER_KEY + "\n", encoding="utf-8")
        out.append(spot)
    return out


@pytest.mark.parametrize("bad", BAD_NAMES)
def test_h4_header_for_a_path_shaped_name_opens_no_key_file(
        tmp_path, monkeypatch, capsys, bad):
    global _OPENED
    baits = {str(p) for p in _bait(tmp_path)}
    folder = _folder(tmp_path, bad)
    monkeypatch.chdir(folder)
    capsys.readouterr()
    _OPENED = []
    try:
        rc = cli.main(["agent-key", "header"])
        opened = set(_OPENED)
    finally:
        _OPENED = None
    out = capsys.readouterr().out.strip()
    assert rc == 0 and json.loads(out or "null") == {}, (bad, out)
    assert not opened & baits, f"{bad!r} opened {sorted(opened & baits)}"


@pytest.mark.parametrize("bad", [n for n in BAD_NAMES if n])
def test_h4_ensure_for_a_path_shaped_name_writes_nothing(
        tmp_path, monkeypatch, capsys, bad):
    async def _claim(_hub_url, name):
        return f"AGENT-KEY: {FOLDER_KEY}\nMinted for '{name}'."

    monkeypatch.setattr(cli, "_claim_agent_key", _claim)
    before = {p for p in tmp_path.rglob("*")}
    args = argparse.Namespace(action="ensure", name=bad, project=None, force=False,
                              hub_url="http://hub.invalid/mcp")
    capsys.readouterr()
    cli.agent_key_command(args)
    out = capsys.readouterr()
    after = {p for p in tmp_path.rglob("*")}
    assert after == before, f"{bad!r} wrote {sorted(map(str, after - before))}"
    assert "refused" in (out.out + out.err).lower(), (bad, out)


# =============================================================================
# H5 ONLY A WELL-FORMED KEY
# =============================================================================


@pytest.mark.parametrize("key", ["AB" * 32, "ab" * 31, "ab" * 32 + "a", "zz" * 32])
def test_h5_a_malformed_held_key_prints_nothing(tmp_path, monkeypatch, capsys, key):
    folder = _folder(tmp_path, None)
    _hold("plain-lane", key)
    rc, out = _header(folder, monkeypatch, capsys, "--name", "plain-lane")
    assert rc == 0 and out == {}, (key, out)


# =============================================================================
# H6 NO IDENTITY; (7) --name STILL WINS (guards)
# =============================================================================


def test_h6_no_identity_prints_an_empty_object(tmp_path, monkeypatch, capsys):
    folder = _folder(tmp_path, None)
    rc, out = _header(folder, monkeypatch, capsys)
    assert rc == 0 and out == {}, out


def test_7_name_still_wins(tmp_path, monkeypatch, capsys):
    folder = _folder(tmp_path, "folder-lane")
    _hold("folder-lane", FOLDER_KEY)
    _hold("named-lane", OTHER_KEY)
    rc, out = _header(folder, monkeypatch, capsys, "--name", "named-lane")
    assert rc == 0 and out == {HEADER: OTHER_KEY}, out
