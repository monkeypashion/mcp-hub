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
          listings_for=None, seats=(), machine_agents=None):
    return build_tree(
        roster=list(roster),
        board=board or {"agents": {}},
        workspaces={"rows": list(rows), "machines": list(machines),
                    "this_machine": this_machine},
        fleet={"ts": fleet_ts, "agents": list(fleet_agents)},
        this_machine=this_machine,
        scoped_to=scoped_to,
        listings_for=listings_for,
        seats=list(seats),
        machine_agents=machine_agents,
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


# ---- containers: a substrate BETWEEN the machine and its agents -------------
#
# The operator's own words: "I expected a container alongside the other
# machines, or underneath — underneath dev-vm-1 makes sense — and underneath
# the container the workspace and under that the agents." Underneath is right,
# and the reason it is not a MACHINE is load-bearing: no token, no edge, no
# ssh. The docker that runs it belongs to the box above.

def _seat(identity, machine, image="mcp-hub-seat:latest", **kw):
    s = {"identity": identity, "machine": machine, "repo": "", "folder": "",
         "launch_args": "", "class": "squad", "cloned_from": "",
         "spec": {"image": image} if image else {}}
    s.update(kw)
    return s


def test_a_container_seat_hangs_under_its_host_not_beside_it():
    t = _tree(seats=[_seat("seat-here", "here")])
    machines = [m["machine"] for m in t["machines"]]
    assert machines == ["here"], f"a container became its own machine: {machines}"
    box = t["machines"][0]["containers"][0]
    assert box["kind"] == "container"
    assert box["identity"] == "seat-here"
    assert box["image"] == "mcp-hub-seat:latest"


def test_a_worktree_seat_grows_no_container_node():
    """A seat with no image is not containerized. Inventing a node for it
    would put a substrate on a machine that does not have one."""
    t = _tree(seats=[_seat("plain-here", "here", image="")])
    assert t["machines"][0]["containers"] == []


def test_the_agent_inside_a_container_hangs_under_the_container():
    t = _tree(
        roster=[{"agent": "seat-here", "worktree": "/work/seat", "klass": "squad"}],
        seats=[_seat("seat-here", "here")],
    )
    box = t["machines"][0]["containers"][0]
    assert [a["agent"] for a in box["agents"]] == ["seat-here"]


def test_a_containerized_agent_is_not_ALSO_listed_under_a_host_workspace():
    """The duplicate rows the operator saw. Its mount point is listed by a host
    workspace, which is true and irrelevant: the agent runs in the container,
    and showing it in both says it is in two places."""
    t = _tree(
        roster=[{"agent": "seat-here", "worktree": "/work/seat", "klass": "squad"}],
        rows=[_ws("squad", "here", ["/work"])],       # /work CONTAINS /work/seat
        seats=[_seat("seat-here", "here")],
    )
    ws = t["machines"][0]["workspaces"][0]
    assert [a["agent"] for a in ws["agents"]] == [], \
        "the seat is claimed by a host workspace as well as its container"
    assert len(list(walk_agents(t))) == 1, "shown twice"


def test_a_REMOTE_containerized_agent_stops_guessing_by_repo_name():
    """Remote rows have no worktree, so they are matched by repo BASENAME —
    and a box with three clones of one repo attributes all of them to every
    workspace listing any of them (dev-vm-1 has three `mcp-hub` folders). The
    seat record carries the real substrate, so a containerized agent opts out
    of the guess entirely.
    """
    t = _tree(
        machines=["here", "box"], this_machine="here",
        rows=[_ws("one", "box", ["/a/mcp-hub"]), _ws("two", "box", ["/b/mcp-hub"])],
        fleet_agents=[{"name": "mcp-hub-seat-box", "project": "org/mcp-hub",
                       "wakeable": True, "idle": True, "sessions": 1, "next": ""}],
        seats=[_seat("mcp-hub-seat-box", "box")],
    )
    box = [m for m in t["machines"] if m["machine"] == "box"][0]
    assert [w["agents"] for w in box["workspaces"]] == [[], []], \
        "the guess still claimed it"
    assert [a["agent"] for a in box["containers"][0]["agents"]] \
        == ["mcp-hub-seat-box"]


def test_a_container_the_hub_knows_with_nobody_in_it_still_shows():
    """Zero agents is a fact worth seeing — a placement that never came up
    looks exactly like a container that was never declared, otherwise."""
    t = _tree(seats=[_seat("empty-here", "here")])
    box = t["machines"][0]["containers"][0]
    assert box["agents"] == []


def test_a_container_puts_its_host_on_the_map_even_with_no_workspaces():
    """A box known ONLY for running a container must not be dropped by the
    'enrolled box with nothing on it' rule."""
    t = _tree(machines=["here"], this_machine="here",
              seats=[_seat("seat-box", "box")])
    assert "box" in [m["machine"] for m in t["machines"]]


def test_containers_are_STRUCTURE_so_a_new_one_forces_a_rebuild():
    """The poll relabels in place and only restructures when structure_key
    moves. Omit containers from it and a seat that comes up never gets a node,
    because the tree relabels forever."""
    before = structure_key(_tree())
    after = structure_key(_tree(seats=[_seat("seat-here", "here")]))
    assert before != after


def test_the_agent_inside_a_container_is_structure_too():
    empty = structure_key(_tree(seats=[_seat("seat-here", "here")]))
    filled = structure_key(_tree(
        roster=[{"agent": "seat-here", "worktree": "/w", "klass": "squad"}],
        seats=[_seat("seat-here", "here")]))
    assert empty != filled


# ---- where a container's work actually lives -------------------------------

def test_seat_folder_prefers_what_the_hub_was_told():
    from mcp_hub.fleet_tree import seat_folder
    assert seat_folder(_seat("s", "here", folder="/declared",
                             spec={"image": "i", "volumes": ["/mounted:/w"]})) \
        == "/declared"


def test_seat_folder_falls_back_to_the_bind_mount():
    from mcp_hub.fleet_tree import seat_folder
    assert seat_folder(_seat("s", "here",
                             spec={"image": "i", "volumes": ["/mounted:/w"]})) \
        == "/mounted"


def test_seat_folder_ignores_a_NAMED_volume():
    """`seat-memory-dev-vm-1:/home/seat/.claude` is a name, not a path. Read as
    a worktree it would attribute the seat to any workspace listing a folder
    of that name."""
    from mcp_hub.fleet_tree import seat_folder
    assert seat_folder(_seat("s", "here",
                             spec={"image": "i",
                                   "volumes": ["seat-memory:/home/seat/.claude"]})) \
        == ""


# ---- a scoped board cannot claim other workspaces are empty -----------------

def test_other_local_workspaces_are_marked_OUT_OF_SCOPE_not_empty():
    """A scoped board is handed a roster filtered to its own workspace, so
    every other local workspace draws with no children. That is not a
    measurement of emptiness and must not be presented as one — `xport` and
    `xport2` each hold a real agent and rendered as bare rows.
    """
    here = _ws("windows", "here", ["/w/windows"])
    other = _ws("xport", "here", ["/w/xport"])
    t = _tree(rows=[here, other], scoped_to=here["path"],
              roster=[{"agent": "a-here", "worktree": "/w/windows",
                       "klass": "squad"}])
    by_name = {w["name"]: w for w in t["machines"][0]["workspaces"]}
    assert by_name["windows"]["in_scope"] is True
    assert by_name["xport"]["in_scope"] is False


def test_an_unscoped_board_measures_every_local_workspace():
    """No --workspace means the whole roster, so every row IS a measurement."""
    t = _tree(rows=[_ws("windows", "here", ["/w/windows"]),
                    _ws("xport", "here", ["/w/xport"])], scoped_to=None)
    assert all(w["in_scope"] is True for w in t["machines"][0]["workspaces"])


def test_a_REMOTE_workspace_is_never_out_of_scope():
    """Remote agents come from the fleet snapshot, which no scope filters.
    Marking them out-of-scope would be a warning about nothing."""
    here = _ws("windows", "here", ["/w/windows"])
    t = _tree(machines=["here", "box"], this_machine="here",
              rows=[here, _ws("squad", "box", ["/b/repo"])],
              scoped_to=here["path"])
    box = [m for m in t["machines"] if m["machine"] == "box"][0]
    assert box["workspaces"][0]["in_scope"] is True


# ---- clones of one repo on one box: refuse to guess -------------------------

def _remote(name, project):
    return {"name": name, "project": project, "wakeable": True, "idle": True,
            "sessions": 1, "next": ""}


def test_two_clones_of_one_repo_make_the_workspace_UNKNOWABLE():
    """dev-vm-1 has three `mcp-hub` folders. A remote row has no worktree, so
    the basename matched all of them and the agent appeared under every
    workspace listing any clone — one of which belonged to a DIFFERENT agent.
    Two distinct paths means we cannot tell which clone this is."""
    t = _tree(
        machines=["here", "box"], this_machine="here",
        rows=[_ws("squad", "box", ["/code/org/mcp-hub"]),
              _ws("general", "box", ["/general/mcp-hub"])],
        fleet_agents=[_remote("mcp-hub-box", "org/mcp-hub")],
    )
    box = [m for m in t["machines"] if m["machine"] == "box"][0]
    assert [w["agents"] for w in box["workspaces"]] == [[], []], \
        "still claimed by a workspace it may not be in"
    assert [a["agent"] for a in box["loose"]] == ["mcp-hub-box"]
    assert box["loose"][0]["ambiguous"] == ["/code/org/mcp-hub",
                                            "/general/mcp-hub"]


def test_the_SAME_path_in_several_workspaces_is_membership_not_ambiguity():
    """`squad` and `runtime` both list `code/monkeypashion/mcp-hub`. That is
    the multi-membership local rows already show, and refusing it would lose a
    true fact to avoid a false one."""
    t = _tree(
        machines=["here", "box"], this_machine="here",
        rows=[_ws("squad", "box", ["/code/org/mcp-hub"]),
              _ws("runtime", "box", ["/code/org/mcp-hub"])],
        fleet_agents=[_remote("mcp-hub-box", "org/mcp-hub")],
    )
    box = [m for m in t["machines"] if m["machine"] == "box"][0]
    assert [[a["agent"] for a in w["agents"]] for w in box["workspaces"]] \
        == [["mcp-hub-box"], ["mcp-hub-box"]]
    assert box["loose"] == []
    assert box["workspaces"][0]["agents"][0]["ambiguous"] == []


def test_one_matching_workspace_still_resolves():
    t = _tree(
        machines=["here", "box"], this_machine="here",
        rows=[_ws("squad", "box", ["/code/org/pm"])],
        fleet_agents=[_remote("pm-box", "org/pm")],
    )
    box = [m for m in t["machines"] if m["machine"] == "box"][0]
    assert [a["agent"] for a in box["workspaces"][0]["agents"]] == ["pm-box"]


# ---- never written is not gone stale ---------------------------------------

def test_a_snapshot_never_written_is_not_reported_as_one_that_stopped():
    """After a reboot the daemon that writes the snapshot does not exist until
    an agent session starts. A board opened before that read `not reporting`
    across the fleet, when the truth was that nothing had looked yet."""
    t = _tree(machines=["here", "box"], this_machine="here", fleet_ts=0,
              fleet_agents=[_remote("pm-box", "org/pm")])
    assert t["fleet_never"] is True
    box = [m for m in t["machines"] if m["machine"] == "box"][0]
    assert box["never"] is True


def test_a_snapshot_that_went_stale_is_not_called_never_written():
    t = _tree(machines=["here", "box"], this_machine="here",
              fleet_ts=NOW - FLEET_STALE_SECONDS - 1,
              fleet_agents=[_remote("pm-box", "org/pm")])
    assert t["fleet_never"] is False
    box = [m for m in t["machines"] if m["machine"] == "box"][0]
    assert box["stale"] is True and box["never"] is False


# ---- exact remote attribution: the machine reports where its agents live ----

def test_a_reported_worktree_places_a_remote_agent_EXACTLY():
    """Two clones of one repo, and the machine has said which folder this
    agent is in. The basename guess would claim both workspaces; the reported
    worktree claims only the one that contains it."""
    t = _tree(
        machines=["here", "box"], this_machine="here",
        rows=[_ws("squad", "box", ["/code/org/mcp-hub"]),
              _ws("general", "box", ["/general/mcp-hub"])],
        fleet_agents=[_remote("mcp-hub-box", "org/mcp-hub")],
        machine_agents={"box": [{"agent": "mcp-hub-box",
                                 "worktree": "/code/org/mcp-hub"}]},
    )
    box = [m for m in t["machines"] if m["machine"] == "box"][0]
    by_name = {w["name"]: [a["agent"] for a in w["agents"]]
               for w in box["workspaces"]}
    assert by_name == {"squad": ["mcp-hub-box"], "general": []}
    assert box["loose"] == []


def test_a_reported_worktree_still_gives_MULTI_membership():
    """The exact path listed by two workspaces is in both — the same rule
    local rows follow. Exactness must not cost the true fact."""
    t = _tree(
        machines=["here", "box"], this_machine="here",
        rows=[_ws("squad", "box", ["/code/org/mcp-hub"]),
              _ws("runtime", "box", ["/code/org/mcp-hub"])],
        fleet_agents=[_remote("mcp-hub-box", "org/mcp-hub")],
        machine_agents={"box": [{"agent": "mcp-hub-box",
                                 "worktree": "/code/org/mcp-hub"}]},
    )
    box = [m for m in t["machines"] if m["machine"] == "box"][0]
    assert [[a["agent"] for a in w["agents"]] for w in box["workspaces"]] \
        == [["mcp-hub-box"], ["mcp-hub-box"]]


def test_a_machine_that_reported_NO_roster_keeps_the_old_behaviour():
    """The degrade path. An edge that has not been upgraded reports nothing,
    and that machine must keep matching by repo name — not empty out."""
    t = _tree(
        machines=["here", "box"], this_machine="here",
        rows=[_ws("squad", "box", ["/code/org/pm"])],
        fleet_agents=[_remote("pm-box", "org/pm")],
        machine_agents={},           # nobody has reported
    )
    box = [m for m in t["machines"] if m["machine"] == "box"][0]
    assert [a["agent"] for a in box["workspaces"][0]["agents"]] == ["pm-box"]


def test_one_machine_reporting_does_not_change_another():
    """Per-machine fallback: an upgraded edge on one box must not switch the
    other box to exact matching it cannot supply."""
    t = _tree(
        machines=["here", "new", "old"], this_machine="here",
        rows=[_ws("a", "new", ["/code/org/pm"]), _ws("b", "old", ["/code/org/pm"])],
        fleet_agents=[_remote("pm-new", "org/pm"), _remote("pm-old", "org/pm")],
        machine_agents={"new": [{"agent": "pm-new", "worktree": "/elsewhere"}]},
    )
    by_machine = {m["machine"]: m for m in t["machines"]}
    # `new` reported /elsewhere, which workspace `a` does not list → loose
    assert [a["agent"] for a in by_machine["new"]["loose"]] == ["pm-new"]
    # `old` reported nothing → basename match, unchanged
    assert [a["agent"] for a in by_machine["old"]["workspaces"][0]["agents"]] \
        == ["pm-old"]


def test_an_agent_missing_from_a_reported_roster_falls_back_not_vanishes():
    """Online but not in squad.conf — registered by hand, say. It must not be
    dropped, and it must not borrow another agent's worktree."""
    t = _tree(
        machines=["here", "box"], this_machine="here",
        rows=[_ws("squad", "box", ["/code/org/pm"])],
        fleet_agents=[_remote("pm-box", "org/pm")],
        machine_agents={"box": [{"agent": "someone-else", "worktree": "/x"}]},
    )
    box = [m for m in t["machines"] if m["machine"] == "box"][0]
    assert [a["agent"] for a in box["workspaces"][0]["agents"]] == ["pm-box"]


def test_a_reported_worktree_in_no_workspace_goes_loose_without_ambiguity():
    t = _tree(
        machines=["here", "box"], this_machine="here",
        rows=[_ws("squad", "box", ["/code/org/pm"])],
        fleet_agents=[_remote("pm-box", "org/pm")],
        machine_agents={"box": [{"agent": "pm-box", "worktree": "/somewhere/else"}]},
    )
    box = [m for m in t["machines"] if m["machine"] == "box"][0]
    assert [a["agent"] for a in box["loose"]] == ["pm-box"]
    assert box["loose"][0]["ambiguous"] == [], \
        "it is not ambiguous — we know exactly where it is, and it is nowhere"


# ---- roster-only remote agents: enrolled there, absent from the hub --------
#
# A remote row used to exist only where the hub had PRESENCE, so an agent alive
# on its machine with a dropped binding rendered as nothing — the one state
# worth seeing. Measured 2026-08-06: 21 of dev-vm-1's 31 roster agents invisible.

def _pushed(**machines):
    return {m: [{"agent": a, "worktree": w} for a, w in rows.items()]
            for m, rows in machines.items()}


def _by_name(tree):
    return {a["agent"]: a for a in walk_agents(tree)}


def test_a_remote_agent_off_the_hub_is_SHOWN_not_dropped():
    t = _tree(machines=("here", "far"),
              machine_agents=_pushed(far={"ghost-far": "/w/ghost"}))
    got = _by_name(t)
    assert "ghost-far" in got, "an agent its machine reports must not vanish"
    assert got["ghost-far"]["off_hub"] is True


def test_it_claims_no_liveness_it_did_not_measure():
    """The roster push carries no pane state. `stopped` would be an invention —
    the row exists to say the hub has lost it, and nothing more."""
    g = _by_name(_tree(machines=("here", "far"),
                       machine_agents=_pushed(far={"ghost-far": "/w/g"})))["ghost-far"]
    assert g["state"] == "" and g["wakeable"] is False
    assert g["stale"] is False, "staleness is a claim about the fleet snapshot"


def test_hub_presence_WINS_over_the_roster_copy():
    """An agent the hub can see is drawn from presence WITH its real state; the
    roster copy must not add a second, thinner row for it."""
    t = _tree(machines=("here", "far"),
              fleet_agents=[{"name": "live-far", "wakeable": True,
                             "state": "idle"}],
              machine_agents=_pushed(far={"live-far": "/w/live"}))
    assert len([a for a in walk_agents(t) if a["agent"] == "live-far"]) == 1
    got = _by_name(t)["live-far"]
    assert not got.get("off_hub") and got["wakeable"] is True


def test_this_machine_is_left_to_its_own_roster():
    """The local roster is the authority here and draws rows WITH pane state.
    Resurrecting a local name from the hub's older copy would add a second,
    thinner row for an agent that is right there to be read."""
    t = _tree(machine_agents=_pushed(here={"gone-here": "/w/gone"}))
    assert "gone-here" not in _by_name(t)


def test_an_off_hub_agent_lands_in_its_workspace_by_REAL_path():
    t = _tree(
        machines=("here", "far"),
        rows=[{"machine": "far", "name": "ws", "path": "/p/ws.code-workspace",
               "listings": ["/w"], "registered": True, "on_disk": True}],
        machine_agents=_pushed(far={"ghost-far": "/w/ghost"}),
    )
    ws = [w for m in t["machines"] for w in m["workspaces"] if w["name"] == "ws"][0]
    assert [a["agent"] for a in ws["agents"]] == ["ghost-far"]
