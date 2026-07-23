"""Tests for multi-session-per-agent support.

One derived identity (`name = <repo>-<hostname>`) can carry more than one live
conversation — e.g. a tmux session AND a Co-work session in the same repo on the
same host both derive `pm-dev-vm-1`. Before this, a second `register()` EVICTED
the first (silent clobber → the loser went deaf while ⚡ still showed). Now the
incumbent is DEMOTED into `_extra_sessions` and wakes fan out to every live
session.

Contract under test:
- second bind does NOT evict the first — both are still tracked, both wakeable
- the most-recently-active session is the primary (carries the lifecycle
  machinery); the previous primary is demoted, not dropped
- when the primary is dropped by ANY path (heartbeat / wake-ack / reaper /
  session-close), a live extra is promoted and the agent STAYS online; on_reap
  fires only when the last session goes
- an extra promoted after a drop starts with a clean verification slate
- session_count / sessions reflect the fan-out set
"""

from __future__ import annotations

import pytest

from mcp_hub.session_registry import SessionRegistry
from tests.test_session_registry import FakeSession


@pytest.fixture
def registry():
    r = SessionRegistry()
    yield r
    r.close()


def _probe_registry(*, deliverable_flag: dict, reaped: list,
                    wake_dead: list | None = None) -> SessionRegistry:
    return SessionRegistry(
        on_reap=reaped.append,
        liveness_probe=lambda _s: deliverable_flag["ok"],
        on_wake_dead=(wake_dead.append if wake_dead is not None else None),
    )


# ---------------------------------------------------------------------------
# bind: demote-don't-evict
# ---------------------------------------------------------------------------


def test_second_bind_does_not_evict_first(registry):
    a, b = FakeSession(), FakeSession()
    registry.bind("pm", a)
    registry.bind("pm", b)
    # Both sessions are still tracked.
    assert registry.session_count("pm") == 2
    sessions = registry.sessions("pm")
    assert set(map(id, sessions)) == {id(a), id(b)}
    # Newest is primary; the old primary was demoted, not dropped.
    assert registry.get("pm") is b
    assert registry.is_bound("pm")


def test_sessions_lists_primary_first(registry):
    a, b, c = FakeSession(), FakeSession(), FakeSession()
    registry.bind("pm", a)
    registry.bind("pm", b)
    registry.bind("pm", c)
    assert registry.session_count("pm") == 3
    assert registry.sessions("pm")[0] is c  # most-recently-active is primary


def test_rebind_known_extra_promotes_it(registry):
    """A session that's currently an extra re-binding (its conversation made a
    tool call) is promoted back to primary without inflating the count."""
    a, b = FakeSession(), FakeSession()
    registry.bind("pm", a)   # a primary
    registry.bind("pm", b)   # b primary, a demoted
    assert registry.get("pm") is b
    registry.bind("pm", a)   # a acts again → promoted back
    assert registry.get("pm") is a
    assert registry.session_count("pm") == 2  # still exactly two, no dupes


def test_same_session_rebind_is_noop_on_count(registry):
    a = FakeSession()
    registry.bind("pm", a)
    registry.bind("pm", a)  # same session refresh
    assert registry.session_count("pm") == 1


# ---------------------------------------------------------------------------
# fan-out push
# ---------------------------------------------------------------------------


async def test_push_reaches_primary(registry):
    """registry.push targets the primary; both sessions are reachable through
    the sessions() list that push_channel fans over."""
    a, b = FakeSession(), FakeSession()
    registry.bind("pm", a)
    registry.bind("pm", b)
    ok = await registry.push("pm", {"n": 1})
    assert ok
    assert b.sends == [{"n": 1}]  # primary got it
    # The extra is delivered to explicitly by push_to_session (what the
    # server's fan-out loop does for each extra).
    ok2 = await registry.push_to_session("pm", a, {"n": 2})
    assert ok2
    assert a.sends == [{"n": 2}]


async def test_push_to_session_failure_does_not_unbind(registry):
    boom = FakeSession(send_raises=RuntimeError("dead"))
    registry.bind("pm", boom)
    ok = await registry.push_to_session("pm", boom, {"n": 1})
    assert ok is False
    # push_to_session never unbinds — the caller owns lifecycle.
    assert registry.is_bound("pm")


# ---------------------------------------------------------------------------
# unbind_session: prune one, keep the rest
# ---------------------------------------------------------------------------


def test_unbind_extra_keeps_primary_online(registry):
    a, b = FakeSession(), FakeSession()
    registry.bind("pm", a)   # a primary
    registry.bind("pm", b)   # b primary, a extra
    fully_offline = registry.unbind_session("pm", a)  # prune the extra
    assert fully_offline is False
    assert registry.get("pm") is b
    assert registry.session_count("pm") == 1


def test_unbind_primary_promotes_extra(registry):
    a, b = FakeSession(), FakeSession()
    registry.bind("pm", a)
    registry.bind("pm", b)   # b primary, a extra
    fully_offline = registry.unbind_session("pm", b)  # drop the primary
    assert fully_offline is False
    assert registry.get("pm") is a  # a promoted
    assert registry.session_count("pm") == 1


def test_unbind_last_session_goes_offline(registry):
    a = FakeSession()
    registry.bind("pm", a)
    assert registry.unbind_session("pm", a) is True
    assert not registry.is_bound("pm")


# ---------------------------------------------------------------------------
# drop paths promote instead of taking the agent offline
# ---------------------------------------------------------------------------


