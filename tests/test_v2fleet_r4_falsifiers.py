"""v2-fleet revision 4 falsifiers (Vortex project v2-fleet, built in mcp-hub's repo). FROZEN.

Three groups, selected by name in each feature's test_command:

test_r4_f1_*   F1 j6-load-meter-daily (changed): coverage-honest, per day, runnable daily.
    1. A change's after window past the wake snapshot's taken_at, with the hub copy lacking
       wake_log: operator_wakes is null with `missing` naming taken_at (on r3 it reads 0).
    2. The same window with the copy's wake_log covering it: the wake_log count, and the source
       names wake_log.
    3. A window past --hub-db-taken-at: voice_relay null with a reason; inside it a real 0 stays 0.
    4. `daily`: one entry per closed UTC day from baseline end to --now, each a measure set that
       validates; a day with a typed turn counts 1; the open day is absent; at most 60 days,
       older ones dropped and flagged `daily_truncated`.
    5. squad/load-meter-daily reads ONLY the local file named by MCP_HUB_DB, writes only under
       $MCP_HUB_STATE_DIR/v2-fleet/readings/, leaves the given file unchanged, and its reading
       validates; without MCP_HUB_DB it exits non-zero and writes nothing. Guard (Term 1): a
       body-less projection (messages(ts, from_agent, to_agent, channel) + wake_log) reads.
    6. squad/systemd/load-meter.service and .timer: the service runs load-meter-daily, the timer
       fires once a day (OnCalendar), and neither enables, installs or sets MCP_HUB_DB.
    7. Term 2: a declared source stop {measure, stopped_at} in changes.json's `source_stops`:
       a before/after pair straddling it is flagged `source_stopped` ("not comparable: source
       stopped", naming the measure) and no saving is reported.
    8. Term 3: every transcript-based count's source names this host and the transcripts dir.
  Guards: the contract is a valid schema and the r2 F1 falsifiers are carried unchanged.

test_r4_f16_*  F16 j6-self-identifying-hub-connection (added): `mcp-hub rebind-url --fleet`.
    Read only, even without --dry-run. One line per comms lane: ok, missing-agent,
    mismatch <found> != <expected>, no-url, or `container: not checkable from host`; exit 0 only
    when every checkable lane is ok. (1) a lane without ?agent= is named and the exit is 1;
    (2) a wrong name is a mismatch; (3) a non-comms lane is not listed; (4) a @docker: lane is
    not checkable and not ok; (5) no byte or mtime of any .mcp.json / .claude.json changes;
    (6) the shared global mcpServers.hub never counts as a lane's stamp.

test_r4_f2_*   F2 (changed): T15 at the LIVE fleet's shape. r3's falsifier
    fleet is 46 plain lanes; the live squad.conf has 31 comms lanes, 5 @docker: lanes and 10
    plain ones. Each comms or container lane reads its hub status every pass, and a container
    lane calls docker. Here docker is a stub that answers at once and logs each call: its
    calls are COUNTED (reported in every message) and their fork cost is in the CPU, but the
    real docker client's own cost is not.
    Falsifier (F2-2): quiet-pass CPU <= 0.1 x the BASE wait (SQUAD_WHO_INTERVAL, the first
    wait), not x the cap. mcp-hub measured the live watcher on 4ed2abc at 0.144 core: every
    change drops the wait to the base, and the live fleet changes about every 40 s, so the
    watcher lives near the base, not the cap. On 4ed2abc: ~2.78 s a pass at a 10 s base, 0.28.
    Guards: F2-1's two bounds at the cap (0.1 x cap; cap + 2 x pass <= 60 s); the fixture is
    the live shape; docker calls a quiet pass <= 20 (4 per container lane, today's count).

Hermetic: tmp dirs only; nothing reaches the live tmux socket, a real ~/.mcp-hub, the hub or a
prod DB. tmux is required for the F2 group (fail, not skip).
"""
# ruff: noqa: E501 — the build's acceptance tests, kept as written
import json
import os
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
SRC = str(ROOT / "src")
CHILD_ENV = {**os.environ, "PYTHONPATH": SRC + (os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else "")}
SCHEMA = json.loads((ROOT / "contract" / "load-reading.schema.json").read_text())
DAILY_SCRIPT = ROOT / "squad" / "load-meter-daily"
UNIT_DIR = ROOT / "squad" / "systemd"
SQUAD = ROOT / "squad" / "squad"

NOW = "2026-10-09T12:00:00Z"            # the wake snapshot's taken_at
BASE_START, BASE_END = "2026-09-26", "2026-10-03"
FIRST_CHANGE = "2026-10-06T00:00:00Z"
LATER = "2026-10-20T00:00:00Z"          # every window of the change has closed
HUB_TAKEN = "2026-10-20T00:00:00Z"      # the hub copy covers everything below it
MARK = "ZQX-MARKER-7731"
VOICE = "hub-voice-bridge"
CHANGE = {"change_id": "F14", "at": FIRST_CHANGE, "delivered": True}


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


# ======================================================================================
# F1 fixtures (r2's shapes; the hub copy may carry wake_log, and may be body-less)
# ======================================================================================

MESSAGES_FULL = """
    CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
        from_agent TEXT NOT NULL, to_agent TEXT, channel TEXT, body TEXT NOT NULL,
        read INTEGER NOT NULL DEFAULT 0, priority TEXT NOT NULL DEFAULT 'normal');
"""
MESSAGES_PROJECTION = """
    CREATE TABLE messages (ts REAL NOT NULL, from_agent TEXT NOT NULL, to_agent TEXT, channel TEXT);
"""
WAKE_LOG = """
    CREATE TABLE wake_log (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
        agent TEXT NOT NULL, reason TEXT NOT NULL, held_max TEXT NOT NULL DEFAULT '');
"""
RELAY = [  # 4 from the bridge, 2 to it, in the baseline; one lane-to-lane; one before the change
    ("2026-09-28T10:00:00Z", VOICE, "lane-a"), ("2026-09-28T10:01:00Z", VOICE, "lane-b"),
    ("2026-09-28T10:02:00Z", VOICE, "lane-a"), ("2026-09-28T10:03:00Z", VOICE, "lane-b"),
    ("2026-09-28T10:04:00Z", "lane-a", VOICE), ("2026-09-28T10:05:00Z", "lane-b", VOICE),
    ("2026-09-28T10:06:00Z", "lane-a", "lane-b"), ("2026-10-05T10:00:00Z", VOICE, "lane-a"),
]
# the after window (10-06 .. 10-13): 3 operator wakes, plus wakes of other reasons
WAKES_AFTER = [("2026-10-07T09:00:00Z", "operator"), ("2026-10-08T09:00:00Z", "operator"),
               ("2026-10-09T09:00:00Z", "operator"), ("2026-10-07T10:00:00Z", "reply"),
               ("2026-10-07T11:00:00Z", "urgent")]


