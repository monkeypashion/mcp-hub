"""Seat control + live view, phase 1 — console cards #144 (design) / #152 (build).

The design is docs/seat-control-plane.md and these tests are its gate,
written before the implementation. Phase 1 is exactly three verbs: **watch,
prompt, interrupt** — "see it stuck → nudge it → see the result", the
smallest honest loop.

The one idea, borrowed from placements: **the console records INTENT; the
machine that owns the seat carries it out and reports what it OBSERVED.** So
every test here speaks only through the HTTP API, and the assertions are
about records and authority, never about tmux.

Four hazards the design named up front, each with a test below:

  H1 STALE INTENT — an interrupt written during a stall must not land
     minutes later in the middle of healthy work. Intent EXPIRES (120s).
  H2 BUTTON-MASHING — one pending action per seat, upsert not queue, or a
     frustrated operator sends five interrupts and the seat gets all five.
  H3 A CONSOLE THAT HOLDS THE FLEET — the console may only ASK. Each
     machine's own edge acts, authenticated by its own machine token, and a
     machine must not be able to realize another machine's seat.
  H4 AN OPEN VERB SET — no arbitrary keystroke pass-through. Unknown verbs
     are refused, and phase-2 verbs are refused by NAME so the refusal
     reads as "not yet" rather than "never".
"""

from __future__ import annotations

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from mcp_hub.server import create_server

OPERATOR_TOKEN = "test-operator-token"
H = {"Authorization": f"Bearer {OPERATOR_TOKEN}"}


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MCP_HUB_API_TOKEN", OPERATOR_TOKEN)
    server = create_server(db_path=tmp_path / "hub.db")
    with TestClient(server.streamable_http_app()) as c:
        yield c


def _machine(client, name="box-1") -> dict:
    r = client.post(
        "/api/v1/machines",
        json={"name": name, "os": "linux", "capabilities": {"docker": True}},
        headers=H,
    )
    assert r.status_code == 201, r.text
    return r.json()


def _mh(machine: dict) -> dict:
    """Headers for a MACHINE token — the authority the edge actually holds."""
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


def _advance(monkeypatch, seconds: float) -> None:
    """Move the API's own clock forward.

    Both time-bounded guards here (action TTL, watch window) are derived
    from stored timestamps rather than stored as countdowns, which is what
    makes them testable this way — and is the same property that stops a
    pending action outliving its TTL by simply being missed.
    """
    import time as _time

    from mcp_hub import api_v1

    base = _time.time()
    monkeypatch.setattr(api_v1, "_now", lambda: base + seconds)


def _act(client, seat="seat-a", kind="interrupt", **args):
    return client.post(
        f"/api/v1/seats/{seat}/actions",
        json={"kind": kind, "args": args}, headers=H,
    )


# ---------------------------------------------------------------------------
# The loop itself
# ---------------------------------------------------------------------------


