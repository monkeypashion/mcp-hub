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
    assert row["after"]["operator_wakes"]["value"] == 0


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
