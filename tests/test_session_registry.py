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


def test_default_registry_does_not_subscribe_to_close():
    """New default contract: registries do NOT subscribe to lifecycle close
    by default. Claude Code's MCP client tears down sessions per tool call;
    auto-dropping on close caused bindings to flap."""
    from mcp_hub import session_registry as sr

    initial_count = len(sr._close_handlers)
    r = SessionRegistry()
    assert len(sr._close_handlers) == initial_count, (
        "Default registry should NOT subscribe to lifecycle close events"
    )
    r.close()  # idempotent — was never subscribed
    assert len(sr._close_handlers) == initial_count


def test_opt_in_subscription_works():
    """Tests / specialised registries can opt in via
    `subscribe_to_session_close()`. close() unsubscribes."""
    from mcp_hub import session_registry as sr

    initial_count = len(sr._close_handlers)
    r = SessionRegistry()
    r.subscribe_to_session_close()
    assert len(sr._close_handlers) == initial_count + 1
    r.close()
    assert len(sr._close_handlers) == initial_count


def test_subscribe_is_idempotent():
    """Calling subscribe twice should still leave only one handler registered."""
    from mcp_hub import session_registry as sr

    initial_count = len(sr._close_handlers)
    r = SessionRegistry()
    r.subscribe_to_session_close()
    r.subscribe_to_session_close()
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


async def test_push_returns_false_when_ping_raises_keeps_binding(registry):
    """Push contract changed: ping failure must NOT drop the binding.
    Claude Code's MCP client cycles streamable-http session_ids ~30s after
    activity, so the bound session can be transiently dead while the agent
    is still very much alive. The activity-based reaper is the only
    authoritative drop path."""
    s = FakeSession(ping_raises=ConnectionResetError("dead socket"))
    registry.bind("alice", s)

    result = await registry.push("alice", {"x": 1})

    assert result is False
    assert s.pings == 1
    assert s.sends == []  # send is skipped when ping fails
    # New contract: binding survives — only the activity reaper drops.
    assert registry.is_bound("alice")


async def test_push_returns_false_when_ping_times_out_keeps_binding(registry):
    """Same contract for timeouts as for exceptions: don't drop on push
    failure, just report False and let the inbox/Stop-hook path deliver."""
    registry.PING_TIMEOUT_SECONDS = 0.05
    s = FakeSession(ping_delay=0.5)
    registry.bind("alice", s)

    result = await registry.push("alice", {"x": 1})

    assert result is False
    assert s.sends == []
    assert registry.is_bound("alice")


async def test_push_returns_false_when_send_raises_keeps_binding(registry):
    """Send failure after a successful ping: binding still kept. Same
    rationale — transient send failure shouldn't drop a bound agent."""
    s = FakeSession(send_raises=BrokenPipeError("write-side dead"))
    registry.bind("alice", s)

    result = await registry.push("alice", {"x": 1})

    assert result is False
    assert s.pings == 1  # ping succeeded
    assert registry.is_bound("alice")


async def test_push_does_not_affect_other_bindings(registry):
    """A push failure to one agent must not collateral-damage other
    bindings. With the new keep-on-failure contract, neither side is
    affected — but the test stays as a sanity check that registry state
    is per-name independent."""
    s_alice = FakeSession(ping_raises=ConnectionResetError())
    s_bob = FakeSession()
    registry.bind("alice", s_alice)
    registry.bind("bob", s_bob)

    result = await registry.push("alice", {"x": 1})

    assert result is False
    # Both bindings survive: alice's because of the new keep-on-failure
    # contract, bob's because nothing happened to bob.
    assert registry.is_bound("alice")
    assert registry.is_bound("bob")


# ---------------------------------------------------------------------------
# Reaper — activity-based liveness
# ---------------------------------------------------------------------------
#
# The reaper used to issue server-initiated pings to verify each bound
# session was reachable. That signal turned out to be unreliable in
# production: Claude Code's MCP client cycles streamable-http sessions
# every ~30s (DELETE /mcp + new POST), so the bound session_id was almost
# always dead by the time the reaper pinged it, even when the agent was
# actively working. The fix: track a per-name `last_activity` timestamp
# refreshed by every `bind()` call (which is itself triggered by every
# tool call from the agent via `touch_session`), and reap names whose
# activity is older than ACTIVITY_TIMEOUT_SECONDS.


def test_check_one_keeps_recent_binding(registry):
    """A binding with a fresh activity timestamp survives the reaper sweep."""
    s = FakeSession()
    registry.bind("alice", s)  # bind() refreshes activity
    alive = registry._check_one("alice")
    assert alive is True
    assert registry.is_bound("alice")


def test_check_one_drops_stale_binding(registry):
    """A binding whose last activity is older than the timeout gets reaped."""
    import time as _t

    registry.ACTIVITY_TIMEOUT_SECONDS = 0.05
    s = FakeSession()
    registry.bind("alice", s)
    # Backdate the activity timestamp past the timeout
    with registry._lock:
        registry._last_activity["alice"] = _t.time() - 1.0

    alive = registry._check_one("alice")
    assert alive is False
    assert not registry.is_bound("alice")


