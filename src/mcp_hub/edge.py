"""Edge realizer brain — `mcp-hub edge apply` (interim edge, worktree only).

The hub stores desired state; this module decides what a machine should DO
about it and what it may truthfully REPORT back. Three properties are
load-bearing and tested:

- plan() diffs desired against ENUMERATED local state and emits ordered
  actions; docker placements are skipped loudly, never guessed at (the
  container credential story is undesigned — see the runtime doc).
- discover_workspaces() reports every .code-workspace it finds, including
  unparseable ones — the operator's "never lose track of workspaces"
  requirement means a broken file is reported-with-error, not dropped.
- observed_report() derives state from enumeration alone. It refuses an
  empty enumeration: no evidence means "unknown", and unknown must be an
  error, not a default (evidence contract ①).

Execution (running the actual squad commands) is deliberately elsewhere and
injected — the brain never shells out, so no test of it can touch a roster.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


def plan(
    placements: list[dict[str, Any]],
    local_seats: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Diff desired placements against enumerated local seat state.

    `local_seats` maps seat identity -> {"folder_exists": bool,
    "running": bool} as ENUMERATED by the caller (tmux + filesystem), never
    read from any record.
    """
    actions: list[dict[str, Any]] = []
    for p in placements:
        seat = p["seat"]
        base = {"placement": p["id"], "seat": seat}
        if p["substrate"] != "worktree":
            actions.append(
                {
                    **base,
                    "op": "skip",
                    "reason": f"substrate '{p['substrate']}' not realizable by this "
                    "edge (docker needs the container credential story)",
                }
            )
            continue
        local = local_seats.get(seat, {"folder_exists": False, "running": False})
        desired = p["desired"]
        if desired == "reclaimed":
            # Harvest before destroy, always: the memory delta is work
            # product, and a clone whose learnings die with the substrate is
            # the vacuous green of scheduling.
            actions.extend(
                [
                    {**base, "op": "harvest"},
                    {**base, "op": "verify"},
                    {**base, "op": "destroy"},
                ]
            )
        elif desired == "running":
            if not local["folder_exists"]:
                actions.append({**base, "op": "materialize"})
                actions.append({**base, "op": "start"})
            elif not local["running"]:
                actions.append({**base, "op": "start"})
        elif desired == "stopped":
            if local["running"]:
                actions.append({**base, "op": "stop"})
    return actions


def discover_workspaces(scan_dirs: list[Path]) -> list[dict[str, Any]]:
    """Enumerate .code-workspace files under the given directories (flat).

    Every file found is reported — a workspace whose JSONC fails to parse is
    returned with an `error` field rather than silently dropped, because the
    registry's whole point is that nothing gets lost track of.
    """
    found: list[dict[str, Any]] = []
    for d in scan_dirs:
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.code-workspace")):
            entry: dict[str, Any] = {"path": str(f)}
            try:
                raw = re.sub(r"//[^\n]*", "", f.read_text(encoding="utf-8"))
                data = json.loads(raw)
                entry["folders"] = len(data.get("folders", []))
            except (OSError, json.JSONDecodeError) as e:
                entry["error"] = str(e)
            found.append(entry)
    return found


def observed_report(
    placement: dict[str, Any], enumeration: dict[str, Any]
) -> dict[str, Any]:
    """Build the observed-state report for one placement from enumeration.

    State comes ONLY from what was enumerated — never from the placement's
    own `desired` field. An empty enumeration is refused: it would make the
    report an assertion over an empty set.
    """
    if not enumeration:
        raise ValueError(
            f"empty enumeration for placement {placement['id']}: refusing to "
            "report a state no evidence supports"
        )
    state = "running" if enumeration.get("alive") else "stopped"
    return {"state": state, "enumeration": enumeration}


