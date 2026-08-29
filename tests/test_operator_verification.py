"""The operator's authority is VERIFIED, never name-matched (card #269).

`_OPERATOR_SENDERS` buys an immediate wake and blocked_by-clear rights. Until
this shipped the hub checked nothing behind the name: the console relays
through an unbound client (12/12 graded `asserted`, a constant), and any
agent typing the name got the same treatment. A bound session is not proof
either — register() is open. The proof is a secret the console holds and a
forger does not: $MCP_HUB_OPERATOR_TOKEN, presented per request in the
`x-mcp-hub-operator-token` header.

UNSET keeps today's behaviour exactly, so the hub can deploy ahead of the
console wiring the header without going deaf to its operator.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mcp_hub.server import (
    OPERATOR_TOKEN_ENV,
    OPERATOR_TOKEN_HEADER,
    _grade_tag_str,
    create_server,
)

TOKEN = "s3cret-console-token"
CARD = (
    "**DECISION**\n**ASK:** approve the widget rebuild?\n**WHY:** it is old\n"
    "**VALUE:** 5/10\n**RISK:** 2/10"
)


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "hub.db"


@pytest.fixture
def server(db: Path):
    return create_server(db_path=db)


class _FakeSess:
    _write_stream = object()

    async def send_ping(self):
        return None


class _FakeReq:
    def __init__(self, headers: dict[str, str]):
        # Starlette's Headers is case-insensitive; a plain dict keyed lower
        # matches how the hub reads it (by the lower-case constant).
        self.headers = headers


class _FakeReqCtx:
    def __init__(self, request):
        self.request = request


class _FakeCtx:
    """Reaches the real boundary the way tests/test_broadcast_scope does:
    ToolManager.call_tool's `context` is injected as the tool's ctx kwarg."""

    def __init__(self, session=None, headers: dict[str, str] | None = None):
        self.session = session or _FakeSess()
        self.request_context = _FakeReqCtx(
            _FakeReq(headers) if headers is not None else None
        )


async def _call(server, name: str, args: dict, ctx=None) -> str:
    result = await server._tool_manager.call_tool(name, args, context=ctx)
    if hasattr(result, "content"):
        for block in result.content:
            if hasattr(block, "text"):
                return block.text
    return result if isinstance(result, str) else str(result)


def _rows(db: Path, from_agent: str) -> list[tuple[str, str]]:
    conn = sqlite3.connect(db)
    try:
        return conn.execute(
            "SELECT body, attribution FROM messages WHERE from_agent=?",
            (from_agent,),
        ).fetchall()
    finally:
        conn.close()


async def _setup(server):
    await _call(server, "register", {"name": "bob", "project": "p"})


class TestOff:
    async def test_unset_token_keeps_todays_grading(self, server, db, monkeypatch):
        """The fallback is the rollout: an unarmed hub grades the console as
        it always did, and a real console with no header still gets through."""
        monkeypatch.delenv(OPERATOR_TOKEN_ENV, raising=False)
        await _setup(server)
        out = await _call(server, "send", {"from_agent": "operator-console",
                                           "to": "bob", "message": "go"},
                          ctx=_FakeCtx(headers={}))
        assert "REFUSED" not in out, out
        assert _rows(db, "operator-console") == [("go", "asserted")]

    async def test_status_says_off_and_names_the_switch(self, server, monkeypatch):
        monkeypatch.delenv(OPERATOR_TOKEN_ENV, raising=False)
        out = await _call(server, "hub_status", {})
        assert "Operator verification: OFF" in out and OPERATOR_TOKEN_ENV in out


class TestOn:
    @pytest.fixture(autouse=True)
    def _arm(self, monkeypatch):
        monkeypatch.setenv(OPERATOR_TOKEN_ENV, TOKEN)

    async def test_matching_token_is_operator_verified(self, server, db):
        await _setup(server)
        out = await _call(server, "send", {"from_agent": "operator-console",
                                           "to": "bob", "message": "go"},
                          ctx=_FakeCtx(headers={OPERATOR_TOKEN_HEADER: TOKEN}))
        assert "REFUSED" not in out, out
        assert _rows(db, "operator-console") == [("go", "operator-verified")]

    async def test_missing_token_is_refused_and_not_written(self, server, db):
        await _setup(server)
        out = await _call(server, "send", {"from_agent": "operator-console",
                                           "to": "bob", "message": "go"},
                          ctx=_FakeCtx(headers={}))
        assert "REFUSED" in out and OPERATOR_TOKEN_HEADER in out, out
        assert _rows(db, "operator-console") == []

    async def test_wrong_token_is_refused(self, server, db):
        await _setup(server)
        out = await _call(server, "send", {"from_agent": "operator-console",
                                           "to": "bob", "message": "go"},
                          ctx=_FakeCtx(headers={OPERATOR_TOKEN_HEADER: "nope"}))
        assert "REFUSED" in out, out
        assert _rows(db, "operator-console") == []

    async def test_no_request_at_all_is_refused(self, server, db):
        """stdio / no transport request: 'presented nothing', never verified."""
        await _setup(server)
        out = await _call(server, "send", {"from_agent": "operator",
                                           "to": "bob", "message": "go"})
        assert "REFUSED" in out, out
        assert _rows(db, "operator") == []

    async def test_bound_session_is_not_proof(self, server, db):
        """register() is open, so a session bound as operator-console proves
        only that someone bound it. The token decides, not the binding."""
        sess = _FakeSess()
        server._hub_registry.bind("operator-console", sess)
        await _setup(server)
        out = await _call(server, "send", {"from_agent": "operator-console",
                                           "to": "bob", "message": "go"},
                          ctx=_FakeCtx(session=sess, headers={}))
        assert "REFUSED" in out, out
        assert _rows(db, "operator-console") == []

    async def test_token_means_nothing_for_a_non_operator_name(self, server, db):
        """Presenting the token does not promote an ordinary sender."""
        await _setup(server)
        out = await _call(server, "send", {"from_agent": "alice", "to": "bob",
                                           "message": "hi"},
                          ctx=_FakeCtx(headers={OPERATOR_TOKEN_HEADER: TOKEN}))
        assert "REFUSED" not in out, out
        assert _rows(db, "alice") == [("hi", "asserted")]

    async def test_broadcast_and_post_pass_through_the_same_gate(self, server, db):
        await _setup(server)
        await _call(server, "create_channel", {"name": "ops", "created_by": "bob"})
        out = await _call(server, "post", {"from_agent": "operator-console",
                                           "channel": "ops", "message": "x"},
                          ctx=_FakeCtx(headers={}))
        assert "REFUSED" in out, out
        out = await _call(server, "broadcast", {"from_agent": "operator-console",
                                                "message": "x", "scope": "fleet"},
                          ctx=_FakeCtx(headers={}))
        assert "REFUSED" in out, out
        assert _rows(db, "operator-console") == []

    async def test_verified_renders_positively_like_session_verified(self, server, db):
        """Card #271: every name carries its grade; no grade = not verified."""
        assert _grade_tag_str("operator-verified") == " ·verified"
        await _setup(server)
        await _call(server, "send", {"from_agent": "operator-console",
                                     "to": "bob", "message": "go"},
                    ctx=_FakeCtx(headers={OPERATOR_TOKEN_HEADER: TOKEN}))
        out = await _call(server, "get_messages", {"agent_name": "bob"})
        assert "operator-console** ·verified" in out, out
        assert "·asserted" not in out and "·ungraded" not in out, out

    async def test_status_says_on(self, server):
        out = await _call(server, "hub_status", {})
        assert "Operator verification: ON" in out

    async def test_decision_answer_without_token_is_refused_card_stays_open(
        self, server, db,
    ):
        """decision_answer takes no from_agent and writes the verdict AS the
        operator — a console message path, so it stands behind the same
        door. Before this any agent could close any card as the operator."""
        await _setup(server)
        await _call(server, "decision_put", {"from_agent": "bob", "card": CARD})
        out = await _call(server, "decision_answer",
                          {"decision": "yes", "agent": "bob"},
                          ctx=_FakeCtx(headers={}))
        assert "REFUSED" in out, out
        assert "No open decision cards" not in await _call(
            server, "decision_list", {})
        assert _rows(db, "operator") == []

    async def test_decision_answer_with_token_is_operator_verified(
        self, server, db,
    ):
        await _setup(server)
        await _call(server, "decision_put", {"from_agent": "bob", "card": CARD})
        out = await _call(server, "decision_answer",
                          {"decision": "yes", "agent": "bob"},
                          ctx=_FakeCtx(headers={OPERATOR_TOKEN_HEADER: TOKEN}))
        assert "decided: yes" in out, out
        grades = [g for _, g in _rows(db, "operator")]
        assert grades == ["operator-verified"], grades
        inbox = await _call(server, "get_messages", {"agent_name": "bob"})
        assert "operator** ·verified" in inbox, inbox


class TestHubAuthored:
    def test_hub_authored_renders_as_hub_not_ungraded(self):
        """The hub's own notices (wake-ack drop, binding displacement) are
        its own acts; under NO GRADE = NOT VERIFIED they must not read as
        pre-grading rows."""
        assert _grade_tag_str("hub-authored") == " ·hub"

    async def test_decision_answer_unset_token_grades_asserted(
        self, server, db, monkeypatch,
    ):
        """Unset = today's behaviour, but the row is graded honestly rather
        than left blank."""
        monkeypatch.delenv(OPERATOR_TOKEN_ENV, raising=False)
        await _setup(server)
        await _call(server, "decision_put", {"from_agent": "bob", "card": CARD})
        out = await _call(server, "decision_answer",
                          {"decision": "yes", "agent": "bob"})
        assert "decided: yes" in out, out
        assert [g for _, g in _rows(db, "operator")] == ["asserted"]
