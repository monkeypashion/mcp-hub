"""The join under the fleet tree: machines → workspaces → seats.

Pure data, so every property here is asserted without a terminal. The ones
that matter are about NOT LYING and NOT LOSING:

  - a remote seat carries presence and nothing else, because there is no pane
    to scrape on another box
  - a snapshot that stopped being written reads as "not reporting", never as
    a fleet that went quiet
  - an agent that matches no known machine is shown under "(machine unknown)",
    never dropped
"""

from __future__ import annotations

from mcp_hub.fleet_tree import (
    FLEET_STALE_SECONDS,
    build_tree,
    machine_of,
    structure_key,
    walk_agents,
)

NOW = 1_800_000_000.0


def _ws(name, machine, listings, **kw):
    row = {
        "name": name, "machine": machine,
        "path": f"/home/me/Projects/{name}.code-workspace",
        "folders": len(listings), "error": "", "on_disk": True,
        "open_now": False, "registered": True, "squad": "",
        "listings": listings,
    }
    row.update(kw)
    return row


def _tree(*, roster=(), rows=(), fleet_agents=(), fleet_ts=NOW,
          machines=("here",), this_machine="here", board=None, scoped_to=None,
          listings_for=None):
    return build_tree(
        roster=list(roster),
        board=board or {"agents": {}},
        workspaces={"rows": list(rows), "machines": list(machines),
                    "this_machine": this_machine},
        fleet={"ts": fleet_ts, "agents": list(fleet_agents)},
        this_machine=this_machine,
        scoped_to=scoped_to,
        listings_for=listings_for,
        now=NOW,
    )


# ---- which box a name belongs to ------------------------------------------

def test_machine_of_matches_the_hostname_suffix():
    assert machine_of("mcp-hub-dev-vm-1", ["dev-vm-1", "here"]) == "dev-vm-1"


def test_a_transport_suffixed_name_still_resolves_to_its_machine():
    """`mcp-hub-fireblade-wsl-xport` is a real seat on a real box. endswith()
    would leave it homeless, which is how it was found."""
    assert machine_of("mcp-hub-fireblade-wsl-xport",
                      ["fireblade-wsl"]) == "fireblade-wsl"


def test_the_longest_machine_name_wins():
    """Given `vm-1` and `dev-vm-1`, an agent on the second matches both and
    exactly one answer is right."""
    assert machine_of("pm-dev-vm-1", ["vm-1", "dev-vm-1"]) == "dev-vm-1"


def test_an_unmatched_name_resolves_to_nothing_rather_than_guessing():
    assert machine_of("pm-some-other-box", ["dev-vm-1", "here"]) == ""


# ---- local seats -----------------------------------------------------------

def test_a_local_seat_lands_in_the_workspace_that_lists_its_folder():
    tree = _tree(
        roster=[{"agent": "alpha", "worktree": "/code/alpha", "klass": "squad"}],
        rows=[_ws("team", "here", ["/code/alpha"])],
    )
    team = tree["machines"][0]["workspaces"][0]
    assert [a["agent"] for a in team["agents"]] == ["alpha"]
    assert tree["machines"][0]["loose"] == []


def test_a_folder_listed_by_two_workspaces_puts_the_seat_in_both():
    """That is exactly where multi-squad membership comes from — the seat is
    in both, so showing it in one would be a choice the data does not make."""
    tree = _tree(
        roster=[{"agent": "alpha", "worktree": "/code/alpha", "klass": "squad"}],
        rows=[_ws("one", "here", ["/code/alpha"]),
              _ws("two", "here", ["/code/alpha"])],
    )
    homes = [w["name"] for w in tree["machines"][0]["workspaces"]
             if any(a["agent"] == "alpha" for a in w["agents"])]
    assert sorted(homes) == ["one", "two"]


def test_a_seat_in_no_workspace_is_shown_loose_not_dropped():
    tree = _tree(
        roster=[{"agent": "alpha", "worktree": "/code/alpha", "klass": "squad"}],
        rows=[_ws("team", "here", ["/code/elsewhere"])],
    )
    assert [a["agent"] for a in tree["machines"][0]["loose"]] == ["alpha"]
    assert [a["agent"] for a in walk_agents(tree)] == ["alpha"]


