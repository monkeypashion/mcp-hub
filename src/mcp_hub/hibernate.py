"""The hibernation scanner (bar 59): query -> hold -> 12h re-hold -> release.

A lane with nothing open still costs its share while it idles. The console
knows which lanes own no open bar; this asks it, every pass, and parks each
candidate with a `kind=hibernation`, `owner=hibernation-scanner` hold. The
hub half (`api_v1.seat_actions`) validates the kind and the owner, the edge
mirrors both verbatim, and the lane (`hold.hard_stop_due`) never hard-stops a
hibernation — a parked lane has no in-flight turn worth destroying.

The rules, each of which is a way this is easy to build wrong:

- **A re-hold is a FRESH ENTRY after a fresh query, never an expiry bump.**
  Bumping `until` keeps a lane parked on a reason nobody re-checked: the hold
  outlives the fact that justified it, and the lane cannot tell.
- **Only the scanner's own live holds are released.** A hibernation scanner
  lifting a brake would hand a lane its share back mid-window.
- **The release carries the console's own words** for why (`left_out[].why`)
  verbatim, or says the console gave none (`cause_source`). Six releases must
  not read as six bar assignments.
- **An unanswerable list does NOTHING** — not even releases. With no list,
  every held lane looks like "no longer a candidate", and a network blip
  would unpark the fleet.
- **A row this cannot read refuses the whole pass**, releases included. A
  half-read list is worse than an unread one: releasing everyone "not in
  candidates" would unpark the very lanes listed under the key we missed.
- **An exempt name matching no seat refuses every hold**; releases still run,
  because releasing is always the safe direction.
- The 12h `until` is the backstop: a scanner that stops running releases the
  fleet by itself within half a day.

Ships UNARMED: bare `mcp-hub hibernate` is report-only, and `--arm` is the
only way it writes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from mcp_hub import edge

KIND = "hibernation"
OWNER = "hibernation-scanner"
# The hub's own hold cap (api_v1._HOLD_MAX_SECONDS). Not longer: the hub
# refuses past it. Not shorter: the re-hold cadence is the scanner's pass
# interval, and the expiry exists for the scanner that stopped running.
TTL_SECONDS = 12 * 3600.0

# Where a release's `cause` came from. Empty and never-said are the same
# bytes unless something says which.
CAUSE_FROM_CONSOLE = "console"
CAUSE_UNRECORDED = "unrecorded"

# Said in the hold when the console's row carries no words of its own. The
# hub requires a release condition, and an invented one would be a claim
# about the console this scanner cannot make — so it says what it knows.
_NO_WHY = "listed as a hibernation candidate (the console gave no why)"
_NO_RELEASE = ("the console stops listing this lane (it stated no release "
               "condition)")


class ConsoleAPI:
    """The console's read-only candidate door.

    `client` is injected (any object with `.get(url, headers=...)`) so tests
    need no socket — the same shape as `edge.HubAPI`.
    """

    def __init__(self, base_url: str = "", token: str = "",
                 client: Any = None) -> None:
        self._base = (base_url or "").rstrip("/")
        if client is None and self._base:
            import httpx

            client = httpx.Client(base_url=self._base, timeout=30)
        self._c = client
        self._h = {"Authorization": f"Bearer {token}"} if token else {}

    def candidates(self, thread: Any) -> dict[str, Any]:
        if self._c is None:
            raise RuntimeError(
                "no console URL configured (--console-url or "
                "$MCP_HUB_CONSOLE_URL)")
        r = self._c.get(f"/threads/{thread}/hibernation-candidates",
                        headers=self._h)
        r.raise_for_status()
        return r.json()


class HubHolds:
    """The hub's seat-action door, as the scanner uses it.

    'Authorization: Bearer' on EVERY call, because that is the only header
    `api_v1.auth()` reads. The first version sent `x-mcp-hub-operator-token`,
    which is the MCP side's header: every call was a 401 whatever the token,
    and the check that "confirmed" it used a deliberately fake token — the
    same answer a bad scheme gives.
    """

    def __init__(self, base_url: str = "", token: str = "",
                 client: Any = None) -> None:
        if client is None:
            import httpx

            client = httpx.Client(base_url=base_url or "", timeout=30)
        self._c = client
        self._h = {"Authorization": f"Bearer {token}"}

    def seats(self) -> list[str]:
        r = self._c.get("/api/v1/seats", headers=self._h)
        r.raise_for_status()
        return [str(s.get("identity") or "") for s in r.json().get("seats", [])
                if isinstance(s, dict) and s.get("identity")]

    def actions(self, seat: str) -> list[dict[str, Any]]:
        r = self._c.get(f"/api/v1/seats/{seat}/actions", headers=self._h)
        r.raise_for_status()
        return list(r.json().get("actions", []))

    def _act(self, seat: str, kind: str, args: dict[str, Any]) -> None:
        r = self._c.post(f"/api/v1/seats/{seat}/actions", headers=self._h,
                         json={"kind": kind, "args": args})
        r.raise_for_status()

    def hold(self, seat: str, *, until: float, reason: str,
             release_condition: str) -> None:
        self._act(seat, "hold", {
            "until": until, "reason": reason,
            "release_condition": release_condition,
            "kind": KIND, "owner": OWNER,
        })

    def release(self, seat: str, *, cause: str = "",
                cause_source: str = CAUSE_UNRECORDED) -> None:
        self._act(seat, "release", {
            "owner": OWNER, "cause": cause, "cause_source": cause_source,
        })


@dataclass
class Report:
    """What one pass SAW and did. The rows are kept verbatim: `held 0` is
    what an empty answer prints and also what a scanner reading a live list
    under unknown keys would print, so the counts cannot testify alone."""

    asked: bool = False
    dry_run: bool = False
    held: list[str] = field(default_factory=list)
    re_held: list[str] = field(default_factory=list)
    released: list[str] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)
    left_alone: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    unparsable: int = 0
    keys_seen: list[str] = field(default_factory=list)
    unknown_exempt: list[str] = field(default_factory=list)
    candidates_seen: list[Any] = field(default_factory=list)
    left_out_seen: list[Any] = field(default_factory=list)
    error: str = ""

    def line(self) -> str:
        if not self.asked:
            return ("hibernate: no pass — the candidate list could not be "
                    f"read ({self.error or 'no answer'}); nothing held, "
                    "nothing released")
        if self.unparsable:
            return ("hibernate: NO USABLE ANSWER — "
                    f"{self.unparsable} candidate row(s) carried no `lane` "
                    f"(keys seen: {', '.join(self.keys_seen) or 'none'}); "
                    "nothing held, nothing released")
        out = (f"hibernate: held {len(self.held)}, re-held "
               f"{len(self.re_held)}, released {len(self.released)}, "
               f"refused {len(self.refused)}")
        if self.dry_run:
            out += " (dry run — nothing written)"
        if self.unknown_exempt:
            out += (f"; exempt list names no seat: "
                    f"{', '.join(self.unknown_exempt)} — every hibernation "
                    f"refused until it resolves")
        return out


def _lane_of(row: Any) -> str:
    if not isinstance(row, dict):
        return ""
    lane = row.get("lane")
    return lane.strip() if isinstance(lane, str) else ""


def _text(row: dict[str, Any], key: str) -> str:
    v = row.get(key)
    return v if isinstance(v, str) else ""


def scan(console: Any, hub: Any, thread: Any, now: float,
         dry_run: bool = False) -> Report:
    """One pass. Never raises on a console or hub failure — reports it."""
    rep = Report(dry_run=dry_run)
    try:
        payload = console.candidates(thread)
        if not isinstance(payload, dict):
            raise ValueError(f"answer is a {type(payload).__name__}, "
                             "not a mapping")
    except Exception as exc:  # noqa: BLE001 — an unanswered pass does nothing
        rep.error = str(exc)[:200]
        return rep
    rep.asked = True
    rows = payload.get("candidates") or []
    left_out = payload.get("left_out") or []
    rep.candidates_seen = list(rows) if isinstance(rows, list) else [rows]
    rep.left_out_seen = list(left_out) if isinstance(left_out, list) else []
    rep.unknown_exempt = [str(n) for n in
                          payload.get("unknown_exempt_names") or []]

    # Step 1 — every row must be readable, or nothing happens at all.
    keys: set[str] = set()
    for row in rep.candidates_seen:
        if not _lane_of(row):
            rep.unparsable += 1
            keys.update(row.keys() if isinstance(row, dict)
                        else {f"<{type(row).__name__}>"})
    if rep.unparsable:
        rep.keys_seen = sorted(str(k) for k in keys)
        return rep

    candidates = {_lane_of(r): r for r in rep.candidates_seen}
    why_left_out = {_lane_of(r): _text(r, "why") for r in rep.left_out_seen
                    if _lane_of(r)}

    def state_of(seat: str) -> dict[str, Any] | None:
        return edge._hold_state(hub.actions(seat), now)

    def ours(state: dict[str, Any] | None) -> bool:
        return bool(state) and state.get("kind") == KIND \
            and state.get("owner") == OWNER

    # Step 2 — park every candidate, as a fresh entry.
    for lane, row in candidates.items():
        if rep.unknown_exempt:
            rep.refused.append(lane)
            rep.reasons.append(
                f"{lane}: refused — exempt list names no seat "
                f"({', '.join(rep.unknown_exempt)})")
            continue
        try:
            state = state_of(lane)
            if state and not ours(state):
                # Another mechanism's live hold (a brake). Writing over it
                # would make this scanner's hold the newest and so the one
                # its owner can no longer release.
                rep.left_alone.append(lane)
                rep.reasons.append(
                    f"{lane}: left alone — held by "
                    f"'{state.get('owner') or 'unowned'}'")
                continue
            if not dry_run:
                hub.hold(lane, until=now + TTL_SECONDS,
                         reason=_text(row, "why") or _NO_WHY,
                         release_condition=_text(row, "release") or _NO_RELEASE)
        except Exception as exc:  # noqa: BLE001 — one refusal ends nothing
            rep.refused.append(lane)
            rep.reasons.append(f"{lane}: {str(exc)[:160]}")
            continue
        (rep.re_held if ours(state) else rep.held).append(lane)

    # Step 3 — release this scanner's own live holds on lanes no longer
    # listed. Runs even when holds were refused: releasing is the safe way.
    try:
        seats = hub.seats()
    except Exception as exc:  # noqa: BLE001
        rep.reasons.append(f"releases skipped — seat list unreadable: "
                           f"{str(exc)[:160]}")
        return rep
    for seat in seats:
        if seat in candidates:
            continue
        try:
            if not ours(state_of(seat)):
                continue
            cause = why_left_out.get(seat, "")
            if not dry_run:
                hub.release(seat, cause=cause,
                            cause_source=(CAUSE_FROM_CONSOLE if cause
                                          else CAUSE_UNRECORDED))
        except Exception as exc:  # noqa: BLE001
            rep.reasons.append(f"{seat}: release failed — {str(exc)[:160]}")
            continue
        rep.released.append(seat)
    return rep
