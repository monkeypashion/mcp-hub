"""Two tests covering the silent-drop bug + the hub-side fix:

1. `test_server_notification_silently_dropped_when_no_get_stream`
   Repro of the SDK behavior we're guarding against: server-initiated
   notifications are silently dropped when the client has no GET /mcp
   listener open. This proves the production bug from 2026-05-11 (broadcast
   reported `woke 4/4` but only 1 of 4 agents actually surfaced it).

2. `test_push_channel_gates_silent_drop`
   Verifies the hub's `push_channel` returns False (no fake delivery) when
   the bound session's transport has no GET stream — exercising the
   `_can_deliver_push` gate added to stop the hub from lying in its `woke`
   counts.

Mechanism that the bug + fix hinge on:

- `ServerSession.send_notification` writes to the session's internal
  write_stream — always succeeds, no exception, regardless of client state.
- The streamable-http transport's message_router routes server-initiated
  notifications to `GET_STREAM_KEY` (the standalone SSE stream the client
  opens via GET /mcp).
- If the client has no GET stream in `_request_streams`, the router logs
  a DEBUG line and drops the message. We run with `event_store=None`, so
  no replay either.

The hub-side fix: before calling send_notification, check whether the
bound session's transport has GET_STREAM_KEY in `_request_streams`. If not,
push returns False — caller falls back to Stop-hook auto-pull.
"""

from __future__ import annotations

import logging
from pathlib import Path

import anyio
import pytest
from mcp.server.streamable_http import (
    GET_STREAM_KEY,
    StreamableHTTPServerTransport,
)
from mcp.shared.message import SessionMessage
from mcp.types import (
    JSONRPCMessage,
    JSONRPCNotification,
)

from mcp_hub.server import create_server


async def test_server_notification_silently_dropped_when_no_get_stream(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Notifications pushed when no GET stream is registered are dropped
    without raising. This is the silent-success failure mode."""
    caplog.set_level(logging.DEBUG, logger="mcp.server.streamable_http")

    transport = StreamableHTTPServerTransport(
        mcp_session_id="test-session-id",
        is_json_response_enabled=False,
        event_store=None,  # No replay — matches our hub config.
    )

    notification = JSONRPCNotification(
        jsonrpc="2.0",
        method="claude/channel",
        params={"content": "hello-from-test", "meta": {}},
    )
    message = SessionMessage(message=JSONRPCMessage(notification))

    drop_log_seen = False
    write_error: BaseException | None = None

    async with transport.connect() as (read_stream, write_stream):
        # Sanity: the transport's internal message_router is now running.
        # `_request_streams` should be empty — no client GET stream open.
        assert GET_STREAM_KEY not in transport._request_streams

        # Write the notification exactly the way ServerSession.send_notification
        # does — straight into the write stream. This must not raise.
        try:
            await write_stream.send(message)
        except BaseException as exc:  # noqa: BLE001
            write_error = exc

        # Give the message_router a tick to process. The router runs in the
        # same task group as `connect()`.
        await anyio.sleep(0.05)

        # Inspect captured logs — the drop path emits a DEBUG line containing
        # "Request stream _GET_stream not found".
        for record in caplog.records:
            if (
                "Request stream" in record.getMessage()
                and GET_STREAM_KEY in record.getMessage()
                and "not found" in record.getMessage()
            ):
                drop_log_seen = True
                break

        # Stop the router by aborting its read stream — keeps the test from
        # hanging in the task group.
        await read_stream.aclose()

    # Assertion 1: the write did not raise.
    assert write_error is None, (
        f"Expected silent success, but write raised {type(write_error).__name__}: {write_error}"
    )

    # Assertion 2: the router took the drop path (logged "not found").
    assert drop_log_seen, (
        "Expected the message_router to log 'Request stream _GET_stream not found' "
        "but no such log was captured. Either the router didn't process the message "
        "or the drop-path log wording changed in the SDK."
    )


# ---------------------------------------------------------------------------
# Hub-side fix
# ---------------------------------------------------------------------------


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
    return str(result) if result is not None else ""


async def test_push_channel_gates_silent_drop(tmp_path: Path) -> None:
    """`push_channel`'s `_can_deliver_push` gate returns False when the
    bound session's transport is not in `session_manager._server_instances`.
    The bug we're guarding against: without the gate, `send_notification`
    would silently succeed on a stale session (proved in the test above),
    `push_channel` would return True, and the broadcast result would lie
    in its `woke` count.

    Setup mirrors production failure mode:
    - Two agents registered, one bound to a session whose transport isn't
      in the manager's active set (= the post-/compact or post-redeploy
      state where the agent's old session_id has been DELETEd).
    - Broadcast from the other agent.

    Expected: the broadcast result reports `woke 0/1` (no actual delivery),
    not the pre-fix `woke 1/1` lie.
    """
    server = create_server(db_path=tmp_path / "test.db")

    # Stub the session_manager so the gate can introspect `_server_instances`.
    # An empty dict means "no transports active" — no match for any session
    # the gate inspects. This mirrors the production state where an agent's
    # session_id has been removed from the manager (post-/compact or
    # post-disconnect) while their binding in the SessionRegistry persists.
    class _FakeManager:
        _server_instances: dict = {}
    server._session_manager = _FakeManager()  # type: ignore[attr-defined]

    registry = server._hub_registry  # type: ignore[attr-defined]

    await _call_tool(server, "register", {"name": "alice", "project": "x"})
    await _call_tool(server, "register", {"name": "bob", "project": "y"})

    class _StaleSession:
        """Mimics a ServerSession with a real-looking _write_stream — the
        attribute the gate uses to look up the matching transport. With an
        empty `_server_instances`, no transport will match → gate returns
        False → push_channel returns False without calling send_notification."""
        _write_stream = object()

        async def send_ping(self):
            return None

        async def send_notification(self, _notif):
            # If we get here the gate has failed — push reached
            # send_notification on a stale session. The bug we're testing
            # for IS this code path being reached.
            raise AssertionError(
                "send_notification should not be reached when the gate detects "
                "a stale session — the gate's job is to short-circuit before "
                "this point."
            )

    registry.bind("bob", _StaleSession())

    out = await _call_tool(
        server, "broadcast",
        {"from_agent": "alice", "message": "hi", "priority": "normal"},
    )

    # The gate must have caught the stale binding: woke=0 despite bob being
    # bound. Without the fix this would be `woke 1/1`.
    assert "woke 0/1" in out, (
        f"Expected 'woke 0/1' (gate caught the stale binding), got: {out!r}"
    )
