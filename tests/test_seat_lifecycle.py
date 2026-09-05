"""W1.1 — the seat tombstone fix (docs/verification/wave-1.md, A1-A4).

The defect class: `archived` was a one-way flag. Three creation paths tested
identity without an archived filter and lied ("already exists" for a row that
404s on GET); no restore path existed anywhere — dev-vm-1 recovered a
tombstoned seat with a hand UPDATE on prod (2026-08-10); the machine pull
served archived seats' specs forever; archive rang no doorbell.

Design rules under test (FDM's lifecycle lessons, adopted 2026-08-10):
- archive FREEZES (pure existence-axis move) — restore reconstructs nothing;
- restore RE-RUNS create validation (the world may have changed);
- every transition APPENDS to api_seat_events (a bare flag destroys
  when/by-whom/why — and purge must leave a death-fact);
- notification is symmetric (archive AND restore ring the doorbell);
- purge is refused while ANY placement row references the seat — counted
  raw, never via active_placements, which excludes desired='reclaimed'.
"""

import argparse
import json
import sqlite3
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from mcp_hub import api_v1
from mcp_hub.cli import seats_command
from mcp_hub.server import create_server

OP = {"Authorization": "Bearer op-token"}


@pytest.fixture()
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MCP_HUB_API_TOKEN", "op-token")
    db_path = tmp_path / "hub.db"
    server = create_server(db_path=db_path)
    with TestClient(server.streamable_http_app()) as c:
        yield c, db_path


def _machine(c, name="box-1") -> str:
    r = c.post(
        "/api/v1/machines",
        json={"name": name, "os": "linux", "capabilities": {}},
        headers=OP,
    )
    assert r.status_code in (200, 201), r.text
    return r.json()["token"]


def _seat(c, identity="w1seat", machine="box-1", **spec) -> None:
    r = c.post(
        "/api/v1/seats",
        json={
            "identity": identity, "machine": machine,
            "folder": "/srv/x", "repo": "acme/x", "spec": spec,
        },
        headers=OP,
    )
    assert r.status_code == 201, r.text


def _archive(c, identity="w1seat") -> None:
    r = c.delete(f"/api/v1/seats/{identity}", headers=OP)
    assert r.status_code == 200, r.text


def _events(db_path: Path, identity: str) -> list[tuple[str, str]]:
    con = sqlite3.connect(db_path)
    try:
        return con.execute(
            "SELECT event, reason FROM api_seat_events"
            " WHERE identity = ? ORDER BY ts",
            (identity,),
        ).fetchall()
    finally:
        con.close()


def _actors(db_path: Path, identity: str) -> list[tuple[str, str]]:
    con = sqlite3.connect(db_path)
    try:
        return con.execute(
            "SELECT event, actor FROM api_seat_events"
            " WHERE identity = ? ORDER BY ts",
            (identity,),
        ).fetchall()
    finally:
        con.close()


# ---------------------------------------------------------------------------
# A1 — honest 409s at every creation path
# ---------------------------------------------------------------------------


class TestHonest409s:
    """Mutation: revert any creation-path check to the unfiltered
    `SELECT 1 ... WHERE identity = ?` + old message → that path's test fails
    on the 'archived'/'restore' assertions."""

    def test_positive_control_live_collision_still_409s(self, rig):
        """The harness can see the ORIGINAL refusal — without this, the
        archived-row assertions below prove nothing about the change."""
        c, _ = rig
        _machine(c)
        _seat(c)
        r = c.post(
            "/api/v1/seats",
            json={"identity": "w1seat", "machine": "box-1",
                  "folder": "/srv/x", "repo": "acme/x"},
            headers=OP,
        )
        assert r.status_code == 409
        assert "already exists" in r.json()["detail"]

    def test_seats_add_names_the_archived_row_and_the_exits(self, rig):
        c, _ = rig
        _machine(c)
        _seat(c)
        _archive(c)
        r = c.post(
            "/api/v1/seats",
            json={"identity": "w1seat", "machine": "box-1",
                  "folder": "/srv/x", "repo": "acme/x"},
            headers=OP,
        )
        assert r.status_code == 409
        err = r.json()["detail"]
        # Pre-fix this said "already exists" — a lie for a row that 404s on
        # GET. The refusal must say ARCHIVED and name both exits.
        assert "archived" in err
        assert "restore" in err
        assert "purge" in err

    def test_clone_target_names_the_archived_row(self, rig):
        c, _ = rig
        _machine(c)
        _seat(c, identity="src")
        _seat(c, identity="src-takeb")
        _archive(c, "src-takeb")
        r = c.post(
            "/api/v1/seats/src/clone",
            json={"suffix": "takeb"},
            headers=OP,
        )
        assert r.status_code == 409
        err = r.json()["detail"]
        assert "archived" in err and "restore" in err

    def test_capsule_place_as_names_the_archived_row(self, rig):
        c, _ = rig
        _machine(c)
        _seat(c, identity="cap1")
        # A squad + capsule around the seat, then tombstone the --as target.
        assert c.post("/api/v1/squads", json={"name": "sq"},
                      headers=OP).status_code in (200, 201)
        assert c.put("/api/v1/squads/sq/members/cap1", json={},
                     headers=OP).status_code in (200, 201)
        cid = c.post("/api/v1/capsules",
                     json={"squad": "sq"}, headers=OP).json()["id"]
        _seat(c, identity="cap1-tb")
        _archive(c, "cap1-tb")
        r = c.post(
            f"/api/v1/capsules/{cid}/place",
            json={"machine": "box-1", "as": "tb"},
            headers=OP,
        )
        assert r.status_code == 409
        err = r.json()["detail"]
        assert "archived" in err and "restore" in err