def make_hub(path, *, body=True, wake_log=None, relay=RELAY):
    c = sqlite3.connect(path)
    c.executescript(MESSAGES_FULL if body else MESSAGES_PROJECTION)
    if body:
        c.executemany("INSERT INTO messages (ts, from_agent, to_agent, body) VALUES (?,?,?,?)",
                      [(ts(t), f, to, f"{MARK} body") for t, f, to in relay])
    else:
        c.executemany("INSERT INTO messages (ts, from_agent, to_agent, channel) VALUES (?,?,?,NULL)",
                      [(ts(t), f, to) for t, f, to in relay])
    if wake_log is not None:
        c.executescript(WAKE_LOG)
        c.executemany("INSERT INTO wake_log (ts, agent, reason) VALUES (?,?,?)",
                      [(ts(t), "lane-a", r) for t, r in wake_log])
    c.commit()
    c.close()


def make_snapshot(path, taken_at=NOW):
    rows = [["2026-09-28T10:00Z", "lane-a", "operator", "", 4],
            ["2026-09-28T11:00Z", "lane-a", "hold", "normal", 9],
            ["2026-10-05T10:00Z", "lane-a", "operator", "", 7]]
    path.write_text(json.dumps({
        "source": "fixture", "taken_at": taken_at, "since": "2026-09-26T00:00Z",
        "columns": ["hour_utc", "recipient", "reason", "held_max", "count"], "rows": rows}))


def line(kind, when, **kw):
    d = {"type": kind, "timestamp": when}
    d.update(kw)
    return json.dumps(d)


def typed(when, kind="human"):
    return line("user", when, message={"role": "user", "content": f"{MARK} typed"}, origin={"kind": kind})


def asst(when):
    return line("assistant", when, message={"role": "assistant", "content": [{"type": "text", "text": MARK}]})


