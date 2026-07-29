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
        }

    hub_reachable = True
    note = ""
    try:
        registry = api.get_registry()
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
            rows[key] = {
                "name": _name_of(d["path"]),
                "machine": d["machine"],
                "path": d["path"],
                "folders": d.get("folders"),
                "error": d.get("error", ""),
                "on_disk": True,
                "open_now": d.get("open_now", False),
                "registered": d.get("registered", False),
                "squad": "",
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
            }

    return {
        "hub_reachable": hub_reachable,
        "note": note,
        "rows": sorted(
            rows.values(), key=lambda r: (r["machine"], r["name"])
        ),
    }
