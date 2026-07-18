"""Tests for the cli stop-hook subcommand.

Covers the pure decision logic (build_hook_response), the text-extraction
helper, the fail-open contract on hub errors, and end-to-end via the SDK's
in-memory transport so we exercise the real MCP call path.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from mcp_hub.cli import (
    _claim_singleton,
    _daemon_alive_for,
    _derive_agent_identity,
    _discover_agent_from_marker,
    _ensure_daemon_alive,
    _extract_text,
    _heartbeat_pidfile,
    _is_live_daemon,
    _parse_org_repo,
    _parse_status_from_agents,
    _release_singleton,
    _resolve_agent_identity,
    _sanitize_ident,
    _spawn_daemon_detached,
    _status_cache_path,
    _write_status_cache,
    build_hook_response,
    build_parser,
    session_rewake_command,
    session_start_command,
    stop_hook_command,
)


@pytest.fixture(autouse=True)
def _no_real_daemons(tmp_path, monkeypatch):
    """Hermetic guard: tests must NEVER spawn real detached daemons or touch
    the real ~/.mcp-hub.

    Without this, any test that drives stop_hook_command trips the daemon
    self-heal and launches a real `python -m mcp_hub.cli heartbeat-daemon`
    process (observed 2026-07-18: orphaned 'alice' and 'ghost-agent' daemons
    from the integration tests, one retrying http://nowhere.invalid/mcp
    forever, plus their pidfiles in the real home dir).

    Tests that exercise the spawn path re-patch these seams explicitly;
    test_spawn_daemon_detached_* calls the original function via its direct
    import, so it is unaffected by the module-attribute no-op here.
    """
    monkeypatch.setattr("mcp_hub.cli._PIDFILE_DIR", tmp_path / "mcp-hub-state")
    monkeypatch.setattr(
        "mcp_hub.cli._spawn_daemon_detached", lambda *_a, **_k: None
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
        is_online=True,
    ) is None


def test_no_messages_unbound_emits_rebind_only_block():
    """Drifted with empty inbox: emit a rebind-only block so the agent
    self-heals on the next Stop after a hub redeploy. Without this, drifted
    agents stay drifted indefinitely until someone DMs them — defeats
    the wake feature for any agent that isn't actively in conversation."""
    response = build_hook_response(
        agent_name="alice",
        project="proj",
        messages_text="",
        is_online=False,
    )
    assert response is not None
    assert response["decision"] == "block"
    reason = response["reason"]
    # Rebind hint with the explicit register() call
    assert 'register(name="alice", project="proj")' in reason
    # No "queued items" header since there's nothing to process
    assert "queued items below" not in reason
    # No discipline reminder since there's nothing to context-switch on
    assert "Discipline reminder" not in reason


def test_no_messages_unbound_no_project_emits_rebind_only_block():
    """Same as above but with project=None — the rebind call should
    omit the `project=` argument cleanly."""
    response = build_hook_response(
        agent_name="alice",
        project=None,
        messages_text="",
        is_online=False,
    )
    assert response is not None
    assert 'register(name="alice")' in response["reason"]
    assert "project=" not in response["reason"]


def test_messages_bound_emits_block_with_content():
    response = build_hook_response(
        agent_name="alice",
        project="proj",
        messages_text="[10:00] **bob**: hello there",
        is_online=True,
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
        is_online=False,
    )
    assert response is not None
    reason = response["reason"]
    assert "ping" in reason
    # Rebind hint must include the agent's exact name + project for copy-paste
    assert 'register(name="alice", project="my-proj")' in reason
    assert "isn't showing as online" in reason


def test_messages_unbound_no_project_still_emits_rebind():
    """project is optional — rebind hint should still appear with just
    name="..." form."""
    response = build_hook_response(
        agent_name="alice",
        project=None,
        messages_text="[10:00] **bob**: hi",
        is_online=False,
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
        is_online=True,
    )
    assert msg_body in response["reason"]


# ---------------------------------------------------------------------------
# Broadcast surfacing
# ---------------------------------------------------------------------------


