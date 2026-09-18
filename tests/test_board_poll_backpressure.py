"""The board's poll must not start a second copy of a scan already running.

`run_worker(..., thread=True, group=..., exclusive=True)` reads as if it did.
It does not: cancelling a THREAD worker only sets a flag, and the executor
thread runs the callable to completion — `subprocess.run` and all. So a tick
slower than its own interval stacks, silently, for as long as the panel is
open, while the group looks protected.

Measured on dev-vm-1 on 2026-09-17: `squad board --json` takes 9-12s against a
3s tick, so 3-4 scans ran concurrently and the board accounted for 70,344
execs in 15s — 63% of all process creation on the operator's working machine.

Every test here drives the real app through Pilot, because the bug lived
entirely in the worker plumbing: a test that called the collector directly
would have passed throughout.
"""
from __future__ import annotations

import threading

import pytest

from mcp_hub.settings_app import SettingsApp

AGENTS = [{"agent": "alpha", "worktree": "/a", "klass": "squad"}]

SNAP = {"agents": {}, "order": [], "counts": {}, "error": None}


def _model_for(cwd):
    return None


def _app(board, now=None):
    # poll_seconds is huge: every tick in these tests is one this file fired
    # deliberately, so a timer can never be the thing under test.
    return SettingsApp(AGENTS, scoped_to=None, model_for=_model_for,
                       squad_bin="/usr/bin/SQUAD", hub_bin="/usr/bin/HUB",
                       board_for=board, dark=None, poll_seconds=3600,
                       this_machine="thisbox", now=now)


async def _settle(pilot, times: int = 6) -> None:
    for _ in range(times):
        await pilot.pause()


@pytest.mark.asyncio
async def test_a_tick_arriving_mid_scan_does_not_start_a_second_scan():
    """The stacking bug itself, in the shape it had on the operator's box."""
    release = threading.Event()
    started = threading.Semaphore(0)
    runs = []

    def board():
        runs.append(1)
        started.release()
        release.wait(10)
        return SNAP

    app = _app(board, now=lambda: 1000.0)
    async with app.run_test(size=(120, 34)) as pilot:
        # on_mount fires the first poll; wait until its THREAD is really in
        # `board()` rather than merely scheduled, or the ticks below would be
        # racing an empty guard and could pass for the wrong reason.
        assert started.acquire(timeout=10)
        for _ in range(6):
            app._poll_board()
            await pilot.pause()
        assert runs == [1], f"{len(runs)} concurrent scans — the tick stacked"
        release.set()
        await _settle(pilot)
    release.set()


@pytest.mark.asyncio
async def test_the_guard_releases_when_the_scan_finishes():
    """A guard that never cleared would fix the storm by killing the board."""
    release = threading.Event()
    started = threading.Semaphore(0)
    runs = []

    def board():
        runs.append(1)
        started.release()
        release.wait(10)
        return SNAP

    app = _app(board, now=lambda: 1000.0)
    async with app.run_test(size=(120, 34)) as pilot:
        assert started.acquire(timeout=10)
        release.set()                   # the scan finishes; the guard clears
        await _settle(pilot)
        app._poll_board()
        # Pumped, never blocked: a sync wait here would hold the event loop
        # that has to START the thread, and the test would deadlock on its
        # own instrument rather than on the code.
        await _settle(pilot)
        assert len(runs) == 2, "the poll stopped polling"


@pytest.mark.asyncio
async def test_an_overrunning_scan_buys_a_gap_before_the_next_one():
    """Backpressure: a scan that outlasts its interval spends at most half the
    time scanning. The gap is measured from the last run, because the script's
    runtime grows with the fleet and a hand-chosen interval goes stale."""
    clock = {"t": 1000.0}
    runs = []

    def board():
        runs.append(clock["t"])
        clock["t"] += 12.0          # a 12s scan, as measured on dev-vm-1
        return SNAP

    app = _app(board, now=lambda: clock["t"])
    async with app.run_test(size=(120, 34)) as pilot:
        await _settle(pilot)
        assert len(runs) == 1
        app._poll_board()           # t=1012, gap runs to t=1024
        await _settle(pilot)
        assert len(runs) == 1, "the next tick ran inside the gap"
        clock["t"] = 1023.9
        app._poll_board()
        await _settle(pilot)
        assert len(runs) == 1, "the gap ended early"
        clock["t"] = 1024.1
        app._poll_board()
        await _settle(pilot)
        assert len(runs) == 2, "the gap never ended"


@pytest.mark.asyncio
async def test_a_quick_scan_buys_no_gap_at_all():
    """The gap prices an OVERRUN. A healthy box must poll exactly as before —
    a fix that slowed every panel down would be a second defect."""
    clock = {"t": 1000.0}
    runs = []

    def board():
        runs.append(clock["t"])
        clock["t"] += 0.2
        return SNAP

    app = _app(board, now=lambda: clock["t"])
    async with app.run_test(size=(120, 34)) as pilot:
        await _settle(pilot)
        for _ in range(4):
            clock["t"] += 3.0       # the ordinary 3s tick
            app._poll_board()
            await _settle(pilot)
        assert len(runs) == 5, f"a fast board was throttled: {runs}"


@pytest.mark.asyncio
async def test_reload_overrides_the_gap_but_never_the_in_flight_guard():
    """A hand on a key is not a timer hammering a slow scan. Reload must run;
    it still must not start a second copy of a scan already going."""
    clock = {"t": 1000.0}
    runs = []

    def board():
        runs.append(clock["t"])
        clock["t"] += 12.0
        return SNAP

    app = _app(board, now=lambda: clock["t"])
    async with app.run_test(size=(120, 34)) as pilot:
        await _settle(pilot)
        assert len(runs) == 1
        await app.action_reload()   # inside the gap, and it runs anyway
        await _settle(pilot)
        assert len(runs) == 2, "Reload did nothing and said 'reloaded'"

    release = threading.Event()
    started = threading.Semaphore(0)
    blocking = []

    def slow_board():
        blocking.append(1)
        started.release()
        release.wait(10)
        return SNAP

    app2 = _app(slow_board, now=lambda: 1000.0)
    async with app2.run_test(size=(120, 34)) as pilot:
        assert started.acquire(timeout=10)
        await app2.action_reload()
        await pilot.pause()
        assert blocking == [1], "Reload started a second concurrent scan"
        release.set()
        await _settle(pilot)
    release.set()
