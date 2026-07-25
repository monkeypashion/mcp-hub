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


async def test_push_to_live_session_sends_directly(registry):
    """Push contract: just send_notification. No ping (the old ping was a
    false-negative gate against Claude Code clients that don't respond to
    ping requests even when fully alive)."""
    s = FakeSession()
    registry.bind("alice", s)

    notif = {"hi": "alice"}
    result = await registry.push("alice", notif)

    assert result is True
    # No ping — that's the whole point of the latency cleanup.
    assert s.pings == 0
    assert s.sends == [notif]
    # Binding survives a successful push
    assert registry.is_bound("alice")


async def test_push_returns_false_when_send_raises_keeps_binding(registry):
    """Send failure: binding still kept. The activity-based reaper is the
    only authoritative drop path — push failures are transient by design."""
    s = FakeSession(send_raises=BrokenPipeError("write-side dead"))
    registry.bind("alice", s)

    result = await registry.push("alice", {"x": 1})

    assert result is False
    # Send was attempted (no pre-ping gate)
    assert s.pings == 0
    # Binding survives — only the activity reaper drops.
    assert registry.is_bound("alice")


async def test_push_does_not_affect_other_bindings(registry):
    """A push failure to one agent must not collateral-damage other
    bindings. With the new keep-on-failure + no-ping contract, neither
    side is affected — sanity check that registry state is per-name
    independent."""
    s_alice = FakeSession(send_raises=ConnectionResetError())
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


def test_check_one_keeps_stale_but_deliverable_binding():
    """The fix for the idle-fleet-offline bug: a binding whose activity has
    gone stale is NOT reaped while its session is still push-deliverable. A
    `--channels` session sitting idle holds a live connection — that IS the
    heartbeat. The reaper refreshes the timestamp and keeps it; on_reap does
    NOT fire (the agent is still genuinely online)."""
    import time as _t

    reaped: list[str] = []
    r = SessionRegistry(
        on_reap=reaped.append,
        liveness_probe=lambda _s: True,  # still deliverable
    )
    try:
        r.ACTIVITY_TIMEOUT_SECONDS = 0.05
        s = FakeSession()
        r.bind("alice", s)
        with r._lock:
            r._last_activity["alice"] = _t.time() - 1.0  # stale
        before = r._last_activity["alice"]

        alive = r._check_one("alice")

        assert alive is True
        assert r.is_bound("alice")
        assert r._last_activity["alice"] > before  # live conn refreshed it
        assert reaped == []  # never marked offline
    finally:
        r.close()


def test_check_one_drops_stale_and_undeliverable_binding():
    """A stale binding whose session is no longer deliverable (connection
    gone) IS reaped, and on_reap fires so the agent is marked offline."""
    import time as _t

    reaped: list[str] = []
    r = SessionRegistry(
        on_reap=reaped.append,
        liveness_probe=lambda _s: False,  # connection dead
    )
    try:
        r.ACTIVITY_TIMEOUT_SECONDS = 0.05
        s = FakeSession()
        r.bind("alice", s)
        with r._lock:
            r._last_activity["alice"] = _t.time() - 1.0  # stale

        alive = r._check_one("alice")

        assert alive is False
        assert not r.is_bound("alice")
        assert reaped == ["alice"]
    finally:
        r.close()


def test_check_one_drops_stale_binding_when_no_probe():
    """With no liveness_probe wired (default), a stale binding is dropped —
    preserves the pre-fix behaviour for setups without a probe."""
    import time as _t

    reaped: list[str] = []
    r = SessionRegistry(on_reap=reaped.append)  # no liveness_probe
    try:
        r.ACTIVITY_TIMEOUT_SECONDS = 0.05
        s = FakeSession()
        r.bind("alice", s)
        with r._lock:
            r._last_activity["alice"] = _t.time() - 1.0

        alive = r._check_one("alice")

        assert alive is False
        assert not r.is_bound("alice")
        assert reaped == ["alice"]
    finally:
        r.close()


def test_check_one_probe_exception_treated_as_undeliverable():
    """If the liveness probe raises, the reaper must not wedge — it treats the
    session as undeliverable and drops the stale binding."""
    import time as _t

    def boom(_s):
        raise RuntimeError("probe blew up")

    r = SessionRegistry(liveness_probe=boom)
    try:
        r.ACTIVITY_TIMEOUT_SECONDS = 0.05
        s = FakeSession()
        r.bind("alice", s)
        with r._lock:
            r._last_activity["alice"] = _t.time() - 1.0

        alive = r._check_one("alice")

        assert alive is False
        assert not r.is_bound("alice")
    finally:
        r.close()


