"""v2-fleet revision 2 falsifiers, F1 only (Vortex project v2-fleet, built in mcp-hub's repo).

F1 moved (j6-load-meter-daily): r1's load meter, checked in v2-fleet main 1c5568f, becomes
src/mcp_hub/load_meter.py, reachable only as the `load-meter` subcommand of mcp_hub.cli.
The move fixes features-json's LOW L2: the hub snapshot is opened `?mode=ro&immutable=1`, so a
WAL-format backup-API copy gets no -wal/-shm beside it.

F2 (the quiet who-poll) failed its fleet-size check and did not merge; its falsifiers stay
with the build's record and return with the fix stage.

Guards: the contract is a valid schema, and server.py never imports the meter. The
build's byte-level guards on cli.py and squad/squad held those files to the build's base;
they proved the build's scope and stay in its record, not here.

Hermetic: tmp_path fixtures only.
"""
# ruff: noqa: E501 — the build's acceptance tests, kept as written
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
SRC = str(ROOT / "src")
# The CLI runs in a child process; give it this checkout's src/ so an uninstalled checkout
# tests ITS code (pytest's pythonpath reaches only the test process, not its children).
CHILD_ENV = {**os.environ, "PYTHONPATH": SRC + (os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else "")}
SCHEMA = json.loads((ROOT / "contract" / "load-reading.schema.json").read_text())
SQUAD = ROOT / "squad" / "squad"
CLI = ROOT / "src" / "mcp_hub" / "cli.py"
METER = ROOT / "src" / "mcp_hub" / "load_meter.py"

NOW = "2026-10-09T12:00:00Z"
BASE_START, BASE_END = "2026-09-26", "2026-10-03"
FIRST_CHANGE = "2026-10-06T00:00:00Z"
MARK = "ZQX-MARKER-7731"
VOICE = "hub-voice-bridge"


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


# ======================================================================================
# F1 fixtures (r1's, unchanged except the WAL-format snapshot)
# ======================================================================================

HUB_SCHEMA = """
    CREATE TABLE agents (name TEXT PRIMARY KEY, project TEXT NOT NULL DEFAULT '',
        bio TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'online',
        registered REAL NOT NULL, last_seen REAL NOT NULL, meta TEXT NOT NULL DEFAULT '{}',
        transcript_mtime REAL, transcript_count INTEGER, transcript_reported_at REAL);
    CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
        from_agent TEXT NOT NULL, to_agent TEXT, channel TEXT, body TEXT NOT NULL,
        read INTEGER NOT NULL DEFAULT 0, priority TEXT NOT NULL DEFAULT 'normal');
"""


def fill_hub(c):
    c.executescript(HUB_SCHEMA)
    t = ts("2026-09-28T10:00:00Z")
    msgs = [  # voice relay: 4 from the bridge, 2 to it; one lane-to-lane; one from the dead 'operator' name
        (t, VOICE, "lane-a"), (t + 60, VOICE, "lane-b"), (t + 120, VOICE, "lane-a"), (t + 180, VOICE, "lane-b"),
        (t + 240, "lane-a", VOICE), (t + 300, "lane-b", VOICE), (t + 360, "lane-a", "lane-b"),
        (t + 420, "operator", "lane-a"),
        (ts("2026-10-05T10:00:00Z"), VOICE, "lane-a"),  # before the change, outside the baseline
    ]
    c.executemany("INSERT INTO messages (ts, from_agent, to_agent, body) VALUES (?,?,?,?)",
                  [(m[0], m[1], m[2], f"{MARK} body") for m in msgs])
    c.commit()


def make_hub_db(path):
    c = sqlite3.connect(path)
    fill_hub(c)
    c.close()


