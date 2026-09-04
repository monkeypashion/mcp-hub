"""Bar 59 — the hibernate verb: park a lane that has nothing open.

The hub half (a hold carrying a `kind` and an `owner`, travelling hub -> edge
-> mirror -> lane) shipped in `89bbc7d`. This is the other half: the SCANNER
that decides who gets parked, arms the holds, re-holds them while the reason
still stands, and releases them the moment it stops standing.

⚠️ **It is mine, and I got that wrong once.** I grepped this repo for the
definition of `GET /threads/{id}/hibernation-candidates`, found nothing, and
concluded the scanner belonged to whoever owned the endpoint. A CONSUMER
never contains the endpoint it consumes. The recorded contract of that
endpoint is *"the list mcp-hub's hibernate verb reads; the console never
holds anyone"* — the console offers a list and places no hold; the holding
is here.

The shape, from the accepted design note: **query -> hold -> 12h re-hold ->
release on bar assignment**, all four inside this verb.

🔴 **RE-HOLD IS A FRESH ENTRY AFTER A FRESH QUERY, NEVER AN EXPIRY BUMP.**
Bumping an `until` keeps a lane parked on a reason nobody has re-checked —
the hold would outlive the fact that justified it, and the lane would have no
way to tell. Every pass asks the console again, and a lane that has stopped
being a candidate is RELEASED rather than renewed. That is what "release on
bar assignment" means in practice: the bar lands, the console stops listing
the lane, and the next pass lets it go.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from mcp_hub.edge import _hold_state

# The mechanism's name on every hold it places. Release is owner-scoped on
# the hub, so this string is what stops the scanner lifting somebody's brake.
OWNER = "hibernation-scanner"
KIND = "hibernation"

# ⭐ TWELVE HOURS, from the accepted note ("rolling 12h re-hold"). It is the
# expiry, not the policy: the policy is the re-hold above. The expiry exists
# so that a scanner which STOPS RUNNING releases the whole fleet by itself
# within half a day, rather than leaving lanes parked by a mechanism nobody
# is running any more. A hold that cannot expire is the one shape this verb
# must never have.
TTL_SECONDS = 12 * 3600.0


@dataclass
class Report:
    """What one pass did, by enumeration. Never a boolean: a pass that held
    nothing and a pass that could not ask are different answers, and a
    caller that cannot tell them apart will read a broken console as a
    quiet fleet."""

    asked: bool = False           # did the candidate query actually answer?
    held: list[str] = field(default_factory=list)
    re_held: list[str] = field(default_factory=list)
    released: list[str] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    def line(self) -> str:
        if not self.asked:
            return f"hibernate: no pass — {'; '.join(self.reasons)}"
        parts = [
            f"held {len(self.held)}", f"re-held {len(self.re_held)}",
            f"released {len(self.released)}",
        ]
        if self.refused:
            parts.append(f"REFUSED {len(self.refused)}")
        out = "hibernate: " + ", ".join(parts)
        if self.reasons:
            out += " — " + "; ".join(self.reasons)
        return out


class ConsoleAPI:
    """Read-only door onto the candidate list. The console never holds."""

    def __init__(self, base_url: str = "", client: Any = None) -> None:
        if client is None:
            import httpx

            client = httpx.Client(base_url=base_url or "", timeout=15)
        self._c = client

    def candidates(self, thread: int | str) -> dict[str, Any]:
        r = self._c.get(f"/threads/{thread}/hibernation-candidates")
        r.raise_for_status()
        return r.json()


class HubHolds:
    """The hold-writing slice of /api/v1, under the OPERATOR token.

    Same injected-client shape as `edge.HubAPI`, for the same reason: the
    whole loop is then testable in-process against the real API, with no
    socket and no fixture pretending to be one.
    """

    def __init__(self, base_url: str = "", token: str = "",
                 client: Any = None) -> None:
        if client is None:
            import httpx

            client = httpx.Client(base_url=base_url or "", timeout=30)
        self._c = client
        self._h = {"x-mcp-hub-operator-token": token}

    def seats(self) -> list[str]:
        r = self._c.get("/api/v1/seats", headers=self._h)
        r.raise_for_status()
        return [s["identity"] for s in r.json().get("seats", [])
                if s.get("identity")]

    def actions(self, seat: str) -> list[dict[str, Any]]:
        r = self._c.get(f"/api/v1/seats/{seat}/actions", headers=self._h)
        r.raise_for_status()
        return r.json().get("actions", [])

    def hold(self, seat: str, *, until: float, reason: str,
             release_condition: str) -> None:
        r = self._c.post(
            f"/api/v1/seats/{seat}/actions", headers=self._h,
            json={"kind": "hold", "args": {
                "until": until, "reason": reason,
                "release_condition": release_condition,
                "kind": KIND, "owner": OWNER}},
        )
        r.raise_for_status()

    def release(self, seat: str) -> None:
        # `owner` is not decoration here: the hub refuses a release whose
        # owner differs from the hold's, which is what stops this scanner
        # lifting a brake and handing a lane its share back in the middle of
        # the window the brake was protecting.
        r = self._c.post(
            f"/api/v1/seats/{seat}/actions", headers=self._h,
            json={"kind": "release", "args": {"owner": OWNER}},
        )
        r.raise_for_status()


def mine(hub: HubHolds, seat: str, now: float) -> bool:
    """Is this seat currently held BY THIS SCANNER?

    Computed with `edge._hold_state` on purpose — the same function the edge
    uses and `test_hold_kind_and_owner.py` pins against the hub's own
    `_live_hold`. A third implementation of "is it held" is a third chance
    for the three to disagree, and the disagreement would show up as a lane
    that one surface calls parked and another calls free.
    """
    try:
        state = _hold_state(hub.actions(seat), now)
    except Exception:  # noqa: BLE001 — an unreadable seat is not a held one
        return False
    return bool(state) and state.get("owner") == OWNER


def scan(console: ConsoleAPI, hub: HubHolds, *, thread: int | str = 1,
         now: float | None = None, ttl: float = TTL_SECONDS,
         dry_run: bool = False) -> Report:
    """One pass: fresh query, hold the candidates, release everyone else."""
    now = time.time() if now is None else now
    rep = Report()

    # 1. THE FRESH QUERY. If it cannot be answered, the pass does NOTHING —
    #    not even releases. With no list, every held lane looks like "no
    #    longer a candidate", so a network blip would unpark the whole fleet
    #    and the next pass would re-park it. Absence of an answer is not an
    #    answer, and the 12h expiry is the backstop that makes doing nothing
    #    safe here.
    try:
        payload = console.candidates(thread)
    except Exception as exc:  # noqa: BLE001
        rep.reasons.append(f"the candidate list could not be read ({exc})")
        return rep
    rep.asked = True

    cands = {str(c.get("lane")): c for c in (payload.get("candidates") or [])
             if c.get("lane")}

    # 2. AN EXEMPT LIST WE CANNOT FULLY RESOLVE REFUSES EVERY HIBERNATION.
    #    The hub enforces this at the write; refusing here too is not a
    #    second gate, it is the difference between one legible refusal and N
    #    identical API errors. An exempt entry naming no known seat is a lane
    #    that believes it is protected and is not — and nothing reveals that
    #    until the lane it was written for is parked.
    #    🔴 Releases still run. Releasing is always the safe direction, and a
    #    typo in the exempt list must not strand lanes already parked.
    unknown = [str(n) for n in (payload.get("unknown_exempt_names") or [])]
    if unknown:
        rep.reasons.append(
            "the exempt list names " + ", ".join(sorted(unknown))
            + " — no seat by that name, so every hibernation is refused "
              "this pass (releases still run)")

    # 3. HOLD / RE-HOLD each candidate. Both are the same write: a fresh
    #    entry, placed after the fresh query above.
    for lane, c in sorted(cands.items()):
        if unknown:
            rep.refused.append(lane)
            continue
        already = mine(hub, lane, now)
        if dry_run:
            (rep.re_held if already else rep.held).append(lane)
            continue
        try:
            hub.hold(lane, until=now + ttl,
                     reason=str(c.get("why") or "nothing open"),
                     release_condition=str(c.get("release") or
                                           "a bar is assigned to this lane"))
        except Exception as exc:  # noqa: BLE001 — one lane, not the pass
            rep.refused.append(lane)
            rep.reasons.append(f"{lane}: hold refused ({exc})")
            continue
        (rep.re_held if already else rep.held).append(lane)

    # 4. RELEASE — the bar landed, the console stopped listing the lane.
    #    Only lanes THIS scanner holds are touched, and only ever by the
    #    owner-scoped release.
    try:
        seats = hub.seats()
    except Exception as exc:  # noqa: BLE001
        rep.reasons.append(f"the seat list could not be read ({exc})")
        return rep
    for seat in sorted(seats):
        if seat in cands or not mine(hub, seat, now):
            continue
        if dry_run:
            rep.released.append(seat)
            continue
        try:
            hub.release(seat)
        except Exception as exc:  # noqa: BLE001
            rep.reasons.append(f"{seat}: release refused ({exc})")
            continue
        rep.released.append(seat)
    return rep
