"""The daily load meter's own tests (v2-fleet F1, j6-load-meter-daily).

The frozen falsifiers check the reading end to end through
`python -m mcp_hub.cli load-meter`. These tests go at the parts underneath:
the window arithmetic, the read-only connection, what counts as a turn, and
the refusals — each as a small, hermetic fixture, so a failure names the rule
it broke rather than "the reading changed".

Hermetic: tmp_path only. Nothing here opens the hub, a real transcript
directory, or any database but the one it just built.
"""
import argparse
import json
import os
import socket
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from mcp_hub import load_meter as m

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((ROOT / "contract" / "load-reading.schema.json").read_text())
VALIDATOR = Draft202012Validator(SCHEMA)

VOICE = m.VOICE_RELAY_AGENT
NOW = "2026-10-09T12:00:00Z"
START, END = "2026-09-26", "2026-10-03"
DAY = "2026-09-28T"

HUB_SCHEMA = """
    CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
        from_agent TEXT NOT NULL, to_agent TEXT, channel TEXT, body TEXT NOT NULL,
        read INTEGER NOT NULL DEFAULT 0, priority TEXT NOT NULL DEFAULT 'normal');
"""


def ts(text):
    return m.parse_instant(text).timestamp()


def write_hub(path, rows, wal=False, scratch=None):
    """A hub database; `wal` makes it a backup-API copy of a WAL-mode one."""
    target = (scratch / "live.db") if wal else path
    conn = sqlite3.connect(target)
    if wal:
        conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(HUB_SCHEMA)
    conn.executemany(
        "INSERT INTO messages (ts, from_agent, to_agent, channel, body) VALUES (?,?,?,?,?)",
        rows)
    conn.commit()
    if wal:
        dst = sqlite3.connect(path)
        conn.backup(dst)
        dst.close()
        for side in ("-wal", "-shm"):
            Path(str(path) + side).unlink(missing_ok=True)
    conn.close()
    return path


def jline(kind, when, **kw):
    rec = {"type": kind, "timestamp": when}
    rec.update(kw)
    return json.dumps(rec)


def typed(when, kind="human", **kw):
    origin = {"origin": {"kind": kind}} if kind else {}
    return jline("user", when, message={"role": "user", "content": "hello"}, **origin, **kw)


def asst(when, **kw):
    return jline("assistant", when,
                 message={"role": "assistant", "content": [{"type": "text", "text": "hi"}]}, **kw)


def write_transcripts(root, files):
    root.mkdir(parents=True, exist_ok=True)
    for rel, lines in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n")
    return root


def write_snapshot(path, rows, source="test-fixture"):
    path.write_text(json.dumps({
        "source": source, "taken_at": NOW, "since": "2026-09-26T00:00Z",
        "columns": ["hour_utc", "recipient", "reason", "held_max", "count"],
        "rows": rows}))
    return path


@pytest.fixture
def lane(tmp_path):
    """One lane, one session: two typed turns, one 600s wait, one hub wake."""
    return write_transcripts(tmp_path / "transcripts", {"lane-a/s1.jsonl": [
        typed(DAY + "10:00:00.000Z"),
        asst(DAY + "10:00:05.000Z"),
        asst(DAY + "10:00:30.000Z"),
        typed(DAY + "10:01:00.000Z", kind="channel"),
        typed(DAY + "10:10:30.000Z"),
        asst(DAY + "10:10:40.000Z"),
    ]})


def argv_run(tmp_path, **over):
    argv = ["--hub-db", str(over.get("hub_db", tmp_path / "hub.db")),
            "--snapshot", str(over.get("snapshot", tmp_path / "snap.json")),
            "--transcripts", str(over.get("transcripts", tmp_path / "transcripts")),
            "--out", str(over.get("out", tmp_path / "reading.json")),
            "--baseline-start", over.get("start", START),
            "--baseline-end", over.get("end", END),
            "--now", over.get("now", NOW)]
    if over.get("changes"):
        argv += ["--changes", str(over["changes"])]
    # r4: when the copy was taken is part of the reading. Left out, the meter
    # falls back to the file's mtime — which is what the timer's run does.
    if over.get("taken_at"):
        argv += ["--hub-db-taken-at", str(over["taken_at"])]
    return argv


def call(tmp_path, **over):
    """Run the meter in-process, the way r1's tests called its main()."""
    args = m.add_arguments(argparse.ArgumentParser()).parse_args(argv_run(tmp_path, **over))
    code = m.run(args)
    out = Path(over.get("out", tmp_path / "reading.json"))
    return code, (json.loads(out.read_text()) if out.exists() else None)


@pytest.fixture
def world(tmp_path, lane):
    write_hub(tmp_path / "hub.db", [
        (ts(DAY + "10:00:00Z"), VOICE, "lane-a", None, "spoken"),
        (ts(DAY + "10:02:00Z"), "lane-a", VOICE, None, "answered"),
        (ts(DAY + "10:03:00Z"), "lane-a", "lane-b", None, "lane to lane"),
        (ts(DAY + "10:04:00Z"), VOICE, None, "#general", "a broadcast, not a DM"),
    ])
    write_snapshot(tmp_path / "snap.json", [
        [DAY + "10:00Z", "lane-a", "operator", "", 3],
        [DAY + "11:00Z", "lane-a", "hold", "normal", 9],
        ["2026-10-05T10:00Z", "lane-a", "operator", "", 7],
    ])
    return tmp_path


# ----------------------------------------------------------------------------
# the read-only contract
# ----------------------------------------------------------------------------

def test_open_hub_ro_refuses_writes_and_leaves_no_sidecars(tmp_path):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    db = write_hub(tmp_path / "wal.db", [(ts(DAY + "10:00:00Z"), VOICE, "lane-a", None, "x")],
                   wal=True, scratch=scratch)
    assert db.read_bytes()[18:20] == b"\x02\x02", "fixture is not WAL-format"
    before = (db.read_bytes(), db.stat().st_mtime_ns)
    conn = m.open_hub_ro(db)
    assert list(conn.execute("SELECT count(*) FROM messages"))[0][0] == 1
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        conn.execute("INSERT INTO messages (ts, from_agent, body) VALUES (1, 'x', 'y')")
    conn.close()
    assert not Path(str(db) + "-wal").exists()
    assert not Path(str(db) + "-shm").exists()
    assert (db.read_bytes(), db.stat().st_mtime_ns) == before