def test_check_one_does_not_probe_recent_binding():
    """Fast path: a recently-active binding is kept WITHOUT consulting the
    probe. Activity alone suffices; the probe is only the fallback for
    bindings that have gone stale."""
    probe_calls: list = []

    def probe(s):
        probe_calls.append(s)
        return True

    r = SessionRegistry(liveness_probe=probe)
    try:
        s = FakeSession()
        r.bind("alice", s)  # fresh activity
        alive = r._check_one("alice")
        assert alive is True
        assert probe_calls == []  # never probed — activity was fresh
    finally:
        r.close()


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


# ---------------------------------------------------------------------------
# touch_activity (heartbeat path) — refresh without bind
# ---------------------------------------------------------------------------


def test_touch_activity_refreshes_existing_binding(registry):
    """The heartbeat daemon's signal: refresh `_last_activity` for an
    already-bound name. Returns True. No index changes."""
    import time as _t

    s = FakeSession()
    registry.bind("alice", s)
    # Backdate so we can detect a refresh
    with registry._lock:
        registry._last_activity["alice"] = _t.time() - 100.0
    before = registry._last_activity["alice"]

    refreshed = registry.touch_activity("alice")

    assert refreshed is True
    assert registry._last_activity["alice"] > before
    # Index must be untouched — same session, no rebind side effect.
    assert registry.get("alice") is s


def test_touch_activity_unbound_returns_false(registry):
    """Heartbeat for an agent with no binding is a no-op. Must NOT create
    a binding (would clobber the wake-target invariant) and must NOT add
    a stray `_last_activity` entry."""
    refreshed = registry.touch_activity("nobody-here")

    assert refreshed is False
    assert not registry.is_bound("nobody-here")
    assert "nobody-here" not in registry._last_activity


def test_touch_activity_does_not_change_session_indexes(registry):
    """Critical: heartbeat must never touch the by_session_id reverse
    index. If it did, a heartbeat-induced index entry could leak across
    rebinds."""
    s = FakeSession()
    registry.bind("alice", s)
    sid = id(s)
    before = set(registry._by_session_id.get(sid, set()))

    registry.touch_activity("alice")

    after = set(registry._by_session_id.get(sid, set()))
    assert after == before, "touch_activity must not mutate session indexes"


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


# ---------------------------------------------------------------------------
# heartbeat_touch — deliverability-verified refresh (stale-binding fix)
#
# The blind spot this covers (observed live 2026-07-18): a client reconnect
# orphans the bound session; the daemon's heartbeats kept refreshing its
# activity via touch_activity, so the reaper never dropped it — the agent
# looked 🟢 online while wakes vanished into a dead socket, and the Stop-hook
# nag (gated on 🟢, deliberately) never fired. heartbeat_touch refuses to keep
# a dead binding warm, and after UNDELIVERABLE_BEATS_TO_DROP consecutive
# misses drops it through the reaper's exact on_reap path so the agent's
# offline status becomes truthful and the existing nag drives re-register.
# ---------------------------------------------------------------------------


def _probe_registry(*, deliverable_flag: dict, reaped: list) -> SessionRegistry:
    """Registry whose probe reads `deliverable_flag['ok']` and whose on_reap
    appends to `reaped` — lets tests flip deliverability mid-flight."""
    return SessionRegistry(
        on_reap=reaped.append,
        liveness_probe=lambda _s: deliverable_flag["ok"],
    )


def test_heartbeat_touch_refreshes_deliverable_binding():
    flag, reaped = {"ok": True}, []
    reg = _probe_registry(deliverable_flag=flag, reaped=reaped)
    reg.bind("alice", FakeSession())
    with reg._lock:
        reg._last_activity["alice"] = 1.0  # ancient
    assert reg.heartbeat_touch("alice") == "refreshed"
    with reg._lock:
        assert reg._last_activity["alice"] > 1.0  # actually refreshed
    assert reg.is_bound("alice")
    assert reaped == []


def test_heartbeat_touch_no_probe_behaves_like_touch_activity():
    """No liveness probe configured (test mode) → trust and refresh."""
    reg = SessionRegistry()
    reg.bind("alice", FakeSession())
    assert reg.heartbeat_touch("alice") == "refreshed"


def test_heartbeat_touch_unbound_is_noop():
    flag, reaped = {"ok": True}, []
    reg = _probe_registry(deliverable_flag=flag, reaped=reaped)
    assert reg.heartbeat_touch("nobody") == "unbound"
    assert reaped == []


