"""Tests for parallel fan-out in broadcast() and post().

Before this change, broadcast and post iterated recipients with sequential
`await push_channel(...)`. Latency was sum(per-recipient send time). With
the anyio task-group fan-out, latency drops to ≈ max(per-recipient send
time) — the slowest single recipient. These tests assert that property.

The tests patch `registry.push` with an async function that sleeps for a
fixed per-call duration, then time the tool invocation. The assertion is
"elapsed is closer to max than to sum" — generous enough to avoid CI
flakiness while still being a clear regression signal if someone removes
the parallel fan-out.
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

import anyio
import pytest

from mcp_hub.server import create_server


@pytest.fixture
def server(tmp_path: Path):
    db = tmp_path / "test.db"
    return create_server(db_path=db)


async def _call_tool(server, name: str, args: dict) -> str:
    result = await server._tool_manager.call_tool(name, args)
    if hasattr(result, "content"):
        for block in result.content:
            if hasattr(block, "text"):
                return block.text
    if isinstance(result, list):
        for block in result:
            if hasattr(block, "text"):
                return block.text
    if isinstance(result, str):
        return result
    return str(result)


class _FakeSess:
    async def send_ping(self): ...
    async def send_notification(self, _n): ...


async def _register_and_bind(server, names: list[str]) -> None:
    """Register each name as an agent and bind a fake session so it shows
    up as a live recipient in registry.names().

    Each agent gets a unique `project=` value — register() dedups by project
    when one is supplied (reuses the existing online name on that project),
    which would collapse our test recipients into a single agent.
    """
    registry = server._hub_registry  # type: ignore[attr-defined]
    for n in names:
        await _call_tool(
            server, "register", {"name": n, "project": f"proj_{n}"},
        )
        registry.bind(n, _FakeSess())


# Per-recipient delay used by the patched push. 100ms × 5 recipients =
# 500ms serial vs ~100ms parallel. The gap is wide enough that even
# noisy CI shouldn't false-positive against the parallel assertion.
_PER_PUSH_DELAY_S = 0.1


async def _delayed_push(*_args, **_kwargs) -> bool:
    """Stand-in for registry.push that simulates a slow recipient send."""
    await anyio.sleep(_PER_PUSH_DELAY_S)
    return True


async def test_broadcast_fans_out_in_parallel(server):
    """5 recipients × 100ms-per-push must complete in ≈ 100ms, not 500ms.

    Asserts elapsed < sum(delays) / 2 — the parallel path is at most max
    plus scheduling overhead; the serial path would be at least sum. The
    midpoint is a safe regression boundary.
    """
    await _call_tool(server, "register", {"name": "sender", "project": "proj_sender"})
    await _register_and_bind(
        server,
        ["recipient_1", "recipient_2", "recipient_3", "recipient_4", "recipient_5"],
    )

    registry = server._hub_registry  # type: ignore[attr-defined]
    with patch.object(registry, "push", side_effect=_delayed_push):
        t0 = time.monotonic()
        out = await _call_tool(
            server, "broadcast",
            {"from_agent": "sender", "message": "hello fleet", "priority": "normal"},
        )
        elapsed = time.monotonic() - t0

    expected_serial_floor = _PER_PUSH_DELAY_S * 5
    assert elapsed < expected_serial_floor / 2, (
        f"broadcast took {elapsed:.3f}s for 5 × {_PER_PUSH_DELAY_S}s pushes — "
        f"expected parallel fan-out (≈ {_PER_PUSH_DELAY_S}s), got serial-shaped "
        f"timing (>= {expected_serial_floor / 2:.3f}s)"
    )
    assert "woke 5/5" in out


async def test_post_fans_out_in_parallel(server):
    """Same property for post() to a named channel."""
    await _call_tool(server, "register", {"name": "sender", "project": "proj_sender"})
    await _call_tool(
        server, "create_channel",
        {"name": "deploys", "created_by": "sender", "description": ""},
    )
    await _register_and_bind(
        server,
        ["recipient_1", "recipient_2", "recipient_3", "recipient_4", "recipient_5"],
    )

    registry = server._hub_registry  # type: ignore[attr-defined]
    with patch.object(registry, "push", side_effect=_delayed_push):
        t0 = time.monotonic()
        out = await _call_tool(
            server, "post",
            {
                "from_agent": "sender",
                "channel": "deploys",
                "message": "rollout starting",
                "priority": "normal",
            },
        )
        elapsed = time.monotonic() - t0

    expected_serial_floor = _PER_PUSH_DELAY_S * 5
    assert elapsed < expected_serial_floor / 2, (
        f"post took {elapsed:.3f}s for 5 × {_PER_PUSH_DELAY_S}s pushes — "
        f"expected parallel fan-out (≈ {_PER_PUSH_DELAY_S}s), got serial-shaped "
        f"timing (>= {expected_serial_floor / 2:.3f}s)"
    )
    assert "woke 5/5" in out


def _server_db(server):
    """Pull the test server's sqlite connection out of a tool function's
    closure. Mirrors the pattern in test_priority_routing._idle_helper_db
    — the create_server-time db_path is captured by every tool closure,
    so we can recover it from any of them."""
    from mcp_hub.server import _get_db as _gdb

    fn = server._tool_manager._tools["register"].fn
    closure_vars = fn.__closure__
    free_names = fn.__code__.co_freevars
    db_path = None
    for name, cell in zip(free_names, closure_vars):
        if name == "db_path":
            db_path = cell.cell_contents
            break
    assert db_path is not None
    return _gdb(db_path)


async def test_broadcast_advances_cursor_only_for_successful_pushes(server):
    """Mixed success: some recipients accept the push, some return False.
    Only the successes get their last_broadcast_seen_id advanced — failures
    catch up via Stop-hook auto-pull on their next turn boundary."""
    await _call_tool(server, "register", {"name": "sender", "project": "proj_sender"})
    await _register_and_bind(server, ["good_1", "good_2", "bad_1"])

    registry = server._hub_registry  # type: ignore[attr-defined]

    async def _mixed_push(name: str, _n) -> bool:
        # Two recipients succeed, one fails. Tests that the post-fan-out
        # batched UPDATE only touches the successes.
        return name != "bad_1"

    with patch.object(registry, "push", side_effect=_mixed_push):
        out = await _call_tool(
            server, "broadcast",
            {"from_agent": "sender", "message": "x", "priority": "normal"},
        )

    assert "woke 2/3" in out

    conn = _server_db(server)
    rows = {
        r["name"]: r["last_broadcast_seen_id"]
        for r in conn.execute(
            "SELECT name, last_broadcast_seen_id FROM agents"
        ).fetchall()
    }
    # Both successes should have been advanced to the broadcast's id.
    # bad_1's cursor stays at 0 (the default).
    assert rows["good_1"] > 0
    assert rows["good_2"] > 0
    assert rows["bad_1"] == 0
