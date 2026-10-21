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
    # A PARKED lane has no turn worth hurrying: hibernation means it ran out
    # of open work, so it waits for its boundary however long that takes.
    # Only the exact word earns this — no kind, or one the hub would refuse,
    # is hard-stopped as before rather than promoted into the exempt class.
    if str(entry.get("kind") or "") == "hibernation":
        return False
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
    if str(entry.get("kind") or "") == "hibernation":
        # Parked, not braked. The brake's wording reads as a reprimand for
        # overspending, and a lane that merely ran out of work would go
        # looking for what it did wrong.
        return (
            f"💤 THIS LANE IS HIBERNATING — it is being stopped at this turn "
            f"boundary because it has no open work, not for anything it did.\n"
            f"· Wakes: {when} at the latest, or when — {cond}\n"
            + (f"· Reason: {reason}\n" if reason else "")
            + "· The stop lands between turns, so no turn of yours is lost, "
            "and waking restarts this lane with --continue.\n"
            "· You do not need to do anything. Do NOT start new work."
        )
    return (
        f"⏸️ THIS LANE IS HELD — it is being stopped at this turn boundary, "
        f"which is the one you have just reached.\n"
        f"· Releases: {when} at the latest, or when — {cond}\n"
        + (f"· Reason: {reason}\n" if reason else "")
        + "· Nothing you have done is lost: the stop lands between turns, "
        "and release restarts this lane with --continue.\n"
        "· You do not need to do anything. Do NOT start new work."
    )


# --- once per hold: the notice marker (v2-fleet r5 F3) ----------------------
#
# A block costs the lane a model turn, and the notice is DERIVED — re-read
# from the mirror at every Stop — so delivering it consumes nothing and not
# re-delivering it loses nothing. The marker records "this lane has been told
# about THIS hold", keyed to the hold's `until`: a bare told-them flag would
# silence every LATER hold too, the silencer-with-no-expiry shape. It is not
# a suppression either: the notice still rides, first, on any block that real
# content pays for. It only stops buying blocks of its own.

def _state_dir() -> Path:
    override = os.environ.get("MCP_HUB_STATE_DIR")
    return Path(override) if override else Path.home() / ".mcp-hub"


def notice_marker_path(agent: str) -> Path:
    return _state_dir() / "hold-notice" / f"{agent}.json"


def notice_delivered(agent: str, entry: dict[str, Any]) -> bool:
    """Has this lane already been told about THIS hold? False on any doubt,
    which re-delivers: the failure direction is a wasted turn, never an
    unannounced stop."""
    try:
        raw = json.loads(notice_marker_path(agent).read_text("utf-8"))
        return float(raw.get("until")) == float(entry.get("until"))
    except Exception:  # noqa: BLE001
        return False


def mark_notice_delivered(agent: str, entry: dict[str, Any],
                          now: float | None = None) -> None:
    now = time.time() if now is None else now
    try:
        target = notice_marker_path(agent)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps({"until": float(entry.get("until") or 0),
                                   "told_at": now}), encoding="utf-8")
        tmp.replace(target)
    except Exception:  # noqa: BLE001 — unwritten means told again next Stop
        pass


def clear_notice_marker(agent: str) -> None:
    """Drop the marker once the hold is gone, so it can never outlive the
    hold it was written for."""
    try:
        notice_marker_path(agent).unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass


# --- the conversation exemption's deferral (v2-fleet r5 F6) -----------------
#
# squad decides; the hook reads. A hold placed by a MECHANISM (it carries an
# `owner`) is deferred while the lane is in conversation with Tim, and squad
# leaves `hold-deferred/<lane>.json` saying so. While it stands the lane is
# NOT being stopped at this boundary, so the hook must neither say it is nor
# stamp one — a stamp left now would stop the lane mid-sentence on the next
# pass.
#
# ⭐ A DEADLINE, NOT A FLAG. A squad that stops running must not silence the
# "being stopped" notice forever — the same reason focus mode stores an
# expiry. Every pass that still defers re-writes the record; one that stops
# re-writing lets it lapse within the TTL.
#
# 🔴 FAILS OPEN AS NOT DEFERRED: an unreadable record means the lane hears the
# notice, which is the direction that loses nothing.

DEFERRAL_TTL_SECONDS = 600.0


def deferral_path(agent: str) -> Path:
    """`$MCP_HUB_STATE_DIR/hold-deferred/<lane>.json` — squad writes the same
    name with the roster's spelling of the lane."""
    return _state_dir() / "hold-deferred" / f"{agent}.json"


def record_deferral(agent: str, *, until: float, owner: str,
                    now: float | None = None) -> bool:
    now = time.time() if now is None else now
    try:
        target = deferral_path(agent)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "agent": agent, "until": until, "owner": owner,
            "deferred_at": now, "expires": now + DEFERRAL_TTL_SECONDS,
        }), encoding="utf-8")
        tmp.replace(target)
        return True
    except Exception:  # noqa: BLE001
        return False


def deferred(agent: str, now: float | None = None) -> bool:
    """Is this lane's hold deferred right now? Never raises.

    Reads `expires` when present, else `deferred_at` plus the TTL; a record
    carrying neither falls back to its file's mtime, so a writer that only
    touches the file still gets a deadline rather than a forever."""
    now = time.time() if now is None else now
    path = deferral_path(agent)
    try:
        raw = json.loads(path.read_text("utf-8"))
        if not isinstance(raw, dict):
            return False
        if raw.get("expires") is not None:
            expires = float(raw["expires"])
        elif raw.get("deferred_at") is not None:
            expires = float(raw["deferred_at"]) + DEFERRAL_TTL_SECONDS
        else:
            expires = path.stat().st_mtime + DEFERRAL_TTL_SECONDS
        return now < expires
    except Exception:  # noqa: BLE001 — unreadable means NOT deferred
        return False


def clear_deferral(agent: str) -> None:
    try:
        deferral_path(agent).unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass
