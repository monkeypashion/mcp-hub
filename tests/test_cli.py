"""Tests for the cli stop-hook subcommand.

Covers the pure decision logic (build_hook_response), the text-extraction
helper, the fail-open contract on hub errors, and end-to-end via the SDK's
in-memory transport so we exercise the real MCP call path.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from mcp_hub.cli import (
    _discover_agent_from_marker,
    _extract_text,
    _resolve_agent_identity,
    build_hook_response,
    build_parser,
    stop_hook_command,
)


# ---------------------------------------------------------------------------
# build_hook_response — pure decision logic
# ---------------------------------------------------------------------------


def test_no_messages_no_block():
    """Empty inbox + bound = nothing to do, Stop proceeds normally."""
    assert build_hook_response(
        agent_name="alice",
        project="proj",
        messages_text="",
        is_bound=True,
    ) is None


def test_no_messages_unbound_still_no_block():
    """Drifted-but-empty: don't proactively re-register on every Stop. The
    rebind hint rides along when there's already actionable content. Empty
    case = let it ride; next message will surface both."""
    assert build_hook_response(
        agent_name="alice",
        project="proj",
        messages_text="",
        is_bound=False,
    ) is None


def test_messages_bound_emits_block_with_content():
    response = build_hook_response(
        agent_name="alice",
        project="proj",
        messages_text="[10:00] **bob**: hello there",
        is_bound=True,
    )
    assert response is not None
    assert response["decision"] == "block"
    assert "hello there" in response["reason"]
    assert "**bob**" in response["reason"]
    # Discipline reminder should be in the reason
    assert "Discipline" in response["reason"]


def test_messages_unbound_emits_block_with_rebind_hint():
    response = build_hook_response(
        agent_name="alice",
        project="my-proj",
        messages_text="[10:00] **bob**: ping",
        is_bound=False,
    )
    assert response is not None
    reason = response["reason"]
    assert "ping" in reason
    # Rebind hint must include the agent's exact name + project for copy-paste
    assert 'register(name="alice", project="my-proj")' in reason
    assert "NOT bound" in reason


def test_messages_unbound_no_project_still_emits_rebind():
    """project is optional — rebind hint should still appear with just
    name="..." form."""
    response = build_hook_response(
        agent_name="alice",
        project=None,
        messages_text="[10:00] **bob**: hi",
        is_bound=False,
    )
    assert response is not None
    assert 'register(name="alice")' in response["reason"]
    assert 'project=' not in response["reason"]  # no empty project= arg


def test_block_reason_contains_messages_verbatim():
    """The queued message content must be passed through unchanged so Claude
    can quote/respond accurately. No paraphrasing."""
    msg_body = "[09:30] **dt**: please review PR #42 — RA already approved"
    response = build_hook_response(
        agent_name="alice",
        project="proj",
        messages_text=msg_body,
        is_bound=True,
    )
    assert msg_body in response["reason"]


# ---------------------------------------------------------------------------
# _extract_text helper
# ---------------------------------------------------------------------------


class _MockBlock:
    def __init__(self, text):
        self.text = text


class _MockResult:
    def __init__(self, content):
        self.content = content


def test_extract_text_from_result_with_content():
    result = _MockResult([_MockBlock("hello")])
    assert _extract_text(result) == "hello"


def test_extract_text_from_list_of_blocks():
    result = [_MockBlock("hello")]
    assert _extract_text(result) == "hello"


def test_extract_text_returns_first_text_block():
    result = _MockResult([_MockBlock("first"), _MockBlock("second")])
    assert _extract_text(result) == "first"


def test_extract_text_handles_none():
    assert _extract_text(None) == ""


def test_extract_text_handles_empty_content():
    assert _extract_text(_MockResult([])) == ""


# ---------------------------------------------------------------------------
# stop_hook_command — fail-open contract
# ---------------------------------------------------------------------------


def test_fail_open_on_hub_exception(capsys):
    """If _query_hub raises (network down, hub crashed, anything), the
    command MUST exit 0 with no stdout. The whole point of fail-open is
    that hub flakiness can't block an agent's Stop."""
    args = argparse.Namespace(
        name="alice", project=None, hub_url="http://nowhere.invalid/mcp"
    )

    with patch("mcp_hub.cli._query_hub", side_effect=ConnectionError("boom")):
        rc = stop_hook_command(args)

    assert rc == 0
    captured = capsys.readouterr()
    assert captured.out == ""  # nothing on stdout (no hook block)
    assert "boom" in captured.err  # but logged to stderr for debugging


def test_no_messages_outputs_nothing(capsys):
    args = argparse.Namespace(name="alice", project=None, hub_url="http://x/mcp")

    async def _fake_query(_url, _name):
        return ("", True)  # no messages, bound

    with patch("mcp_hub.cli._query_hub", side_effect=_fake_query):
        rc = stop_hook_command(args)

    assert rc == 0
    captured = capsys.readouterr()
    assert captured.out == ""


def test_messages_present_outputs_valid_hook_json(capsys):
    args = argparse.Namespace(
        name="alice", project="proj", hub_url="http://x/mcp"
    )

    async def _fake_query(_url, _name):
        return ("[10:00] **bob**: hello", True)

    with patch("mcp_hub.cli._query_hub", side_effect=_fake_query):
        rc = stop_hook_command(args)

    assert rc == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["decision"] == "block"
    assert "hello" in payload["reason"]


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def test_parser_args_free_for_auto_discovery():
    """`--name` is now optional. Bare `stop-hook` triggers auto-discovery
    from <cwd>/.claude/hub-agent.json via the hook's stdin payload. This is
    the canonical shape for a global settings.json hook covering many agents."""
    parser = build_parser()
    args = parser.parse_args(["stop-hook"])
    assert args.subcommand == "stop-hook"
    assert args.name is None
    assert args.project is None


def test_parser_explicit_name_still_works():
    """Explicit --name overrides auto-discovery — useful for tests, manual
    invocations, or non-standard setups."""
    parser = build_parser()
    args = parser.parse_args(["stop-hook", "--name", "alice"])
    assert args.subcommand == "stop-hook"
    assert args.name == "alice"
    assert args.project is None
    # hub_url defaults from env or built-in
    assert args.hub_url


def test_parser_accepts_full_stop_hook_args():
    parser = build_parser()
    args = parser.parse_args(
        [
            "stop-hook",
            "--name", "alice",
            "--project", "myproj",
            "--hub-url", "http://localhost:9090/mcp",
        ]
    )
    assert args.name == "alice"
    assert args.project == "myproj"
    assert args.hub_url == "http://localhost:9090/mcp"


# ---------------------------------------------------------------------------
# Integration — exercises the real MCP call path against an in-process server
# ---------------------------------------------------------------------------


@pytest.fixture
async def live_hub(tmp_path: Path):
    """Start a streamable-http hub on localhost so the cli can hit it via
    a real network call. Yields the URL; teardown stops the server."""
    import socket
    import threading
    import time as _time

    from mcp_hub.server import create_server

    # Find a free port
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    db_path = tmp_path / "live.db"
    server = create_server(db_path=db_path, host="127.0.0.1", port=port)

    # Run the server in a thread so the test can hit it via real HTTP.
    # We don't bother with the reaper here — the test is short-lived.
    stop_event = threading.Event()

    def _serve():
        import asyncio as _asyncio
        loop = _asyncio.new_event_loop()
        _asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(server.run_streamable_http_async())
        except Exception:
            pass
        finally:
            stop_event.set()

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()

    # Poll until the server is responsive
    import urllib.request
    import urllib.error
    deadline = _time.time() + 5.0
    while _time.time() < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/mcp", timeout=0.5)
        except urllib.error.HTTPError:
            break  # 405/406/etc — server is up
        except (urllib.error.URLError, ConnectionError, OSError):
            _time.sleep(0.1)
            continue
        else:
            break

    yield f"http://127.0.0.1:{port}/mcp", server

    # Test ends; thread is daemon so it dies with the process. We don't have
    # a clean shutdown path for run_streamable_http_async without uvicorn
    # signals, so rely on daemon-thread cleanup.


async def test_integration_no_messages_emits_nothing(live_hub):
    """Real cli call via real MCP transport — empty inbox should emit nothing."""
    url, _server = live_hub
    args = argparse.Namespace(name="ghost-agent", project=None, hub_url=url)

    import io
    import sys
    captured_out = io.StringIO()
    saved_stdout = sys.stdout
    sys.stdout = captured_out
    try:
        rc = stop_hook_command(args)
    finally:
        sys.stdout = saved_stdout

    assert rc == 0
    assert captured_out.getvalue() == ""


# ---------------------------------------------------------------------------
# Marker-file auto-discovery
# ---------------------------------------------------------------------------


def test_discover_agent_from_marker_reads_valid_marker(tmp_path):
    """Happy path: a project with a properly-shaped hub-agent.json marker."""
    project = tmp_path / "some-project"
    (project / ".claude").mkdir(parents=True)
    (project / ".claude" / "hub-agent.json").write_text(
        json.dumps({"name": "alice", "project": "some-project"}),
        encoding="utf-8",
    )
    name, proj = _discover_agent_from_marker(str(project))
    assert name == "alice"
    assert proj == "some-project"


def test_discover_agent_from_marker_missing_file_returns_none(tmp_path):
    """Most projects on the dev box aren't hub agents — no marker = no-op,
    not an error."""
    name, proj = _discover_agent_from_marker(str(tmp_path))
    assert name is None
    assert proj is None


def test_discover_agent_from_marker_malformed_json_returns_none(tmp_path):
    """Malformed marker files should fail safe (silent no-op), not crash."""
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "hub-agent.json").write_text("not valid json {{{")
    name, proj = _discover_agent_from_marker(str(tmp_path))
    assert name is None
    assert proj is None


def test_discover_agent_from_marker_missing_fields_returns_none(tmp_path):
    """A marker missing the `name` field is unusable — fail safe."""
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "hub-agent.json").write_text(
        json.dumps({"project": "some-project"}),
        encoding="utf-8",
    )
    name, proj = _discover_agent_from_marker(str(tmp_path))
    assert name is None  # missing
    assert proj == "some-project"


def test_discover_agent_from_marker_no_cwd_returns_none():
    name, proj = _discover_agent_from_marker(None)
    assert name is None
    assert proj is None


# ---------------------------------------------------------------------------
# Identity resolution priority
# ---------------------------------------------------------------------------


def test_resolve_identity_explicit_name_wins(tmp_path, monkeypatch):
    """Explicit --name on the CLI overrides marker discovery — useful for
    tests, manual probing, or any non-standard invocation."""
    # Set up a marker that says alice
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "hub-agent.json").write_text(
        json.dumps({"name": "alice", "project": "marker-project"}),
        encoding="utf-8",
    )

    # But pass --name=bob explicitly
    args = argparse.Namespace(name="bob", project="cli-project", hub_url="x")
    name, project = _resolve_agent_identity(args)

    assert name == "bob"
    assert project == "cli-project"


def test_resolve_identity_falls_back_to_marker(tmp_path, monkeypatch):
    """When --name is omitted, identity resolves from the marker via the
    cwd Claude Code passes via stdin."""
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "hub-agent.json").write_text(
        json.dumps({"name": "alice", "project": "discovered-project"}),
        encoding="utf-8",
    )

    # Simulate Claude Code's hook stdin payload
    import io
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(json.dumps({"cwd": str(tmp_path), "hook_event_name": "Stop"})),
    )

    args = argparse.Namespace(name=None, project=None, hub_url="x")
    name, project = _resolve_agent_identity(args)

    assert name == "alice"
    assert project == "discovered-project"


def test_resolve_identity_no_name_no_marker_returns_none(tmp_path, monkeypatch):
    """No explicit --name + no marker file = silent no-op. The global Stop
    hook fires for every project on the box; only projects opted-in via the
    marker file should produce hook output."""
    import io
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(json.dumps({"cwd": str(tmp_path), "hook_event_name": "Stop"})),
    )

    args = argparse.Namespace(name=None, project=None, hub_url="x")
    name, project = _resolve_agent_identity(args)

    assert name is None
    assert project is None


def test_stop_hook_command_silent_when_no_identity(tmp_path, monkeypatch, capsys):
    """End-to-end: no --name, no marker, hook should exit 0 with no output."""
    import io
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(json.dumps({"cwd": str(tmp_path), "hook_event_name": "Stop"})),
    )

    args = argparse.Namespace(name=None, project=None, hub_url="http://x/mcp")
    rc = stop_hook_command(args)

    assert rc == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