def make_transcripts(d):
    d.mkdir()
    files = {
        "lane-a/s1.jsonl": [typed("2026-09-28T10:00:00.000Z"), asst("2026-09-28T10:00:05.000Z"),
                            typed("2026-09-28T10:10:30.000Z"), asst("2026-09-28T10:10:40.000Z"),
                            typed("2026-09-28T10:11:00.000Z", kind="channel")],
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
    e = {"tmp": tmp_path, "work": work,
         "db": tmp_path / "hub.db", "db_wl": tmp_path / "hub-wake-log.db",
         "snap": tmp_path / "snap.json", "snap_late": tmp_path / "snap-late.json",
         "tr": tmp_path / "transcripts"}
    make_hub(e["db"])
    make_hub(e["db_wl"], wake_log=WAKES_AFTER)
    make_snapshot(e["snap"])
    make_snapshot(e["snap_late"], taken_at=LATER)
    make_transcripts(e["tr"])
    return e


def meter(env, *, db=None, snap=None, changes=None, now=LATER, hub_taken=HUB_TAKEN, end=BASE_END):
    out = env["work"] / "reading.json"
    if out.exists():
        out.unlink()
    cmd = [sys.executable, "-m", "mcp_hub.cli", "load-meter",
           "--hub-db", str(db or env["db"]), "--snapshot", str(snap or env["snap"]),
           "--transcripts", str(env["tr"]), "--out", str(out),
           "--baseline-start", BASE_START, "--baseline-end", end, "--now", now,
           "--hub-db-taken-at", hub_taken]
    if changes is not None:
        cf = env["work"] / "changes.json"
        cf.write_text(json.dumps(changes))
        cmd += ["--changes", str(cf)]
    p = subprocess.run(cmd, capture_output=True, text=True, cwd=env["work"], env=CHILD_ENV, timeout=120)
    assert p.returncode == 0, f"load-meter exited {p.returncode}: {p.stderr[-800:]}"
    r = json.loads(out.read_text())
    errors = list(Draft202012Validator(SCHEMA).iter_errors(r))
    assert not errors, errors[0].message
    return r


def change_row(r, cid="F14"):
    return {c["change_id"]: c for c in r["changes"]}[cid]


# ======================================================================================
# F1 guards: pass on the base and on any good build
# ======================================================================================

def test_r4_f1_guard_contract_is_a_valid_schema_with_daily_and_new_flags():
    Draft202012Validator.check_schema(SCHEMA)
    assert "daily" in SCHEMA["properties"]
    codes = SCHEMA["properties"]["flags"]["items"]["properties"]["code"]["enum"]
    assert {"missing_before", "missing_after", "source_stopped", "daily_truncated"} <= set(codes)


def test_r4_f1_guard_r2_falsifiers_carried():
    assert (ROOT / "tests" / "test_v2fleet_r2_falsifiers.py").is_file()


def test_r4_f1_5_guard_a_bodyless_projection_reads(env):
    """Term 1: the copy mcp-hub builds on prod-1 has no body column; the meter reads it."""
    proj = env["tmp"] / "projection.db"
    make_hub(proj, body=False, wake_log=WAKES_AFTER)
    out = env["work"] / "proj.json"
    p = subprocess.run([sys.executable, "-m", "mcp_hub.cli", "load-meter", "--hub-db", str(proj),
                        "--snapshot", str(env["snap"]), "--transcripts", str(env["tr"]), "--out", str(out),
                        "--baseline-start", BASE_START, "--baseline-end", BASE_END, "--now", NOW],
                       capture_output=True, text=True, cwd=env["work"], env=CHILD_ENV, timeout=120)
    assert p.returncode == 0, p.stderr
    r = json.loads(out.read_text())
    assert {i["name"]: i for i in r["inputs"]}["hub_db"]["read"] is True
    assert r["measures"]["typed_turns"]["voice_relay"]["value"] == 4
    assert r["measures"]["reaching_tim"]["voice_relay"]["value"] == 2


# ======================================================================================
# F1 falsifiers
# ======================================================================================

def test_r4_f1_1_window_past_snapshot_taken_at_is_missing_not_zero(env):
    r = meter(env, changes=[CHANGE])                     # copy has no wake_log
    ch = change_row(r)
    after = ch["after"]["operator_wakes"]
    assert after["value"] is None, f"after window ends past the snapshot's taken_at {NOW}, yet reads {after['value']}"
    assert NOW in after["missing"] or NOW.replace(":00Z", "Z") in after["missing"], after["missing"]
    assert ch["before"]["operator_wakes"]["value"] == 7  # inside the snapshot: still read
    assert r["measures"]["operator_wakes"]["value"] == 4


def test_r4_f1_2_wake_log_covers_what_the_snapshot_does_not(env):
    r = meter(env, db=env["db_wl"], changes=[CHANGE])
    ch = change_row(r)
    after = ch["after"]["operator_wakes"]
    assert after["value"] == 3, after
    assert "wake_log" in after["source"], after["source"]
    before = ch["before"]["operator_wakes"]
    assert before["value"] == 7 and "wake_log" not in before["source"], before


def test_r4_f1_3_window_past_hub_copy_is_missing_and_a_real_zero_stays_zero(env):
    r = meter(env, changes=[CHANGE], hub_taken="2026-10-10T00:00:00Z")
    ch = change_row(r)
    for k in ("typed_turns", "reaching_tim"):
        m = ch["after"][k]["voice_relay"]
        assert m["value"] is None, f"{k}: after window ends past the hub copy, yet reads {m['value']}"
        assert m["missing"].strip() and "2026-10-10" in m["missing"], m["missing"]
    assert ch["before"]["typed_turns"]["voice_relay"]["value"] == 1
    r = meter(env, changes=[CHANGE])                     # copy taken 10-20: covers the after window
    ch = change_row(r)
    assert ch["after"]["typed_turns"]["voice_relay"]["value"] == 0
    assert "missing" not in ch["after"]["typed_turns"]["voice_relay"]


def test_r4_f1_4_daily_one_entry_per_closed_day(env):
    r = meter(env, now=NOW, hub_taken=NOW)
    assert "daily" in r, "no per-day readings"
    days = [d["day"] for d in r["daily"]]
    assert days == ["2026-10-03", "2026-10-04", "2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08"], days
    by = {d["day"]: d["measures"] for d in r["daily"]}
    assert by["2026-10-05"]["typed_turns"]["pane"]["value"] == 1
    assert by["2026-10-04"]["typed_turns"]["pane"]["value"] == 0
    assert not any(f["code"] == "daily_truncated" for f in r["flags"])


def test_r4_f1_4_daily_keeps_at_most_sixty_days_and_flags_the_rest(env):
    r = meter(env, now="2026-12-15T06:00:00Z", hub_taken="2026-12-15T06:00:00Z")
    days = [d["day"] for d in r.get("daily", [])]
    assert len(days) == 60, len(days)
    assert days[0] == "2026-10-16" and days[-1] == "2026-12-14", (days[0], days[-1])
    assert any(f["code"] == "daily_truncated" for f in r["flags"]), r["flags"]


# --- 5: the daily script ---------------------------------------------------------------

def _tree(root):
    return {str(p): (p.read_bytes() if p.is_file() else None, p.stat().st_mtime_ns)
            for p in sorted(root.rglob("*")) if not p.is_dir()}


def _daily(env, state, *, with_db=True):
    e = {k: v for k, v in CHILD_ENV.items() if k != "MCP_HUB_DB"}
    e.update(HOME=str(env["tmp"] / "home"), MCP_HUB_STATE_DIR=str(state),
             LOAD_METER_SNAPSHOT=str(env["snap"]), LOAD_METER_TRANSCRIPTS=str(env["tr"]),
             LOAD_METER_BASELINE_START=BASE_START, LOAD_METER_BASELINE_END=BASE_END,
             LOAD_METER_NOW=LATER, LOAD_METER_HUB_DB_TAKEN_AT=HUB_TAKEN,
             LOAD_METER_PYTHON=sys.executable)
    if with_db:
        e["MCP_HUB_DB"] = str(env["given"])
    return subprocess.run(["bash", str(DAILY_SCRIPT)], capture_output=True, text=True,
                          cwd=env["work"], env=e, timeout=120)


@pytest.fixture
def daily_env(env):
    home = env["tmp"] / "home"
    (home / ".mcp-hub").mkdir(parents=True)
    state = env["tmp"] / "state"
    (state / "v2-fleet").mkdir(parents=True)
    (state / "v2-fleet" / "changes.json").write_text(json.dumps({"changes": [CHANGE]}))
    # decoys where a hub DB usually lives: 50 relay DMs each, so reading one shows in the counts
    decoy = [("2026-09-28T12:00:00Z", VOICE, "lane-a")] * 50
    for d in (home / ".mcp-hub" / "hub.db", state / "hub.db"):
        make_hub(d, relay=decoy)
    env["given"] = env["tmp"] / "given" / "projection.db"
    env["given"].parent.mkdir()
    make_hub(env["given"], body=False, wake_log=WAKES_AFTER)
    return env, state


def test_r4_f1_5_daily_script_reads_only_the_given_file_and_writes_only_readings(daily_env):
    env, state = daily_env
    assert DAILY_SCRIPT.is_file(), "squad/load-meter-daily does not exist"
    before = _tree(env["tmp"])
    p = _daily(env, state)
    assert p.returncode == 0, p.stderr[-800:]
    after = _tree(env["tmp"])
    readings = state / "v2-fleet" / "readings"
    changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
    outside = [k for k in changed if not k.startswith(str(readings) + os.sep)]
    assert not outside, f"written outside {readings}: {outside}"
    out = readings / "2026-10-20.json"
    assert out.is_file(), sorted(changed)
    r = json.loads(out.read_text())
    errors = list(Draft202012Validator(SCHEMA).iter_errors(r))
    assert not errors, errors[0].message
    hub = {i["name"]: i for i in r["inputs"]}["hub_db"]
    assert hub["read"] is True and hub["path"] == str(env["given"]), hub
    assert r["measures"]["typed_turns"]["voice_relay"]["value"] == 4, "counts are not the given file's"
    assert change_row(r)["after"]["operator_wakes"]["value"] == 3
    for side in ("-wal", "-shm", "-journal"):
        assert not Path(str(env["given"]) + side).exists(), side


def test_r4_f1_5_daily_script_without_mcp_hub_db_refuses_and_writes_nothing(daily_env):
    env, state = daily_env
    assert DAILY_SCRIPT.is_file(), "squad/load-meter-daily does not exist"
    before = _tree(env["tmp"])
    p = _daily(env, state, with_db=False)
    assert p.returncode != 0
    assert _tree(env["tmp"]) == before, "files written without MCP_HUB_DB"
    assert "MCP_HUB_DB" in (p.stderr + p.stdout)


# --- 6: the units ----------------------------------------------------------------------

def _unit_lines(path):
    return [ln.strip() for ln in path.read_text().splitlines() if ln.strip() and not ln.strip().startswith(("#", ";"))]


def test_r4_f1_6_units_run_daily_and_install_nothing():
    svc, tmr = UNIT_DIR / "load-meter.service", UNIT_DIR / "load-meter.timer"
    assert svc.is_file() and tmr.is_file(), "squad/systemd/load-meter.service and .timer"
    s, t = _unit_lines(svc), _unit_lines(tmr)
    execs = [ln for ln in s if ln.startswith("ExecStart")]
    assert execs and all("load-meter-daily" in ln for ln in execs), execs
    cal = [ln.split("=", 1)[1].strip() for ln in t if ln.startswith("OnCalendar")]
    assert len(cal) == 1 and re.fullmatch(r"(daily|\*-\*-\* \d{1,2}:\d{2}(:\d{2})?)( UTC)?", cal[0]), cal
    assert not [ln for ln in t if ln.startswith(("OnUnitActiveSec", "OnUnitInactiveSec", "OnBootSec", "OnActiveSec"))]
    for ln in s + t:
        low = ln.lower()
        if ln.startswith("Exec"):
            assert "systemctl" not in low and "enable" not in low and "install" not in low, ln
        assert not re.match(r"Environment\s*=.*MCP_HUB_DB", ln), f"MCP_HUB_DB is mcp-hub's drop-in, not the unit's: {ln}"


# --- 7: a stopped source is not a saving (Term 2) -------------------------------------

def test_r4_f1_7_a_stopped_source_is_flagged_not_comparable(env):
    doc = {"changes": [CHANGE],
           "source_stops": [{"measure": "operator_wakes", "stopped_at": "2026-10-08T00:00:00Z"}]}
    r = meter(env, snap=env["snap_late"], changes=doc)
    ch = change_row(r)
    assert ch["before"]["operator_wakes"]["value"] == 7
    flags = [f for f in r["flags"] if f["code"] == "source_stopped" and f.get("change_id") == "F14"]
    assert flags, f"no source_stopped flag: {r['flags']}"
    assert "not comparable: source stopped" in flags[0]["detail"] and "operator_wakes" in flags[0]["detail"], flags[0]
    assert "saving" not in json.dumps(ch).lower()


# --- 8: transcript counts name the host and the dir (Term 3) --------------------------

def test_r4_f1_8_transcript_sources_name_host_and_dir(env):
    r = meter(env, changes=[CHANGE])
    host = socket.gethostname()
    sets = [r["measures"], change_row(r)["before"], change_row(r)["after"]]
    for st in sets:
        for m in (st["typed_turns"]["pane"], st["reaching_tim"]["pane"], st["wait_total_seconds"]["pane"],
                  st["wait_median_seconds"]["pane"], st["hub_wakes"], st["no_origin"]):
            assert host in m["source"] and str(env["tr"]) in m["source"], m["source"]


# ======================================================================================
# F16: rebind-url --fleet
# ======================================================================================

HOST = "testhost"
COMMS = "--dangerously-load-development-channels server:hub"


@pytest.fixture
def fleet16(tmp_path, monkeypatch):
    from mcp_hub import cli
    home = tmp_path / "home"
    (home / ".mcp-hub").mkdir(parents=True)
    work = tmp_path / "w"
    lanes = {  # agent: (repo, args, how its URL is stamped)
        "good-testhost": ("good", COMMS, ("mcp", "http://hub:8090/mcp?agent=good-testhost")),
        "override-testhost": ("override", COMMS, ("override", "http://hub:8090/mcp?agent=override-testhost")),
        "noagent-testhost": ("noagent", COMMS, ("mcp", "http://hub:8090/mcp")),
        "wrong-testhost": ("wrong", COMMS, ("mcp", "http://hub:8090/mcp?agent=someone-else")),
        "globalonly-testhost": ("globalonly", COMMS, (None, None)),
        "plain-testhost": ("plain", "--continue", ("mcp", "http://hub:8090/mcp")),
        "boxed": ("boxed", "@docker:ctr-boxed", (None, None)),
    }
    remotes, claude_json = {}, {
        "mcpServers": {"hub": {"url": "http://hub:8090/mcp?agent=globalonly-testhost"}}, "projects": {}}
    conf = []
    for agent, (repo, args, (how, url)) in lanes.items():
        wt = work / repo
        wt.mkdir(parents=True)
        remotes[str(wt)] = f"git@github.com:org/{repo}.git"
        if how == "mcp":
            (wt / ".mcp.json").write_text(json.dumps({"mcpServers": {"hub": {"type": "http", "url": url}}}))
        elif how == "override":
            claude_json["projects"][str(wt)] = {"mcpServers": {"hub": {"type": "http", "url": url}}}
        conf.append(f"{agent}|{wt}||{args}|" + ("faculty" if args.startswith("@docker:") else ""))
    (home / ".claude.json").write_text(json.dumps(claude_json))
    cfg = home / ".mcp-hub" / "config.json"
    cfg.write_text(json.dumps({"projects": [f"org/{lanes[a][0]}" for a in lanes]}))
    squad_conf = tmp_path / "squad.conf"
    squad_conf.write_text("\n".join(conf) + "\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SQUAD_CONF", str(squad_conf))
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    monkeypatch.setattr(cli, "SQUAD_CONF", squad_conf)
    monkeypatch.setattr(cli, "_HUB_CONFIG_PATH", cfg)
    monkeypatch.setattr(cli, "_git_remote_url", lambda cwd: remotes.get(str(cwd)))
    monkeypatch.setattr(cli.platform, "node", lambda: HOST)
    return {"cli": cli, "home": home, "work": work, "lanes": lanes}


def _fleet_check(f, capsys, *extra):
    try:
        rc = f["cli"].main(["rebind-url", "--fleet", *extra])
    except SystemExit as e:
        rc = e.code
    out = capsys.readouterr()
    return rc, out.out + out.err


def _lane_line(text, lane):
    hits = [ln for ln in text.splitlines() if re.search(rf"(?<![\w-]){re.escape(lane)}(?![\w-])", ln)]
    return hits[-1] if hits else None


def test_r4_f16_1_lane_without_agent_is_named_and_exit_is_1(fleet16, capsys):
    rc, out = _fleet_check(fleet16, capsys)
    ln = _lane_line(out, "noagent-testhost")
    assert ln is not None and "missing-agent" in ln, out
    assert rc == 1, (rc, out)
    good = _lane_line(out, "good-testhost")
    assert good is not None and re.search(r"\bok\b", good), out
    over = _lane_line(out, "override-testhost")
    assert over is not None and re.search(r"\bok\b", over), out


def test_r4_f16_2_wrong_name_is_a_mismatch(fleet16, capsys):
    rc, out = _fleet_check(fleet16, capsys)
    ln = _lane_line(out, "wrong-testhost")
    assert ln is not None and "mismatch" in ln and "someone-else" in ln, out
    assert rc == 1


def test_r4_f16_3_non_comms_lane_is_not_listed(fleet16, capsys):
    rc, out = _fleet_check(fleet16, capsys)
    assert _lane_line(out, "good-testhost") is not None, out   # the check ran
    assert _lane_line(out, "plain-testhost") is None, out


def test_r4_f16_4_container_lane_is_not_checkable_and_not_ok(fleet16, capsys):
    rc, out = _fleet_check(fleet16, capsys)
    ln = _lane_line(out, "boxed")
    assert ln is not None and "container: not checkable from host" in ln, out
    assert not re.search(r"\bok\b", ln.replace("container: not checkable from host", "")), ln


def test_r4_f16_5_fleet_check_writes_nothing_even_without_dry_run(fleet16, capsys):
    f = fleet16
    files = sorted(f["work"].rglob(".mcp.json")) + [f["home"] / ".claude.json"]
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in files}
    listing = sorted(str(p) for p in f["home"].parent.rglob("*"))
    rc, out = _fleet_check(f, capsys)
    assert _lane_line(out, "noagent-testhost") is not None, out   # the check ran
    assert {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in files} == before
    assert sorted(str(p) for p in f["home"].parent.rglob("*")) == listing, "files created"


