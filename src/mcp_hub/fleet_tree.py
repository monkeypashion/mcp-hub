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


def container_image(seat: dict[str, Any]) -> str:
    """The image a seat runs as, or "" if it is not containerized.

    The seat record's `spec` is substrate-specific by design (one column, not
    one per substrate), so the PRESENCE of an image is what makes a seat a
    container. A worktree seat has no image and must not sprout a container
    node — that would invent a substrate the machine does not have.
    """
    spec = seat.get("spec")
    if not isinstance(spec, dict):
        return ""
    return str(spec.get("image") or "")


def container_members(seat: dict[str, Any]) -> list[str]:
    """Who lives in this container — the identities that hang under its node.

    A POD (`spec.agents`) holds several; a 1:1 seat holds exactly itself. Both
    go through this so the single-seat case is the N=1 case of ONE rule rather
    than a branch that can drift from the other.

    Order is the manifest's, which is the order the operator declared the
    squad in and the order the pod's own workspace file lists.
    """
    ident = str(seat.get("identity") or "")
    spec = seat.get("spec")
    rows = spec.get("agents") if isinstance(spec, dict) else None
    if isinstance(rows, list) and rows:
        out = [str((r or {}).get("identity") or "") for r in rows
               if isinstance(r, dict)]
        return [x for x in out if x]
    return [ident] if ident else []


def container_session(seat: dict[str, Any], agent: str) -> str:
    """The tmux session `docker exec … attach -t` must name.

    A pod names its sessions for the agents; a 1:1 container has the single
    session `seat`, which is what the image has always created and what the
    launch dance answers into. Getting this wrong produces an attach command
    that fails against a perfectly healthy container.
    """
    spec = seat.get("spec")
    rows = spec.get("agents") if isinstance(spec, dict) else None
    return agent if isinstance(rows, list) and rows else "seat"


def container_attach(node: dict[str, Any]) -> list[tuple[str, str]]:
    """[(label, command)] — how the operator gets INTO this container.

    One line per inhabitant. A pod's sessions are named for its agents, so a
    single `-t seat` line would be a command that FAILS against a perfectly
    healthy pod — and an operator reading a failing attach concludes the pod
    is broken rather than the label.

    Pure so it can be tested without a terminal: the rule is the interesting
    part, and it was previously computed inside widget construction where
    nothing could reach it.
    """
    ident = str(node.get("identity") or "")
    members = list(node.get("members") or ([ident] if ident else []))
    pod = members != [ident]
    out = []
    for member in members:
        session = member if pod else "seat"
        label = "Attach" if len(members) == 1 else f"Attach {member}"
        out.append((label, f"docker exec -it {ident} tmux attach -t {session}"))
    return out


def seat_folder(seat: dict[str, Any]) -> str:
    """The HOST path a container seat's work lives at, or "".

    `folder` when the hub was told one; otherwise the first bind mount, which
    is the only other place the fact exists. NAMED volumes are skipped: the
    memory volume is a name, not a path, and reading it as a worktree would
    attribute the seat to whatever workspace happened to contain a folder of
    that name.
    """
    if seat.get("folder"):
        return str(seat["folder"]).rstrip("/")
    spec = seat.get("spec")
    if not isinstance(spec, dict):
        return ""
    for vol in spec.get("volumes") or []:
        host = str(vol).partition(":")[0]
        if host.startswith("/"):
            return host.rstrip("/")
    return ""


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


EDGE_STALE_SECONDS = 120.0  # 4 missed 30s timer passes — deliberately tighter
# than FLEET_STALE_SECONDS; the edge has a doorbell AND a timer behind it.


def _edge_state(info: dict | None, now: float) -> str | None:
    """One derived verdict from the edge's self-report.

    None    — no machine record at all (hub unreachable / unknown machine):
              NO CLAIM, which must never render as either healthy or broken.
    never   — enrolled machine, no edge has ever reported. After enrolment
              this is the ordinary first state, not an alarm.
    stale   — the instrument stopped being written; the LAST result may still
              ride in edge_info but a stopped instrument must not be read as
              a measurement.
    failed  — a fresh pass reported its own failure.
    ok      — a fresh pass reported success.
    """
    if not info:
        return None
    ts = info.get("edge_last_run")
    if not ts:
        return "never"
    if (now - float(ts)) > EDGE_STALE_SECONDS:
        return "stale"
    result = (info.get("edge_result") or {}).get("result")
    return "failed" if result == "failed" else "ok"


