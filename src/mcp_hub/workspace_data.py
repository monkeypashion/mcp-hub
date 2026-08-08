"""Workspace manager data layer — the merged three-truth view.

One row per workspace, three columns, each from its honest source:

    registered   the hub API's /workspace-registry (definitions)
    on_disk      local scan (this machine, fresh) + fleet discoveries (edge
                 reports, as fresh as the last report)
    open_now     board presence pings, via the registry

The hub being unreachable DEGRADES: the local scan still answers, the gap is
named in `note`, and `registered` becomes None — unknown, never defaulted to
False, because "feral" is an accusation the data must actually support.

Consumed by the board's workspaces view and the cockpit; pure function over
an injected API client so tests never need a socket.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mcp_hub.edge import discover_workspaces
from mcp_hub.operator_api import ApiUnavailable


def _name_of(path: str) -> str:
    return path.rsplit("/", 1)[-1].removesuffix(".code-workspace")


def collect_workspaces(
    api: Any,
    scan_dirs: list[Path],
    this_machine: str,
) -> dict[str, Any]:
    """Merge local scan + hub registry into manager rows.

    `api` needs one method: get_registry() -> the /workspace-registry body.
    """
    local = discover_workspaces(scan_dirs)
    rows: dict[tuple[str, str], dict[str, Any]] = {}

    for w in local:
        key = (this_machine, _name_of(w["path"]))
        rows[key] = {
            "name": _name_of(w["path"]),
            "machine": this_machine,
            "path": w["path"],
            "folders": w.get("folders"),
            "error": w.get("error", ""),
            "on_disk": True,
            "open_now": False,
            "registered": None,  # unknown until the hub answers
            "squad": "",
            # The folder paths a workspace lists. Only DEFINITIONS carry them
            # (edge reports a count, not a manifest), so a remote workspace
            # nobody registered has none — which is why the tree attributes a
            # remote agent to a machine but not always to a workspace.
            "listings": [],
        }

    hub_reachable = True
    note = ""
    try:
        registry = api.get_registry()
    except ApiUnavailable as e:
        # Already an operator-ready sentence naming its own fix — "no token
        # here", "API disabled on the hub" and "unreachable" are different
        # problems, and the manager must not flatten them into an outage.
        hub_reachable = False
        note = f"{e} — local scan only"
        registry = {"definitions": [], "discovered": []}
    except Exception as e:  # noqa: BLE001 — any transport failure degrades
        hub_reachable = False
        note = f"hub registry unreachable ({e}) — local scan only"
        registry = {"definitions": [], "discovered": []}

    for d in registry["discovered"]:
        key = (d["machine"], _name_of(d["path"]))
        if key in rows:
            # Local enumeration of THIS machine is fresher than the hub's
            # copy of it; keep disk facts local, take presence from the hub
            # (only the registry knows about board pings).
            rows[key]["open_now"] = d.get("open_now", False)
            if rows[key]["registered"] is None:
                rows[key]["registered"] = d.get("registered", False)
        else:
            # 🔴 `on_disk` MUST NOT be inherited from the hub for THIS machine.
            # The hub's `discovered` list is what machines reported at some
            # earlier moment, so a file deleted since is still in it — and
            # taking True from that record made the manager assert a file that
            # was not there. Measured 2026-08-08: `showcase.code-workspace` was
            # deleted, `find ~` confirmed no copy anywhere, and the row still
            # read `✔ disk`.
            #
            # The clause above already says local enumeration of this machine
            # is fresher than the hub's copy — but it only got to say so when
            # the file still EXISTED. Absence fell through to here, which is
            # exactly the case where freshness matters most.
            #
            # For a REMOTE machine the hub's record is the best evidence we
            # have (we cannot stat another box's disk), so it stands.
            local_authoritative = d["machine"] == this_machine
            rows[key] = {
                "name": _name_of(d["path"]),
                "machine": d["machine"],
                "path": d["path"],
                "folders": d.get("folders"),
                "error": d.get("error", ""),
                "on_disk": not local_authoritative,
                "open_now": d.get("open_now", False),
                "registered": d.get("registered", False),
                "squad": "",
                "listings": [],
            }

    for w in registry["definitions"]:
        key = (w.get("machine", ""), w["name"])
        matched = None
        for k in rows:
            if k[1] == w["name"] and (not w.get("machine") or k[0] == w["machine"]):
                matched = k
                break
        if matched:
            rows[matched]["registered"] = True
            rows[matched]["squad"] = w.get("squad", "")
            rows[matched]["listings"] = list(w.get("listings", []))
        else:
            rows[key] = {
                "name": w["name"],
                "machine": w.get("machine", ""),
                "path": "",
                "folders": len(w.get("listings", [])),
                "error": "",
                "on_disk": False,  # a definition nothing has materialized
                "open_now": False,
                "registered": True,
                "squad": w.get("squad", ""),
                "listings": list(w.get("listings", [])),
            }

    # A hub that ANSWERED has told us everything it knows. Anything still
    # unmatched is therefore not registered — a feral file — and saying
    # "unknown" about it hides the one state this column exists to show.
    # Only an absent hub leaves the question genuinely open.
    if hub_reachable:
        for r in rows.values():
            if r["registered"] is None:
                r["registered"] = False

    # Every machine the fleet is known to have, so an agent named after a box
    # that owns no workspace can still be PLACED on it. Derived from the rows
    # alone, a machine with nothing on disk simply would not exist, and its
    # agents would fall into "(machine unknown)" — visible, but wrong.
    machines = {r["machine"] for r in rows.values() if r["machine"]}
    machines.add(this_machine)
    if hub_reachable:
        try:
            machines.update(
                m["name"] for m in api.list_machines() if m.get("name")
            )
        except Exception:  # noqa: BLE001 — enrolment is a bonus source, not a gate
            pass

    return {
        "hub_reachable": hub_reachable,
        "note": note,
        "machines": sorted(machines),
        # Returned rather than re-derived by the view: the caller already
        # resolved it, and a second derivation is a second chance to disagree
        # (squad deriving from basename while the cli derives from the git
        # remote is exactly how a clone's statusline came to read `hub ?`).
        "this_machine": this_machine,
        "rows": sorted(
            rows.values(), key=lambda r: (r["machine"], r["name"])
        ),
    }