def test_open_hub_ro_names_both_guarantees_in_the_uri(tmp_path):
    db = write_hub(tmp_path / "hub.db", [])
    conn = m.open_hub_ro(db)
    try:
        assert list(conn.execute("PRAGMA database_list"))[0][2] == str(db)
    finally:
        conn.close()


def test_a_missing_hub_db_is_an_unread_input_not_a_crash(tmp_path, world):
    code, reading = call(world, hub_db=tmp_path / "nope.db")
    assert code == 0
    inputs = {i["name"]: i for i in reading["inputs"]}
    assert inputs["hub_db"]["read"] is False and inputs["hub_db"]["missing"]
    assert not (tmp_path / "nope.db").exists(), "a missing db must not be created"


# ----------------------------------------------------------------------------
# the baseline's two rules
# ----------------------------------------------------------------------------

def test_baseline_shorter_than_a_week_is_refused_and_writes_nothing(world, capsys):
    code, reading = call(world, start="2026-09-28")
    assert code == 2
    assert reading is None
    err = capsys.readouterr().err
    assert "baseline" in err.lower() and str(m.MIN_BASELINE_DAYS) in err


def test_baseline_overlapping_the_first_change_is_refused(world, capsys):
    changes = world / "changes.json"
    changes.write_text(json.dumps([
        {"change_id": "F9", "at": "2026-10-06T00:00:00Z", "delivered": True},
        {"change_id": "F2", "at": "2026-10-01T00:00:00Z", "delivered": True},
    ]))
    code, reading = call(world, changes=changes)
    assert code == 2
    assert reading is None
    err = capsys.readouterr().err
    # the EARLIEST change is the one that bounds the baseline, whatever order
    # the file lists them in
    assert "first" in err.lower() and "F2" in err and "F9" not in err


def test_a_baseline_ending_exactly_at_the_first_change_is_accepted(world):
    changes = world / "changes.json"
    changes.write_text(json.dumps([{"change_id": "F2", "at": "2026-10-03T00:00:00Z",
                                    "delivered": True}]))
    code, reading = call(world, changes=changes)
    assert code == 0
    assert reading["baseline"] == {"start": START, "end": END, "days": 7}


def test_a_malformed_baseline_day_is_refused(world, capsys):
    code, reading = call(world, start="26/09/2026")
    assert code == 2
    assert reading is None
    assert "YYYY-MM-DD" in capsys.readouterr().err


def test_a_unreadable_changes_file_is_refused_before_anything_is_written(world, capsys):
    bad = world / "changes.json"
    bad.write_text("{not json")
    code, reading = call(world, changes=bad)
    assert code == 2
    assert reading is None
    assert "changes" in capsys.readouterr().err


# ----------------------------------------------------------------------------
# what counts, and what it is counted as
# ----------------------------------------------------------------------------

def test_a_reading_validates_and_every_number_names_its_source(world):
    code, reading = call(world)
    assert code == 0
    assert not list(VALIDATOR.iter_errors(reading))
    measures = reading["measures"]
    assert measures["typed_turns"]["pane"]["value"] == 2
    assert measures["typed_turns"]["pane"]["per_lane"] == {"lane-a": 2}
    assert measures["reaching_tim"]["pane"]["value"] == 2
    assert measures["wait_total_seconds"]["pane"]["value"] == 600
    assert measures["wait_median_seconds"]["pane"]["value"] == 600
    assert measures["hub_wakes"]["value"] == 1
    assert measures["operator_wakes"]["value"] == 3  # the 5 Oct row is outside
    for group in measures.values():
        checks = [group] if "source" in group else list(group.values())
        for one in checks:
            assert one["source"].strip()


def test_a_the_wait_runs_from_the_last_assistant_turn_to_the_next_typed_turn(tmp_path, world):
    """Not from the FIRST reply: a lane that keeps working is not waiting."""
    write_transcripts(tmp_path / "t2", {"lane-a/s1.jsonl": [
        typed(DAY + "10:00:00.000Z"),
        asst(DAY + "10:00:05.000Z"),     # first reply
        asst(DAY + "10:05:00.000Z"),     # still working
        typed(DAY + "10:10:00.000Z"),    # Tim types: the wait was 5 min, not 10
    ]})
    code, reading = call(world, transcripts=tmp_path / "t2")
    assert code == 0
    assert reading["measures"]["wait_total_seconds"]["pane"]["value"] == 300


def test_a_the_first_typed_turn_of_a_session_is_not_a_wait(tmp_path, world):
    write_transcripts(tmp_path / "t2", {"lane-a/s1.jsonl": [typed(DAY + "10:00:00.000Z")]})
    code, reading = call(world, transcripts=tmp_path / "t2")
    assert code == 0
    assert reading["measures"]["typed_turns"]["pane"]["value"] == 1
    assert reading["measures"]["wait_total_seconds"]["pane"]["value"] == 0
    assert reading["measures"]["wait_median_seconds"]["pane"]["value"] is None


def test_a_no_wait_is_null_and_never_a_median_of_zero(world):
    code, reading = call(world, start="2026-08-01", end="2026-08-08")
    assert code == 0
    median = reading["measures"]["wait_median_seconds"]["pane"]
    assert median["value"] is None and median["missing"].strip()
    # a window with nothing in it is still a MEASUREMENT: zero turns, not null
    assert reading["measures"]["typed_turns"]["pane"]["value"] == 0
    assert "missing" not in reading["measures"]["typed_turns"]["pane"]


