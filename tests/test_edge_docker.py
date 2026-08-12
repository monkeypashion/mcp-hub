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

import json

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

    def __init__(self, world=None, fail=(), denied=False, images=None,
                 tags=None):
        self.world = dict(world or {})       # name -> state
        self.images = dict(images or {})     # name -> image id it RUNS
        self.tags = dict(tags or {})         # image ref -> current id
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
        if cmd[:2] == ["docker", "inspect"]:
            if "inspect" in self.fail:
                return 1, "No such object"
            names = [c for c in cmd[2:] if not c.startswith("--")
                     and c not in ("{{.Name}} {{.Image}}",)]
            return 0, "\n".join(
                f"/{n} {self.images.get(n, 'sha256:unknown')}" for n in names
            )
        if cmd[:3] == ["docker", "image", "inspect"]:
            ref = cmd[3]
            if ref not in self.tags:
                return 1, f"Error: No such image: {ref}"
            return 0, self.tags[ref]
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
    assert state["web-box-1"] == {"materialized": True, "running": False,
                                  "image": "sha256:unknown"}
    assert r.calls[0][:3] == ["docker", "ps", "-a"]


def test_a_running_container_is_both():
    state = enumerate_docker(Runner({"web-box-1": "running"}), ["web-box-1"])
    assert state["web-box-1"] == {"materialized": True, "running": True,
                                  "image": "sha256:unknown"}


def test_an_absent_container_is_neither():
    state = enumerate_docker(Runner({}), ["web-box-1"])
    assert state["web-box-1"] == {"materialized": False, "running": False,
                                  "image": None}


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
    # By PRESENCE, not position: materialize now also enrols the container in
    # the squad roster afterwards (so /voice authorises it), and an
    # index-based assertion breaks on a change that is not a defect.
    assert any(c[:2] == ["docker", "create"] for c in r.calls)
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
    # MUTATING calls, in order: create then start, never `docker run` (which
    # would make a `desired: stopped` seat start anyway). Read-only calls
    # (ps/inspect/image inspect) are enumeration and excluded by name — the
    # assertion is about what this pass CHANGED, not how much it looked.
    mutating = [c[:2] for c in r.calls
                if c[0] == "docker" and c[1] not in ("ps", "inspect", "image")]
    assert mutating == [["docker", "create"], ["docker", "start"]]
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
    #
    # ⚠️ This asserted `["squad", "add"]` until 2026-08-09 — and the seat in
    # this very fixture has NO repo, so what it actually pinned was
    # `squad add ""`, a command that could only ever fail. A repo-less seat
    # now materializes with the verb built for it, carrying the hub's ASSIGNED
    # identity so the `squad start <seat>` that follows finds the name it
    # expects in the roster.
    assert ["squad", "add-folder", "/srv/pm", "--name", "pm-box-1"] in r.calls
    assert not any(c[:2] == ["squad", "add"] for c in r.calls)
    assert api.observed["pl-1"]["enumeration"].get("container")
    assert api.observed["pl-2"]["enumeration"].get("tmux_session")


def test_a_stopped_container_asked_to_stop_produces_no_action(tmp_path):
    r = Runner({"web-box-1": "exited"})
    edge_apply(FakeApi([_placement(desired="stopped")]), "box-1", r, [tmp_path],
               seeder=lambda f: True)
    assert not any(c[:2] == ["docker", "stop"] for c in r.calls)


def test_reclaim_harvests_before_it_destroys():
    """Ordering, not presence: destroying first would make the harvest a
    report about something that no longer exists.

    The seat is explicitly MATERIALIZED here. This test passed `{}` when it
    was written — an absent seat — which read as "reclaim always plans three
    steps" and was the assumption behind the loop below: two placements were
    still being harvested, verified and destroyed every two minutes long after
    their containers were gone. The subject was always the ORDER, so the
    fixture now says so instead of relying on a state it did not mean.
    """
    actions = plan([_placement(desired="reclaimed")],
                   {"web-box-1": {"materialized": True, "running": True}})
    assert [a["op"] for a in actions] == ["harvest", "verify", "destroy"]


def test_a_FINISHED_reclaim_plans_nothing():
    """The completion of a reclaim IS an absence, so a seat that is already
    gone needs no work. Enumeration RAISES when it cannot see the substrate,
    so "not materialized" means we looked and it was not there."""
    assert plan([_placement(desired="reclaimed")], {}) == []


def test_a_finished_reclaim_does_not_pretend_to_harvest():
    """`docker exec` into a container that does not exist preserves nothing.
    Planning a harvest anyway is three failing commands wearing the costume of
    protecting memory."""
    ops = [a["op"] for a in plan([_placement(desired="reclaimed")],
                                 {"web-box-1": {"materialized": False,
                                                "running": False}})]
    assert "harvest" not in ops and ops == []


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


# ---- secrets never enter the control plane ---------------------------------