def test_broadcasts_only_emits_block():
    """No DMs but unseen broadcasts → emit block with broadcasts. This is
    the load-bearing case for drifted agents catching up on the broadcast
    feed: their DM inbox might be empty, but if anyone broadcast while
    they were drifted, those should surface."""
    response = build_hook_response(
        agent_name="alice",
        project="proj",
        messages_text="",
        broadcasts_text="[10:00] **dt**: hub redeploying in 5 min",
        is_online=True,
    )
    assert response is not None
    assert response["decision"] == "block"
    assert "hub redeploying" in response["reason"]
    assert "Broadcasts" in response["reason"]
    # No DM section if there are no DMs
    assert "Direct messages:" not in response["reason"]


def test_dms_and_broadcasts_both_emit_block_with_both_sections():
    """When both are present, they should be rendered in distinct sections
    so the agent can tell them apart for relevance gating."""
    response = build_hook_response(
        agent_name="alice",
        project="proj",
        messages_text="[10:00] **bob**: ping",
        broadcasts_text="[10:01] **dt**: status update",
        is_online=True,
    )
    assert response is not None
    reason = response["reason"]
    assert "Direct messages:" in reason
    assert "ping" in reason
    assert "Broadcasts" in reason
    assert "status update" in reason
    # DMs come before broadcasts (more directed signal first)
    assert reason.index("Direct messages:") < reason.index("Broadcasts")


def test_broadcasts_only_drifted_emits_block_with_rebind():
    """Drifted + broadcasts but no DMs → block with broadcasts + rebind hint.
    The same surfacing path that fixes the 'broadcasts silently bypass
    drifted agents' issue."""
    response = build_hook_response(
        agent_name="alice",
        project="proj",
        messages_text="",
        broadcasts_text="[10:00] **dt**: announcement",
        is_online=False,
    )
    assert response is not None
    reason = response["reason"]
    assert "announcement" in reason
    assert 'register(name="alice", project="proj")' in reason


def test_no_dms_no_broadcasts_bound_returns_none():
    """The steady-state happy path: agent is up to date and online. Most
    Stop fires hit this — no block, no overhead."""
    assert build_hook_response(
        agent_name="alice",
        project="proj",
        messages_text="",
        broadcasts_text="",
        is_online=True,
    ) is None


def test_online_idle_without_wake_marker_is_not_nagged():
    """Regression for the fleet-wide false-rebind loop.

    After PR #3, an idle agent is 🟢 (online) but lacks ⚡ (no open GET /mcp
    stream between turns). The Stop hook fires exactly at that idle moment.
    The rebind nag MUST key on online status, not ⚡ — so an online agent
    with an empty inbox gets NO block, even though it isn't ⚡-wakeable this
    instant. Keying on ⚡ poked every idle agent to re-register every turn."""
    assert build_hook_response(
        agent_name="alice",
        project="proj",
        messages_text="",
        broadcasts_text="",
        is_online=True,
    ) is None


def test_stop_hook_active_suppresses_content_less_reblock():
    """Loop backstop: when a Stop fires because a prior block fired
    (stop_hook_active) and there's nothing new to surface, do not re-block —
    even for a genuinely offline agent. Otherwise a content-less rebind nag
    re-emits every turn and wedges the agent."""
    assert build_hook_response(
        agent_name="alice",
        project="proj",
        messages_text="",
        broadcasts_text="",
        is_online=False,
        stop_hook_active=True,
    ) is None


def test_stop_hook_active_still_surfaces_new_content():
    """stop_hook_active only suppresses content-LESS blocks. A genuinely
    queued DM must still surface even on a re-fire."""
    response = build_hook_response(
        agent_name="alice",
        project="proj",
        messages_text="[10:00] **bob**: urgent ping",
        broadcasts_text="",
        is_online=True,
        stop_hook_active=True,
    )
    assert response is not None
    assert "urgent ping" in response["reason"]


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
        return ("", "", True)  # no DMs, no broadcasts, bound

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
        return ("[10:00] **bob**: hello", "", True)  # DM, no broadcasts, bound

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
    import urllib.error
    import urllib.request
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
# Derived identity — org/repo from git remote + hostname, opt-in gated
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        # scp-like with an SSH alias host — the host must be ignored
        ("git@github-monkeypashion:monkeypashion/mcp-hub.git", ("monkeypashion", "mcp-hub")),
        ("git@github.com:org/repo.git", ("org", "repo")),
        ("https://github.com/org/repo.git", ("org", "repo")),
        ("https://github.com/org/repo", ("org", "repo")),
        ("ssh://git@github.com:22/org/repo.git", ("org", "repo")),
        # nested groups: last two segments win
        ("https://gitlab.com/group/subgroup/repo.git", ("subgroup", "repo")),
        ("https://github.com/org/repo/", ("org", "repo")),
        ("not-a-url", None),
        ("", None),
        ("git@host:only-repo.git", None),
    ],
)
def test_parse_org_repo(url, expected):
    """Every remote URL form for the same repo must parse identically —
    that's the structural same-project guarantee across clones."""
    assert _parse_org_repo(url) == expected


