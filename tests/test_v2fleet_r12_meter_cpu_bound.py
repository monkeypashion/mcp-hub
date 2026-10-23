"""v2-fleet r12 F48 / T62 (j6-the-meters-cpu-bound-counts-its-own-work): the meter's CPU bound
counts the meter's own work, not the interpreter starting. FROZEN.

F48's command runs this file, then test_r5_f4_7 itself, then tests/test_v2fleet_r12_meter_growth.py
(written by the build). Each run of test_r5_f4_7 here is a subprocess pytest:

  (A) A SLOW START-UP: with a sitecustomize.py on the child's PYTHONPATH that spends 2.5 s of CPU
      (busy until time.process_time() has advanced 2.5 s) before anything else runs,
      test_r5_f4_7 passes.
  (B) A BUSY MACHINE (guard): with K processes spinning for the whole run, K twice the CPUs the
      test may use, test_r5_f4_7 passes; and the fixture bites: a 46-lane load-meter run's wall
      time under it is at least twice its wall time without it.
  (C) THE BOUND IS KEPT (guard, a source check): test_r5_f4_7 still builds 46 lanes x 100 turns
      with a ledger row every tenth turn, asserts injected_turns 460 for 2026-10-08 and a
      reading of at most 2.0 s.
  (D) GROWTH (not here: in tests/test_v2fleet_r12_meter_growth.py, which the build writes; held
      by the lane's mutant): by the reading test_r5_f4_7 uses, at 46 and at 460 lanes x 100 turns
      (a ledger row every tenth turn; injected_turns 460 and 4600 for 2026-10-08), the 460-lane
      reading is at most 25 times the 46-lane one. test_r5_f4_7's assert lines are unchanged.
  (E) A SLOW READ (guard, and the wall-clock falsifier): with a sitecustomize.py whose audit
      hook sleeps 0.1 s before each of the first 50 read-only opens of a .jsonl file in a
      process, test_r5_f4_7 passes; and the stand-in bites: the 46-lane load-meter run's wall
      time with it is at least 2.0 s more than without it. Sleeping costs no CPU, so a CPU
      reading is unchanged and a wall-clock reading breaks the 2 s bound on any machine.

Red on the base (merge-prep 1b4a4518) by behaviour: (A) fails, 'load-meter used ... s CPU for
46 lanes', its reading being start-up plus work. (B), (C) and (E) are green there.
"""

from __future__ import annotations

import ast
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
R5 = ROOT / "tests" / "test_v2fleet_r5_falsifiers.py"
F4_7 = "test_r5_f4_7_load_meter_with_a_ledger_costs_under_2s_cpu_at_46_lanes"

SITECUSTOMIZE = '''\
import os, sys, time
_t0 = time.process_time()
while time.process_time() - _t0 < 2.5:
    pass
_here = os.path.dirname(os.path.abspath(__file__))
for _p in sys.path:
    _f = os.path.join(_p or ".", "sitecustomize.py")
    if os.path.abspath(_p or ".") != _here and os.path.isfile(_f):
        exec(compile(open(_f).read(), _f, "exec"))
        break
'''


SLOW_READ = '''\
import os, sys, time
_n = [0]


def _slow_jsonl_read(event, args):
    if event != "open" or _n[0] >= 50 or not args:
        return
    path, mode = args[0], args[1] if len(args) > 1 else None
    flags = args[2] if len(args) > 2 else 0
    if isinstance(path, bytes):
        path = os.fsdecode(path)
    if not isinstance(path, str) or not path.endswith(".jsonl"):
        return
    if mode is not None:
        if any(c in str(mode) for c in "wax+"):
            return
    elif isinstance(flags, int) and flags & os.O_ACCMODE != os.O_RDONLY:
        return
    _n[0] += 1
    time.sleep(0.1)


sys.addaudithook(_slow_jsonl_read)
_here = os.path.dirname(os.path.abspath(__file__))
for _p in sys.path:
    _f = os.path.join(_p or ".", "sitecustomize.py")
    if os.path.abspath(_p or ".") != _here and os.path.isfile(_f):
        exec(compile(open(_f).read(), _f, "exec"))
        break
'''


def _env(path_first=None):
    env = dict(os.environ)
    if path_first:
        env["PYTHONPATH"] = os.pathsep.join([str(path_first)] + ([env["PYTHONPATH"]]
                                                                 if env.get("PYTHONPATH") else []))
    return env


def _f4_7(env=None):
    cmd = [sys.executable, "-m", "pytest", "-o", "pythonpath=src", "-q", "-p", "no:cacheprovider",
           "-rfE", "--tb=short", str(R5), "-k", F4_7]
    return subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT, env=env or _env(),
                          timeout=550, stdin=subprocess.DEVNULL)


def _passed(p):
    return p.returncode == 0 and re.search(r"\b1 passed\b", p.stdout) is not None


