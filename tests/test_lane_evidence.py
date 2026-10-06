"""F14 lane evidence — the hook records, the hub serves, nothing pushes.

The properties pinned here are the ones agreed in #fleet-v2, each of which
has a cheaper wrong version:

  AN ACK NEVER OVERWRITES A BLOCKER — a turn the machinery started that only
  replied refreshes last-seen and keeps the stored text. Without it, a lane's
  real blocker is gone at its next "nothing new" reply.

  STALE IS NOT CURRENT — past the cutoff the text is withheld and the lane
  reads NOT REPORTING; "never recorded" reads differently again.

  THE GRADE IS THE WRITER'S — the hook's unbound client grades `asserted`,
  and the render says so.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from mcp_hub import evidence
from mcp_hub.server import create_server


def _user(content):
    return {"type": "user", "message": {"role": "user", "content": content}}


def _assistant(*blocks):
    return {"type": "assistant",
            "message": {"role": "assistant", "content": list(blocks)}}


def _text(t):
    return {"type": "text", "text": t}


def _tool_use():
    return {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}


def _tool_result():
    return _user([{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}])


def _transcript(tmp_path: Path, *records) -> str:
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return str(p)


# -- reading the turn --------------------------------------------------------


def test_operator_turn_without_tools_is_substantive(tmp_path):
    path = _transcript(tmp_path, _user("where are we on bar 50?"),
                       _assistant(_text("Blocked on the guard.")))
    turn = evidence.read_turn(path)
    assert turn == {"text": "Blocked on the guard.", "trigger": "operator",
                    "tool_calls": 0, "substantive": True}


def test_stop_hook_ack_is_not_substantive(tmp_path):
    path = _transcript(
        tmp_path,
        _user("Stop hook feedback:\n📬 Auto-checked at Stop boundary"),
        _assistant(_text("Nothing new needs action.")),
    )
    turn = evidence.read_turn(path)
    assert turn["trigger"] == "stop-hook"
    assert turn["substantive"] is False


def test_hub_wake_that_used_a_tool_is_substantive(tmp_path):
    # The tool result in the middle is NOT the turn's start: the walk must
    # pass it and reach the <channel> prompt, or every tool turn would read
    # as starting from its own last tool result.
    path = _transcript(
        tmp_path,
        _user('<channel source="hub" from_agent="x">DM from x</channel>'),
        _assistant(_text("checking"), _tool_use()),
        _tool_result(),
        _assistant(_tool_use()),
        _tool_result(),
        _assistant(_text("Re-filed bar 367 to 7 Oct.")),
    )
    turn = evidence.read_turn(path)
    assert turn["trigger"] == "hub-wake"
    assert turn["tool_calls"] == 2
    assert turn["substantive"] is True
    assert turn["text"] == "Re-filed bar 367 to 7 Oct."


def test_turn_with_no_reply_text_records_nothing(tmp_path):
    path = _transcript(tmp_path, _user("go"), _assistant(_tool_use()))
    assert evidence.read_turn(path) is None
    assert evidence.read_turn(None) is None
    assert evidence.read_turn(str(tmp_path / "missing.jsonl")) is None


# -- storing and serving -----------------------------------------------------


@pytest.fixture
def server(tmp_path: Path):
    return create_server(db_path=tmp_path / "evidence.db")


async def _call(server, name: str, args: dict) -> str:
    result = await server._tool_manager.call_tool(name, args)
    if isinstance(result, str):
        return result
    for block in getattr(result, "content", None) or result:
        if hasattr(block, "text"):
            return block.text
    return str(result)


def _put(text, substantive, trigger="operator", tool_calls=0):
    return {"agent_name": "lane-a", "text": text, "substantive": substantive,
            "trigger": trigger, "tool_calls": tool_calls, "commit": "abc1234def",
            "project": "org/repo"}


async def test_ack_refreshes_last_seen_but_keeps_the_blocker(server):
    assert await _call(server, "evidence_put",
                       _put("Blocked: waiting on the deputy.", True)) == "recorded"
    assert await _call(server, "evidence_put",
                       _put("Nothing new.", False, "stop-hook")) == "seen"
    v = json.loads(await _call(server, "evidence",
                               {"lane": "lane-a", "format": "json"}))
    assert v["state"] == "current"
    assert v["text"] == "Blocked: waiting on the deputy."
    assert v["last_seen_at"] >= v["captured_at"]


async def test_the_hook_writer_grades_asserted_and_the_render_says_so(server):
    await _call(server, "evidence_put", _put("Working on F14.", True))
    v = json.loads(await _call(server, "evidence",
                               {"lane": "lane-a", "format": "json"}))
    assert v["grade"] == "asserted"
    text = await _call(server, "evidence", {"lane": "lane-a"})
    assert text.startswith("lane-a ·asserted — evidence")
    assert "Working on F14." in text


async def test_stale_evidence_reads_not_reporting_with_text_withheld(server):
    await _call(server, "evidence_put", _put("Old news.", True))
    later = time.time() + evidence.EVIDENCE_STALE_SECONDS + 60
    with patch("mcp_hub.evidence.time.time", return_value=later):
        v = json.loads(await _call(server, "evidence",
                                   {"lane": "lane-a", "format": "json"}))
        text = await _call(server, "evidence", {"lane": "lane-a"})
    assert v["state"] == "stale"
    assert v["text"] is None
    assert "NOT REPORTING" in text
    assert "Old news." not in text


async def test_never_recorded_is_not_the_same_as_stale(server):
    text = await _call(server, "evidence", {"lane": "nobody"})
    assert "NO EVIDENCE" in text
    # A lane that has only ever acknowledged is alive but has said nothing.
    await _call(server, "evidence_put", _put("Nothing new.", False, "stop-hook"))
    v = json.loads(await _call(server, "evidence",
                               {"lane": "lane-a", "format": "json"}))
    assert v["state"] == "none" and v["last_seen_at"]


async def test_clipping_is_flagged_never_silent(server):
    long = "x" * (evidence.EVIDENCE_MAX_CHARS + 1)
    await _call(server, "evidence_put", _put(long, True))
    v = json.loads(await _call(server, "evidence",
                               {"lane": "lane-a", "format": "json"}))
    assert v["clipped"] is True
    assert len(v["text"]) == evidence.EVIDENCE_MAX_CHARS
    assert "clipped" in await _call(server, "evidence", {"lane": "lane-a"})


async def test_evidence_all_lists_freshest_first_and_can_drop_stale(server):
    t0 = time.time()
    with patch("mcp_hub.evidence.time.time", return_value=t0 - 10 * 3600):
        await _call(server, "evidence_put",
                    {**_put("ancient", True), "agent_name": "lane-old"})
    await _call(server, "evidence_put", _put("fresh", True))
    views = json.loads(await _call(server, "evidence_all", {"format": "json"}))
    assert [v["agent"] for v in views] == ["lane-a", "lane-old"]
    assert [v["state"] for v in views] == ["current", "stale"]
    current = json.loads(await _call(
        server, "evidence_all", {"format": "json", "include_stale": False}))
    assert [v["agent"] for v in current] == ["lane-a"]


# -- the hook ships it --------------------------------------------------------


def test_stop_hook_passes_the_turn_to_the_hub(tmp_path, monkeypatch):
    from mcp_hub import cli

    path = _transcript(tmp_path, _user("status?"),
                       _assistant(_text("Building F14.")))
    payload = {"transcript_path": path, "cwd": str(tmp_path)}
    seen = {}

    async def _fake_query(*_a, **kw):
        seen.update(kw)
        return ("", "", True, "")

    monkeypatch.setattr(cli, "_read_hook_stdin", lambda: payload)
    monkeypatch.setattr(cli, "_resolve_agent_identity",
                        lambda _a, _p: ("lane-a", "org/repo"))
    monkeypatch.setattr(cli, "_ensure_daemon_alive", lambda *_a: None)
    args = SimpleNamespace(hub_url="http://hub.invalid/mcp")
    with patch("mcp_hub.cli._query_hub", side_effect=_fake_query):
        assert cli.stop_hook_command(args) == 0
    ev = seen["turn_evidence"]
    assert ev["text"] == "Building F14."
    assert ev["substantive"] is True and ev["trigger"] == "operator"
    # tmp_path is not a git checkout: the commit is empty, never invented.
    assert ev["commit"] == ""


def test_a_broken_evidence_read_never_breaks_the_hook(tmp_path, monkeypatch):
    from mcp_hub import cli

    monkeypatch.setattr(cli, "_read_hook_stdin",
                        lambda: {"transcript_path": "", "cwd": str(tmp_path)})
    monkeypatch.setattr(cli, "_resolve_agent_identity",
                        lambda _a, _p: ("lane-a", "org/repo"))
    monkeypatch.setattr(cli, "_ensure_daemon_alive", lambda *_a: None)
    monkeypatch.setattr(cli, "_turn_evidence",
                        lambda _p: (_ for _ in ()).throw(RuntimeError("boom")))
    seen = {}

    async def _fake_query(*_a, **kw):
        seen.update(kw)
        return ("", "", True, "")

    with patch("mcp_hub.cli._query_hub", side_effect=_fake_query):
        assert cli.stop_hook_command(
            SimpleNamespace(hub_url="http://hub.invalid/mcp")) == 0
    assert seen["turn_evidence"] is None