@pytest.mark.parametrize("extra,typed_turns,hub_wakes,no_origin", [
    ({}, 1, 0, 0),
    ({"kind": "channel"}, 0, 1, 0),
    ({"kind": "task-notification"}, 0, 0, 0),
    ({"kind": None}, 0, 0, 1),
    ({"isSidechain": True}, 0, 0, 0),
    ({"isMeta": True}, 0, 0, 0),
    ({"isCompactSummary": True}, 0, 0, 0),
])
def test_a_one_turn_is_counted_as_exactly_one_thing(tmp_path, world, extra, typed_turns,
                                                    hub_wakes, no_origin):
    write_transcripts(tmp_path / "t2", {"lane-a/s1.jsonl": [typed(DAY + "10:00:00.000Z", **extra)]})
    code, reading = call(world, transcripts=tmp_path / "t2")
    assert code == 0
    measures = reading["measures"]
    assert measures["typed_turns"]["pane"]["value"] == typed_turns
    assert measures["hub_wakes"]["value"] == hub_wakes
    assert measures["no_origin"]["value"] == no_origin


def test_a_tool_results_are_not_turns_in_either_shape(tmp_path, world):
    write_transcripts(tmp_path / "t2", {"lane-a/s1.jsonl": [
        jline("user", DAY + "10:00:00.000Z",
              message={"role": "user", "content": [{"type": "tool_result", "content": "out"}]}),
        jline("user", DAY + "10:00:10.000Z", toolUseResult={"stdout": "out"},
              message={"role": "user", "content": "out"}),
        jline("summary", None, summary="a compaction summary"),
        "not json at all",
    ]})
    code, reading = call(world, transcripts=tmp_path / "t2")
    assert code == 0
    measures = reading["measures"]
    assert measures["typed_turns"]["pane"]["value"] == 0
    assert measures["no_origin"]["value"] == 0, "a tool result has no origin, and is no turn"


def test_a_the_pane_count_is_declared_an_upper_bound(world):
    """`squad send-keys` lands in the same shape, so the number cannot be exact."""
    code, reading = call(world)
    source = reading["measures"]["typed_turns"]["pane"]["source"].lower()
    assert "upper bound" in source and "send-keys" in source
    assert "no origin (not counted)" in reading["measures"]["no_origin"]["source"]


def test_a_the_voice_relay_is_counted_in_both_directions(world):
    code, reading = call(world)
    assert reading["measures"]["typed_turns"]["voice_relay"]["value"] == 1    # VOICE -> lane
    assert reading["measures"]["reaching_tim"]["voice_relay"]["value"] == 1   # lane -> VOICE
    for side in ("typed_turns", "reaching_tim"):
        assert VOICE in reading["measures"][side]["voice_relay"]["source"]


def test_a_lane_to_lane_dms_and_broadcasts_are_not_the_relay(tmp_path, world):
    write_hub(tmp_path / "hub2.db", [
        (ts(DAY + "10:00:00Z"), "lane-a", "lane-b", None, "lane to lane"),
        (ts(DAY + "10:01:00Z"), VOICE, None, "#general", "a broadcast"),
        (ts(DAY + "10:02:00Z"), "operator", "lane-a", None, "the dead operator name"),
    ])
    code, reading = call(world, hub_db=tmp_path / "hub2.db")
    assert code == 0
    assert reading["measures"]["typed_turns"]["voice_relay"]["value"] == 0
    assert reading["measures"]["reaching_tim"]["voice_relay"]["value"] == 0


def test_a_each_lane_is_named_in_per_lane(tmp_path, world):
    write_transcripts(tmp_path / "t2", {
        "lane-a/s1.jsonl": [typed(DAY + "10:00:00.000Z")],
        "lane-b/s1.jsonl": [typed(DAY + "10:00:00.000Z"), typed(DAY + "10:30:00.000Z")],
        "lane-c/s1.jsonl": [typed("2026-08-01T10:00:00.000Z")],
    })
    code, reading = call(world, transcripts=tmp_path / "t2")
    assert code == 0
    per_lane = reading["measures"]["typed_turns"]["pane"]["per_lane"]
    assert per_lane == {"lane-a": 1, "lane-b": 2, "lane-c": 0}, "a quiet lane reads 0, not absent"


# ----------------------------------------------------------------------------
# an unreadable source is null with a reason, never zero
# ----------------------------------------------------------------------------

def test_a_missing_transcripts_null_the_pane_measures_only(tmp_path, world):
    code, reading = call(world, transcripts=tmp_path / "gone")
    assert code == 0
    assert not list(VALIDATOR.iter_errors(reading))
    measures = reading["measures"]
    for one in (measures["typed_turns"]["pane"], measures["reaching_tim"]["pane"],
                measures["wait_total_seconds"]["pane"], measures["wait_median_seconds"]["pane"],
                measures["hub_wakes"], measures["no_origin"]):
        assert one["value"] is None and one["missing"].strip()
        assert "per_lane" not in one
    assert measures["typed_turns"]["voice_relay"]["value"] == 1
    assert measures["operator_wakes"]["value"] == 3


def test_a_missing_snapshot_nulls_only_the_operator_wakes(tmp_path, world):
    code, reading = call(world, snapshot=tmp_path / "gone.json")
    assert code == 0
    assert reading["measures"]["operator_wakes"]["value"] is None
    assert reading["measures"]["operator_wakes"]["missing"].strip()
    assert reading["measures"]["typed_turns"]["pane"]["value"] == 2
    inputs = {i["name"]: i for i in reading["inputs"]}
    assert inputs["snapshot"]["read"] is False
    assert str(tmp_path / "gone.json") in inputs["snapshot"]["missing"]


def test_a_a_readable_source_carries_no_excuse(world):
    """The contract forbids `missing` beside a real value — both ways round."""
    code, reading = call(world)
    assert code == 0
    assert "missing" not in reading["measures"]["typed_turns"]["pane"]
    assert "missing" not in [i for i in reading["inputs"] if i["name"] == "hub_db"][0]


# ----------------------------------------------------------------------------
# before/after per delivered change
# ----------------------------------------------------------------------------

def changes_file(path, *rows):
    path.write_text(json.dumps(list(rows)))
    return path