def test_r4_f16_6_global_hub_url_is_never_a_lanes_stamp(fleet16, capsys):
    rc, out = _fleet_check(fleet16, capsys)
    ln = _lane_line(out, "globalonly-testhost")
    assert ln is not None and "no-url" in ln, out
    assert not re.search(r"\bok\b", ln), ln


def test_r4_f16_guard_plain_rebind_url_still_parses():
    p = subprocess.run([sys.executable, "-m", "mcp_hub.cli", "rebind-url", "--help"],
                       capture_output=True, text=True, env=CHILD_ENV, timeout=120)
    assert p.returncode == 0 and "--dry-run" in p.stdout, p.stderr


# ======================================================================================
# F2 at the live fleet's shape (conditional falsifier, else a guard)
# ======================================================================================

N_COMMS, N_DOCKER, N_PLAIN = 31, 5, 10
COMMS_LANES = tuple(f"comms{i:02d}" for i in range(N_COMMS))
DOCKER_LANES = tuple(f"boxed{i:02d}" for i in range(N_DOCKER))
PLAIN_LANES = tuple(f"plain{i:02d}" for i in range(N_PLAIN))
ALL_LANES = COMMS_LANES + DOCKER_LANES + PLAIN_LANES
CORE_LIMIT = 0.1
MINUTE_S = 60.0
QUIET_WAITS = 6
PHASE_DEADLINE_S = 900.0
IDLE_PANE = "echo ready; exec sleep 3600"
CLK = os.sysconf("SC_CLK_TCK")
STATUS = '{"online": true, "wakeable": true, "focus_until": 0}'