class TestSecretsStayOnTheMachine:
    """A seat spec lives in the hub's SQLite, and anything holding the operator
    token can read it. So the hub stores the NAME of a secret and the edge
    supplies the VALUE from its own environment — the control plane can be
    fully compromised without leaking a credential."""

    SPEC = {"identity": "s", "spec": {"image": "mcp-hub-seat",
                                      "env_from_host": ["ANTHROPIC_API_KEY"]}}

    def test_the_value_comes_from_the_machine_not_the_spec(self):
        argv = DockerExecutor.create_argv(
            "s", self.SPEC["spec"], {"ANTHROPIC_API_KEY": "sk-live"})
        assert "-e" in argv and "ANTHROPIC_API_KEY=sk-live" in argv

    def test_the_spec_itself_carries_no_value_to_leak(self):
        """The whole point: what the hub stores is a NAME."""
        assert self.SPEC["spec"]["env_from_host"] == ["ANTHROPIC_API_KEY"]
        assert "sk-" not in json.dumps(self.SPEC)

    def test_an_unset_name_is_omitted_not_passed_empty(self):
        """An empty ANTHROPIC_API_KEY authenticates as nothing and produces a
        confusing 401 INSIDE the container; a missing one fails at the door."""
        argv = DockerExecutor.create_argv("s", self.SPEC["spec"], {})
        assert not any(a.startswith("ANTHROPIC_API_KEY") for a in argv)

    def test_the_executor_reads_its_own_environment(self):
        r = Runner()
        DockerExecutor(r, {"ANTHROPIC_API_KEY": "sk-from-edge"}).execute(
            {"op": "materialize", "seat": "s"}, self.SPEC)
        create = next(c for c in r.calls if c[:2] == ["docker", "create"])
        assert "ANTHROPIC_API_KEY=sk-from-edge" in create


def test_create_argv_injects_seat_identity_last_so_it_wins():
    """The container name IS the seat identity — a spec that claims a
    different SEAT_IDENTITY must lose to the placement (docker: last -e
    wins). Drift here = a container reporting one name to docker and
    another to the hub."""
    argv = DockerExecutor.create_argv(
        "seat-a", {"image": "img", "env": {"SEAT_IDENTITY": "impostor"}}
    )
    pairs = [argv[i + 1] for i, a in enumerate(argv) if a == "-e"]
    identity_values = [p for p in pairs if p.startswith("SEAT_IDENTITY=")]
    # both present, ours LAST (docker applies later -e over earlier)
    assert identity_values[-1] == "SEAT_IDENTITY=seat-a"


def test_create_argv_injects_seat_identity_when_spec_has_no_env():
    argv = DockerExecutor.create_argv("seat-b", {"image": "img"})
    assert "SEAT_IDENTITY=seat-b" in argv


# ---- image drift -----------------------------------------------------------
#
# A container with the right NAME in the right STATE reads converged even
# when it runs last month's build. That is a hole in observe-don't-guess:
# `docker ps` cannot see it, and it bit this build for real (the edge
# recreated a seat from a stale `latest` mid-rebuild, 13s before the new
# image finished). The edge now compares the image the container ACTUALLY
# runs against the one its spec names.


def test_enumeration_reports_the_image_a_container_actually_runs():
    r = Runner({"web-box-1": "running"}, images={"web-box-1": "sha256:aaa"})
    state = enumerate_docker(r, ["web-box-1"])
    assert state["web-box-1"]["image"] == "sha256:aaa"
    # ps first (existence/state), inspect second (identity) — never one call
    # pretending to be both: `docker ps --format {{.ImageID}}` DOES NOT EXIST
    # (measured: template error), and .Image gives the tag, which is the same
    # string for a stale container and so cannot detect drift.
    assert r.calls[0][:3] == ["docker", "ps", "-a"]
    assert r.calls[1][:2] == ["docker", "inspect"]


def test_inspect_is_not_called_when_nothing_is_materialized():
    """No containers found = nothing to inspect. `docker inspect` with no
    arguments is an error, so a pointless call would fail the whole pass."""
    r = Runner({})
    enumerate_docker(r, ["web-box-1"])
    assert not any(c[:2] == ["docker", "inspect"] for c in r.calls)


def test_a_failing_inspect_is_a_HARD_error_like_a_failing_ps():
    """Same rule, same reason: a state this pass never observed must not
    reach a report."""
    r = Runner({"web-box-1": "running"}, fail=("inspect",))
    with pytest.raises(EnumerationFailed):
        enumerate_docker(r, ["web-box-1"])


def test_resolve_image_id_returns_the_current_id_of_a_tag():
    from mcp_hub.edge import resolve_image_id

    r = Runner({}, tags={"nginx:alpine": "sha256:bbb"})
    assert resolve_image_id(r, "nginx:alpine") == "sha256:bbb"


def test_an_unpullable_image_is_UNKNOWN_not_drift():
    """An image absent locally means we cannot compare — and an unknown is
    never evidence of drift. Claiming diverged here would make every seat
    on a box that has not pulled yet look broken."""
    from mcp_hub.edge import resolve_image_id

    assert resolve_image_id(Runner({}), "never:pulled") is None


def test_observed_report_says_stale_image_over_running():
    """The honest word. `running` would be TRUE and useless — the container
    is running the wrong thing, and the hub marks any observed != desired
    as diverged, so this alone surfaces the drift."""
    from mcp_hub.edge import observed_report

    rep = observed_report(
        {"id": "pl-1"},
        {"container": "web-box-1", "alive": True, "exists": True,
         "image": "sha256:old", "want_image": "sha256:new",
         "image_matches": False},
    )
    assert rep["state"] == "stale-image"