def test_a_a_delivered_change_gets_a_before_and_an_after(tmp_path, world):
    changes = changes_file(world / "changes.json",
                           {"change_id": "F14", "at": "2026-10-06T00:00:00Z", "delivered": True})
    code, reading = call(world, changes=changes, now="2026-10-20T00:00:00Z")
    assert code == 0
    assert reading["flags"] == []
    row = reading["changes"][0]
    assert row["change_id"] == "F14"
    # windows are the baseline's own length, so before, after and baseline are
    # comparable numbers rather than three different-sized buckets
    assert row["before"] is not None and row["after"] is not None
    assert row["before"]["operator_wakes"]["value"] == 7   # the 5 Oct row
    # the after window ends past the snapshot's taken_at: not read, never 0 (r4)
    assert row["after"]["operator_wakes"]["value"] is None
    assert row["after"]["operator_wakes"]["missing"].strip()


def test_a_an_after_window_that_has_not_closed_is_flagged_not_guessed(world):
    changes = changes_file(world / "changes.json",
                           {"change_id": "F14", "at": "2026-10-06T00:00:00Z", "delivered": True})
    code, reading = call(world, changes=changes)  # now is 3.5 days after the change
    assert code == 0
    row = reading["changes"][0]
    assert row["before"] is not None
    assert row["after"] is None
    flag = [f for f in reading["flags"] if f["code"] == "missing_after"][0]
    assert flag["change_id"] == "F14" and flag["detail"].strip()
    assert not [f for f in reading["flags"] if f["code"] == "missing_before"]


def test_a_an_undelivered_change_is_recorded_but_not_flagged(world):
    changes = changes_file(world / "changes.json",
                           {"change_id": "F99", "at": "2026-10-06T00:00:00Z", "delivered": False})
    code, reading = call(world, changes=changes)
    assert code == 0
    assert reading["changes"][0]["after"] is None
    assert reading["flags"] == []


def test_a_changes_are_read_in_time_order(world):
    changes = changes_file(
        world / "changes.json",
        {"change_id": "F20", "at": "2026-10-08T00:00:00Z", "delivered": True},
        {"change_id": "F14", "at": "2026-10-06T00:00:00Z", "delivered": True})
    code, reading = call(world, changes=changes, now="2026-10-30T00:00:00Z")
    assert code == 0
    assert [c["change_id"] for c in reading["changes"]] == ["F14", "F20"]


# ----------------------------------------------------------------------------
# the job as it actually runs
# ----------------------------------------------------------------------------

def test_a_nothing_is_written_beside_any_source(world, tmp_path):
    def inventory():
        return sorted((str(p), p.stat().st_mtime_ns) for p in tmp_path.rglob("*")
                      if p.is_file() and p.name != "reading.json")

    before = inventory()
    code, reading = call(world)
    assert code == 0
    assert inventory() == before


def test_a_the_reading_carries_no_message_or_turn_text(world, capsys):
    mark = "PRIVATE-BODY-TEXT"
    db = write_hub(world / "bodies.db", [(ts(DAY + "10:00:00Z"), VOICE, "lane-a", None, mark)])
    (world / "transcripts" / "lane-a" / "s2.jsonl").write_text(
        jline("user", DAY + "10:00:00.000Z", origin={"kind": "human"},
              message={"role": "user", "content": mark}) + "\n")
    code, reading = call(world, hub_db=db)
    assert code == 0
    out = capsys.readouterr()
    assert mark not in json.dumps(reading)
    assert mark not in out.out and mark not in out.err


def test_a_the_subcommand_is_the_only_way_in(tmp_path):
    src = (ROOT / "src" / "mcp_hub" / "load_meter.py").read_text()
    assert "__main__" not in src
    assert not (ROOT / "src" / "mcp_hub" / "__main__.py").exists()
    p = subprocess.run([sys.executable, "-m", "mcp_hub.cli", "load-meter", "--help"],
                       capture_output=True, text=True, timeout=120,
                       env={**__import__("os").environ,
                            "PYTHONPATH": str(ROOT / "src")})
    assert p.returncode == 0, p.stderr
    for flag in ("--hub-db", "--snapshot", "--transcripts", "--out",
                 "--baseline-start", "--baseline-end", "--changes", "--now"):
        assert flag in p.stdout, flag


def test_a_the_hub_server_does_not_import_the_meter():
    """A read-only side job must not be able to take the hub down with it."""
    server = (ROOT / "src" / "mcp_hub" / "server.py").read_text()
    assert "import load_meter" not in server
    assert "from .load_meter" not in server
    assert "from mcp_hub.load_meter" not in server


def test_a_now_defaults_to_the_present(world, monkeypatch):
    args = m.add_arguments(argparse.ArgumentParser()).parse_args(
        [a for a in argv_run(world) if a not in ("--now", NOW)])
    assert args.now is None
    assert m.run(args) == 0
    reading = json.loads((world / "reading.json").read_text())
    generated = m.parse_instant(reading["generated_at"])
    assert abs((datetime.now(timezone.utc) - generated).total_seconds()) < 120


# ----------------------------------------------------------------------------
# coverage: what each source can answer for, and what it must refuse to
#
# r4's rule, and the one that stops the meter flattering the changes it
# measures: a window outside a source's coverage is UNREAD, not quiet. The
# wake snapshot answers for `since .. taken_at`, the hub copy for everything
# up to the moment it was taken, and that copy's wake_log for the 14 days
# before that — the hub prunes the table, so an older window reads the
# retention policy rather than the fleet.
# ----------------------------------------------------------------------------

WAKE_LOG_SCHEMA = """
    CREATE TABLE wake_log (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
        agent TEXT NOT NULL, reason TEXT NOT NULL, held_max TEXT NOT NULL DEFAULT '');
"""


def write_wake_log(path, rows):
    """Attach a wake_log to a hub copy: [(instant, reason)]."""
    conn = sqlite3.connect(path)
    conn.executescript(WAKE_LOG_SCHEMA)
    conn.executemany("INSERT INTO wake_log (ts, agent, reason) VALUES (?,?,?)",
                     [(ts(when), "lane-a", reason) for when, reason in rows])
    conn.commit()
    conn.close()
    return path


AFTER_WAKES = [("2026-10-07T09:00:00Z", "operator"), ("2026-10-08T09:00:00Z", "operator"),
               ("2026-10-07T10:00:00Z", "reply"), ("2026-10-07T11:00:00Z", "hold")]