def _live_shape_fleet(tmp):
    home = tmp / "home"
    (home / ".config" / "squad").mkdir(parents=True)
    hub = home / ".mcp-hub"
    hub.mkdir()
    (hub / "board-recap.cache").write_text("")
    work = tmp / "w"
    work.mkdir()
    rows = [f"{a}|{work}||{COMMS} --continue\n" for a in COMMS_LANES]
    rows += [f"{a}|{work}||@docker:ctr-{a}|faculty\n" for a in DOCKER_LANES]
    rows += [f"{a}|{work}||--continue\n" for a in PLAIN_LANES]
    conf = home / ".config" / "squad" / "squad.conf"
    conf.write_text("".join(rows), encoding="utf-8")
    fresh = time.time() + 3600   # `find -mmin -3` stays true for the whole run
    for a in COMMS_LANES:
        sf = hub / f"status-{a}.json"
        sf.write_text(STATUS)
        os.utime(sf, (fresh, fresh))
    bindir = tmp / "bin"
    bindir.mkdir()
    real_tmux = shutil.which("tmux")
    (bindir / "tmux").write_text('#!/bin/sh\ncase "$1" in -S) exit 1 ;; esac\n'
                                 f'exec {real_tmux} "$@"\n')
    calls = tmp / "calls"
    # docker: answers at once and logs the call. A container is up, its pane shows `ready`,
    # its status cache is fresh and wakeable.
    (bindir / "docker").write_text(
        "#!/bin/sh\n"
        f'printf "D %s\\n" "$*" >> {calls}\n'
        'case "$1" in\n'
        '  ps) case "$*" in *name=^*) n="${*#*name=^}"; echo "${n%%[$]*}" ;;\n'
        f'        *) printf "%s\\n" {" ".join("ctr-" + a for a in DOCKER_LANES)} ;; esac ;;\n'
        '  exec) shift; shift\n'
        '    case "$1" in\n'
        '      tmux) echo ready ;;\n'
        '      find) echo "$2" ;;\n'
        f"      cat) echo '{STATUS}' ;;\n"
        '    esac ;;\n'
        'esac\n'
        'exit 0\n')
    waits, pids = tmp / "waits", tmp / "pids"
    (bindir / "sleep").write_text(
        "#!/bin/sh\n"
        'read -r up _ < /proc/uptime\n'
        f'w=; s=; [ -r {pids} ] && read -r w s < {pids}\n'
        'ws=; ss=\n'
        '[ -n "$w" ] && [ -r "/proc/$w/stat" ] && read -r ws < "/proc/$w/stat"\n'
        '[ -n "$s" ] && [ -r "/proc/$s/stat" ] && read -r ss < "/proc/$s/stat"\n'
        f'printf "W %s %s | %s | %s\\n" "$up" "$*" "$ws" "$ss" >> {waits}\n'
        f'printf "W\\n" >> {calls}\n'
        "exit 0\n")
    for f in ("tmux", "docker", "sleep"):
        (bindir / f).chmod(0o755)
    tmpdir = Path(tempfile.mkdtemp(prefix="v2f4-"))
    sock = f"r4who-{uuid.uuid4().hex[:8]}"
    env = {k: v for k, v in os.environ.items() if not k.startswith("TMUX")}
    env.update(HOME=str(home), SQUAD_CONF=str(conf), SQUAD_SOCKET=sock, TMUX_TMPDIR=str(tmpdir),
               PATH=f"{bindir}:{os.environ['PATH']}", MCP_HUB_STATE_DIR=str(hub),
               LANG="C.UTF-8", SQUAD_BOARD_USAGE_TTL="0")
    for k in ("SQUAD_WHO_INTERVAL", "SQUAD_WHO_MAX_INTERVAL"):
        env.pop(k, None)
    t = [real_tmux, "-L", sock]
    tenv = {**env, "PATH": os.environ["PATH"]}
    for a in COMMS_LANES + PLAIN_LANES:
        subprocess.run(t + ["new-session", "-d", "-s", a, IDLE_PANE], check=True, env=tenv)
    return env, t, tenv, tmpdir, waits, pids, calls


