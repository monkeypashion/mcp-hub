"""The per-squad roster read — list_squads(squad=...) (POC-1 GO, console #91).

The console's Squads tab could show member COUNTS only, so a non-dreamteam
squad's seats were invisible the moment they mattered (the poc-harness
watching brief). The roster read answers "who is in this squad and can I
reach them RIGHT NOW" with the same presence vocabulary as list_agents —
one thing looks the same wherever it appears.

Presence is read through the same gates list_agents uses (⚡ =
_can_deliver_push, not merely bound), so the roster can never claim
wakeable what a push would drop.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mcp_hub.server import create_server

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def server(tmp_path: Path):
    return create_server(db_path=tmp_path / "test.db")


async def _call(server, name: str, args: dict) -> str:
    result = await server._tool_manager.call_tool(name, args)
    if hasattr(result, "content"):
        for block in result.content:
            if hasattr(block, "text"):
                return block.text
    return str(result)


class _FakeSess:
    """Bound-and-deliverable in default test mode (no session_manager →
    _can_deliver_push passes through True)."""

    _write_stream = object()

    async def send_ping(self):
        return None

    async def send_notification(self, _notif):
        return None


def _db(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "test.db")
    conn.row_factory = sqlite3.Row
    return conn


async def test_roster_lists_each_member_with_live_presence(server, tmp_path):
    """One line per member, presence read through the list_agents gates:
    a bound member shows ⚡, a registered-but-unbound one shows 🟢 without
    ⚡, an idle one shows 💤."""
    await _call(server, "register",
                {"name": "dt-poc", "project": "dreamteam-ai-labs/dreamteam",
                 "squads": "poc-harness"})
    await _call(server, "register",
                {"name": "observer", "project": "x/y", "squads": "poc-harness"})
    server._hub_registry.bind("dt-poc", _FakeSess())

    conn = _db(tmp_path)
    conn.execute("UPDATE agents SET is_idle = 1 WHERE name = 'observer'")
    conn.commit()
    conn.close()

    out = await _call(server, "list_squads", {"squad": "poc-harness"})
    lines = {ln.strip().split(" ")[1]: ln for ln in out.splitlines() if "🟢" in ln or "⚫" in ln}
    assert "dt-poc" in lines and "⚡" in lines["dt-poc"], out
    assert "observer" in lines, out
    assert "⚡" not in lines["observer"], f"unbound member claims wakeable: {out}"
    assert "💤" in lines["observer"], out
    assert "2 member(s)" in out, out


async def test_roster_consults_the_deliverability_gate_not_mere_binding(tmp_path):
    """A member that IS bound but whose session a push would drop must not
    wear ⚡ in the roster — same truthfulness contract as list_agents, and
    the mutant this kills is `_can_deliver_push(s)` → `True`, which my
    unbound-member test cannot see (no sessions to miscount)."""
    server = create_server(db_path=tmp_path / "test.db")

    class _OtherTransport:
        _write_stream = object()
        _request_streams: dict = {}

    class _FakeManager:
        _server_instances = {"some-other-session": _OtherTransport()}

    server._session_manager = _FakeManager()

    await _call(server, "register",
                {"name": "dt-poc", "project": "d/d", "squads": "poc-harness"})

    class _StaleSession:
        _write_stream = object()

        async def send_ping(self):
            return None

    server._hub_registry.bind("dt-poc", _StaleSession())

    out = await _call(server, "list_squads", {"squad": "poc-harness"})
    line = next(ln for ln in out.splitlines() if "dt-poc" in ln)
    assert "🟢" in line, out
    assert "⚡" not in line, f"roster claims wakeable what a push would drop: {out}"


async def test_roster_shows_a_never_registered_member_honestly(server, tmp_path):
    """A seat added to the squad before its first register() (the poc-harness
    pre-boot state) must appear as NOT YET REGISTERED — omitting it would hide
    the exact gap the operator watches for, and defaulting it to 🟢 would
    manufacture presence."""
    await _call(server, "register",
                {"name": "dt-poc", "project": "d/d", "squads": "poc-harness"})
    conn = _db(tmp_path)
    conn.execute(
        "INSERT INTO squad_members (agent, squad, muted) VALUES ('ghost', 'poc-harness', 0)"
    )
    conn.commit()
    conn.close()

    out = await _call(server, "list_squads", {"squad": "poc-harness"})
    assert "ghost" in out, out
    ghost_line = next(ln for ln in out.splitlines() if "ghost" in ln)
    assert "not yet registered" in ghost_line, out
    assert "🟢" not in ghost_line and "⚡" not in ghost_line, out


async def test_unknown_squad_is_refused_loudly_naming_alternatives(server):
    """An unknown squad answers with the known ones, never an empty success —
    a blank roster for a typo'd name reads as 'squad exists, nobody in it'."""
    await _call(server, "register",
                {"name": "a", "project": "x/y", "squads": "real-squad"})
    out = await _call(server, "list_squads", {"squad": "poc-harnes"})  # typo
    assert "No squad named 'poc-harnes'" in out, out
    assert "real-squad" in out, f"refusal must name the alternatives: {out}"


async def test_agent_and_squad_together_are_refused(server):
    """Two filters = two different questions; guessing which one wins is how
    a caller reads the wrong list without knowing it."""
    await _call(server, "register",
                {"name": "a", "project": "x/y", "squads": "s"})
    out = await _call(server, "list_squads", {"agent": "a", "squad": "s"})
    assert "not both" in out, out
    assert "🟢" not in out and "⚡" not in out, f"refused but listed anyway: {out}"


async def test_muted_member_is_flagged_in_the_roster(server):
    """Muted = member who deliberately isn't listening; the console must see
    the difference between that and a deaf seat."""
    await _call(server, "register",
                {"name": "a", "project": "x/y", "squads": "s"})
    await _call(server, "register",
                {"name": "b", "project": "x/y", "squads": "s"})
    await _call(server, "mute_squad", {"name": "b", "squad": "s", "muted": True})
    out = await _call(server, "list_squads", {"squad": "s"})
    b_line = next(ln for ln in out.splitlines() if " b" in ln)
    assert "muted" in b_line, out
    a_line = next(ln for ln in out.splitlines() if " a" in ln)
    assert "muted" not in a_line, out


async def test_count_view_and_agent_view_are_unchanged(server):
    """The new filter must not disturb the two existing shapes — the console
    bridge and every agent habit already consume them."""
    await _call(server, "register",
                {"name": "a", "project": "x/y", "squads": "s"})
    counts = await _call(server, "list_squads", {})
    assert "**s** — 1 member(s)" in counts, counts
    mine = await _call(server, "list_squads", {"agent": "a"})
    assert "a is in:" in mine and "s" in mine, mine
