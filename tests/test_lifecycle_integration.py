"""End-to-end integration test for the BaseSession close hook.

The unit tests in `test_session_registry.py` validate the registry's behavior
given a synthetic close signal. This file validates the load-bearing assumption
under all that: that the monkey-patch on `BaseSession.__aexit__` actually fires
our close handlers when a real session closes via `async with`.

If this test breaks, every other "drops on disconnect" guarantee is empty
talk — so it's worth its own file.
"""

from __future__ import annotations

import anyio
import mcp.types as types
import pytest
from mcp.shared.message import SessionMessage
from mcp.shared.session import BaseSession

from mcp_hub.session_registry import (
    SessionRegistry,
    _close_handlers,
    _close_handlers_lock,
)


@pytest.fixture
async def memory_streams():
    """A pair of memory streams suitable for BaseSession's read/write."""
    read_send, read_receive = anyio.create_memory_object_stream[
        SessionMessage | Exception
    ](1)
    write_send, write_receive = anyio.create_memory_object_stream[SessionMessage](1)
    try:
        yield read_receive, write_send
    finally:
        await read_send.aclose()
        await read_receive.aclose()
        await write_send.aclose()
        await write_receive.aclose()


async def test_real_basesession_close_fires_global_handler(memory_streams):
    """Exiting a real BaseSession via `async with` triggers the global
    close-handler dispatch installed by `_ensure_aexit_patched`.

    Without this, the entire 'drops on disconnect' story is fiction.
    """
    read_stream, write_stream = memory_streams
    fired_with: list[BaseSession] = []

    def spy(session: BaseSession) -> None:
        fired_with.append(session)

    with _close_handlers_lock:
        _close_handlers.append(spy)

    try:
        async with BaseSession(
            read_stream=read_stream,
            write_stream=write_stream,
            receive_request_type=types.ClientRequest,
            receive_notification_type=types.ClientNotification,
        ) as session:
            # Inside the context: handler should not have fired yet
            assert fired_with == []

        # After __aexit__: handler should have fired exactly once with this session
        assert len(fired_with) == 1
        assert fired_with[0] is session
    finally:
        with _close_handlers_lock:
            try:
                _close_handlers.remove(spy)
            except ValueError:
                pass


async def test_registry_does_not_drop_binding_on_real_session_close(memory_streams):
    """New contract: lifecycle hook does NOT drop bindings by default.

    Claude Code's MCP client tears down and re-creates streamable-http
    sessions per tool call, so dropping on lifecycle close caused
    bindings to flap. Reaper + push-time ping handle correctness
    without lifecycle drop. Tests that want the old behaviour can
    opt in via `subscribe_to_session_close()`."""
    read_stream, write_stream = memory_streams
    registry = SessionRegistry()

    try:
        async with BaseSession(
            read_stream=read_stream,
            write_stream=write_stream,
            receive_request_type=types.ClientRequest,
            receive_notification_type=types.ClientNotification,
        ) as session:
            registry.bind("alice", session)
            assert registry.is_bound("alice")

        # After session close, binding is INTACT (no auto-drop). Reaper
        # or next push will eventually clean it up if the session is
        # genuinely dead.
        assert registry.is_bound("alice")
    finally:
        registry.close()


async def test_opt_in_lifecycle_drop_works(memory_streams):
    """Tests / specialised registries can opt in to the lifecycle-drop
    behaviour via subscribe_to_session_close(). When subscribed, closing
    the session drops the binding (the old default contract)."""
    read_stream, write_stream = memory_streams
    registry = SessionRegistry()
    registry.subscribe_to_session_close()

    try:
        async with BaseSession(
            read_stream=read_stream,
            write_stream=write_stream,
            receive_request_type=types.ClientRequest,
            receive_notification_type=types.ClientNotification,
        ) as session:
            registry.bind("alice", session)
            assert registry.is_bound("alice")

        # With opt-in subscription, lifecycle close DOES drop
        assert not registry.is_bound("alice")
    finally:
        registry.close()


async def test_multiple_registries_each_get_their_own_cleanup(memory_streams):
    """Two registries that both opt in to lifecycle drop should each
    receive close events for the same session and independently drop
    their bindings."""
    read_stream, write_stream = memory_streams
    reg_a = SessionRegistry()
    reg_a.subscribe_to_session_close()
    reg_b = SessionRegistry()
    reg_b.subscribe_to_session_close()

    try:
        async with BaseSession(
            read_stream=read_stream,
            write_stream=write_stream,
            receive_request_type=types.ClientRequest,
            receive_notification_type=types.ClientNotification,
        ) as session:
            reg_a.bind("alice", session)
            reg_b.bind("alice-too", session)
            assert reg_a.is_bound("alice")
            assert reg_b.is_bound("alice-too")

        # Both opted-in registries should have dropped their bindings
        assert not reg_a.is_bound("alice")
        assert not reg_b.is_bound("alice-too")
    finally:
        reg_a.close()
        reg_b.close()
