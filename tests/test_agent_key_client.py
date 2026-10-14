"""MH-H1 client: a lane holds its hub-minted key and presents it everywhere.

The hub enforces a key only once the name is ARMED (a register presented
it), and from then on every request asserting the name must carry it. So
the client's job is completeness: the interactive session (headersHelper,
stamped by rebind-url), the heartbeat daemon, the Stop hook and every CLI
verb that speaks as the lane. A path that forgets the header is a lane that
locks itself out the moment enforcement starts.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import stat
import subprocess
from pathlib import Path

import pytest

from mcp_hub import cli

KEY = "ab" * 32
NAME = "mcp-hub-test-host"


def _write_key(name: str = NAME, key: str = KEY) -> Path:
    path = cli._agent_key_file(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(key)
    return path


def _ensure(monkeypatch, reply: str | Exception, *, force: bool = False):
    calls = []

    async def _claim(hub_url, name):
        calls.append(name)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(cli, "_claim_agent_key", _claim)
    args = argparse.Namespace(action="ensure", name=NAME, project=None,
                              force=force, hub_url="http://hub.invalid/mcp")
    return cli.agent_key_command(args), calls


def test_no_key_file_means_no_header():
    assert cli._with_agent_key(NAME) == {}


def test_a_held_key_is_presented():
    _write_key()
    assert cli._with_agent_key(NAME) == {"headers": {cli.AGENT_KEY_HEADER: KEY}}


def test_ensure_persists_0600_and_never_prints_the_key(monkeypatch, capsys):
    rc, calls = _ensure(monkeypatch, f"AGENT-KEY: {KEY}\nMinted for '{NAME}'.")
    assert rc == 0 and calls == [NAME]
    path = cli._agent_key_file(NAME)
    assert path.read_text() == KEY
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    out = capsys.readouterr()
    assert KEY not in out.out + out.err
    assert "minted" in out.out


def test_ensure_with_a_held_key_does_not_call_the_hub(monkeypatch):
    _write_key()
    rc, calls = _ensure(monkeypatch, "unused")
    assert rc == 0 and calls == []


def test_ensure_is_silent_while_keys_are_off(monkeypatch, capsys):
    rc, _ = _ensure(monkeypatch, "REFUSED: agent keys are off on this hub "
                                 "(MCP_HUB_AGENT_KEYS is unset or 'off')")
    assert rc == 0
    assert not cli._agent_key_file(NAME).exists()
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""


def test_a_claim_conflict_is_loud_and_writes_nothing(monkeypatch, capsys):
    rc, _ = _ensure(monkeypatch, f"REFUSED: '{NAME}' already holds an agent key")
    assert rc == 1
    assert not cli._agent_key_file(NAME).exists()
    assert "already holds" in capsys.readouterr().err


def test_ensure_fails_open_when_the_hub_is_down(monkeypatch):
    rc, _ = _ensure(monkeypatch, OSError("connection refused"))
    assert rc == 0
    assert not cli._agent_key_file(NAME).exists()


def test_header_prints_the_headers_object(capsys):
    args = argparse.Namespace(action="header", name=NAME, project=None,
                              force=False, hub_url="")
    assert cli.agent_key_command(args) == 0
    assert json.loads(capsys.readouterr().out) == {}
    _write_key()
    assert cli.agent_key_command(args) == 0
    assert json.loads(capsys.readouterr().out) == {cli.AGENT_KEY_HEADER: KEY}


def test_the_heartbeat_daemon_ensures_before_its_first_connect(monkeypatch):
    order = []
    monkeypatch.setattr(cli, "_claim_singleton", lambda name: Path("/dev/null"))
    monkeypatch.setattr(cli, "_release_singleton", lambda p: None)
    monkeypatch.setattr(cli, "agent_key_command",
                        lambda a: order.append(("ensure", a.name)) or 0)

    async def _loop(hub_url, name):
        order.append(("loop", name))
        return False

    monkeypatch.setattr(cli, "_heartbeat_loop", _loop)
    args = argparse.Namespace(name=NAME, project=None, hub_url="http://h/mcp")
    assert cli.heartbeat_daemon_command(args) == 0
    assert order == [("ensure", NAME), ("loop", NAME)]


def test_a_failing_ensure_never_stops_the_daemon(monkeypatch):
    monkeypatch.setattr(cli, "_claim_singleton", lambda name: Path("/dev/null"))
    monkeypatch.setattr(cli, "_release_singleton", lambda p: None)

    def _boom(a):
        raise RuntimeError("hub went away")

    ran = []

    async def _loop(hub_url, name):
        ran.append(name)
        return False

    monkeypatch.setattr(cli, "agent_key_command", _boom)
    monkeypatch.setattr(cli, "_heartbeat_loop", _loop)
    args = argparse.Namespace(name=NAME, project=None, hub_url="http://h/mcp")
    assert cli.heartbeat_daemon_command(args) == 0
    assert ran == [NAME]


def test_rebind_url_stamps_the_headers_helper(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_derive_agent_identity",
                        lambda cwd: (NAME, "org/mcp-hub"))
    mcp_json = tmp_path / ".mcp.json"
    mcp_json.write_text(json.dumps(
        {"mcpServers": {"hub": {"type": "http", "url": "http://h/mcp"}}}))
    args = argparse.Namespace(cwd=str(tmp_path), dry_run=False)
    assert cli.rebind_url_command(args) == 0
    hub = json.loads(mcp_json.read_text())["mcpServers"]["hub"]
    assert hub["headersHelper"] == cli._agent_key_helper_command(NAME)
    assert hub["headersHelper"].endswith(NAME)
    # A URL stamped before keys existed still gains the helper.
    hub.pop("headersHelper")
    mcp_json.write_text(json.dumps({"mcpServers": {"hub": hub}}))
    assert cli.rebind_url_command(args) == 0
    assert "headersHelper" in json.loads(mcp_json.read_text())["mcpServers"]["hub"]


def test_every_client_that_speaks_as_a_lane_presents_its_key():
    """Each streamablehttp_client in cli.py passes _with_agent_key, except
    the claim itself (no key yet) and the project-scoped memory reads (no
    agent name is asserted)."""
    src = Path(cli.__file__).read_text(encoding="utf-8")
    exempt = {"_claim_agent_key", "_memory_import", "_memory_verify"}
    missing = []
    for m in re.finditer(r"streamablehttp_client\((.{0,160})", src, re.S):
        fn = re.findall(r"^(?:async )?def (\w+)", src[:m.start()], re.M)[-1]
        if fn in exempt:
            continue
        if "_with_agent_key(" not in m.group(1):
            missing.append(fn)
    assert missing == [], missing


def _run_helper(name: str) -> dict:
    cmd = cli._agent_key_helper_command(name)
    out = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                         timeout=10, check=True)
    return json.loads(out.stdout)


@pytest.mark.skipif(os.name == "nt", reason="POSIX helper")
def test_the_helper_prints_the_key_or_nothing():
    assert _run_helper(NAME) == {}
    _write_key()
    assert _run_helper(NAME) == {cli.AGENT_KEY_HEADER: KEY}


@pytest.mark.skipif(os.name == "nt", reason="POSIX helper")
@pytest.mark.parametrize("bad", ["zz" * 32, "ab" * 31, "ab" * 32 + '"', ""])
def test_the_helper_never_emits_a_malformed_key(bad):
    _write_key(key=bad)
    assert _run_helper(NAME) == {}


@pytest.mark.skipif(os.name == "nt", reason="POSIX helper")
def test_the_helper_refuses_a_path_shaped_name():
    _write_key(name="x")
    for name in ("../agent-keys/x", "X", "a b"):
        cmd = f"{shlex.quote(str(cli._install_agent_key_helper()))} {shlex.quote(name)}"
        out = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                             timeout=10, check=True)
        assert json.loads(out.stdout) == {}


def test_the_helper_is_installed_once_and_executable():
    path = cli._install_agent_key_helper()
    mtime = path.stat().st_mtime_ns
    assert cli._install_agent_key_helper() == path
    assert path.stat().st_mtime_ns == mtime
    assert os.access(path, os.X_OK)


def test_header_without_a_name_derives_it_from_the_folder(monkeypatch, capsys,
                                                         tmp_path):
    seen = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_derive_agent_identity",
                        lambda cwd: seen.append(cwd) or (NAME, "org/r"))
    _write_key()
    args = argparse.Namespace(action="header", name=None, project=None,
                              force=False, hub_url="")
    assert cli.agent_key_command(args) == 0
    assert json.loads(capsys.readouterr().out) == {cli.AGENT_KEY_HEADER: KEY}
    assert seen == [str(tmp_path)]


def test_header_outside_a_hub_folder_is_empty(monkeypatch, capsys, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_derive_agent_identity", lambda cwd: (None, None))
    args = argparse.Namespace(action="header", name=None, project=None,
                              force=False, hub_url="")
    assert cli.agent_key_command(args) == 0
    assert json.loads(capsys.readouterr().out) == {}
