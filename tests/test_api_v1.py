"""Contract tests for the /api/v1 management surface — docs/hub-api-v1.md.

These are the GATE, written before the implementation (brief-is-the-gate):
every test speaks ONLY through the HTTP API — no store imports, no registry
reaching — so the suite admits any implementation that honours the contract
and rejects any that merely resembles it.

The one non-API test is the positive control: /health must pass with no auth
BEFORE any /api/v1 assertion is trusted. If the control fails, every 404
below is an instrument failure, not a contract verdict.
"""

from __future__ import annotations

import io
import tarfile
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


@pytest.fixture()
def noauth_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A hub whose operator never set MCP_HUB_API_TOKEN — API must refuse."""
    monkeypatch.delenv("MCP_HUB_API_TOKEN", raising=False)
    server = create_server(db_path=tmp_path / "hub2.db")
    with TestClient(server.streamable_http_app()) as c:
        yield c


def _machine(client, name="box-1", **caps) -> dict:
    r = client.post(
        "/api/v1/machines",
        json={"name": name, "os": "linux", "capabilities": {"docker": True, **caps}},
        headers=H,
    )
    assert r.status_code == 201, r.text
    return r.json()


def _seat(client, name=None, machine="box-1", repo="acme/widget") -> dict:
    body = {"repo": repo, "machine": machine, "folder": f"/home/x/{repo}"}
    if name:
        body["identity"] = name
    r = client.post("/api/v1/seats", json=body, headers=H)
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------------------
# Positive control
# ---------------------------------------------------------------------------


class TestPositiveControl:
    def test_health_answers_without_auth(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        # Build identity + uptime: the two facts that discriminate
        # deploy vs restart vs untouched without prod ssh.
        assert "commit" in body
        assert isinstance(body["uptime_seconds"], int)
        assert body["uptime_seconds"] >= 0
        assert body["started_at"].endswith("Z")


# ---------------------------------------------------------------------------
# Auth — the lock on the door
# ---------------------------------------------------------------------------

ALL_COLLECTION_ROUTES = [
    ("GET", "/api/v1/machines"),
    ("POST", "/api/v1/machines"),
    ("GET", "/api/v1/seats"),
    ("POST", "/api/v1/seats"),
    ("GET", "/api/v1/squads"),
    ("POST", "/api/v1/squads"),
    ("GET", "/api/v1/workspaces"),
    ("POST", "/api/v1/workspaces"),
    ("GET", "/api/v1/capsules"),
    ("POST", "/api/v1/capsules"),
    ("GET", "/api/v1/placements"),
    ("POST", "/api/v1/placements"),
]


class TestAuth:
    @pytest.mark.parametrize("method,path", ALL_COLLECTION_ROUTES)
    def test_no_token_is_401(self, client, method, path):
        r = client.request(method, path)
        assert r.status_code == 401

    @pytest.mark.parametrize("method,path", ALL_COLLECTION_ROUTES)
    def test_wrong_token_is_401(self, client, method, path):
        r = client.request(method, path, headers={"Authorization": "Bearer nope"})
        assert r.status_code == 401

    def test_unconfigured_api_refuses_503(self, noauth_client):
        # No MCP_HUB_API_TOKEN in the environment: the management surface is
        # OFF, loudly — not open, not guessing.
        r = noauth_client.get(
            "/api/v1/squads", headers={"Authorization": "Bearer anything"}
        )
        assert r.status_code == 503

    def test_machine_token_cannot_manage_squads(self, client):
        token = _machine(client)["token"]
        r = client.post(
            "/api/v1/squads",
            json={"name": "sneaky"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 403


# ---------------------------------------------------------------------------
# Machines
# ---------------------------------------------------------------------------


class TestMachines:
    def test_enroll_issues_token_and_lists(self, client):
        m = _machine(client, "box-a")
        assert m["name"] == "box-a"
        assert m["token"]  # issued exactly here, shown exactly once
        assert m["capabilities"]["docker"] is True
        names = [x["name"] for x in client.get("/api/v1/machines", headers=H).json()["machines"]]
        assert "box-a" in names

    def test_enroll_duplicate_409(self, client):
        _machine(client, "box-b")
        r = client.post(
            "/api/v1/machines", json={"name": "box-b", "os": "linux"}, headers=H
        )
        assert r.status_code == 409

    def test_patch_capabilities(self, client):
        _machine(client, "box-c")
        r = client.patch(
            "/api/v1/machines/box-c",
            json={"capabilities": {"docker": False}},
            headers=H,
        )
        assert r.status_code == 200
        assert r.json()["capabilities"]["docker"] is False

    def test_get_unknown_404(self, client):
        assert client.get("/api/v1/machines/ghost", headers=H).status_code == 404

    def test_delete_refuses_while_placements_exist(self, client):
        _machine(client, "box-d")
        seat = _seat(client, machine="box-d")
        r = client.post(
            "/api/v1/placements",
            json={"seat": seat["identity"], "machine": "box-d", "substrate": "worktree"},
            headers=H,
        )
        assert r.status_code == 201
        assert client.delete("/api/v1/machines/box-d", headers=H).status_code == 409

    def test_edge_pull_with_own_machine_token(self, client):
        token = _machine(client, "box-e")["token"]
        r = client.get(
            "/api/v1/machines/box-e/placements",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200
        assert r.json()["placements"] == []

    def test_edge_pull_embeds_seat_specs(self, client):
        # The pull is "full desired state for this box": a machine token
        # cannot read /seats/*, so materialization data must ride along.
        token = _machine(client, "box-e2")["token"]
        seat = _seat(client, machine="box-e2")
        client.post(
            "/api/v1/placements",
            json={"seat": seat["identity"], "machine": "box-e2", "substrate": "worktree"},
            headers=H,
        )
        r = client.get(
            "/api/v1/machines/box-e2/placements",
            headers={"Authorization": f"Bearer {token}"},
        )
        spec = r.json()["placements"][0]["seat_spec"]
        assert spec["repo"] == "acme/widget"
        assert spec["folder"]
        assert "launch_args" in spec

    def test_edge_pull_foreign_machine_403(self, client):
        _machine(client, "box-f")
        other = _machine(client, "box-g")["token"]
        r = client.get(
            "/api/v1/machines/box-f/placements",
            headers={"Authorization": f"Bearer {other}"},
        )
        assert r.status_code == 403

    def test_edge_status_push_updates_last_seen(self, client):
        token = _machine(client, "box-h")["token"]
        r = client.post(
            "/api/v1/machines/box-h/status",
            json={"seats": [], "containers": [], "disk_free_gb": 100},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200
        m = client.get("/api/v1/machines/box-h", headers=H).json()
        assert m["last_seen"] is not None


# ---------------------------------------------------------------------------
# Seats
# ---------------------------------------------------------------------------


class TestSeats:
    def test_create_assigns_identity_when_omitted(self, client):
        _machine(client)
        s = _seat(client)  # no identity passed
        # Identity is ALWAYS assigned by the hub — never left for a container
        # hostname to derive (runtime design: assigned-over-derived).
        assert s["identity"] == "widget-box-1"

    def test_create_honours_given_identity(self, client):
        _machine(client)
        s = _seat(client, name="widget-custom")
        assert s["identity"] == "widget-custom"

    def test_duplicate_identity_409(self, client):
        _machine(client)
        _seat(client, name="widget-dup")
        r = client.post(
            "/api/v1/seats",
            json={
                "repo": "acme/widget",
                "machine": "box-1",
                "folder": "/elsewhere",
                "identity": "widget-dup",
            },
            headers=H,
        )
        assert r.status_code == 409

    def test_get_merges_presence(self, client):
        _machine(client)
        s = _seat(client)
        got = client.get(f"/api/v1/seats/{s['identity']}", headers=H).json()
        # A freshly defined seat has never registered: presence must say so
        # truthfully rather than omit the block.
        assert got["presence"]["online"] is False

    def test_patch_launch_args(self, client):
        _machine(client)
        s = _seat(client)
        r = client.patch(
            f"/api/v1/seats/{s['identity']}",
            json={"launch_args": "--continue --model fable"},
            headers=H,
        )
        assert r.status_code == 200
        assert r.json()["launch_args"] == "--continue --model fable"

    def test_clone_creates_suffixed_seat(self, client):
        _machine(client)
        _machine(client, "box-2")
        s = _seat(client)
        r = client.post(
            f"/api/v1/seats/{s['identity']}/clone",
            json={"machine": "box-2", "suffix": "runtime"},
            headers=H,
        )
        assert r.status_code == 201
        clone = r.json()
        assert clone["identity"] == "widget-box-1-runtime"
        assert clone["cloned_from"] == "widget-box-1"

    def test_delete_with_active_placement_409(self, client):
        _machine(client)
        s = _seat(client)
        client.post(
            "/api/v1/placements",
            json={"seat": s["identity"], "machine": "box-1", "substrate": "worktree"},
            headers=H,
        )
        r = client.delete(f"/api/v1/seats/{s['identity']}", headers=H)
        assert r.status_code == 409


# ---------------------------------------------------------------------------
# Squads — full lifecycle
# ---------------------------------------------------------------------------


class TestSquads:
    def test_create_defaults_visible(self, client):
        r = client.post("/api/v1/squads", json={"name": "alpha"}, headers=H)
        assert r.status_code == 201
        assert r.json()["board_visibility"] == "shown"

    def test_create_hidden(self, client):
        r = client.post(
            "/api/v1/squads",
            json={"name": "shadow", "board_visibility": "hidden"},
            headers=H,
        )
        assert r.json()["board_visibility"] == "hidden"

    def test_duplicate_409(self, client):
        client.post("/api/v1/squads", json={"name": "beta"}, headers=H)
        assert (
            client.post("/api/v1/squads", json={"name": "beta"}, headers=H).status_code
            == 409
        )

    def test_membership_put_is_idempotent(self, client):
        client.post("/api/v1/squads", json={"name": "gamma"}, headers=H)
        for _ in range(2):
            r = client.put(
                "/api/v1/squads/gamma/members/agent-x", json={}, headers=H
            )
            assert r.status_code in (200, 201)
        members = client.get("/api/v1/squads/gamma/members", headers=H).json()["members"]
        assert [m["seat"] for m in members] == ["agent-x"]

    def test_member_mute_roundtrip(self, client):
        client.post("/api/v1/squads", json={"name": "delta"}, headers=H)
        client.put("/api/v1/squads/delta/members/agent-y", json={}, headers=H)
        r = client.patch(
            "/api/v1/squads/delta/members/agent-y", json={"muted": True}, headers=H
        )
        assert r.status_code == 200
        members = client.get("/api/v1/squads/delta/members", headers=H).json()["members"]
        assert members[0]["muted"] is True

    def test_member_remove(self, client):
        client.post("/api/v1/squads", json={"name": "eps"}, headers=H)
        client.put("/api/v1/squads/eps/members/agent-z", json={}, headers=H)
        assert (
            client.delete("/api/v1/squads/eps/members/agent-z", headers=H).status_code
            == 200
        )
        assert client.get("/api/v1/squads/eps/members", headers=H).json()["members"] == []

    def test_membership_source_is_attributed(self, client):
        # API-set membership must record its origin so the board can stop
        # showing the pooled union (the 2026-07-29 attribution defect).
        client.post("/api/v1/squads", json={"name": "zeta"}, headers=H)
        client.put("/api/v1/squads/zeta/members/agent-s", json={}, headers=H)
        members = client.get("/api/v1/squads/zeta/members", headers=H).json()["members"]
        assert members[0]["source"] == "api"

    def test_rename_cascades_membership(self, client):
        client.post("/api/v1/squads", json={"name": "old-name"}, headers=H)
        client.put("/api/v1/squads/old-name/members/agent-r", json={}, headers=H)
        r = client.patch(
            "/api/v1/squads/old-name", json={"name": "new-name"}, headers=H
        )
        assert r.status_code == 200
        assert client.get("/api/v1/squads/old-name", headers=H).status_code == 404
        members = client.get("/api/v1/squads/new-name/members", headers=H).json()[
            "members"
        ]
        assert [m["seat"] for m in members] == ["agent-r"]

    def test_rename_cascades_queued_broadcast_audience(self, tmp_path, monkeypatch):
        """Dev's review find, silent-loss family: a member with UNREAD
        squad-scoped broadcasts at rename time must not lose them — the
        drain matches audience against the NEW name, so the stamped rows
        must move in the same transaction as the membership."""
        import anyio
        from starlette.testclient import TestClient

        from mcp_hub.server import create_server

        monkeypatch.setenv("MCP_HUB_API_TOKEN", OPERATOR_TOKEN)
        server = create_server(db_path=tmp_path / "rename.db")

        async def tool(name, args):
            res = await server._tool_manager.call_tool(name, args)
            for block in getattr(res, "content", res if isinstance(res, list) else []):
                if hasattr(block, "text"):
                    return block.text
            return str(res)

        with TestClient(server.streamable_http_app()) as client:
            anyio.run(tool, "register", {"name": "speaker"})
            anyio.run(tool, "register", {"name": "hearer"})
            client.post("/api/v1/squads", json={"name": "oldsq"}, headers=H)
            for a in ("speaker", "hearer"):
                client.put(f"/api/v1/squads/oldsq/members/{a}", json={}, headers=H)
            anyio.run(
                tool,
                "broadcast",
                {"from_agent": "speaker", "message": "scoped payload",
                 "scope": "oldsq", "priority": "low"},
            )
            r = client.patch(
                "/api/v1/squads/oldsq", json={"name": "newsq"}, headers=H
            )
            assert r.status_code == 200
            drained = anyio.run(
                tool, "get_broadcasts_for_agent", {"agent_name": "hearer"}
            )
            assert "scoped payload" in drained, (
                "queued broadcast lost across rename — the orphaned-audience leak"
            )

    def test_member_put_updates_mute_true_put_semantics(self, client):
        # Dev's minor 1: PUT with muted:true on an EXISTING membership must
        # take effect — a 200 that changed nothing is a silent no-op wearing
        # a success code.
        client.post("/api/v1/squads", json={"name": "putsq"}, headers=H)
        client.put("/api/v1/squads/putsq/members/agent-m", json={}, headers=H)
        client.put(
            "/api/v1/squads/putsq/members/agent-m", json={"muted": True}, headers=H
        )
        members = client.get("/api/v1/squads/putsq/members", headers=H).json()["members"]
        assert members[0]["muted"] is True

    def test_delete_archives_and_reserves_name(self, client):
        client.post("/api/v1/squads", json={"name": "doomed"}, headers=H)
        r = client.delete("/api/v1/squads/doomed", headers=H)
        assert r.status_code == 200
        assert r.json()["archived"] is True
        # Name stays reserved: an archived squad's history must remain
        # attributable, so the name cannot be silently reused.
        assert (
            client.post("/api/v1/squads", json={"name": "doomed"}, headers=H).status_code
            == 409
        )

    def test_purge_is_structural_and_says_so(self, client):
        client.post("/api/v1/squads", json={"name": "purged"}, headers=H)
        client.put("/api/v1/squads/purged/members/agent-p", json={}, headers=H)
        r = client.delete("/api/v1/squads/purged?purge=true", headers=H)
        assert r.status_code == 200
        body = r.json()
        # The retention decision, encoded in the response contract: purge
        # removes structure, never message history.
        assert body["purged"]["memberships"] == 1
        assert body["messages_retained"] is True

    def test_archived_hidden_from_default_list(self, client):
        client.post("/api/v1/squads", json={"name": "gone"}, headers=H)
        client.delete("/api/v1/squads/gone", headers=H)
        names = [s["name"] for s in client.get("/api/v1/squads", headers=H).json()["squads"]]
        assert "gone" not in names
        names_all = [
            s["name"]
            for s in client.get(
                "/api/v1/squads?include_archived=true", headers=H
            ).json()["squads"]
        ]
        assert "gone" in names_all


# ---------------------------------------------------------------------------
# Workspaces
# ---------------------------------------------------------------------------


class TestWorkspaces:
    def _ws(self, client, name="runtime", squad=None) -> dict:
        body = {
            "name": name,
            "listings": [{"path": "code/acme/widget"}, {"path": "code/acme/gadget"}],
        }
        if squad:
            body["squad"] = squad
        r = client.post("/api/v1/workspaces", json=body, headers=H)
        assert r.status_code == 201, r.text
        return r.json()

    def test_create_and_get(self, client):
        ws = self._ws(client)
        got = client.get(f"/api/v1/workspaces/{ws['id']}", headers=H).json()
        assert len(got["listings"]) == 2

    def test_file_download_is_a_valid_workspace(self, client):
        ws = self._ws(client)
        r = client.get(f"/api/v1/workspaces/{ws['id']}/file", headers=H)
        assert r.status_code == 200
        body = r.json()
        # Exactly what VSCode opens: folders + settings, listings preserved.
        assert [f["path"] for f in body["folders"]] == [
            "code/acme/widget",
            "code/acme/gadget",
        ]
        assert "settings" in body

    def test_squad_typing_recorded(self, client):
        client.post("/api/v1/squads", json={"name": "typed"}, headers=H)
        ws = self._ws(client, name="typed-ws", squad="typed")
        got = client.get(f"/api/v1/workspaces/{ws['id']}", headers=H).json()
        assert got["squad"] == "typed"

    def test_patch_adds_listing(self, client):
        ws = self._ws(client)
        r = client.patch(
            f"/api/v1/workspaces/{ws['id']}",
            json={"add_listings": [{"path": "code/acme/doohickey"}]},
            headers=H,
        )
        assert r.status_code == 200
        assert len(r.json()["listings"]) == 3


# ---------------------------------------------------------------------------
# Capsules — squad-in-a-box
# ---------------------------------------------------------------------------


class TestCapsules:
    def _squad_with_seat(self, client) -> str:
        _machine(client)
        seat = _seat(client)
        client.post("/api/v1/squads", json={"name": "boxed"}, headers=H)
        client.put(f"/api/v1/squads/boxed/members/{seat['identity']}", json={}, headers=H)
        return "boxed"

    def test_compose_produces_hashed_manifest(self, client):
        squad = self._squad_with_seat(client)
        r = client.post("/api/v1/capsules", json={"squad": squad}, headers=H)
        assert r.status_code == 201, r.text
        cap = r.json()
        assert cap["squad"] == squad
        assert len(cap["manifest"]["seats"]) == 1
        # Verifiable: every artifact hashed — the convergence witness.
        for entry in cap["manifest"]["entries"]:
            assert entry["path"]
            assert len(entry["sha256"]) == 64

    def test_compose_unknown_squad_404(self, client):
        r = client.post("/api/v1/capsules", json={"squad": "nope"}, headers=H)
        assert r.status_code == 404

    def test_download_is_a_tarball_containing_manifest(self, client):
        squad = self._squad_with_seat(client)
        cap = client.post("/api/v1/capsules", json={"squad": squad}, headers=H).json()
        r = client.get(f"/api/v1/capsules/{cap['id']}/download", headers=H)
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/gzip"
        with tarfile.open(fileobj=io.BytesIO(r.content), mode="r:gz") as tf:
            names = tf.getnames()
        assert "manifest.json" in names
        assert any(n.endswith(".code-workspace") for n in names)

    def test_place_creates_pending_edge_placements(self, client):
        squad = self._squad_with_seat(client)
        cap = client.post("/api/v1/capsules", json={"squad": squad}, headers=H).json()
        r = client.post(
            f"/api/v1/capsules/{cap['id']}/place", json={"machine": "box-1"}, headers=H
        )
        assert r.status_code == 201
        placements = r.json()["placements"]
        assert len(placements) == 1
        got = client.get(f"/api/v1/placements/{placements[0]}", headers=H).json()
        assert got["status"] == "pending-edge"

    def test_place_on_unknown_machine_404(self, client):
        squad = self._squad_with_seat(client)
        cap = client.post("/api/v1/capsules", json={"squad": squad}, headers=H).json()
        r = client.post(
            f"/api/v1/capsules/{cap['id']}/place", json={"machine": "ghost"}, headers=H
        )
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Workspace registry — never lose track of a workspace
# ---------------------------------------------------------------------------


class TestWorkspaceRegistry:
    def _report(self, client, token, workspaces, extra=None):
        body = {"seats": [], "workspaces": workspaces}
        body.update(extra or {})
        r = client.post(
            "/api/v1/machines/box-1/status",
            json=body,
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200, r.text

    def test_discovered_workspaces_persist(self, client):
        token = _machine(client)["token"]
        self._report(
            client, token, [{"path": "/home/x/Projects/alpha.code-workspace", "folders": 3}]
        )
        reg = client.get("/api/v1/workspace-registry", headers=H).json()
        found = [d for d in reg["discovered"] if d["path"].endswith("alpha.code-workspace")]
        assert len(found) == 1
        assert found[0]["machine"] == "box-1"
        assert found[0]["folders"] == 3

    def test_report_is_a_snapshot_not_an_append(self, client):
        # A deleted workspace file must disappear from the registry at the
        # next report — a registry that only accretes lies by staleness.
        token = _machine(client)["token"]
        self._report(client, token, [{"path": "/p/one.code-workspace", "folders": 1}])
        self._report(client, token, [{"path": "/p/two.code-workspace", "folders": 1}])
        paths = [d["path"] for d in client.get(
            "/api/v1/workspace-registry", headers=H
        ).json()["discovered"]]
        assert "/p/two.code-workspace" in paths
        assert "/p/one.code-workspace" not in paths

    def test_broken_workspace_file_is_kept_with_error(self, client):
        token = _machine(client)["token"]
        self._report(
            client, token, [{"path": "/p/broken.code-workspace", "error": "bad JSONC"}]
        )
        d = client.get("/api/v1/workspace-registry", headers=H).json()["discovered"]
        assert d[0]["error"] == "bad JSONC"

    def test_board_presence_marks_open_now(self, client):
        token = _machine(client)["token"]
        self._report(
            client,
            token,
            [{"path": "/p/live.code-workspace", "folders": 2}],
            extra={"workspace_open": "/p/live.code-workspace"},
        )
        d = client.get("/api/v1/workspace-registry", headers=H).json()["discovered"]
        assert d[0]["open_now"] is True

    def test_presence_survives_next_report_without_ping(self, client):
        # The edge's periodic report must not clobber the board's open
        # signal for paths that still exist.
        token = _machine(client)["token"]
        self._report(
            client,
            token,
            [{"path": "/p/live.code-workspace", "folders": 2}],
            extra={"workspace_open": "/p/live.code-workspace"},
        )
        self._report(client, token, [{"path": "/p/live.code-workspace", "folders": 2}])
        d = client.get("/api/v1/workspace-registry", headers=H).json()["discovered"]
        assert d[0]["open_now"] is True

    def test_drift_annotations_both_directions(self, client):
        token = _machine(client)["token"]
        # Registered AND on disk:
        client.post(
            "/api/v1/workspaces",
            json={"name": "alpha", "machine": "box-1", "listings": []},
            headers=H,
        )
        # Registered but NOT on disk:
        client.post(
            "/api/v1/workspaces",
            json={"name": "ghost", "machine": "box-1", "listings": []},
            headers=H,
        )
        self._report(
            client, token, [{"path": "/p/alpha.code-workspace", "folders": 1},
                            {"path": "/p/feral.code-workspace", "folders": 1}]
        )
        reg = client.get("/api/v1/workspace-registry", headers=H).json()
        defs = {w["name"]: w for w in reg["definitions"]}
        assert defs["alpha"]["on_disk"] is True
        assert defs["ghost"]["on_disk"] is False
        disc = {d["path"].rsplit("/", 1)[-1]: d for d in reg["discovered"]}
        assert disc["alpha.code-workspace"]["registered"] is True
        # Feral: exists on disk, hub never told — the losing-track case,
        # now visible instead of silent.
        assert disc["feral.code-workspace"]["registered"] is False


# ---------------------------------------------------------------------------
# Placements — the edge boundary
# ---------------------------------------------------------------------------


class TestPlacements:
    def _placed(self, client) -> str:
        _machine(client)
        seat = _seat(client)
        r = client.post(
            "/api/v1/placements",
            json={"seat": seat["identity"], "machine": "box-1", "substrate": "worktree"},
            headers=H,
        )
        assert r.status_code == 201, r.text
        return r.json()["id"]

    def test_create_is_pending_edge(self, client):
        pid = self._placed(client)
        got = client.get(f"/api/v1/placements/{pid}", headers=H).json()
        assert got["status"] == "pending-edge"
        assert got["desired"] == "running"

    def test_invalid_substrate_422(self, client):
        _machine(client)
        seat = _seat(client)
        r = client.post(
            "/api/v1/placements",
            json={"seat": seat["identity"], "machine": "box-1", "substrate": "zeppelin"},
            headers=H,
        )
        assert r.status_code == 422

    def test_observed_matching_desired_converges(self, client):
        pid = self._placed(client)
        r = client.post(
            f"/api/v1/placements/{pid}/observed",
            json={"state": "running", "enumeration": {"pid": 1234}},
            headers=H,
        )
        assert r.status_code == 200
        assert (
            client.get(f"/api/v1/placements/{pid}", headers=H).json()["status"]
            == "converged"
        )

    def test_observed_mismatch_diverges(self, client):
        pid = self._placed(client)
        client.post(
            f"/api/v1/placements/{pid}/observed",
            json={"state": "stopped", "enumeration": {}},
            headers=H,
        )
        assert (
            client.get(f"/api/v1/placements/{pid}", headers=H).json()["status"]
            == "diverged"
        )

    def test_patch_desired_resets_to_pending(self, client):
        pid = self._placed(client)
        client.post(
            f"/api/v1/placements/{pid}/observed",
            json={"state": "running", "enumeration": {}},
            headers=H,
        )
        r = client.patch(
            f"/api/v1/placements/{pid}", json={"desired": "stopped"}, headers=H
        )
        assert r.status_code == 200
        # Desired moved past what was last observed: convergence is no longer
        # claimable until the edge reports again.
        assert (
            client.get(f"/api/v1/placements/{pid}", headers=H).json()["status"]
            == "diverged"
        )

    # -- `ran`: the headless terminal ask — run once, EVER ------------------

    def _ran(self, client) -> str:
        pid = self._placed(client)
        r = client.patch(
            f"/api/v1/placements/{pid}", json={"desired": "ran"}, headers=H
        )
        assert r.status_code == 200, r.text
        return pid

    def _observe(self, client, pid, state):
        r = client.post(
            f"/api/v1/placements/{pid}/observed",
            json={"state": state, "enumeration": {"exit_code": 0}},
            headers=H,
        )
        assert r.status_code == 200, r.text
        return client.get(f"/api/v1/placements/{pid}", headers=H).json()

    def test_ran_completed_converges(self, client):
        # No enumeration ever literally reads "ran" — the edge reports what
        # it SAW, and exit-0 `completed` is what satisfies the ask.
        pid = self._ran(client)
        assert self._observe(client, pid, "completed")["status"] == "converged"

    def test_ran_failed_diverges(self, client):
        pid = self._ran(client)
        assert self._observe(client, pid, "failed")["status"] == "diverged"

    def test_ran_running_is_in_flight_not_diverged(self, client):
        # The errand mid-run is a DELAY, not a disagreement; "diverged"
        # would page someone about a job doing exactly what was asked.
        pid = self._ran(client)
        assert self._observe(client, pid, "running")["status"] == "in-flight"

    def test_ran_never_observed_is_pending_edge(self, client):
        pid = self._ran(client)
        got = client.get(f"/api/v1/placements/{pid}", headers=H).json()
        assert got["status"] == "pending-edge"

    def test_reclaimed_still_not_a_desired_value(self, client):
        # Destroy stays behind its own verb (DELETE) — a destroy reachable
        # by typing a word into a state field happens by accident.
        pid = self._placed(client)
        r = client.patch(
            f"/api/v1/placements/{pid}", json={"desired": "reclaimed"},
            headers=H,
        )
        assert r.status_code == 422

    def test_delete_is_reclaim_with_three_phases(self, client):
        pid = self._placed(client)
        r = client.delete(f"/api/v1/placements/{pid}", headers=H)
        assert r.status_code == 202
        body = r.json()
        # Reclaim = harvest + verify + destroy, in that order, all pending
        # until an edge executes them — never silently "done".
        assert body["reclaim"] == {
            "harvest": "pending",
            "verify": "pending",
            "destroy": "pending",
        }
        got = client.get(f"/api/v1/placements/{pid}", headers=H).json()
        assert got["desired"] == "reclaimed"

    def test_a_REPO_LESS_worktree_seat_is_accepted_when_named(self, client):
        """🔴 Most of the on-demand roster has no git remote (13 of
        dev-vm-1's 15 faculty agents, 2026-08-09), so demanding one meant the
        API could not start the agents most worth starting from a UI.
        `squad add-folder` has always treated a plain folder as a first-class
        agent; the seat contract now agrees."""
        _machine(client)
        r = client.post("/api/v1/seats", json={
            "identity": "mindconnect-box-1", "machine": "box-1",
            "folder": "/home/x/Projects/mindconnect", "class": "faculty",
        }, headers=H)
        assert r.status_code == 201, r.text
        assert r.json()["identity"] == "mindconnect-box-1"
        assert r.json()["repo"] == ""
        # ...and it is placeable, which is the whole point.
        assert client.post("/api/v1/placements", json={
            "seat": "mindconnect-box-1", "machine": "box-1",
            "substrate": "worktree",
        }, headers=H).status_code == 201

    def test_a_repo_less_seat_with_NO_identity_is_refused(self, client):
        """The control. repo is the source of a derived NAME — drop both and
        there is nothing to call the seat, so this must still 422 rather than
        invent one from the folder's basename."""
        _machine(client)
        r = client.post("/api/v1/seats", json={
            "machine": "box-1", "folder": "/home/x/Projects/mindconnect",
        }, headers=H)
        assert r.status_code == 422
        assert "repo" in r.json()["detail"]

    def test_folder_is_still_required_for_a_worktree_seat(self, client):
        """Relaxing repo must not relax everything: with no image and no
        folder there is nothing on disk to enrol."""
        _machine(client)
        r = client.post("/api/v1/seats", json={
            "identity": "nowhere-box-1", "machine": "box-1",
        }, headers=H)
        assert r.status_code == 422
        assert "folder" in r.json()["detail"]

    def test_purge_FORGETS_the_row_without_asking_for_a_reclaim(self, client):
        """🔴 Reclaim and unplace were sharing one verb. DELETE meant harvest+
        verify+DESTROY — for a worktree seat, `squad rm`, which unenrols the
        agent and opts its repo out of the hub. So the only way to stop the
        hub scheduling a seat was to demolish the agent behind it, and a row
        written against a real roster agent could never be tidied away
        (measured on production 2026-08-09).

        Purge asks the edge for NOTHING: the row is the whole of the hub's
        contribution, and `plan()` only acts on placements it is served.
        """
        pid = self._placed(client)
        r = client.delete(f"/api/v1/placements/{pid}?purge=true", headers=H)
        assert r.status_code == 200, r.text
        assert r.json()["purged"] is True
        # GONE, not tombstoned as reclaimed — a reclaimed row would still be
        # served to the edge, which is the destroy this exists to avoid.
        assert client.get(
            f"/api/v1/placements/{pid}", headers=H).status_code == 404
        assert not [p for p in client.get(
            "/api/v1/placements", headers=H).json()["placements"]
            if p["id"] == pid]

    def test_purge_is_OPT_IN_so_a_plain_delete_still_destroys(self, client):
        """The control: if purge were the default, every existing DELETE in
        the fleet would silently stop reclaiming and leak containers."""
        pid = self._placed(client)
        assert client.delete(
            f"/api/v1/placements/{pid}", headers=H).status_code == 202
        got = client.get(f"/api/v1/placements/{pid}", headers=H).json()
        assert got["desired"] == "reclaimed"
        # ...and a value that is not exactly "true" must not purge either.
        seat2 = _seat(client, name="widget-2")
        pid2 = client.post(
            "/api/v1/placements",
            json={"seat": seat2["identity"], "machine": "box-1",
                  "substrate": "worktree"},
            headers=H,
        ).json()["id"]
        assert client.delete(
            f"/api/v1/placements/{pid2}?purge=1", headers=H).status_code == 202
        assert client.get(
            f"/api/v1/placements/{pid2}", headers=H).status_code == 200

    def test_purge_needs_the_operator_token(self, client):
        """A machine principal must not be able to drop its own policy —
        that would let an edge quietly stop being managed."""
        pid = self._placed(client)
        token = _machine(client, "box-purge")["token"]
        r = client.delete(f"/api/v1/placements/{pid}?purge=true",
                          headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 403
        assert client.get(
            f"/api/v1/placements/{pid}", headers=H).status_code == 200

    def test_machine_token_reports_own_placement_only(self, client):
        token = _machine(client)["token"]
        foreign = _machine(client, "box-other")["token"]
        seat = _seat(client)
        pid = client.post(
            "/api/v1/placements",
            json={"seat": seat["identity"], "machine": "box-1", "substrate": "worktree"},
            headers=H,
        ).json()["id"]
        ok = client.post(
            f"/api/v1/placements/{pid}/observed",
            json={"state": "running", "enumeration": {}},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert ok.status_code == 200
        denied = client.post(
            f"/api/v1/placements/{pid}/observed",
            json={"state": "running", "enumeration": {}},
            headers={"Authorization": f"Bearer {foreign}"},
        )
        assert denied.status_code == 403


class TestImageUnitValidation:
    """An image unit has no worktree and no git remote — demanding either
    forces an operator to invent a field the roster then carries forever.

    ⚠️ This is the seats-500 lesson repeating and being closed properly: the
    CLI's relaxation shipped first, every CLI test passed against a FakeApi,
    and the LIVE call still 422'd because the SERVER kept the old rule.
    A fake at the client boundary tests the client.
    """

    def test_an_image_unit_with_an_identity_needs_no_repo(self, client):
        r = client.post(
            "/api/v1/seats",
            json={"identity": "web-1", "machine": "box-i1",
                  "spec": {"image": "nginx:alpine"}},
            headers=H,
        )
        assert r.status_code == 201, r.text
        assert r.json()["identity"] == "web-1"

    def test_an_image_unit_without_an_identity_still_needs_a_repo(self, client):
        """Not pedantry: the identity is DERIVED from the repo name when it
        is not given, so with neither there is nothing to call the seat."""
        r = client.post(
            "/api/v1/seats",
            json={"machine": "box-i2", "spec": {"image": "nginx:alpine"}},
            headers=H,
        )
        assert r.status_code == 422
        assert "repo" in r.json()["detail"]

    def test_a_worktree_unit_still_needs_repo_and_folder(self, client):
        r = client.post(
            "/api/v1/seats",
            json={"identity": "w-1", "machine": "box-i3"},
            headers=H,
        )
        assert r.status_code == 422

    def test_an_image_unit_never_needs_a_folder(self, client):
        r = client.post(
            "/api/v1/seats",
            json={"identity": "web-2", "machine": "box-i4",
                  "spec": {"image": "redis:7"}},
            headers=H,
        )
        assert r.status_code == 201, r.text


# ---- machines report their roster so the board can attribute exactly -------

def _tok(client, name):
    return {"Authorization": f"Bearer {_machine(client, name)['token']}"}


def test_a_machine_reports_its_roster_and_it_comes_back(client):
    h = _tok(client, "box-r1")
    client.post("/api/v1/machines/box-r1/status",
                json={"agents": [{"agent": "pm-box", "worktree": "/code/pm"}]},
                headers=h)
    got = client.get("/api/v1/machines", headers=H).json()
    # Exact shape on purpose — this is a wire contract, and a field appearing
    # here unannounced is how a board starts reading something nobody sent.
    # `comms` defaults false for an edge that does not send it; `running` is
    # ABSENT rather than false, because unreadable liveness is not "down".
    assert got["agents"]["box-r1"] == [
        {"agent": "pm-box", "worktree": "/code/pm", "comms": False}
    ]


def test_liveness_and_comms_survive_the_round_trip(client):
    h = _tok(client, "box-r1b")
    client.post("/api/v1/machines/box-r1b/status",
                json={"agents": [
                    {"agent": "up-box", "worktree": "/u",
                     "comms": True, "running": True},
                    {"agent": "down-box", "worktree": "/d",
                     "comms": True, "running": False},
                ]}, headers=h)
    got = client.get("/api/v1/machines", headers=H).json()["agents"]["box-r1b"]
    by = {a["agent"]: a for a in got}
    assert by["up-box"]["running"] is True and by["up-box"]["comms"] is True
    assert by["down-box"]["running"] is False


def test_unreadable_liveness_comes_back_ABSENT_not_false(client):
    """NULL means the edge could not read tmux. Returning `false` would let a
    board draw a whole box as stopped and clear every warning on it — the
    false calm the tri-state exists to prevent."""
    h = _tok(client, "box-r1c")
    client.post("/api/v1/machines/box-r1c/status",
                json={"agents": [{"agent": "unknown-box", "worktree": "/x",
                                  "comms": True}]}, headers=h)
    got = client.get("/api/v1/machines", headers=H).json()["agents"]["box-r1c"]
    assert "running" not in got[0], got[0]


def test_the_roster_is_a_SNAPSHOT_so_a_retired_agent_leaves(client):
    """An accreting roster would keep attributing rows to folders the machine
    no longer has — the same reason discovered workspaces replace rather than
    accumulate."""
    h = _tok(client, "box-r2")
    client.post("/api/v1/machines/box-r2/status",
                json={"agents": [{"agent": "a", "worktree": "/a"},
                                 {"agent": "b", "worktree": "/b"}]}, headers=h)
    client.post("/api/v1/machines/box-r2/status",
                json={"agents": [{"agent": "a", "worktree": "/a"}]}, headers=h)
    got = client.get("/api/v1/machines", headers=H).json()
    assert [a["agent"] for a in got["agents"]["box-r2"]] == ["a"]


def test_a_status_push_WITHOUT_agents_leaves_the_roster_alone(client):
    """Absent key is "this edge does not report rosters", not "no agents".
    Clearing on absence would make an older edge empty its own machine."""
    h = _tok(client, "box-r3")
    client.post("/api/v1/machines/box-r3/status",
                json={"agents": [{"agent": "a", "worktree": "/a"}]}, headers=h)
    client.post("/api/v1/machines/box-r3/status",
                json={"workspaces": []}, headers=h)
    got = client.get("/api/v1/machines", headers=H).json()
    assert [a["agent"] for a in got["agents"]["box-r3"]] == ["a"]


def test_an_empty_agents_LIST_does_clear_it(client):
    """`[]` is a real report — the machine has no agents — and differs from
    the key being absent."""
    h = _tok(client, "box-r4")
    client.post("/api/v1/machines/box-r4/status",
                json={"agents": [{"agent": "a", "worktree": "/a"}]}, headers=h)
    client.post("/api/v1/machines/box-r4/status", json={"agents": []}, headers=h)
    got = client.get("/api/v1/machines", headers=H).json()
    assert "box-r4" not in got["agents"]


def test_a_machine_may_not_report_another_machines_roster(client):
    _machine(client, "box-r6")
    r = client.post("/api/v1/machines/box-r6/status",
                    json={"agents": [{"agent": "x", "worktree": "/x"}]},
                    headers=_tok(client, "box-r5"))
    assert r.status_code == 403


def test_a_reported_reclaim_stops_saying_pending(client):
    """The three steps were written `pending` on DELETE and never written
    again, so a finished reclaim described itself as unstarted forever — two
    seats showed mid-harvest a day after their containers were gone."""
    client.post("/api/v1/machines", json={"name": "boxr"}, headers=H)
    client.post("/api/v1/seats", json={"identity": "s-boxr", "repo": "r",
                                       "machine": "boxr", "folder": "/f"},
                headers=H)
    pid = client.post("/api/v1/placements",
                      json={"seat": "s-boxr", "machine": "boxr",
                            "substrate": "docker"},
                      headers=H).json()["id"]
    client.delete(f"/api/v1/placements/{pid}", headers=H)
    before = client.get("/api/v1/placements", headers=H).json()
    mine = [p for p in before["placements"] if p["id"] == pid][0]
    assert mine["reclaim"]["destroy"] == "pending"

    client.post(f"/api/v1/placements/{pid}/observed",
                json={"state": "reclaimed",
                      "enumeration": {"container": "s-boxr", "exists": False}},
                headers=H)
    after = client.get("/api/v1/placements", headers=H).json()
    mine = [p for p in after["placements"] if p["id"] == pid][0]
    assert mine["reclaim"] == {"harvest": "done", "verify": "done",
                               "destroy": "done"}


def test_a_reclaim_is_not_marked_done_by_a_lesser_observation(client):
    """`stopped` is not `reclaimed`. Only the edge's absence-derived verdict
    closes a reclaim; anything weaker would let a container that merely went
    down be recorded as harvested and destroyed."""
    client.post("/api/v1/machines", json={"name": "boxs"}, headers=H)
    client.post("/api/v1/seats", json={"identity": "s-boxs", "repo": "r",
                                       "machine": "boxs", "folder": "/f"},
                headers=H)
    pid = client.post("/api/v1/placements",
                      json={"seat": "s-boxs", "machine": "boxs",
                            "substrate": "docker"},
                      headers=H).json()["id"]
    client.delete(f"/api/v1/placements/{pid}", headers=H)
    client.post(f"/api/v1/placements/{pid}/observed",
                json={"state": "stopped", "enumeration": {"exists": True}},
                headers=H)
    got = client.get("/api/v1/placements", headers=H).json()
    mine = [p for p in got["placements"] if p["id"] == pid][0]
    assert mine["reclaim"]["harvest"] == "pending"
