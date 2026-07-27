"""URL-identity auto-rebind package (2026-07-27): ephemeral URL stripping,
the .mcp.json rollout command, the verify-when-bound attribution gate, and
the coverage-gap notice.

The full sweep (transport → session → bind with no agent turn) needs a live
streamable-http stack and is exercised in production; these tests pin the
component contracts the sweep composes: identity comes off the URL only for
sessions that could legitimately own it, a bound session cannot write the
record under another name, and a closed gap surfaces exactly once.
"""

import argparse
import json
from pathlib import Path

import pytest

from mcp_hub.cli import _ephemeral_hub_url, rebind_url_command
from mcp_hub.server import create_server

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


class _FakeSess:
    _write_stream = object()

    async def send_ping(self):
        return None


class _FakeCtx:
    def __init__(self, session):
        self.session = session


async def _call_tool(server, name: str, args: dict) -> str:
    result = await server._tool_manager.call_tool(name, args)
    if hasattr(result, "content"):
        for block in result.content:
            if hasattr(block, "text"):
                return block.text
    if isinstance(result, list):
        for block in result:
            if hasattr(block, "text"):
                return block.text
    if isinstance(result, str):
        return result
    return str(result)


# ---------------------------------------------------------------------------
# Ephemeral URL stripping — the cli-side half of the two-layer defence
# ---------------------------------------------------------------------------


def test_ephemeral_url_strips_agent_param():
    url = "http://hub:8090/mcp?agent=mcp-hub-dev-vm-1"
    assert "agent=" not in _ephemeral_hub_url(url)
    assert _ephemeral_hub_url(url).startswith("http://hub:8090/mcp")


def test_ephemeral_url_preserves_other_params_and_plain_urls():
    url = "http://hub:8090/mcp?agent=x&keep=1"
    out = _ephemeral_hub_url(url)
    assert "keep=1" in out and "agent=" not in out
    plain = "http://hub:8090/mcp"
    assert _ephemeral_hub_url(plain) == plain


# ---------------------------------------------------------------------------
# rebind-url — the per-seat rollout command
# ---------------------------------------------------------------------------


def _stamp(tmp_path: Path, monkeypatch, url: str, dry_run: bool = False) -> int:
    monkeypatch.setattr(
        "mcp_hub.cli._derive_agent_identity",
        lambda cwd: ("mcp-hub-test-host", "org/mcp-hub"),
    )
    mcp_json = tmp_path / ".mcp.json"
    mcp_json.write_text(json.dumps({"mcpServers": {"hub": {"url": url}}}))
    args = argparse.Namespace(cwd=str(tmp_path), dry_run=dry_run)
    return rebind_url_command(args)


def test_rebind_url_stamps_identity(tmp_path, monkeypatch):
    assert _stamp(tmp_path, monkeypatch, "http://hub:8090/mcp") == 0
    data = json.loads((tmp_path / ".mcp.json").read_text())
    assert data["mcpServers"]["hub"]["url"].endswith("?agent=mcp-hub-test-host")


def test_rebind_url_is_idempotent_and_replaces_stale_identity(tmp_path, monkeypatch):
    assert _stamp(tmp_path, monkeypatch, "http://h/mcp?agent=old-name") == 0
    url = json.loads((tmp_path / ".mcp.json").read_text())["mcpServers"]["hub"]["url"]
    assert url.count("agent=") == 1
    assert "old-name" not in url


def test_rebind_url_dry_run_writes_nothing(tmp_path, monkeypatch):
    assert _stamp(tmp_path, monkeypatch, "http://h/mcp", dry_run=True) == 0
    url = json.loads((tmp_path / ".mcp.json").read_text())["mcpServers"]["hub"]["url"]
    assert "agent=" not in url