def test_sanitize_ident_lowercases_and_dashes():
    """Windows hostnames like DESKTOP-XYZ.local must become clean idents.
    Rule mirrored in statusline-command.js — change both or neither."""
    assert _sanitize_ident("DESKTOP-XYZ.local") == "desktop-xyz-local"
    assert _sanitize_ident("dev-vm-1") == "dev-vm-1"
    assert _sanitize_ident("...") == ""


def _derivation_env(monkeypatch, *, url, projects, host="myhost"):
    """Patch the derivation inputs: remote URL, opt-in config, hostname."""
    monkeypatch.setattr("mcp_hub.cli._git_remote_url", lambda _cwd: url)
    monkeypatch.setattr(
        "mcp_hub.cli._load_hub_config", lambda: {"projects": projects}
    )
    monkeypatch.setattr("mcp_hub.cli.platform.node", lambda: host)


def test_derive_identity_opted_in(monkeypatch):
    """Opted-in repo derives name=<repo>-<hostname>, project=<org>/<repo>."""
    _derivation_env(
        monkeypatch,
        url="git@github-alias:acme/widgets.git",
        projects=["acme/widgets"],
        host="Win-Box",
    )
    name, project = _derive_agent_identity("/anywhere")
    assert name == "widgets-win-box"
    assert project == "acme/widgets"


def test_derive_identity_not_opted_in_returns_none(monkeypatch):
    """A repo missing from the opt-in list must not participate — the global
    hooks fire in every git repo on the box."""
    _derivation_env(
        monkeypatch, url="git@github.com:acme/widgets.git", projects=["other/repo"]
    )
    assert _derive_agent_identity("/anywhere") == (None, None)


def test_derive_identity_no_remote_returns_none(monkeypatch):
    """Not a git repo / no origin remote → silent no-op."""
    monkeypatch.setattr("mcp_hub.cli._git_remote_url", lambda _cwd: None)
    assert _derive_agent_identity("/anywhere") == (None, None)


def test_derive_identity_no_config_returns_none(monkeypatch):
    """No ~/.mcp-hub/config.json at all → nothing participates."""
    monkeypatch.setattr(
        "mcp_hub.cli._git_remote_url", lambda _cwd: "git@github.com:a/b.git"
    )
    monkeypatch.setattr("mcp_hub.cli._load_hub_config", lambda: {})
    assert _derive_agent_identity("/anywhere") == (None, None)


def test_resolve_identity_derived_beats_marker(tmp_path, monkeypatch):
    """A repo still carrying a committed marker must not drag a migrated
    machine back into the shared identity — derived wins."""
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "hub-agent.json").write_text(
        json.dumps({"name": "shared-marker-name", "project": "marker-proj"}),
        encoding="utf-8",
    )
    _derivation_env(
        monkeypatch,
        url="git@github.com:acme/widgets.git",
        projects=["acme/widgets"],
        host="hostA",
    )
    args = argparse.Namespace(name=None, project=None, hub_url="x")
    name, project = _resolve_agent_identity(args, {"cwd": str(tmp_path)})
    assert name == "widgets-hosta"
    assert project == "acme/widgets"