CHANGE_AT = "2026-10-06T00:00:00Z"
AFTER_CLOSED = "2026-10-20T00:00:00Z"


def window(start, end):
    return (m.parse_instant(start), m.parse_instant(end))


def test_coverage_accepts_only_a_window_wholly_inside_it():
    cov = m.Coverage("the fixture", start=m.parse_instant("2026-10-01T00:00Z"),
                     end=m.parse_instant("2026-10-10T00:00Z"))
    assert cov.covers(window("2026-10-02T00:00Z", "2026-10-09T00:00Z"))
    assert cov.covers(window("2026-10-01T00:00Z", "2026-10-10T00:00Z"))  # exactly
    assert not cov.covers(window("2026-09-30T00:00Z", "2026-10-05T00:00Z"))
    assert not cov.covers(window("2026-10-05T00:00Z", "2026-10-11T00:00Z"))


def test_coverage_is_unbounded_where_it_has_no_boundary():
    """The hub copy holds all of history up to the moment it was taken: only
    its END bounds it, and a baseline from last year is still inside it."""
    cov = m.Coverage("the copy", end=m.parse_instant("2026-10-10T00:00Z"))
    assert cov.covers(window("2025-01-01T00:00Z", "2025-01-08T00:00Z"))
    assert not cov.covers(window("2026-10-09T00:00Z", "2026-10-11T00:00Z"))


def test_the_gap_names_the_boundary_the_window_fell_outside(world):
    cov = m.Coverage("the wake snapshot (fixture, taken 2026-10-09T12:00:00Z)",
                     start=m.parse_instant("2026-09-26T00:00Z"),
                     end=m.parse_instant("2026-10-09T12:00:00Z"),
                     start_text="2026-09-26T00:00Z", end_text="2026-10-09T12:00:00Z")
    why = cov.gap(window("2026-10-06T00:00Z", "2026-10-13T00:00Z"))
    assert "2026-10-09T12:00:00Z" in why, why
    assert "not 0" in why, "the reason has to say what it is NOT"


def test_a_window_past_the_snapshot_falls_back_to_the_copys_wake_log(tmp_path, world):
    """The snapshot stops at its taken_at; the copy's own wake rows carry on.
    On r3 this window read 0 operator wakes — and 0 is the number a change
    gets congratulated for."""
    db = write_hub(tmp_path / "wl.db", [])
    write_wake_log(db, AFTER_WAKES)
    changes = changes_file(world / "changes.json",
                           {"change_id": "F14", "at": CHANGE_AT, "delivered": True})
    code, reading = call(world, hub_db=db, changes=changes, now=AFTER_CLOSED,
                         taken_at=AFTER_CLOSED)
    assert code == 0
    row = reading["changes"][0]
    assert row["after"]["operator_wakes"]["value"] == 2, row["after"]["operator_wakes"]
    assert "wake_log" in row["after"]["operator_wakes"]["source"]
    # inside the snapshot the SNAPSHOT still answers — it is the operator
    # plane's own summary, and the two must not disagree about one window
    before = row["before"]["operator_wakes"]
    assert before["value"] == 7 and "wake_log" not in before["source"], before


def test_a_window_older_than_the_wake_logs_retention_is_null_with_both_reasons(
        tmp_path, world):
    """A copy taken on 25 Oct answers for the 14 days before it. The 6-13 Oct
    window is outside BOTH sources, so it is unread — and the reason names
    both, because the fix is to take a reading with a source that reaches."""
    db = write_hub(tmp_path / "wl.db", [])
    write_wake_log(db, AFTER_WAKES)
    changes = changes_file(world / "changes.json",
                           {"change_id": "F14", "at": CHANGE_AT, "delivered": True})
    code, reading = call(world, hub_db=db, changes=changes, now="2026-10-25T00:00:00Z",
                         taken_at="2026-10-25T00:00:00Z")
    assert code == 0
    after = reading["changes"][0]["after"]["operator_wakes"]
    assert after["value"] is None, after
    assert NOW in after["missing"], "the snapshot's own taken_at, verbatim"
    assert "wake_log" in after["missing"] and "14" in after["missing"], after["missing"]
    assert m.WAKE_LOG_DAYS == 14


def test_a_relay_window_past_the_hub_copy_is_null_and_a_real_zero_stays_zero(world):
    changes = changes_file(world / "changes.json",
                           {"change_id": "F14", "at": CHANGE_AT, "delivered": True})
    code, reading = call(world, changes=changes, now=AFTER_CLOSED,
                         taken_at="2026-10-10T00:00:00Z")
    assert code == 0
    gone = reading["changes"][0]["after"]["typed_turns"]["voice_relay"]
    assert gone["value"] is None and "2026-10-10" in gone["missing"], gone
    code, reading = call(world, changes=changes, now=AFTER_CLOSED, taken_at=AFTER_CLOSED)
    assert code == 0
    inside = reading["changes"][0]["after"]["typed_turns"]["voice_relay"]
    assert inside["value"] == 0 and "missing" not in inside, inside


def test_the_copys_coverage_defaults_to_its_own_mtime(tmp_path, world):
    """A copy carries the moment it was written; `--hub-db-taken-at` is for one
    shipped here hours later. Defaulting to `now` instead would promise the
    reading covers hours the file cannot possibly hold."""
    db = write_hub(tmp_path / "aged.db", [])
    os.utime(db, (ts("2026-10-04T00:00:00Z"), ts("2026-10-04T00:00:00Z")))
    args = m.add_arguments(argparse.ArgumentParser()).parse_args(
        argv_run(world, hub_db=db))
    assert args.hub_db_taken_at is None
    assert m.stamp(m.hub_db_taken_at(args)) == "2026-10-04T00:00:00Z"
    code, reading = call(world, hub_db=db)   # the baseline ends 10-03: inside
    assert code == 0
    assert reading["measures"]["typed_turns"]["voice_relay"]["value"] == 0
    code, reading = call(world, hub_db=db, start="2026-10-01", end="2026-10-08")
    assert code == 0
    late = reading["measures"]["typed_turns"]["voice_relay"]
    assert late["value"] is None and "2026-10-04" in late["missing"], late


