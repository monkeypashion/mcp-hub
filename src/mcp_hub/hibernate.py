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

The shape, from the accepted design note: **query -> hold -> re-hold ->
release on the stated condition**, all four inside this verb.

🔴 **RE-HOLD IS A FRESH ENTRY AFTER A FRESH QUERY, NEVER AN EXPIRY BUMP.**
Bumping an `until` keeps a lane parked on a reason nobody has re-checked —
the hold would outlive the fact that justified it, and the lane would have no
way to tell. Every pass asks the console again, and a lane still listed is
re-held as a new entry.

🔴 **A NOMINATION LIST IS NOT A RELEASE SIGNAL** — the deputy's ruling of
2026-09-21, his word under #465; the formulation is squad-proxy's. Until then
this scanner released every lane that had merely DROPPED OFF the candidate
list and called that "release on bar assignment". It is not. The console's
list answers *"is this lane a reasonable thing to hold right now?"*; the hold
asked *"has a bar landed?"*. Releasing on not-Y when the lane was told Y is
the hold not meaning what it said — and it was never a rare misreading: on
2026-09-20 the armed timer produced 3 holds, 3 automatic releases and 0 by
assignment, because the hold GUARANTEES the lane a turn (`cli.py` :931-:976,
where a held lane is the one path that bypasses the loop backstop), the turn
writes the transcript, and the write resets the very quiet clock that
nominated the lane. **The hold manufactured the evidence that disqualified
it**, every time.

**THE RULED PREDICATE — THREE ARMS, AND A HOLD MUST NEVER BE HARDER TO LEAVE
THAN TO ENTER.** Holding is the destructive act; being named costs nothing.
So a held lane is freed when, and only when:

  · `condition-met`  — the console states the lane owns an open bar
                       (`left_out[].bars_open >= 1`). The stated condition.
  · `condition-unreadable` — the check could not be made at all: no row for
                       the lane, or no readable `bars_open` on it. Ambiguity
                       RELEASES and says so. A lane stranded on a reason
                       nobody can read is the failure this refuses to trade
                       today's defect for.
  · `ttl`            — the hold reached its expiry with the condition still
                       unestablished. See `TTL_SECONDS`.

and it STANDS in exactly one case: the console says, readably, that the lane
owns no open bar. That case is what the old predicate got wrong, and it is
the whole of the change — a lane that merely TAKES A TURN now stays held.

📌 **EVERY RELEASE RECORDS WHICH ARM FIRED** (`args.release_arm`), beside the
`cause` words themselves and the `cause_source` that says whose words they
are. The `cause` field (2026-09-07) is the only reason any of this was ever
distinguishable, and a release with no readable arm is the same defect in a
new hat. ⚠️ **Discriminate on the ARM, never on latency**: squad-proxy's
door now re-reads each candidate's write age fresh at verdict time, so
releases got FASTER while the predicate stayed wrong. A shortened gap is not
evidence of anything here, and nobody quotes it as progress.
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

# Where a release's `cause` came from. Recorded beside the words themselves
# because "the console said this" and "nobody said anything" are different
# readings, and a blank `cause` alone cannot tell them apart — the same
# empty-vs-never confusion that made me file a lifetime count of 0 twice
# from a file that only holds NOW.
CAUSE_FROM_CONSOLE = "console left_out"
CAUSE_UNRECORDED = "absent from the console's left_out"
# The TTL arm is the one release whose words are the SCANNER'S OWN: its clock
# ran out, and no console sentence says so. Recorded as such, because the
# rule everywhere else is that this mechanism COPIES a cause and never
# composes one — and an exception that does not announce itself reads as the
# rule.
CAUSE_FROM_SCANNER = "the scanner's own clock"

