"""`seats update`, `seats clone`, `machines rm` — routes that had no door.

🔴 THE PATTERN I FIXED ONCE AND FAILED TO GENERALISE (2026-08-08). The squads
gap was "the REST route is complete, the CLI door is missing". I closed it for
squads and did not sweep for others. There were three more:

    PATCH  /api/v1/seats/{id}          no CLI  → a seat could not be EDITED
    POST   /api/v1/seats/{id}/clone    no CLI  → unreachable entirely
    DELETE /api/v1/machines/{name}     no CLI  → a dead box stayed forever

⇒ A gap found in one place is a CLASS, not an instance. Sweep the surface the
moment the first one turns up, or you drip-feed the operator for hours.

And exposing them surfaced two defects IN the routes, which is the argument
for having a door at all — an unreachable route is an untested route:
  * PATCH could not touch `spec`, so re-briefing meant teardown
  * clone did not copy `spec`, so a cloned docker seat lost its image
"""
from __future__ import annotations

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from mcp_hub import cli
from mcp_hub.server import create_server

OPERATOR_TOKEN = "test-operator-token"
H = {"Authorization": f"Bearer {OPERATOR_TOKEN}"}


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MCP_HUB_API_TOKEN", OPERATOR_TOKEN)
    server = create_server(db_path=tmp_path / "hub.db")
    with TestClient(server.streamable_http_app()) as c:
        yield c


def _seat(client, identity="s1", spec=None):
    client.post("/api/v1/machines",
                json={"name": "box-1", "os": "linux",
                      "capabilities": {"docker": True}}, headers=H)
    r = client.post("/api/v1/seats", json={
        "repo": "acme/widget", "machine": "box-1", "folder": "/w",
        "identity": identity,
        "spec": spec if spec is not None else {"image": "seat:latest"},
    }, headers=H)
    assert r.status_code == 201, r.text
    return r.json()


def _get(client, identity):
    return client.get(f"/api/v1/seats/{identity}", headers=H).json()


# ------------------------------------------------------------ seats update


class TestSeatUpdate:
    def test_a_brief_can_be_CHANGED_without_tearing_the_seat_down(self, client):
        """🔴 The defect. PATCH only ever wrote launch_args and class, so the
        one field an operator most wants to revise — the brief — could only be
        changed by reclaiming the placement, archiving the seat and declaring
        a new one. That is a teardown wearing an edit's name."""
        _seat(client, spec={"image": "seat:latest", "brief": "OLD"})
        r = client.patch("/api/v1/seats/s1", json={"spec": {"brief": "NEW"}},
                         headers=H)
        assert r.status_code == 200, r.text
        assert _get(client, "s1")["spec"]["brief"] == "NEW"

    def test_patching_the_brief_does_NOT_drop_the_image(self, client):
        """MERGED, not replaced. A PATCH that sent only the brief and silently
        replaced the whole spec would leave a seat that can never be
        materialized again, and nothing would say why."""
        _seat(client, spec={"image": "seat:latest", "memory_volume": "v1"})
        client.patch("/api/v1/seats/s1", json={"spec": {"brief": "hi"}},
                     headers=H)
        spec = _get(client, "s1")["spec"]
        assert spec["image"] == "seat:latest"
        assert spec["memory_volume"] == "v1"
        assert spec["brief"] == "hi"

    def test_an_explicit_null_REMOVES_a_key(self, client):
        """Otherwise a brief could be set and never unset — merge-only would
        make every spec key permanent once written."""
        _seat(client, spec={"image": "seat:latest", "brief": "OLD"})
        client.patch("/api/v1/seats/s1", json={"spec": {"brief": None}},
                     headers=H)
        assert "brief" not in _get(client, "s1")["spec"]

    def test_a_non_object_spec_is_refused(self, client):
        _seat(client)
        r = client.patch("/api/v1/seats/s1", json={"spec": "nope"}, headers=H)
        assert r.status_code == 422


# ------------------------------------------------------------- seats clone


class TestSeatClone:
    def test_the_SPEC_travels(self, client):
        """🔴 The defect. The INSERT omitted `spec` entirely, so cloning a
        DOCKER seat produced a row with no image, no volumes, no env and no
        brief — a worktree seat wearing the original's name, which would be
        declared happily and then fail to materialize citing the wrong thing.
        """
        _seat(client, spec={"image": "seat:latest", "brief": "B",
                            "env": {"K": "V"}})
        r = client.post("/api/v1/seats/s1/clone", json={"suffix": "two"},
                        headers=H)
        assert r.status_code == 201, r.text
        spec = _get(client, "s1-two")["spec"]
        assert spec["image"] == "seat:latest"
        assert spec["brief"] == "B"
        assert spec["env"] == {"K": "V"}

    def test_pod_inhabitants_are_re_identified(self, client):
        """The same lesson capsule minting paid for: suffixing only the
        container leaves two containers holding agents with IDENTICAL hub
        names — the collision moves somewhere nothing can see it."""
        _seat(client, spec={"image": "seat:latest", "squad": "spike",
                            "agents": [{"identity": "alice"},
                                       {"identity": "bob"}]})
        client.post("/api/v1/seats/s1/clone", json={"suffix": "two"},
                    headers=H)
        spec = _get(client, "s1-two")["spec"]
        assert [a["identity"] for a in spec["agents"]] == ["alice-two",
                                                           "bob-two"]
        assert spec["squad"] == "spike-two"

    def test_the_MEMORY_VOLUME_is_not_shared_with_the_original(self, client):
        """Two seats on one volume would write each other's memory and
        results — and reclaiming either would harvest a volume the other is
        still using."""
        _seat(client, spec={"image": "seat:latest", "memory_volume": "mem"})
        client.post("/api/v1/seats/s1/clone", json={"suffix": "two"},
                    headers=H)
        assert _get(client, "s1-two")["spec"]["memory_volume"] == "mem-two"

    def test_it_records_where_it_came_from(self, client):
        _seat(client)
        client.post("/api/v1/seats/s1/clone", json={"suffix": "two"},
                    headers=H)
        assert _get(client, "s1-two")["cloned_from"] == "s1"

    def test_a_repeated_suffix_is_refused(self, client):
        _seat(client)
        client.post("/api/v1/seats/s1/clone", json={"suffix": "two"},
                    headers=H)
        again = client.post("/api/v1/seats/s1/clone", json={"suffix": "two"},
                            headers=H)
        assert again.status_code == 409