class TestTheLoop:
    def test_interrupt_is_recorded_as_pending_intent(self, client):
        _machine(client)
        _seat(client)
        r = _act(client, kind="interrupt")
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["kind"] == "interrupt"
        assert body["status"] == "pending", (
            "a written action must start PENDING — writing it schedules "
            "nothing, exactly like a placement"
        )
        assert body["seat"] == "seat-a"

    def test_prompt_carries_its_text(self, client):
        _machine(client)
        _seat(client)
        r = _act(client, kind="prompt", text="what are you waiting on?")
        assert r.status_code == 201, r.text
        assert r.json()["args"]["text"] == "what are you waiting on?"

    def test_a_prompt_with_no_text_is_refused(self, client):
        """A prompt that types nothing is a no-op wearing the word prompt."""
        _machine(client)
        _seat(client)
        r = _act(client, kind="prompt")
        assert r.status_code == 400, r.text

    def test_the_owning_machine_sees_its_pending_action(self, client):
        m = _machine(client)
        _seat(client)
        _act(client, kind="interrupt")
        r = client.get("/api/v1/seats/seat-a/actions", headers=_mh(m))
        assert r.status_code == 200, r.text
        pending = [a for a in r.json()["actions"] if a["status"] == "pending"]
        assert len(pending) == 1

    def test_edge_reports_what_it_OBSERVED_including_the_pane(self, client):
        """The whole point of the record: not 'we sent Escape' but 'here is
        what the seat looked like afterwards'."""
        m = _machine(client)
        _seat(client)
        aid = _act(client, kind="interrupt").json()["id"]

        r = client.patch(
            f"/api/v1/seats/seat-a/actions/{aid}",
            json={"status": "done", "observed": {"sent": "Escape"},
                  "pane_after": "esc to interrupt\n> "},
            headers=_mh(m),
        )
        assert r.status_code == 200, r.text

        got = client.get("/api/v1/seats/seat-a/actions", headers=H).json()
        done = [a for a in got["actions"] if a["id"] == aid][0]
        assert done["status"] == "done"
        assert done["pane_after"].startswith("esc to interrupt")
        assert done["observed"] == {"sent": "Escape"}

    def test_edge_can_report_a_refusal_and_it_is_not_an_error(self, client):
        """A fail-closed edge must be able to say 'I would not do that' and
        have it recorded as an outcome, not swallowed as a failure."""
        m = _machine(client)
        _seat(client)
        aid = _act(client, kind="interrupt").json()["id"]
        r = client.patch(
            f"/api/v1/seats/seat-a/actions/{aid}",
            json={"status": "refused", "observed": {"why": "no tmux session"}},
            headers=_mh(m),
        )
        assert r.status_code == 200, r.text
        got = client.get("/api/v1/seats/seat-a/actions", headers=H).json()
        assert got["actions"][0]["status"] == "refused"


# ---------------------------------------------------------------------------
# H4 — the verb set is CLOSED
# ---------------------------------------------------------------------------


class TestClosedVerbSet:
    def test_an_unknown_verb_is_refused(self, client):
        _machine(client)
        _seat(client)
        r = _act(client, kind="exec", text="rm -rf /")
        assert r.status_code == 400, r.text

    @pytest.mark.parametrize("verb", ["answer", "restart"])
    def test_phase_two_verbs_are_refused_BY_NAME(self, client, verb):
        """Refused, but recognisably 'not yet' rather than 'never' — a
        refusal whose justification names one mechanism has to say which,
        or the next reader concludes the whole category is forbidden."""
        _machine(client)
        _seat(client)
        r = _act(client, kind=verb, answer="yes")
        assert r.status_code == 400, r.text
        assert verb in r.text.lower()
        # "phase 2", not merely "phase": the message also lists the phase-1
        # verbs, so a looser needle passes on that incidental occurrence even
        # when the not-yet claim has been deleted (mutation-proven).
        assert "phase 2" in r.text.lower(), (
            f"the {verb} refusal must say it is a PHASING limit, not a "
            f"permanent one: {r.text!r}"
        )

    def test_no_agent_writable_surface(self, client):
        """Seats must not be able to drive each other. Operator token only,
        same stance as feature-set registration."""
        m = _machine(client)
        _seat(client)
        r = client.post(
            "/api/v1/seats/seat-a/actions",
            json={"kind": "interrupt", "args": {}}, headers=_mh(m),
        )
        assert r.status_code == 403, (
            "a MACHINE token wrote an action — the console asks and the edge "
            f"acts, never the reverse: {r.text!r}"
        )


# ---------------------------------------------------------------------------
# H3 — authority is per-machine
# ---------------------------------------------------------------------------


class TestAuthority:
    def test_another_machine_cannot_realize_this_seats_action(self, client):
        m1 = _machine(client, "box-1")
        m2 = _machine(client, "box-2")
        _seat(client, machine="box-1")
        aid = _act(client, kind="interrupt").json()["id"]

        r = client.patch(
            f"/api/v1/seats/seat-a/actions/{aid}",
            json={"status": "done"}, headers=_mh(m2),
        )
        assert r.status_code == 403, (
            "box-2 reported an outcome for a seat placed on box-1 — one "
            f"compromised machine would then drive the fleet: {r.text!r}"
        )
        assert m1  # box-1 exists; the refusal is about ownership, not absence

    def test_another_machine_cannot_LIST_this_seats_actions(self, client):
        _machine(client, "box-1")
        m2 = _machine(client, "box-2")
        _seat(client, machine="box-1")
        _act(client, kind="prompt", text="hi")
        r = client.get("/api/v1/seats/seat-a/actions", headers=_mh(m2))
        assert r.status_code == 403, r.text


