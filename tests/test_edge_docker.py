"""The docker substrate: an edge that eats containers.

The unit this edge manages is a CONTAINER — a squad seat, a web app, an
inference server are the same shape to it. So these tests are mostly about the
edge NOT knowing or caring what is inside the image, and about the two places
where a container differs from a tmux seat in ways that matter:

- ENUMERATION. `docker ps -a`, not `docker ps`: a stopped container still
  exists, and calling it unmaterialized makes the planner create a second one
  under a name that is already taken — failing every pass, forever.
- HARVEST. A web app has no learnings. Running a no-op that LOOKS like it
  preserved something is worse than saying there was nothing to preserve.

The runner is injected everywhere, so nothing here can reach a real docker.
"""

from __future__ import annotations

import pytest

from mcp_hub.edge import (
    DockerExecutor,
    EnumerationFailed,
    HubAPI,
    edge_apply,
    enumerate_docker,
    plan,
)


class Runner:
    """Records commands; answers `docker ps` from a scripted world."""

    def __init__(self, world=None, fail=(), denied=False):
        self.world = dict(world or {})       # name -> state
        self.calls: list[list[str]] = []
        self.fail = set(fail)
        self.denied = denied

    def __call__(self, cmd, cwd=None):
        self.calls.append(list(cmd))
        if cmd[:3] == ["docker", "ps", "-a"]:
            if "ps" in self.fail:
                if self.denied:
                    return 1, ("permission denied while trying to connect to "
                               "the docker API at unix:///var/run/docker.sock")
                return 1, "Cannot connect to the Docker daemon"
            body = "\n".join(f"{n}\t{s}" for n, s in self.world.items())
            return 0, body
        if cmd[:2] == ["squad", "ls"]:
            return 0, ""
        return (1, "boom") if cmd[1] in self.fail else (0, "ok")


SPEC = {
    "identity": "web-box-1",
    "spec": {"image": "nginx:alpine", "ports": ["8080:80"],
             "env": {"TZ": "UTC"}, "volumes": ["/srv/site:/usr/share/nginx/html"]},
}


# ---- enumeration -----------------------------------------------------------

def test_a_stopped_container_still_counts_as_materialized():
    """`docker ps` without -a hides it, the planner would create a second one
    under a name already taken, and the run would fail every pass forever."""
    r = Runner({"web-box-1": "exited"})
    state = enumerate_docker(r, ["web-box-1"])
    assert state["web-box-1"] == {"materialized": True, "running": False}
    assert r.calls[0][:3] == ["docker", "ps", "-a"]


def test_a_running_container_is_both():
    state = enumerate_docker(Runner({"web-box-1": "running"}), ["web-box-1"])
    assert state["web-box-1"] == {"materialized": True, "running": True}


def test_an_absent_container_is_neither():
    state = enumerate_docker(Runner({}), ["web-box-1"])
    assert state["web-box-1"] == {"materialized": False, "running": False}


def test_a_docker_daemon_that_cannot_be_reached_is_a_HARD_error():
    """Same contract as `squad ls`: no evidence means unknown, and unknown
    must be an error rather than an empty set that reads as 'nothing here'."""
    with pytest.raises(EnumerationFailed) as e:
        enumerate_docker(Runner(fail=["ps"]), ["web-box-1"])
    assert "refusing to plan or report" in str(e.value)


def test_other_peoples_containers_are_ignored_not_reported():
    """The machine runs things this fleet did not place. Enumerating them
    would invent placements nobody asked for."""
    state = enumerate_docker(
        Runner({"web-box-1": "running", "postgres": "running"}), ["web-box-1"])
    assert set(state) == {"web-box-1"}


# ---- the create command ----------------------------------------------------

def test_create_names_the_container_after_the_seat():
    """The name IS the enumeration key — without it nothing maps back to a
    placement, and a side table would be a second thing to drift."""
    argv = DockerExecutor.create_argv("web-box-1", SPEC["spec"])
    assert argv[:4] == ["docker", "create", "--name", "web-box-1"]
    assert argv[-1] == "nginx:alpine"


def test_create_carries_ports_env_and_volumes():
    argv = DockerExecutor.create_argv("web-box-1", SPEC["spec"])
    joined = " ".join(argv)
    assert "-p 8080:80" in joined
    assert "-e TZ=UTC" in joined
    assert "-v /srv/site:/usr/share/nginx/html" in joined


