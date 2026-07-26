"""`squad ext-align` — repair a cockpit killed by an orphaned extension index.

Background (2026-07-25/26, third occurrence): VSCode's extension index records
`id-version` plus a location path. If that path does not exist, VSCode reports
"invalid extensions detected", never activates the extension, and the terminal
panel simply does not open — it reads as a cockpit bug, not a version bug.

Two rules encoded here, both measured rather than assumed:
  * MISSING index path is the emergency -> rename the real install to the name
    the index asks for. That direction is what actually recovered dev-vm-1.
  * VERSION SKEW with a resolving index is BENIGN -> do nothing. VSCode's own
    cache-invalidation event showed the manifest moving 0.16.0 -> 0.17.0 with
    the folder unchanged and both entries valid, so editing package.json is
    enough on its own to force a re-read. Renaming toward the manifest would
    ORPHAN the index path — i.e. it would CAUSE the emergency above.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess

import pytest

SQUAD = pathlib.Path(__file__).resolve().parents[1] / "squad" / "squad"
EXT_ID = "monkeypashion.squad-terminals"

pytestmark = pytest.mark.skipif(not SQUAD.exists(), reason="squad script not present")


def _run(home: pathlib.Path) -> subprocess.CompletedProcess:
    env = dict(os.environ, HOME=str(home))
    return subprocess.run(
        ["bash", str(SQUAD), "ext-align"],
        capture_output=True, text=True, timeout=60, env=env,
    )


def _setup(home: pathlib.Path, *, dir_version: str, index_version: str) -> pathlib.Path:
    """Install dir named <dir_version>, index pointing at <index_version>."""
    base = home / ".vscode-server" / "extensions"
    base.mkdir(parents=True)
    installed = base / f"{EXT_ID}-{dir_version}"
    installed.symlink_to("/tmp")            # the real install is a symlink into the repo
    (base / "extensions.json").write_text(json.dumps([{
        "identifier": {"id": EXT_ID},
        "version": index_version,
        "location": {"path": str(base / f"{EXT_ID}-{index_version}")},
    }]), encoding="utf-8")
    return base


def test_repairs_orphaned_index_by_renaming_toward_it(tmp_path):
    """dev-vm-1's exact dead-cockpit state: index wants 0.17.0, disk has 0.14.0."""
    base = _setup(tmp_path, dir_version="0.14.0", index_version="0.17.0")
    res = _run(tmp_path)
    assert res.returncode == 0, res.stderr
    assert "REPAIRED dead cockpit" in res.stdout
    assert (base / f"{EXT_ID}-0.17.0").exists(), "install must now answer to the index"
    assert not (base / f"{EXT_ID}-0.14.0").exists()
    # the whole point: the path the index names now resolves
    idx = json.loads((base / "extensions.json").read_text())
    assert pathlib.Path(idx[0]["location"]["path"]).exists()


def test_benign_version_skew_is_left_alone(tmp_path):
    """A resolving index must never be 'fixed' — that would orphan it."""
    base = _setup(tmp_path, dir_version="0.16.0", index_version="0.16.0")
    res = _run(tmp_path)
    assert res.returncode == 0, res.stderr
    assert "REPAIRED" not in res.stdout
    assert (base / f"{EXT_ID}-0.16.0").exists(), "must not rename a healthy install"
    idx = json.loads((base / "extensions.json").read_text())
    assert pathlib.Path(idx[0]["location"]["path"]).exists()


def test_repair_is_idempotent(tmp_path):
    base = _setup(tmp_path, dir_version="0.14.0", index_version="0.17.0")
    assert "REPAIRED" in _run(tmp_path).stdout
    second = _run(tmp_path)
    assert second.returncode == 0
    assert "REPAIRED" not in second.stdout
    assert (base / f"{EXT_ID}-0.17.0").exists()


def test_no_cockpit_install_is_a_silent_noop(tmp_path):
    """Most machines have no cockpit extension; heal must not report noise."""
    (tmp_path / ".vscode-server" / "extensions").mkdir(parents=True)
    res = _run(tmp_path)
    assert res.returncode == 0
    assert res.stdout.strip() == ""


def test_unreadable_index_does_not_fail_the_pass(tmp_path):
    """VSCode owns extensions.json; garbage in it is not ours to fix — fail soft."""
    base = tmp_path / ".vscode-server" / "extensions"
    base.mkdir(parents=True)
    (base / f"{EXT_ID}-0.14.0").symlink_to("/tmp")
    (base / "extensions.json").write_text("{ not json", encoding="utf-8")
    res = _run(tmp_path)
    assert res.returncode == 0, res.stderr
    assert (base / f"{EXT_ID}-0.14.0").exists(), "must not touch anything when blind"


def test_missing_extensions_dir_is_a_noop(tmp_path):
    res = _run(tmp_path)
    assert res.returncode == 0
    assert res.stdout.strip() == ""
