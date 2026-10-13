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

    ⭐ False ALWAYS for `kind="hibernation"` (bar 59). The ten-minute ceiling
    exists because a BRAKE is reaching for a lane that is burning its share
    right now — waiting on it indefinitely is the thing the brake cannot
    afford. A hibernation is the opposite case: the lane is parked precisely
    BECAUSE it has nothing open, so there is no share to reclaim and killing
    its in-flight turn destroys work to save nothing. Same mechanism, two
    reasons, and the grace belongs to only one of them.

    ABSENT kind keeps the old behaviour deliberately. An entry written by an
    older edge, or a mirror this build has not seen, must not be promoted
    into the exempt class by silence — the exemption is a positive mark, and
    NO KIND = NOT EXEMPT, the same fail-closed direction as the sender
    grades.
    """
    now = time.time() if now is None else now
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
        # A different sentence, because a lane told the BRAKE notice when it
        # was merely parked reads it as a reprimand for overspending and
        # goes looking for what it did wrong. It did nothing wrong: it ran
        # out of open work. Say that.
        return (
            f"💤 THIS LANE IS HIBERNATING — it is being parked at this turn "
            f"boundary, which is the one you have just reached.\n"
            f"· Wakes: {when} at the latest, or when — {cond}\n"
            + (f"· Reason: {reason}\n" if reason else "")
            + "· This is NOT a stop for overspending, and no turn of yours "
            "is lost: a parked lane is never hard-stopped mid-turn.\n"
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


# ---------------------------------------------------------------------------
# r5 F3 — the notice is delivered ONCE PER HOLD, keyed to its `until`
#
# The notice used to outrank the loop backstop unconditionally, so a held
# lane re-blocked on every Stop for as long as the hold stood: the lane was
# told the same sentence again and again, and each telling cost it a turn.
# The sentence is still owed — being stopped is the one thing an agent must
# not learn by having its pane disappear — but it is owed ONCE.
#
# ⭐ KEYED TO `until`, NEVER A BARE FLAG, for the same reason
# `hold_hand_started_flag` is: a bare flag would silence the notice for
# every LATER hold on this lane too, and a silencer that cannot expire is
# the silent-drop shape this codebase has shipped before. A new hold has a
# new expiry, does not match, and is announced.
# ---------------------------------------------------------------------------

def state_dir() -> Path:
    """Where the per-lane markers live. Same override as the rest of the cli."""
    override = os.environ.get("MCP_HUB_STATE_DIR")
    return Path(override) if override else Path.home() / ".mcp-hub"


def _safe(agent: str) -> str:
    return "".join(c if (c.isalnum() or c in "-_") else "-"
                   for c in agent.lower()).strip("-") or "unnamed"


def notice_marker_path(agent: str) -> Path:
    return state_dir() / f"hold-notice-{_safe(agent)}.json"


def notice_delivered(agent: str, until: float) -> bool:
    """Has THIS hold's notice already reached the lane?

    Unreadable or absent reads as NOT delivered, which errs towards telling
    the lane again. The failure that matters here is the silent one.
    """
    try:
        raw = json.loads(notice_marker_path(agent).read_text(encoding="utf-8"))
        return abs(float(raw.get("until") or 0) - float(until)) < 0.5
    except Exception:  # noqa: BLE001 — never delivered, as far as we can tell
        return False


def mark_notice_delivered(agent: str, until: float) -> None:
    try:
        p = notice_marker_path(agent)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"until": float(until),
                                 "at": time.time()}), encoding="utf-8")
    except Exception:  # noqa: BLE001 — a marker we cannot write just repeats
        pass


def clear_notice_marker(agent: str) -> None:
    """Drop the marker once the lane is no longer held.

    Not strictly needed (the marker carries its hold's `until`, so a later
    hold never matches it), but a file that outlives every reason to exist
    is one more thing a reader has to reason about.
    """
    try:
        notice_marker_path(agent).unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# r5 F6 — the conversation deferral, as a fact the HOOK can read
#
# squad decides the deferral (it is the enforcement point and the only party
# that can see a pane), and the hook has to know about it: a lane that is
# NOT going to be stopped must not be told it is being stopped. The record is
# the channel, and it carries an expiry for the usual reason — a squad that
# stops running must not silence the notice forever.
# ---------------------------------------------------------------------------

DEFERRAL_TTL_SECONDS = 900.0


def deferral_path(agent: str) -> Path:
    """`<state>/hold-deferred/<agent>.json` — the agent name VERBATIM.

    Same convention as `boundary_dir`, and deliberately not the sanitized
    form: squad writes this file with `$a` as it appears in the roster, and
    two spellings of one lane's filename is a record that reads as absent.
    """
    return state_dir() / "hold-deferred" / f"{agent}.json"


def record_deferral(agent: str, *, until: float, owner: str = "",
                    conversation_end: float = 0.0,
                    now: float | None = None) -> None:
    """Note that this hold was deferred because the lane is in conversation.

    ⚠️ IN PRODUCTION SQUAD IS THE WRITER, with `printf` — at 46 owner-held
    lanes a python fork per lane is the cost F6 is bounded on, and the record
    is a flat JSON object. This is the same shape stated once in the language
    that READS it, so the field names have one definition; `deferred` below
    honours whatever `expires` it finds in the file, so the two cannot drift
    into disagreeing about how long a deferral lasts.
    """
    now = time.time() if now is None else now
    try:
        p = deferral_path(agent)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({
            "agent": agent, "at": now, "hold_until": float(until),
            "owner": owner, "conversation_end": float(conversation_end),
            "expires": now + DEFERRAL_TTL_SECONDS,
        }), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def clear_deferral(agent: str) -> None:
    try:
        deferral_path(agent).unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass


def deferred(agent: str, now: float | None = None) -> bool:
    """Is this lane's hold currently deferred for a conversation?

    Fails open as NOT deferred: an unreadable or lapsed record means the
    lane hears the notice, which is the direction that loses nothing.
    """
    now = time.time() if now is None else now
    try:
        raw = json.loads(deferral_path(agent).read_text(encoding="utf-8"))
        return float(raw.get("expires") or 0) > now
    except Exception:  # noqa: BLE001
        return False
