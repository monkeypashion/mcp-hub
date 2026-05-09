"""Tests for the SessionRegistry — covers binding semantics, lifecycle hook,
and the ping-then-send push contract.

The registry uses object identity to track sessions, so tests use a minimal
`FakeSession` stand-in instead of a real `ServerSession` (which requires the
full MCP transport stack to instantiate). The contract being tested is:

- `is_bound(name)` reflects current state truthfully
- A session that closes (via the lifecycle hook) drops every name bound to it
- `push()` pings before sending; ping failures or send failures both clean
  up the binding and return False
- A push to an unbound name is a no-op returning False (no exception)
"""

from __future__ import annotations

import anyio
import pytest

from mcp_hub.session_registry import SessionRegistry


# ---------------------------------------------------------------------------
# Minimal session stand-in
# ---------------------------------------------------------------------------


class FakeSession:
    """Stand-in for ServerSession — the registry only uses object identity
    plus the async send_ping() / send_notification() methods."""

    def __init__(
        self,
        *,
        ping_raises: BaseException | None = None,
        send_raises: BaseException | None = None,
        ping_delay: float = 0.0,
    ) -> None:
        self.ping_raises = ping_raises
        self.send_raises = send_raises
        self.ping_delay = ping_delay
        self.pings = 0
        self.sends: list = []

    async def send_ping(self):
        self.pings += 1
        if self.ping_delay:
            await anyio.sleep(self.ping_delay)
        if self.ping_raises is not None:
            raise self.ping_raises

    async def send_notification(self, notification):
        if self.send_raises is not None:
            raise self.send_raises
        self.sends.append(notification)


@pytest.fixture
def registry():
    """Fresh registry per test, properly torn down so the global close-hook
    doesn't accumulate handlers across tests."""
    r = SessionRegistry()
    yield r
    r.close()


# ---------------------------------------------------------------------------
# Synchronous binding semantics
# ---------------------------------------------------------------------------


def test_empty_registry(registry):
    assert registry.names() == []
    assert not registry.is_bound("alice")
    assert "alice" not in registry
    assert registry.get("alice") is None


def test_bind_and_get(registry):
    s = FakeSession()
    registry.bind("alice", s)
    assert registry.get("alice") is s
    assert registry.is_bound("alice")
    assert "alice" in registry
    assert registry.names() == ["alice"]


def test_bind_idempotent_for_same_session(registry):
    s = FakeSession()
    registry.bind("alice", s)
    registry.bind("alice", s)  # idempotent
    assert registry.get("alice") is s
    assert registry.names() == ["alice"]


def test_bind_replaces_existing_for_same_name(registry):
    s1, s2 = FakeSession(), FakeSession()
    registry.bind("alice", s1)
    registry.bind("alice", s2)
    assert registry.get("alice") is s2


def test_bind_replacing_session_drops_reverse_index(registry):
    """When alice rebinds from s1 to s2, the close hook for s1 must NOT also
    drop alice — alice has moved to s2 and should survive s1's cleanup."""
    s1, s2 = FakeSession(), FakeSession()
    registry.bind("alice", s1)
    registry.bind("alice", s2)
    # Simulate s1 closing (e.g. its connection died after alice rebound)
    registry._on_session_close(s1)
    assert registry.is_bound("alice")
    assert registry.get("alice") is s2


def test_unbind_name(registry):
    s = FakeSession()
    registry.bind("alice", s)
    registry.unbind_name("alice")
    assert not registry.is_bound("alice")
    assert registry.get("alice") is None


def test_unbind_name_idempotent(registry):
    registry.unbind_name("nobody-here")  # must not raise


def test_one_session_can_bind_multiple_names(registry):
    """Aliasing — one MCP session bound under two names. Both should resolve
    to the same session and both should drop together when the session closes.
    """
    s = FakeSession()
    registry.bind("alice", s)
    registry.bind("alice-alias", s)
    assert registry.get("alice") is s
    assert registry.get("alice-alias") is s
    assert sorted(registry.names()) == ["alice", "alice-alias"]


# ---------------------------------------------------------------------------
# Lifecycle hook
# ---------------------------------------------------------------------------


def test_on_session_close_drops_all_names_for_that_session(registry):
    s = FakeSession()
    registry.bind("alice", s)
    registry.bind("alice-alias", s)
    registry._on_session_close(s)
    assert not registry.is_bound("alice")
    assert not registry.is_bound("alice-alias")
    assert registry.names() == []


def test_on_session_close_unrelated_session_is_noop(registry):
    s_real = FakeSession()
    s_other = FakeSession()
    registry.bind("alice", s_real)
    registry._on_session_close(s_other)  # unrelated
    assert registry.is_bound("alice")


def test_on_session_close_empty_registry_is_noop(registry):
    s = FakeSession()
    registry._on_session_close(s)  # must not raise


def test_close_handler_actually_registered():
    """Creating a registry should register its close handler globally; calling
    close() should remove it. This validates the lifecycle wiring."""
    from mcp_hub import session_registry as sr

    initial_count = len(sr._close_handlers)
    r = SessionRegistry()
    assert len(sr._close_handlers) == initial_count + 1
    r.close()
    assert len(sr._close_handlers) == initial_count


def test_aexit_is_patched():
    """Sanity check: importing the module installs the BaseSession.__aexit__
    patch. Without this, lifecycle detection wouldn't work in production."""
    from mcp_hub import session_registry as sr

    assert sr._aexit_patched
    assert sr._original_aexit is not None


