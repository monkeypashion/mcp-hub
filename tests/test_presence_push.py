"""Gate for the board's presence push — the `open now` column's only producer.

`workspace_open` shipped as a RECEIVER with no sender: the endpoint parsed it,
the registry stored it, tests exercised it, and no code anywhere ever sent one.
The column was therefore blank in the whole fleet, permanently, while looking
like a working feature. These tests pin the sender, and one of them pins that
it does NOT fire when there is nothing legitimate to report.
"""

from __future__ import annotations

import pytest

from mcp_hub.settings_app import SettingsApp

AGENTS = [{"agent": "alpha", "worktree": "/a", "klass": "squad"}]

MODEL = {"agent": "x", "sections": []}


def _app(scoped_to, ping=None, presence_seconds=3600.0):
    return SettingsApp(
        AGENTS, scoped_to=scoped_to, model_for=lambda cwd: MODEL,
        squad_bin="/usr/bin/SQUAD", hub_bin="/usr/bin/HUB",
        board_for=None, poll_seconds=3600,
        presence_ping=ping, presence_seconds=presence_seconds,
    )


@pytest.mark.asyncio
async def test_a_scoped_board_claims_its_workspace_immediately():
    """First paint, not first interval — a board opened and watched for 30s
    must already appear open, or the column tells the operator nothing."""
    sent = []
    app = _app("/home/me/Projects/squad.code-workspace", ping=sent.append)
    async with app.run_test():
        await app.workers.wait_for_complete()
    assert sent == ["/home/me/Projects/squad.code-workspace"]


@pytest.mark.asyncio
async def test_an_unscoped_board_reports_NOTHING():
    """`mcp-hub board` with no --workspace has no workspace open in front of
    anyone. Inventing one would plant a phantom row on every machine that
    ever ran a bare board — drift the operator can never clear."""
    sent = []
    app = _app(None, ping=sent.append)
    async with app.run_test():
        await app.workers.wait_for_complete()
    assert sent == []


@pytest.mark.asyncio
async def test_a_failing_hub_does_not_take_the_board_down():
    """The manager already states the hub problem in a full sentence at the
    top of the `w` view. A dashboard dying on a 60s timer is never right."""
    def boom(_path):
        raise OSError("connection refused")

    app = _app("/w/x.code-workspace", ping=boom)
    async with app.run_test():
        await app.workers.wait_for_complete()
        assert app.is_running
    assert app._presence_error is not None
    assert "connection refused" in app._presence_error


@pytest.mark.asyncio
async def test_a_recovered_ping_clears_the_remembered_error():
    calls = {"n": 0}

    def flaky(_path):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("down")

    app = _app("/w/x.code-workspace", ping=flaky)
    async with app.run_test():
        await app.workers.wait_for_complete()
        assert app._presence_error is not None
        app._push_presence()
        await app.workers.wait_for_complete()
    assert app._presence_error is None


@pytest.mark.asyncio
async def test_presence_is_disabled_when_no_reporter_is_injected():
    """Absent injection the board is exactly what it was — no network, and
    no attribute errors from a None call."""
    app = _app("/w/x.code-workspace", ping=None)
    async with app.run_test():
        await app.workers.wait_for_complete()
        assert app.is_running
    assert app._presence_error is None


def test_the_default_interval_sits_inside_the_hubs_open_now_window():
    """The two constants live in different files and must stay in proportion.

    Read the hub's window rather than restating it, so this tracks a change
    to either side: pinging at 60s against a 180s window means a workspace
    survives two consecutive dropped pings before it blinks out. Asserting
    the literal "180.0" would pass while the relationship broke.
    """
    import inspect
    import re

    from mcp_hub import api_v1

    m = re.search(r"OPEN_NOW_WINDOW\s*=\s*([\d.]+)", inspect.getsource(api_v1))
    assert m, "OPEN_NOW_WINDOW not found — the presence contract moved"
    window = float(m.group(1))

    from mcp_hub.cli import settings_command  # noqa: F401  (real default path)

    default = inspect.signature(SettingsApp.__init__).parameters[
        "presence_seconds"
    ].default
    assert default * 2 < window, (
        f"presence every {default}s cannot sustain a {window}s window"
    )
