"""v2-fleet revision 15.1, the CI-only half of T65 (F51 "The fleet's client runs master"). FROZEN.

Run by the CI presence job (and the CI test job), which check out with full history and fetch hooks-pin. It is
NOT the feature's command: the off-seat check, tests/test_v2fleet_r15_hooks_pin_presence.py, runs no git; what
needs history is here.

  (G1) SNAPSHOT IS THE PIN: contract/hooks-pin-tree.json, rendered as `git ls-tree -r` lines
       ("<mode> blob <blob>\\t<path>", in the snapshot's order), equals `git ls-tree -r 14baebaa tests/`
       byte for byte: path for path, mode for mode, blob for blob.
  (G2) ANCESTRY: every 'fixed' commit in contract/hooks-pin-presence.json is an ancestor of HEAD, and every
       replaced_by commit is on HEAD's line and not on the pin's. With no manifest there is nothing to trace
       here; the off-seat file's (1) is red on that absence.

Both FAIL, never skip, when the pin's objects (or git itself) are absent.
"""
# ruff: noqa: E501 — the build's acceptance tests, kept as written
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

PIN = "14baebaafa87ba5c04c59bbe812a676efb7229a6"
ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "contract" / "hooks-pin-tree.json"
MANIFEST = ROOT / "contract" / "hooks-pin-presence.json"
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")


def _git(*args: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, check=False)
    except OSError as exc:
        raise AssertionError(f"git could not run ({exc}); this CI-only check fails, never skips, without it") from None


def _pin_present() -> None:
    probe = _git("cat-file", "-e", f"{PIN}^{{commit}}")
    assert probe.returncode == 0, (
        f"the pin {PIN} is not in this repo's objects ({probe.stderr.decode(errors='replace').strip()}); "
        "fetch it (git fetch origin hooks-pin). This check fails, never skips, without it."
    )


def _is_ancestor(commit: str, of: str) -> bool:
    return _git("merge-base", "--is-ancestor", commit, of).returncode == 0


def _commit_exists(commit: str) -> bool:
    return _git("cat-file", "-e", f"{commit}^{{commit}}").returncode == 0


def test_g1_the_snapshot_is_the_pins_tests_tree_byte_for_byte():
    _pin_present()
    assert SNAPSHOT.is_file(), f"{SNAPSHOT.relative_to(ROOT)} is absent"
    snap = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    assert snap.get("pin") == PIN, f"the snapshot must name the full pin {PIN}"
    files, modes = snap.get("files") or {}, snap.get("modes") or {}
    assert set(modes) == set(files), "the snapshot's modes and files must cover the same paths"
    rendered = "".join(f"{modes[p]} blob {b}\t{p}\n" for p, b in files.items()).encode("utf-8")
    listed = _git("ls-tree", "-r", PIN, "tests/")
    assert listed.returncode == 0 and listed.stdout, f"git ls-tree -r {PIN} tests/ failed: {listed.stderr!r}"
    if rendered != listed.stdout:
        want = set(listed.stdout.decode().splitlines())
        have = set(rendered.decode().splitlines())
        raise AssertionError(
            f"the snapshot is not the pin's tests tree: missing {sorted(want - have)[:5]}, "
            f"not on the pin {sorted(have - want)[:5]}, order differs: {want == have}")


def test_g2_fixed_and_replaced_by_commits_sit_on_the_right_lines():
    _pin_present()
    if not MANIFEST.is_file():
        return  # nothing to trace; the off-seat file's (1) is red on the absence
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    head = _git("rev-parse", "HEAD").stdout.decode().strip()
    assert FULL_SHA.match(head), "HEAD did not resolve"
    bad = []
    for e in data.get("files") or []:
        p = e.get("path") if isinstance(e, dict) else e
        for f in (e.get("fixed") or []) if isinstance(e, dict) else []:
            c = f.get("commit") if isinstance(f, dict) else None
            if not (isinstance(c, str) and FULL_SHA.match(c) and _commit_exists(c) and _is_ancestor(c, head)):
                bad.append(f"{p}: fixed commit {c!r} is not an ancestor of HEAD {head}")
        for s in (e.get("set_aside") or []) if isinstance(e, dict) else []:
            c = s.get("replaced_by") if isinstance(s, dict) else None
            if not (isinstance(c, str) and FULL_SHA.match(c) and _commit_exists(c)):
                bad.append(f"{p}: replaced_by {c!r} is not a commit in this repo")
            elif not _is_ancestor(c, head) or _is_ancestor(c, PIN):
                bad.append(f"{p}: replaced_by {c} must be on HEAD's line and not on the pin's")
    assert not bad, "\n".join(bad)
