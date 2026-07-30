"""Gate for the edge realizer's enumeration guard.

`squad ls` failing used to fall through as an EMPTY dict, which is not "nothing
is enrolled" — it is "I did not look". The consequences compound: `plan()` sees
no materialized seat and schedules a `materialize` for every placement, then
the pass pushes observed reports describing state it never read, and prints a
cheerful summary. The evidence contract's first rule is that an assertion over
an empty set must be a hard error; this is that rule, applied to the thing that
motivated it.

Discovered in production 2026-07-30: `squad` is not on PATH in a
non-interactive ssh shell, and the heal timer runs in exactly that kind of
shell.
"""

from __future__ import annotations

import pytest

from mcp_hub.edge import EnumerationFailed, edge_apply

PLACEMENTS = [
    {"id": "p1", "seat": "alpha", "substrate": "worktree", "desired": "running",
     "seat_spec": {"folder": "/w/alpha", "repo": "org/alpha"}},
]


class _Api:
    def __init__(self, placements=None):
        self._p = placements if placements is not None else PLACEMENTS
        self.observed = []
        self.status = []

    def pull_placements(self, machine):
        return list(self._p)

    def push_observed(self, pid, report):
        self.observed.append((pid, report))
        return {}

    def push_status(self, machine, payload):
        self.status.append((machine, payload))


def _runner(rc, out="", record=None):
    def run(cmd, cwd=None):
        if record is not None:
            record.append(cmd)
        if cmd[:2] == ["squad", "ls"]:
            return rc, out
        return 0, ""
    return run


LS_OK = "alpha up\nbeta down\n"


def test_a_failed_enumeration_refuses_the_whole_pass(tmp_path):
    api = _Api()
    with pytest.raises(EnumerationFailed) as e:
        edge_apply(api, machine="box", runner=_runner(127, "squad: not found"),
                   scan_dirs=[tmp_path], seeder=lambda folder: None)
    assert "squad ls" in str(e.value)
    assert "127" in str(e.value)
    assert "squad: not found" in str(e.value)


def test_nothing_is_reported_when_enumeration_failed(tmp_path):
    """The dangerous half: a blind pass must not push observations."""
    api = _Api()
    with pytest.raises(EnumerationFailed):
        edge_apply(api, machine="box", runner=_runner(1, "boom"),
                   scan_dirs=[tmp_path], seeder=lambda folder: None)
    assert api.observed == []
    assert api.status == []


def test_no_squad_command_runs_when_enumeration_failed(tmp_path):
    """It must refuse BEFORE acting — a blind plan schedules a materialize
    for every placement, which would create seats that already exist."""
    ran: list[list[str]] = []
    api = _Api()
    with pytest.raises(EnumerationFailed):
        edge_apply(api, machine="box", runner=_runner(3, "nope", record=ran),
                   scan_dirs=[tmp_path], seeder=lambda folder: None)
    assert ran == [["squad", "ls"]]        # looked once, acted never


def test_a_successful_but_EMPTY_enumeration_is_still_a_fact(tmp_path):
    """Distinct from the failure: rc=0 with no rows genuinely means nothing
    is enrolled, and the pass must proceed normally."""
    api = _Api()
    summary = edge_apply(api, machine="box", runner=_runner(0, ""),
                         scan_dirs=[tmp_path], seeder=lambda folder: None)
    assert summary["placements"] == 1
    assert api.status                       # it DID report
    assert summary["observed_reported"] == 1


def test_a_healthy_pass_is_unaffected(tmp_path):
    api = _Api()
    summary = edge_apply(api, machine="box", runner=_runner(0, LS_OK),
                         scan_dirs=[tmp_path], seeder=lambda folder: None)
    assert summary["observed_reported"] == 1
    assert api.status[0][0] == "box"


def test_zero_placements_still_reports_workspaces(tmp_path):
    """The rollout's actual use: no placements, but the disk scan is the
    whole point of running it."""
    (tmp_path / "solo.code-workspace").write_text('{"folders": [{"path": "/a"}]}')
    api = _Api(placements=[])
    summary = edge_apply(api, machine="box", runner=_runner(0, LS_OK),
                         scan_dirs=[tmp_path], seeder=lambda folder: None)
    assert summary["workspaces_reported"] == 1
    assert api.status[0][1]["workspaces"][0]["path"].endswith("solo.code-workspace")


class TestToolResolution:
    def test_a_missing_tool_yields_a_diagnosis_not_a_traceback(self, monkeypatch):
        """The runner must never raise FileNotFoundError at the caller."""
        from mcp_hub import cli

        monkeypatch.setattr(cli.shutil, "which", lambda n: None)
        monkeypatch.setattr(cli.os.path, "exists", lambda p: False)

        import argparse

        captured = {}

        class _Api2:
            def pull_placements(self, m):
                return []

        def fake_edge_apply(api, machine, runner, scan_dirs, **kw):
            captured["rc"], captured["out"] = runner(["squad", "ls"])
            return {"placements": 0, "actions": [], "observed_reported": 0,
                    "workspaces_reported": 0}

        monkeypatch.setattr("mcp_hub.edge.edge_apply", fake_edge_apply)
        monkeypatch.setattr("mcp_hub.edge.HubAPI", lambda **kw: _Api2())
        cli.edge_command(argparse.Namespace(
            action="apply", hub_url="http://h/mcp", machine="box",
            token="t", scan_dir=None, dry_run=False,
        ))
        assert captured["rc"] == 127
        assert "not found" in captured["out"]
        assert "~/.local/bin" in captured["out"]

    def test_the_fixed_install_location_wins_over_PATH(self, monkeypatch):
        from mcp_hub import cli

        monkeypatch.setattr(cli.os.path, "exists", lambda p: p == cli.SQUAD_BIN)
        monkeypatch.setattr(cli.shutil, "which", lambda n: "/usr/bin/squad")
        assert cli._resolve_tool("squad") == cli.SQUAD_BIN

    def test_PATH_is_the_fallback_when_the_fixed_location_is_absent(
        self, monkeypatch
    ):
        from mcp_hub import cli

        monkeypatch.setattr(cli.os.path, "exists", lambda p: False)
        monkeypatch.setattr(cli.shutil, "which", lambda n: "/usr/bin/squad")
        assert cli._resolve_tool("squad") == "/usr/bin/squad"

    def test_an_unknown_tool_is_looked_up_on_PATH_only(self, monkeypatch):
        from mcp_hub import cli

        monkeypatch.setattr(cli.shutil, "which", lambda n: f"/usr/bin/{n}")
        assert cli._resolve_tool("git") == "/usr/bin/git"
