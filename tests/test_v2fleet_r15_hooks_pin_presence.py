"""v2-fleet revision 15.1 falsifiers (Vortex project v2-fleet, line hub-master, built in mcp-hub's repo). FROZEN.

F51 "The fleet's client runs master" (T65, the presence check before the move) (mcp-hub 38600, 38783).
Base: r14's merge-prep bf6f73f9 (F50 built). The pin: hooks-pin 14baebaafa87ba5c04c59bbe812a676efb7229a6.

THIS FILE IS JUDGED OFF THE SEAT, where /work comes from `git archive` and has NO .git (b-153 failed on that).
It runs NO git. It reads only three things: the frozen snapshot contract/hooks-pin-tree.json (the pin's tests
tree as {"pin", "files": {path: blob}, "modes": {path: mode}}, written by the base from
`git ls-tree -r 14baebaa tests/`), the manifest contract/hooks-pin-presence.json (THE BUILD'S TO WRITE and
commit), and the tested tree's files. What needs history (the snapshot equals the pin; the ancestry of every
'fixed' and replaced_by commit) is tests/test_v2fleet_r15_hooks_pin_presence_git.py, run by the CI presence job.

The manifest's shape:

  {
    "pin": "<the full pin sha>",
    "ci_run": "<the CI run id the 'passes' verdicts come from (digits)>",
    "failed": ["<every red of that run, as a pytest node id under the pin's tests tree>", ...],
    "files": [
      {"path": "tests/<file>", "blob": "<that file's blob at the pin>", "verdict": "same"},
      {"path": ..., "blob": ..., "verdict": "passes", "run": "<ci_run>"},
      {"path": ..., "blob": ..., "verdict": "fixed",
       "fixed": [{"test": "<the pin's red node id>", "commit": "<full 40-hex fix commit>",
                  "green_test": "<a node id defined on the tested tree>"}]},
      {"path": ..., "blob": ..., "verdict": "set aside",
       "set_aside": [{"test": "<the pin's red node id>", "reason": "<why>",
                      "replaced_by": "<full 40-hex commit on master's line that replaces it>"}],
       "fixed": [...optional, as above]}
    ]
  }

What each check holds (T65's numbering):
  (0) THE SNAPSHOT: present, readable JSON, names the full pin, 146 files under tests/, each with a 40-hex blob
      and a git mode. Missing or unreadable FAILS, never skips.
  (1) EVERY FILE: the manifest's paths equal the snapshot's, none added, none missing; both name the full pin.
  (2) BLOBS: each entry's blob is the snapshot's blob for that path; every 'same' file on the tested tree has
      that blob (git's blob sha1, computed here with hashlib).
  (3) VERDICTS (shape only): exactly one of same, passes, fixed, set aside. 'passes' names the CI run. 'fixed'
      names a pin test of that file, a full 40-hex commit and a test defined on the tested tree. 'set aside'
      names each red pin test of that file, a non-empty reason and replaced_by, a full 40-hex commit.
  (4a) THE RECORDED RUN: its reds are covered exactly by set-aside and fixed tests, every set-aside test was red
      in it, and no red falls in a 'same' or 'passes' file.
  (4b) THE JOB: a CI job on a hosted runner puts the pin's tests tree in place by the full sha, runs pytest with
      --junitxml, hands the JUnit over through MCP_HUB_PRESENCE_JUNIT, and runs this file and
      tests/test_v2fleet_r15_hooks_pin_presence_git.py.
  (4c) THE LIVE RUN: when MCP_HUB_PRESENCE_JUNIT is set, the JUnit (both attempts judged together by the job)
      agrees: every red set aside, every fixed test green. Unset, this one check skips by name, the only skip.

Red on the base BY ABSENCE (no manifest, no presence job), so the mutants carry the falsifying weight.
"""
# ruff: noqa: E501 — the build's acceptance tests, kept as written
from __future__ import annotations

import hashlib
import json
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