def _busy46(tmp):
    """f4_7's fixture: 46 lanes x 100 turns on 2026-10-08, a ledger row every tenth turn."""
    from tests.test_v2fleet_r5_falsifiers import LM_NOW, _assistant, _ledger_row, _user
    tmp.mkdir(parents=True)
    c = sqlite3.connect(tmp / "hub.db")
    c.executescript("CREATE TABLE messages (id INTEGER PRIMARY KEY, ts REAL, from_agent TEXT, "
                    "to_agent TEXT, channel TEXT);")
    c.commit()
    c.close()
    (tmp / "snap.json").write_text(json.dumps({
        "source": "fixture", "taken_at": LM_NOW, "since": "2026-09-26T00:00Z",
        "columns": ["hour_utc", "recipient", "reason", "held_max", "count"], "rows": []}))
    rows = []
    day = datetime(2026, 10, 8, 6, 0, tzinfo=timezone.utc).timestamp()
    for n in range(46):
        lane, lines, t = f"lane{n:02d}", [], day
        for k in range(100):
            text = f"{lane} turn {k}"
            if k % 10 == 0:
                rows.append(_ledger_row(t - 1, lane, text))
            lines += [_user(t, text), _assistant(t + 5), _user(t + 60, "", kind="channel"),
                      _assistant(t + 65)]
            t += 300
        (tmp / "busy" / lane).mkdir(parents=True)
        (tmp / "busy" / lane / "s.jsonl").write_text("\n".join(lines) + "\n")
    (tmp / "led.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    return tmp


def _meter_wall(fx, env=None):
    from tests.test_v2fleet_r5_falsifiers import LM_END, LM_NOW, LM_START
    cmd = [sys.executable, "-m", "mcp_hub.cli", "load-meter", "--hub-db", str(fx / "hub.db"),
           "--snapshot", str(fx / "snap.json"), "--transcripts", str(fx / "busy"),
           "--out", str(fx / "reading.json"), "--baseline-start", LM_START,
           "--baseline-end", LM_END, "--now", LM_NOW, "--hub-db-taken-at", LM_NOW,
           "--injections", str(fx / "led.jsonl")]
    env = dict(env or _env())
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT / "src")] + ([env["PYTHONPATH"]]
                                                               if env.get("PYTHONPATH") else []))
    t0 = time.monotonic()
    p = subprocess.run(cmd, capture_output=True, text=True, cwd=fx, env=env, timeout=300,
                       stdin=subprocess.DEVNULL)
    wall = time.monotonic() - t0
    assert p.returncode == 0, f"load-meter exited {p.returncode}: {p.stderr[-800:]}"
    return wall


def test_r12_t62_a_a_slow_start_up_is_not_the_meters_work(tmp_path):
    (tmp_path / "sitecustomize.py").write_text(SITECUSTOMIZE)
    p = _f4_7(_env(tmp_path))
    assert _passed(p), "with 2.5 s of CPU spent at interpreter start-up, test_r5_f4_7 failed " \
                       f"(its reading counts start-up):\n{p.stdout[-2500:]}"


def test_r12_t62_b_guard_a_busy_machine_does_not_break_the_bound(tmp_path):
    fx = _busy46(tmp_path / "fx")
    quiet = min(_meter_wall(fx) for _ in range(3))
    k = 2 * len(os.sched_getaffinity(0))
    hogs = [subprocess.Popen([sys.executable, "-c", "while True:\n    pass\n"],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL) for _ in range(k)]
    try:
        time.sleep(0.5)
        busy = _meter_wall(fx)
        p = _f4_7()
    finally:
        for h in hogs:
            if h.poll() is None:
                os.kill(h.pid, signal.SIGKILL)
            h.wait()
    assert busy >= 2 * quiet, f"the busy-machine fixture is too weak: {k} hogs took the 46-lane " \
                              f"run from {quiet:.2f} s to {busy:.2f} s wall, not twice"
    assert _passed(p), f"with {k} CPU hogs (46-lane wall {quiet:.2f} s -> {busy:.2f} s) " \
                       f"test_r5_f4_7 failed:\n{p.stdout[-2500:]}"


def _calls(fn):
    return {n.func.id for n in ast.walk(fn) if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)}


def test_r12_t62_c_guard_the_bound_and_the_fixture_are_kept():
    tree = ast.parse(R5.read_text())
    defs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert F4_7 in defs, f"{F4_7} is gone"
    fn = defs[F4_7]
    scope = [fn] + [defs[c] for c in sorted(_calls(fn)) if c in defs and c != "_meter"]
    nodes = [n for f in scope for n in ast.walk(f)]
    consts = {n.value for n in nodes if isinstance(n, ast.Constant)}
    for want in (46, 100, 460, "2026-10-08"):
        assert want in consts, f"{F4_7} (and its helpers) no longer use {want!r}"
    assert any(isinstance(n, ast.BinOp) and isinstance(n.op, ast.Mod)
               and isinstance(n.right, ast.Constant) and n.right.value == 10
               and isinstance(p, ast.Compare)
               for p in nodes for n in [getattr(p, "left", None)]), \
        "the ledger row is no longer written every tenth turn (k % 10 == 0)"
    bounds = [n for n in ast.walk(fn) if isinstance(n, ast.Compare) and len(n.ops) == 1
              and isinstance(n.ops[0], ast.LtE) and isinstance(n.comparators[0], ast.Constant)
              and isinstance(n.comparators[0].value, (int, float))]
    assert bounds, f"{F4_7} has no `reading <= bound` assertion"
    assert all(b.comparators[0].value <= 2.0 for b in bounds), \
        f"{F4_7}'s bound was raised: {[b.comparators[0].value for b in bounds]}"
    assert any(b.comparators[0].value == 2.0 for b in bounds), f"{F4_7} has no 2.0 s bound"


def test_r12_t62_e_guard_a_slow_read_does_not_break_the_bound(tmp_path):
    site = tmp_path / "site"
    site.mkdir()
    (site / "sitecustomize.py").write_text(SLOW_READ)
    fx = _busy46(tmp_path / "fx")
    plain = min(_meter_wall(fx) for _ in range(3))
    slow = _meter_wall(fx, _env(site))
    assert slow - plain >= 2.0, f"the slow-read stand-in is too weak: it took the 46-lane run " \
                                f"from {plain:.2f} s to {slow:.2f} s wall, not 2.0 s more"
    p = _f4_7(_env(site))
    assert _passed(p), f"with a slow read (46-lane wall {plain:.2f} s -> {slow:.2f} s) " \
                       f"test_r5_f4_7 failed:\n{p.stdout[-2500:]}"
