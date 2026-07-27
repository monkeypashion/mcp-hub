"""Broadcast scoping — who a fleet message actually reaches.

WHY THIS EXISTS, measured 2026-07-27: `broadcast` had no scoping, so one
squad's multi-turn investigation woke every agent on the hub. Three separate
instances of an uninvolved agent (an original and two of its own transported
copies) independently answered into a lane whose context none of them held,
within 21 seconds of each other and unaware of one another.

The unit is a TEAM, not a project and not an org. That squad spanned
dreamteam-ai-labs/{pm,factory-operations,dreamteam,spike} AND
monkeypashion/vps-hetzner — four projects, two orgs, collaborating correctly.
Scoping by project would have severed them; by org would have grouped vps with
the agents that needed excluding. Both cut across the real boundary.

THE TRAP THESE TESTS EXIST FOR: there are TWO delivery paths. A broadcast
reaches an agent by live push AND by the Stop-hook cursor catch-up. Filtering
only the first looks like a fix and is cosmetic — several of the messages that
caused the cross-lane replies arrived through the second.
"""
from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import patch

import pytest

from mcp_hub.server import create_server


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


async def _enrol(server, name: str, project: str, team: str = "") -> None:
    await _call(server, "register",
                {"name": name, "project": project, "team": team})


async def _squad(server) -> None:
    """The real shape that motivated this: one squad, four projects, two orgs —
    plus an uninvolved agent that kept answering into it."""
    await _enrol(server, "pm", "dreamteam-ai-labs/pm", "dreamteam")
    await _enrol(server, "fo", "dreamteam-ai-labs/factory-operations", "dreamteam")
    await _enrol(server, "vps", "monkeypashion/vps-hetzner", "dreamteam")   # other ORG
    await _enrol(server, "hub", "monkeypashion/mcp-hub", "hublane")
    await _enrol(server, "hub-clone", "monkeypashion/mcp-hub", "hublane")


# ---- the live push path ---------------------------------------------------
#
# Observed through broadcast's own "woke N/M" return, where M is the recipient
# count after filtering. registry.names() is faked because these agents have no
# real bound sessions; push_channel itself is a closure inside create_server and
# cannot be patched by name.

FLEET = ["pm", "fo", "vps", "hub", "hub-clone"]


def _recipients(out: str) -> int:
    m = re.search(r"woke \d+/(\d+) connected agents", out)
    assert m, f"no recipient count in: {out}"
    return int(m.group(1))


async def test_a_team_broadcast_reaches_the_team_across_projects_and_orgs(server):
    """The collaboration that WORKED must keep working. vps is in a different
    org from pm and fo and belongs with them anyway."""
    await _squad(server)
    with patch.object(server._hub_registry, "names", lambda: list(FLEET)):
        out = await _call(server, "broadcast",
                          {"from_agent": "pm", "message": "who deleted it?"})
    # fo + vps, not hub or hub-clone, not the sender
    assert _recipients(out) == 2, out


async def test_a_sender_with_no_team_still_reaches_everyone(server):
    """Backwards compatibility, and the safety rule: a group we cannot NAME is
    not a group we may silently exclude people from."""
    await _squad(server)
    await _enrol(server, "stranger", "someone/else")      # no team
    with patch.object(server._hub_registry, "names", lambda: FLEET + ["stranger"]):
        out = await _call(server, "broadcast",
                          {"from_agent": "stranger", "message": "hub redeploying"})
    assert _recipients(out) == len(FLEET), out


async def test_scope_fleet_stays_available_and_explicit(server):
    """A genuine all-hands announcement must still be possible from inside a
    team — it just has to be asked for."""
    await _squad(server)
    with patch.object(server._hub_registry, "names", lambda: list(FLEET)):
        team = await _call(server, "broadcast",
                           {"from_agent": "pm", "message": "x"})
        fleet = await _call(server, "broadcast",
                            {"from_agent": "pm", "message": "hub down", "scope": "fleet"})
    assert _recipients(team) == 2 and _recipients(fleet) == 4, (team, fleet)


async def test_an_unknown_scope_is_refused(server):
    await _squad(server)
    out = await _call(server, "broadcast",
                      {"from_agent": "pm", "message": "x", "scope": "everyone"})
    assert "Invalid scope" in out


# ---- the Stop-hook catch-up path — the one that actually bit --------------

async def test_the_catch_up_path_filters_too(server):
    """THE test. Filtering only the live push is cosmetic: an out-of-team agent
    would still be handed the whole thread at its next turn boundary, which is
    exactly where the cross-lane replies came from."""
    await _squad(server)
    await _call(server, "broadcast", {"from_agent": "pm", "message": "squad-only detail"})

    out_of_team = await _call(server, "get_broadcasts_for_agent",
                              {"agent_name": "hub", "bind": False})
    assert "squad-only detail" not in out_of_team, \
        f"out-of-team agent caught up on it anyway:\n{out_of_team}"

    in_team = await _call(server, "get_broadcasts_for_agent",
                          {"agent_name": "fo", "bind": False})
    assert "squad-only detail" in in_team, f"in-team agent missed it:\n{in_team}"


async def test_a_fleet_broadcast_still_reaches_everyone_on_catch_up(server):
    await _squad(server)
    await _call(server, "broadcast",
                    {"from_agent": "pm", "message": "all hands", "scope": "fleet"})

    out = await _call(server, "get_broadcasts_for_agent",
                      {"agent_name": "hub", "bind": False})
    assert "all hands" in out, out


async def test_a_filtered_row_is_never_re_offered(server):
    """The cursor advances past everything, including rows this agent was never
    party to. Otherwise a filtered row sits at the head of the queue forever and
    every later broadcast is stuck behind it."""
    await _squad(server)
    await _call(server, "broadcast", {"from_agent": "pm", "message": "squad-only"})
    await _call(server, "broadcast",
                    {"from_agent": "pm", "message": "for everyone", "scope": "fleet"})

    first = await _call(server, "get_broadcasts_for_agent",
                        {"agent_name": "hub", "bind": False})
    assert "for everyone" in first and "squad-only" not in first, first
    second = await _call(server, "get_broadcasts_for_agent",
                         {"agent_name": "hub", "bind": False})
    assert "squad-only" not in second and "for everyone" not in second, \
        f"re-offered after the cursor advanced:\n{second}"


# ---- team persistence -----------------------------------------------------

async def test_re_registering_without_a_team_does_not_drop_you_from_it(server):
    """An agent that hasn't learned to send a team yet must not silently leave
    its squad on a reconnect — and today every agent reconnects constantly."""
    await _enrol(server, "fo", "dreamteam-ai-labs/factory-operations", "dreamteam")
    await _call(server, "register",
                {"name": "fo", "project": "dreamteam-ai-labs/factory-operations"})

    await _enrol(server, "pm", "dreamteam-ai-labs/pm", "dreamteam")
    await _call(server, "broadcast", {"from_agent": "pm", "message": "still here?"})

    out = await _call(server, "get_broadcasts_for_agent", {"agent_name": "fo", "bind": False})
    assert "still here?" in out, f"a bare re-register dropped fo from its team:\n{out}"