def make_wal_snapshot(path, scratch):
    """A backup-API copy of a WAL-mode hub: the copy's header says WAL, with no -wal beside it."""
    live = scratch / "live-hub.db"
    src = sqlite3.connect(live)
    src.execute("PRAGMA journal_mode=WAL")
    fill_hub(src)
    dst = sqlite3.connect(path)
    src.backup(dst)
    dst.close()
    src.close()
    for side in ("-wal", "-shm"):
        Path(str(path) + side).unlink(missing_ok=True)
    head = Path(path).read_bytes()[:20]
    assert head[18] == 2 and head[19] == 2, "fixture is not WAL-format"


def make_snapshot(path):
    rows = [["2026-09-28T10:00Z", "lane-a", "operator", "", 4],
            ["2026-09-28T11:00Z", "lane-a", "hold", "normal", 9],
            ["2026-10-05T10:00Z", "lane-a", "operator", "", 7]]
    path.write_text(json.dumps({
        "source": "fixture", "taken_at": NOW, "since": "2026-09-26T00:00Z",
        "columns": ["hour_utc", "recipient", "reason", "held_max", "count"], "rows": rows}))


def line(kind, when, **kw):
    d = {"type": kind, "timestamp": when}
    d.update(kw)
    return json.dumps(d)


def typed(when, kind="human", **kw):
    origin = {"origin": {"kind": kind}} if kind else {}
    return line("user", when, message={"role": "user", "content": f"{MARK} typed"}, **origin, **kw)


def asst(when, **kw):
    return line("assistant", when, message={"role": "assistant", "content": [{"type": "text", "text": MARK}]}, **kw)


def make_transcripts(d):
    d.mkdir()
    day = "2026-09-28T"
    files = {
        "lane-a/s1.jsonl": [
            typed(day + "10:00:00.000Z"),
            asst(day + "10:00:05.000Z"),
            line("user", day + "10:00:10.000Z",
                 message={"role": "user", "content": [{"type": "tool_result", "content": MARK}]}),
            line("user", day + "10:00:20.000Z", toolUseResult={"stdout": MARK},
                 message={"role": "user", "content": MARK}),
            asst(day + "10:00:30.000Z"),
            typed(day + "10:00:40.000Z", isSidechain=True),
            asst(day + "10:00:45.000Z", isSidechain=True),
            typed(day + "10:00:50.000Z", isMeta=True),
            typed(day + "10:01:00.000Z", kind="channel"),
            typed(day + "10:01:30.000Z", kind="task-notification"),
            typed(day + "10:02:00.000Z", kind=None),
            typed(day + "10:02:30.000Z", isCompactSummary=True),
            "not json " + MARK,
            typed(day + "10:10:30.000Z"),
            asst(day + "10:10:40.000Z"),
        ],
        "lane-b/s1.jsonl": [
            line("summary", None, summary=MARK),
            typed(day + "10:00:00.000Z"),
            asst(day + "10:00:10.000Z"),
            typed(day + "10:05:10.000Z"),
            typed(day + "10:05:15.000Z", kind="channel"),
            asst(day + "10:05:20.000Z"),
            typed(day + "10:20:20.000Z"),
            asst(day + "10:20:30.000Z"),
        ],
        "lane-b/s2.jsonl": [typed("2026-08-01T10:00:00.000Z"), asst("2026-08-01T10:00:10.000Z")],
        "lane-b/s3.jsonl": [typed("2026-10-05T10:00:00.000Z"), asst("2026-10-05T10:00:10.000Z")],
    }
    for rel, lines in files.items():
        f = d / rel
        f.parent.mkdir(exist_ok=True)
        f.write_text("\n".join(lines) + "\n")


