"""W1.2 — machinery health (docs/verification/wave-1.md, B1-B4).

The five-day blindness: five systemd units on fireblade-wsl died 203/EXEC and
every surface reported normal, because the edge's pass summary went to stdout
and the journal — the one place nothing was watching — and the hub's only
machine fact was last_seen. The edge now reports on ITSELF, the hub stores
it, and every hop that used to discard the record renders it.

Two instruments, two vocabularies, never one phrase: `stale`/`never` describe
the heartbeat DAEMON's snapshot; `edge_state` describes the RECONCILER. A
dead daemon and a dead edge are different failures.
"""

import argparse
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from mcp_hub.edge import edge_apply, push_failure
from mcp_hub.fleet_tree import EDGE_STALE_SECONDS, _edge_state, build_tree
from mcp_hub.server import create_server

OP = {"Authorization": "Bearer op-token"}
NOW = 1_800_000_000.0


class _RecordingApi:
    """The producer's side of the wire, recorded."""

    def __init__(self, fail_push: bool = False):
        self.status_posts: list[tuple[str, dict]] = []
        self.fail_push = fail_push

    def pull_placements(self, machine):
        return []

    def pull_seats(self, machine):
        return []  # the lane leg's discovery door — no lane seats here

    def push_observed(self, pid, report):
        pass

    def push_status(self, machine, payload):
        if self.fail_push:
            raise RuntimeError("hub unreachable")
        self.status_posts.append((machine, payload))


def _ok_runner(cmd):
    return 0, ""


# ---------------------------------------------------------------------------
# B1 — every pass reports on itself
# ---------------------------------------------------------------------------


class TestProducer:
    def test_edge_apply_sends_its_own_verdict(self, tmp_path):
        """Mutation: drop the "edge" key from the push_status payload →
        this fails."""
        api = _RecordingApi()
        edge_apply(api, machine="box-1", runner=_ok_runner,
                   scan_dirs=[tmp_path])
        assert len(api.status_posts) == 1
        _, payload = api.status_posts[0]
        edge = payload["edge"]
        assert edge["result"] == "ok"
        assert edge["ts"] > 0
        assert edge["placements"] == 0
        assert edge["errors"] == []


# ---------------------------------------------------------------------------
# B3 — a failing pass reaches the hub (structural: from the except path)
# ---------------------------------------------------------------------------


class TestFailurePath:
    def test_push_failure_reports_failed(self):
        api = _RecordingApi()
        push_failure(api, "box-1", "EnumerationFailed: cannot see tmux")
        _, payload = api.status_posts[0]
        assert payload["edge"]["result"] == "failed"
        assert "cannot see tmux" in payload["edge"]["errors"][0]

    def test_the_reporter_cannot_die_of_its_own_report(self):
        """push_status raises on HTTP error; an exception here would replace
        the real error with the reporting error at exactly the moment the
        real one matters. Mutation: remove the try/except → this fails."""
        api = _RecordingApi(fail_push=True)
        push_failure(api, "box-1", "boom")  # must not raise

    def _drive_edge_command(self, monkeypatch, action, capsys):
        from mcp_hub import cli
        from mcp_hub.edge import EnumerationFailed

        recorder = _RecordingApi()

        def boom(api, machine, runner, scan_dirs, **kw):
            raise EnumerationFailed("tmux gone")

        monkeypatch.setattr("mcp_hub.edge.edge_apply", boom)
        monkeypatch.setattr("mcp_hub.edge.HubAPI", lambda **kw: recorder)
        if action == "watch":
            # One doorbell ring, then return — the loop itself is not under
            # test here, the except path inside one_pass is.
            monkeypatch.setattr(
                "mcp_hub.edge.watch_forever",
                lambda base, token, machine, fn: fn("test-ring"),
            )
        cli.edge_command(argparse.Namespace(
            action=action, hub_url="http://h/mcp", machine="box",
            token="t", scan_dir=None, dry_run=False,
        ))
        return recorder

    def test_apply_except_path_posts_failed(self, monkeypatch, capsys):
        """Pre-fix, EnumerationFailed died on stderr and NO status ever
        posted (push_status is the last step of a SUCCESSFUL pass).
        Mutation: remove the push_failure call from the apply except → this
        fails."""
        recorder = self._drive_edge_command(monkeypatch, "apply", capsys)
        assert len(recorder.status_posts) == 1
        assert recorder.status_posts[0][1]["edge"]["result"] == "failed"

    def test_watch_except_path_posts_failed(self, monkeypatch, capsys):
        """Same rule on the doorbell path — a silently-failing watcher is
        the shape the doorbell doc warns about. Mutation: remove the
        push_failure call from one_pass → this fails."""
        recorder = self._drive_edge_command(monkeypatch, "watch", capsys)
        assert len(recorder.status_posts) == 1
        assert recorder.status_posts[0][1]["edge"]["result"] == "failed"