def test_check_one_unbound_returns_false(registry):
    """Unbound names report not-alive without raising."""
    alive = registry._check_one("nobody-here")
    assert alive is False


def test_check_one_does_not_drop_recently_refreshed_binding(registry):
    """Even with a tight timeout, a fresh bind() keeps the binding alive
    on the next reap. This is the steady-state pattern: every tool call
    refreshes activity via touch_session -> bind()."""
    registry.ACTIVITY_TIMEOUT_SECONDS = 0.05
    s = FakeSession()
    registry.bind("alice", s)
    # Refresh immediately — the binding has just been touched
    registry.bind("alice", s)
    alive = registry._check_one("alice")
    assert alive is True
    assert registry.is_bound("alice")


def test_bind_refreshes_activity_on_same_session(registry):
    """Re-binding the same name to the same session is the steady-state
    refresh path (every tool call hits this). It must update the activity
    timestamp even though the indexes don't change."""
    import time as _t

    s = FakeSession()
    registry.bind("alice", s)
    # Backdate so we can detect a refresh
    with registry._lock:
        registry._last_activity["alice"] = _t.time() - 100.0
    before = registry._last_activity["alice"]

    registry.bind("alice", s)  # same session — exercise the no-op path
    after = registry._last_activity["alice"]

    assert after > before, "bind() on same session must refresh activity"


def test_bind_refreshes_activity_on_session_swap(registry):
    """When a name is rebound to a different session, the activity
    timestamp must also refresh — the new session is the new source
    of liveness signal."""
    import time as _t

    s1, s2 = FakeSession(), FakeSession()
    registry.bind("alice", s1)
    with registry._lock:
        registry._last_activity["alice"] = _t.time() - 100.0
    before = registry._last_activity["alice"]

    registry.bind("alice", s2)  # swap path
    after = registry._last_activity["alice"]

    assert after > before
    assert registry.get("alice") is s2


def test_unbind_clears_activity_timestamp(registry):
    """Dropping a binding must clear its activity timestamp so a future
    re-bind starts fresh and stale data can't survive across cycles."""
    s = FakeSession()
    registry.bind("alice", s)
    assert "alice" in registry._last_activity

    registry.unbind_name("alice")
    assert "alice" not in registry._last_activity


async def test_reaper_drops_stale_keeps_active(registry):
    """Stale binding gets dropped by the background reaper; an active
    binding (whose activity keeps getting refreshed) survives."""
    import time as _t

    registry.REAPER_INTERVAL_SECONDS = 0.05
    registry.ACTIVITY_TIMEOUT_SECONDS = 0.1

    s_stale = FakeSession()
    s_active = FakeSession()
    registry.bind("stale", s_stale)
    registry.bind("active", s_active)
    # Backdate stale so it's already past the timeout; the very first
    # reaper sweep will drop it.
    with registry._lock:
        registry._last_activity["stale"] = _t.time() - 10.0

    async with anyio.create_task_group() as tg:
        tg.start_soon(registry.run_reaper)
        # Keep refreshing 'active' across multiple reaper cycles to prove
        # activity-based reaping is forgiving of recently-touched bindings.
        for _ in range(4):
            await anyio.sleep(0.05)
            registry.bind("active", s_active)
        tg.cancel_scope.cancel()

    assert not registry.is_bound("stale")
    assert registry.is_bound("active")


async def test_reaper_survives_iteration_errors(registry):
    """A failure in one name's check must not kill the reaper loop for
    subsequent cycles. The production reaper wraps each `_check_one` call
    in try/except so a transient error stays scoped to that one name."""
    registry.REAPER_INTERVAL_SECONDS = 0.05
    registry.ACTIVITY_TIMEOUT_SECONDS = 60.0  # don't actually reap during test

    s = FakeSession()
    registry.bind("alice", s)

    real_check = registry._check_one
    calls = {"n": 0}

    def flaky_check(name: str) -> bool:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return real_check(name)

    registry._check_one = flaky_check  # type: ignore[method-assign]

    async with anyio.create_task_group() as tg:
        tg.start_soon(registry.run_reaper)
        await anyio.sleep(0.3)
        tg.cancel_scope.cancel()

    # The flaky check ran more than once — loop survived its first error.
    assert calls["n"] >= 2
    # Alice's binding is still intact (activity is recent, well within timeout).
    assert registry.is_bound("alice")


async def test_reaper_clean_cancel(registry):
    """Cancelling the reaper exits cleanly without raising."""
    registry.REAPER_INTERVAL_SECONDS = 1.0  # so we're sleeping when cancelled

    async with anyio.create_task_group() as tg:
        tg.start_soon(registry.run_reaper)
        await anyio.sleep(0.05)  # enough to enter the sleep
        tg.cancel_scope.cancel()
    # Reaching here means the cancel propagated cleanly