def _cpu_ticks(stat):
    if not stat.strip():
        return None
    f = stat.rsplit(")", 1)[1].split()
    return [int(x) for x in f[11:15]]


def _waits(log):
    out = []
    for ln in (log.read_text().splitlines() if log.exists() else []):
        head, _, rest = ln.partition(" | ")
        parts = head.split()
        if len(parts) != 3 or parts[0] != "W":
            continue
        ws, _, ss = rest.partition(" | ")
        w, s = _cpu_ticks(ws), _cpu_ticks(ss)
        try:
            out.append((float(parts[1]), float(parts[2]), sum(w) if w else None, (s[0] + s[1]) if s else None))
        except ValueError:
            pass
    return out


def _docker_per_pass(calls):
    """Docker calls between consecutive waits: [n for the pass after wait 1, after wait 2, ...]."""
    per, cur, seen = [], 0, 0
    for ln in (calls.read_text().splitlines() if calls.exists() else []):
        if ln == "W":
            if seen:
                per.append(cur)
            seen, cur = seen + 1, 0
        elif ln.startswith("D "):
            cur += 1
    return per


@pytest.fixture(scope="module")
def live_shape_run(tmp_path_factory):
    assert shutil.which("tmux"), "these falsifiers need tmux on PATH (apt-get install tmux)"
    tmp = tmp_path_factory.mktemp("r4who")
    env, t, tenv, tmpdir, waits, pids, calls = _live_shape_fleet(tmp)
    srv = subprocess.run(t + ["display-message", "-p", "#{pid}"], capture_output=True, text=True,
                         env=tenv, check=True).stdout.strip()
    out, err = tmp / "stdout", tmp / "stderr"
    rec = {}
    proc = None
    try:
        # the journal names states only; the one-shot board shows each lane's hub glyph
        one = subprocess.run(["bash", str(SQUAD), "who"], capture_output=True, env=env,
                             stdin=subprocess.DEVNULL, timeout=300)
        rec["oneshot"] = one.stdout
        with open(out, "wb") as fo, open(err, "wb") as fe:
            proc = subprocess.Popen(["bash", str(SQUAD), "who", "--watch"], stdout=fo, stderr=fe,
                                    stdin=subprocess.DEVNULL, env=env, start_new_session=True)
        pids.write_text(f"{proc.pid} {srv}\n")
        deadline = time.time() + PHASE_DEADLINE_S
        while time.time() < deadline and len(_waits(waits)) < QUIET_WAITS and proc.poll() is None:
            time.sleep(0.2)
        rec["quiet_ok"] = len(_waits(waits)) >= QUIET_WAITS
        rec["out"] = out.read_bytes()
        rec["err"] = err.read_bytes()
        rec["alive"] = proc.poll() is None
    finally:
        if proc is not None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=20)
        subprocess.run(t + ["kill-server"], capture_output=True, env=tenv)
        shutil.rmtree(tmpdir, ignore_errors=True)
    rec["waits"] = _waits(waits)[:QUIET_WAITS]
    rec["docker"] = _docker_per_pass(calls)[:QUIET_WAITS - 1]
    return rec