def test_a_malformed_hub_db_taken_at_is_refused_before_anything_is_written(world, capsys):
    code, reading = call(world, taken_at="the 4th of October")
    assert code == 2
    assert reading is None
    assert "--hub-db-taken-at" in capsys.readouterr().err


def test_a_bodyless_projection_is_read_without_its_bodies(tmp_path, world):
    """What leaves prod-1 is messages(ts, from_agent, to_agent, channel) plus
    wake_log — no bodies at all. The meter must never select one."""
    db = tmp_path / "projection.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE messages (ts REAL NOT NULL, from_agent TEXT NOT NULL, "
        "to_agent TEXT, channel TEXT);")
    conn.executemany("INSERT INTO messages (ts, from_agent, to_agent, channel) VALUES (?,?,?,NULL)",
                     [(ts(DAY + "10:00:00Z"), VOICE, "lane-a"),
                      (ts(DAY + "10:02:00Z"), "lane-a", VOICE)])
    conn.commit()
    conn.close()
    code, reading = call(world, hub_db=db)
    assert code == 0
    assert reading["measures"]["typed_turns"]["voice_relay"]["value"] == 1
    assert reading["measures"]["reaching_tim"]["voice_relay"]["value"] == 1
    assert {i["name"]: i for i in reading["inputs"]}["hub_db"]["read"] is True


def test_a_copy_with_no_wake_log_says_so_rather_than_counting_zero(tmp_path, world):
    hub = m.HubMessages(write_hub(tmp_path / "plain.db", []))
    assert hub.wake_rows is None, "no table must not read as no rows"
    hub = m.HubMessages(write_wake_log(write_hub(tmp_path / "wl.db", []), AFTER_WAKES))
    assert hub.wakes(window("2026-10-07T00:00Z", "2026-10-08T00:00Z"), "operator") == 1


# ----------------------------------------------------------------------------
# the day-by-day record
# ----------------------------------------------------------------------------

def test_closed_days_stops_before_the_open_one():
    days = m.closed_days(m.parse_day("2026-10-03"), m.parse_instant("2026-10-06T00:00:01Z"))
    assert [d.date().isoformat() for d in days] == ["2026-10-03", "2026-10-04", "2026-10-05"]
    # the open day is absent even a second before it closes: half a day beside
    # whole ones reads as a quiet day
    days = m.closed_days(m.parse_day("2026-10-03"), m.parse_instant("2026-10-05T23:59:59Z"))
    assert [d.date().isoformat() for d in days] == ["2026-10-03", "2026-10-04"]
    assert m.closed_days(m.parse_day("2026-10-03"), m.parse_instant("2026-10-03T12:00Z")) == []


def test_each_day_is_its_own_measure_set_and_validates(world):
    code, reading = call(world)
    assert code == 0
    assert not list(VALIDATOR.iter_errors(reading))
    days = [d["day"] for d in reading["daily"]]
    assert days == ["2026-10-03", "2026-10-04", "2026-10-05", "2026-10-06",
                    "2026-10-07", "2026-10-08"], days
    by = {d["day"]: d["measures"] for d in reading["daily"]}
    assert by["2026-10-05"]["operator_wakes"]["value"] == 7   # the 5 Oct row
    assert by["2026-10-04"]["operator_wakes"]["value"] == 0   # measured, not missing
    assert by["2026-10-04"]["typed_turns"]["pane"]["value"] == 0
    assert all(set(d["measures"]) == set(reading["measures"]) for d in reading["daily"])


def test_a_coverage_gap_inside_a_day_is_a_missing_line_and_never_a_flag(world):
    """`flags` is for the reading's own refusals; a source that does not reach
    is the MEASURE's business, and putting it in flags would make every
    backfilled day look like a fault in the meter."""
    code, reading = call(world, now="2026-10-12T00:00:00Z", taken_at="2026-10-11T00:00:00Z")
    assert code == 0
    by = {d["day"]: d["measures"] for d in reading["daily"]}
    assert by["2026-10-10"]["operator_wakes"]["value"] is None
    assert by["2026-10-10"]["operator_wakes"]["missing"].strip()
    assert reading["flags"] == [], reading["flags"]


def test_the_days_past_sixty_are_dropped_and_said_out_loud(world):
    code, reading = call(world, now="2026-12-15T06:00:00Z", taken_at="2026-12-15T06:00:00Z")
    assert code == 0
    assert not list(VALIDATOR.iter_errors(reading))
    days = [d["day"] for d in reading["daily"]]
    assert len(days) == m.MAX_DAILY_DAYS == 60
    assert days[0] == "2026-10-16" and days[-1] == "2026-12-14"
    flag = [f for f in reading["flags"] if f["code"] == "daily_truncated"]
    assert flag and "2026-10-03" in flag[0]["detail"], flag


# ----------------------------------------------------------------------------
# a source that stopped is not a change that worked (Term 2)
# ----------------------------------------------------------------------------

def stops_file(path, changes, stops):
    path.write_text(json.dumps({"changes": changes, "source_stops": stops}))
    return path


def test_a_stop_inside_a_before_after_span_is_flagged_not_comparable(world):
    """operator-console stopped on 8 Oct. Every "after" window straddling that
    date reads fewer operator wakes — and the drop belongs to the console
    going away, not to the change."""
    changes = stops_file(
        world / "changes.json", [{"change_id": "F14", "at": CHANGE_AT, "delivered": True}],
        [{"measure": "operator_wakes", "stopped_at": "2026-10-08T00:00:00Z"}])
    code, reading = call(world, changes=changes, now=AFTER_CLOSED)
    assert code == 0
    flag = [f for f in reading["flags"] if f["code"] == "source_stopped"]
    assert flag and flag[0]["change_id"] == "F14", reading["flags"]
    assert "not comparable: source stopped" in flag[0]["detail"]
    assert "operator_wakes" in flag[0]["detail"]
    assert "saving" not in json.dumps(reading["changes"]).lower()


