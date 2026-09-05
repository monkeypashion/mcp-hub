"""Bar 59, the scanner half — query -> hold -> 12h re-hold -> release.

The hub half (`test_hold_kind_and_owner.py`) proves a hold CARRIES a kind and
an owner. This proves the mechanism that places them: who gets parked, who
stops being parked, and — the part that is easy to build wrong — that a
re-hold is a fresh entry after a fresh query rather than an expiry bump.
"""

from __future__ import annotations

import pytest

from mcp_hub import hibernate
from mcp_hub.hibernate import KIND, OWNER, TTL_SECONDS, ConsoleAPI, HubHolds

NOW = 1_788_500_000.0


class FakeConsole:
    """The read-only candidate door. `boom` makes the pass unanswerable."""

    def __init__(self, candidates=(), unknown=(), boom=False):
        self.payload = {
            "candidates": [dict(c) for c in candidates],
            "left_out": [],
            "unknown_exempt_names": list(unknown),
        }
        self.boom = boom
        self.asked = 0

    def candidates(self, thread):          # noqa: D102
        self.asked += 1
        if self.boom:
            raise RuntimeError("console unreachable")
        return self.payload


class FakeHub:
    """Records every write, and answers `actions` the way the hub does —
    newest id last, `status='done'`, so `edge._hold_state` reads it."""

    def __init__(self, seats, existing=None, refuse=()):
        self._seats = list(seats)
        self._actions = {s: [] for s in seats}
        self._id = 0
        self.refuse = set(refuse)
        for seat, args in (existing or {}).items():
            self._append(seat, "hold", args)

    def _append(self, seat, kind, args):
        self._id += 1
        self._actions.setdefault(seat, []).append(
            {"id": self._id, "kind": kind, "args": dict(args),
             "status": "done", "requested_at": NOW - 10})

    # -- the HubHolds surface -------------------------------------------
    def seats(self):
        return list(self._seats)

    def actions(self, seat):
        return list(self._actions.get(seat, []))

    def hold(self, seat, *, until, reason, release_condition):
        if seat in self.refuse:
            raise RuntimeError("hold refused by the hub")
        self._append(seat, "hold", {
            "until": until, "reason": reason,
            "release_condition": release_condition,
            "kind": KIND, "owner": OWNER})

    def release(self, seat):
        self._append(seat, "release", {"owner": OWNER})

    # -- what the tests read --------------------------------------------
    def holds(self, seat):
        return [a for a in self._actions.get(seat, []) if a["kind"] == "hold"]

    def writes(self, seat):
        return [a["kind"] for a in self._actions.get(seat, [])]


def held_by_scanner(until=NOW + 600):
    return {"until": until, "kind": KIND, "owner": OWNER,
            "reason": "nothing open", "release_condition": "a bar lands"}


def run(console, hub, **kw):
    return hibernate.scan(console, hub, thread=1, now=NOW, **kw)


# --- query -> hold ----------------------------------------------------------

def test_a_candidate_is_parked_with_the_kind_and_the_owner(tmp_path):
    hub = FakeHub(["lane-a", "lane-b"])
    rep = run(FakeConsole([{"lane": "lane-a", "why": "nothing open",
                            "release": "a bar is assigned"}]), hub)
    assert rep.held == ["lane-a"] and rep.released == []
    args = hub.holds("lane-a")[-1]["args"]
    assert args["kind"] == KIND and args["owner"] == OWNER
    assert args["until"] == NOW + TTL_SECONDS
    assert args["reason"] == "nothing open"
    assert args["release_condition"] == "a bar is assigned"
    assert hub.writes("lane-b") == [], "a non-candidate was touched"


def test_the_holds_own_words_come_from_the_candidate_not_from_here():
    """The console said WHY. Substituting our own wording would make the
    hold's stated reason unfalsifiable against the list that caused it."""
    hub = FakeHub(["lane-a"])
    run(FakeConsole([{"lane": "lane-a", "why": "no open bar since 14:02Z",
                      "release": "bar 61 is assigned"}]), hub)
    args = hub.holds("lane-a")[-1]["args"]
    assert args["reason"] == "no open bar since 14:02Z"
    assert args["release_condition"] == "bar 61 is assigned"


# --- the re-hold ------------------------------------------------------------