def test_onboard_adds_project_and_prints_identity(tmp_path, monkeypatch, capsys):
    """`mcp-hub onboard` appends org/repo to the machine config and prints
    the derived identity — the one-command Windows onboarding path."""
    from mcp_hub.cli import onboard_command

    cfg = tmp_path / "config.json"
    monkeypatch.setattr("mcp_hub.cli._HUB_CONFIG_PATH", cfg)
    monkeypatch.setattr(
        "mcp_hub.cli._git_remote_url", lambda _cwd: "git@github.com:acme/widgets.git"
    )
    monkeypatch.setattr("mcp_hub.cli.platform.node", lambda: "Win-Box")

    args = argparse.Namespace(path=str(tmp_path))
    assert onboard_command(args) == 0
    assert json.loads(cfg.read_text())["projects"] == ["acme/widgets"]
    out = capsys.readouterr().out
    assert "opted in: acme/widgets" in out
    assert "name=widgets-win-box" in out

    # Idempotent: second run changes nothing, reports already opted in.
    assert onboard_command(args) == 0
    assert json.loads(cfg.read_text())["projects"] == ["acme/widgets"]
    assert "already opted in" in capsys.readouterr().out


def test_onboard_fails_cleanly_outside_git(tmp_path, monkeypatch, capsys):
    """Not a git repo → error message + exit 1, config untouched."""
    from mcp_hub.cli import onboard_command

    cfg = tmp_path / "config.json"
    monkeypatch.setattr("mcp_hub.cli._HUB_CONFIG_PATH", cfg)
    monkeypatch.setattr("mcp_hub.cli._git_remote_url", lambda _cwd: None)

    assert onboard_command(argparse.Namespace(path=str(tmp_path))) == 1
    assert not cfg.exists()
    assert "not a git repo" in capsys.readouterr().err


def test_resolve_identity_marker_fallback_when_not_opted_in(tmp_path, monkeypatch):
    """Legacy agents (marker present, machine not opted in) keep working."""
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "hub-agent.json").write_text(
        json.dumps({"name": "legacy-agent", "project": "legacy-proj"}),
        encoding="utf-8",
    )
    _derivation_env(
        monkeypatch, url="git@github.com:acme/widgets.git", projects=[]
    )
    args = argparse.Namespace(name=None, project=None, hub_url="x")
    name, project = _resolve_agent_identity(args, {"cwd": str(tmp_path)})
    assert name == "legacy-agent"
    assert project == "legacy-proj"


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


# ---------------------------------------------------------------------------
# session-start subcommand — auto-register context injection
# ---------------------------------------------------------------------------


def test_session_start_emits_register_context_for_marked_project(tmp_path, monkeypatch, capsys):
    """When a project has a hub-agent.json marker, the session-start hook
    must emit JSON with `additionalContext` instructing the agent to call
    register() with the marker's name + project. This is what makes a fresh
    session ⚡ from the first turn without operator nudging."""
    import io

    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    (claude_dir / "hub-agent.json").write_text(
        json.dumps({"name": "features-json-dev", "project": "features-json"})
    )
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(json.dumps({"cwd": str(tmp_path), "hook_event_name": "SessionStart"})),
    )

    args = argparse.Namespace(name=None, project=None)
    rc = session_start_command(args)

    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    # Must use the SessionStart-specific schema so Claude Code routes it correctly.
    assert payload["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    context = payload["hookSpecificOutput"]["additionalContext"]
    # Must instruct the agent to call register with the marker's identity.
    assert 'register(name="features-json-dev", project="features-json")' in context


def test_session_start_context_is_resume_race_resilient(tmp_path, monkeypatch, capsys):
    """The register instruction must tell the agent NOT to give up if the hub
    MCP server is still connecting on resume — it should wait/retry rather than
    conclude the hub is down. This is the gap that left dreamteam-lead
    connected-but-unregistered (MCP up, never registered)."""
    import io
    monkeypatch.setattr(
        "sys.stdin", io.StringIO(json.dumps({"cwd": str(tmp_path)}))
    )
    args = argparse.Namespace(name="alice", project="proj")
    rc = session_start_command(args)
    assert rc == 0
    context = json.loads(capsys.readouterr().out)[
        "hookSpecificOutput"
    ]["additionalContext"]
    low = context.lower()
    assert "wait" in low  # tells the agent to wait for the server
    assert "retry" in low  # ...and retry rather than give up
    assert 'register(name="alice", project="proj")' in context


def test_session_start_silent_when_no_marker(tmp_path, monkeypatch, capsys):
    """A project without a hub-agent.json marker isn't a hub agent. The
    SessionStart hook must emit nothing — same fail-open contract as the
    Stop hook. Otherwise every Claude Code session on the box gets an
    "unrecognised agent" register prompt injected on startup."""
    import io
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(json.dumps({"cwd": str(tmp_path), "hook_event_name": "SessionStart"})),
    )

    args = argparse.Namespace(name=None, project=None)
    rc = session_start_command(args)

    assert rc == 0
    assert capsys.readouterr().out == ""


