"""Gate for workspace_data.collect_workspaces — the manager's merged view.

Three truth columns per workspace, each from its honest source: registered
(hub API), on disk (local scan + fleet discoveries), open now (board
presence). The hub being unreachable must DEGRADE (local scan still works,
with the gap named) — never crash, never silently pretend fleet coverage.
"""

from __future__ import annotations

import json
from pathlib import Path

from mcp_hub.workspace_data import collect_workspaces


class _FakeAPI:
    def __init__(self, registry=None, fail=False):
        self._registry = registry or {"definitions": [], "discovered": []}
        self._fail = fail

    def get_registry(self):
        if self._fail:
            raise ConnectionError("hub unreachable")
        return self._registry


def _local(tmp_path: Path, name: str, folders: int = 1) -> Path:
    f = tmp_path / f"{name}.code-workspace"
    f.write_text(json.dumps({"folders": [{"path": f"p{i}"} for i in range(folders)]}))
    return f


class TestCollect:
    def test_merges_local_and_registry(self, tmp_path: Path):
        _local(tmp_path, "alpha", 2)
        api = _FakeAPI(
            {
                "definitions": [
                    {"id": 1, "name": "alpha", "machine": "here", "listings": [],
                     "squad": "runtime", "on_disk": True}
                ],
                "discovered": [
                    {"machine": "dev-vm-1", "path": "/x/beta.code-workspace",
                     "folders": 3, "error": "", "open_now": False,
                     "registered": False, "reported_at": 1.0}
                ],
            }
        )
        out = collect_workspaces(api, scan_dirs=[tmp_path], this_machine="here")
        assert out["hub_reachable"] is True
        rows = {r["name"]: r for r in out["rows"]}
        # Local file + matching definition → one row, both columns true.
        assert rows["alpha"]["on_disk"] is True
        assert rows["alpha"]["registered"] is True
        assert rows["alpha"]["machine"] == "here"
        # Fleet discovery from another machine appears without a local file.
        assert rows["beta"]["machine"] == "dev-vm-1"
        assert rows["beta"]["registered"] is False

    def test_hub_unreachable_degrades_to_local_with_gap_named(self, tmp_path: Path):
        _local(tmp_path, "solo")
        out = collect_workspaces(
            _FakeAPI(fail=True), scan_dirs=[tmp_path], this_machine="here"
        )
        assert out["hub_reachable"] is False
        assert "unreachable" in out["note"]
        rows = {r["name"]: r for r in out["rows"]}
        assert rows["solo"]["on_disk"] is True
        # Registration status is UNKNOWN when the hub is away — never
        # defaulted to False, which would read as "feral" and mislead.
        assert rows["solo"]["registered"] is None

    def test_a_hub_that_answered_makes_an_unmatched_file_FERAL_not_unknown(
        self, tmp_path: Path
    ):
        """The drift state this column exists for must be reachable.

        A hub that answered has told us everything it knows, so a local file
        it has no definition for is unregistered — not "unknown". Leaving it
        None meant `✗ NOT REGISTERED` could never appear on any machine that
        had not run `edge apply`, i.e. on any machine in the fleet.
        """
        _local(tmp_path, "feral")
        out = collect_workspaces(_FakeAPI(), scan_dirs=[tmp_path], this_machine="here")
        assert out["hub_reachable"] is True
        assert out["rows"][0]["registered"] is False

    def test_but_an_ABSENT_hub_still_leaves_the_question_open(self, tmp_path: Path):
        """The other half of the same rule: only an answer can convict."""
        _local(tmp_path, "feral")
        out = collect_workspaces(
            _FakeAPI(fail=True), scan_dirs=[tmp_path], this_machine="here"
        )
        assert out["rows"][0]["registered"] is None

    def test_an_operator_ready_reason_is_shown_VERBATIM_not_called_unreachable(
        self, tmp_path: Path
    ):
        """A hub that answers is not an outage, and must not be called one.

        `ApiUnavailable` messages already name their own fix. Wrapping them in
        "hub registry unreachable (...)" is what sent the operator hunting for
        a downed hub when the real cause was a missing local token.
        """
        from mcp_hub.operator_api import ApiUnavailable

        class _Off:
            def get_registry(self):
                raise ApiUnavailable(
                    "the hub's management API is disabled"
                    " (MCP_HUB_API_TOKEN is not set on the hub)"
                )

        _local(tmp_path, "solo")
        out = collect_workspaces(_Off(), scan_dirs=[tmp_path], this_machine="here")
        assert out["hub_reachable"] is False
        assert out["note"] == (
            "the hub's management API is disabled"
            " (MCP_HUB_API_TOKEN is not set on the hub) — local scan only"
        )
        assert "unreachable" not in out["note"]
        # Degrading is unchanged: the scan still answers, registration unknown.
        rows = {r["name"]: r for r in out["rows"]}
        assert rows["solo"]["on_disk"] is True
        assert rows["solo"]["registered"] is None

    def test_local_file_wins_over_stale_fleet_discovery_of_same_machine(
        self, tmp_path: Path
    ):
        _local(tmp_path, "gamma", 4)
        api = _FakeAPI(
            {
                "definitions": [],
                "discovered": [
                    {"machine": "here", "path": str(tmp_path / "gamma.code-workspace"),
                     "folders": 1, "error": "", "open_now": True,
                     "registered": False, "reported_at": 1.0}
                ],
            }
        )
        out = collect_workspaces(api, scan_dirs=[tmp_path], this_machine="here")
        rows = {r["name"]: r for r in out["rows"]}
        # Local enumeration is fresher than the hub's copy of this machine:
        # folder count comes from disk; open_now survives from the registry.
        assert rows["gamma"]["folders"] == 4
        assert rows["gamma"]["open_now"] is True

    def test_ghost_definition_flagged(self, tmp_path: Path):
        api = _FakeAPI(
            {
                "definitions": [
                    {"id": 2, "name": "ghost", "machine": "here", "listings": [],
                     "squad": "", "on_disk": False}
                ],
                "discovered": [],
            }
        )
        out = collect_workspaces(api, scan_dirs=[tmp_path], this_machine="here")
        rows = {r["name"]: r for r in out["rows"]}
        assert rows["ghost"]["registered"] is True
        assert rows["ghost"]["on_disk"] is False
