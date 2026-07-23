"""Disruption-stamp detection via the hub process nonce.

Background: the heartbeat daemon leaves a `hub-reconnect.stamp` breadcrumb that
`squad heal` reads to relaunch agents that lived through a hub restart (their
wake streams die on restart and only a relaunch revives them). The old trigger
inferred "restarted" from a post-reconnect heartbeat returning "no binding" —
which the deliverability reaper can produce WITHOUT any restart (drop the
binding during a long-enough blip). That false-positived and mass-restarted the
whole fleet for a wifi flap (2026-07-20; reproduced end-to-end by
mcp-hub-fireblade 2026-07-23, Arm B).

Fix under test: the hub stamps every heartbeat reply with `hub_boot=<nonce>`
(fresh per process). The daemon stamps ONLY when that nonce CHANGES across a
reconnect — positive restart evidence — and persists the last-seen nonce
per-box so a restart during the daemon's own downtime is still caught.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from mcp_hub import cli
from mcp_hub.server import create_server

# ---------------------------------------------------------------------------
# Server side: every heartbeat reply carries the process nonce
# ---------------------------------------------------------------------------


@pytest.fixture
def server(tmp_path: Path):
    return create_server(db_path=tmp_path / "t.db")


async def _call(server, name: str, args: dict) -> str:
    result = await server._tool_manager.call_tool(name, args)
    if hasattr(result, "content"):
        for block in result.content:
            if hasattr(block, "text"):
                return block.text
    return result if isinstance(result, str) else str(result)


async def test_heartbeat_reply_carries_boot_nonce_even_when_unbound(server):
    """The daemon needs the nonce EXACTLY when the binding is gone (that's the
    ambiguous case), so the marker must ride the 'no binding' reply too."""
    out = await _call(server, "heartbeat", {"agent_name": "ghost"})
    assert "no binding" in out
    m = re.search(r"hub_boot=([0-9a-f]+)", out)
    assert m, f"heartbeat reply missing hub_boot marker: {out!r}"
    assert m.group(1) == server._hub_registry.boot_id  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Daemon side: nonce parse + the stamp decision truth table (isolated state)
# ---------------------------------------------------------------------------


@pytest.fixture
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect all daemon state to an isolated dir via $MCP_HUB_STATE_DIR —
    the override that makes this machinery testable without a HOME hack."""
    d = tmp_path / "state"
    monkeypatch.setenv("MCP_HUB_STATE_DIR", str(d))
    return d


def _stamped(d: Path) -> bool:
    return (d / "hub-reconnect.stamp").exists()


def test_parse_hub_boot():
    assert cli._parse_hub_boot("heartbeat ok (10:00:00) [hub_boot=abc123]") == "abc123"
    assert cli._parse_hub_boot("heartbeat ignored — no binding [hub_boot=deadbeef]") == "deadbeef"
    assert cli._parse_hub_boot("old hub with no marker") is None
    assert cli._parse_hub_boot("") is None


def test_first_sighting_records_baseline_without_stamping(state_dir):
    # Edge case 1: nothing to compare against → never stamp on first connect.
    assert cli._maybe_stamp_hub_restart("ok [hub_boot=11aa22bb]") is False
    assert not _stamped(state_dir)
    assert cli._read_seen_boot() == "11aa22bb"


def test_unchanged_nonce_does_not_stamp(state_dir):
    cli._maybe_stamp_hub_restart("ok [hub_boot=11aa22bb]")  # baseline
    assert cli._maybe_stamp_hub_restart("ok [hub_boot=11aa22bb]") is False
    assert not _stamped(state_dir)


def test_changed_nonce_stamps_and_updates(state_dir):
    cli._maybe_stamp_hub_restart("ok [hub_boot=11aa22bb]")  # baseline
    assert cli._maybe_stamp_hub_restart("ok [hub_boot=33cc44dd]") is True
    assert _stamped(state_dir)
    assert cli._read_seen_boot() == "33cc44dd"


def test_old_hub_without_marker_never_stamps(state_dir):
    # No positive evidence either way → leave state untouched, don't stamp.
    assert cli._maybe_stamp_hub_restart("heartbeat ok (no marker)") is False
    assert not _stamped(state_dir)
    assert cli._read_seen_boot() is None


def test_reaper_drop_during_blip_does_not_stamp(state_dir):
    """THE regression (fireblade Arm B): a blip long enough for the reaper to
    drop the binding makes the post-reconnect beat say 'no binding' — but the
    SAME hub is still running (same nonce), so it must NOT stamp. This is the
    false positive that mass-restarted the fleet; it must stay dead."""
    cli._maybe_stamp_hub_restart("heartbeat ok (10:00:00) [hub_boot=aabbccdd]")  # bound baseline
    reaped = "heartbeat ignored — 'a' has no binding [hub_boot=aabbccdd]"  # reaped, same hub
    assert cli._maybe_stamp_hub_restart(reaped) is False
    assert not _stamped(state_dir)


def test_real_restart_stamps_even_from_no_binding(state_dir):
    """Positive: a genuine restart makes the agent unbound AND changes the
    nonce — must stamp."""
    cli._maybe_stamp_hub_restart("heartbeat ok [hub_boot=aabbccdd]")  # bound baseline
    restarted = "heartbeat ignored — 'a' has no binding [hub_boot=eeff0011]"
    assert cli._maybe_stamp_hub_restart(restarted) is True
    assert _stamped(state_dir)


def test_restart_while_daemon_down_is_caught_on_reconnect(state_dir):
    """Edge case 2: the hub restarts while THIS daemon is down. A fresh daemon
    reads the previous daemon's persisted nonce from disk, sees a different one
    on its first connect, and stamps — no invisible false-negative."""
    cli._write_seen_boot("aabbccdd")  # a prior daemon's persisted last-seen
    assert cli._maybe_stamp_hub_restart("no binding [hub_boot=eeff0011]") is True
    assert _stamped(state_dir)