def test_rebind_url_refuses_the_shared_global_scope(tmp_path, monkeypatch):
    """The user-global mcpServers.hub is shared by EVERY seat on the box —
    stamping one seat's identity into it made every reconnecting agent on
    dev-vm-1 announce itself as that seat (the 2026-07-27 cross-delivery
    incident: dt received the hub maintainer's DMs within the hour). The
    global scope must never be a stamping target, even when it is the only
    hub config present."""
    home = tmp_path / "home"
    home.mkdir()
    work = tmp_path / "repo"
    work.mkdir()
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    monkeypatch.setattr(
        "mcp_hub.cli._derive_agent_identity",
        lambda cwd: ("mcp-hub-test-host", "org/mcp-hub"),
    )
    (home / ".claude.json").write_text(json.dumps({
        "someOtherSetting": {"keep": "me"},
        "mcpServers": {"hub": {"url": "http://h/mcp"}},
    }))
    rc = rebind_url_command(argparse.Namespace(cwd=str(work), dry_run=False))
    assert rc == 1
    data = json.loads((home / ".claude.json").read_text())
    assert "agent=" not in data["mcpServers"]["hub"]["url"]  # untouched
    assert data["someOtherSetting"] == {"keep": "me"}


def test_rebind_url_stamps_per_project_override(tmp_path, monkeypatch):
    """~/.claude.json's per-project override denotes ONE seat (keyed by cwd)
    — that scope is safe to stamp even though the file is user-level."""
    home = tmp_path / "home"
    home.mkdir()
    work = tmp_path / "repo"
    work.mkdir()
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    monkeypatch.setattr(
        "mcp_hub.cli._derive_agent_identity",
        lambda cwd: ("mcp-hub-test-host", "org/mcp-hub"),
    )
    (home / ".claude.json").write_text(json.dumps({
        "mcpServers": {"hub": {"url": "http://global/mcp"}},
        "projects": {str(work): {"mcpServers": {"hub": {"url": "http://mine/mcp"}}}},
    }))
    rc = rebind_url_command(argparse.Namespace(cwd=str(work), dry_run=False))
    assert rc == 0
    data = json.loads((home / ".claude.json").read_text())
    assert "agent=mcp-hub-test-host" in (
        data["projects"][str(work)]["mcpServers"]["hub"]["url"]
    )
    assert "agent=" not in data["mcpServers"]["hub"]["url"]  # global untouched


