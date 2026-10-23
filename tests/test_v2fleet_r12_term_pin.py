"""v2-fleet r12 F47 / T61 (j6-the-scratch-fleet-names-its-own-terminal): the scratch-fleet tests
attach their tmux client whatever terminal the caller names, or none. FROZEN.

Each case runs, as subprocesses with the environment as stated and nothing else changed,
`python -m pytest -o pythonpath=src -q tests/test_v2fleet_r5_falsifiers.py -k test_r5_f5_` and
T59 (5) of tests/test_v2fleet_r11_inbox_once.py, which drives the same Fleet:

  (1) TERM removed from the environment: both exit 0 (the 11 F5 tests and T59 (5) pass);
  (2) the same with TERM='' and with TERM=dumb;
  (3) GUARD: the same with TERM=xterm-256color;
  (4) GUARD: ci.yml's Tests step still sets TERM: xterm-256color.

Red on the base (merge-prep 1b4a4518) by behaviour: (1) and (2) give 6 failed, 5 passed in the
F5 run, every failure 'the scratch tmux client never attached', and T59 (5) fails. tmux is
required (fail, not skip).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
T59_5 = ("tests/test_v2fleet_r11_inbox_once.py"
         "::test_r11_t59_5_four_heal_passes_on_a_pane_in_use_leave_one_nudge")
UNSET = object()


def _run(term, args):
    env = {k: v for k, v in os.environ.items() if k != "TERM"}
    if term is not UNSET:
        env["TERM"] = term
    cmd = [sys.executable, "-m", "pytest", "-o", "pythonpath=src", "-q", "-p", "no:cacheprovider",
           *args]
    return subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT, env=env, timeout=500,
                          stdin=subprocess.DEVNULL)


@pytest.mark.parametrize("term", [UNSET, "", "dumb", "xterm-256color"],
                         ids=["1-unset", "2-empty", "2-dumb", "3-guard-xterm-256color"])
def test_r12_t61_the_scratch_fleet_attaches_whatever_term_the_caller_names(term):
    if not shutil.which("tmux"):
        pytest.fail("tmux is required by T61 (fail, not skip)")
    label = "unset" if term is UNSET else repr(term)
    f5 = _run(term, ["tests/test_v2fleet_r5_falsifiers.py", "-k", "test_r5_f5_"])
    t59 = _run(term, [T59_5])
    assert f5.returncode == 0, f"with TERM {label} the r5 F5 tests exited {f5.returncode}:\n" \
                               f"{f5.stdout[-3000:]}\nand T59 (5): {t59.stdout[-300:]}"
    assert re.search(r"\b11 passed\b", f5.stdout), f"with TERM {label}: {f5.stdout[-600:]}"
    assert t59.returncode == 0, f"with TERM {label} T59 (5) exited {t59.returncode}:\n" \
                                f"{t59.stdout[-3000:]}"
    assert re.search(r"\b1 passed\b", t59.stdout), f"with TERM {label}: {t59.stdout[-600:]}"


def test_r12_t61_4_guard_ci_keeps_its_term_line():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    m = re.search(r"^(\s*)- name: Tests\s*\n((?:\1  .*\n?)+)", ci, re.M)
    assert m, "ci.yml has no Tests step"
    assert re.search(r"^\s+TERM: xterm-256color\s*$", m.group(2), re.M), \
        f"ci.yml's Tests step lost its TERM line:\n{m.group(2)}"