def _quiet(rec):
    qw = rec["waits"]
    assert rec["quiet_ok"] and len(qw) >= QUIET_WAITS, f"only {len(qw)} passes completed (needed {QUIET_WAITS})"
    assert all(w[2] is not None and w[3] is not None for w in qw), "CPU counters not recorded"
    cpu = [((b[2] - a[2]) + (b[3] - a[3])) / CLK for a, b in zip(qw, qw[1:])]
    secs = [w[1] for w in qw]
    cap = max(secs)
    assert secs.count(cap) >= 2 and secs[-1] == cap, f"the waits never settled at a cap: {secs}"
    return cpu, cap


def _shape(rec):
    return (f"{len(ALL_LANES)} lanes ({N_COMMS} comms, {N_DOCKER} docker, {N_PLAIN} plain); "
            f"docker calls a quiet pass {rec['docker']} (stubbed: their fork cost is counted, the real client's is not)")


def test_r4_f2_1_quiet_pass_cpu_at_live_shape_under_a_tenth_of_a_core(live_shape_run):
    cpu, cap = _quiet(live_shape_run)
    per_pass = sum(cpu) / len(cpu)
    assert per_pass <= CORE_LIMIT * cap, (
        f"{_shape(live_shape_run)}: a quiet pass costs {per_pass:.2f} s CPU "
        f"(passes {[round(c, 2) for c in cpu]}); at the {cap:g} s cap that is {per_pass / cap:.3f} core (limit {CORE_LIMIT})")


def test_r4_f2_1_change_within_a_minute_at_live_shape(live_shape_run):
    cpu, cap = _quiet(live_shape_run)
    per_pass = sum(cpu) / len(cpu)
    worst = cap + 2 * per_pass
    assert worst <= MINUTE_S, (
        f"{_shape(live_shape_run)}: cap {cap:g} s + 2 x {per_pass:.2f} s pass = {worst:.1f} s (limit {MINUTE_S:g} s)")


def test_r4_f2_2_quiet_pass_cpu_under_a_tenth_of_a_core_at_the_base_interval(live_shape_run):
    """The live watcher sits near the base wait (each change resets to it), so T15 is judged there."""
    cpu, cap = _quiet(live_shape_run)
    base = live_shape_run["waits"][0][1]
    per_pass = sum(cpu) / len(cpu)
    assert per_pass <= CORE_LIMIT * base, (
        f"{_shape(live_shape_run)}: a quiet pass costs {per_pass:.2f} s CPU "
        f"(passes {[round(c, 2) for c in cpu]}); at the {base:g} s base wait that is "
        f"{per_pass / base:.3f} core (limit {CORE_LIMIT}): make the pass cheaper, or the base longer")
    assert cap + 2 * per_pass <= MINUTE_S, f"cap {cap:g} s + 2 x {per_pass:.2f} s > {MINUTE_S:g} s"


def test_r4_f2_guard_fixture_is_the_live_shape(live_shape_run):
    """The measure is only worth its number if the glyph and docker paths actually ran."""
    rec = live_shape_run
    assert rec["alive"], rec["err"][-400:]
    lines = [x for x in rec["out"].decode(errors="replace").splitlines() if x.strip()]
    for a in ALL_LANES:
        hit = [ln for ln in lines if re.search(rf"\b{a}\b", ln)]
        assert hit, f"{a} not on the board"
        assert "down" not in hit[0], hit[0]
    board = [x for x in rec["oneshot"].decode(errors="replace").splitlines() if x.strip()]
    for a in COMMS_LANES + DOCKER_LANES:
        hit = [ln for ln in board if re.search(rf"\b{a}\b", ln)]
        assert hit and "⚡" in hit[0], f"{a}: hub glyph not read: {hit[:1]}"
    assert rec["docker"] and min(rec["docker"]) >= N_DOCKER, f"docker calls a pass: {rec['docker']}"
    # pinned at r3's merge (4ed2abc): liveness, capture_for and hub_glyph's two execs per container lane
    assert max(rec["docker"]) <= 4 * N_DOCKER, f"docker calls a quiet pass grew past {4 * N_DOCKER}: {rec['docker']}"
    assert rec["err"] == b"", rec["err"][:400]


# ======================================================================================
# F2 guards on REAL time: the glyph a cheaper pass shows must still follow the clock and the
# file. Read from each lane's tmux title (set-titles-string on the private socket). The
# `sleep` on PATH only logs each wait and then really sleeps: no clock is stubbed.
# ======================================================================================

GLYPH_BASE, GLYPH_CAP = 2, 4          # a short poll, so two passes fit around each event
STALE_AGE_S = 170                     # hub_glyph: `find -mmin -3`, i.e. stale past 180 s
FOCUS_S = 8


def _status_body(online=True, focus_until=None):
    # offline is written with wakeable false too: hub_glyph's `*"online"*true*` pattern also
    # matches a later `"wakeable": true`, so {"online": false, "wakeable": true} reads ⚡ today
    body = {"online": online, "wakeable": online}
    if focus_until is not None:
        body["focus_until"] = focus_until
    return json.dumps(body)