def test_restart_policy_is_never_always():
    """Docker restarting a container the hub asked to STOP would make
    `observed` disagree with reality on every pass — the edge decides what
    runs, or the reports are fiction."""
    argv = DockerExecutor.create_argv("web-box-1", SPEC["spec"])
    assert "--restart" in argv
    assert argv[argv.index("--restart") + 1] == "no"
    assert "always" not in argv


def test_a_command_override_lands_after_the_image():
    argv = DockerExecutor.create_argv(
        "job-1", {"image": "busybox", "command": ["sleep", "3600"]})
    assert argv[-3:] == ["busybox", "sleep", "3600"]


# ---- executing -------------------------------------------------------------

def _ex(runner):
    return DockerExecutor(runner)


def test_materialize_creates_but_does_not_start():
    """`desired: stopped` on a seat that does not exist yet must produce a
    container that is NOT running, so the two ops stay separate."""
    r = Runner()
    _ex(r).execute({"op": "materialize", "seat": "web-box-1"}, SPEC)
    assert r.calls[-1][:2] == ["docker", "create"]
    assert not any(c[:2] == ["docker", "start"] for c in r.calls)


def test_materialize_without_an_image_refuses_rather_than_guessing():
    """A wrong image starts the wrong software under the right name, and
    enumeration cannot detect that — `docker ps` would report it healthy."""
    r = Runner()
    out = _ex(r).execute({"op": "materialize", "seat": "x"},
                         {"identity": "x", "spec": {}})
    assert out["skipped"] and "refusing to guess" in out["reason"]
    assert r.calls == []


@pytest.mark.parametrize("op,expect", [
    ("start", ["docker", "start", "web-box-1"]),
    ("stop", ["docker", "stop", "web-box-1"]),
    ("destroy", ["docker", "rm", "-f", "web-box-1"]),
])
def test_the_lifecycle_ops_map_to_docker(op, expect):
    r = Runner()
    _ex(r).execute({"op": op, "seat": "web-box-1"}, SPEC)
    assert r.calls[-1] == expect


def test_harvest_is_skipped_for_a_unit_with_nothing_to_harvest():
    """A web app has no learnings. A no-op that LOOKS like it preserved
    something is worse than saying there was nothing to preserve."""
    r = Runner()
    out = _ex(r).execute({"op": "harvest", "seat": "web-box-1"}, SPEC)
    assert out["skipped"] and "nothing to harvest" in out["reason"]
    assert r.calls == []


def test_harvest_runs_for_a_seat_that_carries_memory():
    """This is the whole agent-vs-service difference, in one field."""
    r = Runner()
    seat = {"identity": "pm-box-1",
            "spec": {"image": "mcp-hub-seat", "memory_volume": "pm-memory"}}
    _ex(r).execute({"op": "harvest", "seat": "pm-box-1"}, seat)
    assert r.calls[-1] == ["docker", "exec", "pm-box-1", "mcp-hub", "memory-export"]


def test_verify_shells_nothing_it_is_re_enumeration():
    r = Runner()
    out = _ex(r).execute({"op": "verify", "seat": "web-box-1"}, SPEC)
    assert r.calls == [] and "re-enumeration" in out["deferred"]


# ---- the whole loop --------------------------------------------------------

class FakeApi:
    def __init__(self, placements):
        self._p = placements
        self.observed: dict[str, dict] = {}
        self.status: dict = {}

    def pull_placements(self, machine):
        return self._p

    def push_observed(self, pid, report):
        self.observed[pid] = report
        return {}

    def push_status(self, machine, payload):
        self.status = payload


def _placement(desired="running", substrate="docker", seat="web-box-1"):
    return {"id": "pl-1", "seat": seat, "machine": "box-1",
            "substrate": substrate, "desired": desired, "seat_spec": SPEC}


def test_a_container_placement_converges_and_is_reported_as_a_container(tmp_path):
    """End to end: nothing exists, desired=running, and the observed record
    must say `container` — calling it a tmux_session would put a false claim
    in the one place the fleet trusts completely."""
    class Live(Runner):
        """Mutates SELF.world — `Runner.__init__` copies the dict it is given,
        so a closure over the original would leave the second enumeration
        seeing an empty world and reporting `stopped` for a container it had
        just started (which is exactly what this fixture did first time)."""

        def __call__(self, cmd, cwd=None):
            rc, out = super().__call__(cmd, cwd)
            if cmd[:2] == ["docker", "create"]:
                self.world[cmd[3]] = "created"
            elif cmd[:2] == ["docker", "start"]:
                self.world[cmd[2]] = "running"
            return rc, out

    r = Live({})
    api = FakeApi([_placement()])
    summary = edge_apply(api, "box-1", r, [tmp_path], seeder=lambda f: True)

    assert summary["placements"] == 1
    assert [c[:2] for c in r.calls if c[0] == "docker" and c[1] != "ps"] == \
        [["docker", "create"], ["docker", "start"]]
    report = api.observed["pl-1"]
    assert report["state"] == "running"
    assert report["enumeration"]["container"] == "web-box-1"
    assert "tmux_session" not in report["enumeration"]


