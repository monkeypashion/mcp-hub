"""v2-fleet r18 (F56, T71; j6-a-bad-marker-names-no-lane): a folder marker
that cannot be read as a lane names no lane, and never breaks a hook or a
hub connection.

Since r16.1 `mcp-hub agent-key header` (the headersHelper) resolves the lane
on every hub connection through cli._discover_agent_from_marker, which raised
for a .claude/hub-agent.json that is not UTF-8 (UnicodeDecodeError) or is
valid JSON but not an object ([], "x", 42, null, true: .get on it). THE RULE:
the reader returns (None, None) for any marker it cannot read as a JSON
object, whatever the reason, and never raises; a name or project that is not
a non-empty string is None, as before. The header then prints {} and the
hooks no-op, exactly as for a folder with no marker.

Off the seat: temporary HOME and MCP_HUB_STATE_DIR; the in-process cases
replace cli._git_remote_url (no git); the subprocess cases run
`python -m mcp_hub.cli` in the folder with stdin at EOF, the way Claude Code
runs the headersHelper and the SessionStart hook.
FALSIFIERS (red on the parent): B1, B2 and B3 for the shapes [], "x", 42,
null, true and the bytes ff fe 7b 7d. GUARDS (green on the parent): the empty
file in B1 to B3, (4) and (5). The carried r16.1 header and agent-key client
tests are (6), in the same command.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from mcp_hub import cli

HEADER = "x-mcp-hub-agent-key"
FOLDER_KEY = "5a" * 32
HUB_URL = "http://127.0.0.1:9/mcp"

# Each bad shape, as the bytes of .claude/hub-agent.json.
BAD = {
    "list": b"[]",
    "string": b'"x"',
    "number": b"42",
    "null": b"null",
    "true": b"true",
    "not-utf8": bytes([0xFF, 0xFE, 0x7B, 0x7D]),
    "empty": b"",
}
# The shapes the parent raises on (the empty file it already read as no lane).
RAISED_ON_PARENT = ["list", "string", "number", "null", "true", "not-utf8"]


@pytest.fixture(autouse=True)
def _private(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("MCP_HUB_STATE_DIR", str(tmp_path / "state" / "hub"))
    monkeypatch.delenv("MCP_HUB_URL", raising=False)
    monkeypatch.delenv("MCP_HUB_AGENT_KEY", raising=False)
    monkeypatch.setattr(cli, "_git_remote_url", lambda _cwd: None)   # no git
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))


def _folder(tmp_path: Path, raw: bytes | None, sub: str = "lane") -> Path:
    folder = tmp_path / "work" / sub
    folder.mkdir(parents=True)
    if raw is not None:
        (folder / ".claude").mkdir()
        (folder / ".claude" / "hub-agent.json").write_bytes(raw)
    return folder


def _hold(name: str, key: str) -> Path:
    path = Path(os.environ["MCP_HUB_STATE_DIR"]) / "agent-keys" / f"{name}.key"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(key + "\n", encoding="utf-8")
    return path


def _read(folder: Path):
    """The reader's answer, or what it raised, so a raise is a failed
    assertion and not an error."""
    try:
        return cli._discover_agent_from_marker(str(folder))
    except Exception as exc:  # noqa: BLE001
        return f"raised {type(exc).__name__}"


def _cli(folder: Path, *argv: str, stdin: bytes = b"") -> subprocess.CompletedProcess:
    src = str(Path(cli.__file__).resolve().parents[1])
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (src, env.get("PYTHONPATH", "")) if p)
    env["PWD"] = str(folder)
    return subprocess.run(
        [sys.executable, "-m", "mcp_hub.cli", *argv],
        cwd=str(folder), input=stdin, capture_output=True, env=env, timeout=60)


# =============================================================================
# B1 EVERY BAD SHAPE NAMES NO LANE
# =============================================================================


@pytest.mark.parametrize("shape", list(BAD))
def test_b1_a_bad_marker_names_no_lane_and_never_raises(shape, tmp_path):
    folder = _folder(tmp_path, BAD[shape])
    assert _read(folder) == (None, None), f"{shape}: {_read(folder)!r}"


# =============================================================================
# B2 THE CONNECTION CARRIES ON
# =============================================================================


@pytest.mark.parametrize("shape", list(BAD))
def test_b2_the_header_prints_empty_and_rc_0(shape, tmp_path):
    folder = _folder(tmp_path, BAD[shape])
    p = _cli(folder, "agent-key", "header")
    assert p.returncode == 0, f"{shape}: rc {p.returncode}: {p.stderr.decode()[-400:]}"
    assert p.stdout.decode() == "{}\n", f"{shape}: {p.stdout!r}"


# =============================================================================
# B3 THE HOOKS CARRY ON
# =============================================================================


@pytest.mark.parametrize("shape", list(BAD))
def test_b3_session_start_exits_0_and_asks_no_lane_to_register(shape, tmp_path):
    folder = _folder(tmp_path, BAD[shape])
    payload = json.dumps({"hook_event_name": "SessionStart", "cwd": str(folder),
                          "session_id": "r18-b3"}).encode()
    p = _cli(folder, "session-start", "--hub-url", HUB_URL, stdin=payload)
    assert p.returncode == 0, f"{shape}: rc {p.returncode}: {p.stderr.decode()[-400:]}"
    out = p.stdout.decode()
    assert "register" not in out.lower(), f"{shape}: a lane was named: {out[:300]}"
    assert out.strip() == "", f"{shape}: {out[:300]}"


# =============================================================================
# GUARDS
# =============================================================================


def test_4_a_valid_marker_still_names_its_lane_and_presents_its_key(
        tmp_path, monkeypatch, capsys):
    folder = _folder(tmp_path, json.dumps({"name": "folder-lane", "project": "p"}).encode())
    assert _read(folder) == ("folder-lane", "p")
    _hold("folder-lane", FOLDER_KEY)
    monkeypatch.chdir(folder)
    capsys.readouterr()
    rc = cli.main(["agent-key", "header"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == {HEADER: FOLDER_KEY}


def test_4_a_name_that_is_not_a_string_names_no_lane(tmp_path):
    folder = _folder(tmp_path, json.dumps({"name": 5}).encode())
    assert _read(folder) == (None, None)


def test_5_an_opted_in_git_clones_derived_lane_is_unchanged(tmp_path, monkeypatch):
    clone = _folder(tmp_path, BAD["list"], "clone")
    monkeypatch.setattr(cli, "_git_remote_url",
                        lambda cwd: "git@github.com:org/repo.git"
                        if Path(cwd).resolve() == clone.resolve() else None)
    monkeypatch.setattr(cli, "_load_hub_config", lambda: {"projects": ["org/repo"]})
    derived = cli._derive_agent_identity(str(clone))
    assert derived[0], "setup: no derived identity"
    args = cli.build_parser().parse_args(["agent-key", "header"])
    try:
        got = cli._resolve_agent_identity(args, {"cwd": str(clone)})
    except Exception as exc:  # noqa: BLE001
        got = f"raised {type(exc).__name__}"
    assert got == derived, got