def test_a_stop_outside_the_span_is_not_flagged(world):
    changes = stops_file(
        world / "changes.json", [{"change_id": "F14", "at": CHANGE_AT, "delivered": True}],
        [{"measure": "operator_wakes", "stopped_at": "2026-11-01T00:00:00Z"}])
    code, reading = call(world, changes=changes, now=AFTER_CLOSED)
    assert code == 0
    assert not [f for f in reading["flags"] if f["code"] == "source_stopped"]


def test_a_bare_list_of_changes_still_reads(world):
    """r1's shape: a list, no document around it, and no source stops."""
    doc = m.load_changes(changes_file(world / "changes.json",
                                      {"change_id": "F14", "at": CHANGE_AT,
                                       "delivered": True}))
    assert [c["change_id"] for c in doc["changes"]] == ["F14"]
    assert doc["source_stops"] == []
    assert m.load_changes(None) == {"changes": [], "source_stops": []}


def test_a_source_stop_missing_its_fields_is_refused_not_ignored(world, capsys):
    bad = world / "changes.json"
    bad.write_text(json.dumps({"changes": [], "source_stops": [{"measure": "x"}]}))
    code, reading = call(world, changes=bad)
    assert code == 2
    assert reading is None
    assert "changes" in capsys.readouterr().err


# ----------------------------------------------------------------------------
# a count is this host's count (Term 3)
# ----------------------------------------------------------------------------

def test_every_transcript_count_names_this_host_and_the_directory(world):
    """A transcript count read on dev-vm-1 is dev-vm-1's lanes, not the
    fleet's. The source line is where that stops being quoted fleet-wide."""
    changes = changes_file(world / "changes.json",
                           {"change_id": "F14", "at": CHANGE_AT, "delivered": True})
    code, reading = call(world, changes=changes, now=AFTER_CLOSED)
    assert code == 0
    host, where = socket.gethostname(), str(world / "transcripts")
    sets = [reading["measures"], reading["changes"][0]["before"],
            reading["changes"][0]["after"]] + [d["measures"] for d in reading["daily"]]
    for one in sets:
        for measure in (one["typed_turns"]["pane"], one["reaching_tim"]["pane"],
                        one["wait_total_seconds"]["pane"],
                        one["wait_median_seconds"]["pane"], one["hub_wakes"],
                        one["no_origin"]):
            assert host in measure["source"], measure["source"]
            assert where in measure["source"], measure["source"]
    # and the hub's own counts name the relay, not the host's transcripts
    assert host not in reading["measures"]["typed_turns"]["voice_relay"]["source"]


# ----------------------------------------------------------------------------
# the job as the timer runs it: squad/load-meter-daily
# ----------------------------------------------------------------------------

DAILY = ROOT / "squad" / "load-meter-daily"


def daily_world(tmp_path):
    """A box the daily script can run on: a state dir, sources, and decoys.

    The decoys matter: ~/.mcp-hub/hub.db and the state dir's own hub.db are
    where a hub database usually lives, and a default would read one of them.
    """
    home = tmp_path / "home"
    (home / ".mcp-hub").mkdir(parents=True)
    state = tmp_path / "state"
    (state / "v2-fleet").mkdir(parents=True)
    for decoy in (home / ".mcp-hub" / "hub.db", state / "hub.db"):
        write_hub(decoy, [(ts(DAY + "10:00:00Z"), VOICE, "lane-a", None, "decoy")] * 9)
    given = tmp_path / "given" / "copy.db"
    given.parent.mkdir()
    write_hub(given, [(ts(DAY + "10:00:00Z"), VOICE, "lane-a", None, "relayed"),
                      (ts(DAY + "10:02:00Z"), "lane-a", VOICE, None, "answered")])
    write_snapshot(tmp_path / "snap.json", [[DAY + "10:00Z", "lane-a", "operator", "", 3]])
    write_transcripts(tmp_path / "transcripts",
                      {"lane-a/s1.jsonl": [typed(DAY + "10:00:00.000Z"),
                                           asst(DAY + "10:00:05.000Z")]})
    return {"home": home, "state": state, "given": given, "tmp": tmp_path}


def run_daily(box, **over):
    env = {k: v for k, v in os.environ.items() if k != "MCP_HUB_DB"}
    env.update(HOME=str(box["home"]), MCP_HUB_STATE_DIR=str(box["state"]),
               PYTHONPATH=str(ROOT / "src"),
               LOAD_METER_SNAPSHOT=str(box["tmp"] / "snap.json"),
               LOAD_METER_TRANSCRIPTS=str(box["tmp"] / "transcripts"),
               LOAD_METER_BASELINE_START=START, LOAD_METER_BASELINE_END=END,
               LOAD_METER_NOW=NOW, LOAD_METER_PYTHON=sys.executable)
    if "db" not in over:
        env["MCP_HUB_DB"] = str(box["given"])
    for key, value in over.items():
        if key == "db":
            if value is not None:
                env["MCP_HUB_DB"] = str(value)
        else:
            env[key] = str(value)
    return subprocess.run(["bash", str(DAILY)], capture_output=True, text=True,
                          cwd=str(box["tmp"]), env=env, timeout=120)


def file_tree(root):
    return {str(p): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in sorted(root.rglob("*")) if p.is_file()}


def test_the_daily_job_reads_the_file_it_was_given_and_no_other(tmp_path):
    box = daily_world(tmp_path)
    before = file_tree(tmp_path)
    proc = run_daily(box)
    assert proc.returncode == 0, proc.stderr[-800:]
    out = box["state"] / "v2-fleet" / "readings" / "2026-10-09.json"
    assert out.is_file(), sorted(file_tree(tmp_path))
    reading = json.loads(out.read_text())
    assert not list(VALIDATOR.iter_errors(reading))
    hub = {i["name"]: i for i in reading["inputs"]}["hub_db"]
    assert hub["path"] == str(box["given"]) and hub["read"] is True
    # the given copy's one relayed DM, not the decoys' nine
    assert reading["measures"]["typed_turns"]["voice_relay"]["value"] == 1
    changed = {k for k in set(before) | set(file_tree(tmp_path))
               if before.get(k) != file_tree(tmp_path).get(k)}
    readings = str(box["state"] / "v2-fleet" / "readings")
    assert all(k.startswith(readings + os.sep) for k in changed), changed
    left = list((box["state"] / "v2-fleet" / "readings").glob(".*"))
    assert not left, f"a partial reading was left behind: {left}"