# ---------------------------------------------------------------------------
# A2 — restore round-trips, with honest refusals and re-validation
# ---------------------------------------------------------------------------


class TestRestore:
    """Mutation: delete the restore route → every test here fails (pre-fix
    state, where the only restore was a hand UPDATE on prod)."""

    def test_round_trip_identical(self, rig):
        c, _ = rig
        _machine(c)
        _seat(c, image="img:1", memory_volume="vol-1")
        before = c.get("/api/v1/seats/w1seat", headers=OP).json()
        _archive(c)
        assert c.get("/api/v1/seats/w1seat", headers=OP).status_code == 404
        r = c.post("/api/v1/seats/w1seat/restore", headers=OP)
        assert r.status_code == 200, r.text
        after = c.get("/api/v1/seats/w1seat", headers=OP).json()
        # FREEZE, don't unwind: archive touched one axis, so restore had
        # nothing to reconstruct — the seat comes back byte-identical.
        for key in ("identity", "machine", "folder", "repo", "spec", "class"):
            assert after.get(key) == before.get(key), key
        # ...and it is placeable again.
        r = c.post(
            "/api/v1/placements",
            json={"seat": "w1seat", "machine": "box-1",
                  "substrate": "worktree"},
            headers=OP,
        )
        assert r.status_code in (200, 201), r.text

    def test_restoring_a_live_seat_is_a_distinct_refusal(self, rig):
        c, _ = rig
        _machine(c)
        _seat(c)
        r = c.post("/api/v1/seats/w1seat/restore", headers=OP)
        assert r.status_code == 409
        assert "not archived" in r.json()["detail"]

    def test_restoring_nothing_is_404(self, rig):
        c, _ = rig
        _machine(c)
        r = c.post("/api/v1/seats/ghost/restore", headers=OP)
        assert r.status_code == 404

    def test_restore_revalidates_the_changed_world(self, rig):
        """FDM's sharpest trap: the invariant that held at archive time may
        not hold now. Here the seat's MACHINE was retired during the archived
        interval — restore must refuse, not resurrect a seat pointing at a
        machine that is gone.

        Mutation: make restore skip validation ("it existed before") → this
        fails."""
        c, _ = rig
        _machine(c)
        _seat(c)
        _archive(c)
        assert c.delete("/api/v1/machines/box-1",
                        headers=OP).status_code == 200
        r = c.post("/api/v1/seats/w1seat/restore", headers=OP)
        assert r.status_code == 409
        assert "machine" in r.json()["detail"]


# ---------------------------------------------------------------------------
# A3 — the machine pull stops serving archived specs (harvest exception kept)
# ---------------------------------------------------------------------------