def test_a_docker_only_machine_never_runs_squad_ls(tmp_path):
    """A container host may have no squad installed at all; running it there
    would turn a healthy machine into an EnumerationFailed every 2 minutes."""
    r = Runner({"web-box-1": "running"})
    edge_apply(FakeApi([_placement()]), "box-1", r, [tmp_path],
               seeder=lambda f: True)
    assert not any(c[:2] == ["squad", "ls"] for c in r.calls)


def test_both_substrates_are_realized_in_one_pass(tmp_path):
    """A box running agent seats in tmux AND a web app in a container is the
    ordinary case, not the exotic one."""
    r = Runner({"web-box-1": "running"})
    api = FakeApi([
        _placement(),
        {"id": "pl-2", "seat": "pm-box-1", "machine": "box-1",
         "substrate": "worktree", "desired": "running",
         "seat_spec": {"identity": "pm-box-1", "folder": "/srv/pm"}},
    ])
    edge_apply(api, "box-1", r, [tmp_path], seeder=lambda f: True)
    assert any(c[:2] == ["squad", "ls"] for c in r.calls)      # worktree side
    assert any(c[:3] == ["docker", "ps", "-a"] for c in r.calls)  # docker side
    # The worktree seat is absent from `squad ls`, so it materializes there —
    # and via SQUAD, not docker.
    assert any(c[:2] == ["squad", "add"] for c in r.calls)
    assert api.observed["pl-1"]["enumeration"].get("container")
    assert api.observed["pl-2"]["enumeration"].get("tmux_session")


def test_a_stopped_container_asked_to_stop_produces_no_action(tmp_path):
    r = Runner({"web-box-1": "exited"})
    edge_apply(FakeApi([_placement(desired="stopped")]), "box-1", r, [tmp_path],
               seeder=lambda f: True)
    assert not any(c[:2] == ["docker", "stop"] for c in r.calls)


def test_reclaim_harvests_before_it_destroys():
    """Ordering, not presence: destroying first would make the harvest a
    report about something that no longer exists."""
    actions = plan([_placement(desired="reclaimed")], {})
    assert [a["op"] for a in actions] == ["harvest", "verify", "destroy"]


def test_the_hub_api_client_targets_the_machine_placement_route():
    seen = {}

    class C:
        def get(self, path, headers=None):
            seen["path"] = path
            return type("R", (), {"raise_for_status": lambda s: None,
                                  "json": lambda s: {"placements": []}})()

    HubAPI(client=C(), token="t").pull_placements("box-9")
    assert seen["path"] == "/api/v1/machines/box-9/placements"


# ---- the API endpoint, not a fake ------------------------------------------

