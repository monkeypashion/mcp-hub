"""/health and hub_status must answer WHICH build is running and for HOW LONG.

The hub could not tell you which build it was — /health said commit "unknown"
in prod for two whole verification cycles (2026-07-27, 2026-08-07), so every
deploy check needed prod ssh, which exactly one seat has. The cause was an
env-resolution gap, not a missing feature: the Dockerfile ARG chain baked the
literal string "unknown" into MCP_HUB_GIT_SHA, while Coolify was injecting the
real sha as SOURCE_COMMIT all along (measured in the prod container's env).
"""
from __future__ import annotations

import time

from mcp_hub import server as server_mod
from mcp_hub.server import _resolve_commit


class TestResolveCommit:
    def test_source_commit_is_honoured(self, monkeypatch):
        """Coolify's runtime env var must resolve — this is the prod path."""
        monkeypatch.delenv("MCP_HUB_GIT_SHA", raising=False)
        monkeypatch.setenv("SOURCE_COMMIT", "abc123deadbeef")
        assert _resolve_commit() == "abc123deadbeef"

    def test_baked_literal_unknown_does_not_shadow_source_commit(
        self, monkeypatch
    ):
        """The exact prod failure: the ARG chain bakes the STRING "unknown"
        into MCP_HUB_GIT_SHA. That must fall through to SOURCE_COMMIT, not
        win the resolution."""
        monkeypatch.setenv("MCP_HUB_GIT_SHA", "unknown")
        monkeypatch.setenv("SOURCE_COMMIT", "abc123deadbeef")
        assert _resolve_commit() == "abc123deadbeef"

    def test_explicit_baked_sha_wins_over_source_commit(self, monkeypatch):
        """A deliberately baked sha is more specific than Coolify's ambient
        one, so it stays first in the chain."""
        monkeypatch.setenv("MCP_HUB_GIT_SHA", "baked00feed")
        monkeypatch.setenv("SOURCE_COMMIT", "abc123deadbeef")
        assert _resolve_commit() == "baked00feed"


class TestUptime:
    def test_process_start_is_captured_at_import(self):
        """_PROCESS_STARTED must predate now and be a real recent timestamp —
        a lazy or per-call capture would report uptime 0 forever, an
        instrument that cannot produce the discriminating reading."""
        started = server_mod._PROCESS_STARTED
        assert 0 < started <= time.time()
        # a test session is minutes old, not days: catches a stale constant
        assert time.time() - started < 24 * 3600