def test_a_re_hold_is_a_FRESH_ENTRY_never_an_expiry_bump():
    """🔴 The whole point of the rolling re-hold. Bumping an `until` keeps a
    lane parked on a reason nobody re-checked — the hold outlives the fact
    that justified it, and the lane cannot tell."""
    hub = FakeHub(["lane-a"], existing={"lane-a": held_by_scanner()})
    before = dict(hub.holds("lane-a")[0]["args"])
    rep = run(FakeConsole([{"lane": "lane-a", "why": "still nothing open"}]),
              hub)
    assert rep.re_held == ["lane-a"] and rep.held == []
    entries = hub.holds("lane-a")
    assert len(entries) == 2, "the re-hold did not write a new entry"
    assert entries[0]["args"] == before, "the original entry was mutated"
    assert entries[-1]["args"]["until"] == NOW + TTL_SECONDS


def test_every_pass_asks_the_console_again():
    hub = FakeHub(["lane-a"], existing={"lane-a": held_by_scanner()})
    console = FakeConsole([{"lane": "lane-a"}])
    run(console, hub)
    run(console, hub)
    assert console.asked == 2, "a pass re-held without re-asking"


# --- release ----------------------------------------------------------------

def test_a_lane_that_stopped_being_a_candidate_is_RELEASED():
    """'Release on bar assignment', as it actually happens: the bar lands,
    the console stops listing the lane, the next pass lets it go."""
    hub = FakeHub(["lane-a"], existing={"lane-a": held_by_scanner()})
    rep = run(FakeConsole([]), hub)
    assert rep.released == ["lane-a"]
    assert hub.writes("lane-a")[-1] == "release"


def test_a_hold_that_is_NOT_the_scanners_is_never_released():
    """A hibernation scanner lifting a brake would hand a lane its share
    back in the middle of the window the brake was protecting."""
    braked = {"until": NOW + 600, "kind": "brake", "owner": "brake"}
    hub = FakeHub(["lane-a"], existing={"lane-a": braked})
    rep = run(FakeConsole([]), hub)
    assert rep.released == []
    assert hub.writes("lane-a") == ["hold"], "the brake was lifted"


def test_an_unheld_lane_is_not_released_for_the_sake_of_it():
    hub = FakeHub(["lane-a", "lane-b"])
    rep = run(FakeConsole([]), hub)
    assert rep.released == []
    assert hub.writes("lane-a") == [] and hub.writes("lane-b") == []


# --- failing closed ---------------------------------------------------------

def test_an_unanswerable_list_does_NOTHING_not_even_releases():
    """🔴 With no list, every held lane looks like 'no longer a candidate'.
    A network blip would unpark the fleet and the next pass would re-park it.
    Absence of an answer is not an answer."""
    hub = FakeHub(["lane-a"], existing={"lane-a": held_by_scanner()})
    rep = run(FakeConsole(boom=True), hub)
    assert rep.asked is False
    assert rep.held == [] and rep.released == [] and rep.re_held == []
    assert hub.writes("lane-a") == ["hold"]
    assert "could not be read" in rep.line()


def test_an_unresolvable_exempt_name_refuses_every_hibernation():
    """An exempt entry naming no known seat is a lane that believes it is
    protected and is not — and nothing reveals that until it is parked."""
    hub = FakeHub(["lane-a"])
    rep = run(FakeConsole([{"lane": "lane-a"}], unknown=["lane-typo"]), hub)
    assert rep.held == [] and rep.refused == ["lane-a"]
    assert hub.writes("lane-a") == []
    assert "lane-typo" in rep.line()


def test_the_same_typo_does_NOT_strand_a_lane_already_parked():
    """Releasing is always the safe direction. A broken exempt list must not
    hold the fleet down as well as refusing to add to it."""
    hub = FakeHub(["lane-a", "lane-b"],
                  existing={"lane-b": held_by_scanner()})
    rep = run(FakeConsole([{"lane": "lane-a"}], unknown=["lane-typo"]), hub)
    assert rep.refused == ["lane-a"]
    assert rep.released == ["lane-b"], "a typo stranded a parked lane"


def test_one_refused_hold_does_not_end_the_pass():
    hub = FakeHub(["lane-a", "lane-b"], refuse=["lane-a"])
    rep = run(FakeConsole([{"lane": "lane-a"}, {"lane": "lane-b"}]), hub)
    assert rep.refused == ["lane-a"] and rep.held == ["lane-b"]
    assert any("lane-a" in r for r in rep.reasons)


def test_an_expired_hold_is_not_this_scanners_any_more():
    """The expiry is the backstop: a scanner that stops running releases the
    fleet by itself within half a day. An elapsed hold has already released
    itself, so there is nothing to release and nothing to re-hold."""
    hub = FakeHub(["lane-a"],
                  existing={"lane-a": held_by_scanner(until=NOW - 1)})
    rep = run(FakeConsole([]), hub)
    assert rep.released == []
    assert hub.writes("lane-a") == ["hold"]