class TestSeatsEndpointAcceptsContainers:
    """These go through the REAL route. The CLI tests above use a fake API and
    could not see that the INSERT still demanded `folder` after validation had
    stopped requiring it — a 500 on every container seat, found by curl."""

    @staticmethod
    def _client(tmp_path, monkeypatch):
        monkeypatch.setenv("MCP_HUB_API_TOKEN", "t")
        from starlette.testclient import TestClient

        from mcp_hub.server import create_server
        c = TestClient(create_server(db_path=tmp_path / "hub.db")
                       .streamable_http_app())
        c.__enter__()
        c.post("/api/v1/machines", headers={"Authorization": "Bearer t"},
               json={"name": "box", "os": "linux"})
        return c

    def test_a_container_seat_needs_no_folder(self, tmp_path, monkeypatch):
        c = self._client(tmp_path, monkeypatch)
        r = c.post("/api/v1/seats", headers={"Authorization": "Bearer t"},
                   json={"repo": "o/x", "machine": "box", "identity": "web-box",
                         "spec": {"image": "nginx:alpine"}})
        assert r.status_code == 201, r.text
        assert r.json()["spec"]["image"] == "nginx:alpine"

    def test_a_worktree_seat_still_needs_one(self, tmp_path, monkeypatch):
        c = self._client(tmp_path, monkeypatch)
        r = c.post("/api/v1/seats", headers={"Authorization": "Bearer t"},
                   json={"repo": "o/x", "machine": "box"})
        assert r.status_code == 422 and "folder" in r.text

    def test_the_spec_survives_the_round_trip_to_the_edge(self, tmp_path,
                                                          monkeypatch):
        """The edge reads seat_spec from the placement pull; a spec that does
        not survive that trip means the executor has no image to run."""
        c = self._client(tmp_path, monkeypatch)
        h = {"Authorization": "Bearer t"}
        c.post("/api/v1/seats", headers=h,
               json={"repo": "o/x", "machine": "box", "identity": "web-box",
                     "spec": {"image": "nginx:alpine", "ports": ["80:80"]}})
        c.post("/api/v1/placements", headers=h,
               json={"seat": "web-box", "machine": "box", "substrate": "docker"})
        pulled = c.get("/api/v1/machines/box/placements", headers=h).json()
        spec = pulled["placements"][0]["seat_spec"]["spec"]
        assert spec == {"image": "nginx:alpine", "ports": ["80:80"]}

    def test_a_non_object_spec_is_refused_not_stored(self, tmp_path, monkeypatch):
        c = self._client(tmp_path, monkeypatch)
        r = c.post("/api/v1/seats", headers={"Authorization": "Bearer t"},
                   json={"repo": "o/x", "machine": "box", "spec": "nginx"})
        assert r.status_code == 422

    def test_an_old_database_gains_the_column_by_migration(self, tmp_path,
                                                           monkeypatch):
        """CREATE TABLE IF NOT EXISTS does nothing for a deployed hub, so the
        column has to arrive by ALTER or every live hub keeps the old shape —
        which is a 500 on read, not a graceful degrade."""
        import sqlite3

        dbp = tmp_path / "old.db"
        conn = sqlite3.connect(dbp)
        conn.executescript(
            "CREATE TABLE api_seats (identity TEXT PRIMARY KEY, repo TEXT NOT"
            " NULL, machine TEXT NOT NULL, folder TEXT NOT NULL, launch_args"
            " TEXT NOT NULL DEFAULT '', class TEXT NOT NULL DEFAULT 'squad',"
            " cloned_from TEXT NOT NULL DEFAULT '', archived INTEGER NOT NULL"
            " DEFAULT 0, created REAL NOT NULL);"
        )
        conn.commit()
        conn.close()

        monkeypatch.setenv("MCP_HUB_API_TOKEN", "t")
        from starlette.testclient import TestClient

        from mcp_hub.server import create_server
        with TestClient(create_server(db_path=dbp).streamable_http_app()) as c:
            h = {"Authorization": "Bearer t"}
            c.post("/api/v1/machines", headers=h,
                   json={"name": "box", "os": "linux"})
            r = c.post("/api/v1/seats", headers=h,
                       json={"repo": "o/x", "machine": "box",
                             "identity": "web-box",
                             "spec": {"image": "busybox"}})
            assert r.status_code == 201, r.text
        cols = [x[1] for x in sqlite3.connect(dbp)
                .execute("PRAGMA table_info(api_seats)")]
        assert "spec" in cols


# ---- the error that costs twenty minutes -----------------------------------

DENIED = ("permission denied while trying to connect to the docker API at "
          "unix:///var/run/docker.sock")


def test_a_socket_permission_error_names_the_stale_group_manager():
    """Observed live: `docker ps` worked over ssh and failed in the systemd
    unit, same box, same user. The user joined the `docker` group AFTER the
    --user manager started, so every service it spawns keeps the old
    supplementary groups while interactive logins get fresh ones."""
    with pytest.raises(EnumerationFailed) as e:
        enumerate_docker(Runner(fail=["ps"], denied=True), ["web-box-1"])
    msg = str(e.value)
    assert "systemd --user" in msg
    assert "supplementary groups" in msg
    assert "getent group docker" in msg          # the check, not just the cause
    # …and it warns that the obvious fix is destructive on a box running seats
    assert "terminate-user" in msg and "kill" in msg


def test_an_unrelated_docker_failure_gets_no_group_lecture():
    """A hint that fires for everything is noise, and would send someone
    chasing groups when the daemon is simply not running."""
    with pytest.raises(EnumerationFailed) as e:
        enumerate_docker(Runner(fail=["ps"]), ["web-box-1"])
    msg = str(e.value)
    assert "supplementary groups" not in msg
    assert "refusing to plan or report" in msg   # the refusal still stands