def test_session_start_explicit_name_wins(tmp_path, monkeypatch, capsys):
    """Explicit --name overrides marker discovery. Useful for one-off
    testing or non-standard hook configurations."""
    import io
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(json.dumps({"cwd": str(tmp_path), "hook_event_name": "SessionStart"})),
    )

    args = argparse.Namespace(name="alice", project="proj")
    rc = session_start_command(args)

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert 'register(name="alice", project="proj")' in (
        payload["hookSpecificOutput"]["additionalContext"]
    )


def test_session_start_handles_marker_without_project(tmp_path, monkeypatch, capsys):
    """Marker file with name but no project — the register call must omit
    the project= kwarg cleanly rather than emit invalid syntax."""
    import io

    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    (claude_dir / "hub-agent.json").write_text(json.dumps({"name": "alice"}))
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(json.dumps({"cwd": str(tmp_path), "hook_event_name": "SessionStart"})),
    )

    args = argparse.Namespace(name=None, project=None)
    rc = session_start_command(args)

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    context = payload["hookSpecificOutput"]["additionalContext"]
    # No spurious project="" or project=None
    assert 'register(name="alice")' in context
    assert "project=" not in context


def test_session_rewake_emits_stderr_and_exits_2_for_marked_project(tmp_path, monkeypatch, capsys):
    """asyncRewake variant: when marker present, write register instruction
    to stderr and exit 2 so Claude Code's asyncRewake mechanism may fire
    an unprompted first turn. Empirically untested — docs are ambiguous on
    whether this triggers from cold session start. Worst case: no-op,
    additionalContext path still works."""
    import io

    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    (claude_dir / "hub-agent.json").write_text(
        json.dumps({"name": "features-json-dev", "project": "features-json"})
    )
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(json.dumps({"cwd": str(tmp_path), "hook_event_name": "SessionStart"})),
    )

    args = argparse.Namespace(name=None, project=None)
    rc = session_rewake_command(args)

    assert rc == 2  # asyncRewake trigger
    captured = capsys.readouterr()
    # Stderr carries the instruction shown to Claude as a system reminder
    assert 'register(name="features-json-dev", project="features-json")' in captured.err
    # Stdout is empty — asyncRewake reads stderr (or stdout if stderr empty)
    assert captured.out == ""


def test_session_rewake_silent_when_no_marker(tmp_path, monkeypatch, capsys):
    """No marker → exit 0, no wake. Same fail-open contract as the rest
    of the cli — non-hub projects on the box don't get spurious wake
    events."""
    import io
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(json.dumps({"cwd": str(tmp_path), "hook_event_name": "SessionStart"})),
    )

    args = argparse.Namespace(name=None, project=None)
    rc = session_rewake_command(args)

    assert rc == 0  # NOT 2 — no wake when no marker
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == ""


# ---------------------------------------------------------------------------
# Heartbeat-daemon singleton — stops the Windows daemon leak
# ---------------------------------------------------------------------------


def test_heartbeat_pidfile_sanitizes_agent_name(tmp_path, monkeypatch):
    """Pidfile lives under the stable per-user dir and the agent name is
    sanitized so a name with path-hostile chars can't escape the dir or
    collide."""
    monkeypatch.setattr("mcp_hub.cli._PIDFILE_DIR", tmp_path)
    pf = _heartbeat_pidfile("dream/team:lead")
    assert pf.parent == tmp_path
    assert pf.name == "heartbeat-dream_team_lead.pid"


def test_claim_singleton_wins_when_no_prior(tmp_path, monkeypatch):
    """First daemon for an agent: no prior pidfile → win the claim and record
    our PID."""
    monkeypatch.setattr("mcp_hub.cli._PIDFILE_DIR", tmp_path)
    pf = _claim_singleton("alice", getpid=lambda: 4242)
    assert pf is not None
    assert pf.read_text(encoding="utf-8") == "4242"