PIN = "14baebaafa87ba5c04c59bbe812a676efb7229a6"
PIN_FILES = 146
ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "contract" / "hooks-pin-tree.json"
MANIFEST = ROOT / "contract" / "hooks-pin-presence.json"
CI = ROOT / ".github" / "workflows" / "ci.yml"
GIT_FILE = "tests/test_v2fleet_r15_hooks_pin_presence_git.py"
VERDICTS = ("same", "passes", "fixed", "set aside")
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
MODES = ("100644", "100755", "120000")


def git_blob_sha1(data: bytes) -> str:
    """git's blob id for these bytes, with no git: sha1 over 'blob <size>\\0' + the bytes."""
    return hashlib.sha1(b"blob %d\x00" % len(data) + data).hexdigest()


def _read_json(path: Path, what: str):
    assert path.is_file(), f"{path.relative_to(ROOT)} is absent: {what}. This check fails, never skips, without it."
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise AssertionError(f"{path.relative_to(ROOT)} is unreadable ({exc}): {what}") from None


def _snapshot() -> dict[str, str]:
    snap = _read_json(SNAPSHOT, "the frozen snapshot of the pin's tests tree")
    assert isinstance(snap, dict), "the snapshot must be a JSON object"
    assert snap.get("pin") == PIN, f"the snapshot must name the full pin {PIN}, got {snap.get('pin')!r}"
    files, modes = snap.get("files"), snap.get("modes")
    assert isinstance(files, dict) and files, "the snapshot needs a non-empty 'files' map {path: blob}"
    assert isinstance(modes, dict) and set(modes) == set(files), "the snapshot needs a 'modes' map over the same paths"
    bad = sorted(p for p, b in files.items()
                 if not (isinstance(p, str) and p.startswith("tests/") and isinstance(b, str) and FULL_SHA.match(b)
                         and modes.get(p) in MODES))
    assert not bad, f"snapshot rows that are not tests/<path> with a 40-hex blob and a git mode: {bad}"
    assert len(files) == PIN_FILES, f"the pin's tests tree has {PIN_FILES} files; the snapshot has {len(files)}"
    return dict(files)


def _manifest() -> dict:
    data = _read_json(MANIFEST, "no verdict is frozen for the pin's tests (the build writes and commits it)")
    assert isinstance(data, dict) and isinstance(data.get("files"), list), "manifest needs a 'files' list"
    return data


def _entries() -> dict[str, dict]:
    entries: dict[str, dict] = {}
    for e in _manifest()["files"]:
        assert isinstance(e, dict) and isinstance(e.get("path"), str), f"bad entry: {e!r}"
        assert e["path"] not in entries, f"{e['path']} is listed twice"
        entries[e["path"]] = e
    return entries


def _node_file(nodeid: str) -> str:
    return nodeid.split("::", 1)[0]


def _defines(text: str, nodeid: str) -> bool:
    name = nodeid.split("::")[-1].split("[", 1)[0]
    return re.search(rf"^\s*(async\s+)?def {re.escape(name)}\(", text, re.M) is not None


def _covered(entries: dict[str, dict]) -> tuple[set[str], set[str]]:
    set_aside: set[str] = set()
    fixed: set[str] = set()
    for e in entries.values():
        set_aside |= {s.get("test") for s in e.get("set_aside") or [] if isinstance(s, dict)}
        fixed |= {f.get("test") for f in e.get("fixed") or [] if isinstance(f, dict)}
    return set_aside, fixed


# --- (0) the snapshot -------------------------------------------------------------------------------------

def test_0_the_snapshot_is_present_and_names_the_pins_146_files():
    _snapshot()


# --- (1) every file ---------------------------------------------------------------------------------------

def test_1_every_pin_test_file_has_exactly_one_entry():
    snap = _snapshot()
    data = _manifest()
    listed = set(_entries())
    missing = sorted(set(snap) - listed)
    extra = sorted(listed - set(snap))
    assert not missing and not extra, f"manifest vs the snapshot of the pin's tests tree: missing {missing}, not on the pin {extra}"
    assert data.get("pin") == PIN, f"the manifest must name the full pin sha {PIN}"


