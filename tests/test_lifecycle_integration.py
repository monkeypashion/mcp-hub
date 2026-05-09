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


async def test_registry_drops_binding_on_real_session_close(memory_streams):
    """End-to-end: bind a name in a real registry, close the session,
    verify the binding is gone. This is the bug we're fixing."""
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
            assert "alice" in registry

        # After session close, the registry should have dropped alice
        assert not registry.is_bound("alice")
        assert "alice" not in registry
        assert registry.names() == []
    finally:
        registry.close()


async def test_multiple_registries_each_get_their_own_cleanup(memory_streams):
    """Two registries co-existing should both receive close events for the
    same session, each independently dropping their bindings."""
    read_stream, write_stream = memory_streams
    reg_a = SessionRegistry()
    reg_b = SessionRegistry()

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

        # Both registries should have dropped their bindings
        assert not reg_a.is_bound("alice")
        assert not reg_b.is_bound("alice-too")
    finally:
        reg_a.close()
        reg_b.close()