# ---------------------------------------------------------------------------
# H1 — intent EXPIRES
# ---------------------------------------------------------------------------


class TestStaleIntent:
    def test_a_stale_pending_action_expires_and_is_not_offered(self, client, monkeypatch):
        """An interrupt written during a stall must never land minutes later
        in the middle of healthy work."""
        m = _machine(client)
        _seat(client)
        aid = _act(client, kind="interrupt").json()["id"]

        # Advance the API's own clock rather than sleeping, and rather than
        # adding a test-only endpoint — a hidden route on a production API
        # is a surface nobody audits.
        _advance(monkeypatch, 10_000)

        r = client.get("/api/v1/seats/seat-a/actions", headers=_mh(m))
        assert r.status_code == 200, r.text
        pending = [a for a in r.json()["actions"] if a["status"] == "pending"]
        assert not pending, (
            "a stale interrupt was still offered to the edge — this is the "
            "hazard the TTL exists to refuse"
        )
        expired = [a for a in r.json()["actions"] if a["id"] == aid]
        assert expired and expired[0]["status"] == "expired", (
            "the stale action must be recorded EXPIRED, not deleted — a "
            "vanished intent is indistinguishable from one never written"
        )

    def test_expiry_does_not_touch_a_fresh_action(self, client):
        m = _machine(client)
        _seat(client)
        _act(client, kind="interrupt")
        r = client.get("/api/v1/seats/seat-a/actions", headers=_mh(m))
        assert [a for a in r.json()["actions"] if a["status"] == "pending"], (
            "the expiry sweep ate a fresh action — a guard that fires on "
            "everything is the same as no guard"
        )


# ---------------------------------------------------------------------------
# H2 — one pending action per seat
# ---------------------------------------------------------------------------


class TestButtonMashing:
    def test_five_interrupts_leave_one_pending(self, client):
        m = _machine(client)
        _seat(client)
        for _ in range(5):
            assert _act(client, kind="interrupt").status_code == 201
        r = client.get("/api/v1/seats/seat-a/actions", headers=_mh(m))
        pending = [a for a in r.json()["actions"] if a["status"] == "pending"]
        assert len(pending) == 1, (
            f"mashing queued {len(pending)} interrupts; the seat would "
            "receive every one of them"
        )

    def test_a_new_intent_REPLACES_the_pending_one(self, client):
        """Upsert semantics: the latest ask is the ask. A prompt written
        after an interrupt must not be silently dropped as a duplicate."""
        m = _machine(client)
        _seat(client)
        _act(client, kind="interrupt")
        _act(client, kind="prompt", text="status?")
        r = client.get("/api/v1/seats/seat-a/actions", headers=_mh(m))
        pending = [a for a in r.json()["actions"] if a["status"] == "pending"]
        assert len(pending) == 1
        assert pending[0]["kind"] == "prompt", (
            "the newer intent lost to the older one — restating must move "
            "the ask forward, not pin it"
        )

    def test_pending_on_one_seat_does_not_block_another(self, client):
        m = _machine(client)
        _seat(client, "seat-a")
        _seat(client, "seat-b")
        _act(client, seat="seat-a", kind="interrupt")
        _act(client, seat="seat-b", kind="interrupt")
        r = client.get("/api/v1/seats/seat-b/actions", headers=_mh(m))
        assert [a for a in r.json()["actions"] if a["status"] == "pending"]


# ---------------------------------------------------------------------------
# The watch leg — view ON DEMAND
# ---------------------------------------------------------------------------