# --- (2) blobs --------------------------------------------------------------------------------------------

def test_2_each_blob_is_the_snapshots_and_same_means_same_on_the_tested_tree():
    snap = _snapshot()
    entries = _entries()
    stale = sorted(p for p, e in entries.items() if p in snap and e.get("blob") != snap[p])
    assert not stale, f"blob is not the snapshot's blob for that path: {stale}"
    drifted = []
    for p, e in entries.items():
        if e.get("verdict") != "same":
            continue
        here = ROOT / p
        blob = git_blob_sha1(here.read_bytes()) if here.is_file() and not here.is_symlink() else ""
        if blob != e.get("blob"):
            drifted.append(p)
    assert not drifted, f"'same' but not byte-identical on the tested tree: {drifted}"


# --- (3) verdicts (shape only; ancestry is the git file's G2) ----------------------------------------------

def test_3_each_entry_has_one_verdict_with_its_evidence():
    data = _manifest()
    run = str(data.get("ci_run") or "")
    assert re.fullmatch(r"\d+", run), f"ci_run must name the CI run the passes come from, got {run!r}"
    bad = []
    for p, e in _entries().items():
        v = e.get("verdict")
        if v not in VERDICTS:
            bad.append(f"{p}: verdict {v!r} is not one of {VERDICTS}")
            continue
        if v == "passes" and str(e.get("run") or "") != run:
            bad.append(f"{p}: 'passes' must name the run {run}")
        if v == "fixed" and (not e.get("fixed") or e.get("set_aside")):
            bad.append(f"{p}: 'fixed' needs a non-empty fixed list and no set_aside")
        if v == "set aside" and not e.get("set_aside"):
            bad.append(f"{p}: 'set aside' needs a non-empty set_aside list")
        if v in ("same", "passes") and (e.get("fixed") or e.get("set_aside")):
            bad.append(f"{p}: '{v}' carries fixed/set_aside items")
        for f in e.get("fixed") or []:
            t, c, g = (f.get("test"), f.get("commit"), f.get("green_test")) if isinstance(f, dict) else (None, None, None)
            if not (isinstance(t, str) and "::" in t and _node_file(t) == p):
                bad.append(f"{p}: fixed item {f!r} must name a test of this file")
            if not (isinstance(c, str) and FULL_SHA.match(c)):
                bad.append(f"{p}: fixed item {f!r} must name a full 40-hex commit")
            gp = ROOT / _node_file(g) if isinstance(g, str) else None
            if not (gp and gp.is_file() and "::" in g and _defines(gp.read_text(encoding="utf-8"), g)):
                bad.append(f"{p}: fixed item {f!r} must name a green_test defined on the tested tree")
        for s in e.get("set_aside") or []:
            t, r, c = (s.get("test"), s.get("reason"), s.get("replaced_by")) if isinstance(s, dict) else (None, None, None)
            if not (isinstance(t, str) and "::" in t and _node_file(t) == p):
                bad.append(f"{p}: set-aside item {s!r} must name a test of this file")
            if not (isinstance(r, str) and r.strip()):
                bad.append(f"{p}: set-aside item {t!r} has no reason")
            if not (isinstance(c, str) and FULL_SHA.match(c)):
                bad.append(f"{p}: set-aside item {t!r} must name replaced_by as a full 40-hex commit")
    assert not bad, "\n".join(bad)


# --- (4) the run ------------------------------------------------------------------------------------------

