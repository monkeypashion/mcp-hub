"""v2-fleet r17 (F53, T68; j6-unread-ids-for-the-doorbell): a lane's unread
messages can be counted, with their priority, without reading or marking
anything.

The lane doorbell needs "anything new for this lane, and how urgent?" many
times a minute. get_messages is the only unread read today, and it marks what
it returns read and touches the caller's session; get_history renders no
priority. THE RULE: a new hub tool unread_ids(agent_name) answers with the ids
and priorities of the agent's unread DMs, oldest first, and their count, and
nothing else; it writes nothing and binds nothing, and its gate is
get_messages' own (_reader_refusal).

The answer is one JSON object: {"count": <n>, "unread": [{"id": <int>,
"priority": "low"|"normal"|"urgent"}, ...]}.

Hermetic: the hub's own tools in-process on a temp DB; nothing opens a socket.
FALSIFIERS (red on the parent, which has no such tool): D1 to D5. GUARD: (6)
is the carried drain and key tests in the same test command.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from mcp_hub.server import create_server

KEY_HEADER = "x-mcp-hub-agent-key"
MODE_ENV = "MCP_HUB_AGENT_KEYS"
KEY_RE = re.compile(r"AGENT-KEY: ([0-9a-f]{64})")

SENDER = "zz-sender-r17"
MARKS = {
    "normal": "MARK-r17-normal-7f3a",
    "low": "MARK-r17-low-91c2",
    "urgent": "MARK-r17-urgent-c04e",
}
Y_MARK = "MARK-r17-y-5d11"
BCAST_MARK = "MARK-r17-broadcast-3b70"
POST_MARK = "MARK-r17-post-a8e6"


@pytest.fixture(autouse=True)
def _keys_on(monkeypatch):
    monkeypatch.setenv(MODE_ENV, "observe")
    monkeypatch.delenv("MCP_HUB_OPERATOR_TOKEN", raising=False)


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "hub.db"


@pytest.fixture
def server(db: Path):
    return create_server(db_path=db)


class _Sess:
    _write_stream = object()

    def __init__(self, client_name: str = "") -> None:
        self.sent: list[object] = []
        if client_name:
            self.client_params = SimpleNamespace(
                clientInfo=SimpleNamespace(name=client_name))

    async def send_ping(self):
        return None

    async def send_notification(self, notification):
        self.sent.append(notification)


class _Req:
    def __init__(self, headers: dict[str, str]):
        self.headers = {k.lower(): v for k, v in headers.items()}
        self.client = SimpleNamespace(host="100.64.0.9", port=1)


class _Ctx:
    """An HTTP request's context: a transport session plus its headers."""

    def __init__(self, session=None, headers: dict[str, str] | None = None):
        self.session = session or _Sess()
        self.request_context = SimpleNamespace(request=_Req(headers or {}))


def _text(result) -> str:
    if isinstance(result, str):
        return result
    for block in getattr(result, "content", None) or result:
        if hasattr(block, "text"):
            return block.text
    return str(result)


async def _call(server, name, args, ctx=None) -> str:
    return _text(await server._tool_manager.call_tool(name, args, context=ctx))


async def _unread(server, name, ctx=None) -> str:
    """unread_ids' raw answer; a hub without the tool answers "" (so every
    case below is red by assertion on the parent, never by an error)."""
    try:
        return await _call(server, "unread_ids", {"agent_name": name}, ctx)
    except Exception as exc:  # noqa: BLE001 - ToolError("Unknown tool: ...")
        if "Unknown tool" in str(exc):
            return ""
        raise


def _parsed(raw: str) -> dict | None:
    try:
        out = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return out if isinstance(out, dict) else None