def test_matching_image_reports_running_as_before():
    from mcp_hub.edge import observed_report

    rep = observed_report(
        {"id": "pl-1"},
        {"container": "web-box-1", "alive": True, "exists": True,
         "image": "sha256:new", "want_image": "sha256:new",
         "image_matches": True},
    )
    assert rep["state"] == "running"


def test_unknown_image_does_not_invent_drift():
    from mcp_hub.edge import observed_report

    rep = observed_report(
        {"id": "pl-1"},
        {"container": "web-box-1", "alive": True, "exists": True},
    )
    assert rep["state"] == "running"


def test_a_stopped_container_is_stopped_even_with_a_stale_image():
    """Drift is about WHAT it runs; stopped is about WHETHER it runs. A
    stopped container reporting stale-image would hide that it is down."""
    from mcp_hub.edge import observed_report

    rep = observed_report(
        {"id": "pl-1"},
        {"container": "web-box-1", "alive": False, "exists": True,
         "image": "sha256:old", "want_image": "sha256:new",
         "image_matches": False},
    )
    assert rep["state"] == "stopped"


def test_a_stale_image_makes_a_live_placement_report_stale_image(tmp_path):
    """End to end: the container is up, docker is happy, and the edge says
    stale-image — which the hub reads as diverged because observed != desired.
    Without this the board shows green while a seat runs last month's build."""
    class Live(Runner):
        def __call__(self, cmd, cwd=None):
            return super().__call__(cmd, cwd)

    r = Live({"web-box-1": "running"},
             images={"web-box-1": "sha256:OLD"},
             tags={"nginx:alpine": "sha256:NEW"})
    api = FakeApi([_placement()])
    edge_apply(api, "box-1", r, [tmp_path], seeder=lambda f: True)

    report = api.observed["pl-1"]
    assert report["state"] == "stale-image"
    assert report["enumeration"]["image"] == "sha256:OLD"
    assert report["enumeration"]["want_image"] == "sha256:NEW"


def test_a_current_image_still_reports_running(tmp_path):
    r = Runner({"web-box-1": "running"},
               images={"web-box-1": "sha256:SAME"},
               tags={"nginx:alpine": "sha256:SAME"})
    api = FakeApi([_placement()])
    edge_apply(api, "box-1", r, [tmp_path], seeder=lambda f: True)
    assert api.observed["pl-1"]["state"] == "running"


def test_one_image_inspect_per_DISTINCT_image(tmp_path):
    """A squad of N seats off one image asks docker once, not N times."""
    p1 = {**_placement(), "id": "pl-1", "seat": "web-box-1"}
    p2 = {**_placement(), "id": "pl-2", "seat": "web-box-2"}
    p2 = {**p2, "seat_spec": dict(p2["seat_spec"], identity="web-box-2")}
    r = Runner({"web-box-1": "running", "web-box-2": "running"},
               images={"web-box-1": "sha256:S", "web-box-2": "sha256:S"},
               tags={"nginx:alpine": "sha256:S"})
    api = FakeApi([p1, p2])
    edge_apply(api, "box-1", r, [tmp_path], seeder=lambda f: True)
    assert len([c for c in r.calls if c[:3] == ["docker", "image", "inspect"]]) == 1


# ---- a completed reclaim ---------------------------------------------------

def test_a_destroyed_substrate_reports_RECLAIMED_not_stopped():
    """Measured on the live probe: after the edge harvested and destroyed
    it, the placement read `want reclaimed · saw stopped · diverged` —
    a SUCCESSFUL reclaim reporting as a failure forever, because "stopped"
    never equals "reclaimed".

    Absence is the evidence: we enumerated and the container was not there.
    """
    from mcp_hub.edge import observed_report

    rep = observed_report(
        {"id": "pl-1", "desired": "reclaimed"},
        {"container": "web-box-1", "alive": False, "exists": False},
    )
    assert rep["state"] == "reclaimed"


def test_a_reclaim_still_pending_does_NOT_claim_reclaimed():
    """The container is still there, so the work has not happened. Claiming
    otherwise would mark the placement converged and stop the edge from
    ever finishing the job."""
    from mcp_hub.edge import observed_report

    rep = observed_report(
        {"id": "pl-1", "desired": "reclaimed"},
        {"container": "web-box-1", "alive": False, "exists": True},
    )
    assert rep["state"] == "stopped"


def test_absence_without_a_reclaim_request_is_still_stopped():
    """A container that vanished on its own is NOT a completed reclaim —
    that would silently absolve whatever destroyed it."""
    from mcp_hub.edge import observed_report

    rep = observed_report(
        {"id": "pl-1", "desired": "running"},
        {"container": "web-box-1", "alive": False, "exists": False},
    )
    assert rep["state"] == "stopped"


def test_a_reclaimed_worktree_seat_uses_its_own_absence_key():
    """Worktree enumeration says `enrolled`, docker says `exists` — the
    same rule must read both, or reclaim looks complete for containers and
    permanently diverged for tmux seats."""
    from mcp_hub.edge import observed_report

    rep = observed_report(
        {"id": "pl-1", "desired": "reclaimed"},
        {"tmux_session": "seat-1", "alive": False, "enrolled": False},
    )
    assert rep["state"] == "reclaimed"


