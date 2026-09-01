"""The turn-boundary half of a hold (#318, operator-approved 2026-09-01).

A hold is recorded on the hub and mirrored to disk by the edge; squad's
`up_one`/`relaunch_agent` refuse to START a held lane. That covers a lane
that is already down. It does NOT cover the lane the ceiling watcher
actually reaches for — one that is RUNNING and burning its share right now.

The operator's ruling is that such a lane is stopped **at its next turn
boundary, not mid-turn**, and hard-stopped if it is still mid-turn ten
minutes later — with the notice saying, in his words, that the one in-flight
turn is LOST.

⭐ ONLY THE STOP HOOK CAN SEE A TURN BOUNDARY. It fires at the end of every
turn, inside the agent's own process. So it OBSERVES the boundary and leaves
a stamp; squad — which owns lane lifecycle, and is the enforcement point for
exactly the reason the mirror exists — ACTS on it. The hook does not stop its
own lane: it runs inside the process it would be killing, and it must return
0 and fail open no matter what.

🔴 EVERY READ HERE FAILS OPEN, in one direction only: anything unreadable,
malformed, or absent means NOT HELD. A hold that cannot be read must never
block a turn. That matches squad's direction (a dead edge un-holds lanes)
and it is why every entry carries its own expiry as well.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

# The operator's number, and it is a DEADLINE not a poll interval: measured
# from when the hold was recorded, so a lane that never reaches a boundary
# is stopped ten minutes after the ask rather than ten minutes after somebody
# noticed. See `hard_stop_due`.
HARD_STOP_AFTER_SECONDS = 600.0


def held_lanes_path() -> Path:
    """Where the edge leaves the mirror. Same env override as `edge`."""
    override = os.environ.get("MCP_HUB_HELD_FILE")
    if override:
        return Path(override)
    return Path.home() / ".mcp-hub" / "held-lanes.json"


def boundary_dir() -> Path:
    override = os.environ.get("MCP_HUB_HOLD_BOUNDARY_DIR")
    if override:
        return Path(override)
    return Path.home() / ".mcp-hub" / "hold-boundary"


def held_entry(agent: str, now: float | None = None) -> dict[str, Any] | None:
    """This lane's live hold, or None. Never raises.

    The expiry is re-checked HERE rather than trusted from the file: the
    mirror is rebuilt by an edge pass that may be minutes old, and an expired
    hold must read as released everywhere it is read, not only where it was
    written.
    """
    now = time.time() if now is None else now
    try:
        raw = json.loads(held_lanes_path().read_text(encoding="utf-8"))
        entry = (raw.get("held") or {}).get(agent)
        if not isinstance(entry, dict):
            return None
        if float(entry.get("until") or 0) <= now:
            return None
        return entry
    except Exception:  # noqa: BLE001 — unreadable means NOT held, always
        return None


def stamp_boundary(agent: str, now: float | None = None) -> bool:
    """Record that this lane reached a turn boundary while held.

    Written once and left alone: the value squad needs is the FIRST boundary
    after the hold, and re-stamping every subsequent Stop would keep pushing
    the moment forward on a lane that is already stoppable.
    """
    now = time.time() if now is None else now
    try:
        d = boundary_dir()
        d.mkdir(parents=True, exist_ok=True)
        target = d / f"{agent}.json"
        if target.exists():
            return True
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps({"reached_at": now, "agent": agent}),
                       encoding="utf-8")
        tmp.replace(target)
        return True
    except Exception:  # noqa: BLE001 — a stamp we cannot write is not a turn
        return False                                      # we block. Fail open.


def boundary_reached_at(agent: str) -> float | None:
    try:
        raw = json.loads((boundary_dir() / f"{agent}.json").read_text("utf-8"))
        return float(raw.get("reached_at") or 0) or None
    except Exception:  # noqa: BLE001
        return None


def clear_boundary(agent: str) -> None:
    """Drop the stamp once the lane is no longer held.

    A stamp that outlived its hold would make the NEXT hold look as though it
    had already reached a boundary — stopping a lane mid-turn under a rule
    that exists to prevent exactly that.
    """
    try:
        (boundary_dir() / f"{agent}.json").unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass


def hard_stop_due(entry: dict[str, Any], agent: str,
                  now: float | None = None) -> bool:
    """Has this hold waited out its grace without reaching a boundary?

    False once a boundary is stamped: the lane is stoppable cleanly and there
    is no in-flight turn to lose. False too when the hold carries no
    `held_at` — an entry that cannot say when it started cannot be shown to
    have waited, and inferring one would let a mirror-format change hard-stop
    the fleet.
    """
    now = time.time() if now is None else now
    if boundary_reached_at(agent):
        return False
    try:
        held_at = float(entry.get("held_at") or 0)
    except (TypeError, ValueError):
        return False
    if held_at <= 0:
        return False
    return (now - held_at) >= HARD_STOP_AFTER_SECONDS


def hook_notice(agent: str, entry: dict[str, Any]) -> str:
    """What the held agent is told at the boundary it just reached.

    It says STOPPING, not stopped: squad acts on the stamp within one heal
    pass, so the lane is still up while this is read. Announcing a completed
    stop that has not happened is the "delivered live" mistake in a new
    costume.
    """
    cond = str(entry.get("release_condition") or "no condition recorded")
    until = float(entry.get("until") or 0)
    when = time.strftime("%H:%M", time.localtime(until)) if until else "?"
    reason = str(entry.get("reason") or "")
    return (
        f"⏸️ THIS LANE IS HELD — it is being stopped at this turn boundary, "
        f"which is the one you have just reached.\n"
        f"· Releases: {when} at the latest, or when — {cond}\n"
        + (f"· Reason: {reason}\n" if reason else "")
        + "· Nothing you have done is lost: the stop lands between turns, "
        "and release restarts this lane with --continue.\n"
        "· You do not need to do anything. Do NOT start new work."
    )