# ⭐ TWO HOURS — A DECISION, NOT AN INHERITANCE (2026-09-21). It was twelve,
# from the accepted note ("rolling 12h re-hold"), where the expiry was a pure
# backstop: the thing that releases the whole fleet when the scanner STOPS
# RUNNING. Under the ruled predicate the TTL is LOAD-BEARING — it is the only
# arm that ever frees a lane the console readably says owns no bar — so the
# number had to be set deliberately rather than inherited. Twelve hours is a
# long time to be wrong about a live lane, and a wrongly-held lane pays a
# model turn at every Stop boundary for the whole of it.
#
# Why two hours, in the terms this mechanism actually measures:
#   · the only constant anyone has MEASURED here is the 60-minute quiet
#     window that nominates a lane. A hold may stand for two of those
#     without a fresh affirmative decision; beyond that it has outlived any
#     evidence of the kind that placed it;
#   · it is eight of the 15-minute pass intervals, so a hold outliving its
#     last fresh query reads as several missed passes, never as one blip;
#   · a scanner that dies frees the fleet inside one working block.
# A lane still listed is re-held every pass, so this bounds the UNLISTED
# hold — which is precisely the case the ruling created.
TTL_SECONDS = 2 * 3600.0

# The interval this scanner is run on, and the reason it has to be named
# here: a hold that simply ELAPSES releases itself INSIDE the hub
# (`edge._hold_state` reports an expired hold as no hold at all) and writes
# no release row at all. The TTL would then free lanes with no recorded
# cause — the one thing the ruling forbids, since every release must say
# which arm fired. So the last pass that can still speak ends the hold
# explicitly. ⚠️ A missed pass still lapses the hold safely; only its cause
# is lost, which is the right direction to fail in.
TTL_MARGIN_SECONDS = 15 * 60.0

# WHICH ARM FREED THE LANE. Recorded verbatim on every release, and the only
# thing a later reader may partition on — see the docstring on latency.
ARM_CONDITION = "condition-met"
ARM_UNREADABLE = "condition-unreadable"
ARM_TTL = "ttl"


