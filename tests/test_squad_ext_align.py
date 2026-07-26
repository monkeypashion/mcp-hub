"""`squad ext-align` — keep the cockpit extension's installed dir name in step.

ONE RULE, measured: the installed directory must be named
`<id>-<manifest version>`. VSCode keys its extension cache on the FOLDER NAME,
not on the version inside package.json, so a folder left behind a bumped
manifest means the manifest is never re-read and every new menu entry is
silently invisible — no error anywhere.

The two field recoveries only look like different rules:
  * dev-vm-1 2026-07-25 — index 0.17.0, dir 0.14.0, manifest 0.17.0. Renaming to
    0.17.0 fixed it. Read at the time as "rename to what the index asks for",
    but the manifest agreed, so that incident never discriminated between the
    two rules.
  * fireblade-wsl 2026-07-26 — index 0.17.0, dir 0.17.0, manifest 0.20.0. Only
    the manifest rule explains this one, and the old "version skew is benign"
    branch is precisely what kept that box stale through four bumps.

Both are asserted below, which is the point: one rule has to satisfy both.

These tests run a COPY of the script beside a synthetic package.json, because
`ext_align` resolves the manifest relative to `$0`. Reading the real repo
manifest would make every expectation drift with the next version bump.
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


def _script(tmp_path: pathlib.Path, manifest_version: str) -> pathlib.Path:
    """A copy of squad whose sibling manifest we control."""
    root = tmp_path / "repo"
    (root / "vscode-squad-terminals").mkdir(parents=True)
    (root / "vscode-squad-terminals" / "package.json").write_text(
        json.dumps({"name": "squad-terminals", "version": manifest_version}),
        encoding="utf-8",
    )
    script = root / "squad"
    script.write_text(SQUAD.read_text(encoding="utf-8"), encoding="utf-8")
    return script


def _run(home: pathlib.Path, script: pathlib.Path) -> subprocess.CompletedProcess:
    env = dict(os.environ, HOME=str(home))
    return subprocess.run(
        ["bash", str(script), "ext-align"],
        capture_output=True, text=True, timeout=60, env=env,
    )


def _install(home: pathlib.Path, *, dir_version: str, index_version: str) -> pathlib.Path:
    """Install dir named <dir_version>, index pointing at <index_version>."""
    base = home / ".vscode-server" / "extensions"
    base.mkdir(parents=True)
    (base / f"{EXT_ID}-{dir_version}").symlink_to("/tmp")   # the real install is a symlink
    (base / "extensions.json").write_text(json.dumps([{
        "identifier": {"id": EXT_ID},
        "version": index_version,
        "location": {"path": str(base / f"{EXT_ID}-{index_version}")},
    }]), encoding="utf-8")
    return base


def test_renames_forward_to_the_manifest(tmp_path):
    """fireblade-wsl's measured case — a RESOLVING index and a stale folder.

    The old code called this benign and took no action, which is exactly why the
    operator's menus sat four bumps out of date with no error to show for it.
    """
    base = _install(tmp_path, dir_version="0.17.0", index_version="0.17.0")
    res = _run(tmp_path, _script(tmp_path, "0.20.0"))
    assert res.returncode == 0, res.stderr
    assert (base / f"{EXT_ID}-0.20.0").exists(), "folder must follow the manifest"
    assert not (base / f"{EXT_ID}-0.17.0").exists()
    assert "Reload Window" in res.stdout, "a rename is inert until the window reloads"


def test_repairs_dev_vm_1s_dead_cockpit(tmp_path):
    """The orphaned-index emergency: index 0.17.0, disk 0.14.0, manifest 0.17.0.

    The proven recovery, preserved. Renaming to the manifest lands on the same
    name the index wants, so the one rule covers this without a second branch.
    """
    base = _install(tmp_path, dir_version="0.14.0", index_version="0.17.0")
    res = _run(tmp_path, _script(tmp_path, "0.17.0"))
    assert res.returncode == 0, res.stderr
    assert (base / f"{EXT_ID}-0.17.0").exists(), "install must answer to the index again"
    assert not (base / f"{EXT_ID}-0.14.0").exists()
    idx = json.loads((base / "extensions.json").read_text())
    assert pathlib.Path(idx[0]["location"]["path"]).exists(), "the index must resolve"


def test_aligned_install_is_left_alone_silently(tmp_path):
    base = _install(tmp_path, dir_version="0.20.0", index_version="0.20.0")
    res = _run(tmp_path, _script(tmp_path, "0.20.0"))
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "", "heal must not report noise on a healthy box"
    assert (base / f"{EXT_ID}-0.20.0").exists()


def test_is_idempotent_and_never_oscillates(tmp_path):
    """The hazard of two rules with different targets: rename back and forth.

    Run 1 renames toward the manifest, orphaning the index. Run 2 must NOT then
    rename back toward the index — that is an infinite flip, and every second
    heal pass would leave the operator on stale menus.
    """
    base = _install(tmp_path, dir_version="0.17.0", index_version="0.17.0")
    script = _script(tmp_path, "0.20.0")
    assert "renamed" in _run(tmp_path, script).stdout

    second = _run(tmp_path, script)
    assert second.returncode == 0
    assert "renamed" not in second.stdout
    assert (base / f"{EXT_ID}-0.20.0").exists(), "must stay on the manifest name"
    assert not (base / f"{EXT_ID}-0.17.0").exists()
    # ...and it should say why the index still looks wrong, rather than acting on it
    assert "Reload Window to re-index" in second.stdout


def test_two_installs_are_reported_not_resolved(tmp_path):
    """Deleting an install is not a repair a heal pass gets to make."""
    base = _install(tmp_path, dir_version="0.17.0", index_version="0.17.0")
    (base / f"{EXT_ID}-0.20.0").symlink_to("/tmp")
    res = _run(tmp_path, _script(tmp_path, "0.20.0"))
    assert res.returncode == 0, res.stderr
    assert "two installs present" in res.stdout
    assert (base / f"{EXT_ID}-0.17.0").exists(), "neither install may be removed"
    assert (base / f"{EXT_ID}-0.20.0").exists()


def test_unparseable_manifest_never_guesses_a_name(tmp_path):
    """A guessed rename is the one move that manufactures a dead cockpit."""
    base = _install(tmp_path, dir_version="0.17.0", index_version="0.17.0")
    script = _script(tmp_path, "0.20.0")
    (script.parent / "vscode-squad-terminals" / "package.json").write_text(
        "{ not json", encoding="utf-8")
    res = _run(tmp_path, script)
    assert res.returncode == 0, res.stderr
    assert (base / f"{EXT_ID}-0.17.0").exists(), "must not touch anything when blind"


def test_no_cockpit_install_is_a_silent_noop(tmp_path):
    """Most machines have no cockpit extension; heal must not report noise."""
    (tmp_path / ".vscode-server" / "extensions").mkdir(parents=True)
    res = _run(tmp_path, _script(tmp_path, "0.20.0"))
    assert res.returncode == 0
    assert res.stdout.strip() == ""


def test_unreadable_index_does_not_fail_the_pass(tmp_path):
    """VSCode owns extensions.json; an unreadable one usually means mid-write."""
    base = tmp_path / ".vscode-server" / "extensions"
    base.mkdir(parents=True)
    (base / f"{EXT_ID}-0.14.0").symlink_to("/tmp")
    (base / "extensions.json").write_text("{ not json", encoding="utf-8")
    res = _run(tmp_path, _script(tmp_path, "0.20.0"))
    assert res.returncode == 0, res.stderr
    assert (base / f"{EXT_ID}-0.14.0").exists(), "don't race a live writer for no gain"


def test_missing_extensions_dir_is_a_noop(tmp_path):
    res = _run(tmp_path, _script(tmp_path, "0.20.0"))
    assert res.returncode == 0
    assert res.stdout.strip() == ""