def test_heartbeat_touch_undeliverable_does_not_refresh():
    """The core of the fix: a dead binding's activity must NOT be kept warm."""
    flag, reaped = {"ok": False}, []
    reg = _probe_registry(deliverable_flag=flag, reaped=reaped)
    reg.bind("alice", FakeSession())
    with reg._lock:
        reg._last_activity["alice"] = 1.0
    assert reg.heartbeat_touch("alice") == "undeliverable"
    with reg._lock:
        assert reg._last_activity["alice"] == 1.0  # untouched
    assert reg.is_bound("alice")  # kept — first strike only


def test_heartbeat_touch_drops_after_consecutive_misses():
    """Third consecutive undeliverable beat drops the binding and fires
    on_reap (same contract as the reaper → agent marked offline in DB)."""
    flag, reaped = {"ok": False}, []
    reg = _probe_registry(deliverable_flag=flag, reaped=reaped)
    reg.bind("alice", FakeSession())
    assert reg.heartbeat_touch("alice") == "undeliverable"
    assert reg.heartbeat_touch("alice") == "undeliverable"
    assert reg.heartbeat_touch("alice") == "dropped"
    assert not reg.is_bound("alice")
    assert reaped == ["alice"]
    # Subsequent beats see no binding.
    assert reg.heartbeat_touch("alice") == "unbound"


def test_heartbeat_touch_flicker_resets_strikes():
    """A deliverable beat between failures resets the counter — transient
    GET-stream flickers never accumulate toward a drop."""
    flag, reaped = {"ok": False}, []
    reg = _probe_registry(deliverable_flag=flag, reaped=reaped)
    reg.bind("alice", FakeSession())
    assert reg.heartbeat_touch("alice") == "undeliverable"
    assert reg.heartbeat_touch("alice") == "undeliverable"
    flag["ok"] = True
    assert reg.heartbeat_touch("alice") == "refreshed"  # resets strikes
    flag["ok"] = False
    assert reg.heartbeat_touch("alice") == "undeliverable"
    assert reg.heartbeat_touch("alice") == "undeliverable"
    assert reg.is_bound("alice")  # never reached 3 consecutive
    assert reaped == []


def test_heartbeat_touch_rebind_resets_strikes():
    """register() replacing the binding wipes the dead session's strikes —
    a fresh binding must not inherit them."""
    flag, reaped = {"ok": False}, []
    reg = _probe_registry(deliverable_flag=flag, reaped=reaped)
    reg.bind("alice", FakeSession())
    assert reg.heartbeat_touch("alice") == "undeliverable"
    assert reg.heartbeat_touch("alice") == "undeliverable"
    reg.bind("alice", FakeSession())  # re-register on a new session
    assert reg.heartbeat_touch("alice") == "undeliverable"
    assert reg.heartbeat_touch("alice") == "undeliverable"
    assert reg.is_bound("alice")  # fresh binding: strikes started over
    assert reaped == []


def test_heartbeat_touch_probe_exception_counts_as_undeliverable():
    reaped: list = []
    def _boom(_s):
        raise RuntimeError("probe exploded")
    reg = SessionRegistry(on_reap=reaped.append, liveness_probe=_boom)
    reg.bind("alice", FakeSession())
    assert reg.heartbeat_touch("alice") == "undeliverable"
    assert reg.heartbeat_touch("alice") == "undeliverable"
    assert reg.heartbeat_touch("alice") == "dropped"
    assert reaped == ["alice"]


# ---------------------------------------------------------------------------
# Wake-ack expectation — dead-stream detection (the last lying-⚡ mode)
#
# A client whose SSE stream is half-dead accepts pushes but renders nothing:
# binding fresh, deliverability probe green, ⚡ lit, wakes vanishing. The
# server can't introspect the far end, so it demands evidence it CAN see:
# a pushed wake must be followed by agent activity (bind or message drain)
# within WAKE_ACK_TIMEOUT_SECONDS; WAKE_STRIKES_TO_DROP consecutive unacked
# wakes drop the binding (on_reap → truthful offline) and fire on_wake_dead
# (server queues relaunch guidance).
# ---------------------------------------------------------------------------


def _ack_registry(reaped: list, wake_dead: list) -> SessionRegistry:
    reg = SessionRegistry(
        on_reap=reaped.append, on_wake_dead=wake_dead.append
    )
    reg.WAKE_ACK_TIMEOUT_SECONDS = 0.01  # expire instantly for tests
    return reg


def _expire(reg, name):
    """Force the pending expectation past its deadline."""
    import time as _t

    with reg._lock:
        if name in reg._wake_expect:
            reg._wake_expect[name] = _t.time() - 1.0


