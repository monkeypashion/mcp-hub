"""bar 59 — a hold carries a KIND and an OWNER, across all three layers.

The #318 hold is one mechanism serving two purposes that pull in opposite
directions:

  BRAKE        a lane is burning its share right now. It must stop, and the
               ten-minute ceiling exists because waiting on it forever is
               the thing a brake cannot afford.
  HIBERNATION  a lane has nothing open. It is parked to stop it costing
               anything. Hard-stopping it destroys an in-flight turn to
               reclaim a share nobody is spending.

Before this, the lane could not tell them apart and `hard_stop_due` applied
the brake's ceiling to both. So `kind` has to survive every hop —
hub -> edge -> mirror -> lane — and each hop is a test below.

The direction of every failure is the point, and it is the SAME direction
throughout: **NO KIND = NOT EXEMPT**. A hold written by an older edge, a
mirror this build has not seen, an unrecognised kind — all keep the old
hard-stoppable behaviour. The exemption is a positive mark that something
has to LOSE, exactly like the sender grades. A default of "hibernation"
anywhere would make a format change silently un-stoppable, and nothing
would report it.

`owner` is the second half: a hibernation scanner that could lift a brake
would hand a lane its share back in the middle of the window the brake was
protecting, and neither mechanism would notice.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from mcp_hub import edge, hold
from mcp_hub.server import create_server

OPERATOR_TOKEN = "test-operator-token"
H = {"Authorization": f"Bearer {OPERATOR_TOKEN}"}
# Layer 2 and 3 call `edge`/`hold` directly with an explicit `now`, so a
# fixed far-future stamp is fine there. Layer 1 goes through the HTTP
# surface, where the PRE-EXISTING 12h cap applies — so those use `_until()`.
# (The first draft used the fixed stamp everywhere and was refused with
# "`until` is 614293.7h out": bar 14/42's cap doing exactly its job.)
FUTURE = 4_000_000_000.0


def _until(hours: float = 1.0) -> float:
    return time.time() + hours * 3600.0


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MCP_HUB_API_TOKEN", OPERATOR_TOKEN)
    monkeypatch.delenv("MCP_HUB_HIBERNATION_EXEMPT", raising=False)
    server = create_server(db_path=tmp_path / "hub.db")
    with TestClient(server.streamable_http_app()) as c:
        yield c


def _exempt_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                   names: str, db: str = "hub-x.db"):
    """A hub whose exempt list is set BEFORE the app is built.

    The list is resolved at app-construction time on purpose: an exempt list
    re-read per request could change under a hold that was already validated
    against the old one.
    """
    monkeypatch.setenv("MCP_HUB_API_TOKEN", OPERATOR_TOKEN)
    monkeypatch.setenv("MCP_HUB_HIBERNATION_EXEMPT", names)
    server = create_server(db_path=tmp_path / db)
    return TestClient(server.streamable_http_app())


def _machine(client, name="box-1") -> dict:
    r = client.post(
        "/api/v1/machines",
        json={"name": name, "os": "linux", "capabilities": {"docker": True}},
        headers=H,
    )
    assert r.status_code == 201, r.text
    return r.json()


def _mh(machine: dict) -> dict:
    return {"Authorization": f"Bearer {machine['token']}"}


def _seat(client, name="seat-a", machine="box-1") -> dict:
    r = client.post(
        "/api/v1/seats",
        json={"identity": name, "repo": "acme/widget", "machine": machine,
              "folder": "/home/x/widget"},
        headers=H,
    )
    assert r.status_code == 201, r.text
    return r.json()


def _post(client, seat: str, verb: str, args: dict):
    """The action VERB and the hold's own `args.kind` are two different
    fields that happen to share a word. Passed separately here on purpose —
    folding them into one **kwargs is how the first draft of this file
    silently sent `kind="hold"` as the hold kind."""
    return client.post(
        f"/api/v1/seats/{seat}/actions",
        json={"kind": verb, "args": args}, headers=H,
    )


def _act(client, seat="seat-a", kind="hold", **args):
    return _post(client, seat, kind, args)


def _hold(client, seat="seat-a", **extra):
    args = {"until": _until(), "release_condition": "the window resets"}
    args.update(extra)
    return _post(client, seat, "hold", args)


def _carry_out(client, machine, aid, seat="seat-a"):
    """The edge reports it DONE. Only a done action is a live hold — a
    pending one is an ASK, and enforcing an ask would stop a lane nobody
    has confirmed stopping."""
    r = client.patch(
        f"/api/v1/seats/{seat}/actions/{aid}",
        json={"status": "done", "observed": {"sent": "hold"}},
        headers=_mh(machine),
    )
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# Layer 1 — the hub validates the kind
# ---------------------------------------------------------------------------


class TestTheHubValidatesKind:
    def test_the_two_points_already_satisfied_are_not_rebuilt(self, client):
        """`until` and `release_condition` were ALREADY enforced before bar
        59 (bar 14/42). Pinned here so the bar-59 work is not credited with
        them and so a later refactor cannot quietly drop them."""
        _machine(client)
        _seat(client)
        assert _act(client, kind="hold",
                    release_condition="x").status_code == 400
        assert _act(client, kind="hold", until=_until()).status_code == 400

    def test_a_hold_with_no_kind_is_still_accepted(self, client):
        """Backward compatibility is not a courtesy here — it is the
        fail-closed direction. An older writer keeps working AND keeps the
        hard-stoppable behaviour."""
        _machine(client)
        _seat(client)
        r = _hold(client)
        assert r.status_code == 201, r.text
        assert r.json()["args"].get("kind", "") == ""

    def test_an_unknown_kind_is_refused(self, client):
        """Not carried through as an unrecognised string: every reader
        downstream treats a kind it does not know as 'not hibernation', so a
        typo would silently make a parked lane hard-stoppable."""
        _machine(client)
        _seat(client)
        r = _hold(client, kind="hibernate", owner="scanner")
        assert r.status_code == 400, r.text
        assert "hibernate" in r.text

    def test_a_kinded_hold_needs_an_owner(self, client):
        _machine(client)
        _seat(client)
        r = _hold(client, kind="hibernation")
        assert r.status_code == 400, r.text
        assert "owner" in r.text

    def test_a_hibernation_with_kind_and_owner_is_recorded(self, client):
        _machine(client)
        _seat(client)
        r = _hold(client, kind="hibernation", owner="hibernation-scanner")
        assert r.status_code == 201, r.text
        assert r.json()["args"]["kind"] == "hibernation"
        assert r.json()["args"]["owner"] == "hibernation-scanner"

    def test_the_brake_carries_its_own_owner(self, client):
        """The deputy's amendment: the owner rule binds the bar-69 brake
        too. A brake write with no owner is refused like any other kinded
        hold."""
        _machine(client)
        _seat(client)
        assert _hold(client, kind="brake").status_code == 400
        assert _hold(client, kind="brake", owner="brake").status_code == 201


# ---------------------------------------------------------------------------
# Layer 1b — the exempt list, enforced AT THE WRITE
# ---------------------------------------------------------------------------


class TestTheExemptList:
    def test_an_exempt_seat_cannot_be_hibernated(self, tmp_path, monkeypatch):
        with _exempt_client(tmp_path, monkeypatch, "seat-a") as c:
            _machine(c)
            _seat(c)
            r = _hold(c, kind="hibernation", owner="scanner")
            assert r.status_code == 409, r.text
            assert "exempt" in r.text

    def test_an_exempt_seat_can_still_be_braked(self, tmp_path, monkeypatch):
        """The exemption is about having nothing to do, not about being
        stoppable. A lane that may never be PARKED may still be overspending,
        and refusing the brake there would make the exempt list a way to opt
        out of the ceiling."""
        with _exempt_client(tmp_path, monkeypatch, "seat-a",
                            db="hub-b.db") as c:
            _machine(c)
            _seat(c)
            assert _hold(c, kind="brake", owner="brake").status_code == 201

    def test_a_non_exempt_seat_is_unaffected(self, tmp_path, monkeypatch):
        """Contrast, so the refusal above cannot pass for the wrong reason —
        e.g. hibernation being refused outright whenever a list is set."""
        with _exempt_client(tmp_path, monkeypatch, "seat-a",
                            db="hub-c.db") as c:
            _machine(c)
            _seat(c)
            _seat(c, name="seat-b")
            r = _hold(c, seat="seat-b", kind="hibernation", owner="scanner")
            assert r.status_code == 201, r.text

    def test_an_exempt_name_matching_no_seat_refuses_every_hibernation(
            self, tmp_path, monkeypatch):
        """🔴 The fail-closed heart of the list. A typo'd entry protects
        nothing, and nothing reveals that until the lane it was written for
        is parked. So an unresolvable list means we cannot say ANY seat is
        safe to hibernate, and we say so instead of guessing."""
        with _exempt_client(tmp_path, monkeypatch, "seat-a,seat-tpyo",
                            db="hub-d.db") as c:
            _machine(c)
            _seat(c)
            _seat(c, name="seat-b")
            r = _hold(c, seat="seat-b", kind="hibernation", owner="scanner")
            assert r.status_code == 500, r.text
            assert "seat-tpyo" in r.text, (
                "the refusal must NAME the unresolved entry — an operator "
                "cannot fix a list the error will not identify"
            )

    def test_an_unresolvable_list_does_not_block_the_brake(
            self, tmp_path, monkeypatch):
        """The list is a hibernation control. A broken one must not take the
        brake down with it — that would turn a typo into an unstoppable
        fleet, which is worse than the fault it guards."""
        with _exempt_client(tmp_path, monkeypatch, "seat-tpyo",
                            db="hub-e.db") as c:
            _machine(c)
            _seat(c)
            assert _hold(c, kind="brake", owner="brake").status_code == 201


# ---------------------------------------------------------------------------
# Layer 1c — release is owner-scoped
# ---------------------------------------------------------------------------


class TestReleaseChecksTheOwner:
    def test_a_foreign_owner_cannot_lift_a_hold(self, client):
        m = _machine(client)
        _seat(client)
        aid = _hold(client, kind="brake", owner="brake").json()["id"]
        _carry_out(client, m, aid)
        r = _act(client, kind="release", owner="hibernation-scanner")
        assert r.status_code == 409, r.text
        assert "brake" in r.text

    def test_the_placing_owner_lifts_its_own(self, client):
        m = _machine(client)
        _seat(client)
        aid = _hold(client, kind="brake", owner="brake").json()["id"]
        _carry_out(client, m, aid)
        assert _act(client, kind="release", owner="brake").status_code == 201

    def test_a_release_with_no_owner_is_the_by_hand_override(self, client):
        """The operator is not bound by owner-releases-own. A rule that could
        strand a lane held by a mechanism nobody can run any more would make
        the hold unsafe in exactly the way the expiry exists to prevent."""
        m = _machine(client)
        _seat(client)
        aid = _hold(client, kind="brake", owner="brake").json()["id"]
        _carry_out(client, m, aid)
        assert _act(client, kind="release").status_code == 201

    def test_a_pending_hold_is_not_yet_a_hold(self, client):
        """Only an action the edge carried out counts. A pending hold is an
        ASK, and scoping a release to an ask would let an unrealized write
        block the release of nothing."""
        _machine(client)
        _seat(client)
        _hold(client, kind="brake", owner="brake")  # left pending
        assert _act(client, kind="release", owner="scanner").status_code == 201

    def test_an_expired_hold_scopes_nothing(self, client, monkeypatch):
        """It released itself. Anyone may write a release afterwards — there
        is nothing left to own."""
        from mcp_hub import api_v1
        m = _machine(client)
        _seat(client)
        until = _until()
        aid = _hold(client, until=until, kind="brake",
                    owner="brake").json()["id"]
        _carry_out(client, m, aid)
        monkeypatch.setattr(api_v1, "_now", lambda: until + 1)
        assert _act(client, kind="release", owner="scanner").status_code == 201


# ---------------------------------------------------------------------------
# Layer 2 — the edge carries both fields through the mirror
# ---------------------------------------------------------------------------


class TestTheMirrorCarriesIt:
    def _actions(self, **args):
        base = {"until": FUTURE, "release_condition": "the window resets"}
        base.update(args)
        return [{"id": 1, "kind": "hold", "status": "done",
                 "requested_at": 100.0, "args": base}]

    def test_kind_and_owner_reach_the_mirror(self):
        state = edge._hold_state(
            self._actions(kind="hibernation", owner="scanner"), now=1000.0)
        assert state is not None
        assert state["kind"] == "hibernation"
        assert state["owner"] == "scanner"

    def test_an_unkinded_hold_stays_unkinded(self):
        """Never defaulted to 'brake'. Harmless today and wrong the moment a
        third kind exists — and an invented value is a record that mirrors a
        plausible story instead of observing one."""
        state = edge._hold_state(self._actions(), now=1000.0)
        assert state is not None
        assert state["kind"] == ""
        assert state["owner"] == ""

    def test_the_existing_fields_still_travel(self):
        """Contrast: the new keys must not have displaced the old ones."""
        state = edge._hold_state(
            self._actions(kind="brake", owner="brake", reason="over share"),
            now=1000.0)
        assert state["until"] == FUTURE
        assert state["held_at"] == 100.0
        assert state["reason"] == "over share"
        assert state["release_condition"] == "the window resets"


# ---------------------------------------------------------------------------
# Layer 3 — the lane, where the difference finally lands
# ---------------------------------------------------------------------------


class TestTheLaneHonoursTheKind:
    def _entry(self, **extra):
        e = {"until": FUTURE, "held_at": 100.0,
             "release_condition": "the window resets"}
        e.update(extra)
        return e

    def test_a_hibernation_is_never_hard_stopped(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MCP_HUB_HOLD_BOUNDARY_DIR", str(tmp_path / "b"))
        entry = self._entry(kind="hibernation")
        assert hold.hard_stop_due(entry, "lane-a", now=100.0 + 10_000) is False

    def test_a_brake_IS_hard_stopped(self, tmp_path, monkeypatch):
        """The contrast that stops the test above passing vacuously. Same
        entry, same clock, one word different."""
        monkeypatch.setenv("MCP_HUB_HOLD_BOUNDARY_DIR", str(tmp_path / "b"))
        entry = self._entry(kind="brake")
        assert hold.hard_stop_due(entry, "lane-a", now=100.0 + 10_000) is True

    def test_no_kind_is_hard_stopped_like_before(self, tmp_path, monkeypatch):
        """NO KIND = NOT EXEMPT. An older mirror keeps the old behaviour
        rather than being promoted into the exempt class by silence."""
        monkeypatch.setenv("MCP_HUB_HOLD_BOUNDARY_DIR", str(tmp_path / "b"))
        assert hold.hard_stop_due(self._entry(), "lane-a",
                                  now=100.0 + 10_000) is True

    def test_an_unrecognised_kind_is_hard_stopped(self, tmp_path, monkeypatch):
        """Belt-and-braces with the hub's own refusal: if a value the hub
        would reject ever reaches the lane, the lane treats it as not
        hibernation. Only the exact word earns the exemption."""
        monkeypatch.setenv("MCP_HUB_HOLD_BOUNDARY_DIR", str(tmp_path / "b"))
        assert hold.hard_stop_due(self._entry(kind="Hibernation"), "lane-a",
                                  now=100.0 + 10_000) is True

    def test_a_boundary_still_cancels_the_hard_stop_for_a_brake(
            self, tmp_path, monkeypatch):
        """The pre-existing rule is untouched: once a boundary is stamped
        there is no in-flight turn to lose."""
        monkeypatch.setenv("MCP_HUB_HOLD_BOUNDARY_DIR", str(tmp_path / "b"))
        assert hold.stamp_boundary("lane-a", now=200.0) is True
        assert hold.hard_stop_due(self._entry(kind="brake"), "lane-a",
                                  now=100.0 + 10_000) is False

    def test_the_parked_lane_is_told_it_is_parked(self):
        """A lane handed the BRAKE notice when it was merely parked reads it
        as a reprimand for overspending and goes looking for what it did
        wrong. It did nothing wrong: it ran out of open work."""
        notice = hold.hook_notice("lane-a", self._entry(
            kind="hibernation", release_condition="a bar is assigned"))
        assert "HIBERNATING" in notice
        assert "a bar is assigned" in notice
        assert "no turn of yours is lost" in notice.replace("\n", " ")

    def test_the_braked_lane_still_gets_the_brake_notice(self):
        """Contrast, so the branch above cannot have replaced both."""
        notice = hold.hook_notice("lane-a", self._entry(kind="brake"))
        assert "HELD" in notice
        assert "HIBERNATING" not in notice