class TestMachinePull:
    def test_archived_seat_spec_absent_from_pull(self, rig):
        """The archived+running state is API-unreachable (archive refuses
        while active placements exist), so it is constructed by direct DB
        write — exactly how the six orphaned prod rows came to exist
        (legacy/hand-edits).

        Mutation: revert :604 to the unfiltered seat lookup → fails."""
        c, db_path = rig
        token = _machine(c)
        _seat(c, image="img:1")
        r = c.post(
            "/api/v1/placements",
            json={"seat": "w1seat", "machine": "box-1", "substrate": "docker"},
            headers=OP,
        )
        assert r.status_code in (200, 201)
        con = sqlite3.connect(db_path)
        con.execute("UPDATE api_seats SET archived = 1 WHERE identity = 'w1seat'")
        con.commit()
        con.close()
        pulled = c.get(
            "/api/v1/machines/box-1/placements",
            headers={"Authorization": f"Bearer {token}"},
        ).json()["placements"]
        assert len(pulled) == 1
        assert "seat_spec" not in pulled[0]

    def test_reclaimed_placement_still_carries_the_spec(self, rig):
        """The exception is for HARVEST, not destroy: docker harvest reads
        spec.memory_volume; withholding the spec turns harvest into a
        clean-looking skip = silent memory loss. The reachable path: reclaim
        requested → seat archived mid-reclaim (allowed — active_placements
        excludes reclaimed rows).

        Mutation: drop the desired='reclaimed' exception → fails."""
        c, _ = rig
        token = _machine(c)
        _seat(c, image="img:1", memory_volume="vol-1")
        pid = c.post(
            "/api/v1/placements",
            json={"seat": "w1seat", "machine": "box-1", "substrate": "docker"},
            headers=OP,
        ).json()["id"]
        assert c.delete(f"/api/v1/placements/{pid}",
                        headers=OP).status_code == 202
        _archive(c)  # legal mid-reclaim
        pulled = c.get(
            "/api/v1/machines/box-1/placements",
            headers={"Authorization": f"Bearer {token}"},
        ).json()["placements"]
        assert len(pulled) == 1
        assert pulled[0]["desired"] == "reclaimed"
        spec = pulled[0].get("seat_spec", {}).get("spec", {})
        assert json.loads(spec)["memory_volume"] == "vol-1" if isinstance(
            spec, str) else spec.get("memory_volume") == "vol-1"


# ---------------------------------------------------------------------------
# A4 — doorbells + purge + the event trail
# ---------------------------------------------------------------------------


class TestDoorbellsAndPurge:
    @pytest.fixture()
    def rings(self, monkeypatch):
        calls: list[tuple[str, str]] = []
        real = api_v1.notify_machine

        def recorder(machine, reason="placement"):
            calls.append((machine, reason))
            return real(machine, reason)

        monkeypatch.setattr(api_v1, "notify_machine", recorder)
        return calls

    def test_archive_and_restore_both_ring_the_doorbell(self, rig, rings):
        """Symmetric notification (FDM): an asymmetric doorbell is how
        observers drift from the record.

        Mutation: remove either notify_machine call → the matching half
        fails."""
        c, _ = rig
        _machine(c)
        _seat(c)
        _archive(c)
        assert ("box-1", "seat-archived") in rings
        c.post("/api/v1/seats/w1seat/restore", headers=OP)
        assert ("box-1", "seat-restored") in rings

    def test_purge_refused_while_any_placement_row_references(self, rig):
        """Counted RAW — active_placements excludes desired='reclaimed', and
        a purge gated on it would delete a seat whose reclaimed placement row
        still references it.

        Mutation: swap the raw count for active_placements → fails (the
        placement here is reclaimed)."""
        c, _ = rig
        _machine(c)
        _seat(c)
        pid = c.post(
            "/api/v1/placements",
            json={"seat": "w1seat", "machine": "box-1",
                  "substrate": "worktree"},
            headers=OP,
        ).json()["id"]
        assert c.delete(f"/api/v1/placements/{pid}",
                        headers=OP).status_code == 202
        _archive(c)
        r = c.delete("/api/v1/seats/w1seat?purge=true", headers=OP)
        assert r.status_code == 409
        assert "placement" in r.json()["detail"]
        # Drop the placement row (the operator act) — purge now proceeds.
        assert c.delete(f"/api/v1/placements/{pid}?purge=true",
                        headers=OP).status_code == 200
        r = c.delete("/api/v1/seats/w1seat?purge=true", headers=OP)
        assert r.status_code == 200
        assert c.get("/api/v1/seats/w1seat", headers=OP).status_code == 404

    def test_every_transition_appends_and_purge_leaves_a_death_fact(
        self, rig
    ):
        """FDM's hardest-won lesson: a bare flag destroys when/by-whom/why,
        and a purge that leaves nothing loses 'did this ever exist' — the
        question people ask during the incident, when the row is gone.

        Mutation: drop the event INSERT from any transition → that event is
        missing here."""
        c, db_path = rig
        _machine(c)
        _seat(c)
        _archive(c)
        c.post("/api/v1/seats/w1seat/restore", headers=OP)
        _archive(c)
        c.delete("/api/v1/seats/w1seat?purge=true", headers=OP)
        events = [e for e, _ in _events(db_path, "w1seat")]
        assert events == ["archived", "restored", "archived", "purged"]
        # The death-fact SURVIVES the row it describes.
        con = sqlite3.connect(db_path)
        assert not con.execute(
            "SELECT 1 FROM api_seats WHERE identity = 'w1seat'"
        ).fetchone()
        con.close()


