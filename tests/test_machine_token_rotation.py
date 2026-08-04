"""Rotating a machine token — the recovery path that did not exist.

Enrolment returns a token exactly once and the hub keeps only a hash, so a
caller that drops it has destroyed it. Both machines in this fleet lost theirs
that way on 2026-07-30, which left `edge apply` authenticating with the
OPERATOR token — one credential that drives every machine, on every box,
indefinitely.

The properties that matter: the old token STOPS working, the new one starts,
the operator token is required to ask, and the client writes the answer to disk
before it prints anything.
"""

from __future__ import annotations

import json
import pathlib

import pytest
from starlette.testclient import TestClient

from mcp_hub import cli
from mcp_hub.operator_api import MACHINE_TOKEN_FILE

OP = "operator-token-for-tests"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # Same shape as test_api_v1's fixture: speak ONLY through HTTP, so these
    # admit any implementation honouring the contract.
    monkeypatch.setenv("MCP_HUB_API_TOKEN", OP)
    from mcp_hub.server import create_server

    server = create_server(db_path=tmp_path / "hub.db")
    with TestClient(server.streamable_http_app()) as c:
        yield c


def _enrol(client, name="box-1"):
    r = client.post("/api/v1/machines",
                    headers={"Authorization": f"Bearer {OP}"},
                    json={"name": name, "os": "linux",
                          "capabilities": {"worktree": True}})
    assert r.status_code == 201, r.text
    return r.json()["token"]


def test_the_old_token_stops_working_and_the_new_one_starts(client):
    old = _enrol(client)
    path = "/api/v1/machines/box-1/placements"

    assert client.get(path, headers={"Authorization": f"Bearer {old}"}
                      ).status_code == 200

    r = client.post("/api/v1/machines/box-1/rotate-token",
                    headers={"Authorization": f"Bearer {OP}"})
    assert r.status_code == 200, r.text
    new = r.json()["token"]
    assert new and new != old

    # BOTH directions, or this proves only that a new token exists.
    assert client.get(path, headers={"Authorization": f"Bearer {new}"}
                      ).status_code == 200
    assert client.get(path, headers={"Authorization": f"Bearer {old}"}
                      ).status_code == 401