# ------------------------------------------------------------- machines rm


def test_a_machine_can_be_retired(client):
    client.post("/api/v1/machines",
                json={"name": "old-box", "os": "linux"}, headers=H)
    r = client.delete("/api/v1/machines/old-box", headers=H)
    assert r.status_code == 200, r.text
    names = [m["name"] for m in
             client.get("/api/v1/machines", headers=H).json()["machines"]]
    assert "old-box" not in names


# ------------------------------------------------------- the CLI doors


class FakeApi:
    def __init__(self):
        self.calls: list[tuple] = []

    def update_seat(self, identity, spec=None, launch_args=None, klass=None):
        self.calls.append(("update", identity, spec, launch_args))
        return {"identity": identity}

    def clone_seat(self, identity, suffix, machine=""):
        self.calls.append(("clone", identity, suffix, machine))
        return {"identity": f"{identity}-{suffix}", "machine": "box-1"}

    def delete_machine(self, name):
        self.calls.append(("delete_machine", name))
        return {"name": name}

    def list_machines(self):
        return []


def _parse(argv):
    return cli.build_parser().parse_args(argv)


def test_seats_update_sends_the_brief(capsys, tmp_path):
    p = tmp_path / "b.md"
    p.write_text("SPIKE IT")
    api = FakeApi()
    rc = cli.seats_command(
        _parse(["seats", "update", "s1", "--brief", f"@{p}"]), api=api)
    assert rc == 0
    assert api.calls[0][:2] == ("update", "s1")
    assert api.calls[0][2]["brief"] == "SPIKE IT"


def test_seats_update_WARNS_that_a_running_container_keeps_the_old_brief(
        capsys, tmp_path):
    """The edit changes the DECLARATION. Without saying so, an operator waits
    for a change that will never arrive."""
    api = FakeApi()
    cli.seats_command(
        _parse(["seats", "update", "s1", "--brief", "x"]), api=api)
    out = capsys.readouterr().out.lower()
    assert "old brief" in out and "re-place" in out


def test_seats_update_with_nothing_to_change_refuses(capsys):
    api = FakeApi()
    assert cli.seats_command(_parse(["seats", "update", "s1"]), api=api) == 1
    assert api.calls == []


def test_seats_clone_requires_a_suffix(capsys):
    """Without one the clone would collide with the original — the exact
    duplicate-identity collapse the runtime exists to prevent."""
    api = FakeApi()
    assert cli.seats_command(_parse(["seats", "clone", "s1"]), api=api) == 1
    assert "--as" in capsys.readouterr().err
    assert api.calls == []


def test_seats_clone_reaches_the_hub_and_says_it_is_not_running(capsys):
    api = FakeApi()
    rc = cli.seats_command(
        _parse(["seats", "clone", "s1", "--as", "two"]), api=api)
    assert rc == 0
    assert api.calls[0][:3] == ("clone", "s1", "two")
    assert "placements set" in capsys.readouterr().out


def test_machines_rm_refuses_without_a_NAME(capsys):
    """🔴 `enrol` defaults to this hostname because you can only enrol the box
    you are on. You RETIRE a machine precisely when you are not on it — so the
    same default here would retire the wrong one on a bare `machines rm`."""
    api = FakeApi()
    rc = cli.machines_command(_parse(["machines", "rm"]), api=api)
    assert rc == 1
    assert api.calls == [], "retired a machine nobody named"


def test_machines_rm_names_the_placement_consequence(capsys):
    api = FakeApi()
    rc = cli.machines_command(_parse(["machines", "rm", "old-box"]), api=api)
    assert rc == 0
    assert api.calls == [("delete_machine", "old-box")]
    assert "placements" in capsys.readouterr().out.lower()


def test_every_new_action_is_reachable():
    """Dispatch AND parser — implemented-but-unreachable is the shape this
    whole file exists because of."""
    for argv in (["seats", "update", "--help"], ["seats", "clone", "--help"],
                 ["machines", "rm", "--help"]):
        with pytest.raises(SystemExit) as e:
            cli.main(argv)
        assert e.value.code == 0