@pytest.fixture
def env(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    db, wal, snap, tr = tmp_path / "hub.db", tmp_path / "hub-wal.db", tmp_path / "snap.json", tmp_path / "transcripts"
    make_hub_db(db)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    make_wal_snapshot(wal, scratch)
    make_snapshot(snap)
    make_transcripts(tr)
    home = tmp_path / "home"   # the child's pinned HOME; see run(). Made here, before any snapshot.
    home.mkdir()
    return {"db": db, "wal": wal, "snap": snap, "tr": tr, "tmp": tmp_path, "work": work, "home": home}


def run(env, *, db=None, snap=None, tr=None, start=BASE_START, end=BASE_END, changes=None, now=NOW):
    out = env["work"] / "reading.json"
    if out.exists():
        out.unlink()
    cmd = [sys.executable, "-m", "mcp_hub.cli", "load-meter",
           "--hub-db", str(db or env["db"]), "--snapshot", str(snap or env["snap"]),
           "--transcripts", str(tr or env["tr"]), "--out", str(out),
           "--baseline-start", start, "--baseline-end", end, "--now", now]
    if changes is not None:
        cf = env["work"] / "changes.json"
        cf.write_text(json.dumps(changes))
        cmd += ["--changes", str(cf)]
    # HOME is PINNED to this test's tmp dir (declared base test edit, v2f r10). The meter
    # reads the injections ledger from $MCP_HUB_STATE_DIR, else ~/.mcp-hub, so an inherited
    # HOME let this run read the LIVE box's ledger: 58 real rows turned into an
    # injection_unmatched flag and the assertion on `flags` failed by whose machine it ran on.
    child = {k: v for k, v in CHILD_ENV.items() if k != "MCP_HUB_STATE_DIR"}
    child["HOME"] = str(env["home"])
    p = subprocess.run(cmd, capture_output=True, text=True, cwd=env["work"], env=child, timeout=120)
    data = json.loads(out.read_text()) if out.exists() else None
    return p, data


def all_measures(reading):
    sets = [reading["measures"]]
    for ch in reading["changes"]:
        sets += [x for x in (ch["before"], ch["after"]) if x]
    out = []
    for st in sets:
        for v in st.values():
            out += list(v.values()) if "source" not in v else [v]
    return out


CHANGE = {"change_id": "F14", "at": FIRST_CHANGE, "delivered": True}


# ======================================================================================
# Guards: pass on the base and on any good build
# ======================================================================================

def test_guard_contract_is_valid_json_schema():
    Draft202012Validator.check_schema(SCHEMA)


def test_guard_server_never_imports_the_meter():
    # By import, not by text: mcp-hub's base commit names the verb ("load-meter" in _CLI_SUBCOMMANDS, with a
    # comment saying the logic lives in load_meter.py). What must never happen is server.py importing the meter.
    import ast
    tree = ast.parse((ROOT / "src" / "mcp_hub" / "server.py").read_text())
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            imported += [f"{node.module or ''}.{a.name}" for a in node.names] + [node.module or ""]
        elif isinstance(node, ast.Call) and getattr(node.func, "attr", getattr(node.func, "id", "")) in (
                "import_module", "__import__"):
            imported += [a.value for a in node.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]
    assert not [m for m in imported if "load_meter" in m], imported


def test_f1_subcommand_reachable_and_meter_has_no_entry_of_its_own():
    p = subprocess.run([sys.executable, "-m", "mcp_hub.cli", "load-meter", "--help"],
                       capture_output=True, text=True, env=CHILD_ENV, timeout=120)
    assert p.returncode == 0, p.stderr
    for flag in ("--hub-db", "--snapshot", "--transcripts", "--out", "--baseline-start", "--baseline-end"):
        assert flag in p.stdout, flag
    assert METER.is_file()
    src = METER.read_text()
    assert "__main__" not in src, "the meter is reached only through `mcp-hub load-meter`"
    assert not (ROOT / "src" / "mcp_hub" / "__main__.py").exists()


def test_a_every_number_names_its_source_and_validates(env):
    p, r = run(env, changes=[CHANGE])
    assert p.returncode == 0, p.stderr
    assert r is not None
    errors = list(Draft202012Validator(SCHEMA).iter_errors(r))
    assert not errors, errors[0].message
    ms = all_measures(r)
    assert ms and all(m["source"].strip() for m in ms)
    m = r["measures"]
    assert m["typed_turns"]["pane"]["value"] == 5
    assert m["typed_turns"]["pane"]["per_lane"] == {"lane-a": 2, "lane-b": 3}
    assert m["typed_turns"]["voice_relay"]["value"] == 4
    assert m["reaching_tim"]["pane"]["value"] == 5
    assert m["reaching_tim"]["pane"]["per_lane"] == {"lane-a": 2, "lane-b": 3}
    assert m["reaching_tim"]["voice_relay"]["value"] == 2
    assert m["wait_total_seconds"]["pane"]["value"] == 1800
    assert m["wait_median_seconds"]["pane"]["value"] == 600
    assert m["operator_wakes"]["value"] == 4
    assert m["hub_wakes"]["value"] == 2 and m["no_origin"]["value"] == 1
    assert r["baseline"] == {"start": BASE_START, "end": BASE_END, "days": 7}
    assert {i["name"] for i in r["inputs"]} == {"hub_db", "snapshot", "transcripts"}
    assert all(i["read"] for i in r["inputs"])


def test_b_short_baseline_refused(env):
    p, r = run(env, start="2026-09-28", end="2026-10-03")
    assert p.returncode != 0
    assert r is None
    assert "7" in p.stderr and "baseline" in p.stderr.lower()


def test_b_baseline_ending_after_first_change_refused(env):
    p, r = run(env, start="2026-09-30", end="2026-10-07", changes=[CHANGE])
    assert p.returncode != 0
    assert r is None
    assert "first" in p.stderr.lower() and "F14" in p.stderr


def test_b_good_baseline_before_first_change_accepted(env):
    p, r = run(env, changes=[CHANGE])
    assert p.returncode == 0, p.stderr
    assert r is not None


def test_c_delivered_change_without_after_reading_is_flagged(env):
    p, r = run(env, changes=[CHANGE])  # now is 3.5 days after the change: after window incomplete
    assert p.returncode == 0, p.stderr
    ch = {c["change_id"]: c for c in r["changes"]}["F14"]
    assert ch["after"] is None
    assert any(f["code"] == "missing_after" and f["change_id"] == "F14" for f in r["flags"])


def test_c_delivered_change_with_both_readings_is_not_flagged(env):
    p, r = run(env, changes=[CHANGE], now="2026-10-20T00:00:00Z")
    assert p.returncode == 0, p.stderr
    ch = r["changes"][0]
    assert ch["before"] is not None and ch["after"] is not None
    assert r["flags"] == []
    assert ch["before"]["typed_turns"]["pane"]["value"] == 1  # the 5 Oct typed turn
    assert ch["before"]["typed_turns"]["voice_relay"]["value"] == 1
    assert ch["after"]["typed_turns"]["pane"]["value"] == 0  # readable, genuinely zero
    assert ch["after"]["wait_median_seconds"]["pane"]["value"] is None  # no waits: null, not 0


def snapshot_state(env, db):
    files = [db] + sorted(env["tr"].rglob("*.jsonl"))
    return ([(f, f.read_bytes(), f.stat().st_mtime_ns) for f in files],
            sorted(str(x) for x in env["tmp"].rglob("*") if "work" not in x.parts))


def test_d_hub_db_and_transcripts_untouched(env):
    before = snapshot_state(env, env["db"])
    p, r = run(env, changes=[CHANGE])
    assert p.returncode == 0, p.stderr
    assert snapshot_state(env, env["db"]) == before, "bytes, mtimes or files (journal/wal) changed"


def test_d_wal_format_snapshot_gets_no_sidecars_and_reads_the_same(env):
    """L2: a WAL-format backup-API copy opened mode=ro alone grows -wal and -shm beside it."""
    before = snapshot_state(env, env["wal"])
    p, r = run(env, db=env["wal"], changes=[CHANGE])
    assert p.returncode == 0, p.stderr
    for side in ("-wal", "-shm"):
        assert not Path(str(env["wal"]) + side).exists(), f"{side} left beside the snapshot"
    assert snapshot_state(env, env["wal"]) == before, "bytes, mtimes or files changed"
    m = r["measures"]
    assert m["typed_turns"]["voice_relay"]["value"] == 4
    assert m["reaching_tim"]["voice_relay"]["value"] == 2
    assert {i["name"]: i for i in r["inputs"]}["hub_db"]["read"] is True


def test_d_connection_helper_is_immutable_and_refuses_writes(env):
    code = (
        "import sqlite3, sys\n"
        "from mcp_hub import load_meter as m\n"
        "c = m.open_hub_ro(sys.argv[1])\n"
        "uri = [r for r in c.execute('PRAGMA database_list')][0][2]\n"
        "try:\n"
        "    c.execute(\"INSERT INTO messages (ts, from_agent, body) VALUES (1, 'x', 'y')\")\n"
        "except sqlite3.OperationalError as e:\n"
        "    print('refused', e); c.close(); sys.exit(0)\n"
        "sys.exit(3)\n")
    p = subprocess.run([sys.executable, "-c", code, str(env["wal"])], capture_output=True, text=True,
                       env=CHILD_ENV, timeout=120)
    assert p.returncode == 0, p.stderr
    assert "readonly" in p.stdout.lower()
    for side in ("-wal", "-shm"):
        assert not Path(str(env["wal"]) + side).exists(), side
    src = METER.read_text()
    assert "immutable=1" in src and "mode=ro" in src


def test_e_unreadable_source_is_missing_null_never_zero(env):
    p, r = run(env, db=env["tmp"] / "no-such.db", tr=env["tmp"] / "no-such-transcripts")
    assert p.returncode == 0, p.stderr
    assert r is not None
    assert not list(Draft202012Validator(SCHEMA).iter_errors(r))
    m = r["measures"]
    for v in (m["typed_turns"]["pane"], m["typed_turns"]["voice_relay"], m["reaching_tim"]["pane"],
              m["reaching_tim"]["voice_relay"], m["wait_total_seconds"]["pane"], m["wait_median_seconds"]["pane"]):
        assert v["value"] is None
        assert v["missing"].strip()
    assert m["hub_wakes"]["value"] is None and m["hub_wakes"]["missing"].strip()
    assert m["no_origin"]["value"] is None and m["no_origin"]["missing"].strip()
    assert m["operator_wakes"]["value"] == 4  # snapshot still readable
    inputs = {i["name"]: i for i in r["inputs"]}
    assert inputs["hub_db"]["read"] is False and inputs["hub_db"]["missing"]
    assert inputs["transcripts"]["read"] is False and inputs["snapshot"]["read"] is True


def test_e_one_source_missing_leaves_the_other_channel_readable(env):
    p, r = run(env, tr=env["tmp"] / "no-such-transcripts")
    assert p.returncode == 0, p.stderr
    m = r["measures"]
    assert m["typed_turns"]["pane"]["value"] is None
    assert m["typed_turns"]["voice_relay"]["value"] == 4


def test_f_no_text_content_leaks(env):
    p, r = run(env, changes=[CHANGE], now="2026-10-20T00:00:00Z")
    assert p.returncode == 0, p.stderr
    assert MARK not in p.stdout and MARK not in p.stderr
    for f in env["work"].rglob("*"):
        if f.is_file():
            assert MARK not in f.read_text(), f.name
    assert MARK not in json.dumps(r)


def test_g_only_origin_kind_human_is_a_typed_turn(env):
    p, r = run(env)
    assert p.returncode == 0, p.stderr
    m = r["measures"]
    assert m["typed_turns"]["pane"]["per_lane"] == {"lane-a": 2, "lane-b": 3}
    assert m["hub_wakes"]["value"] == 2
    assert m["hub_wakes"]["per_lane"] == {"lane-a": 1, "lane-b": 1}
    assert m["no_origin"]["value"] == 1
    assert "no origin (not counted)" in m["no_origin"]["source"]
    src = m["typed_turns"]["pane"]["source"].lower()
    assert "upper bound" in src and "send-keys" in src
