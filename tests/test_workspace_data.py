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


def test_a_DELETED_local_workspace_is_not_reported_as_on_disk(tmp_path):
    """🔴 The hub REMEMBERS what a machine once reported, so a file deleted
    since is still in `discovered` — and the row used to take `on_disk: True`
    straight from that record.

    Measured 2026-08-08: `showcase.code-workspace` was deleted, `find ~`
    confirmed no copy anywhere, and `mcp-hub workspaces list` still printed
    `✔ disk`. The manager asserted a file that was not there, which is the one
    thing this column exists to answer.

    The clause that keeps local enumeration authoritative only ran when the
    file still EXISTED; absence fell through to the branch that trusted the
    hub. Absence is exactly where freshness matters most.
    """
    registry = {"definitions": [],
                "discovered": [{"machine": "thisbox",
                                "path": "/home/me/Projects/gone.code-workspace",
                                "registered": False}]}
    rows = collect_workspaces(_FakeAPI(registry), [tmp_path], "thisbox")
    gone = [r for r in rows["rows"] if r["name"] == "gone"]
    assert gone, "the row vanished entirely — it should be shown, just not as on-disk"
    assert gone[0]["on_disk"] is False, (
        "reported a locally-deleted workspace as present on disk, on the "
        "authority of the hub's memory of it")


def test_a_REMOTE_machines_workspace_still_trusts_the_hub(tmp_path):
    """The positive control, and it is what stops the fix going too far: we
    cannot stat another box's disk, so for a REMOTE machine the hub's record is
    the best evidence there is. Without this, 'ignore the hub' would pass the
    test above while blanking every remote row in the fleet view."""
    registry = {"definitions": [],
                "discovered": [{"machine": "otherbox",
                                "path": "/home/me/Projects/remote.code-workspace",
                                "registered": True}]}
    rows = collect_workspaces(_FakeAPI(registry), [tmp_path], "thisbox")
    remote = [r for r in rows["rows"] if r["name"] == "remote"]
    assert remote and remote[0]["on_disk"] is True, (
        "blanked a remote machine's workspace — this machine's scan says "
        "nothing about another box's disk")