def test_a_folder_nested_under_a_listed_directory_still_matches():
    tree = _tree(
        roster=[{"agent": "alpha", "worktree": "/code/org/alpha", "klass": "s"}],
        rows=[_ws("team", "here", ["/code"])],
    )
    assert tree["machines"][0]["workspaces"][0]["agents"]


def test_a_relative_workspace_listing_resolves_against_its_own_file():
    """VSCode allows relative folder entries and hand-written workspace files
    use them freely; compared raw they never match anything."""
    tree = _tree(
        roster=[{"agent": "alpha", "worktree": "/home/me/Projects/alpha",
                 "klass": "squad"}],
        rows=[_ws("team", "here", ["alpha"])],
    )
    assert tree["machines"][0]["workspaces"][0]["agents"]


def test_local_listings_are_read_from_disk_not_from_the_hubs_copy():
    """This machine's disk is fresher than anything the hub was told, and
    unlike a remote row it is actually readable."""
    tree = _tree(
        roster=[{"agent": "alpha", "worktree": "/code/alpha", "klass": "squad"}],
        rows=[_ws("team", "here", ["/stale/path"])],
        listings_for=lambda path: ["/code/alpha"],
    )
    assert tree["machines"][0]["workspaces"][0]["agents"]


def test_the_board_state_rides_the_local_seat():
    tree = _tree(
        roster=[{"agent": "alpha", "worktree": "/a", "klass": "squad"}],
        board={"agents": {"alpha": {"state": "waiting", "waiting_seconds": 90,
                                    "next": {"hand": True}}}},
    )
    seat = tree["machines"][0]["loose"][0]
    assert seat["local"] is True and seat["state"] == "waiting"
    assert seat["hand"] is True


# ---- remote seats ----------------------------------------------------------

def test_a_remote_seat_is_placed_on_its_machine_with_presence_only():
    tree = _tree(
        fleet_agents=[{"name": "pm-dev-vm-1", "project": "org/pm",
                       "wakeable": True, "idle": True, "sessions": 1}],
        machines=["here", "dev-vm-1"],
    )
    remote = [m for m in tree["machines"] if m["machine"] == "dev-vm-1"][0]
    seat = remote["loose"][0]
    assert seat["local"] is False
    assert seat["wakeable"] is True and seat["state"] == "idle"
    # No board record can exist for it — there is no pane on that box.
    assert seat["rec"] is None and seat["hand"] is False


def test_a_remote_seat_lands_in_a_registered_workspace_that_lists_its_repo():
    tree = _tree(
        rows=[_ws("runtime", "dev-vm-1", ["/home/me/Projects/code/org/pm"])],
        fleet_agents=[{"name": "pm-dev-vm-1", "project": "org/pm",
                       "wakeable": True, "idle": False, "sessions": 1}],
        machines=["here", "dev-vm-1"],
    )
    remote = [m for m in tree["machines"] if m["machine"] == "dev-vm-1"][0]
    assert [a["agent"] for a in remote["workspaces"][0]["agents"]] \
        == ["pm-dev-vm-1"]
    assert remote["loose"] == []


def test_a_seat_already_in_the_local_roster_is_not_shown_twice():
    """This box's own agents appear in the fleet snapshot too. The roster is
    authoritative here, because only it carries real state."""
    tree = _tree(
        roster=[{"agent": "alpha-here", "worktree": "/a", "klass": "squad"}],
        fleet_agents=[{"name": "alpha-here", "project": "org/alpha",
                       "wakeable": True, "idle": True, "sessions": 1}],
    )
    seats = list(walk_agents(tree))
    assert [s["agent"] for s in seats] == ["alpha-here"]
    assert seats[0]["local"] is True


def test_a_seat_matching_no_known_machine_is_shown_not_dropped():
    tree = _tree(
        fleet_agents=[{"name": "pm-mystery-box", "project": "org/pm",
                       "wakeable": True, "idle": True, "sessions": 1}],
        machines=["here", "dev-vm-1"],
    )
    unknown = [m for m in tree["machines"] if m["unknown"]]
    assert len(unknown) == 1
    assert [a["agent"] for a in unknown[0]["loose"]] == ["pm-mystery-box"]


# ---- staleness -------------------------------------------------------------

