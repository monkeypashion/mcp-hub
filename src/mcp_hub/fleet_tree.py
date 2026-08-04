"""One navigable picture of the fleet: machines → workspaces → agents.

This replaces the board's two separate surfaces — a flat roster of THIS
machine's agents on the left, and a `w` view of every workspace everywhere —
with a single tree. They were always two projections of one structure; the
operator had to hold the join in their head.

The join is the whole job, and it is done here rather than in the widget so it
can be tested without a terminal:

    machine    every box the fleet knows (workspace rows ∪ enrolled machines
               ∪ this one), THIS one first because it is the one you can act on
    workspace  a .code-workspace file, with its three truth columns intact
    agent      a seat, attributed to the workspace whose folders contain it

Two grades of agent, and the difference is deliberately visible rather than
smoothed over:

  LOCAL   in this machine's squad roster. `squad board --json` scraped its
          pane, so state, model, context and waiting-time are all real.
  REMOTE  known only from ~/.mcp-hub/fleet-board.json — the daemons' fleet
          snapshot. That carries presence (wakeable, idle, sessions, project,
          bio `next:`) and nothing else. There is no pane to scrape on another
          box, so a remote row is THINNER, and pretending otherwise would be
          the "delivered live" mistake in a new costume.

Staleness is a first-class answer. fleet-board.json is a cache, and a cache
that has stopped being written must read as "not reporting", never as a fleet
that has gone quiet — so past FLEET_STALE_SECONDS every remote state becomes
`unknown` and the machine says so.

Nothing is dropped. An agent whose name matches no known machine lands under
"(machine unknown)" rather than vanishing, for the same reason
`discover_workspaces` reports files it cannot parse.
"""

from __future__ import annotations

import posixpath
from typing import Any, Callable

# Same 5-minute window board_data applies to the decisions cache: an
# instrument that is not reporting must not be read as a measurement.
FLEET_STALE_SECONDS = 300.0

UNKNOWN_MACHINE = ""


def machine_of(agent: str, machines: list[str]) -> str:
    """Which box an agent name belongs to, or "" if nothing matches.

    Identity is `<repo>-<hostname>`, so the machine is a SUFFIX — except when
    transport has added a per-worktree suffix, and `mcp-hub-fireblade-wsl-xport`
    is a real name on a real box. So the test is containment of `-<machine>`,
    not endswith, and the LONGEST match wins: given machines `vm-1` and
    `dev-vm-1`, an agent on the latter matches both and only one is right.
    """
    best = ""
    for m in machines:
        if not m:
            continue
        if agent == m or f"-{m}" in agent:
            if len(m) > len(best):
                best = m
    return best


def _resolve(listing: str, ws_path: str) -> str:
    """A workspace folder entry as an absolute path.

    VSCode allows relative folder paths and hand-written workspace files use
    them freely; compared raw against an absolute worktree they never match,
    which would put every agent in "not in a workspace".
    """
    listing = listing.rstrip("/")
    if not listing:
        return ""
    if listing.startswith("/") or listing.startswith("~"):
        return listing
    if not ws_path:
        return listing
    return posixpath.normpath(posixpath.join(posixpath.dirname(ws_path), listing))


def _contains(listing: str, worktree: str) -> bool:
    if not listing or not worktree:
        return False
    listing = listing.rstrip("/")
    worktree = worktree.rstrip("/")
    return worktree == listing or worktree.startswith(listing + "/")


def _repo_of(project: str, agent: str, machine: str) -> str:
    """The repo directory an agent lives in.

    `project` is `<org>/<repo>` and is the honest source. Falling back to the
    name means stripping the machine suffix, which is right often enough to be
    useful and is only ever used to MATCH — never displayed as a fact.
    """
    if project and "/" in project:
        return project.rsplit("/", 1)[-1]
    if machine and f"-{machine}" in agent:
        return agent.split(f"-{machine}", 1)[0]
    return agent


def _remote_state(entry: dict[str, Any], stale: bool) -> str:
    if stale:
        return "unknown"
    return "idle" if entry.get("idle", True) else "working"


