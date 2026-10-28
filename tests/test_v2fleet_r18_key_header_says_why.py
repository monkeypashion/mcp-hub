"""v2-fleet r18 (F58, T73; j6-a-key-header-says-why): each time a lane's hub
connection asks for its key, a local line says what it found and why.

F52's live proof failed when Claude Code ran the headersHelper and got 0
headers, and the helper left no trace of which branch it took. THE RULE: each
run of `mcp-hub agent-key header` appends ONE JSON line to
<state dir>/agent-key-header.jsonl (mode 0600). What the header prints is
unchanged byte for byte; a line that cannot be written is dropped silently;
over 256 KiB the log is moved to .1 (one old copy) before writing; only the
header action logs. No line ever holds the key, any 8-character run of it, a
malformed file's content or an exception's message.

The line's fields (the names this file pins; the text names their meaning):
  time    UTC, ISO 8601, ending Z
  cwd     the folder the name was resolved from
  pwd     the PWD environment variable as the run saw it
  source  "--name", "derived", "marker" or "none"
  name    the name, or null
  key_dir the key folder it looked in (<state dir>/agent-keys)
  module  the path of the mcp_hub package (or its file) that ran
  reason  "key", "no identity", "not plain", "no key file", "unreadable",
          "malformed" or "error: <exception class>"

Off the seat: temporary HOME and MCP_HUB_STATE_DIR; cli._git_remote_url is
replaced (no git); the header runs in-process as the headersHelper runs it
(cwd the folder, no --name, stdin at EOF).
FALSIFIERS (red on the parent, which writes no line): D1, D2, D3, D5 and (7)'s
line. GUARDS (green on the parent): D4 (output and rc unchanged when the log
cannot be written) and D6 (nothing else logs). The
carried r16.1 header, F56 bad-marker and agent-key client tests are (8), in
the same command.
"""

from __future__ import annotations

import io
import json
import os
import re
import stat
from pathlib import Path

import pytest

import mcp_hub
from mcp_hub import cli

HEADER = "x-mcp-hub-agent-key"
KEY = "0123456789abcdef" * 4
LOG_NAME = "agent-key-header.jsonl"
LIMIT = 256 * 1024
FIELDS = {"time", "cwd", "pwd", "source", "name", "key_dir", "module", "reason"}
PATH_FIELDS = {"cwd", "pwd", "key_dir", "module"}
TIME_RE = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?Z")

# Every secret-like text a line or stderr must never hold.
_SECRETS: list[str] = [KEY, KEY[:63], KEY.upper()]


@pytest.fixture(autouse=True)
def _private(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("MCP_HUB_STATE_DIR", str(tmp_path / "state" / "hub"))
    monkeypatch.delenv("MCP_HUB_URL", raising=False)
    monkeypatch.delenv("MCP_HUB_AGENT_KEY", raising=False)
    monkeypatch.setattr(cli, "_git_remote_url", lambda _cwd: None)   # no git
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(""))


def _state() -> Path:
    return Path(os.environ["MCP_HUB_STATE_DIR"])


def _log() -> Path:
    return _state() / LOG_NAME


def _lines() -> list[dict]:
    try:
        text = _log().read_text(encoding="utf-8")
    except (FileNotFoundError, IsADirectoryError):
        return []
    return [json.loads(ln) for ln in text.splitlines() if ln.strip()]


def _raw_log() -> str:
    try:
        return _log().read_text(encoding="utf-8")
    except (FileNotFoundError, IsADirectoryError):
        return ""


def _hold(name: str, content: str) -> Path:
    path = _state() / "agent-keys" / f"{name}.key"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _folder(tmp_path: Path, name: str | None, sub: str = "lane") -> Path:
    folder = tmp_path / "work" / sub
    folder.mkdir(parents=True)
    if name is not None:
        (folder / ".claude").mkdir()
        (folder / ".claude" / "hub-agent.json").write_text(
            json.dumps({"name": name, "project": "p"}), encoding="utf-8")
    return folder


def _header(folder: Path, monkeypatch, capsys, *extra: str) -> tuple[object, str, str]:
    """(rc or what it raised, stdout, stderr) of `agent-key header` run as the
    headersHelper runs it."""
    monkeypatch.chdir(folder)
    monkeypatch.setenv("PWD", str(folder))
    capsys.readouterr()
    try:
        rc: object = cli.main(["agent-key", "header", *extra])
    except Exception as exc:  # noqa: BLE001
        rc = f"raised {type(exc).__name__}"
    out = capsys.readouterr()
    return rc, out.out, out.err


def _no_secret(err: str, extra: tuple[str, ...] = ()) -> None:
    """No line of the log, nor stderr, holds a key or any 8-character run of
    one, nor any extra content named."""
    raw = _raw_log()
    for secret in _SECRETS:
        for i in range(len(secret) - 7):
            run = secret[i:i + 8]
            assert run not in raw, f"an 8-character run of a key is in the log: {run}"
            assert run not in err, f"an 8-character run of a key is on stderr: {run}"
    for text in extra:
        for line in _lines():
            for field, value in line.items():
                if field not in PATH_FIELDS:
                    assert text not in json.dumps(value), (field, value)
        assert text not in err, "a key file's content is on stderr"


