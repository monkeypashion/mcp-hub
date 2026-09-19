"""Daemon self-staleness: a long-lived heartbeat daemon must not outlive its
own code.

Background (2026-07-23): the daemon singleton is old-wins, so across session
relaunches the surviving daemon is always the OLDEST process on the box — a
client-side fix (the hub-restart nonce detector) shipped fleet-wide yet not
one running daemon executed it. Fix under test: the loop compares the source
checkout's HEAD against the HEAD it started under once per heartbeat and,
when it moves, exits and respawns a successor from the new tree.

HEAD, not file mtime, on purpose: mtime fires DURING a `git pull` while the
tree is half-old/half-new (respawning then = crash loop mid-deploy); HEAD
moves once, atomically, after checkout completes.
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

import pytest

from mcp_hub import cli


def _git(repo: Path, *argv: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t",
         *argv],
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    d = tmp_path / "checkout"
    d.mkdir()
    _git(d, "init", "-q")
    _git(d, "commit", "-q", "--allow-empty", "-m", "one")
    return d


# ---------------------------------------------------------------------------
# _source_head — the staleness probe
# ---------------------------------------------------------------------------


def test_source_head_reads_checkout_and_tracks_commits(repo: Path):
    first = cli._source_head(repo)
    assert first is not None and len(first) == 40
    assert cli._source_head(repo) == first  # stable while HEAD is still
    _git(repo, "commit", "-q", "--allow-empty", "-m", "two")
    second = cli._source_head(repo)
    assert second is not None and second != first


def test_source_head_none_outside_a_repo(tmp_path: Path):
    """None = guard disabled (plain wheel install, git absent). The daemon
    must fail open — never crash, never churn — when it can't read a HEAD."""
    bare = tmp_path / "not-a-repo"
    bare.mkdir()
    assert cli._source_head(bare) is None


def test_source_head_works_from_a_subdirectory(repo: Path):
    """The probe anchors on this module's directory, which sits levels below
    the repo root (src/mcp_hub/) — `git -C` must resolve upward."""
    sub = repo / "src" / "pkg"
    sub.mkdir(parents=True)
    assert cli._source_head(sub) == cli._source_head(repo)


# ---------------------------------------------------------------------------
# heartbeat_daemon_command — respawn ordering around the singleton
# ---------------------------------------------------------------------------


def _daemon_args() -> argparse.Namespace:
    return argparse.Namespace(name="alice", project=None, hub_url="http://x/mcp")


def test_stale_exit_releases_pidfile_before_spawning_successor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Order is the contract: the singleton is old-wins, so a successor
    spawned while our pidfile still stands would see a 'live owner' and stand
    down — the daemon would just die. Release MUST come first."""
    monkeypatch.setenv("MCP_HUB_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)

    async def stale_loop(hub_url: str, agent_name: str) -> bool:
        return True  # code moved under us

    spawned: list[tuple[str, str, bool]] = []

    def spy_spawn(agent_name: str, hub_url: str) -> None:
        pidfile_free = not cli._heartbeat_pidfile(agent_name).exists()
        spawned.append((agent_name, hub_url, pidfile_free))

    monkeypatch.setattr(cli, "_heartbeat_loop", stale_loop)
    monkeypatch.setattr(cli, "_spawn_daemon_detached", spy_spawn)

    assert cli.heartbeat_daemon_command(_daemon_args()) == 0
    assert spawned == [("alice", "http://x/mcp", True)]


def test_keyboard_interrupt_does_not_respawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Ctrl-C / OS reap is a deliberate stop — a daemon that resurrects
    itself on termination would be unkillable (squad down, squad rm)."""
    monkeypatch.setenv("MCP_HUB_STATE_DIR", str(tmp_path / "state"))

    async def interrupted_loop(hub_url: str, agent_name: str) -> bool:
        raise KeyboardInterrupt

    spawned: list[str] = []
    monkeypatch.setattr(cli, "_heartbeat_loop", interrupted_loop)
    monkeypatch.setattr(
        cli, "_spawn_daemon_detached", lambda *a: spawned.append("no")
    )

    assert cli.heartbeat_daemon_command(_daemon_args()) == 0
    assert spawned == []
    assert not cli._heartbeat_pidfile("alice").exists()  # released on the way out


# --- ownership re-check inside the loop -------------------------------------
# Measured 2026-09-19: pids 2528703 and 219469 were both beating for
# slipstream-testlane-dev-vm-1 (same derived identity, same cwd), the older
# holding no pidfile at all. The claim is made once at startup and the loop
# never re-read it, so a displaced daemon beat on regardless — and a beat
# refreshes a binding, which is the one thing a heartbeat must never do on
# behalf of an agent that is gone.


class _FakeSession:
    """Records tool calls so a test can assert the beat never went out."""

    def __init__(self):
        self.calls: list[str] = []

    async def initialize(self):
        return None

    async def call_tool(self, name, args=None):
        self.calls.append(name)
        raise AssertionError(f"daemon called {name!r} after losing its claim")


def _patch_transport(monkeypatch, session):
    import contextlib

    import mcp
    import mcp.client.streamable_http as sh

    @contextlib.asynccontextmanager
    async def fake_client(*a, **kw):
        yield (None, None, None)

    @contextlib.asynccontextmanager
    async def fake_session(*a, **kw):
        yield session

    monkeypatch.setattr(sh, "streamablehttp_client", fake_client)
    monkeypatch.setattr(mcp, "ClientSession", fake_session)


def test_loop_stands_down_when_another_daemon_owns_the_agent(
    tmp_path, monkeypatch
):
    """A daemon whose pidfile now names a LIVE successor exits without beating.

    Asserted on the tool calls, not just the return value: standing down after
    the beat would still have refreshed the binding, which is the defect."""
    monkeypatch.setenv("MCP_HUB_STATE_DIR", str(tmp_path / "state"))
    cli._heartbeat_pidfile("alice").parent.mkdir(parents=True, exist_ok=True)
    cli._heartbeat_pidfile("alice").write_text("999999", encoding="utf-8")

    session = _FakeSession()
    _patch_transport(monkeypatch, session)
    monkeypatch.setattr(cli, "_is_live_daemon", lambda pid: True)

    import asyncio

    # Bounded on purpose: without the re-check the loop does not fail, it
    # reconnects forever, so an unguarded assert here HANGS instead of
    # reporting. Verified 2026-09-19 — with the check removed this test hits
    # the timeout; with it, it returns in ~1s.
    async def _bounded():
        return await asyncio.wait_for(
            cli._heartbeat_loop("http://x/mcp", "alice"), timeout=10
        )

    respawn = asyncio.run(_bounded())

    assert respawn is False, "standing down must not respawn a successor"
    assert session.calls == [], "beat must not go out once the claim is lost"
    assert cli._heartbeat_pidfile("alice").read_text(encoding="utf-8") == "999999"