def test_an_enumeration_with_NO_absence_key_never_claims_reclaimed():
    """UNKNOWN is not ABSENCE — the evidence contract's first rule.

    An enumeration carrying neither `exists` nor `enrolled` told us nothing
    about whether the substrate is there, and a truthiness check would read
    that silence as "gone" and mark a destroy complete that may never have
    happened. Found by mutation: `is False` -> `not present` survived every
    other test in this file.
    """
    from mcp_hub.edge import observed_report

    rep = observed_report(
        {"id": "pl-1", "desired": "reclaimed"},
        {"container": "web-box-1", "alive": False},
    )
    assert rep["state"] == "stopped"


# ---- reclaim absence is a measurement of the BOX, not of one artifact ------
#
# Five seat-named tmux sessions ran 4-6 days past their placements' reading
# `reclaimed · converged` (dt's sweep, 2026-08-12). The container verdicts
# were honest — the sessions fell between every instrument: not roster rows,
# not tmux placements, honestly absent as containers. These tests pin the
# widened predicate: absence needs the artifact gone AND nothing alive AND
# (when the box was sweepable) no seat-named session on any socket.


def test_a_VISIBLY_ALIVE_session_is_never_reported_reclaimed():
    """The executed proof that started this: the worktree absence key is the
    ROSTER ROW — a record, deleted by `squad rm` before its kill — while the
    `alive` measurement sat ignored in the same dict. A record asserts;
    only the process measurement is evidence of destruction."""
    from mcp_hub.edge import observed_report

    rep = observed_report(
        {"id": "pl-1", "desired": "reclaimed"},
        {"tmux_session": "seat-1", "alive": True, "enrolled": False},
    )
    assert rep["state"] == "running"


def test_alive_UNKNOWN_withholds_the_reclaimed_verdict():
    """`alive` missing is "we could not look", not "nothing runs" — the same
    UNKNOWN ≠ ABSENT rule the no-absence-key test above pins, applied to
    the second fact. Found the same way: a truthiness check here reads
    None as False and grants absence on silence."""
    from mcp_hub.edge import observed_report

    rep = observed_report(
        {"id": "pl-1", "desired": "reclaimed"},
        {"tmux_session": "seat-1", "enrolled": False},
    )
    assert rep["state"] == "stopped"


def test_a_seat_named_session_on_ANY_socket_reads_leftover_not_reclaimed():
    """The five specimens exactly: container honestly gone, a session
    wearing the seat's name still running on the box. `leftover` diverges
    against `reclaimed`, so the board surfaces it instead of converging
    over a running process."""
    from mcp_hub.edge import observed_report

    rep = observed_report(
        {"id": "pl-1", "desired": "reclaimed"},
        {"container": "web-box-1", "alive": False, "exists": False,
         "host_sessions": ["squad"]},
    )
    assert rep["state"] == "leftover"


def test_a_FAILED_sweep_withholds_reclaimed_rather_than_granting_it():
    """A sweep that could not look everywhere grants nothing — a failed
    look must never read as a clean one."""
    from mcp_hub.edge import observed_report

    rep = observed_report(
        {"id": "pl-1", "desired": "reclaimed"},
        {"container": "web-box-1", "alive": False, "exists": False,
         "host_sessions_unknown": True},
    )
    assert rep["state"] == "stopped"


def test_a_MEASURED_EMPTY_sweep_completes_the_reclaim():
    """The ordinary success: artifact gone, nothing alive, sweep ran and
    found nothing wearing the name."""
    from mcp_hub.edge import observed_report

    rep = observed_report(
        {"id": "pl-1", "desired": "reclaimed"},
        {"container": "web-box-1", "alive": False, "exists": False,
         "host_sessions": []},
    )
    assert rep["state"] == "reclaimed"


class TestHostTmuxSweep:
    """host_tmux_sessions: enumerate the box, let the caller subtract the
    known — the inverse of every instrument the five fell between."""

    def _sock(self, d, name):
        import socket as socketlib
        s = socketlib.socket(socketlib.AF_UNIX)
        s.bind(str(d / name))
        return s

    def test_no_socket_dir_is_a_MEASURED_empty_not_a_failure(self, tmp_path):
        from mcp_hub.edge import host_tmux_sessions
        out = host_tmux_sessions(lambda cmd, cwd=None: (0, ""),
                                 socket_dir=tmp_path / "absent")
        assert out == {}

    def test_sessions_are_reported_per_socket(self, tmp_path):
        from mcp_hub.edge import host_tmux_sessions
        keep = [self._sock(tmp_path, "squad"), self._sock(tmp_path, "other")]
        (tmp_path / "not-a-socket").write_text("")   # ignored: plain file

        def runner(cmd, cwd=None):
            if cmd[2].endswith("squad"):
                return 0, "seat-1\nmcp-hub-dev\n"
            return 0, "ghost\n"

        out = host_tmux_sessions(runner, socket_dir=tmp_path)
        assert out == {"squad": {"seat-1", "mcp-hub-dev"},
                       "other": {"ghost"}}
        del keep

    def test_a_dead_socket_is_measured_empty_but_a_FAILURE_is_None(
            self, tmp_path):
        """`no server running` is tmux answering the question; anything
        else is the question failing. Conflating them either invents
        sessions or, worse, reads blindness as a clean box."""
        from mcp_hub.edge import host_tmux_sessions
        keep = [self._sock(tmp_path, "dead"), self._sock(tmp_path, "live")]

        def dead_runner(cmd, cwd=None):
            if cmd[2].endswith("dead"):
                return 1, "no server running on " + cmd[2]
            return 0, "seat-1\n"

        assert host_tmux_sessions(dead_runner, socket_dir=tmp_path) == \
            {"live": {"seat-1"}}

        def broken_runner(cmd, cwd=None):
            return 1, "some other failure"

        assert host_tmux_sessions(broken_runner, socket_dir=tmp_path) is None
        del keep