def _check_line(line: dict, folder: Path, reason: str, source: str, name) -> None:
    assert set(line) >= FIELDS, f"fields missing: {sorted(FIELDS - set(line))}"
    assert line["reason"] == reason, line
    assert line["source"] == source, line
    assert line["name"] == name, line
    assert TIME_RE.fullmatch(str(line["time"])), line["time"]
    assert Path(line["cwd"]).resolve() == folder.resolve(), line["cwd"]
    assert line["pwd"] == str(folder), line["pwd"]
    assert Path(line["key_dir"]).resolve() == (_state() / "agent-keys").resolve(), line
    pkg = Path(mcp_hub.__file__).resolve()
    ok = {pkg, pkg.parent, Path(cli.__file__).resolve()}
    assert Path(line["module"]).resolve() in ok, line["module"]


# =============================================================================
# D1 EACH EMPTY HEADER SAYS WHY
# =============================================================================


def test_d1a_no_identity(tmp_path, monkeypatch, capsys):
    folder = _folder(tmp_path, None)
    rc, out, err = _header(folder, monkeypatch, capsys)
    assert rc == 0 and out == "{}\n", (rc, out)
    lines = _lines()
    assert len(lines) == 1, f"one line per run, got {len(lines)}"
    _check_line(lines[0], folder, "no identity", "none", None)
    assert stat.S_IMODE(_log().stat().st_mode) == 0o600, oct(_log().stat().st_mode)
    _no_secret(err)


def test_d1b_not_plain(tmp_path, monkeypatch, capsys):
    folder = _folder(tmp_path, "../x")
    rc, out, err = _header(folder, monkeypatch, capsys)
    assert rc == 0 and out == "{}\n", (rc, out)
    lines = _lines()
    assert len(lines) == 1, f"one line per run, got {len(lines)}"
    _check_line(lines[0], folder, "not plain", "marker", "../x")
    _no_secret(err)


def test_d1c_no_key_file(tmp_path, monkeypatch, capsys):
    folder = _folder(tmp_path, "folder-lane")
    rc, out, err = _header(folder, monkeypatch, capsys)
    assert rc == 0 and out == "{}\n", (rc, out)
    lines = _lines()
    assert len(lines) == 1, f"one line per run, got {len(lines)}"
    _check_line(lines[0], folder, "no key file", "marker", "folder-lane")
    _no_secret(err)


def test_d1d_unreadable(tmp_path, monkeypatch, capsys):
    if os.geteuid() == 0:
        pytest.skip("D1 (d) needs a non-root user: root reads a mode-000 file")
    folder = _folder(tmp_path, "folder-lane")
    path = _hold("folder-lane", KEY + "\n")
    path.chmod(0)
    try:
        rc, out, err = _header(folder, monkeypatch, capsys)
    finally:
        path.chmod(0o600)
    assert rc == 0 and out == "{}\n", (rc, out)
    lines = _lines()
    assert len(lines) == 1, f"one line per run, got {len(lines)}"
    _check_line(lines[0], folder, "unreadable", "marker", "folder-lane")
    _no_secret(err)


@pytest.mark.parametrize("content", ["abc", KEY[:63], KEY.upper()],
                         ids=["abc", "63-hex", "64-uppercase"])
def test_d1e_malformed(content, tmp_path, monkeypatch, capsys):
    folder = _folder(tmp_path, "folder-lane")
    _hold("folder-lane", content + "\n")
    rc, out, err = _header(folder, monkeypatch, capsys)
    assert rc == 0 and out == "{}\n", (rc, out)
    lines = _lines()
    assert len(lines) == 1, f"one line per run, got {len(lines)}"
    _check_line(lines[0], folder, "malformed", "marker", "folder-lane")
    _no_secret(err, (content,))


# =============================================================================
# D2 A FOUND KEY IS LOGGED WITHOUT IT
# =============================================================================


def test_d2_a_found_key_prints_as_before_and_logs_without_it(tmp_path, monkeypatch, capsys):
    folder = _folder(tmp_path, "folder-lane")
    _hold("folder-lane", KEY + "\n")
    rc, out, err = _header(folder, monkeypatch, capsys)
    assert rc == 0
    assert out == json.dumps({HEADER: KEY}) + "\n", out
    lines = _lines()
    assert len(lines) == 1, f"one line per run, got {len(lines)}"
    _check_line(lines[0], folder, "key", "marker", "folder-lane")
    _no_secret(err)


def test_d2_runs_add_one_line_each(tmp_path, monkeypatch, capsys):
    folder = _folder(tmp_path, "folder-lane")
    _hold("folder-lane", KEY + "\n")
    for _ in range(3):
        rc, out, err = _header(folder, monkeypatch, capsys)
        assert rc == 0 and out == json.dumps({HEADER: KEY}) + "\n"
    assert [ln["reason"] for ln in _lines()] == ["key"] * 3, _lines()
    _no_secret(err)