def build_tree(
    *,
    roster: list[dict[str, Any]],
    board: dict[str, Any],
    workspaces: dict[str, Any],
    fleet: dict[str, Any],
    this_machine: str,
    scoped_to: str | None = None,
    listings_for: Callable[[str], list[str]] | None = None,
    placements: list[dict[str, Any]] | None = None,
    seats: list[dict[str, Any]] | None = None,
    machine_agents: dict[str, list[dict[str, str]]] | None = None,
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
    # Seat identity IS the agent name, by construction — the hub assigns it as
    # `<repo>-<machine>`, the same rule the derived identity uses. So a
    # placement attaches to a row by name and needs no second mapping to drift
    # out of step.
    by_seat = {p["seat"]: p for p in (placements or []) if p.get("seat")}
    machines = list(workspaces.get("machines") or [])
    for r in rows:
        if r.get("machine") and r["machine"] not in machines:
            machines.append(r["machine"])
    for s in seats or []:
        if s.get("machine") and s["machine"] not in machines:
            machines.append(s["machine"])
    if this_machine and this_machine not in machines:
        machines.append(this_machine)

    # {machine: {agent: worktree}} — the API hands back a LIST per machine so
    # the wire format stays additive; the lookup is built once here rather than
    # scanned per agent.
    pushed_of: dict[str, dict[str, dict[str, Any]]] = {
        mach: {a["agent"]: a
               for a in rows if isinstance(a, dict) and a.get("agent")}
        for mach, rows in (machine_agents or {}).items()
        if isinstance(rows, list)
    }
    worktree_of: dict[str, dict[str, str]] = {
        mach: {name: (a.get("worktree") or "") for name, a in rows.items()}
        for mach, rows in pushed_of.items()
    }

    fleet_ts = float(fleet.get("ts") or 0)
    stale = (now - fleet_ts) > FLEET_STALE_SECONDS if fleet_ts else True
    # NEVER WRITTEN is not the same fact as GONE STALE, and after a reboot it
    # is the ordinary one: the snapshot's writer is the heartbeat daemon, which
    # only exists once an agent session starts. A board opened on a freshly
    # booted box read `not reporting` across the whole fleet when the truth was
    # that nothing on THIS box had looked yet (fireblade-wsl, 07:08 board vs
    # 07:30 first daemon). Same rendering for both told the operator the fleet
    # was down.
    never = not fleet_ts
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
            # Whether this board can SEE the agents in here at all. A scoped
            # board is handed a roster filtered to its own workspace, so every
            # OTHER local workspace draws with no children — and an empty node
            # reads "no agents", which is a measurement nobody took. Remote
            # rows are unaffected: their agents come from the fleet snapshot,
            # which no scope filters.
            "in_scope": (r["machine"] != this_machine
                         or not scoped_to
                         or r.get("path") == scoped_to),
            # Filled in after the attribution passes, once the containers
            # actually hold their agents. See "workspaces whose agents live in
            # containers" below.
            "container_ids": [],
            "in_containers": 0,
            "agents": [],
        }
        ws_nodes.setdefault(r["machine"], []).append(node)

    # -- containers: a substrate BETWEEN the machine and its agents --------
    #
    # A container is not a machine. It has no token, no edge, no ssh — the
    # docker that runs it belongs to its host, and its identity derives from
    # that host's name. So it hangs UNDER the box, beside that box's
    # workspaces, and an agent inside it hangs under the container rather than
    # under a host workspace.
    #
    # That last part is also the only honest fix for the duplicate rows. A
    # remote agent with no worktree is matched by repo BASENAME, and a box
    # with three clones of one repo (dev-vm-1 has three `mcp-hub` folders)
    # attributes all three agents to every workspace listing any of them. The
    # seat record carries the real folder, so a containerized agent stops
    # guessing entirely.
    containers: dict[str, list[dict[str, Any]]] = {}
    by_identity: dict[str, dict[str, Any]] = {}
    for s in seats or []:
        ident = str(s.get("identity") or "")
        image = container_image(s)
        if not ident or not image:
            continue          # a worktree seat is not a container
        m = str(s.get("machine") or "") or machine_of(ident, machines)
        members = container_members(s)
        node = {
            "kind": "container",
            "key": f"c:{m}/{ident}",
            "identity": ident,
            # The tree's agent-shaped lookups key on this. A POD is NOT an
            # agent — no marker, no registration, nothing on the hub by that
            # name — so it carries no agent name and anything asking gets an
            # honest blank rather than a container's name masquerading as one.
            "agent": "" if len(members) > 1 or members != [ident] else ident,
            "members": members,
            "machine": m,
            "local": m == this_machine,
            "image": image,
            "folder": seat_folder(s),
            "klass": str(s.get("class") or ""),
            "placement": by_seat.get(ident),
            "agents": [],
        }
        containers.setdefault(m, []).append(node)
        # EVERY inhabitant points at this node, so a pod's agents hang under
        # their container exactly as a 1:1 seat's single agent does. Keyed on
        # the members rather than on the container name: for a pod those are
        # different strings, and keying on the container would leave its
        # agents homeless — matched by repo basename, which is the
        # attribution defect the container nodes exist to end.
        for member in members:
            by_identity[member] = node

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
            "placement": by_seat.get(name),
        }
        box = by_identity.get(name)
        if box is not None:
            # It runs in a container: that is WHERE it is, and the host
            # workspace listing its mount point is not a second home.
            box["agents"].append(dict(node, key=f"a:{box['key']}/{name}"))
            continue
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
            # Set below when several clones on that box share this repo name.
            "ambiguous": [],
            "hand": False,     # a hand is a board fact; no pane, no claim
            "rec": None,
            "placement": by_seat.get(name),
        }
        box = by_identity.get(name)
        if box is not None:
            box["agents"].append(dict(node, key=f"a:{box['key']}/{name}"))
            continue
        # EXACT first: when the machine has reported its roster we know this
        # agent's worktree and can match it the way a local row does, by
        # containment of a real path. Everything below is the fallback for a
        # machine whose edge has not reported one.
        wt = worktree_of.get(m, {}).get(name, "")
        if wt:
            node["worktree"] = wt
            homes = [w for w in ws_nodes.get(m, [])
                     if any(_contains(x, wt) for x in w["listings"])]
            if homes:
                for w in homes:
                    w["agents"].append(dict(node, key=f"a:{w['key']}/{name}"))
            else:
                loose.setdefault(m, []).append(node)
            continue
        repo = _repo_of(entry.get("project", ""), name, m)
        homes = [
            w for w in ws_nodes.get(m, [])
            if any(posixpath.basename(x) == repo for x in w["listings"])
        ]
        # A remote row has NO worktree, so `repo` is matched by basename — and
        # a box with several clones of one repo has several folders with that
        # basename at DIFFERENT paths. dev-vm-1 has three `mcp-hub` folders:
        # `code/monkeypashion/mcp-hub` (listed by squad AND runtime, which is
        # genuine multi-membership) and `general/mcp-hub`, a different clone
        # belonging to `mcp-hub-dev-vm-1-general`. The old code claimed all
        # three, so one row in every board was false.
        #
        # Two distinct PATHS means we cannot tell which clone this is. Several
        # workspaces listing the SAME path is not ambiguity — it is the
        # multi-membership local rows already show. So: resolve when the
        # candidates agree on a path, and refuse when they do not, rather than
        # picking one. `node["ambiguous"]` is what the row says instead.
        paths = {x for w in homes for x in w["listings"]
                 if posixpath.basename(x) == repo}
        if len(paths) > 1:
            node["ambiguous"] = sorted(paths)
            homes = []
        if homes:
            for w in homes:
                w["agents"].append(dict(node, key=f"a:{w['key']}/{name}"))
        else:
            loose.setdefault(m, []).append(node)

    # -- roster-only remote agents: ENROLLED there, ABSENT from the hub ----
    #
    # A remote row used to exist only where the hub had PRESENCE for it, so an
    # agent alive on its machine but with a dropped hub binding rendered as
    # nothing at all. That is the one state most worth seeing: it is what a
    # redeploy leaves behind, and clearing it is an operator action. Measured
    # 2026-08-06 — 21 of dev-vm-1's 31 roster agents were invisible, two of
    # them tmux-up with a lost binding, while the board above them read clean.
    #
    # The machine already pushes its whole roster (api_machine_agents, used
    # until now only to look up a worktree), so the fact was on the hub the
    # whole time and only the JOIN was missing. Same lesson as the container
    # rows: the data was already there.
    #
    # What such a row may claim is deliberately narrow. The push carries no
    # tmux state, so it says `not on hub` and never `stopped` — a row that
    # invented liveness would be the same error pointing the other way.
    for m in sorted(worktree_of):
        if m == this_machine:
            # The local roster is this box's authority and has already drawn
            # its agents WITH pane state. A local name missing from it is not
            # a fact the hub's older copy of it should resurrect.
            continue
        for name, wt in sorted(worktree_of[m].items()):
            if name in placed_names:
                continue
            placed_names.add(name)
            node = {
                "kind": "agent",
                "key": f"a:{m}/{name}",
                "agent": name,
                "machine": m,
                "local": False,
                "roster_ix": None,
                "worktree": wt,
                "klass": "",
                "project": "",
                "wakeable": False,
                "sessions": 0,
                "next": "",
                "state": "",
                # NOT stale: staleness is a claim about the fleet snapshot,
                # and this row does not come from it.
                "stale": False,
                "off_hub": True,
                # What separates an agent that SHOULD be on the hub from one
                # that never would be. `running` may be absent entirely —
                # that is UNKNOWN and must not be read as False.
                "comms": bool(pushed_of[m][name].get("comms")),
                "running": pushed_of[m][name].get("running"),
                "ambiguous": [],
                "hand": False,
                "rec": None,
                "placement": by_seat.get(name),
            }
            box = by_identity.get(name)
            if box is not None:
                box["agents"].append(dict(node, key=f"a:{box['key']}/{name}"))
                continue
            # Exact containment only. A roster row carries a real path, so the
            # basename fallback the presence rows need has no place here.
            homes = [w for w in ws_nodes.get(m, [])
                     if wt and any(_contains(x, wt) for x in w["listings"])]
            if homes:
                for w in homes:
                    w["agents"].append(dict(node, key=f"a:{w['key']}/{name}"))
            else:
                loose.setdefault(m, []).append(node)

    # -- workspaces whose agents live in containers ------------------------
    #
    # A containerized agent hangs under its container and NOT under the host
    # workspace listing its mount point — deliberately, because it runs in one
    # place and showing it in two says otherwise. But that leaves such a
    # workspace drawing with no children, and an empty row reads as "no
    # agents": a measurement nobody took. `capsule`, whose every member is
    # containerized, rendered as an empty shell while its agents were on the
    # board the whole time, one node down.
    #
    # So the workspace says where they WENT instead of reclaiming them. This
    # is a NOTE, not a second home — the agents stay under their containers,
    # `walk_agents` still yields each exactly once, and the count is derived
    # from what the container actually holds rather than from what the
    # workspace hoped for.
    #
    # A container with no known folder links to nothing. That is honest: the
    # folder is the only evidence tying a container to a workspace, and
    # guessing by repo name is the attribution defect the container nodes were
    # built to end.
    for m, cts in containers.items():
        for w in ws_nodes.get(m, []):
            mine = [c for c in cts
                    if c["folder"]
                    and any(_contains(x, c["folder"]) for x in w["listings"])]
            w["container_ids"] = [c["identity"] for c in mine]
            w["in_containers"] = sum(len(c["agents"]) for c in mine)

    # -- assemble ----------------------------------------------------------
    order = sorted(
        set(machines) | set(ws_nodes) | set(loose) | set(containers),
        key=lambda m: (m != this_machine, m == UNKNOWN_MACHINE, m),
    )
    out: list[dict[str, Any]] = []
    for m in order:
        wss = sorted(ws_nodes.get(m, []), key=lambda w: w["name"])
        for w in wss:
            w["agents"].sort(key=lambda a: (a["roster_ix"] is None, a["agent"]))
        cts = sorted(containers.get(m, []), key=lambda c: c["identity"])
        for c in cts:
            c["agents"].sort(key=lambda a: (a["roster_ix"] is None, a["agent"]))
        lo = sorted(loose.get(m, []), key=lambda a: (a["roster_ix"] is None,
                                                     a["roster_ix"] or 0,
                                                     a["agent"]))
        if not wss and not cts and not lo and m != this_machine:
            continue          # an enrolled box with nothing on it: no node
        out.append({
            "kind": "machine",
            "key": f"m:{m}",
            "machine": m,
            "local": m == this_machine,
            "unknown": m == UNKNOWN_MACHINE,
            "stale": stale and m != this_machine,
            "never": never and m != this_machine,
            # The edge's own report, a SEPARATE instrument from the fleet
            # snapshot above: `stale`/`never` describe the heartbeat DAEMON,
            # edge_state describes the RECONCILER — a dead daemon and a dead
            # edge are different failures, and one shared "not reporting"
            # phrase is exactly how five days of 203/EXEC read as a quiet
            # healthy box. Unlike the snapshot fields this applies to the
            # LOCAL machine too: a box's own dead edge must show on its own
            # board.
            "edge_state": _edge_state(
                (workspaces.get("machine_info") or {}).get(m), now
            ),
            "edge_info": (workspaces.get("machine_info") or {}).get(m),
            "workspaces": wss,
            "containers": cts,
            "loose": lo,
            "agent_count": sum(len(w["agents"]) for w in wss)
            + sum(len(c["agents"]) for c in cts) + len(lo),
            "open_count": sum(1 for w in wss if w["open_now"] or w["here"]),
            "drift_count": sum(1 for w in wss if w["drift"]),
        })
    return {
        "machines": out,
        "this_machine": this_machine,
        "fleet_stale": stale,
        "fleet_never": never,
        "fleet_ts": fleet_ts,
        "note": workspaces.get("note", ""),
    }


def walk_agents(tree: dict[str, Any]):
    """Every agent node, in the order the tree shows them."""
    for m in tree.get("machines", []):
        for w in m["workspaces"]:
            yield from w["agents"]
        for c in m.get("containers", []):
            yield from c["agents"]
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
        ), tuple(
            # Containers are structure too: omit them and a seat that comes up
            # never gets a node, because the poll would relabel forever.
            (c["key"], tuple(a["key"] for a in c["agents"]))
            for c in m.get("containers", [])
        ), tuple(a["key"] for a in m["loose"]))
        for m in tree.get("machines", [])
    )