class TestWatchLeg:
    def test_no_viewer_declared_means_the_edge_is_told_not_to_stream(self, client):
        """A fleet permanently streaming every pane is cost and exposure
        with no reader."""
        m = _machine(client)
        _seat(client)
        r = client.get("/api/v1/seats/seat-a/watch", headers=_mh(m))
        assert r.status_code == 200, r.text
        assert r.json()["watching"] is False

    def test_declaring_a_viewer_turns_streaming_on(self, client):
        m = _machine(client)
        _seat(client)
        assert client.post(
            "/api/v1/seats/seat-a/watch", json={}, headers=H
        ).status_code == 200
        r = client.get("/api/v1/seats/seat-a/watch", headers=_mh(m))
        assert r.json()["watching"] is True

    def test_a_viewer_declaration_lapses(self, client, monkeypatch):
        """Same 180s open-now window as the workspace column: a console tab
        left open in a closed laptop must not stream forever."""
        m = _machine(client)
        _seat(client)
        client.post("/api/v1/seats/seat-a/watch", json={}, headers=H)
        _advance(monkeypatch, 10_000)
        r = client.get("/api/v1/seats/seat-a/watch", headers=_mh(m))
        assert r.json()["watching"] is False, (
            "a lapsed viewer kept the stream alive — the window is what "
            "makes view-on-demand bounded"
        )

    def test_pane_captures_round_trip_edge_to_console(self, client):
        m = _machine(client)
        _seat(client)
        client.post("/api/v1/seats/seat-a/watch", json={}, headers=H)
        r = client.post(
            "/api/v1/seats/seat-a/view",
            json={"pane": "● Thinking...\n> "}, headers=_mh(m),
        )
        assert r.status_code == 200, r.text
        got = client.get("/api/v1/seats/seat-a/view", headers=H)
        assert got.status_code == 200, got.text
        assert got.json()["pane"] == "● Thinking...\n> "
        assert got.json()["captured_at"], "a capture with no timestamp cannot be aged"

    def test_view_with_no_capture_yet_is_absent_not_empty(self, client):
        """The instrument distinction this codebase keeps relearning: no
        capture is NOT a blank screen."""
        _machine(client)
        _seat(client)
        r = client.get("/api/v1/seats/seat-a/view", headers=H)
        assert r.status_code == 200, r.text
        assert r.json()["pane"] is None, (
            "an unstreamed seat rendered as an empty pane — absence of "
            "measurement must not read as a measurement of emptiness"
        )

    def test_captures_are_ring_buffered_not_a_transcript(self, client):
        """Durable history is the memory volume's job; this is a live view."""
        m = _machine(client)
        _seat(client)
        client.post("/api/v1/seats/seat-a/watch", json={}, headers=H)
        for i in range(40):
            client.post(
                "/api/v1/seats/seat-a/view",
                json={"pane": f"frame {i}"}, headers=_mh(m),
            )
        r = client.get("/api/v1/seats/seat-a/view", headers=H)
        assert r.json()["pane"] == "frame 39", "the latest capture must win"
        assert r.json()["kept"] <= 20, (
            f"{r.json()['kept']} captures retained — this is becoming a "
            "transcript, which the design explicitly refuses"
        )

    def test_the_view_reports_its_OBSERVED_reach_not_an_assumed_one(
        self, client, monkeypatch
    ):
        """A count bound has no duration: its horizon is ring ÷ write rate,
        so it is shortest on a thrashing seat — exactly when someone opens
        it during an incident. The response therefore dates the far end
        instead of letting a reader infer "the last N minutes".

        The clock ticks a full minute per call so the far end is STRICTLY
        older than the newest. A `<=` here passed against an `oldest` wired
        to MAX — the reach rendered as instantaneous and the assertion could
        not tell (mutation-proven)."""
        ticks = iter(range(1_000, 1_000_000, 60))

        from mcp_hub import api_v1

        monkeypatch.setattr(api_v1, "_now", lambda: float(next(ticks)))

        m = _machine(client)
        _seat(client)
        client.post("/api/v1/seats/seat-a/watch", json={}, headers=H)
        for i in range(3):
            client.post("/api/v1/seats/seat-a/view",
                        json={"pane": f"f{i}"}, headers=_mh(m))

        body = client.get("/api/v1/seats/seat-a/view", headers=H).json()
        assert body["oldest_captured_at"] is not None, (
            "the view must date its OLDEST capture — without it the reach "
            "is unknowable and gets quoted as a duration it does not have"
        )
        assert body["oldest_captured_at"] < body["captured_at"], (
            "the far end is not older than the newest — the view is "
            f"reporting no reach at all: {body!r}"
        )
        assert body["ring"] == 20, "the bound itself must be legible to a reader"

    def test_an_empty_view_dates_nothing(self, client):
        _machine(client)
        _seat(client)
        body = client.get("/api/v1/seats/seat-a/view", headers=H).json()
        assert body["oldest_captured_at"] is None, (
            "an unstreamed seat was given a capture window — same error as "
            "rendering its pane as empty rather than absent"
        )

    def test_another_machine_cannot_push_this_seats_pane(self, client):
        _machine(client, "box-1")
        m2 = _machine(client, "box-2")
        _seat(client, machine="box-1")
        r = client.post(
            "/api/v1/seats/seat-a/view",
            json={"pane": "forged"}, headers=_mh(m2),
        )
        assert r.status_code == 403, r.text