# ---------------------------------------------------------------------------
# Push contract
# ---------------------------------------------------------------------------


async def test_push_to_unbound_returns_false(registry):
    result = await registry.push("nobody", {"hi": "there"})
    assert result is False


async def test_push_to_live_session_pings_then_sends(registry):
    s = FakeSession()
    registry.bind("alice", s)

    notif = {"hi": "alice"}
    result = await registry.push("alice", notif)

    assert result is True
    assert s.pings == 1
    assert s.sends == [notif]
    # Binding survives a successful push
    assert registry.is_bound("alice")


async def test_push_drops_binding_when_ping_raises(registry):
    s = FakeSession(ping_raises=ConnectionResetError("dead socket"))
    registry.bind("alice", s)

    result = await registry.push("alice", {"x": 1})

    assert result is False
    assert s.pings == 1
    assert s.sends == []  # send must not be attempted after ping failure
    assert not registry.is_bound("alice")  # binding dropped


async def test_push_drops_binding_when_ping_times_out(registry):
    # Ping delay exceeds the registry's PING_TIMEOUT_SECONDS — should trigger
    # the timeout path. Use a tight override so the test runs fast.
    registry.PING_TIMEOUT_SECONDS = 0.05
    s = FakeSession(ping_delay=0.5)
    registry.bind("alice", s)

    result = await registry.push("alice", {"x": 1})

    assert result is False
    assert s.sends == []
    assert not registry.is_bound("alice")


async def test_push_drops_binding_when_send_raises_after_live_ping(registry):
    s = FakeSession(send_raises=BrokenPipeError("write-side dead"))
    registry.bind("alice", s)

    result = await registry.push("alice", {"x": 1})

    assert result is False
    assert s.pings == 1  # ping succeeded
    assert not registry.is_bound("alice")  # send-failure also drops binding


async def test_push_does_not_affect_other_bindings(registry):
    """A push failure to one agent must not collateral-damage other bindings."""
    s_alice = FakeSession(ping_raises=ConnectionResetError())
    s_bob = FakeSession()
    registry.bind("alice", s_alice)
    registry.bind("bob", s_bob)

    result = await registry.push("alice", {"x": 1})

    assert result is False
    assert not registry.is_bound("alice")
    assert registry.is_bound("bob")  # bob is unaffected


# ---------------------------------------------------------------------------
# Reaper
# ---------------------------------------------------------------------------


async def test_ping_one_keeps_live_binding(registry):
    s = FakeSession()
    registry.bind("alice", s)
    alive = await registry._ping_one("alice")
    assert alive is True
    assert registry.is_bound("alice")
    assert s.pings == 1


async def test_ping_one_drops_dead_binding(registry):
    s = FakeSession(ping_raises=ConnectionResetError())
    registry.bind("alice", s)
    alive = await registry._ping_one("alice")
    assert alive is False
    assert not registry.is_bound("alice")


async def test_ping_one_drops_on_timeout(registry):
    registry.PING_TIMEOUT_SECONDS = 0.05
    s = FakeSession(ping_delay=0.5)
    registry.bind("alice", s)
    alive = await registry._ping_one("alice")
    assert alive is False
    assert not registry.is_bound("alice")


async def test_ping_one_unbound_returns_false(registry):
    alive = await registry._ping_one("nobody-here")
    assert alive is False


async def test_reaper_drops_dead_keeps_live(registry):
    """One reaper cycle: dead binding gets dropped, live binding survives."""
    import anyio

    registry.REAPER_INTERVAL_SECONDS = 0.05  # fast cycle for the test

    s_alice = FakeSession(ping_raises=ConnectionResetError())
    s_bob = FakeSession()
    registry.bind("alice", s_alice)
    registry.bind("bob", s_bob)

    async with anyio.create_task_group() as tg:
        tg.start_soon(registry.run_reaper)
        # Wait long enough for one reaper cycle to fire (interval + a margin)
        await anyio.sleep(0.2)
        tg.cancel_scope.cancel()

    assert not registry.is_bound("alice")
    assert registry.is_bound("bob")
    assert s_bob.pings >= 1  # bob got pinged


async def test_reaper_survives_ping_exceptions(registry):
    """A bad ping in one iteration must not kill the reaper for subsequent
    iterations — important so a single zombie can't permanently disable
    background liveness checks."""
    import anyio

    registry.REAPER_INTERVAL_SECONDS = 0.05

    s_dead = FakeSession(ping_raises=BrokenPipeError())
    s_live = FakeSession()
    registry.bind("dead", s_dead)
    registry.bind("live", s_live)

    async with anyio.create_task_group() as tg:
        tg.start_soon(registry.run_reaper)
        # Wait for multiple cycles so we know the loop didn't die after the
        # first failure.
        await anyio.sleep(0.3)
        tg.cancel_scope.cancel()

    assert not registry.is_bound("dead")
    assert registry.is_bound("live")
    # Multiple cycles means live got pinged more than once
    assert s_live.pings >= 2


async def test_reaper_clean_cancel(registry):
    """Cancelling the reaper exits cleanly without raising."""
    import anyio

    registry.REAPER_INTERVAL_SECONDS = 1.0  # so we're sleeping when cancelled

    async with anyio.create_task_group() as tg:
        tg.start_soon(registry.run_reaper)
        await anyio.sleep(0.05)  # enough to enter the sleep
        tg.cancel_scope.cancel()
    # Reaching here means the cancel propagated cleanly
