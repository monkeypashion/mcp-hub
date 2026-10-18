"""The transport leak: every clean disconnect held its transport forever.

MEASURED before the fix, on a local hub with nothing else running — 5, then
10, then 10 clean connect/disconnect cycles:

    Transports:  1 -> 7 -> 18 -> 29        every cycle leaked, none reclaimed
    with the fix: 1 -> 1 ->  1 ->  1        (the 1 is the live gauge reader)

Cause is in the library, not here. A client's `async with` exit sends DELETE;
`_handle_delete_request` calls `terminate()`, which closes the streams and
sets `_terminated = True` — and nothing unregisters it from the manager's
`_server_instances`. The only cleanup that would is `run_server`'s `finally`,
guarded by `not is_terminated` because it exists for the CRASH path. So the
clean path every well-behaved client takes had no reclaim at all.

It costs LATENCY before memory: `_can_deliver_push` walks that dict once per
bound session, so `list_agents` is O(sessions x transports) over a set that
only grows — the 798ms floor never beaten across 9,891 prod calls.

🔴 THE SAFETY PROPERTY IS THE INVERSE ONE, and it is why `is_terminated` is
the only gate: dropping a LIVE transport makes an agent silently unwakeable,
which is far worse than the leak. Anything that cannot answer is left alone.
"""
from __future__ import annotations

from mcp_hub.server import _reclaim_terminated


class T:
    def __init__(self, terminated):
        self.is_terminated = terminated


class Mute:
    """A transport that cannot say whether it is terminated."""


class Mgr:
    def __init__(self, instances, owners=None):
        self._server_instances = instances
        self._session_owners = owners if owners is not None else {}


def test_a_terminated_transport_is_reclaimed():
    m = Mgr({"a": T(True)})
    assert _reclaim_terminated(m, "a") == 1
    assert m._server_instances == {}


def test_a_LIVE_transport_is_never_reclaimed():
    """The bug this must not introduce: a live session dropped here is an
    agent that goes silently unwakeable."""
    m = Mgr({"a": T(False)})
    assert _reclaim_terminated(m, "a") == 0
    assert "a" in m._server_instances


def test_a_transport_that_cannot_answer_is_left_alone():
    """Absence of evidence is not evidence of termination."""
    m = Mgr({"a": Mute()})
    assert _reclaim_terminated(m, "a") == 0
    assert "a" in m._server_instances


def test_a_NORMAL_delete_also_clears_stragglers():
    """🔴 THE TEST THAT USED TO PIN THE BUG. It asserted that a DELETE with a
    known sid reclaimed ONLY that session and left `b` behind — which is what
    the code did, so the suite was green over a backstop that could not fire.
    The sweep ran only when the sid was unreadable, i.e. almost never, while
    the docstring promised straggler coverage.

    A straggler is a transport terminated with no DELETE of its own. Nothing
    else ever reclaims one, so if a normal DELETE does not, it is held for the
    life of the process — the same shape as the leak this function closes.
    """
    m = Mgr({"a": T(True), "b": T(True)})
    assert _reclaim_terminated(m, "a") == 2
    assert m._server_instances == {}


def test_the_sweep_still_spares_a_live_session_on_the_targeted_path():
    """The inverse property, restated for the sweeping version: widening what
    is EXAMINED must not widen what is DROPPED."""
    m = Mgr({"a": T(True), "live": T(False), "mute": Mute()})
    assert _reclaim_terminated(m, "a") == 1
    assert set(m._server_instances) == {"live", "mute"}


def test_the_sweep_reclaims_every_terminated_straggler():
    """No sid (or an unknown one) falls back to the backstop sweep — the
    catch-all for a DELETE whose id we could not read."""
    m = Mgr({"a": T(True), "b": T(False), "c": T(True)})
    assert _reclaim_terminated(m) == 2
    assert set(m._server_instances) == {"b"}


def test_an_unknown_sid_still_sweeps_rather_than_doing_nothing():
    m = Mgr({"a": T(True)})
    assert _reclaim_terminated(m, "not-a-session") == 1


def test_the_owner_record_goes_with_it():
    """A stale owner entry outliving its session is the same leak, smaller."""
    m = Mgr({"a": T(True)}, {"a": "someone"})
    _reclaim_terminated(m, "a")
    assert m._session_owners == {}


def test_a_manager_with_no_instances_is_a_quiet_no_op():
    class Bare:
        pass
    assert _reclaim_terminated(Bare(), "a") == 0


def test_a_broken_manager_never_raises():
    """A reclaim that can break a request is worse than the leak."""
    class Hostile:
        @property
        def _server_instances(self):
            raise RuntimeError("boom")
    assert _reclaim_terminated(Hostile(), "a") == 0


def test_reclaiming_twice_is_safe():
    m = Mgr({"a": T(True)})
    assert _reclaim_terminated(m, "a") == 1
    assert _reclaim_terminated(m, "a") == 0