def build_tree(
    *,
    roster: list[dict[str, Any]],
    board: dict[str, Any],
    workspaces: dict[str, Any],
    fleet: dict[str, Any],
    this_machine: str,
    scoped_to: str | None = None,
    listings_for: Callable[[str], list[str]] | None = None,
    now: float,
) -> dict[str, Any]:
    """Merge roster + board + workspace registry + fleet snapshot into a tree.

    `fleet` is the raw fleet-board.json body ({"ts", "agents": [...]}) — passed
    whole rather than pre-filtered so its timestamp travels with its contents
    and staleness cannot be lost on the way in.

    `listings_for` reads a LOCAL workspace file's folder list. Remote rows use
    the listings the hub holds from their registration, because no local reader
    can see another machine's disk.
    """
    live = board.get("agents") or {}
    rows = workspaces.get("rows") or []
    machines = list(workspaces.get("machines") or [])
    for r in rows:
        if r.get("machine") and r["machine"] not in machines:
            machines.append(r["machine"])
    if this_machine and this_machine not in machines:
        machines.append(this_machine)

    fleet_ts = float(fleet.get("ts") or 0)
    stale = (now - fleet_ts) > FLEET_STALE_SECONDS if fleet_ts else True
    fleet_agents = [
        a for a in (fleet.get("agents") or [])
        if isinstance(a, dict) and a.get("name")
    ]

    # -- workspace shells, per machine ------------------------------------
    ws_nodes: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        listings = list(r.get("listings") or [])
        if r["machine"] == this_machine and r.get("path") and listings_for:
            # This machine's disk is fresher than anything the hub was told,
            # and unlike a remote row it is actually readable.
            try:
                listings = listings_for(r["path"]) or listings
            except Exception:  # noqa: BLE001 — an unreadable file is not a crash
                pass
        node = {
            "kind": "workspace",
            "key": f"w:{r['machine']}/{r['name']}",
            "name": r["name"],
            "machine": r["machine"],
            "path": r.get("path", ""),
            "registered": r.get("registered"),
            "on_disk": bool(r.get("on_disk")),
            "open_now": bool(r.get("open_now")),
            "here": bool(scoped_to) and r.get("path") == scoped_to,
            "error": r.get("error", ""),
            "squad": r.get("squad", ""),
            "folders": r.get("folders"),
            "drift": (r.get("registered") is False) or not r.get("on_disk"),
            "listings": [_resolve(x, r.get("path", "")) for x in listings],
            "agents": [],
        }
        ws_nodes.setdefault(r["machine"], []).append(node)

    # -- local agents: the roster is authoritative for this box ------------
    placed_names: set[str] = set()
    loose: dict[str, list[dict[str, Any]]] = {}
    local_ws = ws_nodes.get(this_machine, [])
    for ix, a in enumerate(roster):
        name = a.get("agent", "")
        if not name:
            continue
        placed_names.add(name)
        rec = live.get(name) or {}
        node = {
            "kind": "agent",
            "key": f"a:{this_machine}/{name}",
            "agent": name,
            "machine": this_machine,
            "local": True,
            "roster_ix": ix,
            "worktree": a.get("worktree", ""),
            "klass": a.get("klass", ""),
            "state": rec.get("state", ""),
            "hand": bool((rec.get("next") or {}).get("hand"))
            or rec.get("state") == "waiting",
            "rec": rec or None,
        }
        # A folder listed by three workspaces belongs to three workspaces —
        # that is exactly where multi-squad membership comes from, so the seat
        # appears under each rather than under whichever matched first.
        homes = [w for w in local_ws
                 if any(_contains(x, node["worktree"]) for x in w["listings"])]
        if homes:
            for w in homes:
                w["agents"].append(dict(node, key=f"a:{w['key']}/{name}"))
        else:
            loose.setdefault(this_machine, []).append(node)

    # -- remote agents: presence only, and only what the snapshot says -----
    for entry in fleet_agents:
        name = entry["name"]
        if name in placed_names:
            continue          # the roster already showed it, with real data
        placed_names.add(name)
        m = machine_of(name, machines)
        node = {
            "kind": "agent",
            "key": f"a:{m}/{name}",
            "agent": name,
            "machine": m,
            "local": False,
            "roster_ix": None,
            "worktree": "",
            "klass": "",
            "project": entry.get("project", ""),
            "wakeable": bool(entry.get("wakeable")),
            "sessions": int(entry.get("sessions") or 0),
            "next": str(entry.get("next") or ""),
            "state": _remote_state(entry, stale),
            "stale": stale,
            "hand": False,     # a hand is a board fact; no pane, no claim
            "rec": None,
        }
        repo = _repo_of(entry.get("project", ""), name, m)
        homes = [
            w for w in ws_nodes.get(m, [])
            if any(posixpath.basename(x) == repo for x in w["listings"])
        ]
        if homes:
            for w in homes:
                w["agents"].append(dict(node, key=f"a:{w['key']}/{name}"))
        else:
            loose.setdefault(m, []).append(node)

    # -- assemble ----------------------------------------------------------
    order = sorted(
        set(machines) | set(ws_nodes) | set(loose),
        key=lambda m: (m != this_machine, m == UNKNOWN_MACHINE, m),
    )
    out: list[dict[str, Any]] = []
    for m in order:
        wss = sorted(ws_nodes.get(m, []), key=lambda w: w["name"])
        for w in wss:
            w["agents"].sort(key=lambda a: (a["roster_ix"] is None, a["agent"]))
        lo = sorted(loose.get(m, []), key=lambda a: (a["roster_ix"] is None,
                                                     a["roster_ix"] or 0,
                                                     a["agent"]))
        if not wss and not lo and m != this_machine:
            continue          # an enrolled box with nothing on it: no node
        out.append({
            "kind": "machine",
            "key": f"m:{m}",
            "machine": m,
            "local": m == this_machine,
            "unknown": m == UNKNOWN_MACHINE,
            "stale": stale and m != this_machine,
            "workspaces": wss,
            "loose": lo,
            "agent_count": sum(len(w["agents"]) for w in wss) + len(lo),
            "open_count": sum(1 for w in wss if w["open_now"] or w["here"]),
            "drift_count": sum(1 for w in wss if w["drift"]),
        })
    return {
        "machines": out,
        "this_machine": this_machine,
        "fleet_stale": stale,
        "fleet_ts": fleet_ts,
        "note": workspaces.get("note", ""),
    }


def walk_agents(tree: dict[str, Any]):
    """Every agent node, in the order the tree shows them."""
    for m in tree.get("machines", []):
        for w in m["workspaces"]:
            yield from w["agents"]
        yield from m["loose"]


def structure_key(tree: dict[str, Any]) -> tuple:
    """What the tree's SHAPE is, ignoring anything that merely ticks.

    The board polls every 3 seconds. Rebuilding the tree on each poll would
    collapse the operator's expansions and move their cursor out from under
    them — the same churn the live section's render key exists to prevent,
    one level up.
    """
    return tuple(
        (m["key"], tuple(
            (w["key"], tuple(a["key"] for a in w["agents"]))
            for w in m["workspaces"]
        ), tuple(a["key"] for a in m["loose"]))
        for m in tree.get("machines", [])
    )