def _agrees(entries: dict[str, dict], reds: set[str], *, live: bool) -> list[str]:
    set_aside, fixed = _covered(entries)
    problems = []
    allowed = set_aside if live else set_aside | fixed
    unlisted = sorted(reds - allowed)
    if unlisted:
        problems.append(f"reds not set aside{'' if live else ' or fixed'} by name: {unlisted}")
    if live:
        still = sorted(reds & fixed)
        if still:
            problems.append(f"'fixed' but still red: {still}")
    else:
        stale = sorted(set_aside - reds)
        if stale:
            problems.append(f"set aside but not red in the run: {stale}")
    for r in sorted(reds):
        e = entries.get(_node_file(r))
        if e is None:
            problems.append(f"red outside the pin's tests tree: {r}")
        elif e.get("verdict") in ("same", "passes"):
            problems.append(f"red in a '{e.get('verdict')}' file: {r}")
    return problems


def test_4a_the_recorded_run_agrees_with_the_manifest():
    data = _manifest()
    failed = data.get("failed")
    assert isinstance(failed, list) and all(isinstance(f, str) and "::" in f for f in failed), \
        "'failed' must list the run's reds as node ids (an empty list if it had none)"
    problems = _agrees(_entries(), set(failed), live=False)
    assert not problems, "\n".join(problems)


def _ci_jobs() -> dict[str, str]:
    assert CI.is_file(), f"{CI.relative_to(ROOT)} is absent"
    jobs: dict[str, list[str]] = {}
    current = None
    in_jobs = False
    for line in CI.read_text(encoding="utf-8").splitlines():
        if re.match(r"^jobs:\s*$", line):
            in_jobs = True
            continue
        if in_jobs and re.match(r"^\S", line):
            in_jobs = False
        if not in_jobs:
            continue
        m = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
        if m:
            current = m.group(1)
            jobs[current] = []
        elif current:
            jobs[current].append(line)
    return {k: "\n".join(v) for k, v in jobs.items()}


def test_4b_a_hosted_ci_job_runs_the_pins_tests_and_both_frozen_files():
    hits = []
    for name, body in _ci_jobs().items():
        swaps_tests = re.search(rf"{PIN}\b[^\n]*\btests\b", body) is not None
        if (swaps_tests and "--junitxml" in body and "MCP_HUB_PRESENCE_JUNIT" in body
                and "tests/test_v2fleet_r15_hooks_pin_presence.py" in body and GIT_FILE in body):
            runs_on = re.search(r"^\s*runs-on:\s*(.+)$", body, re.M)
            assert runs_on and "self-hosted" not in runs_on.group(1), \
                f"job {name} must run on a hosted runner, outside any seat host"
            hits.append(name)
    assert hits, (
        "no CI job runs the pin's tests tree over this src: a job must put the pin's tests/ in place by the "
        f"full sha ({PIN} ... tests), run pytest with --junitxml, hand the JUnit to "
        "tests/test_v2fleet_r15_hooks_pin_presence.py through MCP_HUB_PRESENCE_JUNIT, and run "
        f"{GIT_FILE}"
    )


def _junit_reds(path: Path, entries: dict[str, dict]) -> tuple[set[str], int]:
    root = ET.parse(path).getroot()
    reds: set[str] = set()
    cases = 0
    paths = set(entries)
    for case in root.iter("testcase"):
        cases += 1
        if case.find("failure") is None and case.find("error") is None:
            continue
        parts = (case.get("classname") or "").split(".")
        node = None
        for k in range(len(parts), 0, -1):
            cand = "/".join(parts[:k]) + ".py"
            if cand in paths:
                node = "::".join([cand, *parts[k:], case.get("name") or ""])
                break
        reds.add(node or f"{case.get('classname')}::{case.get('name')}")
    return reds, cases


def test_4c_the_live_run_agrees_with_the_manifest():
    junit = os.environ.get("MCP_HUB_PRESENCE_JUNIT")
    if not junit:
        pytest.skip("MCP_HUB_PRESENCE_JUNIT unset: the live presence run is CI's step; the recorded run is checked above")
    entries = _entries()
    reds, cases = _junit_reds(Path(junit), entries)
    assert cases > 0, f"{junit} holds no test cases"
    problems = _agrees(entries, reds, live=True)
    assert not problems, "\n".join(problems)