def test_the_pass_sweeps_ONCE_and_stamps_reclaim_enumerations(tmp_path):
    """Pass-level: the sweep result reaches each reclaimed placement's
    enumeration (here: a seat-named session on a foreign socket -> the
    observed record says `leftover` and names the socket), and a failed
    sweep arrives as `host_sessions_unknown`, never as silence."""
    r = Runner()   # container gone: empty world
    api = FakeApi([_placement(desired="reclaimed")])
    summary = edge_apply(api, "box-1", r, [tmp_path], seeder=lambda f: True,
                         session_sweep=lambda: {"squad": {"web-box-1"}})
    assert summary["placements"] == 1
    report = api.observed["pl-1"]
    assert report["state"] == "leftover"
    assert report["enumeration"]["host_sessions"] == ["squad"]

    api2 = FakeApi([_placement(desired="reclaimed")])
    edge_apply(api2, "box-1", Runner(), [tmp_path], seeder=lambda f: True,
               session_sweep=lambda: None)
    rep2 = api2.observed["pl-1"]
    assert rep2["enumeration"].get("host_sessions_unknown") is True
    assert rep2["state"] != "reclaimed"


# ---------------------------------------------------------------------------
# repo_mount — the host clones, the container mounts (docs/seat-repo-access.md)
# ---------------------------------------------------------------------------


class TestRepoMount:
    """The credential leaves the container. The host clones — where the token
    already lives, in this machine's own environment — and the container
    receives a DIRECTORY.

    Every assertion here reads what the container would actually RECEIVE (the
    docker argv), never the spec. A spec still naming the token is the normal
    case, not the failure — the drop happens at the one place the value would
    enter the container.
    """

    SPEC = {
        "image": "mcp-hub-seat",
        "env_from_host": ["CLAUDE_CODE_OAUTH_TOKEN", "SEAT_GITHUB_TOKEN"],
        "repo_mount": {"repo": "dreamteam-ai-labs/browser-agent-test-fixture"},
        "memory_volume": "seat-memory-x",
    }
    ENV = {"CLAUDE_CODE_OAUTH_TOKEN": "oauth-value",
           "SEAT_GITHUB_TOKEN": "ghp-SECRET-VALUE"}

    def test_the_github_token_never_enters_the_container(self):
        """Mutation: return `wanted` unfiltered from injected_credentials()
        → the secret rides in and this fails.

        This is the whole feature. A seat whose code is mounted has no reason
        to hold a GitHub credential.
        """
        argv = DockerExecutor.create_argv("s1", self.SPEC, self.ENV)
        assert not any("ghp-SECRET-VALUE" in a for a in argv)
        assert not any(a.startswith("SEAT_GITHUB_TOKEN") for a in argv)

    def test_the_anthropic_credential_still_arrives(self):
        """Positive control. A change that dropped EVERY credential would pass
        the test above while making the seat unable to run at all."""
        argv = DockerExecutor.create_argv("s1", self.SPEC, self.ENV)
        assert "CLAUDE_CODE_OAUTH_TOKEN=oauth-value" in argv

    def test_the_checkout_is_mounted_at_the_workdir(self):
        from mcp_hub.edge import SEAT_WORK_DIR, repo_mount_dir

        argv = DockerExecutor.create_argv("s1", self.SPEC, self.ENV)
        want = repo_mount_dir("s1", self.SPEC["repo_mount"]["repo"])
        assert f"{want}:{SEAT_WORK_DIR}" in argv

    def test_the_memory_volume_is_still_mounted_beside_it(self):
        """The two mounts are independent; a repo mount must not cost the seat
        its durable state — that regression would be invisible until a
        recreate."""
        from mcp_hub.edge import SEAT_STATE_DIR

        argv = DockerExecutor.create_argv("s1", self.SPEC, self.ENV)
        assert f"seat-memory-x:{SEAT_STATE_DIR}" in argv

    def test_two_seats_on_one_repo_get_SEPARATE_checkouts(self):
        """A shared working tree would let two seats fight over the index and
        the checked-out ref, silently."""
        from mcp_hub.edge import repo_mount_dir

        a = repo_mount_dir("seat-a", "org/thing")
        b = repo_mount_dir("seat-b", "org/thing")
        assert a != b
        assert a.name == b.name == "thing"

    def test_a_seat_without_repo_mount_keeps_its_token(self):
        """Positive control for the DROP: today's container-side clone path is
        untouched, so existing seats keep working."""
        from mcp_hub.edge import injected_credentials

        spec = {k: v for k, v in self.SPEC.items() if k != "repo_mount"}
        assert injected_credentials(spec) == [
            "CLAUDE_CODE_OAUTH_TOKEN", "SEAT_GITHUB_TOKEN"]

    def test_the_clone_runs_BEFORE_the_container_is_created(self, tmp_path,
                                                            monkeypatch):
        monkeypatch.setattr("mcp_hub.edge.SEAT_REPOS_ROOT", tmp_path)
        r = Runner()
        DockerExecutor(r, self.ENV).execute(
            {"op": "materialize", "seat": "s1"}, {"spec": self.SPEC})
        kinds = [c[0] for c in r.calls]
        assert kinds[:2] == ["git", "docker"], kinds
        assert "clone" in r.calls[0]

    def test_a_failed_clone_STOPS_the_materialize(self, tmp_path, monkeypatch):
        """Mutation: ignore the rc from _prepare_repo_mount → a container is
        created over an empty directory.

        `docker ps` would call that healthy and the agent would sit in an
        empty workdir with nothing saying why.
        """
        monkeypatch.setattr("mcp_hub.edge.SEAT_REPOS_ROOT", tmp_path)

        class FailingGit(Runner):
            def __call__(self, cmd, cwd=None):
                if cmd[0] == "git":
                    self.calls.append(list(cmd))
                    return 128, "fatal: repository not found"
                return super().__call__(cmd, cwd)

        r = FailingGit()
        out = DockerExecutor(r, self.ENV).execute(
            {"op": "materialize", "seat": "s1"}, {"spec": self.SPEC})
        assert out["skipped"] is True
        assert "repo_mount" in out["reason"] and "128" in out["reason"]
        assert not any(c[0] == "docker" for c in r.calls)

    def test_an_existing_checkout_is_MOVED_not_recloned(self, tmp_path,
                                                       monkeypatch):
        """The repo is assigned PER BUILD, so the second materialize has to be
        able to change what the tree holds."""
        from mcp_hub.edge import repo_mount_argv

        monkeypatch.setattr("mcp_hub.edge.SEAT_REPOS_ROOT", tmp_path)
        (tmp_path / "s1" / "org" / "thing" / ".git").mkdir(parents=True)
        cmds = repo_mount_argv("s1", {"repo": "org/thing", "ref": "topic"},
                               root=tmp_path)
        # Asserted by position RELATIVE to -C rather than by index: the argv
        # gained a `-c credential.helper=…` prefix and an index-based
        # assertion breaks on a change that is not a defect.
        assert "clone" not in cmds[0]
        at = cmds[0].index("-C")
        assert cmds[0][at + 1] == str(tmp_path / "s1/org/thing")
        assert "fetch" in cmds[0]
        assert "origin/topic" in cmds[-1]

    def test_a_bad_repo_mount_is_refused_at_the_edge_too(self, tmp_path,
                                                         monkeypatch):
        """The hub refuses such a spec at write time — but a spec stored
        BEFORE this guard existed would otherwise materialize here. Same
        reason the volumes check is repeated at the edge."""
        monkeypatch.setattr("mcp_hub.edge.SEAT_REPOS_ROOT", tmp_path)
        r = Runner()
        spec = dict(self.SPEC, repo_mount={"repo": "../../etc"})
        out = DockerExecutor(r, self.ENV).execute(
            {"op": "materialize", "seat": "s1"}, {"spec": spec})
        assert out["skipped"] is True
        assert not r.calls