def test_claim_singleton_creates_state_dir_if_missing(tmp_path, monkeypatch):
    """The per-user state dir is created on first claim if absent."""
    state = tmp_path / "nested" / ".mcp-hub"
    monkeypatch.setattr("mcp_hub.cli._PIDFILE_DIR", state)
    assert not state.exists()
    pf = _claim_singleton("alice", getpid=lambda: 4242)
    assert pf is not None and pf.exists()


def test_claim_singleton_stands_down_for_live_owner(tmp_path, monkeypatch):
    """A live daemon already owns the agent → newcomer returns None (stand
    down) and the incumbent's pidfile is left untouched. This is the core
    leak fix: extra daemons exit instead of looping forever."""
    monkeypatch.setattr("mcp_hub.cli._PIDFILE_DIR", tmp_path)
    pf_path = _heartbeat_pidfile("alice")
    pf_path.write_text("1111", encoding="utf-8")

    with patch("mcp_hub.cli._is_live_daemon", return_value=True) as live:
        result = _claim_singleton("alice", getpid=lambda: 2222)

    assert result is None
    live.assert_called_once_with(1111)
    assert pf_path.read_text(encoding="utf-8") == "1111"  # incumbent untouched


def test_claim_singleton_takes_over_dead_owner(tmp_path, monkeypatch):
    """A stale pidfile (owner dead / PID recycled to a stranger) is removed
    and the newcomer claims it."""
    monkeypatch.setattr("mcp_hub.cli._PIDFILE_DIR", tmp_path)
    pf_path = _heartbeat_pidfile("alice")
    pf_path.write_text("1111", encoding="utf-8")

    with patch("mcp_hub.cli._is_live_daemon", return_value=False):
        result = _claim_singleton("alice", getpid=lambda: 2222)

    assert result is not None
    assert pf_path.read_text(encoding="utf-8") == "2222"


def test_claim_singleton_takes_over_garbage_pidfile(tmp_path, monkeypatch):
    """A corrupt/non-integer pidfile must not block startup — treat as stale
    and claim it."""
    monkeypatch.setattr("mcp_hub.cli._PIDFILE_DIR", tmp_path)
    pf_path = _heartbeat_pidfile("alice")
    pf_path.write_text("not-a-pid", encoding="utf-8")

    result = _claim_singleton("alice", getpid=lambda: 2222)
    assert result is not None
    assert pf_path.read_text(encoding="utf-8") == "2222"


def test_claim_singleton_is_race_safe_second_caller_stands_down(tmp_path, monkeypatch):
    """Two real claims for the same agent: the first wins (atomic O_EXCL), the
    second sees a live owner and stands down. Exercises the actual filesystem
    create path, not just mocks."""
    monkeypatch.setattr("mcp_hub.cli._PIDFILE_DIR", tmp_path)
    first = _claim_singleton("alice", getpid=lambda: 1111)
    assert first is not None
    # Second caller: incumbent (1111) reported live → must stand down.
    with patch("mcp_hub.cli._is_live_daemon", return_value=True):
        second = _claim_singleton("alice", getpid=lambda: 2222)
    assert second is None
    assert first.read_text(encoding="utf-8") == "1111"


def test_release_singleton_removes_pidfile_when_owner(tmp_path, monkeypatch):
    """Clean exit by the current owner removes the pidfile."""
    monkeypatch.setattr("mcp_hub.cli._PIDFILE_DIR", tmp_path)
    pf_path = _heartbeat_pidfile("alice")
    pf_path.write_text("2222", encoding="utf-8")

    _release_singleton(pf_path, getpid=lambda: 2222)
    assert not pf_path.exists()


def test_release_singleton_keeps_successor_claim(tmp_path, monkeypatch):
    """If a successor daemon already took over (pidfile names someone else),
    our exit must NOT delete their claim."""
    monkeypatch.setattr("mcp_hub.cli._PIDFILE_DIR", tmp_path)
    pf_path = _heartbeat_pidfile("alice")
    pf_path.write_text("3333", encoding="utf-8")  # successor's PID

    _release_singleton(pf_path, getpid=lambda: 2222)
    assert pf_path.exists()
    assert pf_path.read_text(encoding="utf-8") == "3333"