def test_the_daily_job_names_the_reading_for_the_utc_day_of_its_now(tmp_path):
    box = daily_world(tmp_path)
    proc = run_daily(box, LOAD_METER_NOW="2026-10-11T23:59:59Z")
    assert proc.returncode == 0, proc.stderr[-800:]
    assert (box["state"] / "v2-fleet" / "readings" / "2026-10-11.json").is_file()


def test_the_daily_job_reads_the_fleets_changes_file_when_there_is_one(tmp_path):
    box = daily_world(tmp_path)
    (box["state"] / "v2-fleet" / "changes.json").write_text(json.dumps(
        {"changes": [{"change_id": "F14", "at": CHANGE_AT, "delivered": True}],
         "source_stops": [{"measure": "operator_wakes",
                           "stopped_at": "2026-10-08T00:00:00Z"}]}))
    proc = run_daily(box, LOAD_METER_NOW=AFTER_CLOSED,
                     LOAD_METER_HUB_DB_TAKEN_AT=AFTER_CLOSED)
    assert proc.returncode == 0, proc.stderr[-800:]
    reading = json.loads((box["state"] / "v2-fleet" / "readings"
                          / "2026-10-20.json").read_text())
    assert [c["change_id"] for c in reading["changes"]] == ["F14"]
    assert [f["code"] for f in reading["flags"]] == ["source_stopped"]


@pytest.mark.parametrize("over,named", [
    ({"db": None}, "MCP_HUB_DB"),
    ({"db": "/nonexistent/hub-copy.db"}, "MCP_HUB_DB"),
    ({"LOAD_METER_BASELINE_START": ""}, "LOAD_METER_BASELINE_START"),
    ({"LOAD_METER_BASELINE_END": ""}, "LOAD_METER_BASELINE_END"),
    ({"LOAD_METER_NOW": "20 Oct 2026"}, "LOAD_METER_NOW"),
])
def test_the_daily_job_refuses_by_name_and_writes_nothing(tmp_path, over, named):
    """Each of these has NO safe default. A guessed hub copy reads a source
    nobody chose; a guessed baseline becomes the "before" that gets quoted."""
    box = daily_world(tmp_path)
    before = file_tree(tmp_path)
    listing = sorted(str(p) for p in tmp_path.rglob("*"))
    proc = run_daily(box, **over)
    assert proc.returncode != 0
    assert named in (proc.stdout + proc.stderr), proc.stdout + proc.stderr
    assert file_tree(tmp_path) == before, "a refusal wrote something"
    assert sorted(str(p) for p in tmp_path.rglob("*")) == listing, "a refusal created a path"


def test_the_daily_job_leaves_the_given_copy_byte_identical(tmp_path):
    box = daily_world(tmp_path)
    given = box["given"]
    before = (given.read_bytes(), given.stat().st_mtime_ns)
    assert run_daily(box).returncode == 0
    assert (given.read_bytes(), given.stat().st_mtime_ns) == before
    for side in ("-wal", "-shm", "-journal"):
        assert not Path(str(given) + side).exists(), side


def test_the_units_carry_no_policy_of_their_own():
    """The service runs the script and sets no MCP_HUB_DB: which copy, taken
    when, is a per-machine fact and belongs in mcp-hub's drop-in — not in a
    file that ships in the repo for every machine."""
    units = ROOT / "squad" / "systemd"
    service = (units / "load-meter.service").read_text()
    timer = (units / "load-meter.timer").read_text()
    execs = [ln for ln in service.splitlines() if ln.startswith("ExecStart")]
    assert execs and all(ln.rstrip().endswith("load-meter-daily") for ln in execs), execs
    for unit in (service, timer):
        assert not [ln for ln in unit.splitlines()
                    if ln.startswith(("Environment", "EnvironmentFile"))
                    and "MCP_HUB_DB" in ln], unit
    assert "OnCalendar=" in timer
    assert not [ln for ln in timer.splitlines()
                if ln.startswith(("OnUnitActiveSec", "OnBootSec", "OnActiveSec"))]
    assert DAILY.is_file() and os.access(DAILY, os.X_OK), "the unit's ExecStart must be runnable"


# ----------------------------------------------------------------------------
# a day is half-open: an event exactly at midnight belongs to ONE day
# (declared base test edit, v2f r10: the inclusive-end mutant of `_in`
# survived every falsifier above, because no fixture put an event on the line)
# ----------------------------------------------------------------------------

def test_an_event_exactly_at_midnight_is_counted_in_one_day_only(tmp_path):
    """Two adjacent days share the instant 00:00. If both claimed it, a
    daily series would count a midnight turn twice and the sum of the days
    would exceed the whole; if neither did, it would vanish. The day that
    STARTS at midnight owns it."""
    root = write_transcripts(tmp_path / "transcripts", {"lane-a/s1.jsonl": [
        asst("2026-09-28T23:59:00.000Z"),
        typed("2026-09-29T00:00:00.000Z"),
        asst("2026-09-29T00:00:05.000Z"),
    ]})
    tr = m._load_transcripts(root)
    day1 = (m.parse_day("2026-09-28"), m.parse_day("2026-09-29"))
    day2 = (m.parse_day("2026-09-29"), m.parse_day("2026-09-30"))
    whole = (m.parse_day("2026-09-28"), m.parse_day("2026-09-30"))
    first, second, both = tr.read(day1), tr.read(day2), tr.read(whole)
    assert first["typed"]["lane-a"] == 0, "the day that ENDS at midnight claimed it"
    assert second["typed"]["lane-a"] == 1, "the day that STARTS at midnight lost it"
    assert first["waits"] == [] and second["waits"] == [60.0]
    assert (first["typed"]["lane-a"] + second["typed"]["lane-a"]
            == both["typed"]["lane-a"] == 1), "the days do not sum to the whole"