class TestRepoMountCredential:
    """The host does the cloning, so the credential has to reach git THERE.

    The first implementation of `repo_mount` built a bare `git clone` and
    shipped. Measured against the live private repo on 2026-08-11: `fatal:
    could not read Username`. The design sentence "the host clones, where the
    credential already lives" was declared and not enforced — inside the very
    change that closes another instance of that shape.
    """

    RM = {"repo": "dreamteam-ai-labs/dreamteam"}

    def test_the_clone_carries_a_credential_helper(self, tmp_path):
        """Mutation: drop `cred` from the argv → this fails, and the feature
        cannot fetch any private repo."""
        from mcp_hub.edge import repo_mount_argv

        argv = repo_mount_argv("s1", self.RM, root=tmp_path)[0]
        assert "-c" in argv
        assert any(a.startswith("credential.helper=") for a in argv)

    def test_the_fetch_path_carries_it_too(self, tmp_path):
        """A private repo needs auth to FETCH, not only to clone — and the
        fetch path is the one every re-assignment takes."""
        from mcp_hub.edge import repo_mount_argv

        (tmp_path / "s1" / "org" / "thing" / ".git").mkdir(parents=True)
        for argv in repo_mount_argv("s1", {"repo": "org/thing"},
                                    root=tmp_path):
            assert any(a.startswith("credential.helper=") for a in argv)

    def test_the_argv_carries_the_VARIABLE_never_the_value(self, tmp_path):
        """The helper holds the literal `${SEAT_GITHUB_TOKEN}`, expanded by
        the shell git spawns. So an argv is safe to log, print in an error, or
        record in a journal — which the failure path above does."""
        from mcp_hub.edge import repo_mount_argv

        argv = repo_mount_argv("s1", self.RM, root=tmp_path)[0]
        joined = " ".join(argv)
        assert "$SEAT_GITHUB_TOKEN" in joined
        assert "ghp_" not in joined and "github_pat_" not in joined

    def test_the_ref_still_lands_beside_the_credential(self, tmp_path):
        """The `--branch` splice is positional; adding `-c …` in front moved
        the anchor it counted from."""
        from mcp_hub.edge import repo_mount_argv

        argv = repo_mount_argv("s1", dict(self.RM, ref="topic"),
                               root=tmp_path)[0]
        assert argv[-4:-2] == ["--branch", "topic"]
        assert argv[-2].endswith(".git") and argv[-1].endswith("dreamteam")

    def test_a_missing_token_is_NAMED_not_left_in_a_git_error(self, tmp_path,
                                                              monkeypatch):
        """A hand-run `edge apply` does not load edge-env; the systemd unit
        does. Same trap the credentials gate already documents, now reachable
        by a second route."""
        monkeypatch.setattr("mcp_hub.edge.SEAT_REPOS_ROOT", tmp_path)

        class FailingGit(Runner):
            def __call__(self, cmd, cwd=None):
                if cmd[0] == "git":
                    self.calls.append(list(cmd))
                    return 128, "could not read Username for 'https://github.com'"
                return super().__call__(cmd, cwd)

        spec = {"image": "i", "repo_mount": self.RM}
        out = DockerExecutor(FailingGit(), {}).execute(
            {"op": "materialize", "seat": "s1"}, {"spec": spec})
        assert "SEAT_GITHUB_TOKEN is not set" in out["reason"]
        assert "edge-env" in out["reason"]

    def test_a_token_that_IS_set_gets_no_misleading_hint(self, tmp_path,
                                                         monkeypatch):
        """Positive control: the hint must not fire on every failure, or it
        sends the reader after the wrong cause."""
        monkeypatch.setattr("mcp_hub.edge.SEAT_REPOS_ROOT", tmp_path)

        class FailingGit(Runner):
            def __call__(self, cmd, cwd=None):
                if cmd[0] == "git":
                    self.calls.append(list(cmd))
                    return 128, "fatal: repository not found"
                return super().__call__(cmd, cwd)

        spec = {"image": "i", "repo_mount": self.RM}
        out = DockerExecutor(FailingGit(), {"SEAT_GITHUB_TOKEN": "x"}).execute(
            {"op": "materialize", "seat": "s1"}, {"spec": spec})
        assert "is not set" not in out["reason"]
        assert "repository not found" in out["reason"]