def test_release_singleton_missing_pidfile_is_noop(tmp_path, monkeypatch):
    """No pidfile (already cleaned) → no error."""
    monkeypatch.setattr("mcp_hub.cli._PIDFILE_DIR", tmp_path)
    pf_path = _heartbeat_pidfile("alice")
    _release_singleton(pf_path, getpid=lambda: 2222)  # must not raise


def test_is_live_daemon_rejects_nonpositive_pid():
    """PID 0 / negative are never valid daemons."""
    assert _is_live_daemon(0) is False
    assert _is_live_daemon(-1) is False


# ---------------------------------------------------------------------------
# Daemon self-heal — the Stop hook revives a dead/absent keep-alive daemon
# ---------------------------------------------------------------------------


def test_daemon_alive_for_no_pidfile_is_false(tmp_path, monkeypatch):
    """No pidfile → no daemon → self-heal should fire."""
    monkeypatch.setattr("mcp_hub.cli._PIDFILE_DIR", tmp_path)
    assert _daemon_alive_for("alice") is False


def test_daemon_alive_for_live_owner_is_true(tmp_path, monkeypatch):
    """Pidfile names a live daemon → alive."""
    monkeypatch.setattr("mcp_hub.cli._PIDFILE_DIR", tmp_path)
    _heartbeat_pidfile("alice").write_text("1234", encoding="utf-8")
    with patch("mcp_hub.cli._is_live_daemon", return_value=True):
        assert _daemon_alive_for("alice") is True


def test_daemon_alive_for_dead_owner_is_false(tmp_path, monkeypatch):
    """Pidfile names a dead PID → not alive → self-heal should fire."""
    monkeypatch.setattr("mcp_hub.cli._PIDFILE_DIR", tmp_path)
    _heartbeat_pidfile("alice").write_text("1234", encoding="utf-8")
    with patch("mcp_hub.cli._is_live_daemon", return_value=False):
        assert _daemon_alive_for("alice") is False


def test_ensure_daemon_alive_spawns_when_absent():
    """No live daemon → spawn exactly one detached daemon."""
    with patch("mcp_hub.cli._daemon_alive_for", return_value=False), \
         patch("mcp_hub.cli._spawn_daemon_detached") as spawn:
        _ensure_daemon_alive("alice", "http://hub/mcp")
    spawn.assert_called_once_with("alice", "http://hub/mcp")


def test_ensure_daemon_alive_noop_when_already_running():
    """A daemon is already alive → do not spawn a second one."""
    with patch("mcp_hub.cli._daemon_alive_for", return_value=True), \
         patch("mcp_hub.cli._spawn_daemon_detached") as spawn:
        _ensure_daemon_alive("alice", "http://hub/mcp")
    spawn.assert_not_called()


def test_ensure_daemon_alive_is_fail_open(capsys):
    """A spawn error must never propagate into the Stop hook."""
    with patch("mcp_hub.cli._daemon_alive_for", return_value=False), \
         patch("mcp_hub.cli._spawn_daemon_detached", side_effect=OSError("boom")):
        _ensure_daemon_alive("alice", "http://hub/mcp")  # must not raise
    assert "self-heal failed" in capsys.readouterr().err


def test_spawn_daemon_detached_builds_module_invocation():
    """Spawn via `python -m mcp_hub.cli heartbeat-daemon` with the agent name,
    detached, and no inherited stdio (so it outlives the hook)."""
    with patch("mcp_hub.cli.subprocess.Popen") as popen:
        _spawn_daemon_detached("alice", "http://hub/mcp")
    cmd = popen.call_args.args[0]
    assert cmd[:3] == [sys.executable, "-m", "mcp_hub.cli"]
    assert "heartbeat-daemon" in cmd
    assert cmd[cmd.index("--name") + 1] == "alice"
    assert cmd[cmd.index("--hub-url") + 1] == "http://hub/mcp"
    kwargs = popen.call_args.kwargs
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stdout"] is subprocess.DEVNULL
    # Detached: new session on POSIX, DETACHED_PROCESS flags on Windows.
    assert kwargs.get("start_new_session") or kwargs.get("creationflags")