@pytest.fixture(scope="module")
def glyph_run(tmp_path_factory):
    assert shutil.which("tmux"), "these guards need tmux on PATH (apt-get install tmux)"
    tmp = tmp_path_factory.mktemp("r4glyph")
    home = tmp / "home"
    (home / ".config" / "squad").mkdir(parents=True)
    hub = home / ".mcp-hub"
    hub.mkdir()
    (hub / "board-recap.cache").write_text("")
    work = tmp / "w"
    work.mkdir()
    lanes = ("stale01", "focus01", "flip01")
    conf = home / ".config" / "squad" / "squad.conf"
    conf.write_text("".join(f"{a}|{work}||{COMMS} --continue\n" for a in lanes), encoding="utf-8")
    bindir = tmp / "bin"
    bindir.mkdir()
    real_tmux, real_sleep = shutil.which("tmux"), shutil.which("sleep")
    (bindir / "tmux").write_text('#!/bin/sh\ncase "$1" in -S) exit 1 ;; esac\n'
                                 f'exec {real_tmux} "$@"\n')
    (bindir / "docker").write_text("#!/bin/sh\nexit 0\n")
    waits = tmp / "waits"
    (bindir / "sleep").write_text("#!/bin/sh\n"
                                  'read -r up _ < /proc/uptime\n'
                                  f'printf "W %s %s\\n" "$up" "$*" >> {waits}\n'
                                  f'exec {real_sleep} "$@"\n')
    for f in ("tmux", "docker", "sleep"):
        (bindir / f).chmod(0o755)
    tmpdir = Path(tempfile.mkdtemp(prefix="v2f4g-"))
    sock = f"r4gl-{uuid.uuid4().hex[:8]}"
    env = {k: v for k, v in os.environ.items() if not k.startswith("TMUX")}
    env.update(HOME=str(home), SQUAD_CONF=str(conf), SQUAD_SOCKET=sock, TMUX_TMPDIR=str(tmpdir),
               PATH=f"{bindir}:{os.environ['PATH']}", MCP_HUB_STATE_DIR=str(hub), LANG="C.UTF-8",
               SQUAD_WHO_INTERVAL=str(GLYPH_BASE), SQUAD_WHO_MAX_INTERVAL=str(GLYPH_CAP))
    t = [real_tmux, "-L", sock]
    tenv = {**env, "PATH": os.environ["PATH"]}
    for a in lanes:
        subprocess.run(t + ["new-session", "-d", "-s", a, IDLE_PANE], check=True, env=tenv)

    def boot_at(wall):        # a wall-clock instant on the boottime axis the waits use
        return wall - time.time() + time.clock_gettime(time.CLOCK_BOOTTIME)

    now = time.time()
    stale, focus, flip = (hub / f"status-{a}.json" for a in lanes)
    stale.write_text(_status_body())
    os.utime(stale, (int(now) - STALE_AGE_S, int(now) - STALE_AGE_S))
    focus_until = int(now) + FOCUS_S
    focus.write_text(_status_body(focus_until=focus_until))
    flip.write_text(_status_body())
    rec = {"events": {"stale01": boot_at(int(now) - STALE_AGE_S + 180), "focus01": boot_at(focus_until)},
           "samples": {a: [] for a in lanes}}
    proc = None
    try:
        proc = subprocess.Popen(["bash", str(SQUAD), "who", "--watch"], stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, env=env,
                                start_new_session=True)
        deadline = time.time() + 300
        flipped_at_wall = None
        while time.time() < deadline and proc.poll() is None:
            b = time.clock_gettime(time.CLOCK_BOOTTIME)
            for a in lanes:
                title = subprocess.run(t + ["show", "-t", a, "-v", "set-titles-string"], capture_output=True,
                                       text=True, env=tenv).stdout.strip()
                rec["samples"][a].append((b, title))
            if flipped_at_wall is None and rec["samples"]["flip01"][-1][1].startswith("⚡") \
                    and time.time() - now >= 1.5:
                flip.write_text(_status_body(online=False))     # a REWRITE, a second or more later
                flipped_at_wall = time.time()
                rec["events"]["flip01"] = boot_at(flipped_at_wall)
            last = max(rec["events"].values())
            if len(rec["events"]) == 3 and len([w for w in _glyph_waits(waits) if w > last]) >= 3:
                break
            time.sleep(0.2)
        rec["alive"] = proc.poll() is None
    finally:
        if proc is not None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=20)
        subprocess.run(t + ["kill-server"], capture_output=True, env=tenv)
        shutil.rmtree(tmpdir, ignore_errors=True)
    rec["waits"] = _glyph_waits(waits)
    return rec


def _glyph_waits(log):
    out = []
    for ln in (log.read_text().splitlines() if log.exists() else []):
        p = ln.split()
        if len(p) == 3 and p[0] == "W":
            out.append(float(p[1]))
    return out


def _glyph_follows(rec, lane, before, after):
    """`before` shows on the title before the event; `after` by the end of the second pass after it."""
    assert rec["alive"], "the watcher died"
    ev = rec["events"].get(lane)
    assert ev is not None, f"{lane}: its event never happened"
    seen = rec["samples"][lane]
    early = [title for b, title in seen if b < ev]
    assert any(before(x) for x in early), f"{lane}: never showed the first glyph before the event: {early[-3:]}"
    later = [w for w in rec["waits"] if w > ev]
    assert len(later) >= 2, f"{lane}: fewer than two passes after the event: {later}"
    by = later[1]
    titles = [title for b, title in seen if b >= by]
    assert titles, f"{lane}: no title sampled after the second pass"
    assert after(titles[0]), f"{lane}: two passes after the event the title reads {titles[0]!r}"


def test_r4_f2_guard_a_status_file_ageing_past_three_minutes_reads_stale(glyph_run):
    _glyph_follows(glyph_run, "stale01", lambda x: x.startswith("⚡"), lambda x: x.startswith("?"))


def test_r4_f2_guard_an_elapsed_focus_drops_the_silencer(glyph_run):
    _glyph_follows(glyph_run, "focus01", lambda x: x.startswith("⚡🔕"),
                   lambda x: x.startswith("⚡") and "🔕" not in x)


def test_r4_f2_guard_a_rewrite_to_offline_shows_offline(glyph_run):
    _glyph_follows(glyph_run, "flip01", lambda x: x.startswith("⚡"), lambda x: x.startswith("✖"))
