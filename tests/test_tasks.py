"""Hub tasks — the build's own tests (v2-fleet r6, F17-F21).

The frozen falsifiers (tests/test_v2fleet_r6_falsifiers.py) pin the five
features' headline behaviour. These are the edges around them — the refusals,
the arithmetic and the two or three places where a plausible implementation
would be wrong in a way no headline test would notice:

- the `tasks` table has NO `result` column: a result is something the owner
  SAID at a moment under a grade, and a column loses both halves;
- a notice carries the task's REF and none of its text, so a forged title
  cannot ride a pointer into somebody's drain;
- squad membership is read NOW, so a joiner may claim a task made before they
  joined and a leaver may not claim one made while they were in;
- the event's grade is `_attribution`'s, which means a bound session records
  `session-verified` and an identity mismatch records nothing at all.

Hermetic: tmp DBs only, no network, no live hub. Tool calls go through
`ToolManager.call_tool`, with a fake Context where the grade is the subject
(the pattern in tests/test_operator_verification.py).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from mcp_hub import refs, tasks
from mcp_hub.server import create_server

AGENTS = ("alice", "bob", "carol", "mallory")
SQUAD_REF = "hub.squad/1?name=crew"
PEM = "-----BEGIN RSA PRIVATE KEY-----\nnope\n"
GH_TOKEN = "ghp_" + "b" * 24


# -- harness -------------------------------------------------------------------


async def _call(server, name: str, args: dict, ctx=None) -> str:
    result = await server._tool_manager.call_tool(name, args, context=ctx)
    if hasattr(result, "content"):
        for block in result.content:
            if hasattr(block, "text"):
                return block.text
    if isinstance(result, list):
        for block in result:
            if hasattr(block, "text"):
                return block.text
    return result if isinstance(result, str) else str(result)


class _FakeSess:
    _write_stream = object()

    async def send_ping(self):
        return None


class _FakeReqCtx:
    request = None


class _FakeCtx:
    """`ToolManager.call_tool`'s `context` is injected as the tool's ctx
    kwarg, which is the whole boundary `_attribution` reads."""

    def __init__(self, session=None):
        self.session = session or _FakeSess()
        self.request_context = _FakeReqCtx()


@pytest.fixture
def server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # Per-agent keys OFF explicitly: the grade under test is the SESSION's,
    # and an ambient MCP_HUB_AGENT_KEYS would change the answer.
    monkeypatch.setenv("MCP_HUB_AGENT_KEYS", "off")
    srv = create_server(db_path=tmp_path / "tasks.db")
    conn = _db(srv)
    try:
        for a in AGENTS:
            conn.execute(
                "INSERT INTO agents (name, project, bio, status, registered,"
                " last_seen) VALUES (?, 'p', '', 'online', 0, 0)", (a,)
            )
        for a in ("bob", "carol"):
            conn.execute(
                "INSERT INTO squad_members (agent, squad, muted, expires)"
                " VALUES (?, 'crew', 0, 0)", (a,)
            )
        conn.commit()
    finally:
        conn.close()
    return srv


def _db(server) -> sqlite3.Connection:
    conn = sqlite3.connect(server._hub_db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _ref(text: str) -> str:
    assert "hub.task/1?id=" in text, f"no task ref in: {text!r}"
    head = text.index("hub.task/1?id=")
    digits = ""
    for ch in text[head + len("hub.task/1?id="):]:
        if not ch.isdigit():
            break
        digits += ch
    return f"hub.task/1?id={digits}"


def _id(ref: str) -> int:
    return int(ref.rsplit("=", 1)[1])


async def _create(server, frm="alice", to="bob", title="write it up",
                  body="the usual", ctx=None, **extra) -> str:
    out = await _call(server, "create_task", {
        "from_agent": frm, "to": to, "title": title, "body": body, **extra,
    }, ctx=ctx)
    return _ref(out)


def _count(server, sql: str, params: tuple = ()) -> int:
    conn = _db(server)
    try:
        return int(conn.execute(sql, params).fetchone()[0])
    finally:
        conn.close()


def _row(server, ref: str):
    conn = _db(server)
    try:
        return conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (_id(ref),)
        ).fetchone()
    finally:
        conn.close()


def _events(server, ref: str) -> list[sqlite3.Row]:
    conn = _db(server)
    try:
        return conn.execute(
            "SELECT * FROM task_events WHERE task_id = ? ORDER BY seq",
            (_id(ref),),
        ).fetchall()
    finally:
        conn.close()


def _notices(server, ref: str) -> list[sqlite3.Row]:
    conn = _db(server)
    try:
        return [
            r for r in conn.execute(
                "SELECT * FROM messages WHERE from_agent = 'hub' ORDER BY id"
            ).fetchall() if ref in r["body"]
        ]
    finally:
        conn.close()


async def _claim(server, who: str, ref: str, version: int | None = None,
                 ctx=None) -> str:
    v = int(_row(server, ref)["version"]) if version is None else version
    return await _call(server, "claim_task", {
        "from_agent": who, "ref": ref, "version": v,
    }, ctx=ctx)


async def _json(server, ref: str) -> dict:
    return json.loads(await _call(server, "get_task",
                                  {"ref": ref, "format": "json"}))


# -- the store ----------------------------------------------------------------


def test_ensure_schema_is_idempotent_and_keeps_rows(tmp_path: Path):
    conn = sqlite3.connect(tmp_path / "s.db")
    conn.row_factory = sqlite3.Row
    tasks.ensure_schema(conn)
    conn.execute(
        "INSERT INTO tasks (title, body, requester, addressed_to, state,"
        " created_at, version) VALUES ('t', 'b', 'alice', ?, 'open', 1.0, 1)",
        (SQUAD_REF,),
    )
    conn.commit()
    tasks.ensure_schema(conn)  # a second server boot must not wipe anything
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1


def test_tasks_table_has_no_result_column(tmp_path: Path):
    """A result is the owner's words at a moment, under a grade. It lives on
    the done event; a column would keep the text and lose the attribution."""
    conn = sqlite3.connect(tmp_path / "s.db")
    tasks.ensure_schema(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
    assert cols == {
        "id", "title", "body", "requester", "addressed_to", "owner", "state",
        "created_at", "last_notice_msg_id", "version",
    }
    assert "result" not in cols
    # r8's clocks are not here yet either — and not as empty columns.
    assert not {"deadline_at", "lease_seconds", "lease_until"} & cols


def test_two_events_cannot_share_a_seq(tmp_path: Path):
    """The log's order is a claim about what happened in which order, so a
    duplicate seq is unrepresentable rather than merely unlikely."""
    conn = sqlite3.connect(tmp_path / "s.db")
    tasks.ensure_schema(conn)
    for _ in range(2):
        try:
            conn.execute(
                "INSERT INTO task_events (task_id, seq, act, actor, grade, ts)"
                " VALUES (1, 1, 'claim', 'bob', 'asserted', 1.0)"
            )
        except sqlite3.IntegrityError:
            return
    pytest.fail("task_events accepted two events at seq 1")


# -- the ref ------------------------------------------------------------------


def test_hub_task_is_a_registered_scheme_requiring_an_id():
    assert "hub.task/1" in refs.registered_schemes()
    assert refs.parse_ref("hub.task/1?id=7").get("id") == "7"
    with pytest.raises(refs.RefError):
        refs.parse_ref("hub.task/1?name=7")      # id is the identity
    with pytest.raises(refs.RefError):
        refs.make_ref("hub.task/1")              # no half-identities


def test_a_task_ref_is_not_a_message_ref():
    assert tasks.task_ref(12) == "hub.task/1?id=12"
    assert refs.parse_ref(tasks.task_ref(12)).scheme == "hub.task/1"


# -- F17 create ---------------------------------------------------------------


async def test_create_refuses_an_empty_title_and_stores_nothing(server):
    out = await _call(server, "create_task", {
        "from_agent": "alice", "to": "bob", "title": "   ", "body": "b",
    })
    assert out.startswith("REFUSED")
    assert _count(server, "SELECT COUNT(*) FROM tasks") == 0


async def test_create_refuses_a_name_nobody_answers(server):
    for bad in ("nobody", "crew"):  # "crew" is a SQUAD, not an agent
        out = await _call(server, "create_task", {
            "from_agent": "alice", "to": bad, "title": "t", "body": "b",
        })
        assert out.startswith("REFUSED"), out
        assert "hub.squad/1?name=" in out, (
            f"the refusal does not name the squad form: {out!r}")
    assert _count(server, "SELECT COUNT(*) FROM tasks") == 0


async def test_create_refuses_an_addressee_that_is_neither_agent_nor_squad(server):
    out = await _call(server, "create_task", {
        "from_agent": "alice", "to": "hub.channel/1?name=general",
        "title": "t", "body": "b",
    })
    assert out.startswith("REFUSED") and "hub.channel/1" in out
    out = await _call(server, "create_task", {
        "from_agent": "alice", "to": "", "title": "t", "body": "b",
    })
    assert out.startswith("REFUSED")


async def test_create_addressed_to_yourself_tells_nobody(server):
    ref = await _create(server, frm="alice", to="alice")
    assert _notices(server, ref) == [], (
        "a notice to the requester about their own task is noise, not a party")
    assert _row(server, ref)["state"] == "open"


async def test_every_secret_pattern_is_refused_naming_only_the_pattern(server):
    for field, value, name in (("body", PEM, "a PEM private key block"),
                               ("title", GH_TOKEN, "a GitHub token")):
        args = {"from_agent": "alice", "to": "bob", "title": "t", "body": "b"}
        args[field] = f"here: {value}"
        out = await _call(server, "create_task", args)
        assert out.startswith("REFUSED") and name in out, out
        assert value not in out, "the refusal echoed the secret"
    assert _count(server, "SELECT COUNT(*) FROM tasks") == 0


async def test_a_refused_clock_names_r8_and_the_field(server):
    for field in ("deadline", "lease"):
        out = await _call(server, "create_task", {
            "from_agent": "alice", "to": "bob", "title": "t", "body": "b",
            field: "whenever",
        })
        assert out.startswith("REFUSED") and field in out and "r8" in out, out


async def test_create_records_the_requester_and_one_event(server):
    ref = await _create(server)
    ev = _events(server, ref)
    assert [(e["seq"], e["act"], e["actor"], e["from_state"], e["to_state"])
            for e in ev] == [(1, "create", "alice", "", "open")]
    row = _row(server, ref)
    assert row["requester"] == "alice"
    assert row["addressed_to"] == "hub.agent/1?name=bob"
    assert row["owner"] is None, "an open task's owner is NULL, not ''"
    assert int(row["version"]) == 1


# -- F18 claim ----------------------------------------------------------------


async def test_claim_refuses_a_ref_that_is_not_a_task(server):
    await _create(server)
    for bad in ("hub.msg/1?id=1", "hub.task/1", "nonsense", "hub.task/1?id=99"):
        out = await _call(server, "claim_task", {
            "from_agent": "bob", "ref": bad, "version": 1,
        })
        assert out.startswith("REFUSED"), f"{bad} was accepted: {out!r}"


async def test_claiming_your_own_claimed_task_again_is_refused(server):
    ref = await _create(server)
    assert not (await _claim(server, "bob", ref)).startswith("REFUSED")
    out = await _claim(server, "bob", ref)
    assert out.startswith("REFUSED") and "bob" in out
    assert _row(server, ref)["owner"] == "bob"


async def test_a_done_task_cannot_be_claimed(server):
    ref = await _create(server)
    await _claim(server, "bob", ref)
    await _call(server, "task_done",
                {"from_agent": "bob", "ref": ref, "result": "ok"})
    out = await _claim(server, "bob", ref)
    assert out.startswith("REFUSED") and "done" in out
    assert _row(server, ref)["state"] == "done"


async def test_membership_is_read_now_not_when_the_task_was_made(server):
    """Both directions of the same rule: a leaver cannot claim, a joiner can."""
    ref = await _create(server, to=SQUAD_REF)
    conn = _db(server)
    conn.execute("DELETE FROM squad_members WHERE agent = 'bob'")
    conn.execute("INSERT INTO squad_members (agent, squad, muted, expires)"
                 " VALUES ('mallory', 'crew', 0, 0)")
    conn.commit()
    conn.close()
    out = await _claim(server, "bob", ref)
    assert out.startswith("REFUSED") and "current member" in out, out
    out = await _claim(server, "mallory", ref)
    assert not out.startswith("REFUSED"), out
    assert _row(server, ref)["owner"] == "mallory"


async def test_every_transition_moves_the_version_by_one(server):
    ref = await _create(server)
    assert int(_row(server, ref)["version"]) == 1
    await _claim(server, "bob", ref)
    assert int(_row(server, ref)["version"]) == 2
    await _call(server, "task_done",
                {"from_agent": "bob", "ref": ref, "result": "ok"})
    assert int(_row(server, ref)["version"]) == 3
    # A refused act records an event and does NOT move the version.
    before = int(_row(server, ref)["version"])
    await _claim(server, "carol", ref)
    assert int(_row(server, ref)["version"]) == before


# -- F19 done -----------------------------------------------------------------


async def test_done_twice_is_refused(server):
    ref = await _create(server)
    await _claim(server, "bob", ref)
    args = {"from_agent": "bob", "ref": ref, "result": "ok"}
    assert not (await _call(server, "task_done", args)).startswith("REFUSED")
    out = await _call(server, "task_done", args)
    assert out.startswith("REFUSED"), "done is not idempotent, it is terminal"
    assert len([e for e in _events(server, ref) if e["act"] == "done"]) == 1


async def test_an_empty_result_is_allowed_and_the_claim_still_stands(server):
    ref = await _create(server)
    await _claim(server, "bob", ref)
    out = await _call(server, "task_done", {"from_agent": "bob", "ref": ref})
    assert not out.startswith("REFUSED"), out
    view = await _json(server, ref)
    assert view["state"] == "done" and view["result"] == ""


async def test_the_result_is_the_done_events_reason(server):
    ref = await _create(server)
    await _claim(server, "bob", ref)
    await _call(server, "task_done", {
        "from_agent": "bob", "ref": ref, "result": "notes at drafts/r6.md",
    })
    done = [e for e in _events(server, ref) if e["act"] == "done"][0]
    assert done["reason"] == "notes at drafts/r6.md"
    assert (await _json(server, ref))["result"] == done["reason"]


async def test_a_refused_done_records_the_actor_but_no_notice(server):
    ref = await _create(server)
    await _claim(server, "bob", ref)
    before = len(_notices(server, ref))
    out = await _call(server, "task_done",
                      {"from_agent": "carol", "ref": ref, "result": "mine"})
    assert out.startswith("REFUSED") and "bob" in out
    refused = [e for e in _events(server, ref) if e["actor"] == "carol"]
    assert len(refused) == 1 and refused[0]["grade"]
    assert refused[0]["notice_msg_id"] is None
    assert len(_notices(server, ref)) == before, (
        "a refusal is not a transition, so it tells nobody")
    assert "mine" not in json.dumps([dict(e) for e in _events(server, ref)]), (
        "a refused result was recorded anyway")


# -- F20 the reads ------------------------------------------------------------


async def test_get_task_refuses_an_unknown_format_and_an_unknown_task(server):
    ref = await _create(server)
    out = await _call(server, "get_task", {"ref": ref, "format": "yaml"})
    assert out.startswith("REFUSED") and "json" in out
    out = await _call(server, "get_task", {"ref": "hub.task/1?id=404"})
    assert out.startswith("REFUSED")


async def test_json_on_an_open_task_is_null_where_nothing_happened(server):
    view = await _json(server, await _create(server))
    assert set(view) == set(tasks.TASK_JSON_FIELDS)
    assert view["owner"] is None and view["owner_grade"] is None
    assert view["result"] is None, (
        "an unfinished task has no result; '' would read as 'finished, no "
        "comment'")
    assert view["requester_grade"] == "asserted"
    assert view["created_ts"] == view["updated_ts"]
    assert [e["act"] for e in view["events"]] == ["create"]
    assert set(view["events"][0]) == set(tasks.EVENT_JSON_FIELDS)


async def test_list_tasks_refuses_a_state_that_is_not_one(server):
    # v2-fleet r7 (declared base change): accepted is a state now; "reopened" never is.
    out = await _call(server, "list_tasks", {"state": "reopened"})
    assert out.startswith("REFUSED") and "open" in out


async def test_list_tasks_takes_a_squad_by_name_or_by_ref(server):
    ref = await _create(server, to=SQUAD_REF)
    for arg in ("crew", SQUAD_REF):
        out = await _call(server, "list_tasks", {"squad": arg})
        assert ref in out, f"squad={arg!r} missed the task: {out!r}"
    out = await _call(server, "list_tasks", {"squad": "hub.agent/1?name=bob"})
    assert out.startswith("REFUSED")


async def test_list_tasks_says_so_when_nothing_matches(server):
    await _create(server)
    out = await _call(server, "list_tasks", {"owner": "carol"})
    assert "hub.task/1?id=" not in out
    assert "owner=carol" in out, (
        f"an empty list must name the filters it applied: {out!r}")


async def test_the_log_shows_a_refusal_as_a_non_transition(server):
    ref = await _create(server)
    await _claim(server, "carol", ref)
    text = await _call(server, "get_task", {"ref": ref})
    assert "claim-refused by carol ·asserted" in text, text
    assert "open→open" in text, (
        "a refused act must read as no movement, not as a transition")


# -- F21 the notices ----------------------------------------------------------


async def test_a_notice_carries_the_ref_and_none_of_the_task_text(server):
    """The task row is the truth; the notice only points at it. Keeping the
    text out is also what stops a forged title riding a pointer into a drain."""
    secret_ish = "TITLE-MARKER-7719"
    body_marker = "BODY-MARKER-7719"
    ref = await _create(server, to="bob", title=f"ship {secret_ish}",
                        body=f"details {body_marker}")
    await _claim(server, "bob", ref)
    await _call(server, "task_done", {
        "from_agent": "bob", "ref": ref, "result": f"done {body_marker}-r",
    })
    ns = _notices(server, ref)
    assert len(ns) == 3
    for n in ns:
        assert ref in n["body"]
        assert secret_ish not in n["body"], "a notice carried the title"
        assert body_marker not in n["body"], "a notice carried task text"


async def test_a_squad_create_leaves_the_chain_to_start_at_the_claim(server):
    """No first notice to reply to is not the same thing as a broken chain."""
    ref = await _create(server, to=SQUAD_REF)
    await _claim(server, "bob", ref)
    await _call(server, "task_done",
                {"from_agent": "bob", "ref": ref, "result": "ok"})
    ns = _notices(server, ref)
    assert [n["to_agent"] for n in ns] == ["alice", "alice"]
    conn = _db(server)
    try:
        def parent(mid):
            return [r["object"] for r in conn.execute(
                "SELECT object FROM lineage_edges WHERE subject = ?"
                " AND predicate = 'replies-to'", (f"hub.msg/1?id={mid}",))]

        assert parent(ns[0]["id"]) == []
        assert parent(ns[1]["id"]) == [f"hub.msg/1?id={ns[0]['id']}"]
        assert int(_row(server, ref)["last_notice_msg_id"]) == ns[1]["id"]
    finally:
        conn.close()


async def test_a_notice_is_hub_authored_and_unread_until_pulled(server):
    ref = await _create(server, to="bob")
    (notice,) = _notices(server, ref)
    assert notice["from_agent"] == "hub"
    assert notice["attribution"] == "hub-authored"
    assert notice["priority"] == "normal"
    assert notice["read"] == 0, "a notice is queued, never pre-read"
    drained = await _call(server, "get_messages",
                          {"agent_name": "bob", "bind": False})
    assert ref in drained, "the notice never reached the addressee's inbox"


# -- the grade on the record is _attribution's --------------------------------


async def test_a_bound_session_records_session_verified(server):
    sess = _FakeSess()
    server._hub_registry.bind("bob", sess)
    ctx = _FakeCtx(sess)
    ref = await _create(server, frm="alice", to="bob")
    out = await _claim(server, "bob", ref, ctx=ctx)
    assert not out.startswith("REFUSED"), out
    claim = [e for e in _events(server, ref) if e["act"] == "claim"][0]
    assert claim["grade"] == "session-verified", (
        "the event's grade is _attribution's, not a constant")
    view = await _json(server, ref)
    assert view["owner_grade"] == "session-verified"
    assert "claimed by bob ·verified" in await _call(
        server, "get_task", {"ref": ref})


async def test_an_identity_mismatch_writes_no_task_and_no_event(server):
    """A session bound to bob asserting alice is the one provable
    mis-attribution at the tool boundary — nothing is recorded under either."""
    sess = _FakeSess()
    server._hub_registry.bind("bob", sess)
    ctx = _FakeCtx(sess)
    out = await _call(server, "create_task", {
        "from_agent": "alice", "to": "carol", "title": "t", "body": "b",
    }, ctx=ctx)
    assert out.startswith("REFUSED") and "identity mismatch" in out
    assert _count(server, "SELECT COUNT(*) FROM tasks") == 0
    assert _count(server, "SELECT COUNT(*) FROM task_events") == 0

    ref = await _create(server, frm="alice", to="bob")
    out = await _claim(server, "carol", ref, ctx=ctx)
    assert out.startswith("REFUSED") and "identity mismatch" in out
    assert [e["actor"] for e in _events(server, ref)] == ["alice"], (
        "a refusal before the record was reached must record nothing")


# -- a task is never a kind of message ----------------------------------------


async def test_a_task_reaches_no_feed_and_no_channel(server):
    marker = "FEED-MARKER-5513"
    ref = await _create(server, to=SQUAD_REF, body=f"work: {marker}")
    await _claim(server, "bob", ref)
    for out in (
        await _call(server, "get_broadcasts", {"limit": 50}),
        await _call(server, "get_history", {"agent_or_channel": "#general"}),
        await _call(server, "get_history", {"agent_or_channel": "bob"}),
        await _call(server, "get_history", {"agent_or_channel": "alice"}),
    ):
        assert marker not in out
    assert _count(server, "SELECT COUNT(*) FROM messages WHERE body LIKE ?",
                  (f"%{marker}%",)) == 0
    assert marker in await _call(server, "get_task", {"ref": ref})