# --- the report -------------------------------------------------------------

def test_a_dry_run_writes_nothing_and_still_says_what_it_would_do():
    hub = FakeHub(["lane-a", "lane-b"],
                  existing={"lane-b": held_by_scanner()})
    rep = run(FakeConsole([{"lane": "lane-a"}]), hub, dry_run=True)
    assert rep.held == ["lane-a"] and rep.released == ["lane-b"]
    assert hub.writes("lane-a") == [] and hub.writes("lane-b") == ["hold"]


def test_a_quiet_pass_and_an_UNASKED_pass_do_not_read_the_same():
    """A caller that cannot tell them apart reads a broken console as a
    quiet fleet."""
    quiet = run(FakeConsole([]), FakeHub([]))
    blind = run(FakeConsole(boom=True), FakeHub([]))
    assert quiet.asked is True and blind.asked is False
    assert quiet.line() != blind.line()
    assert "no pass" in blind.line()


@pytest.mark.parametrize("cls,attr", [(ConsoleAPI, "candidates"),
                                      (HubHolds, "hold")])
def test_the_real_clients_accept_an_injected_client(cls, attr):
    """No socket in the test path — same shape as edge.HubAPI, for the same
    reason: the loop has to be testable against the real API in-process."""
    obj = cls(client=object())
    assert hasattr(obj, attr)


def test_the_verb_is_reachable_from_the_console_script():
    """🔴 `server._CLI_SUBCOMMANDS` is a SECOND registry, checked before
    argparse ever runs. A verb with a parser and a dispatch line and no
    entry there is read as a server flag and refused — it ships INERT with
    every other surface reporting success."""
    from mcp_hub import cli
    from mcp_hub.server import _CLI_SUBCOMMANDS

    assert "hibernate" in _CLI_SUBCOMMANDS
    names = set(cli.build_parser()._subparsers._group_actions[0].choices)
    assert "hibernate" in names


# ---------------------------------------------------------------------------
# The wire, not the rule.
#
# ⛔ Every test above swaps `HubHolds` for `FakeHub`, which is right for the
# RULE and blind to the DOOR — and the door is where this shipped broken on
# 2026-09-04: the class sent `x-mcp-hub-operator-token`, a header
# `api_v1.auth()` does not read, so every call was 401 whatever the token.
# The check that "confirmed" it was a 401 from a deliberately fake token,
# which is the same answer a bad scheme gives. An instrument that cannot
# distinguish the failure cannot report it.
# ---------------------------------------------------------------------------

class RecordingClient:
    """The httpx surface `HubHolds` actually uses, remembering headers."""

    class _R:
        status_code = 200

        def __init__(self, payload):
            self._p = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._p

    def __init__(self, payload=None):
        self.payload = payload or {"seats": [{"identity": "a"}], "actions": []}
        self.calls = []

    def get(self, url, headers=None, **kw):
        self.calls.append(("GET", url, dict(headers or {}), None))
        return self._R(self.payload)

    def post(self, url, headers=None, json=None, **kw):
        self.calls.append(("POST", url, dict(headers or {}), json))
        return self._R({})


def _headers(client):
    return [c[2] for c in client.calls]


def test_the_scheme_is_bearer_because_that_is_what_auth_READS():
    c = RecordingClient()
    hub = HubHolds(token="sekrit", client=c)
    hub.seats()
    hub.hold("a", until=NOW + 60, reason="r", release_condition="c")
    hub.release("a")
    assert _headers(c), "no call was made, so nothing was pinned"
    for h in _headers(c):
        assert h.get("Authorization") == "Bearer sekrit"


def test_the_header_that_never_worked_is_not_sent():
    # Named explicitly: `api_v1.auth()` reads ONLY the bearer header, so this
    # one is not merely redundant — sending it alone is a silent 401.
    c = RecordingClient()
    hub = HubHolds(token="sekrit", client=c)
    hub.seats()
    hub.release("a")
    for h in _headers(c):
        assert "x-mcp-hub-operator-token" not in {k.lower() for k in h}


def test_every_write_carries_auth_not_just_the_first():
    c = RecordingClient()
    hub = HubHolds(token="t", client=c)
    for seat in ("a", "b", "c"):
        hub.hold(seat, until=NOW + 60, reason="r", release_condition="c")
    assert len([h for h in _headers(c) if h.get("Authorization") == "Bearer t"]) == 3
