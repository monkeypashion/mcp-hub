"""Which agents the settings panel shows, and in what order.

The panel's view layer is Textual's (see settings_app.py); what remains ours is
deciding WHICH agents belong in it. That rule is shared with squad's ws_agents()
and the cockpit's tab list, so a third spelling here is how teardown and the tab
list would come to disagree about what they are acting on.

(This file used to test a hand-rolled curses state machine as well. That panel
was deleted — the toolkit owns keyboard, focus and mouse now — and its tests
went with it rather than being left to pass against nothing.)
"""
from __future__ import annotations

import json

from mcp_hub import cli

# ---- workspace scoping ----------------------------------------------------

def test_agents_are_scoped_by_folder_membership(tmp_path, monkeypatch):
    """The SAME rule squad's ws_agents() and the cockpit's tab list use. A third
    spelling is how teardown and the tab list come to disagree about what they
    are acting on."""
    conf = tmp_path / "squad.conf"
    conf.write_text(
        f"a1|{tmp_path}/one||--continue|squad\n"
        f"a2|{tmp_path}/two||--continue|faculty\n", encoding="utf-8")
    monkeypatch.setattr(cli, "SQUAD_CONF", conf)
    ws = tmp_path / "team.code-workspace"
    ws.write_text(json.dumps({"folders": [{"path": str(tmp_path / "one")}]}),
                  encoding="utf-8")
    (tmp_path / "one").mkdir()
    (tmp_path / "two").mkdir()

    scoped = cli._agents_in_workspace(str(ws))
    assert [r["agent"] for r in scoped] == ["a1"]


def test_no_workspace_means_the_whole_roster(tmp_path, monkeypatch):
    conf = tmp_path / "squad.conf"
    conf.write_text(f"a1|{tmp_path}/one|||\na2|{tmp_path}/two|||\n", encoding="utf-8")
    monkeypatch.setattr(cli, "SQUAD_CONF", conf)
    assert [r["agent"] for r in cli._agents_in_workspace(None)] == ["a1", "a2"]


def test_a_workspace_listing_no_roster_folder_scopes_to_nothing(tmp_path, monkeypatch):
    """Empty is the honest answer. Falling back to the whole roster HERE would
    silently widen the panel to the machine — the opposite of what the operator
    asked for — and look identical to a correctly scoped one."""
    conf = tmp_path / "squad.conf"
    conf.write_text(f"a1|{tmp_path}/one|||\n", encoding="utf-8")
    monkeypatch.setattr(cli, "SQUAD_CONF", conf)
    ws = tmp_path / "empty.code-workspace"
    ws.write_text(json.dumps({"folders": [{"path": str(tmp_path / "nowhere")}]}),
                  encoding="utf-8")
    assert cli._agents_in_workspace(str(ws)) == []


def test_the_roster_order_is_preserved(tmp_path, monkeypatch):
    """File order is the order squad lists agents and the order the tabs appear.
    Re-sorting here would disagree with both for no reason."""
    conf = tmp_path / "squad.conf"
    conf.write_text(f"zeta|{tmp_path}/z|||\nalpha|{tmp_path}/a|||\n", encoding="utf-8")
    monkeypatch.setattr(cli, "SQUAD_CONF", conf)
    assert [r["agent"] for r in cli._roster_all()] == ["zeta", "alpha"]
