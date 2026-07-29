"""Gate for `mcp-hub edge apply` — the interim edge realizer (task #1).

Scope of this gate: the realizer's BRAIN — planning, discovery, reporting —
as pure functions with injected inputs. The squad-command execution path is
exercised through a recording runner, never a real shell: a test that can
reach a live roster is a test that can truncate one (2026-07-27, measured).

Contract under test (docs/hub-api-v1.md, edge boundary):
- plan(): desired-vs-local diff → ordered actions; worktree substrate only,
  docker placements are SKIPPED loudly (reported, never guessed at).
- discover_workspaces(): enumerates .code-workspace files — the operator's
  "never lose track of workspaces" requirement; report what IS, not what's
  registered.
- observed_report(): built from enumeration (tmux/session facts), never from
  the desired record — nothing may assert its own state.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mcp_hub.edge import (
    HubAPI,
    SquadExecutor,
    discover_workspaces,
    edge_apply,
    observed_report,
    plan,
    seed_first_launch,
)


def _placement(
    pid="pl-1",
    seat="widget-box-1",
    substrate="worktree",
    desired="running",
    **kw,
) -> dict:
    return {
        "id": pid,
        "seat": seat,
        "machine": "box-1",
        "substrate": substrate,
        "desired": desired,
        "status": "pending-edge",
        **kw,
    }


class TestPlan:
    def test_missing_seat_folder_yields_materialize_then_start(self):
        actions = plan(
            placements=[_placement()],
            local_seats={},  # nothing exists on this box yet
        )
        assert [a["op"] for a in actions] == ["materialize", "start"]
        assert actions[0]["placement"] == "pl-1"
        assert actions[0]["seat"] == "widget-box-1"
        # A just-materialized seat has NO conversation history: its first
        # start must be fresh — `--continue` on a virgin seat exits with
        # "No conversation found to continue" (live demo, 2026-07-29).
        assert actions[1]["fresh"] is True

    def test_start_of_existing_seat_is_not_fresh(self):
        actions = plan(
            placements=[_placement()],
            local_seats={"widget-box-1": {"materialized": True, "running": False}},
        )
        assert actions[0]["op"] == "start"
        assert not actions[0].get("fresh")

    def test_existing_running_seat_yields_no_actions(self):
        actions = plan(
            placements=[_placement()],
            local_seats={"widget-box-1": {"materialized": True, "running": True}},
        )
        assert actions == []

    def test_existing_stopped_seat_yields_start_only(self):
        actions = plan(
            placements=[_placement()],
            local_seats={"widget-box-1": {"materialized": True, "running": False}},
        )
        assert [a["op"] for a in actions] == ["start"]

    def test_desired_stopped_running_seat_yields_stop(self):
        actions = plan(
            placements=[_placement(desired="stopped")],
            local_seats={"widget-box-1": {"materialized": True, "running": True}},
        )
        assert [a["op"] for a in actions] == ["stop"]

    def test_reclaimed_yields_harvest_verify_destroy_in_order(self):
        actions = plan(
            placements=[_placement(desired="reclaimed")],
            local_seats={"widget-box-1": {"materialized": True, "running": False}},
        )
        assert [a["op"] for a in actions] == ["harvest", "verify", "destroy"]

    def test_docker_substrate_is_skipped_loudly_not_guessed(self):
        actions = plan(
            placements=[_placement(substrate="docker")],
            local_seats={},
        )
        assert len(actions) == 1
        assert actions[0]["op"] == "skip"
        assert "docker" in actions[0]["reason"]

    def test_converged_placement_produces_nothing(self):
        p = _placement()
        p["status"] = "converged"
        actions = plan(
            placements=[p],
            local_seats={"widget-box-1": {"materialized": True, "running": True}},
        )
        assert actions == []


class TestDiscoverWorkspaces:
    def test_finds_and_types_workspace_files(self, tmp_path: Path):
        (tmp_path / "runtime.code-workspace").write_text(
            json.dumps({"folders": [{"path": "code/a"}, {"path": "code/b"}]})
        )
        (tmp_path / "notes.txt").write_text("not a workspace")
        sub = tmp_path / "Projects"
        sub.mkdir()
        (sub / "general.code-workspace").write_text(json.dumps({"folders": []}))

        found = discover_workspaces([tmp_path, sub])
        by_name = {w["path"].rsplit("/", 1)[-1]: w for w in found}
        assert set(by_name) == {"runtime.code-workspace", "general.code-workspace"}
        assert by_name["runtime.code-workspace"]["folders"] == 2
        assert by_name["general.code-workspace"]["folders"] == 0

    def test_unparseable_file_reported_not_dropped(self, tmp_path: Path):
        # Losing track of a workspace because its JSONC didn't parse is
        # exactly the silent loss the registry exists to prevent.
        (tmp_path / "broken.code-workspace").write_text("{ not json //")
        found = discover_workspaces([tmp_path])
        assert len(found) == 1
        assert found[0]["error"]

    def test_missing_scan_dir_is_fine(self, tmp_path: Path):
        assert discover_workspaces([tmp_path / "nope"]) == []


class TestObservedReport:
    def test_running_state_comes_from_enumeration(self):
        report = observed_report(
            _placement(),
            enumeration={"tmux_session": "widget-box-1", "alive": True},
        )
        assert report["state"] == "running"
        assert report["enumeration"]["tmux_session"] == "widget-box-1"

    def test_dead_session_reports_stopped_even_if_desired_running(self):
        # The report must describe reality, not echo desire — a realizer
        # that reports desired state is the vacuous green of scheduling.
        report = observed_report(
            _placement(desired="running"),
            enumeration={"tmux_session": "widget-box-1", "alive": False},
        )
        assert report["state"] == "stopped"

    def test_empty_enumeration_is_an_error_not_a_pass(self):
        # An assertion over an empty set is a hard error (evidence contract
        # ①): no enumeration means we know nothing, not that it's stopped.
        with pytest.raises(ValueError):
            observed_report(_placement(), enumeration={})


class _RecordingRunner:
    """Stands in for the shell: records commands, simulates squad state.

    `squad start <seat>` makes the seat's tmux session appear; `squad
    stop`/`squad rm` removes it — enough state for the orchestrator's
    re-enumeration to observe the effect of its own actions.
    """

    def __init__(self) -> None:
        self.commands: list[list[str]] = []
        self.sessions: set[str] = set()
        self.enrolled: set[str] = set()

    def __call__(self, cmd: list[str], cwd: str | None = None) -> tuple[int, str]:
        self.commands.append(cmd)
        if cmd[:2] == ["squad", "start"]:
            self.sessions.add(cmd[2])
        elif cmd[:2] == ["squad", "restart"]:
            self.sessions.add(cmd[2])
        elif cmd[:2] == ["squad", "stop"]:
            self.sessions.discard(cmd[2])
        elif cmd[:2] == ["squad", "rm"]:
            self.sessions.discard(cmd[2])
            self.enrolled.discard(cmd[2])
        elif cmd[:2] == ["squad", "add"]:
            # `squad add org/repo` enrolls the derived seat; the fake maps the
            # repo tail to the seat name the tests use (<repo>-box-N is close
            # enough not to matter — enrollment is tracked per demo seat).
            self.enrolled |= {s for s in self.pending_enroll}
        if cmd[:2] == ["squad", "ls"]:
            lines = [
                f"{name} {'up' if name in self.sessions else 'down'} -"
                for name in sorted(self.enrolled)
            ]
            return 0, "\n".join(lines)
        return 0, "ok"

    pending_enroll: set[str] = set()


class TestExecutor:
    def _spec(self):
        return {
            "identity": "widget-box-1",
            "repo": "acme/widget",
            "folder": "/home/x/acme/widget",
            "launch_args": "",
        }

    def test_materialize_uses_squad_add(self):
        r = _RecordingRunner()
        SquadExecutor(r).execute({"op": "materialize", "seat": "widget-box-1"}, self._spec())
        assert ["squad", "add", "acme/widget"] in r.commands

    def test_start_stop_map_to_squad_verbs(self):
        r = _RecordingRunner()
        ex = SquadExecutor(r)
        ex.execute({"op": "start", "seat": "widget-box-1"}, self._spec())
        ex.execute({"op": "stop", "seat": "widget-box-1"}, self._spec())
        assert ["squad", "start", "widget-box-1"] in r.commands
        assert ["squad", "stop", "widget-box-1"] in r.commands

    def test_skip_runs_nothing(self):
        r = _RecordingRunner()
        out = SquadExecutor(r).execute(
            {"op": "skip", "seat": "s", "reason": "docker"}, self._spec()
        )
        assert r.commands == []
        assert out["skipped"]

    def test_fresh_start_uses_restart_fresh(self):
        r = _RecordingRunner()
        SquadExecutor(r).execute(
            {"op": "start", "seat": "widget-box-1", "fresh": True}, self._spec()
        )
        assert ["squad", "restart", "widget-box-1", "--fresh"] in r.commands


class TestSeedFirstLaunch:
    def test_seeds_trust_and_hub_on_fresh_file(self, tmp_path: Path):
        cj = tmp_path / ".claude.json"
        ok = seed_first_launch("/home/x/repo", claude_json=cj)
        assert ok
        data = json.loads(cj.read_text())
        entry = data["projects"]["/home/x/repo"]
        assert entry["hasTrustDialogAccepted"] is True
        assert entry["enabledMcpjsonServers"] == ["hub"]

    def test_preserves_existing_entries(self, tmp_path: Path):
        cj = tmp_path / ".claude.json"
        cj.write_text(json.dumps({"projects": {"/other": {"keep": 1}}, "top": True}))
        seed_first_launch("/home/x/repo", claude_json=cj)
        data = json.loads(cj.read_text())
        assert data["projects"]["/other"] == {"keep": 1}
        assert data["top"] is True

    def test_unparseable_file_fails_open_untouched(self, tmp_path: Path):
        # Transport's rule, inherited: never clobber a real file we can't
        # parse — the operator answers one dialog instead of losing settings.
        cj = tmp_path / ".claude.json"
        cj.write_text("{ definitely not json")
        ok = seed_first_launch("/home/x/repo", claude_json=cj)
        assert not ok
        assert cj.read_text() == "{ definitely not json"

    def test_idempotent(self, tmp_path: Path):
        cj = tmp_path / ".claude.json"
        seed_first_launch("/home/x/repo", claude_json=cj)
        seed_first_launch("/home/x/repo", claude_json=cj)
        entry = json.loads(cj.read_text())["projects"]["/home/x/repo"]
        assert entry["enabledMcpjsonServers"] == ["hub"]  # no duplicate


class TestEdgeApplyEndToEnd:
    """The full loop against the REAL API (in-process), fake shell only."""

    @pytest.fixture()
    def hub(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        from starlette.testclient import TestClient

        from mcp_hub.server import create_server

        monkeypatch.setenv("MCP_HUB_API_TOKEN", "op-token")
        server = create_server(db_path=tmp_path / "edge-hub.db")
        with TestClient(server.streamable_http_app()) as c:
            yield c

    def test_instruction_to_converged(self, hub, tmp_path: Path):
        op = {"Authorization": "Bearer op-token"}
        machine_token = hub.post(
            "/api/v1/machines",
            json={"name": "box-1", "os": "linux", "capabilities": {}},
            headers=op,
        ).json()["token"]
        hub.post(
            "/api/v1/seats",
            json={"repo": "acme/widget", "machine": "box-1", "folder": "/home/x/w"},
            headers=op,
        )
        pid = hub.post(
            "/api/v1/placements",
            json={"seat": "widget-box-1", "machine": "box-1", "substrate": "worktree"},
            headers=op,
        ).json()["id"]

        (tmp_path / "demo.code-workspace").write_text(json.dumps({"folders": []}))
        runner = _RecordingRunner()
        runner.pending_enroll = {"widget-box-1"}
        seeded: list[str] = []
        api = HubAPI(client=hub, token=machine_token)
        summary = edge_apply(
            api,
            machine="box-1",
            runner=runner,
            scan_dirs=[tmp_path],
            seeder=seeded.append,
        )

        # It acted: materialize + FRESH start went through the squad verbs,
        # and the seat folder was pre-authorized before launch.
        assert ["squad", "add", "acme/widget"] in runner.commands
        assert ["squad", "restart", "widget-box-1", "--fresh"] in runner.commands
        assert seeded == ["/home/x/w"]
        # It reported truthfully: the hub now shows the placement converged,
        # from enumeration the runner itself produced.
        got = hub.get(f"/api/v1/placements/{pid}", headers=op).json()
        assert got["status"] == "converged"
        # It kept the registry honest: the workspace file was discovered and
        # pushed with machine status.
        assert summary["workspaces_reported"] == 1
        assert hub.get("/api/v1/machines/box-1", headers=op).json()["last_seen"]

    def test_second_apply_is_idempotent(self, hub, tmp_path: Path):
        op = {"Authorization": "Bearer op-token"}
        machine_token = hub.post(
            "/api/v1/machines",
            json={"name": "box-2", "os": "linux", "capabilities": {}},
            headers=op,
        ).json()["token"]
        hub.post(
            "/api/v1/seats",
            json={"repo": "acme/widget", "machine": "box-2", "folder": "/home/x/w",
                  "identity": "widget-box-2"},
            headers=op,
        )
        hub.post(
            "/api/v1/placements",
            json={"seat": "widget-box-2", "machine": "box-2", "substrate": "worktree"},
            headers=op,
        )
        runner = _RecordingRunner()
        runner.pending_enroll = {"widget-box-2"}
        seeded: list[str] = []
        api = HubAPI(client=hub, token=machine_token)
        edge_apply(api, machine="box-2", runner=runner, scan_dirs=[], seeder=seeded.append)
        n_first = len(runner.commands)
        edge_apply(api, machine="box-2", runner=runner, scan_dirs=[], seeder=seeded.append)
        # Second pass: state already converged; only enumeration (`squad ls`)
        # runs — no second materialize/start, nothing mutating.
        assert runner.commands.count(["squad", "restart", "widget-box-2", "--fresh"]) == 1
        mutating = [
            c
            for c in runner.commands[n_first:]
            if c[0] == "squad" and c[1] != "ls"
        ]
        assert mutating == []