# ---------------------------------------------------------------------------
# CLI — the verbs that make the routes reachable by an operator
# ---------------------------------------------------------------------------


class _FakeApi:
    def __init__(self):
        self.calls: list[tuple] = []

    def restore_seat(self, identity):
        self.calls.append(("restore", identity))
        return {"identity": identity}

    def delete_seat(self, identity, purge=False):
        self.calls.append(("delete", identity, purge))
        return {"identity": identity}


def _args(**kw) -> argparse.Namespace:
    base = dict(action="rm", identity="s1", machine="box", hub_url="",
                json=False, dry_run=False, purge=False, yes=False)
    base.update(kw)
    return argparse.Namespace(**base)


class TestSeatsCli:
    def test_restore_calls_the_route(self, capsys):
        api = _FakeApi()
        assert seats_command(_args(action="restore"), api=api) == 0
        assert api.calls == [("restore", "s1")]
        assert "restored" in capsys.readouterr().out

    def test_purge_without_yes_refuses_and_calls_nothing(self, capsys):
        """Nothing dies unnamed — the destructive verb states what it will
        destroy and waits.

        Mutation: drop the `--yes` gate → this fails (a call is recorded)."""
        api = _FakeApi()
        assert seats_command(_args(purge=True), api=api) == 1
        assert api.calls == []
        assert "--yes" in capsys.readouterr().err

    def test_purge_dry_run_writes_nothing(self, capsys):
        api = _FakeApi()
        assert seats_command(_args(purge=True, yes=True, dry_run=True),
                             api=api) == 0
        assert api.calls == []
        assert "would PURGE" in capsys.readouterr().out

    def test_purge_with_yes_calls_purge(self, capsys):
        api = _FakeApi()
        assert seats_command(_args(purge=True, yes=True), api=api) == 0
        assert api.calls == [("delete", "s1", True)]

    def test_plain_rm_still_archives_and_names_the_undo(self, capsys):
        api = _FakeApi()
        assert seats_command(_args(), api=api) == 0
        assert api.calls == [("delete", "s1")] or api.calls == [
            ("delete", "s1", False)
        ]
        assert "seats restore" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Bar 37 limb 2, hub half — the trail names the CREDENTIAL, not a literal
# ---------------------------------------------------------------------------


class TestActorComesFromAuth:
    """`actor` was the literal "operator-api" at all three call sites, so the
    trail asserted a constant instead of observing the door. Mutation: revert
    `actor_of(got)` to `"operator-api"` → every assertion here fails.

    ⚠️ SCOPE, stated because the bar's wording invites the wider claim: this
    pins ONE path (the seat-event path). It is not "the hub attributes every
    write". And every seat_event caller is behind `operator_only`, so the
    honest value here is the GRADE — a machine principal cannot reach these
    routes at all, which is why this alone cannot attribute a deputy write.
    """

    def test_archive_and_restore_record_the_authenticated_grade(self, rig):
        c, db_path = rig
        _machine(c)
        _seat(c, "w1seat")
        _archive(c)
        r = c.post("/api/v1/seats/w1seat/restore", headers=OP)
        assert r.status_code == 200, r.text
        rows = _actors(db_path, "w1seat")
        assert [e for e, _ in rows] == ["archived", "restored"]
        assert {a for _, a in rows} == {"operator"}, rows

    def test_purge_death_fact_names_the_grade_too(self, rig):
        c, db_path = rig
        _machine(c)
        _seat(c, "w1purge")
        r = c.delete("/api/v1/seats/w1purge?purge=true", headers=OP)
        assert r.status_code == 200, r.text
        rows = _actors(db_path, "w1purge")
        assert rows == [("purged", "operator")], rows

    def test_no_row_still_carries_the_old_literal(self, rig):
        """The literal is gone from the trail entirely — a leftover call site
        would keep writing "operator-api" and this catches it."""
        c, db_path = rig
        _machine(c)
        _seat(c, "w1lit")
        _archive(c, "w1lit")
        assert "operator-api" not in {a for _, a in _actors(db_path, "w1lit")}