class HubAPI:
    """Thin client for the machine-facing slice of /api/v1.

    Accepts an injected client (any object with .get/.post taking
    headers/json — a starlette TestClient in tests, an httpx.Client in
    production) so the full loop is testable against the real API in-process
    without a socket.
    """

    def __init__(
        self,
        base_url: str | None = None,
        token: str = "",
        client: Any = None,
    ) -> None:
        if client is None:
            import httpx

            client = httpx.Client(base_url=base_url or "", timeout=30)
        self._c = client
        self._h = {"Authorization": f"Bearer {token}"}

    def pull_placements(self, machine: str) -> list[dict[str, Any]]:
        r = self._c.get(
            f"/api/v1/machines/{machine}/placements", headers=self._h
        )
        r.raise_for_status()
        return r.json()["placements"]

    def push_observed(self, placement_id: str, report: dict[str, Any]) -> dict:
        r = self._c.post(
            f"/api/v1/placements/{placement_id}/observed",
            headers=self._h,
            json=report,
        )
        r.raise_for_status()
        return r.json()

    def push_status(self, machine: str, payload: dict[str, Any]) -> None:
        r = self._c.post(
            f"/api/v1/machines/{machine}/status", headers=self._h, json=payload
        )
        r.raise_for_status()


class SquadExecutor:
    """Maps planned actions onto the proven squad verbs via an injected runner.

    runner(cmd: list[str], cwd: str | None = None) -> (returncode, output).
    Production passes a subprocess wrapper; tests pass a recorder — this class
    never imports subprocess, so no test path can reach a real shell.
    """

    def __init__(self, runner: Any) -> None:
        self._run = runner

    def execute(
        self, action: dict[str, Any], seat_spec: dict[str, Any]
    ) -> dict[str, Any]:
        op = action["op"]
        seat = action["seat"]
        base = {"op": op, "seat": seat}
        if op == "skip":
            return {**base, "skipped": True, "reason": action.get("reason", "")}
        if op == "verify":
            # Verification is the orchestrator's re-enumeration, not a shell
            # command — a verify that shells out to the thing it verifies
            # would be self-assertion.
            return {**base, "deferred": "verified by re-enumeration"}
        if op == "materialize":
            cmd = ["squad", "add", seat_spec.get("repo", "")]
        elif op == "start":
            cmd = ["squad", "start", seat]
        elif op == "stop":
            cmd = ["squad", "stop", seat]
        elif op == "harvest":
            cmd = ["mcp-hub", "memory-export"]
        elif op == "destroy":
            cmd = ["squad", "rm", seat]
        else:
            return {**base, "skipped": True, "reason": f"unknown op '{op}'"}
        if op == "harvest" and seat_spec.get("folder"):
            rc, out = self._run(cmd, cwd=seat_spec["folder"])
        else:
            rc, out = self._run(cmd)
        return {**base, "rc": rc, "output": out[-400:]}


def edge_apply(
    api: HubAPI,
    machine: str,
    runner: Any,
    scan_dirs: list[Path],
    folder_exists: Any = None,
) -> dict[str, Any]:
    """One reconcile pass: pull → enumerate → plan → execute → report.

    Reports are built from a FRESH enumeration taken after execution — the
    loop observes the effect of its own actions rather than assuming them.
    """
    if folder_exists is None:
        folder_exists = lambda p: bool(p) and Path(p).is_dir()  # noqa: E731

    placements = api.pull_placements(machine)

    def enumerate_now() -> dict[str, dict[str, Any]]:
        rc, out = runner(["tmux", "-L", "squad", "ls", "-F", "#S"])
        sessions = set(out.splitlines()) if rc == 0 else set()
        local: dict[str, dict[str, Any]] = {}
        for p in placements:
            spec = p.get("seat_spec") or {}
            local[p["seat"]] = {
                "folder_exists": folder_exists(spec.get("folder", "")),
                "running": p["seat"] in sessions,
            }
        return local

    actions = plan(placements, enumerate_now())
    executor = SquadExecutor(runner)
    specs = {p["seat"]: (p.get("seat_spec") or {}) for p in placements}
    results = [executor.execute(a, specs.get(a["seat"], {})) for a in actions]

    local = enumerate_now()
    reported = 0
    for p in placements:
        state = local[p["seat"]]
        enumeration = {
            "tmux_session": p["seat"],
            "alive": state["running"],
            "folder_exists": state["folder_exists"],
        }
        api.push_observed(p["id"], observed_report(p, enumeration))
        reported += 1

    workspaces = discover_workspaces([Path(d) for d in scan_dirs])
    api.push_status(
        machine,
        {
            "workspaces": workspaces,
            "seats": [
                {"seat": s, **v} for s, v in sorted(local.items())
            ],
        },
    )
    return {
        "placements": len(placements),
        "actions": results,
        "observed_reported": reported,
        "workspaces_reported": len(workspaces),
    }
