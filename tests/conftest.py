"""Shared hermeticity guards — hoisted from file-local fixtures (Wave 1).

Both guards below existed as file-local fixtures after real incidents, which
meant every NEW test file had to remember to re-declare them — and none did.
A guard that must be copied into each file is a guard that is absent from the
next file. Hoisting is Wave 1 verification infrastructure (see
docs/verification/wave-1.md).

The originals are deliberately left in place where they carry incident
history in their docstrings (tests/test_cli.py:45, tests/test_doorbell.py:37);
double-applying either guard is harmless.
"""

import pytest


@pytest.fixture(autouse=True)
def _hermetic_state_and_daemons(tmp_path, monkeypatch):
    """No test may touch the real ~/.mcp-hub or spawn a real detached daemon.

    History: 2026-07-18, integration tests orphaned real 'alice' and
    'ghost-agent' heartbeat daemons into the developer's home dir, one
    retrying http://nowhere.invalid/mcp forever. The guard lived only in
    tests/test_cli.py; any other file driving stop_hook_command was exposed.

    Tests that exercise the spawn path call the original function via direct
    import (see test_spawn_daemon_detached_*), so the module-attribute no-op
    here does not blind them.
    """
    monkeypatch.setenv("MCP_HUB_STATE_DIR", str(tmp_path / "mcp-hub-state"))
    monkeypatch.setattr(
        "mcp_hub.cli._spawn_daemon_detached", lambda *_a, **_k: None,
        raising=False,
    )


@pytest.fixture(autouse=True)
def _no_watcher_leak():
    """api_v1._watchers is module-level state: never leak between tests.

    History: the doorbell suite cleared it file-locally (before AND after,
    tests/test_doorbell.py:37-44); any other file that touched the watch
    route inherited the previous test's watchers silently.
    """
    from mcp_hub import api_v1

    api_v1._watchers.clear()
    yield
    api_v1._watchers.clear()
