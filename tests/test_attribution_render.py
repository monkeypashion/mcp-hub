"""The attribution grade is RENDERED wherever the sender's name is.

Recorded at five write sites and read at none (factory-operations,
2026-08-28): an `asserted` message and a `session-verified` one were
byte-identical to every reader. Silence now means session-verified;
`asserted` and `ungraded` are marked beside the name — in every read tool,
in text and json alike.

Card #271 (2026-08-29): verified is no longer the silent case. Every name
carries its grade, and the reader's rule is NO GRADE = NOT VERIFIED — a
quote or a suffix-dropping client can only ever LOSE a mark, never gain one.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from mcp_hub.server import _grade_tag_str, create_server


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "hub.db"


@pytest.fixture
def server(db: Path):
    return create_server(db_path=db)


async def _call(server, name: str, args: dict) -> str:
    result = await server._tool_manager.call_tool(name, args)
    if hasattr(result, "content"):
        for block in result.content:
            if hasattr(block, "text"):
                return block.text
    return result if isinstance(result, str) else str(result)


def _set_grade(db: Path, from_agent: str, grade: str) -> None:
    conn = sqlite3.connect(db)
    conn.execute("UPDATE messages SET attribution=? WHERE from_agent=?",
                 (grade, from_agent))
    conn.commit()
    conn.close()


class TestTag:
    def test_session_verified_is_marked_positively(self):
        """Card #271: a positive token survives quoting and truncation
        because there is something to lose; silence favoured the impostor."""
        assert _grade_tag_str("session-verified") == " ·verified"

    def test_no_grade_value_never_renders_as_silence(self):
        """The reader's rule NO GRADE = NOT VERIFIED only holds if the render
        never emits nothing."""
        for g in ("session-verified", "operator-verified", "asserted", "", "x"):
            assert _grade_tag_str(g) != "", g

    def test_asserted_is_marked(self):
        assert "asserted" in _grade_tag_str("asserted")

    def test_legacy_empty_is_marked_not_silent(self):
        """A pre-grading row must not read as verified — absence is not a
        measurement."""
        assert _grade_tag_str("") == " ·ungraded"


class TestReadTools:
    async def test_get_messages_marks_an_asserted_sender(self, server):
        await _call(server, "register", {"name": "bob", "project": "p"})
        await _call(server, "send", {"from_agent": "alice", "to": "bob",
                                     "message": "hi"})
        out = await _call(server, "get_messages", {"agent_name": "bob"})
        assert "alice** ·asserted" in out, out

    async def test_get_messages_marks_session_verified_positively(self, server, db):
        await _call(server, "register", {"name": "bob", "project": "p"})
        await _call(server, "send", {"from_agent": "alice", "to": "bob",
                                     "message": "hi"})
        _set_grade(db, "alice", "session-verified")
        out = await _call(server, "get_messages", {"agent_name": "bob"})
        assert "alice** ·verified" in out, out
        assert "·asserted" not in out and "·ungraded" not in out

    async def test_get_messages_marks_a_legacy_row_ungraded(self, server, db):
        await _call(server, "register", {"name": "bob", "project": "p"})
        await _call(server, "send", {"from_agent": "alice", "to": "bob",
                                     "message": "hi"})
        _set_grade(db, "alice", "")
        out = await _call(server, "get_messages", {"agent_name": "bob"})
        assert "alice** ·ungraded" in out, out

    async def test_get_history_marks_dm_rows(self, server):
        await _call(server, "register", {"name": "bob", "project": "p"})
        await _call(server, "send", {"from_agent": "alice", "to": "bob",
                                     "message": "hi"})
        out = await _call(server, "get_history", {"agent_or_channel": "bob"})
        assert "alice ·asserted → bob" in out, out

    async def test_get_broadcasts_marks_the_sender(self, server):
        await _call(server, "register", {"name": "alice", "project": "p",
                                         "squads": "s1"})
        await _call(server, "broadcast", {"from_agent": "alice",
                                          "message": "hello squad"})
        out = await _call(server, "get_broadcasts", {})
        assert "alice** ·asserted" in out, out

    async def test_get_history_marks_broadcast_rows(self, server):
        await _call(server, "register", {"name": "alice", "project": "p",
                                         "squads": "s1"})
        await _call(server, "broadcast", {"from_agent": "alice",
                                          "message": "hello squad"})
        out = await _call(server, "get_history", {"agent_or_channel": "#general"})
        assert "alice ·asserted" in out, out

    async def test_channel_messages_text_and_json_carry_the_grade(self, server):
        await _call(server, "create_channel", {"name": "topic",
                                               "created_by": "alice"})
        await _call(server, "post", {"from_agent": "alice", "channel": "topic",
                                     "message": "note"})
        text = await _call(server, "get_channel_messages", {"channel": "topic"})
        assert "alice** ·asserted" in text, text
        rows = json.loads(await _call(server, "get_channel_messages",
                                      {"channel": "topic", "format": "json"}))
        assert rows[0]["attribution"] == "asserted"