def test_a_machine_cannot_rotate_its_own_credential(client):
    """It could otherwise lock the operator out of it, and the recovery path
    for THAT is the one that was already missing."""
    tok = _enrol(client)
    r = client.post("/api/v1/machines/box-1/rotate-token",
                    headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 403, r.text


def test_rotating_an_unenrolled_machine_is_a_404_not_a_new_machine(client):
    r = client.post("/api/v1/machines/ghost-box/rotate-token",
                    headers={"Authorization": f"Bearer {OP}"})
    assert r.status_code == 404, r.text


def test_rotation_does_not_disturb_the_machines_other_fields(client):
    _enrol(client)
    before = client.get("/api/v1/machines/box-1",
                        headers={"Authorization": f"Bearer {OP}"}).json()
    client.post("/api/v1/machines/box-1/rotate-token",
                headers={"Authorization": f"Bearer {OP}"})
    after = client.get("/api/v1/machines/box-1",
                       headers={"Authorization": f"Bearer {OP}"}).json()
    assert after["capabilities"] == before["capabilities"]
    assert after["os"] == before["os"]


# ---- the client half -------------------------------------------------------

class FakeApi:
    def __init__(self, token="new-token-abc", fail=None):
        self._token = token
        self._fail = fail
        self.rotated: list[str] = []

    def rotate_machine_token(self, name):
        if self._fail:
            from mcp_hub.operator_api import ApiUnavailable
            raise ApiUnavailable(self._fail)
        self.rotated.append(name)
        return {"name": name, "os": "linux", "token": self._token}


def _args(tmp_path, **kw):
    import argparse
    base = dict(action="rotate", name="box-1", os="linux",
                token_file=str(tmp_path / "machine.token"), force=False,
                print_token=False, hub_url="http://h/mcp", json=False)
    base.update(kw)
    return argparse.Namespace(**base)


def test_the_token_is_on_disk_before_anything_is_printed(tmp_path, capsys):
    """The 2026-07-30 loss, pinned: the token went through a shell pipeline
    that printed it and never saved it."""
    api = FakeApi()
    dest = tmp_path / "machine.token"
    rc = cli.machines_command(_args(tmp_path), api=api)
    assert rc == 0
    assert dest.read_text() == "new-token-abc"
    assert oct(dest.stat().st_mode)[-3:] == "600"
    out = capsys.readouterr().out
    assert "previous token is now invalid" in out
    assert "new-token-abc" not in out          # not printed unless asked


def test_the_token_is_printed_only_on_request(tmp_path, capsys):
    rc = cli.machines_command(_args(tmp_path, print_token=True), api=FakeApi())
    assert rc == 0
    assert "new-token-abc" in capsys.readouterr().out


def test_rotate_overwrites_a_stale_file_without_force(tmp_path):
    """The whole point is that what is on disk is stale or missing, so the
    enrol-time --force guard would make the recovery path unusable."""
    dest = tmp_path / "machine.token"
    dest.write_text("stale-token")
    assert cli.machines_command(_args(tmp_path), api=FakeApi()) == 0
    assert dest.read_text() == "new-token-abc"


def test_rotating_an_unenrolled_machine_says_to_enrol_it(tmp_path, capsys):
    api = FakeApi(fail="hub API error 404 on /api/v1/machines/x/rotate-token")
    rc = cli.machines_command(_args(tmp_path), api=api)
    assert rc == 1
    err = capsys.readouterr().err
    assert "not enrolled" in err and "enrol" in err


def test_a_rotation_that_returns_no_token_is_a_loud_failure(tmp_path, capsys):
    """The old token is already dead at that point; reporting success would
    strand the machine silently."""
    rc = cli.machines_command(_args(tmp_path), api=FakeApi(token=""))
    assert rc == 1
    assert "unrecoverable" in capsys.readouterr().err


def test_write_machine_token_uses_the_module_default_when_given_no_dest(
        tmp_path, monkeypatch):
    """Patch the CONSTANT, never reload the module.

    The first version of this test monkeypatched `Path.home` and reloaded
    `operator_api` to re-derive its constants — but the restoring reload ran
    while the patch was still active, so TOKEN_FILE and MACHINE_TOKEN_FILE
    stayed pointed at a tmp_path for the rest of the session. Seven unrelated
    tests failed in the full run and passed in isolation.
    """
    from mcp_hub import operator_api

    dest = tmp_path / "machine.token"
    monkeypatch.setattr(operator_api, "MACHINE_TOKEN_FILE", dest)
    where = operator_api.write_machine_token("t")
    assert where == str(dest)
    assert dest.read_text() == "t"
    assert oct(dest.stat().st_mode)[-3:] == "600"


def test_the_documented_default_path_is_the_one_edge_reads():
    """Two names for one file is how a rotation lands somewhere the edge
    never looks."""
    assert str(MACHINE_TOKEN_FILE).endswith("/.mcp-hub/machine.token")


def test_rotate_is_reachable_through_the_parser():
    args = cli.build_parser().parse_args(["machines", "rotate"])
    assert args.subcommand == "machines" and args.action == "rotate"


def test_the_systemd_units_point_at_the_venv_binary_not_a_bare_name():
    """systemd user units get a bare PATH with no ~/.local/bin — the same gap
    that made `edge apply` die on a raw FileNotFoundError over ssh."""
    root = pathlib.Path(__file__).resolve().parents[1]
    svc = (root / "squad/systemd/mcp-hub-edge.service").read_text()
    assert ".venv/bin/mcp-hub edge apply" in svc
    assert "\nExecStart=mcp-hub" not in svc
    timer = (root / "squad/systemd/mcp-hub-edge.timer").read_text()
    assert "OnUnitActiveSec=2min" in timer
    assert "RandomizedDelaySec" in timer      # no thundering herd after an outage


def test_json_is_still_valid_after_a_rotation(client):
    """Guards the shape the edge client parses."""
    _enrol(client)
    r = client.post("/api/v1/machines/box-1/rotate-token",
                    headers={"Authorization": f"Bearer {OP}"})
    body = json.loads(r.text)
    assert set(body) >= {"name", "os", "capabilities", "token"}