# ---------------------------------------------------------------------------
# Unknown seats, and the API-disabled control
# ---------------------------------------------------------------------------


class TestEdges:
    def test_action_on_an_unknown_seat_is_404_not_a_silent_row(self, client):
        _machine(client)
        r = _act(client, seat="no-such-seat", kind="interrupt")
        assert r.status_code == 404, r.text

    def test_everything_needs_auth(self, client):
        _machine(client)
        _seat(client)
        for method, path in [
            ("post", "/api/v1/seats/seat-a/actions"),
            ("get", "/api/v1/seats/seat-a/actions"),
            ("post", "/api/v1/seats/seat-a/watch"),
            ("get", "/api/v1/seats/seat-a/watch"),
            ("post", "/api/v1/seats/seat-a/view"),
            ("get", "/api/v1/seats/seat-a/view"),
        ]:
            kw = {"json": {"kind": "interrupt"}} if method == "post" else {}
            r = getattr(client, method)(path, **kw)
            assert r.status_code == 401, f"{method} {path} answered unauthenticated"


# ---------------------------------------------------------------------------
# GET /api/v1/machines/{name}/seats — the lane leg's discovery door.
# /seats is operator-only and a lane seat has no placement to carry its spec,
# so the edge needs a machine-scoped list or console actions into lanes are
# stored and never realized (2026-08-28: "stop everyone" interrupted 0 of 7).
# ---------------------------------------------------------------------------


class TestMachineSeats:
    def test_a_machine_lists_its_own_seats_with_spec(self, client):
        m = _machine(client)
        client.post(
            "/api/v1/seats",
            json={"identity": "vps-lane", "machine": "box-1",
                  "folder": "/home/x/vps", "repo": "acme/vps",
                  "spec": {"substrate": "lane"}},
            headers=H,
        )
        r = client.get("/api/v1/machines/box-1/seats", headers=_mh(m))
        assert r.status_code == 200
        seats = r.json()["seats"]
        assert [s["identity"] for s in seats] == ["vps-lane"]
        assert seats[0]["spec"]["substrate"] == "lane"

    def test_a_machine_token_cannot_list_another_machines_seats(self, client):
        m1 = _machine(client, "box-1")
        _machine(client, "box-2")
        _seat(client, "seat-b2", machine="box-2")
        r = client.get("/api/v1/machines/box-2/seats", headers=_mh(m1))
        assert r.status_code == 403

    def test_operator_token_may_list_any_machine(self, client):
        _machine(client)
        _seat(client)
        r = client.get("/api/v1/machines/box-1/seats", headers=H)
        assert r.status_code == 200
        assert [s["identity"] for s in r.json()["seats"]] == ["seat-a"]

    def test_no_token_is_refused(self, client):
        _machine(client)
        r = client.get("/api/v1/machines/box-1/seats")
        assert r.status_code in (401, 403)

    def test_archived_and_other_machine_seats_are_absent(self, client):
        m = _machine(client)
        _machine(client, "box-2")
        _seat(client, "mine", machine="box-1")
        _seat(client, "theirs", machine="box-2")
        _seat(client, "gone", machine="box-1")
        r = client.delete("/api/v1/seats/gone", headers=H)
        assert r.status_code in (200, 204), r.text
        r = client.get("/api/v1/machines/box-1/seats", headers=_mh(m))
        assert [s["identity"] for s in r.json()["seats"]] == ["mine"]