def test_rebind_url_prefers_repo_scope_over_user_scope(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    work = tmp_path / "repo"
    work.mkdir()
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    monkeypatch.setattr(
        "mcp_hub.cli._derive_agent_identity",
        lambda cwd: ("mcp-hub-test-host", "org/mcp-hub"),
    )
    (home / ".claude.json").write_text(json.dumps(
        {"mcpServers": {"hub": {"url": "http://user-scope/mcp"}}}))
    (work / ".mcp.json").write_text(json.dumps(
        {"mcpServers": {"hub": {"url": "http://repo-scope/mcp"}}}))
    assert rebind_url_command(argparse.Namespace(cwd=str(work), dry_run=False)) == 0
    assert "agent=" in json.loads((work / ".mcp.json").read_text())[
        "mcpServers"]["hub"]["url"]
    assert "agent=" not in json.loads((home / ".claude.json").read_text())[
        "mcpServers"]["hub"]["url"]


# ---------------------------------------------------------------------------
# Verify-when-bound — the attribution gate (item 34, fo's specimen)
# ---------------------------------------------------------------------------


def test_attribution_unbound_session_is_asserted_and_passes(tmp_path):
    server = create_server(db_path=tmp_path / "t.db")
    gate = server._hub_attribution
    grade, err = gate(_FakeCtx(_FakeSess()), "anyone")
    assert grade == "asserted" and err == ""
    grade, err = gate(None, "anyone")  # no ctx at all (direct calls)
    assert grade == "asserted" and err == ""


def test_attribution_bound_session_verified_for_own_name(tmp_path):
    server = create_server(db_path=tmp_path / "t.db")
    sess = _FakeSess()
    server._hub_registry.bind("alice", sess)
    grade, err = server._hub_attribution(_FakeCtx(sess), "alice")
    assert grade == "session-verified" and err == ""


def test_attribution_bound_session_refused_for_other_name(tmp_path):
    """fo's 2026-07-27 specimen: a session bound to its own identity asserted
    a peer's name (inverted from/to) and wrote the record under it. The gate
    refuses at the tool boundary."""
    server = create_server(db_path=tmp_path / "t.db")
    sess = _FakeSess()
    server._hub_registry.bind("factory-operations", sess)
    grade, err = server._hub_attribution(_FakeCtx(sess), "factory-data-model")
    assert "REFUSED" in err
    assert "factory-operations" in err  # names the identity it holds


async def test_send_stamps_attribution_asserted_without_ctx(tmp_path):
    """Ephemeral-style calls (no ctx) keep working and grade 'asserted'."""
    server = create_server(db_path=tmp_path / "t.db")
    await _call_tool(server, "register", {"name": "alice", "project": "p"})
    await _call_tool(server, "register", {"name": "bob", "project": "p"})
    out = await _call_tool(
        server, "send", {"from_agent": "alice", "to": "bob", "message": "hi"},
    )
    assert "sent" in out.lower() or "queued" in out.lower()
    import sqlite3
    conn = sqlite3.connect(tmp_path / "t.db")
    row = conn.execute(
        "SELECT attribution FROM messages WHERE to_agent='bob'"
    ).fetchone()
    assert row[0] == "asserted"


async def test_broadcast_stamps_attribution_too(tmp_path):
    """send() had this covered; broadcast() did not — found by mutation while
    merging broadcast scoping. Dropping `attribution` from broadcast's INSERT
    left all 587 tests green, so the column could have been lost in a merge
    without anything saying so. broadcast() writes the SAME row type as send()
    and its INSERT now carries two independently-added columns, which is
    exactly the shape that loses one quietly."""
    server = create_server(db_path=tmp_path / "t.db")
    await _call_tool(server, "register", {"name": "alice", "project": "p"})
    await _call_tool(server, "broadcast", {"from_agent": "alice", "message": "hi"})
    import sqlite3

    from mcp_hub.server import _BROADCAST_CHANNEL
    conn = sqlite3.connect(tmp_path / "t.db")
    row = conn.execute(
        "SELECT attribution FROM messages WHERE from_agent='alice' "
        "AND channel=?", (_BROADCAST_CHANNEL,),
    ).fetchone()
    assert row is not None, "broadcast wrote no row"
    assert row[0] == "asserted"


async def test_post_stamps_attribution_too(tmp_path):
    """Second member of the same exposure class as broadcast() — found by dev
    running the generalisation, not by reading. Dropping `attribution` from
    post()'s INSERT left the suite green."""
    server = create_server(db_path=tmp_path / "t.db")
    await _call_tool(server, "register", {"name": "alice", "project": "p"})
    await _call_tool(
        server, "create_channel", {"name": "topic", "created_by": "alice"},
    )
    out = await _call_tool(
        server, "post",
        {"from_agent": "alice", "channel": "topic", "message": "hi"},
    )
    assert "not found" not in out, out  # the fixture actually posted
    import sqlite3
    conn = sqlite3.connect(tmp_path / "t.db")
    row = conn.execute(
        "SELECT attribution FROM messages WHERE channel='topic'"
    ).fetchone()
    assert row is not None, "post wrote no row"
    assert row[0] == "asserted"


_ATTR_CARD = (
    "**DECISION**\n"
    "**ASK:** approve the widget rebuild\n"
    "**WHY:** the current one is broken\n"
    "**VALUE:** dashboards work again [7/10]\n"
    "**RISK:** an hour lost if wrong [3/10]\n"
)


async def test_decision_put_stamps_attribution_on_fresh_insert(tmp_path):
    """Third member of the class. decision_put has TWO writers and both stamp;
    this covers the fresh INSERT."""
    server = create_server(db_path=tmp_path / "t.db")
    await _call_tool(server, "register", {"name": "alice", "project": "p"})
    out = await _call_tool(
        server, "decision_put", {"from_agent": "alice", "card": _ATTR_CARD},
    )
    assert "opened" in out, out  # fresh INSERT path, not the update path
    import sqlite3
    conn = sqlite3.connect(tmp_path / "t.db")
    row = conn.execute("SELECT attribution FROM decisions").fetchone()
    assert row is not None, "decision_put wrote no row"
    assert row[0] == "asserted"


async def test_decision_put_stamps_attribution_on_the_update_path(tmp_path):
    """The update-in-place path stamps too, and the obvious test for it is
    VACUOUS: call_tool cannot inject a Context, so both writes grade
    'asserted', and a dropped column on the UPDATE would simply leave the
    INSERT's identical value behind — passing while testing nothing.

    So the stored value is first replaced with a sentinel. If the UPDATE stops
    writing attribution, the sentinel survives and this fails."""
    server = create_server(db_path=tmp_path / "t.db")
    await _call_tool(server, "register", {"name": "alice", "project": "p"})
    await _call_tool(
        server, "decision_put", {"from_agent": "alice", "card": _ATTR_CARD},
    )
    import sqlite3
    conn = sqlite3.connect(tmp_path / "t.db")
    conn.execute("UPDATE decisions SET attribution = 'SENTINEL'")
    conn.commit()
    conn.close()

    # Same ask, different score — token overlap keeps it on the UPDATE path
    # rather than superseding into a second row.
    out = await _call_tool(
        server, "decision_put",
        {"from_agent": "alice", "card": _ATTR_CARD.replace("[7/10]", "[9/10]")},
    )
    assert "updated" in out, out  # precondition: the UPDATE path really ran

    conn = sqlite3.connect(tmp_path / "t.db")
    rows = conn.execute("SELECT attribution FROM decisions").fetchall()
    assert len(rows) == 1, f"expected one row (update, not supersede): {rows}"
    assert rows[0][0] == "asserted", "the UPDATE left the sentinel in place"


# ---------------------------------------------------------------------------
# Coverage-gap notice — one-shot awareness of non-delivery
# ---------------------------------------------------------------------------


async def test_gap_notice_surfaces_once_then_clears(tmp_path):
    server = create_server(db_path=tmp_path / "t.db")
    await _call_tool(server, "register", {"name": "alice", "project": "p"})
    await _call_tool(server, "register", {"name": "bob", "project": "p"})
    import sqlite3
    import time as _t
    conn = sqlite3.connect(tmp_path / "t.db")
    # Simulate a binding death 60s ago, with traffic arriving during the gap.
    conn.execute(
        "UPDATE agents SET status='offline', offline_since=? WHERE name='alice'",
        (_t.time() - 60,),
    )
    conn.commit()
    conn.close()
    await _call_tool(server, "send", {"from_agent": "bob", "to": "alice",
                                      "message": "arrived during the gap"})
    # Coming back online closes the gap and queues the notice…
    await _call_tool(server, "register", {"name": "alice", "project": "p"})
    inbox = await _call_tool(server, "get_messages", {"agent_name": "alice"})
    assert "Coverage gap" in inbox
    assert "arrived during the gap" in inbox  # the queue itself still drains
    # …exactly once.
    again = await _call_tool(server, "get_messages", {"agent_name": "alice"})
    assert "Coverage gap" not in again


async def test_gap_notice_ignores_the_agents_own_traffic(tmp_path):
    """You cannot miss your own messages. Found by the notice's first live
    firing (2026-07-27): it reported one missed message to the seat that
    shipped it — that seat's own deploy broadcast, sent during its gap."""
    server = create_server(db_path=tmp_path / "t.db")
    await _call_tool(server, "register", {"name": "alice", "project": "p"})
    import sqlite3
    import time as _t
    conn = sqlite3.connect(tmp_path / "t.db")
    conn.execute(
        "UPDATE agents SET status='offline', offline_since=? WHERE name='alice'",
        (_t.time() - 60,),
    )
    conn.commit()
    conn.close()
    await _call_tool(server, "broadcast",
                     {"from_agent": "alice", "message": "my own announcement"})
    await _call_tool(server, "register", {"name": "alice", "project": "p"})
    assert "Coverage gap" not in await _call_tool(
        server, "get_messages", {"agent_name": "alice"}
    )


async def test_no_gap_no_notice(tmp_path):
    server = create_server(db_path=tmp_path / "t.db")
    await _call_tool(server, "register", {"name": "alice", "project": "p"})
    await _call_tool(server, "register", {"name": "alice", "project": "p"})
    assert await _call_tool(server, "get_messages", {"agent_name": "alice"}) == ""


async def test_unregister_clears_gap_state(tmp_path):
    """Deliberate departure: no coverage gap owed on return."""
    server = create_server(db_path=tmp_path / "t.db")
    await _call_tool(server, "register", {"name": "alice", "project": "p"})
    import sqlite3
    import time as _t
    conn = sqlite3.connect(tmp_path / "t.db")
    conn.execute(
        "UPDATE agents SET offline_since=? WHERE name='alice'", (_t.time() - 60,)
    )
    conn.commit()
    conn.close()
    await _call_tool(server, "unregister", {"name": "alice"})
    await _call_tool(server, "register", {"name": "alice", "project": "p"})
    assert await _call_tool(server, "get_messages", {"agent_name": "alice"}) == ""