def test_reaper_drop_promotes_extra_no_reap():
    """A stale + undeliverable primary is dropped, but a live extra keeps the
    agent online — on_reap must NOT fire while a session survives."""
    flag, reaped = {"ok": False}, []
    reg = _probe_registry(deliverable_flag=flag, reaped=reaped)
    a, b = FakeSession(), FakeSession()
    reg.bind("pm", a)
    reg.bind("pm", b)  # b primary, a extra
    with reg._lock:
        reg._last_activity["pm"] = 1.0  # ancient → past ACTIVITY_TIMEOUT
    survives = reg._check_one("pm")
    assert survives is True          # agent still online (via promoted a)
    assert reg.get("pm") is a        # extra promoted to primary
    assert reg.session_count("pm") == 1
    assert reaped == []              # NOT marked offline


def test_reaper_drop_last_session_reaps():
    """No extra to promote → the reaper fully drops and fires on_reap once."""
    flag, reaped = {"ok": False}, []
    reg = _probe_registry(deliverable_flag=flag, reaped=reaped)
    reg.bind("pm", FakeSession())
    with reg._lock:
        reg._last_activity["pm"] = 1.0
    survives = reg._check_one("pm")
    assert survives is False
    assert not reg.is_bound("pm")
    assert reaped == ["pm"]


def test_heartbeat_drop_promotes_extra(registry):
    flag, reaped = {"ok": False}, []
    reg = _probe_registry(deliverable_flag=flag, reaped=reaped)
    a, b = FakeSession(), FakeSession()
    reg.bind("pm", a)
    reg.bind("pm", b)  # b primary, a extra
    # Three undeliverable beats would normally drop the binding; here the
    # third promotes the extra and reports the agent still reachable.
    assert reg.heartbeat_touch("pm") == "undeliverable"
    assert reg.heartbeat_touch("pm") == "undeliverable"
    assert reg.heartbeat_touch("pm") == "refreshed"  # promoted, stays online
    assert reg.get("pm") is a
    assert reg.session_count("pm") == 1
    assert reaped == []


def test_heartbeat_drop_last_session_reaps():
    flag, reaped = {"ok": False}, []
    reg = _probe_registry(deliverable_flag=flag, reaped=reaped)
    reg.bind("pm", FakeSession())
    assert reg.heartbeat_touch("pm") == "undeliverable"
    assert reg.heartbeat_touch("pm") == "undeliverable"
    assert reg.heartbeat_touch("pm") == "dropped"
    assert not reg.is_bound("pm")
    assert reaped == ["pm"]


def test_wake_ack_drop_promotes_extra():
    reaped, wake_dead = [], []
    reg = SessionRegistry(on_reap=reaped.append, on_wake_dead=wake_dead.append)
    a, b = FakeSession(), FakeSession()
    reg.bind("pm", a)
    reg.bind("pm", b)  # b primary, a extra
    # Two unacked wakes (WAKE_STRIKES_TO_DROP) drop the primary; a is promoted.
    for _ in range(reg.WAKE_STRIKES_TO_DROP):
        reg.expect_wake_ack("pm")
        with reg._lock:
            reg._wake_expect["pm"] = 1.0  # force overdue
        dropped = reg.sweep_wake_acks()
    assert dropped == []             # agent never went fully offline
    assert reg.get("pm") is a
    assert reg.session_count("pm") == 1
    assert reaped == [] and wake_dead == []


def test_promoted_extra_has_clean_slate():
    """After promotion the new primary isn't charged the dead primary's
    strikes: it takes a full fresh set of misses to drop it in turn."""
    flag, reaped = {"ok": False}, []
    reg = _probe_registry(deliverable_flag=flag, reaped=reaped)
    a, b = FakeSession(), FakeSession()
    reg.bind("pm", a)
    reg.bind("pm", b)
    # Drop b (primary) via 3 misses → a promoted.
    reg.heartbeat_touch("pm")
    reg.heartbeat_touch("pm")
    assert reg.heartbeat_touch("pm") == "refreshed"
    assert reg.get("pm") is a
    # a now needs its OWN 3 consecutive misses before it drops.
    assert reg.heartbeat_touch("pm") == "undeliverable"
    assert reg.heartbeat_touch("pm") == "undeliverable"
    assert reg.heartbeat_touch("pm") == "dropped"
    assert not reg.is_bound("pm")
    assert reaped == ["pm"]


# ---------------------------------------------------------------------------
# session-close drops one session, promotes on primary close
# ---------------------------------------------------------------------------


def test_session_close_of_primary_promotes_extra(registry):
    registry.subscribe_to_session_close()
    a, b = FakeSession(), FakeSession()
    registry.bind("pm", a)
    registry.bind("pm", b)   # b primary
    registry._on_session_close(b)   # simulate b's transport closing
    assert registry.get("pm") is a  # a promoted
    assert registry.session_count("pm") == 1
    assert registry.is_bound("pm")


def test_session_close_of_extra_leaves_primary(registry):
    a, b = FakeSession(), FakeSession()
    registry.bind("pm", a)
    registry.bind("pm", b)   # b primary, a extra
    registry._on_session_close(a)   # the extra closes
    assert registry.get("pm") is b  # primary untouched
    assert registry.session_count("pm") == 1


def test_session_close_of_only_session_goes_offline(registry):
    a = FakeSession()
    registry.bind("pm", a)
    registry._on_session_close(a)
    assert not registry.is_bound("pm")
    assert registry.session_count("pm") == 0


# ---------------------------------------------------------------------------
# generation token: rebinding a new primary re-mints (degrades safe)
# ---------------------------------------------------------------------------


def test_generation_changes_on_primary_change(registry):
    a, b = FakeSession(), FakeSession()
    registry.bind("pm", a)
    gen_a = registry.generation("pm")
    registry.bind("pm", b)
    gen_b = registry.generation("pm")
    assert gen_a != gen_b  # new primary → new token → compact render fails safe