# ---------------------------------------------------------------------------
# B1/B4 — the hub stores the verdict and names what it drops
# ---------------------------------------------------------------------------


class TestHubStorage:
    @pytest.fixture()
    def rig(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("MCP_HUB_API_TOKEN", "op-token")
        server = create_server(db_path=tmp_path / "hub.db")
        with TestClient(server.streamable_http_app()) as c:
            token = c.post(
                "/api/v1/machines",
                json={"name": "box-1", "os": "linux", "capabilities": {}},
                headers=OP,
            ).json()["token"]
            yield c, {"Authorization": f"Bearer {token}"}

    def _machine_record(self, c):
        return next(
            m for m in c.get("/api/v1/machines", headers=OP).json()["machines"]
            if m["name"] == "box-1"
        )

    def test_blind_state_is_null_not_healthy(self, rig):
        """An enrolled machine whose edge has NEVER reported: the fields are
        NULL — no claim — never a default that reads as health."""
        c, _ = rig
        m = self._machine_record(c)
        assert m["edge_last_run"] is None
        assert m["edge_result"] is None
        assert _edge_state(m, NOW) == "never"

    def test_edge_report_stored_and_served(self, rig):
        """Mutation: drop the edge-key handling from machine_status → the
        fields stay NULL and this fails."""
        c, mh = rig
        r = c.post(
            "/api/v1/machines/box-1/status",
            json={"edge": {"ts": NOW, "result": "ok", "placements": 2,
                           "actions": 1, "errors": []}},
            headers=mh,
        )
        assert r.status_code == 200
        m = self._machine_record(c)
        assert m["edge_last_run"] == NOW
        assert m["edge_result"]["result"] == "ok"
        assert m["edge_result"]["placements"] == 2

    def test_failed_report_stored(self, rig):
        c, mh = rig
        c.post(
            "/api/v1/machines/box-1/status",
            json={"edge": {"ts": NOW, "result": "failed",
                           "errors": ["tmux gone"]}},
            headers=mh,
        )
        m = self._machine_record(c)
        assert m["edge_result"]["result"] == "failed"
        assert _edge_state(m, NOW) == "failed"

    def test_unstored_payload_keys_are_named_never_silently_dropped(self, rig):
        """B4 — the shape that hid this whole channel: the edge sent a
        "seats" key for a month and the hub read only the keys it knew,
        silently. A key is handled, or its drop is observable; never neither.

        Mutation: return {"ok": True} without the ignored list → fails."""
        c, mh = rig
        r = c.post(
            "/api/v1/machines/box-1/status",
            json={"seats": [], "mystery": 1,
                  "edge": {"ts": NOW, "result": "ok", "errors": []}},
            headers=mh,
        )
        assert r.json()["ignored"] == ["mystery", "seats"]


# ---------------------------------------------------------------------------
# B2 — derived state + the two instruments stay two instruments
# ---------------------------------------------------------------------------


class TestDerivedState:
    def test_no_record_is_no_claim(self):
        assert _edge_state(None, NOW) is None

    def test_never_stale_failed_ok(self):
        assert _edge_state({"edge_last_run": None}, NOW) == "never"
        assert _edge_state(
            {"edge_last_run": NOW - EDGE_STALE_SECONDS - 1,
             "edge_result": {"result": "ok"}}, NOW) == "stale"
        assert _edge_state(
            {"edge_last_run": NOW - 5,
             "edge_result": {"result": "failed"}}, NOW) == "failed"
        assert _edge_state(
            {"edge_last_run": NOW - 5,
             "edge_result": {"result": "ok"}}, NOW) == "ok"

    def _node(self, *, fleet_ts, machine_info):
        tree = build_tree(
            roster=[],
            board={"agents": {}},
            workspaces={
                "rows": [{
                    "name": "w", "machine": "there",
                    "path": "/x/w.code-workspace", "folders": 0, "error": "",
                    "on_disk": True, "open_now": False, "registered": True,
                    "squad": "", "listings": [],
                }],
                "machines": ["there"],
                "machine_info": machine_info,
                "this_machine": "here",
            },
            fleet={"ts": fleet_ts, "agents": []},
            this_machine="here",
            now=NOW,
        )
        return next(m for m in tree["machines"] if m["machine"] == "there")

    def test_daemon_alive_edge_dead_and_vice_versa(self):
        """The instruments must be able to DISAGREE — one shared phrase is
        how a dead reconciler read as a quiet box for five days.

        Mutation: derive edge_state from the fleet snapshot ts → both halves
        fail."""
        # Daemon fresh, edge stale:
        node = self._node(
            fleet_ts=NOW,
            machine_info={"there": {
                "edge_last_run": NOW - EDGE_STALE_SECONDS - 60,
                "edge_result": {"result": "ok"},
            }},
        )
        assert node["stale"] is False
        assert node["edge_state"] == "stale"
        # Daemon stale, edge fresh:
        node = self._node(
            fleet_ts=NOW - 10_000,
            machine_info={"there": {
                "edge_last_run": NOW - 5,
                "edge_result": {"result": "ok"},
            }},
        )
        assert node["stale"] is True
        assert node["edge_state"] == "ok"

    def test_local_machine_shows_its_own_dead_edge(self):
        """The snapshot fields are suppressed for the local machine (its
        writer is local), but the edge is a HUB fact: a box's own dead edge
        must show on its own board — fireblade's five days were exactly
        this."""
        tree = build_tree(
            roster=[],
            board={"agents": {}},
            workspaces={
                "rows": [{
                    "name": "w", "machine": "here",
                    "path": "/x/w.code-workspace", "folders": 0, "error": "",
                    "on_disk": True, "open_now": False, "registered": True,
                    "squad": "", "listings": [],
                }],
                "machines": ["here"],
                "machine_info": {"here": {
                    "edge_last_run": NOW - 999_999,
                    "edge_result": {"result": "ok"},
                }},
                "this_machine": "here",
            },
            fleet={"ts": NOW, "agents": []},
            this_machine="here",
            now=NOW,
        )
        node = next(m for m in tree["machines"] if m["machine"] == "here")
        assert node["edge_state"] == "stale"


# ---------------------------------------------------------------------------
# B1 — the surfaces render it
# ---------------------------------------------------------------------------


class TestSurfaces:
    def test_machine_label_uses_the_edge_vocabulary(self):
        from mcp_hub.settings_app import SettingsApp

        p = {"primary": "white", "secondary": "grey", "warning": "red"}
        base = {"unknown": False, "machine": "box", "local": False,
                "stale": False, "never": False, "drift_count": 0}
        label = SettingsApp._machine_label(
            None, {**base, "edge_state": "failed"}, p)
        assert "edge FAILING" in label
        label = SettingsApp._machine_label(
            None, {**base, "edge_state": "stale"}, p)
        assert "edge not reporting" in label
        label = SettingsApp._machine_label(
            None, {**base, "edge_state": "never"}, p)
        assert "no edge yet" in label
        # ok and no-claim render NOTHING — the label carries only
        # exceptional facts.
        label = SettingsApp._machine_label(
            None, {**base, "edge_state": "ok"}, p)
        assert "edge" not in label
        label = SettingsApp._machine_label(
            None, {**base, "edge_state": None}, p)
        assert "edge" not in label

    def test_workspaces_list_carries_the_edge_bit(self, monkeypatch, capsys):
        import time as _time

        from mcp_hub import cli

        canned = {
            "hub_reachable": True, "note": "",
            "machines": ["box-2"], "this_machine": "here",
            # Relative to REAL time: the command derives `now` itself
            # (time.time()), so a fixed-epoch timestamp here would read as
            # in-the-future and the state would come out "ok".
            "machine_info": {"box-2": {
                "edge_last_run": _time.time() - 999_999,
                "edge_result": {"result": "ok"},
            }},
            "rows": [{
                "name": "w", "machine": "box-2",
                "path": "/x/w.code-workspace", "folders": 0, "error": "",
                "on_disk": True, "open_now": False, "registered": True,
                "squad": "", "listings": [],
            }],
        }
        monkeypatch.setattr(
            "mcp_hub.workspace_data.collect_workspaces",
            lambda api, dirs, machine: canned,
        )
        rc = cli.workspaces_command(
            argparse.Namespace(
                action="list", machine=None, hub_url="http://h/mcp",
                scan_dir=None, json=False, dry_run=False, paths=[],
            ),
            api=object(),
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "edge not reporting" in out
