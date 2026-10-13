"""MH-H2 (audit 2026-10-13): the Stop hook never throws away drained text.

get_messages marks the rows read on the hub before the hook prints them. A
failure in any LATER leg — the broadcast drain, list_agents, or the client
context's own exit during a hub restart — used to raise past the text, so
the caller printed nothing and the messages were read and never shown.
"""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace

import mcp
import mcp.client.streamable_http as streamable_http
import pytest

from mcp_hub import cli

DM = "📬 [10:00] **bob** → lane-a ⟨hub.msg/1?id=1⟩: please look"


def _result(text):
    return SimpleNamespace(content=[SimpleNamespace(text=text)])


def _install(monkeypatch, *, fail=(), exit_raises=False):
    calls = []

    class _Session:
        def __init__(self, *_a, **_k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def initialize(self):
            return None

        async def call_tool(self, name, args):
            calls.append(name)
            if name in fail:
                raise RuntimeError(f"{name} failed mid-hook")
            if name == "get_messages":
                return _result(DM)
            if name == "list_agents":
                return _result("🟢 **lane-a**")
            return _result("")

    @contextlib.asynccontextmanager
    async def _client(*_a, **_k):
        yield (None, None, None)
        if exit_raises:
            raise RuntimeError("the hub went away during the session DELETE")

    monkeypatch.setattr(mcp, "ClientSession", _Session)
    monkeypatch.setattr(streamable_http, "streamablehttp_client", _client)
    return calls


def _query():
    return asyncio.run(cli._query_hub("http://hub.invalid/mcp", "lane-a"))


def test_a_list_agents_failure_keeps_the_drained_dm(monkeypatch):
    _install(monkeypatch, fail=("list_agents",))
    dms, _bcs, online, _card = _query()
    assert dms == DM
    assert online is True, "an unanswered list_agents must not read as offline"


def test_a_broadcast_failure_keeps_the_drained_dm(monkeypatch):
    _install(monkeypatch, fail=("get_broadcasts_for_agent",))
    dms, bcs, _online, _card = _query()
    assert dms == DM
    assert bcs == ""


def test_a_failing_context_exit_keeps_the_drained_dm(monkeypatch):
    _install(monkeypatch, exit_raises=True)
    dms, _bcs, _online, _card = _query()
    assert dms == DM


def test_a_failure_before_the_drain_still_raises(monkeypatch):
    """Nothing was consumed, so the caller's fail-open path is right."""
    _install(monkeypatch, fail=("get_messages",))
    with pytest.raises(RuntimeError):
        _query()


def test_the_happy_path_is_unchanged(monkeypatch):
    calls = _install(monkeypatch)
    dms, _bcs, online, _card = _query()
    assert dms == DM and online is True
    assert calls == ["get_messages", "get_broadcasts_for_agent", "list_agents"]