def _conn(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    return conn


def _ids(db: Path, to: str) -> list[int]:
    return [r[0] for r in _conn(db).execute(
        "SELECT id FROM messages WHERE to_agent=? ORDER BY ts, id", (to,))]


def _id_of(db: Path, body: str) -> int:
    row = _conn(db).execute("SELECT id FROM messages WHERE body=?", (body,)).fetchone()
    assert row is not None, f"setup: no row for {body!r}"
    return row[0]


async def _setup(server, db: Path):
    """Lanes x and y, each with its own interactive session; three DMs to x
    (normal, low, urgent, in that order) and one to y; also a normal broadcast
    to x's squad and a normal post to a channel x subscribes to, which are not
    DMs and must never be counted."""
    sx, sy = _Sess("claude-code"), _Sess("claude-code")
    await _call(server, "register", {"name": "x", "project": "p", "squads": "sq"},
                _Ctx(sx))
    await _call(server, "register", {"name": "y", "project": "p"}, _Ctx(sy))
    await _call(server, "register", {"name": SENDER, "project": "p", "squads": "sq"})
    for prio in ("normal", "low", "urgent"):
        await _call(server, "send", {"from_agent": SENDER, "to": "x",
                                     "message": MARKS[prio], "priority": prio})
    await _call(server, "send", {"from_agent": SENDER, "to": "y",
                                 "message": Y_MARK})
    await _call(server, "create_channel", {"name": "r17-chan", "created_by": SENDER})
    await _call(server, "subscribe_channel", {"name": "x", "channel": "r17-chan"})
    await _call(server, "broadcast", {"from_agent": SENDER, "message": BCAST_MARK,
                                      "priority": "normal"})
    await _call(server, "post", {"from_agent": SENDER, "channel": "r17-chan",
                                 "message": POST_MARK, "priority": "normal"})
    return sx, sy


def _snapshot(server, db: Path) -> dict:
    """Every row of every table but agent_key_events, and the registry."""
    conn = _conn(db)
    names = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    tables = {t: sorted(repr(tuple(r)) for r in conn.execute(f'SELECT * FROM "{t}"'))
              for t in names if t != "agent_key_events"}
    reg = server._hub_registry
    tables["registry"] = sorted(
        (n, id(reg.get(n)), reg.session_count(n), reg.generation(n),
         reg.has_pending_wake_ack(n))
        for n in reg.names())
    return tables


# =============================================================================
# D1 IDS AND PRIORITY ONLY
# =============================================================================


async def test_d1_ids_and_priority_only(server, db):
    await _setup(server, db)
    raw = await _unread(server, "x", _Ctx())
    out = _parsed(raw)
    assert out is not None, f"unread_ids must answer one JSON object: {raw!r}"
    assert out.get("count") == 3, out
    assert [(u.get("id"), u.get("priority")) for u in out.get("unread", [])] == list(
        zip(_ids(db, "x"), ("normal", "low", "urgent"))), out
    others = {_id_of(db, BCAST_MARK), _id_of(db, POST_MARK)}
    assert not others & {u.get("id") for u in out.get("unread", [])}, (
        f"a broadcast or channel post was counted: {out}")
    for marker in (*MARKS.values(), Y_MARK, BCAST_MARK, POST_MARK):
        assert marker not in raw, f"no message text may be returned: {raw!r}"
    assert SENDER not in raw, f"no sender may be returned: {raw!r}"
    for (ts,) in _conn(db).execute("SELECT ts FROM messages"):
        assert str(int(ts)) not in raw, f"no timestamp may be returned: {raw!r}"
    assert not re.search(r"\d\d:\d\d:\d\d", raw), raw


# =============================================================================
# D2 IT CHANGES NOTHING
# =============================================================================


def _key_counts(db: Path) -> dict:
    """agent_key_events as (name, verdict) -> summed count: the gate's own
    count. The action column is the gate's name for the call (get_messages'
    gate says "read", get_history's "history"), so it is not compared."""
    out: dict = {}
    for r in _conn(db).execute("SELECT name, verdict, count FROM agent_key_events"):
        out[(r[0], r[1])] = out.get((r[0], r[1]), 0) + r[2]
    return out


def _delta(after: dict, before: dict) -> dict:
    return {k: after.get(k, 0) - before.get(k, 0)
            for k in set(after) | set(before) if after.get(k, 0) != before.get(k, 0)}


async def _d2(server, db, ctx_factory, *, expect_key_rows: bool):
    rows = _conn(db).execute(
        "SELECT COUNT(*) FROM messages WHERE to_agent='x' AND read=0").fetchone()[0]
    before, keys0 = _snapshot(server, db), _key_counts(db)
    raw = await _unread(server, "x", ctx_factory())
    assert _parsed(raw) and _parsed(raw).get("count") == rows, raw
    assert _snapshot(server, db) == before, "unread_ids changed a row or the registry"
    keys1 = _key_counts(db)
    await _call(server, "get_history", {"agent_or_channel": "x"}, ctx_factory())
    keys2 = _key_counts(db)
    assert _delta(keys1, keys0) == _delta(keys2, keys1), (
        "unread_ids' key events differ from the gate's own count",
        _delta(keys1, keys0), _delta(keys2, keys1))
    assert bool(_delta(keys1, keys0)) is expect_key_rows, _delta(keys1, keys0)
    drained = await _call(server, "get_messages", {"agent_name": "x", "bind": False})
    for prio in ("normal", "low", "urgent"):
        assert MARKS[prio] in drained, (prio, drained)


async def test_d2_it_changes_nothing(server, db):
    await _setup(server, db)
    await _d2(server, db, _Ctx, expect_key_rows=False)


async def test_d2_a_keyed_name_changes_only_the_gates_own_count(server, db):
    await _setup(server, db)
    out = await _call(server, "claim_agent_key", {"name": "x"}, _Ctx())
    key = KEY_RE.search(out).group(1)
    await _call(server, "register", {"name": "x", "project": "p", "squads": "sq"},
                _Ctx(_Sess("claude-code"), {KEY_HEADER: key}))
    # Observe: a caller with no key is let through and counted as would-refuse.
    await _d2(server, db, _Ctx, expect_key_rows=True)


# =============================================================================
# D3 NO BINDING
# =============================================================================


async def test_d3_no_binding(server, db):
    sx, _sy = await _setup(server, db)
    reg = server._hub_registry
    caller = _Sess("claude-code")   # an interactive session bound to nothing
    raw = await _unread(server, "x", _Ctx(caller))
    assert _parsed(raw) and _parsed(raw).get("count") == 3, raw
    assert reg.names_for_session(caller) == set(), "the caller's session was bound"
    assert reg.get("x") is sx, "x's binding moved"


# =============================================================================
# D4 THE GATE IS GET_MESSAGES'
# =============================================================================


async def test_d4_a_session_bound_to_another_name_is_refused(server, db):
    _sx, sy = await _setup(server, db)
    raw = await _unread(server, "x", _Ctx(sy))
    assert raw.startswith("REFUSED") and "mismatch" in raw, raw
    assert _parsed(raw) is None, raw
    assert str(_ids(db, "x")[0]) not in raw, raw
    unread = _conn(db).execute(
        "SELECT COUNT(*) FROM messages WHERE to_agent='x' AND read=0").fetchone()[0]
    assert unread == 3, "a refused read must mark nothing"


async def test_d4_an_armed_key_is_checked_as_get_messages_checks_it(
        server, db, monkeypatch):
    await _setup(server, db)
    out = await _call(server, "claim_agent_key", {"name": "x"}, _Ctx())
    key = KEY_RE.search(out).group(1)
    # Arm: x's interactive session registers presenting the key.
    await _call(server, "register", {"name": "x", "project": "p"},
                _Ctx(_Sess("claude-code"), {KEY_HEADER: key}))
    monkeypatch.setenv(MODE_ENV, "enforce")
    waiting = _conn(db).execute(
        "SELECT COUNT(*) FROM messages WHERE to_agent='x' AND read=0").fetchone()[0]
    good = await _unread(server, "x", _Ctx(headers={KEY_HEADER: key}))
    assert _parsed(good) and _parsed(good).get("count") == waiting >= 3, good
    bad = await _unread(server, "x", _Ctx(headers={KEY_HEADER: "0" * 64}))
    assert bad.startswith("REFUSED") and _parsed(bad) is None, bad
    drained = await _call(server, "get_messages", {"agent_name": "x", "bind": False},
                          _Ctx(headers={KEY_HEADER: "0" * 64}))
    assert drained.startswith("REFUSED"), "get_messages' own gate changed"


# =============================================================================
# D5 DRAINED ROWS DROP OUT
# =============================================================================


async def test_d5_drained_rows_drop_out(server, db):
    await _setup(server, db)
    first = _parsed(await _unread(server, "x", _Ctx()))
    assert first and first.get("count") == 3, first
    await _call(server, "get_messages", {"agent_name": "x", "bind": False})
    empty = _parsed(await _unread(server, "x", _Ctx()))
    assert empty == {"count": 0, "unread": []}, empty
    await _call(server, "send", {"from_agent": SENDER, "to": "x",
                                 "message": "later", "priority": "low"})
    later = _parsed(await _unread(server, "x", _Ctx()))
    assert later and later.get("count") == 1, later
    assert [(u.get("id"), u.get("priority")) for u in later["unread"]] == [
        (_ids(db, "x")[-1], "low")], later