class TestRosterEnrolment:
    """`/voice` authorises a container by membership in this machine's squad
    roster. `voice_host.py` states the edge shells out to `squad
    add-container` — it did not, so every seat materialized by `edge apply`
    was refused audio. Measured 2026-08-11 on dev-vm-1: zero streams all day,
    both live seats recording RMS 0, three REFUSED lines naming the empty
    roster. These tests make that sentence true.
    """

    SPEC = {"identity": "s1", "spec": {"image": "mcp-hub-seat:latest"}}

    @pytest.fixture()
    def squad(self, monkeypatch, tmp_path):
        monkeypatch.setattr("mcp_hub.edge._squad_bin", lambda: "/fake/squad")
        monkeypatch.setattr("mcp_hub.edge.SEAT_REPOS_ROOT",
                            tmp_path / "seat-repos")
        return "/fake/squad"

    def test_materialize_enrols_the_container(self, squad):
        """Mutation: delete the _enrol_container call → this fails, and every
        edge-created seat silently loses audio again."""
        r = Runner()
        out = DockerExecutor(r).execute(
            {"op": "materialize", "seat": "s1"}, self.SPEC)
        enrol = next(c for c in r.calls if c[0] == squad)
        assert enrol[1] == "add-container"
        # name, folder, CONTAINER — the third arg is what the gate matches on
        # (voice_host.parse_squad_roster reads `@docker:<container>`).
        assert enrol[2] == "s1" and enrol[4] == "s1"
        assert out["enrolled"]["ok"] is True

    def test_enrolment_happens_only_AFTER_a_successful_create(self, squad):
        """A roster row for a container that failed to materialize is a lie
        the gate would then trust."""
        class FailCreate(Runner):
            def __call__(self, cmd, cwd=None):
                if cmd[:2] == ["docker", "create"]:
                    self.calls.append(list(cmd))
                    return 1, "no such image"
                return super().__call__(cmd, cwd)

        r = FailCreate()
        DockerExecutor(r).execute({"op": "materialize", "seat": "s1"},
                                  self.SPEC)
        assert not any(c[0] == squad for c in r.calls)

    def test_a_machine_without_squad_still_materializes(self, monkeypatch,
                                                        tmp_path):
        """BEST EFFORT, NEVER FATAL — the docker-only edge is a supported
        shape, and a seat without audio beats a placement that refuses."""
        monkeypatch.setattr("mcp_hub.edge._squad_bin", lambda: None)
        r = Runner()
        out = DockerExecutor(r).execute(
            {"op": "materialize", "seat": "s1"}, self.SPEC)
        assert out["rc"] == 0
        assert out["enrolled"]["skipped"] is True
        assert "no `squad`" in out["enrolled"]["reason"]

    def test_the_skip_is_REPORTED_never_silent(self, monkeypatch, tmp_path):
        """The bug being fixed was invisibility, so a failure to enrol must
        not itself be invisible."""
        monkeypatch.setattr("mcp_hub.edge._squad_bin", lambda: None)
        out = DockerExecutor(Runner()).execute(
            {"op": "materialize", "seat": "s1"}, self.SPEC)
        assert "enrolled" in out and out["enrolled"].get("reason")

    def test_an_already_enrolled_seat_is_not_an_error(self, squad,
                                                      monkeypatch):
        """A re-materialized seat (memory-volume fix, image swap) keeps the row
        it already has — this path runs on every recreate."""
        class Enrolled(Runner):
            def __call__(self, cmd, cwd=None):
                if cmd[0] == "/fake/squad":
                    self.calls.append(list(cmd))
                    return 1, "!! 's1' is already enrolled"
                return super().__call__(cmd, cwd)

        out = DockerExecutor(Enrolled()).execute(
            {"op": "materialize", "seat": "s1"}, self.SPEC)
        assert out["enrolled"] == {"ok": True, "already": True}

    def test_a_repo_mount_seat_enrols_the_CHECKOUT_as_its_folder(self, squad,
                                                                 tmp_path):
        """The roster folder is the tab's cwd. For a repo_mount seat the right
        one already exists — the tree the seat is working in."""
        from mcp_hub.edge import repo_mount_dir

        spec = {"identity": "s1", "spec": {
            "image": "i", "repo_mount": {"repo": "org/thing"}}}
        r = Runner()
        DockerExecutor(r).execute({"op": "materialize", "seat": "s1"}, spec)
        enrol = next(c for c in r.calls if c[0] == squad)
        assert enrol[3] == str(repo_mount_dir("s1", "org/thing"))

    def test_a_pod_is_named_not_silently_skipped(self, squad):
        """A pod's rows are per-AGENT and name a tmux session each; one row for
        the container would give the workspace a tab attaching to a session no
        inhabitant uses."""
        spec = {"identity": "p", "spec": {
            "image": "i", "agents": [{"identity": "a"}, {"identity": "b"}]}}
        out = DockerExecutor(Runner()).execute(
            {"op": "materialize", "seat": "p"}, spec)
        assert out["enrolled"]["skipped"] is True
        assert "pod" in out["enrolled"]["reason"]


