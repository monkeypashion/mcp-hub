"""v2-fleet r12 F48 / T62 (D) (j6-the-meters-cpu-bound-counts-its-own-work): the meter's cost
grows with its lanes, not with their square.

By the reading test_r5_f4_7 uses (_meter with cpu on: the load-meter call's own CPU, interpreter
start-up outside it), load-meter reads 46 lanes and 460 lanes x 100 turns on 2026-10-08, a ledger
row every tenth turn. The fixtures inject 460 and 4600 turns, and the 460-lane reading is at most
25 times the 46-lane one: a linear meter measures about 10 times, a quadratic one about 100.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from tests.test_v2fleet_r5_falsifiers import LM_NOW, _assistant, _ledger_row, _meter, _user

GROWTH_BOUND = 25


def _fixture(tmp, lanes):
    """`lanes` lanes x 100 turns on 2026-10-08, a ledger row every tenth turn, as test_r5_f4_7."""
    tmp.mkdir(parents=True)
    db = tmp / "hub.db"
    c = sqlite3.connect(db)
    c.executescript("CREATE TABLE messages (id INTEGER PRIMARY KEY, ts REAL, from_agent TEXT, "
                    "to_agent TEXT, channel TEXT);")
    c.commit()
    c.close()
    snap = tmp / "snap.json"
    snap.write_text(json.dumps({
        "source": "fixture", "taken_at": LM_NOW, "since": "2026-09-26T00:00Z",
        "columns": ["hour_utc", "recipient", "reason", "held_max", "count"], "rows": []}))
    tr = tmp / "busy"
    rows = []
    day = datetime(2026, 10, 8, 6, 0, tzinfo=timezone.utc).timestamp()
    for n in range(lanes):
        lane, lines, t = f"lane{n:03d}", [], day
        for k in range(100):
            text = f"{lane} turn {k}"
            if k % 10 == 0:
                rows.append(_ledger_row(t - 1, lane, text))
            lines += [_user(t, text), _assistant(t + 5), _user(t + 60, "", kind="channel"),
                      _assistant(t + 65)]
            t += 300
        (tr / lane).mkdir(parents=True)
        (tr / lane / "s.jsonl").write_text("\n".join(lines) + "\n")
    led = tmp / "injections.jsonl"
    led.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return {"tmp": tmp, "db": db, "snap": snap, "tr": tr}, led


def _reading(tmp, lanes):
    """The least of three readings, and the day's injected_turns."""
    lm, led = _fixture(tmp, lanes)
    cpus, injected = [], None
    for _ in range(3):
        r, cpu = _meter(lm, led, cpu=True)
        injected = {d["day"]: d["measures"] for d in r["daily"]}["2026-10-08"]["injected_turns"]
        cpus.append(cpu)
    return min(cpus), injected


def test_r12_t62_d_ten_times_the_lanes_costs_at_most_25_times_the_cpu(tmp_path):
    small, inj_small = _reading(tmp_path / "l46", 46)
    large, inj_large = _reading(tmp_path / "l460", 460)
    assert inj_small["value"] == 460, inj_small
    assert inj_large["value"] == 4600, inj_large
    assert large <= GROWTH_BOUND * small, (
        f"load-meter used {small:.3f} s CPU for 46 lanes and {large:.3f} s for 460 "
        f"({large / small:.1f} times; limit {GROWTH_BOUND})")
