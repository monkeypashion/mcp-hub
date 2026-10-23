"""v2-fleet r12 F46 / T60 (j6-the-tests-pass-the-linter-too): the linter that guards the code
guards the tests too, and finds nothing in either. FROZEN.

  (1) `python -m ruff check src tests --no-cache` exits 0 with no finding.
  (2) CI SEES IT: ci.yml's Ruff step installs ruff at exactly the version the dev extra pins
      (read from pyproject.toml) and runs `ruff check src tests`.
  (3) GUARD, no escape by configuration: [tool.ruff] is exactly {line-length: 100} and
      [tool.ruff.lint] exactly {select: [E, F, I, W]}; no ruff.toml or .ruff.toml; no per-line
      noqa in src or tests names E501 or I001; every '# ruff: noqa' comment is the house line
      in a tests/test_v2fleet_r*.py file, or the kept-test waiver line in tests/test_seat_hold.py.
  (4) GUARD: the dev extra pins ruff with '==' to one version; requirements.lock names no ruff.

Red on the base (merge-prep 1b4a4518 plus the dev extra's ruff pin) by behaviour: (1) exits 1
with 81 findings in 13 test files (72 E501, 8 I001, 1 F401); (2) the step is
`pip install ruff && ruff check src`. (3) and (4) are green there.

"No assertion changes" is a STAGE CHECK the lane and features-json run once on the build (each
touched file's normalised syntax tree is unchanged; no +/- line with 'assert' and no added
NEUTERS line in a kept feature's test files, Slipstream's testFilesOf set), not a test here: a
kept test pinned to today's trees would go red the first time a later feature rightly edits one
of these files.
"""

from __future__ import annotations

import io
import re
import subprocess
import sys
import tokenize
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOUSE = "# ruff: noqa: E501 \N{EM DASH} the build's acceptance tests, kept as written"
SEAT_HOLD = ("# ruff: noqa: E501 \N{EM DASH} a kept test's long check line, kept as written until "
             "Slipstream can re-earn a kept test whose check is rewrapped (F69)")
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache"}


def _pyproject():
    return tomllib.loads((ROOT / "pyproject.toml").read_text())


def _ruff_pins():
    dev = _pyproject()["project"]["optional-dependencies"]["dev"]
    return [d for d in dev if re.match(r"\s*ruff\b", d)]


def _pinned_version():
    pins = _ruff_pins()
    assert len(pins) == 1, f"the dev extra names ruff {len(pins)} times: {pins}"
    m = re.fullmatch(r"\s*ruff\s*==\s*([0-9][0-9A-Za-z.]*)\s*", pins[0])
    assert m, f"the dev extra's ruff is not pinned to one exact version: {pins[0]!r}"
    return m.group(1)


def _comments(path):
    try:
        toks = tokenize.generate_tokens(io.StringIO(path.read_text(encoding="utf-8")).readline)
        return [(t.start[0], t.string) for t in toks if t.type == tokenize.COMMENT]
    except (tokenize.TokenError, SyntaxError, UnicodeDecodeError):
        return []


def _py_files():
    for top in ("src", "tests"):
        yield from sorted(p for p in (ROOT / top).rglob("*.py")
                          if not SKIP_DIRS.intersection(p.relative_to(ROOT).parts))


def test_r12_t60_1_ruff_check_src_tests_finds_nothing():
    p = subprocess.run([sys.executable, "-m", "ruff", "check", "src", "tests", "--no-cache"],
                       capture_output=True, text=True, cwd=ROOT, timeout=180,
                       stdin=subprocess.DEVNULL)
    assert "No module named ruff" not in p.stderr, \
        "ruff is not installed: install the dev extra (pip install -e '.[dev]')"
    assert p.returncode == 0, f"ruff check src tests exited {p.returncode}:\n{p.stdout[-4000:]}"


def test_r12_t60_2_ci_lints_src_and_tests_with_the_pinned_ruff():
    ver = _pinned_version()
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    m = re.search(r"^(\s*)- name: Ruff\s*\n((?:\1  .*\n)+)", ci, re.M)
    assert m, "ci.yml has no Ruff step"
    run = re.search(r"run:\s*(.+)", m.group(2))
    assert run, f"the Ruff step has no run line:\n{m.group(2)}"
    cmd = run.group(1).strip()
    installs = re.findall(r"pip install ([^&;]*)", cmd)
    named = [w for i in installs for w in i.split() if re.match(r"ruff\b", w)]
    assert named == [f"ruff=={ver}"], \
        f"the Ruff step installs {named or 'no ruff'}, not ruff=={ver} (the dev extra's pin): {cmd}"
    assert re.search(r"(^|&&|;)\s*ruff check src tests\s*($|&&|;)", cmd), \
        f"the Ruff step does not run `ruff check src tests`: {cmd}"


def test_r12_t60_3_guard_no_escape_by_configuration():
    ruff = _pyproject()["tool"]["ruff"]
    assert ruff == {"line-length": 100, "lint": {"select": ["E", "F", "I", "W"]}}, ruff
    found = [str(p.relative_to(ROOT)) for name in ("ruff.toml", ".ruff.toml")
             for p in ROOT.rglob(name) if not SKIP_DIRS.intersection(p.relative_to(ROOT).parts)]
    assert not found, f"a ruff config file outside pyproject.toml: {found}"
    per_line, file_level = [], []
    for p in _py_files():
        rel = str(p.relative_to(ROOT))
        for line, text in _comments(p):
            if re.match(r"#\s*ruff\s*:\s*noqa", text, re.I):
                house = (text.strip() == HOUSE
                         and re.fullmatch(r"tests/test_v2fleet_r[^/]*\.py", rel))
                waiver = text.strip() == SEAT_HOLD and rel == "tests/test_seat_hold.py"
                if not (house or waiver):
                    file_level.append(f"{rel}:{line}: {text}")
            elif re.search(r"noqa\s*:[^#]*\b(E501|I001)\b", text, re.I):
                per_line.append(f"{rel}:{line}: {text}")
    assert not per_line, f"per-line noqa naming E501 or I001: {per_line}"
    assert not file_level, f"a '# ruff: noqa' that is neither the house line in a v2fleet test " \
                           f"nor the waiver line in tests/test_seat_hold.py: {file_level}"


def test_r12_t60_4_guard_ruff_is_pinned_in_the_dev_extra_and_not_in_the_lock():
    _pinned_version()
    lock = (ROOT / "requirements.lock").read_text()
    assert not re.search(r"^\s*ruff\b", lock, re.M | re.I), "requirements.lock names ruff"