def test_a_stale_snapshot_reports_unknown_rather_than_idle():
    """The exact inversion this guards: a daemon that stopped writing would
    otherwise make every remote seat read as a quiet, healthy idle."""
    tree = _tree(
        fleet_ts=NOW - FLEET_STALE_SECONDS - 1,
        fleet_agents=[{"name": "pm-dev-vm-1", "project": "org/pm",
                       "wakeable": True, "idle": True, "sessions": 1}],
        machines=["here", "dev-vm-1"],
    )
    remote = [m for m in tree["machines"] if m["machine"] == "dev-vm-1"][0]
    assert remote["stale"] is True
    assert remote["loose"][0]["state"] == "unknown"
    assert tree["fleet_stale"] is True


def test_a_fresh_snapshot_reports_the_state_it_carries():
    tree = _tree(
        fleet_ts=NOW - 10,
        fleet_agents=[{"name": "pm-dev-vm-1", "project": "org/pm",
                       "wakeable": True, "idle": False, "sessions": 1}],
        machines=["here", "dev-vm-1"],
    )
    remote = [m for m in tree["machines"] if m["machine"] == "dev-vm-1"][0]
    assert remote["stale"] is False
    assert remote["loose"][0]["state"] == "working"


def test_a_missing_snapshot_is_stale_not_fresh():
    """No file at all means ts=0. Treating that as "now" would make an absent
    instrument read as a perfect one."""
    tree = _tree(fleet_ts=0, machines=["here", "dev-vm-1"])
    assert tree["fleet_stale"] is True


def test_this_machine_is_never_marked_stale_by_a_remote_cache():
    """The local roster comes from `squad board --json`, not from the fleet
    snapshot, so a stale cache says nothing about this box."""
    tree = _tree(
        roster=[{"agent": "alpha", "worktree": "/a", "klass": "squad"}],
        fleet_ts=0,
    )
    assert tree["machines"][0]["local"] is True
    assert tree["machines"][0]["stale"] is False


# ---- order and shape -------------------------------------------------------

def test_this_machine_comes_first_and_unknown_comes_last():
    tree = _tree(
        roster=[{"agent": "a-here", "worktree": "/a", "klass": "squad"}],
        fleet_agents=[
            {"name": "b-aaa-box", "project": "o/b", "wakeable": True,
             "idle": True, "sessions": 1},
            {"name": "c-nowhere", "project": "o/c", "wakeable": True,
             "idle": True, "sessions": 1},
        ],
        machines=["aaa-box", "here"],
    )
    assert [m["machine"] for m in tree["machines"]] == ["here", "aaa-box", ""]


def test_an_enrolled_machine_with_nothing_on_it_gets_no_node():
    tree = _tree(machines=["here", "empty-box"])
    assert [m["machine"] for m in tree["machines"]] == ["here"]


def test_workspace_drift_and_presence_survive_the_join():
    rows = [
        _ws("feral", "here", [], registered=False),
        _ws("gone", "here", [], on_disk=False, path=""),
        _ws("live", "here", [], open_now=True),
    ]
    by_name = {w["name"]: w for w in _tree(rows=rows)["machines"][0]["workspaces"]}
    assert by_name["feral"]["drift"] is True
    assert by_name["gone"]["drift"] is True
    assert by_name["live"]["drift"] is False and by_name["live"]["open_now"]


def test_the_scoped_workspace_is_the_one_marked_here():
    rows = [_ws("mine", "here", []), _ws("other", "here", [])]
    tree = _tree(rows=rows,
                 scoped_to="/home/me/Projects/mine.code-workspace")
    by_name = {w["name"]: w for w in tree["machines"][0]["workspaces"]}
    assert by_name["mine"]["here"] is True
    assert by_name["other"]["here"] is False


def test_structure_key_ignores_a_state_tick_but_notices_a_new_seat():
    """The board polls every three seconds. If the key moved on every tick the
    tree would rebuild under the operator's cursor, which is the churn the
    live section's render key already exists to prevent."""
    roster = [{"agent": "alpha", "worktree": "/a", "klass": "squad"}]
    idle = _tree(roster=roster, board={"agents": {"alpha": {"state": "idle"}}})
    busy = _tree(roster=roster,
                 board={"agents": {"alpha": {"state": "waiting",
                                             "waiting_seconds": 300}}})
    assert structure_key(idle) == structure_key(busy)

    grown = _tree(roster=[*roster,
                          {"agent": "beta", "worktree": "/b", "klass": "squad"}])
    assert structure_key(grown) != structure_key(idle)