def test_wake_ack_two_unacked_wakes_drop_binding():
    reaped, wake_dead = [], []
    reg = _ack_registry(reaped, wake_dead)
    reg.bind("alice", FakeSession())

    reg.expect_wake_ack("alice")
    _expire(reg, "alice")
    assert reg.sweep_wake_acks() == []  # strike 1 — binding survives
    assert reg.is_bound("alice")

    reg.expect_wake_ack("alice")
    _expire(reg, "alice")
    assert reg.sweep_wake_acks() == ["alice"]  # strike 2 — dropped
    assert not reg.is_bound("alice")
    assert reaped == ["alice"]
    assert wake_dead == ["alice"]


def test_wake_ack_drain_clears_expectation_and_strikes():
    """get_messages-style ack resets everything — even after a strike."""
    reaped, wake_dead = [], []
    reg = _ack_registry(reaped, wake_dead)
    reg.bind("alice", FakeSession())

    reg.expect_wake_ack("alice")
    _expire(reg, "alice")
    reg.sweep_wake_acks()  # strike 1
    reg.wake_ack("alice")  # agent drained its inbox

    reg.expect_wake_ack("alice")
    _expire(reg, "alice")
    assert reg.sweep_wake_acks() == []  # back to strike 1, not 2
    assert reg.is_bound("alice")
    assert wake_dead == []


def test_wake_ack_bind_is_an_ack():
    """Any interactive tool call (bind, incl. same-session refresh) acks."""
    reaped, wake_dead = [], []
    reg = _ack_registry(reaped, wake_dead)
    s = FakeSession()
    reg.bind("alice", s)

    reg.expect_wake_ack("alice")
    reg.bind("alice", s)  # same-session touch — the common turn pattern
    _expire(reg, "alice")  # no pending expectation left to expire
    assert reg.sweep_wake_acks() == []
    with reg._lock:
        assert "alice" not in reg._wake_strikes


def test_wake_ack_unbound_noop_and_no_stacking():
    reaped, wake_dead = [], []
    reg = _ack_registry(reaped, wake_dead)
    reg.expect_wake_ack("nobody")  # unbound — no-op
    assert reg.sweep_wake_acks() == []

    reg.bind("alice", FakeSession())
    reg.expect_wake_ack("alice")
    with reg._lock:
        first_deadline = reg._wake_expect["alice"]
    reg.expect_wake_ack("alice")  # burst of wakes — deadline must not reset
    with reg._lock:
        assert reg._wake_expect["alice"] == first_deadline


def test_wake_ack_timely_ack_before_sweep_is_clean():
    """The healthy path: wake pushed, agent acts within the window."""
    reaped, wake_dead = [], []
    reg = _ack_registry(reaped, wake_dead)
    reg.WAKE_ACK_TIMEOUT_SECONDS = 60.0  # generous — not expiring in-test
    reg.bind("alice", FakeSession())
    reg.expect_wake_ack("alice")
    reg.wake_ack("alice")
    assert reg.sweep_wake_acks() == []
    assert reg.is_bound("alice")


def test_wake_ack_strike_keeps_render_unproven_after_expiry():
    """A missed ack must stay VISIBLE to the render gate after the sweeper runs.

    Proven live on mcp-hub-fireblade-wsl 2026-07-25: sweep_wake_acks() deletes
    the expired expectation and records a strike, so has_pending_wake_ack() —
    which only consults _wake_expect — flips back to False ~90s after the push.
    A drain later than that reads "render is not in doubt" and falsely claims
    "already delivered live" on a stream that rendered nothing (the deaf agent
    had drained 90 MINUTES after the push).

    The strike IS the negative evidence: one unacked wake is already proof the
    stream didn't render. Only a genuine ack may clear it.
    """
    reaped, wake_dead = [], []
    reg = _ack_registry(reaped, wake_dead)
    reg.bind("alice", FakeSession())

    reg.expect_wake_ack("alice")
    assert reg.has_pending_wake_ack("alice"), "pending pre-sweep (already passed)"

    _expire(reg, "alice")
    assert reg.sweep_wake_acks() == []  # strike 1 — binding survives, deaf + ⚡

    assert reg.has_pending_wake_ack("alice"), (
        "after the sweeper expired an unacked wake, render is STILL unproven — "
        "the strike is the evidence and the gate must not lose it"
    )

    # ...and a real ack is still the only thing that clears it.
    reg.wake_ack("alice")
    assert not reg.has_pending_wake_ack("alice")