@dataclass
class Report:
    """What one pass did, by enumeration. Never a boolean: a pass that held
    nothing and a pass that could not ask are different answers, and a
    caller that cannot tell them apart will read a broken console as a
    quiet fleet."""

    asked: bool = False           # did the candidate query actually answer?
    # ⚠️ A dry pass fills the SAME lists as a real one, so the counts read
    # identically. On 2026-09-05 an unarmed run printed "held 7" having held
    # nobody — a row that says a thing was done when it was not is the whole
    # false-green shape, banner above it or not. The wording carries it.
    dry: bool = False
    # ⭐ CANDIDATE ROWS THE PARSER COULD NOT USE. Not a count for curiosity:
    # `held 0, re-held 0, released 0` is what this pass printed for 94
    # consecutive passes with a genuinely empty list, and a console row
    # carrying its lane under a key this scanner does not read prints THE
    # SAME LINE. The two readings are "the fleet is quiet" and "I cannot see
    # the fleet", and nothing downstream could tell them apart until the
    # narrowed clause (2026-09-08) made the list non-empty for the first
    # time. A pass that cannot parse its answer REFUSES; it does not report.
    unparsable: int = 0
    held: list[str] = field(default_factory=list)
    re_held: list[str] = field(default_factory=list)
    released: list[str] = field(default_factory=list)
    # ⭐ HOLDS THAT THIS PASS DELIBERATELY LEFT IN PLACE — the state the
    # ruled predicate created and the old one could not have. A lane the
    # console readably says owns no bar is not released and is not re-held
    # either (it is no longer listed), so without this list it appears in no
    # count at all and the pass reads as though the fleet were free. It is
    # also the only surface on which the MUST-FIRE case is visible at all:
    # "held lane took a turn, hold STANDS" prints here, once, per pass.
    standing: list[str] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    # ⭐ WHY EACH RELEASE HAPPENED, as {lane: arm}. The counts above cannot
    # tell a release on the stated condition from one on an unreadable check
    # or on the clock, and those are three different events — one of them
    # bar 59's clause and two of them not. Kept beside the row rather than
    # only on the write, so a REPORT-ONLY pass says which arm WOULD fire.
    arms: dict[str, str] = field(default_factory=dict)
    # ⭐ THE CONSOLE'S ANSWER, VERBATIM — the rows as they arrived, not a
    # summary of them. Every count above reads identically whether the fleet
    # is quiet or this scanner is blind to it, and for 94 passes nothing
    # downstream could tell those apart. The rows themselves are the only
    # witness: a report-only pass prints them, so a reader who KNOWS a lane
    # has been quiet >= 60 min can see whether the console said so, whether
    # it said so under a key this parser does not read, or whether it left
    # the lane out and why. Held verbatim (no coercion, non-mappings
    # included) — a row normalised on the way in cannot testify about its
    # own shape.
    candidates_seen: list[Any] = field(default_factory=list)
    left_out_seen: list[Any] = field(default_factory=list)

    def line(self) -> str:
        if not self.asked:
            return f"hibernate: no pass — {'; '.join(self.reasons)}"
        if self.unparsable:
            # Leads with the refusal on purpose. A reader who sees "held 0"
            # first has already drawn the wrong conclusion by the time the
            # reason arrives at the end of the line.
            return ("hibernate: NO USABLE ANSWER — "
                    + '; '.join(self.reasons))
        w = "would hold" if self.dry else "held"
        parts = [
            f"{w} {len(self.held)}",
            f"{'would re-hold' if self.dry else 're-held'} {len(self.re_held)}",
            f"{'would release' if self.dry else 'released'} {len(self.released)}",
        ]
        # Printed unconditionally once there is one: a standing hold is a
        # lane this pass chose to leave parked, and a line that omits it
        # reads exactly like a pass that parked nobody.
        if self.standing:
            parts.append(f"{len(self.standing)} still held")
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
    """The hold-writing slice of /api/v1, under the MANAGEMENT API token.

    ⛔ 2026-09-05: this class shipped sending `x-mcp-hub-operator-token`, a
    header `api_v1.auth()` NEVER READS — so every call, with any token, was
    401 "missing bearer token". I read a 401 from a fake token as proof the
    door worked; a 401 cannot tell a bad token from a bad SCHEME, and the
    instrument I used could not have reported this failure. The scheme is
    `Authorization: Bearer <token>`, exactly as `edge.HubAPI` already did.
    ⚠️ TWO SECRETS, ONE WORD "operator". `MCP_HUB_API_TOKEN` (this one, and
    `auth()`'s operator PRINCIPAL) is not `MCP_HUB_OPERATOR_TOKEN`, which
    only grades operator-named sends and is read from its own header. I
    recorded that distinction in the console's own source on 2026-09-03 and
    then contradicted it here two days later.

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
        self._h = {"Authorization": f"Bearer {token}"}

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

    def release(self, seat: str, *, cause: str = "",
                cause_source: str = CAUSE_UNRECORDED,
                arm: str = ARM_UNREADABLE) -> None:
        # `owner` is not decoration here: the hub refuses a release whose
        # owner differs from the hold's, which is what stops this scanner
        # lifting a brake and handing a lane its share back in the middle of
        # the window the brake was protecting.
        #
        # ⭐ 2026-09-07 — `cause` EXISTS SO BAR 59 CAN BE MEASURED AT ALL.
        # The hold row carries `reason` and `release_condition`; the release
        # row carried nothing but the owner, so six automatic releases could
        # not be told apart from each other. The bar's clause is "released
        # automatically WHEN IT IS ASSIGNED AN OPEN BAR", and the scanner
        # releases whoever stopped being a candidate for ANY cause — a bar
        # landing, an exemption changing, a seat vanishing, the console's
        # list being regated. Without this field the clause is unevidenceable
        # even on a day it genuinely works, which is not a bar being unmet,
        # it is a bar that cannot be read.
        #
        # 🔴 The scanner does NOT decide the cause, it COPIES it. `cause` is
        # the console's own `left_out[].why`, verbatim, and `cause_source`
        # says where it came from — the alternative is a mechanism grading
        # its own behaviour against the criterion it is judged by, which is
        # marking your own homework in the one place it matters most.
        #
        # 📌 2026-09-21 — `release_arm` WIDENS THE SAME IDEA, on the deputy's
        # ruling: `cause` says what was said about the lane, `release_arm`
        # says WHICH OF THE THREE ARMS of the predicate actually fired. The
        # two are not redundant. A row reading cause="owns an open bar here"
        # is the clause; the identical sentence arriving on a lane whose
        # `bars_open` could not be read is NOT, and only the arm tells them
        # apart. It is also what a reader must partition on instead of the
        # release LATENCY, which moved for an unrelated reason while the
        # predicate was still wrong.
        r = self._c.post(
            f"/api/v1/seats/{seat}/actions", headers=self._h,
            json={"kind": "release", "args": {
                "owner": OWNER, "cause": cause,
                "cause_source": cause_source,
                "release_arm": arm}},
        )
        r.raise_for_status()


def held_state(hub: HubHolds, seat: str, now: float) -> dict[str, Any] | None:
    """This scanner's live hold on the seat, or None.

    Computed with `edge._hold_state` on purpose — the same function the edge
    uses and `test_hold_kind_and_owner.py` pins against the hub's own
    `_live_hold`. A third implementation of "is it held" is a third chance
    for the three to disagree, and the disagreement would show up as a lane
    that one surface calls parked and another calls free.

    ⭐ Returns the STATE, not a boolean, because the TTL arm needs the hold's
    own `until` and a caller that can only ask "is it held" cannot see the
    clock it is judged against.
    """
    try:
        state = _hold_state(hub.actions(seat), now)
    except Exception:  # noqa: BLE001 — an unreadable seat is not a held one
        return None
    if state and state.get("owner") == OWNER:
        return state
    return None


def mine(hub: HubHolds, seat: str, now: float) -> bool:
    """Is this seat currently held BY THIS SCANNER?"""
    return held_state(hub, seat, now) is not None


def bars_open(row: Any) -> int | None:
    """The console's open-bar count for a lane — or None for "cannot say".

    🔴 ABSENT IS NOT ZERO, and the distinction carries the whole ruling.
    Zero means the console looked and the lane owns nothing, which is the one
    reading that keeps a hold standing. Absent means nobody answered the
    question, which RELEASES. Collapsing the two would strand lanes on a
    reason nobody could read — the failure the deputy refused to trade the
    old defect for.

    ⚠️ The structured field ONLY. Several `left_out[].why` sentences say in
    prose what `bars_open` would say in a number ("owns a bar blocked until a
    date/bar — still work" carries no count at all), and matching on another
    service's prose is a check written against the wrong surface of the same
    answer. An unreadable row releases and says so; it does not get parsed
    harder.

    🔴 OPEN, AND IT GATES THE FIRST ARMED PASS — the two fields disagree on
    at least one live row. 2026-09-21 09:5xZ, verbatim from the console:
    `{"lane": "vps-hetzner-dev-vm-1", "bars_open": 2, "why": "ACTIVE BUT
    UNOWNED — a main-session response 1750s ago ..."}`. A count of 2 and a
    sentence saying the lane owns nothing cannot both be the answer to "is
    this lane assigned an open bar". The likeliest reading is that
    `bars_open` counts owner-OR-EXECUTOR while the nomination test reads
    owner only (`GET /cards?lane=` is documented as the former) — but that
    is a guess about somebody else's field, and this scanner has already
    been wrong once about an instrument it does not own. ASKED of
    squad-proxy-dev-vm-1, who own the door. Until they answer, the
    contradiction is LEGIBLE rather than hidden: the arm and the console's
    sentence travel together on every release row, so a `condition-met`
    carrying an "ACTIVE BUT UNOWNED" cause is visible as exactly that, and
    must not be quoted as the clause.
    """
    if not isinstance(row, dict):
        return None
    v = row.get("bars_open")
    # `isinstance(True, int)` is True in Python, and a boolean here would
    # read as 1 bar or 0 bars — an answer, from a field that gave none.
    if isinstance(v, bool) or not isinstance(v, int):
        return None
    return v


def release_arm(row: Any, state: dict[str, Any] | None, now: float,
                margin: float = TTL_MARGIN_SECONDS,
                ) -> tuple[str, str, str] | None:
    """Which arm frees this held lane — or None, meaning the hold STANDS.

    Returns `(arm, cause, cause_source)`. The order is the ruling's: the
    stated condition first, so a lane that genuinely got a bar records THAT
    even when its clock was also nearly out; then the unreadable check, which
    releases on ambiguity; then the TTL. `None` is returned for exactly one
    input — a readable row saying the lane owns no open bar.
    """
    have_row = isinstance(row, dict)
    why = str(row.get("why") or "") if have_row else ""
    src = CAUSE_FROM_CONSOLE if have_row else CAUSE_UNRECORDED
    n = bars_open(row)
    if n is not None and n >= 1:
        return ARM_CONDITION, why, src
    if n is None:
        return ARM_UNREADABLE, why, src
    until = float((state or {}).get("until") or 0.0)
    if until - now <= margin:
        # The scanner's own sentence, and it says so in `cause_source`. The
        # console's last word travels with it: a reader asking why a lane sat
        # parked for the whole TTL wants the reason it kept failing the
        # check, not just the fact that it did.
        return (ARM_TTL,
                "the hold reached its TTL with its release condition still "
                "unestablished; the console's last word on this lane was: "
                + (why or "(nothing)"),
                CAUSE_FROM_SCANNER)
    return None


def scan(console: ConsoleAPI, hub: HubHolds, *, thread: int | str = 1,
         now: float | None = None, ttl: float = TTL_SECONDS,
         ttl_margin: float = TTL_MARGIN_SECONDS,
         dry_run: bool = False) -> Report:
    """One pass: fresh query, hold the candidates, release everyone else."""
    now = time.time() if now is None else now
    rep = Report(dry=dry_run)

    # 1. THE FRESH QUERY. If it cannot be answered, the pass does NOTHING —
    #    not even releases. Absence of an answer is not an answer, and a
    #    network blip would otherwise unpark the whole fleet for one pass and
    #    re-park it on the next.
    #    ⚠️ THIS IS THE ONE PLACE THE RULING'S "AN UNREADABLE CHECK RELEASES"
    #    MEETS THE OLDER ANTI-THRASH REFUSAL, and the choice is deliberate
    #    rather than inherited: the unreadable ARM covers a lane the console
    #    DID answer about and could not establish; a console that answers
    #    nothing at all is not a judgement on any particular lane, so the
    #    holds are left to the TTL — which is now 2h rather than 12h, and is
    #    load-bearing here precisely because of this branch. Stated to the
    #    deputy rather than decided quietly: if he wants a dead console to
    #    free the fleet at once, this branch is the one line to change.
    try:
        payload = console.candidates(thread)
    except Exception as exc:  # noqa: BLE001
        rep.reasons.append(f"the candidate list could not be read ({exc})")
        return rep
    rep.asked = True

    # 1b. A ROW WE CANNOT PARSE IS NOT AN ABSENT ROW — and for 94 passes the
    #     difference was invisible, because the list was empty and every key
    #     name below was therefore untested against the live console. The
    #     narrowed clause makes `candidates[]` non-empty for the first time,
    #     so a key-name mismatch across that boundary would land as a clean
    #     "held 0" and be read as a quiet fleet. Refuse the whole pass —
    #     releases included, because a partially-read list would unpark lanes
    #     that are still candidates under a key we failed to read, which is
    #     exactly the hazard step 1 refuses a network blip for.
    #     `isinstance` rather than duck-typing: a row that is not a mapping
    #     at all would raise on `.get` and take the whole pass down with a
    #     traceback. Loud beats silent, but REFUSED-and-legible beats both.
    #     Captured BEFORE the refusal below, and kept even when the pass
    #     refuses: a pass that could not read its answer is exactly the pass
    #     whose answer someone needs to look at.
    raw = payload.get("candidates") or []
    rep.candidates_seen = list(raw)
    rep.left_out_seen = list(payload.get("left_out") or [])
    usable = [c for c in raw if isinstance(c, dict) and c.get("lane")]
    cands = {str(c["lane"]): c for c in usable}
    unusable = [c for c in raw
                if not (isinstance(c, dict) and c.get("lane"))]
    if unusable:
        seen = sorted({str(k) for c in unusable if isinstance(c, dict)
                       for k in c})
        rep.unparsable = len(unusable)
        rep.reasons.append(
            f"{len(unusable)} of {len(raw)} candidate rows carry no `lane` "
            f"(keys present: {', '.join(seen) or 'none'}) — nothing held and "
            "nothing released this pass, because a list I cannot fully read "
            "must not be reported as an empty one")
        return rep

    # THE CONSOLE ALREADY SAYS WHY EACH LANE IS NOT A CANDIDATE, and this
    # scanner used to throw that away one line before the release that needed
    # it. `left_out[].why` is the console's own sentence — "owns an open bar
    # here", "exempt seat", "NO HUB SEAT ..." — and it is the only witness to
    # the difference the bar's clause turns on. Read here, at the fresh query,
    # so the words travelling into the release row are the ones from the same
    # answer that decided the release.
    # ⚠️ THE WHOLE ROW, not just its sentence. Until 2026-09-21 this kept
    # only `why`, because prose was all the release needed when any absence
    # was a release. The ruled predicate turns on `bars_open`, which is on
    # the same row and was being discarded one line before the decision that
    # needed it — the same shape as throwing `why` away used to be.
    left_out = {str(o.get("lane")): o
                for o in (payload.get("left_out") or [])
                if isinstance(o, dict) and o.get("lane")}

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

    # 4. RELEASE — ON THE STATED CONDITION, on an unreadable check, or on the
    #    clock. NOT on absence from the list: that is the 2026-09-21 ruling
    #    and the reason this loop exists in its present shape. `release_arm`
    #    holds the whole decision; what is left here is doing it to seats
    #    this scanner actually holds, by the owner-scoped release, and
    #    reporting which arm fired for each one.
    try:
        seats = hub.seats()
    except Exception as exc:  # noqa: BLE001
        rep.reasons.append(f"the seat list could not be read ({exc})")
        return rep
    for seat in sorted(seats):
        if seat in cands:
            continue
        state = held_state(hub, seat, now)
        if state is None:
            continue
        verdict = release_arm(left_out.get(seat), state, now, ttl_margin)
        if verdict is None:
            # ⭐ THE MUST-FIRE CASE, and the only branch that says nothing to
            # the hub. The console looked and the lane owns no open bar, so
            # the condition the lane was given is simply not met yet. A lane
            # that merely took a turn lands here — which is the whole point
            # of the change, and why it is REPORTED rather than passed over.
            rep.standing.append(seat)
            continue
        arm, cause, source = verdict
        rep.arms[seat] = arm
        if dry_run:
            rep.released.append(seat)
            continue
        try:
            hub.release(seat, cause=cause, cause_source=source, arm=arm)
        except Exception as exc:  # noqa: BLE001
            rep.reasons.append(f"{seat}: release refused ({exc})")
            rep.arms.pop(seat, None)
            continue
        rep.released.append(seat)
    return rep