# ---------------------------------------------------------------------------
# _parse_status_from_agents — statusline wakeability snapshot
# ---------------------------------------------------------------------------

# A realistic list_agents() render: 🟢 = online, ⚡ = wakeable, 💤 = idle.
_AGENTS_SAMPLE = (
    "🟢 **dreamteam-lead** ⚡ (dreamteam) — DreamTeam factory lead.\n"
    "🟢 **factory-operations-dev** 💤 (factory-operations) — Owns the gateway.\n"
    "🟢 **vps-admin** ⚡ 💤 (vps-hetzner) — VPS/infra-ops lane.\n"
    "🟢 **mcp-hub-dev** ⚡ 💤 (mcp-hub) — Hub maintainer.\n"
)


def test_parse_status_self_wakeable_and_fleet_counts():
    s = _parse_status_from_agents(_AGENTS_SAMPLE, "mcp-hub-dev")
    assert s["online"] is True
    assert s["wakeable"] is True
    assert s["fleet_total"] == 4
    assert s["fleet_wakeable"] == 3  # dreamteam-lead, vps-admin, mcp-hub-dev


def test_parse_status_self_online_but_not_wakeable():
    """Bound (🟢) but no ⚡ — the exact failure we surface in the statusline."""
    s = _parse_status_from_agents(_AGENTS_SAMPLE, "factory-operations-dev")
    assert s["online"] is True
    assert s["wakeable"] is False


def test_parse_status_self_absent_is_offline():
    s = _parse_status_from_agents(_AGENTS_SAMPLE, "nobody")
    assert s["online"] is False
    assert s["wakeable"] is False
    # Fleet totals are still computed for everyone else.
    assert s["fleet_total"] == 4
    assert s["fleet_wakeable"] == 3


def test_parse_status_bio_markers_do_not_skew_counts():
    """A bio that mentions ⚡ or 🟢 (after the ` — ` separator) must not be
    miscounted as wakeability/an extra agent."""
    text = (
        "🟢 **alice** ⚡ (proj) — explains the ⚡ wake marker and 🟢 presence\n"
        "🟢 **bob** 💤 (proj) — idle, no markers here\n"
    )
    s = _parse_status_from_agents(text, "alice")
    assert s["fleet_total"] == 2
    assert s["fleet_wakeable"] == 1  # only alice's real ⚡, not bob's bio
    assert s["wakeable"] is True


def test_parse_status_empty_text():
    s = _parse_status_from_agents("", "alice")
    assert s == {
        "online": False,
        "wakeable": False,
        "fleet_wakeable": 0,
        "fleet_total": 0,
    }


# ---------------------------------------------------------------------------
# _write_status_cache — atomic snapshot write (fail-soft)
# ---------------------------------------------------------------------------


def test_write_status_cache_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr("mcp_hub.cli._PIDFILE_DIR", tmp_path)
    _write_status_cache("mcp-hub-dev", _AGENTS_SAMPLE)

    path = _status_cache_path("mcp-hub-dev")
    assert path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["agent"] == "mcp-hub-dev"
    assert data["online"] is True
    assert data["wakeable"] is True
    assert data["fleet_wakeable"] == 3
    assert data["fleet_total"] == 4
    assert isinstance(data["ts"], int) and data["ts"] > 0
    # No leftover temp file from the atomic write.
    assert not list(tmp_path.glob("*.tmp"))


def test_write_status_cache_sanitizes_filename(tmp_path, monkeypatch):
    monkeypatch.setattr("mcp_hub.cli._PIDFILE_DIR", tmp_path)
    _write_status_cache("weird/name:1", _AGENTS_SAMPLE)
    # Path is sanitized; the write still lands somewhere readable.
    assert _status_cache_path("weird/name:1").exists()


def test_write_status_cache_is_fail_soft(tmp_path, monkeypatch):
    """A write error must be swallowed — the heartbeat loop must never die
    because the cosmetic status cache couldn't be written."""
    monkeypatch.setattr("mcp_hub.cli._PIDFILE_DIR", tmp_path)

    def _boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr("mcp_hub.cli._parse_status_from_agents", _boom)
    # Must not raise.
    _write_status_cache("mcp-hub-dev", _AGENTS_SAMPLE)