# =============================================================================
# D3 AN UNEXPECTED ERROR STILL CONNECTS
# =============================================================================


def test_d3_an_unexpected_error_prints_empty_and_logs_its_class_only(
        tmp_path, monkeypatch, capsys):
    folder = _folder(tmp_path, "folder-lane")
    _hold("folder-lane", KEY + "\n")

    def _boom(*_a, **_k):
        raise RuntimeError("k3y-text")

    monkeypatch.setattr(cli, "_derive_agent_identity", _boom)
    rc, out, err = _header(folder, monkeypatch, capsys)
    assert rc == 0 and out == "{}\n", (rc, out)
    lines = _lines()
    assert len(lines) == 1, f"one line per run, got {len(lines)}"
    assert lines[0]["reason"] == "error: RuntimeError", lines[0]
    assert "k3y-text" not in _raw_log(), "an exception's message is in the log"
    assert "k3y-text" not in err, "an exception's message is on stderr"
    _no_secret(err)


# =============================================================================
# D4 THE LOG NEVER BREAKS THE HEADER
# =============================================================================


@pytest.mark.parametrize("held", [False, True], ids=["no-key", "key"])
def test_d4_a_log_that_cannot_be_written_changes_nothing(held, tmp_path, monkeypatch, capsys):
    folder = _folder(tmp_path, "folder-lane")
    if held:
        _hold("folder-lane", KEY + "\n")
    _log().mkdir(parents=True)
    rc, out, err = _header(folder, monkeypatch, capsys)
    assert rc == 0, rc
    want = json.dumps({HEADER: KEY}) + "\n" if held else "{}\n"
    assert out == want, out
    assert _log().is_dir() and not any(_log().iterdir())
    _no_secret(err)


# =============================================================================
# D5 THE LOG STAYS SMALL
# =============================================================================


def test_d5_a_log_over_256_kib_moves_to_dot_1_before_the_line(tmp_path, monkeypatch, capsys):
    folder = _folder(tmp_path, "folder-lane")
    _log().parent.mkdir(parents=True, exist_ok=True)
    row = json.dumps({"reason": "old", "pad": "x" * 200}) + "\n"
    big = row * (LIMIT // len(row) + 1)
    assert len(big.encode()) > LIMIT
    _log().write_text(big, encoding="utf-8")
    old = _log().with_name(LOG_NAME + ".1")
    old.write_text("OLDER\n", encoding="utf-8")
    rc, out, err = _header(folder, monkeypatch, capsys)
    assert rc == 0 and out == "{}\n", (rc, out)
    assert old.read_text(encoding="utf-8") == big, "the big log was not moved to .1"
    lines = _lines()
    assert len(lines) == 1 and lines[0]["reason"] == "no key file", lines[:2]
    names = sorted(p.name for p in _state().iterdir() if p.name.startswith(LOG_NAME))
    assert names == [LOG_NAME, LOG_NAME + ".1"], names


def test_d5_a_log_under_the_limit_is_appended_to(tmp_path, monkeypatch, capsys):
    folder = _folder(tmp_path, "folder-lane")
    _log().parent.mkdir(parents=True, exist_ok=True)
    _log().write_text(json.dumps({"reason": "earlier"}) + "\n", encoding="utf-8")
    rc, out, _err = _header(folder, monkeypatch, capsys)
    assert rc == 0 and out == "{}\n"
    assert [ln["reason"] for ln in _lines()] == ["earlier", "no key file"], _lines()
    assert not _log().with_name(LOG_NAME + ".1").exists()


# =============================================================================
# D6 ONLY THE HEADER LOGS (guard)
# =============================================================================


def test_d6_ephemeral_clients_and_ensure_write_no_line(tmp_path, monkeypatch, capsys):
    _hold("folder-lane", KEY + "\n")
    assert cli._agent_key_headers("folder-lane") == {HEADER: KEY}
    assert cli._with_agent_key("folder-lane") == {"headers": {HEADER: KEY}}
    assert cli._agent_key_headers("../x") == {}
    (_state() / "agent-keys" / "folder-lane.key").unlink()
    rc = cli.main(["agent-key", "ensure", "--name", "folder-lane",
                   "--hub-url", "http://127.0.0.1:9/mcp"])
    assert rc == 0
    assert not _log().exists(), _raw_log()[:400]
    _no_secret(capsys.readouterr().err)


# =============================================================================
# (7) --NAME STILL WINS
# =============================================================================


def test_7_name_wins_and_logs_its_source(tmp_path, monkeypatch, capsys):
    folder = _folder(tmp_path, "marker-lane")
    _hold("named-lane", KEY + "\n")
    rc, out, err = _header(folder, monkeypatch, capsys, "--name", "named-lane")
    assert rc == 0 and out == json.dumps({HEADER: KEY}) + "\n", (rc, out)
    lines = _lines()
    assert len(lines) == 1, f"one line per run, got {len(lines)}"
    _check_line(lines[0], folder, "key", "--name", "named-lane")
    _no_secret(err)
