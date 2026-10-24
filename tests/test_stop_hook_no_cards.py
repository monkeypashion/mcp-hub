"""The Stop hook no longer touches DECISION cards (operator, 2026-10-06).

Driven through stop_hook_command, not the helpers: the old card tests call
`_extract_decision_card` and `build_hook_response(card_nag=...)` directly, so
they stay green whether or not the hook still uses them. Only a turn that
WOULD have filed a card and been nagged, run through the real entry point,
can show the leg is gone.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

WAITING_TURN = (
    "Still waiting on you to decide which window to deploy in.\n\n"
    "**DECISION**\n**ASK:** Deploy tonight?\n**WHY:** It's ready.\n"
    "**VALUE:** Unblocks brain. [6/10]\n**RISK:** Drops bindings. [3/10]"
)
# Waiting on the operator with NO card: the old hook's nag trigger.
WAITING_NO_CARD = "Still waiting on you to decide which window to deploy in."


def _run(tmp_path, monkeypatch, text, inbox=""):
    from mcp_hub import cli

    t = tmp_path / "t.jsonl"
    t.write_text("\n".join(json.dumps(r) for r in (
        {"type": "user", "message": {"role": "user", "content": "deploy?"}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": text}]}},
    )) + "\n")
    monkeypatch.setattr(cli, "_read_hook_stdin",
                        lambda: {"transcript_path": str(t), "cwd": str(tmp_path)})
    monkeypatch.setattr(cli, "_resolve_agent_identity",
                        lambda _a, _p: ("lane-a", "org/repo"))
    monkeypatch.setattr(cli, "_ensure_daemon_alive", lambda *_a: None)
    monkeypatch.setenv("MCP_HUB_STATE_DIR", str(tmp_path / "state"))
    calls = []

    async def _fake_query(*a, **kw):
        calls.append((a, kw))
        return (inbox, "", True, "card #1200 kept open (1/3)")

    with patch("mcp_hub.cli._query_hub", side_effect=_fake_query):
        rc = cli.stop_hook_command(SimpleNamespace(hub_url="http://hub.invalid/mcp"))
    return rc, calls


def test_a_card_in_the_reply_is_not_shipped(tmp_path, monkeypatch, capsys):
    rc, calls = _run(tmp_path, monkeypatch, WAITING_TURN)
    assert rc == 0
    (args, kw), = calls
    # Nothing card-shaped rides to the hub. The positional slots after
    # (url, name, project) were card and decided; the F14 evidence record
    # legitimately carries the reply text, so only the card slots are checked.
    assert args == ("http://hub.invalid/mcp", "lane-a", "org/repo")
    assert "card" not in kw and "decided" not in kw


def test_waiting_language_earns_no_nag_and_a_quiet_stop_stays_quiet(
    tmp_path, monkeypatch, capsys
):
    # Empty inbox, online, a reply that reads as waiting on the operator
    # with no card, and the hub still reporting an open card: the old hook
    # blocked here with a nag and a 📌 notice. Now the Stop proceeds silently.
    rc, _ = _run(tmp_path, monkeypatch, WAITING_NO_CARD)
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_no_card_notice_rides_along_with_real_mail(tmp_path, monkeypatch, capsys):
    rc, _ = _run(tmp_path, monkeypatch, WAITING_TURN,
                 inbox="[10:00] **bob** ⟨hub.msg/1?id=7⟩: hello")
    out = capsys.readouterr().out
    assert "bob" in out
    assert "card #1200" not in out and "DECISION" not in out


def test_once_shape_is_the_default_with_a_per_lane_escape(tmp_path, monkeypatch):
    from mcp_hub import cli

    monkeypatch.setenv("MCP_HUB_STATE_DIR", str(tmp_path))
    block = {"decision": "block", "reason": "📬 hello"}
    once = {"hookSpecificOutput": {"hookEventName": "Stop",
                                   "additionalContext": "📬 hello"}}
    # No file: every lane gets the single-delivery shape.
    assert cli._stop_output_shape("lane-a", block) == once
    # A lane named in the escape file keeps the old shape; others don't.
    (tmp_path / "stop-output-block").write_text("lane-a\n")
    assert cli._stop_output_shape("lane-a", block) == block
    assert cli._stop_output_shape("lane-b", block) == once