class TestCredentialPolicyAtMaterialize:
    """The policy is enforced at the last place that can refuse before a
    value enters the container — the same station as check_volumes."""

    def test_an_undeclared_name_refuses_the_materialize(self):
        spec = {"image": "i", "allowed_env": ["GOOD"],
                "env_from_host": ["SMUGGLED"]}
        out = DockerExecutor(Runner(), {"SMUGGLED": "v"}).execute(
            {"op": "materialize", "seat": "s1"}, {"spec": spec})
        assert out["skipped"] and "SMUGGLED" in out["reason"]

    def test_a_pre_policy_spec_still_materializes(self):
        r = Runner()
        out = DockerExecutor(r).execute(
            {"op": "materialize", "seat": "s1"},
            {"spec": {"image": "i"}})
        assert out.get("rc") == 0

class TestRosterDeEnrolment:
    """The mirror of TestRosterEnrolment: what materialize adds, destroy
    removes. Measured asymmetry (fb, 2026-08-11): `voicebar rows after
    destroy: 1` — and a REUSED container name would inherit an authorisation
    nobody granted."""

    @pytest.fixture()
    def squad(self, monkeypatch):
        monkeypatch.setattr("mcp_hub.edge._squad_bin", lambda: "/fake/squad")
        return "/fake/squad"

    def test_destroy_deenrols_the_container(self, squad):
        """Mutation: delete the _deenrol_container call → this fails, and
        destroyed seats leave authorisations behind again."""
        r = Runner(world={"s1": "running"})
        out = DockerExecutor(r).execute(
            {"op": "destroy", "seat": "s1"}, {"spec": {"image": "i"}})
        rm = next(c for c in r.calls if c[0] == squad)
        assert rm[1] == "rm" and rm[2] == "s1"
        assert out["deenrolled"]["ok"] is True

    def test_deenrolment_happens_only_AFTER_a_successful_rm(self, squad):
        """A row removed for a container that is still running would make
        the roster lie in the opposite direction."""
        class FailRm(Runner):
            def __call__(self, cmd, cwd=None):
                if cmd[:3] == ["docker", "rm", "-f"]:
                    self.calls.append(list(cmd))
                    return 1, "cannot remove"
                return super().__call__(cmd, cwd)

        r = FailRm()
        DockerExecutor(r).execute({"op": "destroy", "seat": "s1"},
                                  {"spec": {"image": "i"}})
        assert not any(c[0] == squad for c in r.calls)

    def test_a_machine_without_squad_still_destroys(self):
        r = Runner(world={"s1": "running"})
        out = DockerExecutor(r).execute(
            {"op": "destroy", "seat": "s1"}, {"spec": {"image": "i"}})
        assert out["rc"] == 0
        assert out["deenrolled"]["skipped"]

    def test_an_already_missing_row_reads_as_done(self, squad):
        class UnknownAgent(Runner):
            def __call__(self, cmd, cwd=None):
                if cmd[0] == "/fake/squad":
                    self.calls.append(list(cmd))
                    return 1, "!! unknown agent 's1' (see conf)"
                return super().__call__(cmd, cwd)

        out = DockerExecutor(UnknownAgent()).execute(
            {"op": "destroy", "seat": "s1"}, {"spec": {"image": "i"}})
        assert out["deenrolled"] == {"ok": True, "already": True}

    def test_a_pod_destroy_does_not_reach_past_what_materialize_added(
            self, squad):
        r = Runner()
        out = DockerExecutor(r).execute(
            {"op": "destroy", "seat": "p1"},
            {"spec": {"image": "i", "agents": [{"identity": "a"}]}})
        assert not any(c[0] == squad for c in r.calls)
        assert out["deenrolled"]["skipped"]
