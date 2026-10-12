"""v2-fleet revision 2 falsifiers (Vortex project v2-fleet, built in mcp-hub's repo). FROZEN.

F1 moved (j6-load-meter-daily): r1's load meter, checked in v2-fleet main 1c5568f, becomes
src/mcp_hub/load_meter.py, reachable only as the `load-meter` subcommand of mcp_hub.cli.
The move fixes features-json's LOW L2: the hub snapshot is opened `?mode=ro&immutable=1`, so a
WAL-format backup-API copy gets no -wal/-shm beside it.

F2 (j6-squad-who-quiet-poll): `squad who --watch` with stdout not a terminal (the journal)
writes one line per lane on its first pass, then only a line per lane state change; it backs
its poll off while nothing changes (doubling from SQUAD_WHO_INTERVAL up to
SQUAD_WHO_MAX_INTERVAL) and drops back to SQUAD_WHO_INTERVAL on a change. It waits between
passes with one `sleep <seconds>` per pass, as today; the falsifiers read those waits.

Guards (pass on the base and on any good build): the contract and frozen paths; cli.py
changes only inside build_parser() and main() and only by adding lines; squad/squad changes
only inside the `who)` arm (new top-level functions are allowed); server.py never imports
the meter.

Hermetic: tmp_path fixtures only. The F2 tests run squad on a PRIVATE tmux socket under a
private TMUX_TMPDIR, with HOME, SQUAD_CONF and a fake docker in tmp_path; the tmux wrapper
refuses `-S <socket>` so the box-wide inventory never lists the live fleet's sockets.
Every child runs in its own process group and is stopped by that group's id.
"""
import ast
import difflib
import hashlib
import json
import os
import re
import shutil
import signal
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
    return {"db": db, "wal": wal, "snap": snap, "tr": tr, "tmp": tmp_path, "work": work}


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
    p = subprocess.run(cmd, capture_output=True, text=True, cwd=env["work"], env=CHILD_ENV, timeout=120)
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


def test_guard_frozen_paths_name_existing_files():
    paths = json.loads((ROOT / ".claude" / "frozen-paths.json").read_text())["paths"]
    for p in ("contract/load-reading.schema.json", "tests/test_v2fleet_r2_falsifiers.py",
              "src/mcp_hub/server.py", "src/mcp_hub/api_v1.py", ".claude/frozen-paths.json"):
        assert p in paths, p
    assert all((ROOT / p).exists() for p in paths)


def test_guard_server_never_imports_the_meter():
    src = (ROOT / "src" / "mcp_hub" / "server.py").read_text()
    assert "load_meter" not in src and "load-meter" not in src


# cli.py: every top-level statement except build_parser() and main() is byte-identical to the
# base (c587989); no top-level def is added or removed; inside build_parser() and main() the
# base's lines all survive, in order (lines may only be added).
CLI_OPEN = ("build_parser", "main")
CLI_REST_SHA = "97f51e2f6a914650"
CLI_DEFS = {
    "TranscriptActivity": "f5fb1a3a3246bd8f",
    "_agents_in_workspace": "5ac15aac87eb6335",
    "_all_already_delivered": "e70bc0e48b43a192",
    "_all_low_priority": "16515bc06533faaa",
    "_call_heartbeat": "a8d7e4daa81b9c3e",
    "_capsule_attach": "beb47626b8657b99",
    "_card_nag_grace": "0720740c857c0dec",
    "_claim_singleton": "991d872ffa15c2f5",
    "_claude_memory_dir": "7792c884958a1168",
    "_claude_project_dirname": "2c8bcb77ebe2d64f",
    "_closing_section": "d045833412d11ec3",
    "_daemon_alive_for": "dda7822569cc971b",
    "_defer_spool": "e61d33ee458f8ca3",
    "_derive_agent_identity": "1e5c151d71d61fa7",
    "_dig": "d551aeff4a937927",
    "_discover_agent_from_marker": "66a3d2c07b0c1552",
    "_edit": "3763cfd7f401aab4",
    "_elapsed_ms": "d6c9062f386a6da2",
    "_emit": "d7b7038cae7374de",
    "_ensure_daemon_alive": "50b0fef28cc574c3",
    "_ephemeral_hub_url": "e9822b324972d193",
    "_explicit_project": "57aa06c7f6d43b6d",
    "_extract_decided": "090dbd566f260fe3",
    "_extract_decision_card": "b281de4be9020946",
    "_extract_text": "e443402a16f766d5",
    "_fleet_board_path": "9a03c2e8e7e750fe",
    "_fmt_until": "7162f9cb81e5e8fb",
    "_git_remote_url": "0681575df2263c87",
    "_heartbeat_loop": "672c8694cab8ae3b",
    "_heartbeat_pidfile": "62f7525a642ae318",
    "_history_stale_fields": "11f1bfd40665c639",
    "_history_unclassified_paths": "cabcfa86eb7a372d",
    "_hub_config_candidates": "70ed4658d2e0685d",
    "_is_live_daemon": "9937e81dbc23cda7",
    "_is_safe_memory_filename": "02cc2e7709bff5e8",
    "_known_workspace_files": "aaecdbb43f6cafc7",
    "_launch_flags": "d326600e5180002c",
    "_launch_pair": "72c4e2db5115527c",
    "_load_hub_config": "166de3ee8e4fccb8",
    "_log_activity_event": "82f12f9ce058cc74",
    "_log_beat_if_new_hour": "21f0c6598a4e0211",
    "_log_nag_event": "1f879148b03eb812",
    "_mark_hub_disruption": "00a5e6e4a6c38255",
    "_maybe_stamp_hub_restart": "ed1ee2aef9067027",
    "_memory_export": "31d70b0887198d40",
    "_memory_import": "5fd4c24e4f056622",
    "_memory_verify": "4992d73b6c9f1e42",
    "_merge_memory_index": "ab420c2a470ef62e",
    "_mute_squad": "d110a12da6057eef",
    "_norm_path": "889060dc3daa1637",
    "_parse_fleet_rows": "574bb381da9c96c2",
    "_parse_focus_remaining": "f7d7bcee9272c166",
    "_parse_hub_boot": "e67854ab8c1c245d",
    "_parse_org_repo": "18b5bba422323be1",
    "_parse_pod_agents": "a24b11c8085602ac",
    "_parse_squads_for_status": "50f7b27032576821",
    "_parse_status_from_agents": "c108a120c3eada5b",
    "_path_under": "b9b7258bf9ae01ad",
    "_placements_move": "00c0ff886b4ddc63",
    "_placements_unplace": "a9e98371f4abe1ff",
    "_query_hub": "686627cbfd3fced8",
    "_read_brief_and_inputs": "6c98ed13cabc3e8a",
    "_read_hook_stdin": "60171535d6fac12a",
    "_read_last_assistant_text": "25713dab6c894d42",
    "_read_seen_boot": "0dea3d2a31f9fa63",
    "_reads_as_waiting_on_operator": "f30b363368835279",
    "_record_mute_in_cache": "0414b35a0dd9d88b",
    "_rekey_deep": "fa5067f1ed4a8c6a",
    "_rekey_transcript": "bf7a5ba9fdaba7fe",
    "_release_singleton": "31814b15ee528015",
    "_report_leftovers": "3b27f33f8ce6fa05",
    "_resolve_agent_identity": "628a3ca993cbf8e8",
    "_resolve_for_memory": "78aa977bb301b045",
    "_resolve_squads": "af10ffd37f0ebc56",
    "_resolve_tool": "d9bf2b83ae2e8a0e",
    "_roster_all": "ba896d1a43717f15",
    "_roster_row": "9b571318a4ea0e16",
    "_sanitize_ident": "6556a80104ecd799",
    "_seat_headless": "720daf013418437f",
    "_seat_headless_pod": "1e460ee99ead24f1",
    "_seat_home_setup": "fef3ed2805598522",
    "_seat_launch": "fc5f6c834ba4f62a",
    "_seat_logs": "352d40af4270823d",
    "_seat_logs_artifact": "055de1ae459df857",
    "_seat_onboarding": "d8a8e735f3fb28ae",
    "_seat_prepare": "6c139ba81e55cb4e",
    "_seat_pulse": "4e7a83a4a74cfb3c",
    "_seat_supervise": "01848a107137e816",
    "_seat_voice": "8ca1b03cf066555d",
    "_seen_boot_path": "898964d2b2ad0a05",
    "_send_dm": "97b4aff8bf507f7e",
    "_set_focus": "7d08cf217e5bcf62",
    "_settings_model": "ce36c19fb3663cd2",
    "_source_head": "9fdb293994df73a3",
    "_spawn_daemon_detached": "581667c38bdac483",
    "_spool_append": "4236e029ed03ece8",
    "_spool_read": "c192817491cc1814",
    "_spool_take": "34437c3606e4b879",
    "_squads_fork": "4dbbdfb80b167d14",
    "_squads_merge": "90916098ca961828",
    "_state_dir": "df24d0ec5755ee6d",
    "_status_cache_path": "bc1faa9957f289bc",
    "_stop_output_shape": "94ee286e8f32b1b3",
    "_text_digest": "874c3c18d89a4207",
    "_transcript_activity": "34954a49b2cca0da",
    "_turn_evidence": "b8831188eb1d65db",
    "_voice_container_map": "0af140a2770e2282",
    "_voice_roster": "04b40df6d23a711d",
    "_waiting_analysis": "ae416df1faff1ab3",
    "_workspace_folders": "3d1db880b37efed8",
    "_workspace_listings": "b1fb13a33a9d6653",
    "_workspace_scan_dirs": "c4098575d97367ce",
    "_workspace_suffix": "4e671f1591172a57",
    "_workspaces_listing": "5363494562851ffb",
    "_workspaces_remove": "362583819701faec",
    "_write_decisions_cache": "3294df74972b9a2a",
    "_write_fleet_board": "69b4d5777a147324",
    "_write_seen_boot": "622f1bc492e5d5f1",
    "_write_status_cache": "ca8326514a37182e",
    "build_hook_response": "fe0763cf65d7e80b",
    "capsules_command": "0e8aaf0eaa248246",
    "edge_command": "fa6ca9d213c62e1b",
    "focus_command": "11bd2ceeed4ada68",
    "heartbeat_daemon_command": "003d018a942a4e13",
    "hibernate_command": "a38b2b7ef035a25e",
    "identity_command": "e04f42debdf29b3a",
    "machines_command": "9cab840deef0550c",
    "memory_export_command": "fd7116ed39a57754",
    "memory_import_command": "843575ce7d77a9c9",
    "memory_verify_command": "97c69a32aa08d3de",
    "mute_command": "f4c08086ca3ce513",
    "onboard_command": "0f21333e543be1a2",
    "parse_until": "e5ad5c4d5a1f47fd",
    "placements_command": "b91c6a112bef6b61",
    "rebind_url_command": "dcc1f7300ea52efb",
    "refuse_unhonoured_dry_run": "92700c5f0dc4d8dd",
    "seat_entry_command": "306cf108f92e4cec",
    "seats_command": "a21da122a017b49a",
    "send_command": "03d0713905a0b420",
    "session_rewake_command": "ebf4f75dd98c394f",
    "session_start_command": "39575cdd26ee6390",
    "settings_command": "52d3dc39a4c71f53",
    "squads_command": "a005a4a1466646f4",
    "stop_hook_command": "d89db745ba26624e",
    "transport_history_command": "3f38db7728a1fd26",
    "voice_client_command": "d8c973e98919052b",
    "voice_host_command": "fcc8896a6fa20d7b",
    "workspaces_command": "85f03157967545a0",
}
CLI_OPEN_BASE = {
    "build_parser": [
        "9df5ab6ada98aa5d", "d78b525614980382", "c109c381e547ea56", "58812fad9c452589", "ff19446752c33c18", "0b65f97224d72a8c",
        "e3b0c44298fc1c14", "d9dd8f728849426b", "27f5b001c3d78ee1", "6b65209badcaa7fe", "91eb890b55fad0e2", "87be3c3ccf1856f6",
        "1041a09773c6789a", "098e9b15689c6342", "9cdccdefdef4fc02", "7e0fac0ef6c88806", "81569c211997d0ff", "cbcc598a73091647",
        "ec2df0c78951df12", "ff19446752c33c18", "0d059cc16a1ca805", "47ee1345665b29af", "e11e4bc421c08765", "d046c02695d8c604",
        "b9c45b61a5910441", "574d85a5212673f9", "b6ac848c8d4bafa9", "ec2df0c78951df12", "ff19446752c33c18", "0d059cc16a1ca805",
        "199ab660846884f2", "e11e4bc421c08765", "c1ec6377de32c020", "ff19446752c33c18", "0d059cc16a1ca805", "0af44e2123a92982",
        "a3219dbf9b7d0d32", "f603bcf3b0a9a6e5", "ff19446752c33c18", "e3b0c44298fc1c14", "b091d1cc24b508df", "90b6a72a86d75e03",
        "5a910b674fdfb6fc", "ae77f4df5c191234", "91eb890b55fad0e2", "909c45de24594410", "367386c275ae1540", "352feeaa4e3900d3",
        "6e419649585a7a90", "223baa6218312858", "ec2df0c78951df12", "ff19446752c33c18", "a9ade6c5bb99653f", "47ee1345665b29af",
        "e11e4bc421c08765", "b26032a479d094cd", "ff19446752c33c18", "a9ade6c5bb99653f", "199ab660846884f2", "e11e4bc421c08765",
        "2043c368c3b11554", "ff19446752c33c18", "e3b0c44298fc1c14", "140dc4b6b4299650", "387463b66fe81e9f", "b6b8cacd2099d687",
        "91eb890b55fad0e2", "13707d3135b30536", "3ffd511362a52eeb", "f5e1757df2d57a64", "fc6945915dd28790", "e65de3645f8715e4",
        "52345adad64c491a", "96b8062d3c067973", "ec2df0c78951df12", "ff19446752c33c18", "114fda18d3133e7a", "47ee1345665b29af",
        "e11e4bc421c08765", "b26032a479d094cd", "ff19446752c33c18", "114fda18d3133e7a", "199ab660846884f2", "e11e4bc421c08765",
        "2043c368c3b11554", "ff19446752c33c18", "e3b0c44298fc1c14", "78c3e153d833ce0d", "63e3ab515e9739c9", "d14dfc6af3e833e3",
        "91eb890b55fad0e2", "c900b9f7e91d7068", "a3ad1209f0ecf00c", "ac346a140efe8e79", "5ccbe5c3a8f7e2b7", "ed2960113f2fdf7e",
        "ec2df0c78951df12", "ff19446752c33c18", "f98034cf65690b97", "a08e4b69eb503b9a", "e11e4bc421c08765", "0da6f815bf534329",
        "ff19446752c33c18", "e3b0c44298fc1c14", "1c35f709e610ba7d", "3e18bc4a3617c076", "3eb5527c1349bdc6", "91eb890b55fad0e2",
        "02d54e3266128cee", "0ea557c56f245e9c", "d9e1400923f5fdb2", "6a7e0c7914623ee9", "ec2df0c78951df12", "ff19446752c33c18",
        "679b016e05d681bd", "aa84a3174a10d584", "0af44e2123a92982", "a3219dbf9b7d0d32", "f603bcf3b0a9a6e5", "ff19446752c33c18",
        "e3b0c44298fc1c14", "3414012d82b11557", "d45fef2634d15fd1", "589d619a5bde6e2e", "91eb890b55fad0e2", "7c341fffedfb37e9",
        "29785ab0e87235f8", "e00154dd8b6eb425", "fcae20653f299219", "aacbc00f16d0a3c1", "ec2df0c78951df12", "ff19446752c33c18",
        "7b3f3d7dcda2b438", "7241652c5c3ed834", "591643a79ab959ae", "f97dce66c7d3821d", "ff19446752c33c18", "7241652c5c3ed834",
        "5b8b2609c104fd32", "6eb94447ce233448", "ff19446752c33c18", "7241652c5c3ed834", "adc909f7fc62bfcb", "d046c02695d8c604",
        "f3bb51658878a785", "508d04e1dec7ef93", "ec2df0c78951df12", "ff19446752c33c18", "7241652c5c3ed834", "0af44e2123a92982",
        "a3219dbf9b7d0d32", "f603bcf3b0a9a6e5", "ff19446752c33c18", "e3b0c44298fc1c14", "a52802b2d383bf89", "d9e659a863ed8d58",
        "851777c04732b7b9", "91eb890b55fad0e2", "e3e3e4281e90ef97", "4c746397e3abcf34", "5c4305d2e97fc5a0", "24537c14ff315f8f",
        "ec2df0c78951df12", "ff19446752c33c18", "c2d5109f8c136de3", "7f676dee520846cb", "0af44e2123a92982", "a3219dbf9b7d0d32",
        "f603bcf3b0a9a6e5", "ff19446752c33c18", "e3b0c44298fc1c14", "f54a1faa7eabaabc", "3f7e3e58516454b1", "1d637ae7070498cc",
        "91eb890b55fad0e2", "865855f67f071dd2", "08f0032505c54797", "5a957369a1da43ca", "df1a9e658555db25", "024d929de4091724",
        "cdb52836f1641f93", "247482748a4c137c", "ec2df0c78951df12", "ff19446752c33c18", "c97b5f49be71002d", "47ee1345665b29af",
        "e11e4bc421c08765", "d046c02695d8c604", "b9c45b61a5910441", "6f3a8384887a54a2", "0be2e337934ebd77", "ec2df0c78951df12",
        "ff19446752c33c18", "c97b5f49be71002d", "199ab660846884f2", "e11e4bc421c08765", "e12afe616ccc692e", "ff19446752c33c18",
        "c97b5f49be71002d", "0af44e2123a92982", "a3219dbf9b7d0d32", "f603bcf3b0a9a6e5", "ff19446752c33c18", "e3b0c44298fc1c14",
        "ecda84c6102fab77", "5769f44ffb016958", "cc93db7cc915e5ce", "91eb890b55fad0e2", "1373d38cf3b1d56d", "6cc717c93b82b102",
        "d0af76c869a0686c", "dec1760bad3152f2", "ec2df0c78951df12", "ff19446752c33c18", "ff7fc6d92725a0f8", "ca74d645c7556302",
        "0189c83301e6f091", "721df9c4e3dca7d8", "e2812a4dc306e44c", "980a4b286ad16377", "ff19446752c33c18", "e3b0c44298fc1c14",
        "6093d3ad960fc18d", "c4bb15a863e6a426", "1625b1e5b8b54ec1", "91eb890b55fad0e2", "17d3c1cf900059f7", "bc8cebbeed693d77",
        "2faa1d6b82ca45c6", "e58f12258e112b33", "872a7cdea4738bd6", "2a356f82fbd56788", "ec2df0c78951df12", "ff19446752c33c18",
        "1d0130fbcf2e3033", "329231517b4b76e4", "6deb1736ce086330", "ea18e200d5dd2326", "e100651cdd7f928d", "ff19446752c33c18",
        "6deb1736ce086330", "c8f8bd3f6451c43f", "d046c02695d8c604", "d8a7af62c4534a51", "93908ed083d957df", "d6e2873ef85853f6",
        "ec2df0c78951df12", "ff19446752c33c18", "e3b0c44298fc1c14", "fe573e8f84cf22f4", "548899a75705c5a4", "f55df2c1f2c3aa31",
        "91eb890b55fad0e2", "197c0d6572dc9a8e", "ff9c1fcfaef5bb0a", "ca40b605186e334e", "f0516e935a9f714f", "1ef89463573fceb2",
        "ec2df0c78951df12", "ff19446752c33c18", "0c72ce7e05859875", "c8f8bd3f6451c43f", "80dee78f85ae61e1", "ff19446752c33c18",
        "e3b0c44298fc1c14", "9a423f5be4d50047", "c4c519f13a77eafc", "51e566a92493320e", "91eb890b55fad0e2", "a0f5fd3d670d1d50",
        "4ef19cf6c63cc8f9", "dbe182d830e49d02", "84e26fcff67804ab", "e502969a27cd169b", "42c8f8dc73636bb1", "ec2df0c78951df12",
        "ff19446752c33c18", "677a0129e54fe0d0", "9e01a6e007843c49", "0be5c6a0f94b85e3", "11ab4f0c2d243798", "e3b0c44298fc1c14",
        "f488db2b4add62b2", "9733b0cb51e110e2", "9a348a7dc2e01536", "91eb890b55fad0e2", "1d76bfe0808f665e", "d2728366b08723f8",
        "1c57cf93660e4c5f", "0fd66114daddc27a", "a53656c79409b0c9", "6b513550c7e78992", "e8464e15d2a854d4", "ec2df0c78951df12",
        "ff19446752c33c18", "2f51ac29e3a89171", "e0f81da0857e6812", "e66e66a4e5c2d7d0", "c3e717e32afc2dc4", "ff19446752c33c18",
        "e66e66a4e5c2d7d0", "5d4c4b5bdeefb905", "e11e4bc421c08765", "d046c02695d8c604", "3f2ea6f7b5c81bd1", "c3f533203a9d00d3",
        "92fb3c5e105c0324", "ec2df0c78951df12", "ff19446752c33c18", "e3b0c44298fc1c14", "9cad0431d588a64b", "6a4c041125a8d1ce",
        "3d05673f3cb8ad25", "91eb890b55fad0e2", "af31b3d133e07727", "8cb3be597c7941da", "8c85aef6f6202f76", "b44330987246e2c9",
        "cac05cbac430505c", "55851d69d0d68e1c", "67c705d48d9a4192", "ec2df0c78951df12", "ff19446752c33c18", "69d67ded4159ec00",
        "11fe4954a7cf2c2d", "1e37f50681377595", "ff19446752c33c18", "e3b0c44298fc1c14", "da418156f6b6486e", "f4871d69cf9b62ab",
        "aa66900e58a9fb91", "91eb890b55fad0e2", "11d40f95b0c6c9ba", "a1052983930367ee", "0797c9678f510370", "820ec253d38ff5dc",
        "8fd452d04df94099", "1d5da70d355f3443", "ec2df0c78951df12", "ff19446752c33c18", "8c22b6727ea74b89", "8a2d183e7e0d50c2",
        "8067132dced3713b", "91eb890b55fad0e2", "f1773fd6f6bd7fe4", "1a3ac3d918b56a35", "f39fecdc0178a299", "62422a3d0419a846",
        "ec2df0c78951df12", "ff19446752c33c18", "a2dc25870d8c7eec", "a6f8f9679dc088f9", "79da88baa3df1aa3", "e78c7aa774cc6e2f",
        "ff19446752c33c18", "a6f8f9679dc088f9", "cfd4263daf289a0a", "ec9fe396dfdd3164", "ff19446752c33c18", "0b6bbe66ce3cb5e6",
        "a6f8f9679dc088f9", "8d7962afeb1ac439", "ff19446752c33c18", "a6f8f9679dc088f9", "fc50e59aee7fb495", "2be4b9c6b4949480",
        "ff19446752c33c18", "a6f8f9679dc088f9", "e7eaef550b42c808", "ba440698d1e2bc50", "ff19446752c33c18", "e3b0c44298fc1c14",
        "b89e49c737c84298", "56c67d44f35cbaa0", "20dd57624607bc2c", "ff19446752c33c18", "74bad5e7a31a4922", "f0352c2b7f176c67",
        "b89e49c737c84298", "5e294baa148c2707", "fbaf099c47480b8c", "ff19446752c33c18", "882435f18b5e024d", "b89e49c737c84298",
        "e7eaef550b42c808", "ba440698d1e2bc50", "ff19446752c33c18", "e3b0c44298fc1c14", "2168f5a0b49f0522", "e5a6d25c08cbdd8b",
        "fda98e47a3e3bcd9", "91eb890b55fad0e2", "c62e3bf5b6e1fa02", "eb4536e38a6d0754", "f553a43ced6657f4", "d3fcc938e06864a0",
        "42c8965b2505f4ec", "ec2df0c78951df12", "ff19446752c33c18", "390108d1137bad04", "4573a498e9dbf6d0", "8390687ba8215e59",
        "9833d778357e7f2e", "eff260d44c71611d", "ff19446752c33c18", "390108d1137bad04", "4e6eb80c8dbbc8e4", "12b1ef9d9e283965",
        "ff19446752c33c18", "b09d065199c15b4a", "390108d1137bad04", "f28fb4b733c55a57", "3ff4d04ae82d3985", "ff19446752c33c18",
        "390108d1137bad04", "591643a79ab959ae", "d63d77844d6ad495", "ff19446752c33c18", "390108d1137bad04", "2b1b6b23741c21be",
        "61f05553e409fa3b", "ff19446752c33c18", "390108d1137bad04", "e7eaef550b42c808", "ba440698d1e2bc50", "ff19446752c33c18",
        "2714e563bf07fa2f", "e3b0c44298fc1c14", "d095ae7099c21101", "e47697208551e8f1", "e824762b28574782", "91eb890b55fad0e2",
        "53a41f105c7f21cd", "052e2ba9ec11e8fe", "10f1400880b6d08f", "cd523a78ce75e752", "9fffd7870e66621c", "daa98880b52bf510",
        "076385cfddb03f47", "353b1036beac5d64", "99eab1dbeb8ddae4", "ec2df0c78951df12", "ff19446752c33c18", "64003fbf9de0873a",
        "974965b5f150a50a", "0663f230deaf3ccb", "a95cab2eacc7b29d", "ff19446752c33c18", "64003fbf9de0873a", "433a1fabcb24d0c6",
        "f4fdb04ced1b45bd", "ff19446752c33c18", "64003fbf9de0873a", "2594f611dbf75126", "cee8c64414f1a9f3", "ff19446752c33c18",
        "64003fbf9de0873a", "2d87cc9ee67c6d15", "efd7c7111389cd66", "ff19446752c33c18", "64003fbf9de0873a", "b55df042ac5f1dfc",
        "12b1ef9d9e283965", "ff19446752c33c18", "64003fbf9de0873a", "e7eaef550b42c808", "ba440698d1e2bc50", "ff19446752c33c18",
        "64003fbf9de0873a", "4c53905759cce9de", "60cc731be3a23173", "ff19446752c33c18", "64003fbf9de0873a", "5b8b2609c104fd32",
        "6c5dd803ba6a9c5c", "ff19446752c33c18", "64003fbf9de0873a", "f8473b94d589fdb5", "b0ddecced38552bc", "376796ea1fba23c1",
        "ff19446752c33c18", "2c62030f3c46d2d2", "e3b0c44298fc1c14", "425ff737720cc8d6", "feb4586a5b3ed0df", "c6ada0d2b16d12c0",
        "91eb890b55fad0e2", "153704183efab6a4", "b5c5e91d2e401cb4", "98e554fab6bc92c0", "15d9a04bd1a1fb78", "d8c000b258945884",
        "ec2df0c78951df12", "ff19446752c33c18", "b9ca556dc4608b5c", "f75a6a511f886206", "a1fbe7762424e906", "6a07ca1b742d2608",
        "31682060803e5474", "20a094db27ab4063", "ae54b068e3ed6a93", "2968c6206b7f8cac", "ed33fc3b7dea2040", "ff19446752c33c18",
        "587245f728f67333", "bce2771499e42c58", "b9ca556dc4608b5c", "8acd0e7eba6b0c3f", "0d47113a7c88f371", "65c6d0c98e631dbe",
        "ff19446752c33c18", "467baa7da4abb500", "03e2ec3b63022f9c", "7e7b6d600d8a25b9", "94e579a8f6855175", "1cbf1a6e8adefd69",
        "7a461067381e7496", "49bfb812f994776f", "b9ca556dc4608b5c", "29963a1b24e96e87", "7a8d8a731f3c1602", "252ed517cd43633c",
        "ff19446752c33c18", "9e35f7b836dc27e2", "01769a4cc8444eb4", "563950bfd15335b8", "1a150993548e5c11", "36e179007d6d99b4",
        "a170d155b0f26733", "d5f52a65235652a3", "b9ca556dc4608b5c", "444dc5b3df042100", "9467af8307f24380", "5a15e57a4da93116",
        "11e6473838eee3e2", "ff19446752c33c18", "b9ca556dc4608b5c", "cc141a40f57179c9", "18c70a48e1490d09", "967c68893ac575bb",
        "40a8833d0ba92b1d", "e1b5ebdd9bd016fc", "ff19446752c33c18", "b9ca556dc4608b5c", "87d3843064a137a4", "f28cc71b3b81a385",
        "b4acad5d47a72e8d", "ff19446752c33c18", "b9ca556dc4608b5c", "fe45fc7f162b9273", "12bfd99efbe246dc", "ca52e3f548a5f3c2",
        "16229b0327184edf", "ff19446752c33c18", "fff610df6b458f7b", "b9ca556dc4608b5c", "6f471ce357c2cea8", "34118eec32dbb885",
        "569e03f8b0e0c3e5", "36b4684246905a9c", "84f360baeff4a26f", "ff19446752c33c18", "b9ca556dc4608b5c", "6e5ef4d63df41eff",
        "5720f038490c4533", "8700c93756405ae3", "ff19446752c33c18", "b9ca556dc4608b5c", "ff3555127dccbdc5", "cde008040474f022",
        "6e7a20894670dffe", "7260dded237f17b3", "57945fe6939813f1", "ff19446752c33c18", "b418620edcf7f45c", "a49d5fe65f2839e0",
        "495f275c873fbc9a", "b9ca556dc4608b5c", "e7eaef550b42c808", "ba440698d1e2bc50", "ff19446752c33c18", "17379000801a803e",
        "b9ca556dc4608b5c", "f4a092453ef092af", "8df49d1d78f71fb6", "7fe9ec54d02537c4", "e586399908ab82a2", "ff19446752c33c18",
        "b9ca556dc4608b5c", "4c11c67d49a8f0a5", "e07dafc46b5ccdd2", "882dd98fd2e2236e", "48ca8eaf7e55967d", "53f29d2ce6df350d",
        "ff19446752c33c18", "b9ca556dc4608b5c", "e064a0a3e142b64b", "8547ad6d1c6a6dba", "ff19446752c33c18", "f630d605528dc029",
        "e7e720f8415eae27", "aa7c6d256ec9eaac", "907320b09b7d3a31", "209232601e483eae", "47c860083335e780", "b9ca556dc4608b5c",
        "a4560aca136f2723", "868201a5d855d94a", "9958457fb9936435", "3d70f3b55421411f", "62d691bef13de89c", "ff19446752c33c18",
        "b9ca556dc4608b5c", "f8473b94d589fdb5", "8609e6b4b9211393", "ff19446752c33c18", "e3b0c44298fc1c14", "5c82f6929bb1041b",
        "d764721d36445efd", "b86a70f0659d3329", "91eb890b55fad0e2", "77c9c62df4e022d0", "7a2291b949f0c54d", "efe547cc557a7bad",
        "742ddec56feccad7", "9237634d179cdb72", "1e0f6c3a161fb36b", "57ef2465ac9ac7d7", "ec2df0c78951df12", "ff19446752c33c18",
        "5003a6a1127cede1", "9ef97199d9b1fce8", "bb1f356d16990233", "fe5a01e30e1b59ee", "95d1ad3d8640013a", "ff19446752c33c18",
        "0284b4575c6f29e2", "0a7ed4328d4bb527", "0f6bd031d6e35588", "1e6e7453c82370c4", "39a9ecdf0aee657b", "98a198c996478857",
        "3c71ed846b17b6cc", "5003a6a1127cede1", "59880a4ffaa7728a", "f4c9fd9ea38dc43d", "548cea602d9479e3", "bb07d05df0ef11f6",
        "ea6d7796d8518fcb", "7ae9d52dd822651d", "ff19446752c33c18", "5dc53a4083eab1e8", "810e5bce319d5850", "e56058b85b5a97f0",
        "7962fea32c56a401", "a26b8c76d242bb8c", "5003a6a1127cede1", "f8473b94d589fdb5", "dff8d87717ec69aa", "02bea200f65fa503",
        "ff19446752c33c18", "e9e7cf43c8a2522a", "480624a0a3ae091a", "5003a6a1127cede1", "8fc1dabd7e24cac4", "9179dd768242de0a",
        "ff19446752c33c18", "5003a6a1127cede1", "e4024aa624823425", "2d54443a803efbe2", "9a41bc22804671ef", "ff19446752c33c18",
        "5003a6a1127cede1", "e6196b0c41ba3080", "684070b4784cb04d", "4fb8be0d79d90a15", "56aac0bb55985332", "ff19446752c33c18",
        "5003a6a1127cede1", "e7eaef550b42c808", "ba440698d1e2bc50", "ff19446752c33c18", "4c9801cd69f73cba", "ba83d5ec5f5e5c24",
        "e3b0c44298fc1c14", "5fbab6e0ca21c7ec", "8375d8277ccaee76", "cf196949d6021000", "91eb890b55fad0e2", "aa35a20c339fc303",
        "3b01c7c68b027a8c", "1fb9870ea11727d0", "4ae01ac2ffb05fa5", "9f0d2d0fef9bcdf6", "8739e264d36f2736", "4fc7b08ccba6a66c",
        "e470f299dc6c7ab9", "87b29754947d1eae", "ec2df0c78951df12", "ff19446752c33c18", "3ff8e9e6c56820ec", "92a214eecdc8e39b",
        "590041f806dcf0f4", "d80dbd0262b32c3e", "c605c5f66ab26d24", "3ff8e9e6c56820ec", "0af44e2123a92982", "a3219dbf9b7d0d32",
        "ba440698d1e2bc50", "ff19446752c33c18", "3ff8e9e6c56820ec", "3731e7f1728836e3", "e11e4bc421c08765", "900e875e81b68e10",
        "ff19446752c33c18", "3ff8e9e6c56820ec", "2c32a7c72ec8073d", "e11e4bc421c08765", "de55c211909d6fa5", "ff19446752c33c18",
        "3ff8e9e6c56820ec", "fc8cd119bfc269f4", "67fa4d9c3931e868", "e11e4bc421c08765", "60cc731be3a23173", "ff19446752c33c18",
        "3ff8e9e6c56820ec", "243d808bf900705a", "e2812a4dc306e44c", "8b2e5e3a1b8ba1be", "ff19446752c33c18", "e3b0c44298fc1c14",
        "1c963952d59968a3", "503f0dda24ce3273", "d91ddffbca6aee05", "91eb890b55fad0e2", "998165f771c55b94", "cae3420a2f24671b",
        "3c1f96427af9eef1", "589d63fb998573fd", "40ab0bd7c5b8b999", "cb1dbb87efce5cb3", "febd942529ffe303", "aa44d33f763a6e7c",
        "13ed6a3fdc0bd8e9", "5dda8e8aedd4a666", "ec2df0c78951df12", "ff19446752c33c18", "b5c7b8eda157e2af", "207df0357893298d",
        "828a6c03f898062a", "b5c7b8eda157e2af", "d41423e4e33d8490", "091ea2402b0d33ca", "3e09334f56f3ca8e", "b5c7b8eda157e2af",
        "e7eaef550b42c808", "7feeed95f6ddbb00", "b5c7b8eda157e2af", "2a55c5237a5477c5", "89c6bf8ed2ba6392", "b5c7b8eda157e2af",
        "38b9abdf16eabc80", "b1ef211bbb9dcd23", "968c4747ffac2113", "537e8d3d78d991b6", "b5c7b8eda157e2af", "5b8b2609c104fd32",
        "3d481805e0598b1a", "e3b0c44298fc1c14", "9cc4f1af973b2be6", "fff607200f4bcc10", "af061d0ef7a671e1", "91eb890b55fad0e2",
        "5c7f1e9dcd5160ab", "830c716b9960337c", "946444f34e373c10", "e0805bf23b6eb6b5", "ea7203ee5d93dbaf", "ec2df0c78951df12",
        "ff19446752c33c18", "f48dbd0bb31bd377", "d470a3e28bf8d09b", "7dbcb25b7f256996", "960b0d784174c7ff", "5f45950476ec0473",
        "ff19446752c33c18", "7daa1560d1a30eaf", "0b842fc2489f6578", "f48dbd0bb31bd377", "c8f8bd3f6451c43f", "cfce6ce189da9ad7",
        "5c95a7037ba51660", "ff19446752c33c18", "f48dbd0bb31bd377", "5b8b2609c104fd32", "91767f2f6614a2ac", "82962e5d1251b21f",
        "20334d3796a6e8fe", "ff19446752c33c18", "f32c5dc24af22e55", "f48dbd0bb31bd377", "b55df042ac5f1dfc", "4ce899bf67ff2172",
        "ff19446752c33c18", "f48dbd0bb31bd377", "e7eaef550b42c808", "ba440698d1e2bc50", "ff19446752c33c18", "f48dbd0bb31bd377",
        "9c790d5adc05d5d5", "a6746a47141c01fc", "fd77612a794f360b", "ff19446752c33c18", "f4c00b3d00ef3b7a", "f22be69b4fdf1f85",
        "f48dbd0bb31bd377", "edc1f3820c0c9f98", "9bdef349e0b9f9d3", "576c215288929587", "c826745aace35c9a", "ff19446752c33c18",
        "e3b0c44298fc1c14", "c40964a254cac61e", "2455803601914fa5", "2efb960887d0cdba", "91eb890b55fad0e2", "0919dfa1d9b86f84",
        "5012c013bf4a04d3", "d6230da4b1822e9f", "864224d40f705442", "d82b0e20563fb410", "d583ac9522350335", "6cce6855ef7d3fb0",
        "c22e58eb47d5a117", "09d090d9ba0c8dd4", "ffe98dcf2d843f88", "ec2df0c78951df12", "bfecbb484d774bff", "ff19446752c33c18",
        "52e1ab430d545072", "f75a6a511f886206", "99d45f0072419289", "ff71c4fc11c80d48", "ec8fd3777cc0098c", "aada8b5d0023a7aa",
        "dbca363d018e27c6", "ff19446752c33c18", "2b315cd1fbe8da41", "4a6ff9539b12e3ff", "1dfe97c0e510a796", "0996d984e420138d",
        "9b8f6ffe49189593", "34f55eebf9c563e6", "560649d5b386ad56", "c33899f5172d9888", "9f56eb4a978e82eb", "fc6276f0bdf97e1e",
        "f51b256e461db60e", "c1efef5c4ccf14e8", "52e1ab430d545072", "d295a53e959602f4", "a318ac56e924febe", "3c85c4024ae3c941",
        "ff19446752c33c18", "865418e32c6bf0f8", "dab46dba3138fb2c", "103fe0a84d2f25ca", "cab1e6227e867f34", "4c8f5e41f2f0ecb0",
        "52e1ab430d545072", "d03920ad3186cbb3", "dc3ba7a9a25c0a23", "fe03ebfea3a05451", "15c6a43a61feacf8", "ff19446752c33c18",
        "52e1ab430d545072", "a4560aca136f2723", "1c8e2987bee1747f", "ff19446752c33c18", "52e1ab430d545072", "0a8a2c16372cfcb2",
        "af3f4720293dc5ea", "ff19446752c33c18", "33a0a935fe2f704b", "564a19ea5f421359", "52e1ab430d545072", "e7eaef550b42c808",
        "ba440698d1e2bc50", "ff19446752c33c18", "5e336bbb363ef2a6", "04285d98e2fe5889", "e3b0c44298fc1c14", "0bfa33430caf23f6",
        "0bbae38cad522c04", "6d9092d3cfc19168", "91eb890b55fad0e2", "8e927f8b36b0b987", "831709d715459a34", "060c38c51571c90f",
        "3ffff3f2af192333", "ec2df0c78951df12", "ff19446752c33c18", "b47fd76ecadfd773", "55e4d19b5c4b5a5a", "8c274417f5ec05ed",
        "a3bfdc2e0d94f3e0", "e3b0c44298fc1c14", "c7fd01902cb6cac1", "7832bebcd57803d9", "cb4b228d6d5d6add", "91eb890b55fad0e2",
        "5be0f9f7dfe552c2", "c3bcd4bb8cd9bd72", "0a4cea896d4c4e7a", "4a4a9af9fa9e357a", "ec2df0c78951df12", "ff19446752c33c18",
        "73706b254ec347bb", "ab5f8da92fc46455", "63a255ec1caec8a2", "16666c6adbcbe3a3", "3cd5fe78a57dd8bc", "936dd9a529825f20",
        "4fc3a38a8799d092", "513d9ef1c89119e4", "02748ca8ac117779", "a910a19fa4563798", "1b3a062efbd54b92", "981776de53d13342",
        "5845fa003bbcbb8e", "431abf12f3863b22", "513d9ef1c89119e4", "302bf0742f493b4f", "cdc5b546473fa814", "3283069e1864d926",
        "5845fa003bbcbb8e", "e3b0c44298fc1c14", "f86cb52f65a91bc5", "a57a09171bd8f1e1", "ffa90f861802e5d7", "91eb890b55fad0e2",
        "1db7047192b16499", "fa0c038ab29b37e5", "4193d5d85ccce4b7", "c50ad00a3831ede9", "adf977f7a29658ac", "ac7b04af2a6861e0",
        "ec2df0c78951df12", "ff19446752c33c18", "43d588be4db3b059", "865ec650ef386687", "e11e4bc421c08765", "e12b2c7811e4d377",
        "ff19446752c33c18", "43d588be4db3b059", "fcc854db4cef0975", "e2812a4dc306e44c", "ded0bf0593e7dc63", "ff19446752c33c18",
        "e3b0c44298fc1c14", "616da6130533a2d4"],
    "main": [
        "006faa322092ebb4", "1bdfb0699b3fc72c", "aa06bff0497a6777", "7196104080f6fd96", "bb26bc20b96c4602", "fe2546e264f9a1c2",
        "b82fe01df6c7459f", "9ca67e07d4f4d555", "da488a04cf2e34b4", "cc7f4e3163eaa3ce", "7067aaecbab33b25", "6223f31d04866d4f",
        "e3b0c44298fc1c14", "6510db195a7897b8", "b30e64f83934cc72", "e3b0c44298fc1c14", "4079a20e1989e606", "cd18822e2965e4d5",
        "61b76490c55500c5", "a1e8f5893ae0c0fe", "bd308755e4bbf704", "f9647b79fcf622ac", "2cd839c54746e595", "39d0aa8d35c984fc",
        "21645ee19bac9b38", "4f20b63dc9166dd1", "b7e5b0f6ff6f8b4f", "d586a38069d979d4", "9325e3bc92220be4", "0a33a0cc55053c09",
        "9feae9567c7b6dfd", "dc9c2b351c6a110c", "07743feabbc0bdbe", "047741bd226508be", "613f16237f317090", "a14fb16b31c65633",
        "839fcf2235e184fc", "d0da46fe06189f3d", "5e8e922c0f4c01de", "d7528f2749d4ee56", "432e528ce8caedb6", "a14fb16b31c65633",
        "27e29d9e00b3a618", "ff1d8760104a5779", "af3397907cf40cf5", "513f7881e43aba9c", "6b6e8f87910d4206", "1aa20282471c651e",
        "6663bc6ddf1d9cae", "7f16774d6d52a376", "f5e427dfdfa4196b", "eb83f3bc52be8970", "30150ef2082eff40", "44a3d2ad20e6b55a",
        "f205cfafe1d90fbe", "cb367bf91fc33c57", "dcc598fa6c1bccc4", "79d962fd1ee475db", "e4a3942f52ed9657", "166025dd415dc901",
        "554fa4252c105d7b", "d82bf48022e21e26", "2663ec6415a808ae", "aca1636ff84e668d", "4cad8554b41cfa41", "f36f35b62af26dfd",
        "5b41c265fb1af25c", "4aeda9b76d22cf63", "6be4e834e249f68d", "c4da38f20aac33d3", "0b4814af7f9626a8", "5b78c0cb9f5f0ac1",
        "3ec308ac4130f3be", "9ab5fe3225c8ccd8", "e3b0c44298fc1c14", "33d1a28c01c618db", "e894e71c35f32db0"],
}


def _sha(s):
    return hashlib.sha256(s.encode()).hexdigest()[:16]


def _cli_units(text):
    tree = ast.parse(text)
    defs, rest, opened = {}, [], {}
    for node in tree.body:
        seg = ast.get_source_segment(text, node)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.decorator_list:
                start = node.decorator_list[0].lineno
                seg = "\n".join(text.splitlines()[start - 1:node.end_lineno])
            if node.name in CLI_OPEN:
                opened[node.name] = seg
            else:
                defs[node.name] = _sha(seg)
        else:
            rest.append(seg)
    return defs, _sha("\n".join(rest)), opened


def test_guard_cli_changes_only_parser_and_dispatch():
    defs, rest, opened = _cli_units(CLI.read_text())
    assert set(defs) == set(CLI_DEFS), sorted(set(defs) ^ set(CLI_DEFS))
    changed = sorted(n for n in defs if defs[n] != CLI_DEFS[n])
    assert not changed, f"cli.py functions changed outside parser/dispatch: {changed}"
    assert rest == CLI_REST_SHA, "cli.py top-level statements (imports, constants) changed"
    for name in CLI_OPEN:
        base = [ln for ln in CLI_OPEN_BASE[name]]
        now = [_sha(ln) for ln in opened[name].splitlines()]
        sm = difflib.SequenceMatcher(a=base, b=now, autojunk=False)
        removed = [op for op in sm.get_opcodes() if op[0] in ("replace", "delete")]
        assert not removed, f"{name}(): base lines changed or removed {removed[:3]}"


# squad/squad: every top-level function and every arm of the main `case` other than `who)`
# is byte-identical to the base (c587989, which carries rows_prime/ls from b52bb18), and so is
# every other top-level line. New top-level functions are allowed.
SQUAD_REST_SHA = "5cc50f60798bfc7d"
SQUAD_UNITS = {
    "arm:*": "45918539a5fdc304",
    "arm:add": "2de658a72b108515",
    "arm:add-container": "f04ab89c42ec6b8b",
    "arm:add-folder": "0ad94c45fa3cbe19",
    "arm:answer": "63b564da05ecd757",
    "arm:args": "48777963dd3566f2",
    "arm:attach": "424dd413fa0d8b41",
    "arm:attached": "f02b6bced92d0562",
    "arm:board": "eae564ff7a01f0e1",
    "arm:bootstrap": "df7a06b8d33ac042",
    "arm:cmd": "92784a8580c79300",
    "arm:comms": "5cbb4a656f1709b4",
    "arm:compaction": "826f08584fdf30db",
    "arm:dash": "b07223aa58d6b8c2",
    "arm:down": "ea9ca11d90acc79d",
    "arm:duplicate": "45c9e1a20ece44c5",
    "arm:ext-align": "ec3a0ec5cbda4f9e",
    "arm:handoff": "2d364d491a3194a0",
    "arm:heal": "c692dbe2602102bf",
    "arm:key": "169178b4168678b4",
    "arm:launch": "6c257abb1b80be7e",
    "arm:launch-cmd": "7ea52235c2692c0b",
    "arm:ls": "964207cd4740dc28",
    "arm:model": "7bd858860595d4ac",
    "arm:preflight": "4c2cabe2a9f28e61",
    "arm:pull-local": "5c4bdd55f27672a8",
    "arm:restart": "7aeb3ef838cbb7d4",
    "arm:resume": "3f9fa5363c7d917e",
    "arm:rm": "54acd22954d45fdb",
    "arm:showcase": "6cb6159a91fe80c9",
    "arm:start": "8824e68e2eaaa7e7",
    "arm:stop": "108a260868f9196a",
    "arm:supervise": "913ae11da5d87ddc",
    "arm:teardown": "bc474731ce9f2a98",
    "arm:transport": "3094c0ed7bc5cf1f",
    "arm:up": "48f4cb55a2d5b4bc",
    "arm:who": "cfd427f925a5fde5",
    "arm:ws-add": "4f15910bfd288e44",
    "arm:ws-new": "3c3c310dfee197e9",
    "arm:ws-remove": "1c6e4912db996c82",
    "arm:ws-squad": "445847dff129d67c",
    "fn:add_agent": "87828ecdff254a58",
    "fn:add_folder": "e332c1b7f7376b68",
    "fn:agent_started": "2fed72a37045b54a",
    "fn:agents": "170c0fb700cf8c87",
    "fn:all_box_sessions": "16d29f36dd41a102",
    "fn:args_pair_value": "e1a6b7c709b27c9b",
    "fn:arm_comms": "a4249b23044b790b",
    "fn:boundary_reached": "e6fb881c4f01e3e2",
    "fn:classify_agent": "7103ce31d601be15",
    "fn:classify_text": "0b6db1aa54e63747",
    "fn:comms_agents": "d06b28c18fb6dd61",
    "fn:compaction_answer": "d0aaa1b7a0521452",
    "fn:compaction_answer_leg": "4d88115124f653a8",
    "fn:compaction_boundary_since": "a0256d516396b91e",
    "fn:compaction_ctx": "3e9b2ae4ddf6814f",
    "fn:compaction_dir": "7f36ce1dbec1b789",
    "fn:compaction_exec_at": "c873365b33e9a56e",
    "fn:compaction_flag": "0df387ffb5bcec3b",
    "fn:compaction_in_scope": "cef1c7729795e586",
    "fn:compaction_one": "69236f8d83c05ad2",
    "fn:compaction_pass": "035b6893b92e01eb",
    "fn:compaction_recross": "b7d76ef38d260ff6",
    "fn:compaction_row": "624dcc2741bc038c",
    "fn:compaction_safe_to_type": "8e6d170e037add65",
    "fn:compaction_scope_load": "7a2c656345e8a9da",
    "fn:compaction_scope_readable": "0af475e6d9538ac7",
    "fn:compaction_session": "b6b37de66bfc0140",
    "fn:compaction_tokens": "8b638dc41039bfe9",
    "fn:compaction_type": "4afd4075544cb441",
    "fn:container_alive": "b1b5e488da3a5335",
    "fn:container_of": "8fadb49ac10dbe83",
    "fn:dest_candidate": "f856c98b2597d594",
    "fn:dest_minn": "b3d4c9684ab51416",
    "fn:dest_state_dir": "c370ae7891fdb433",
    "fn:dest_suffix": "1620fc124b4ca9bb",
    "fn:duplicate_agent": "afb735f1bf55efc0",
    "fn:enc_project_dir": "3b28067654d3fb38",
    "fn:ext_align": "197559c50564efab",
    "fn:field": "3bb4ce3893781349",
    "fn:fleet_wake_counts": "2c033103b6daba1f",
    "fn:focus_glyph": "f84feb67071fbb9c",
    "fn:handoff": "848688b913ecbaf3",
    "fn:has_comms": "ba6bbff6efe99b01",
    "fn:has_comms_args": "efbb1c03e50841d1",
    "fn:has_resume": "610e00d612db802c",
    "fn:heal_action": "3221262f91530523",
    "fn:heal_in_scope": "0f0843c0c4eeb20c",
    "fn:held_at": "2e0caa4c09c68e51",
    "fn:held_mirror_unknown": "2982935fba8cdd1d",
    "fn:held_reason": "d348a38b91487799",
    "fn:held_until": "00f78c897e74a536",
    "fn:hold_enforce_one": "7ae0d0b9f4869429",
    "fn:hold_enforce_pass": "a76df52227ceb5d4",
    "fn:hold_hand_started_flag": "a28d87a8df24f361",
    "fn:hold_release_pass": "dc0765bdc63fb3e3",
    "fn:hold_stop_lane": "ff3abe61afaa9da7",
    "fn:hold_stopped_flag": "3f87883aec9dc974",
    "fn:hub_bin": "79bcd77052e08d80",
    "fn:hub_glyph": "87821df93332562e",
    "fn:hub_optedin": "34aee5869e2e95f9",
    "fn:hub_project": "6500a02b79e06054",
    "fn:is_faculty": "ed49fa15944de78b",
    "fn:is_supervised": "795f70163d163865",
    "fn:kill_daemon": "c240f53f175b45a9",
    "fn:launch_agent_cmd": "9fc918a85527dbf2",
    "fn:launch_line": "48a483a8e1d9701c",
    "fn:marker_project": "b55ae7646a28f8f6",
    "fn:migrate_roster_name": "72e1a161a3eaf57a",
    "fn:need_agent": "0f80a612ab746868",
    "fn:nudge_copy": "edbb837db51a2d64",
    "fn:optin_project": "a97964332b51b4f9",
    "fn:org_alias": "25308257b6d00128",
    "fn:pace_verdict": "a2850a6f731cdb30",
    "fn:pane_alive": "56f0414ff921c79a",
    "fn:pane_cmd": "1dcf41ad30cacd7e",
    "fn:pane_flat": "d9e096bf07de2b73",
    "fn:path_exists": "4299cc01d4d96988",
    "fn:pidfile": "1c447e5d803af235",
    "fn:placement_id": "65c738705d264a3d",
    "fn:preflight_host": "bd4a3d0a110d2739",
    "fn:preflight_report": "558f4adf71451b71",
    "fn:project_still_present": "6fd174bf36e93b16",
    "fn:pull_local": "9ad54ce5cf940fff",
    "fn:register_ws_suffix": "0730806815b0a0bc",
    "fn:relaunch_agent": "57c7213a52867579",
    "fn:resolve_agent": "350b7867ca4445cd",
    "fn:resolve_dest": "f98ea74268a70d3b",
    "fn:resolve_rsh": "5b9de0a966d1a45b",
    "fn:rm_agent": "702576256bd402c3",
    "fn:roster_set_args": "a2a73d9ab00ad7d3",
    "fn:rows": "aa50c5e4f75f8458",
    "fn:rows_prime": "59e00db9f91c108d",
    "fn:rows_unprime": "71066bcc81173e2e",
    "fn:safe_name": "22f05e16d19b9d71",
    "fn:sanitize_ident": "d4d7b864714b660f",
    "fn:seed_claude_trust": "a2091f2165828116",
    "fn:seed_hub_tool_permissions": "dbf435e025025777",
    "fn:self_stale": "e174575350f07554",
    "fn:session_of": "fce58df4d06da2a2",
    "fn:set_placement_intent": "1704b2083cf0d635",
    "fn:short_label": "cc756e76e3ad53ff",
    "fn:spawn_daemon": "8cb4a44dc5552424",
    "fn:squad_agents": "42ac73b327af1e3f",
    "fn:status_file": "4926cfa73f168829",
    "fn:status_json": "b85d5b732fc08c7c",
    "fn:strip_args_pair": "765bf2fbb79afdd3",
    "fn:strip_args_tokens": "4638f5cdc2b1b00a",
    "fn:strip_continue": "72330548fb0717d4",
    "fn:suffix_taken": "d6d907c4de4c4b43",
    "fn:supervise_flag": "5b17a573966b9d5b",
    "fn:supervise_pass": "2699ff9498843826",
    "fn:teardown_workspace": "24d59fb33db4d6b6",
    "fn:tm": "4bac8e97e8a76438",
    "fn:tmt": "fe227d62df8a027e",
    "fn:transport_agent": "b2461986621f9a31",
    "fn:transport_gate": "d0eab835ada9ca80",
    "fn:type_line": "8f91e7a358307dd0",
    "fn:unmanaged_filter": "439058f9b3a73703",
    "fn:unmanaged_sessions": "0449dc8e0ac5a7b7",
    "fn:unregister_ws_suffix": "b685c4d307068bc7",
    "fn:unseed_claude_trust": "b20fe0663f38c996",
    "fn:untilde": "dba31c4480c084ea",
    "fn:up_one": "a084ebd0af3a6802",
    "fn:usage_refresh": "6396abd74c8a28c6",
    "fn:wait_shell_ready": "618e0f681c9e040f",
    "fn:wake_verified_since": "4aa309c6452bfb11",
    "fn:want_down_flag": "195b2d1fc89fc513",
    "fn:wants_down": "696d5618e4dd18c6",
    "fn:worktree_project": "7d55da169a00a65d",
    "fn:write_hub_marker": "c74abf80b2278b06",
    "fn:ws_add_folder": "282d8938bc888783",
    "fn:ws_agents": "352007eca683d20d",
    "fn:ws_comms_off": "cb2366ebb2e9b7a2",
    "fn:ws_create": "01fa40773edc290e",
    "fn:ws_del_folder": "cf053f8878c136e7",
    "fn:ws_folders": "b7c7c375dcb71bd4",
    "fn:ws_label": "becf3715a6dcabea",
    "fn:ws_lacks_tab_settings": "8ec41225730a7964",
    "fn:ws_of_worktree": "4cd4c85b068628a5",
    "fn:ws_paths_with_suffix": "968e6299541ef9bf",
    "fn:ws_squad": "9a1cadbb0cf4ab06",
    "fn:ws_suffix_of": "c6372e0767834558",
}
_FN = re.compile(r'^([A-Za-z_][A-Za-z0-9_]*)\s*\(\)\s*\{(.*)$')
_ARM = re.compile(r'^  ([^ #\t)][^)\s]*)\)')


def _squad_units(text):
    lines = text.split("\n")
    units, rest, i, in_main, cur = {}, [], 0, False, None
    while i < len(lines):
        ln = lines[i]
        m = _FN.match(ln)
        if m and not in_main:
            if re.search(r'\}\s*(#.*)?$', m.group(2)):
                units["fn:" + m.group(1)] = ln
                i += 1
                continue
            j = i + 1
            while j < len(lines) and not re.match(r'^\}\s*(#.*)?$', lines[j]):
                j += 1
            units["fn:" + m.group(1)] = "\n".join(lines[i:j + 1])
            i = j + 1
            continue
        if ln.startswith('case "${1:-help}" in'):
            in_main, cur = True, None
            rest.append(ln)
        elif in_main and ln.startswith("esac"):
            in_main = False
            rest.append(ln)
        elif in_main:
            a = _ARM.match(ln)
            if a:
                cur = "arm:" + a.group(1)
                units[cur] = ln
            elif cur is None:
                rest.append(ln)
            else:
                units[cur] += "\n" + ln
        else:
            rest.append(ln)
        i += 1
    return {k: _sha(v) for k, v in units.items()}, _sha("\n".join(rest))


def test_guard_squad_changes_only_the_who_arm():
    units, rest = _squad_units(SQUAD.read_text())
    missing = sorted(set(SQUAD_UNITS) - set(units))
    assert not missing, f"squad units gone: {missing}"
    changed = sorted(k for k, h in SQUAD_UNITS.items() if k != "arm:who" and units[k] != h)
    assert not changed, f"squad changed outside the who arm: {changed}"
    new_arms = sorted(k for k in units if k.startswith("arm:") and k not in SQUAD_UNITS)
    assert not new_arms, f"new squad verbs: {new_arms}"
    assert rest == SQUAD_REST_SHA, "squad top-level lines outside functions and arms changed"
    for k in ("fn:rows_prime", "fn:rows", "fn:field", "arm:ls"):
        assert units[k] == SQUAD_UNITS[k], k


# ======================================================================================
# F1 falsifiers (r1's, moved: the meter runs as `python -m mcp_hub.cli load-meter`)
# ======================================================================================

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


# ======================================================================================
# F2: the quiet who-poll (squad who --watch into a pipe, as under systemd)
# ======================================================================================

BASE_S, MAX_S = 0.4, 1.6   # SQUAD_WHO_INTERVAL / SQUAD_WHO_MAX_INTERVAL for the run
QUIET_S = 12.0             # nothing changes for this long after the watcher starts
SETTLE_S = 4.0             # watched after the change is journaled
LANES = ("alpha", "beta", "gamma")


def _fleet(tmp):
    home = tmp / "home"
    (home / ".config" / "squad").mkdir(parents=True)
    (home / ".mcp-hub").mkdir()
    # who reads the recap cache with a bare redirect; on a fresh HOME a missing file is a
    # stderr line every pass, which no live box has.
    (home / ".mcp-hub" / "board-recap.cache").write_text("")
    work = tmp / "w"
    work.mkdir()
    conf = home / ".config" / "squad" / "squad.conf"
    conf.write_text("".join(f"{a}|{work}||--continue\n" for a in LANES), encoding="utf-8")
    bindir = tmp / "bin"
    bindir.mkdir()
    calls = tmp / "calls"
    real_tmux = shutil.which("tmux")
    # The wrapper logs every call with its time, and refuses `-S <socket>`: the box-wide
    # inventory walks /tmp/tmux-$UID, which holds the LIVE fleet's sockets.
    (bindir / "tmux").write_text(
        "#!/bin/sh\n"
        'case "$1" in -S) exit 1 ;; esac\n'
        f'printf "%s %s\\n" "$(date +%s.%N)" "$*" >> {calls}\n'
        f'exec {real_tmux} "$@"\n')
    (bindir / "docker").write_text("#!/bin/sh\nexit 0\n")
    # The watcher waits between passes with `sleep <seconds>`; the wrapper logs each wait,
    # so the backoff is read from the waits themselves, not from timings a busy box skews.
    (bindir / "sleep").write_text(
        "#!/bin/sh\n"
        f'printf "%s %s\\n" "$(date +%s.%N)" "$*" >> {tmp / "sleeps"}\n'
        f'exec {shutil.which("sleep")} "$@"\n')
    for f in ("tmux", "docker", "sleep"):
        (bindir / f).chmod(0o755)
    # A short private TMUX_TMPDIR: a socket path past ~100 bytes cannot be bound.
    tmpdir = Path(tempfile.mkdtemp(prefix="v2f-tmux-"))
    sock = f"whopoll-{uuid.uuid4().hex[:8]}"
    env = {k: v for k, v in os.environ.items() if not k.startswith("TMUX")}
    env.update(HOME=str(home), SQUAD_CONF=str(conf), SQUAD_SOCKET=sock, TMUX_TMPDIR=str(tmpdir),
               PATH=f"{bindir}:{os.environ['PATH']}", MCP_HUB_STATE_DIR=str(home / ".mcp-hub"))
    t = [real_tmux, "-L", sock]
    tenv = {**env, "PATH": os.environ["PATH"]}
    # alpha and beta run a program with a line on screen (alive, idle); gamma has no session
    # (down). A blank screen is not a lane's: who's harvest exits on it under pipefail.
    for a in ("alpha", "beta"):
        subprocess.run(t + ["new-session", "-d", "-s", a, "echo ready; exec sleep 600"], check=True, env=tenv)
    return env, calls, t, tenv, tmpdir


def _sleeps(log):
    """(time, seconds) of every one-argument `sleep` the watcher ran: its waits between passes."""
    out = []
    for ln in (log.read_text().splitlines() if log.exists() else []):
        parts = ln.split()
        if len(parts) == 2:
            try:
                out.append((float(parts[0]), float(parts[1])))
            except ValueError:
                pass
    return out


@pytest.fixture(scope="module")
def watch(tmp_path_factory):
    # Not a skip: a verdict image without tmux would pass F2 with nothing run.
    assert shutil.which("tmux"), "F2's falsifiers need tmux on PATH (apt-get install tmux)"
    tmp = tmp_path_factory.mktemp("whopoll")
    env, calls, t, tenv, tmpdir = _fleet(tmp)
    env.update(SQUAD_WHO_INTERVAL=str(BASE_S), SQUAD_WHO_MAX_INTERVAL=str(MAX_S))
    out, err = tmp / "stdout", tmp / "stderr"
    rec = {"t0": time.time()}
    with open(out, "wb") as fo, open(err, "wb") as fe:
        proc = subprocess.Popen(["bash", str(SQUAD), "who", "--watch"], stdout=fo, stderr=fe,
                                stdin=subprocess.DEVNULL, env=env, start_new_session=True)
    try:
        deadline = time.time() + 20.0
        while not out.read_bytes() and time.time() < deadline:
            time.sleep(0.05)
        time.sleep(0.3)
        rec["early"] = out.read_bytes()
        time.sleep(max(QUIET_S - (time.time() - rec["t0"]), 6.0))
        rec["quiet"] = out.read_bytes()
        rec["quiet_err"] = err.read_bytes()
        rec["t_change"] = time.time()
        subprocess.run(t + ["kill-session", "-t", "=beta"], check=True, env=tenv)
        seen = None
        deadline = time.time() + MAX_S + 15.0
        while time.time() < deadline:
            new = out.read_bytes()[len(rec["quiet"]):].decode(errors="replace")
            if "beta" in new:
                seen = time.time()
                break
            time.sleep(0.05)
        rec["t_seen"] = seen
        time.sleep(SETTLE_S)
        rec["final"] = out.read_bytes()
        rec["alive"] = proc.poll() is None
    finally:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        proc.wait(timeout=10)
        subprocess.run(t + ["kill-server"], capture_output=True, env=tenv)
        shutil.rmtree(tmpdir, ignore_errors=True)
    rec["sleeps"] = _sleeps(tmp / "sleeps")
    rec["sleeps_all"] = [v for _, v in rec["sleeps"]]
    return rec


def _lines(b):
    return [x for x in b.decode(errors="replace").splitlines() if x.strip()]


def test_f2_guard_one_shot_who_still_reports_every_lane(tmp_path):
    assert shutil.which("tmux"), "F2's falsifiers need tmux on PATH (apt-get install tmux)"
    env, calls, t, tenv, tmpdir = _fleet(tmp_path)
    try:
        p = subprocess.run(["bash", str(SQUAD), "who"], capture_output=True, text=True, env=env,
                           stdin=subprocess.DEVNULL, timeout=60)
    finally:
        subprocess.run(t + ["kill-server"], capture_output=True, env=tenv)
        shutil.rmtree(tmpdir, ignore_errors=True)
    assert p.returncode == 0, p.stderr
    rows = {ln.split()[1]: ln for ln in p.stdout.splitlines() if len(ln.split()) > 1 and ln.split()[1] in LANES}
    assert set(rows) == set(LANES), p.stdout
    assert "idle" in rows["alpha"] and "idle" in rows["beta"] and "down" in rows["gamma"]


def test_f2_watcher_runs_and_its_first_pass_names_every_lane(watch):
    assert watch["alive"], "the watcher died"
    first = _lines(watch["early"])
    assert first, "nothing journaled on the first pass"
    for a in LANES:
        assert any(re.search(rf"\b{a}\b", ln) for ln in first), a
    assert len(first) <= len(LANES) + 2, f"first pass wrote {len(first)} lines for {len(LANES)} lanes"
    assert b"\x1b[2J" not in watch["final"], "screen-clearing codes written to the journal"


def test_f2_journal_is_silent_while_nothing_changes(watch):
    assert watch["quiet"] == watch["early"], (
        f"{len(_lines(watch['quiet'])) - len(_lines(watch['early']))} lines journaled while nothing changed")
    assert watch["quiet_err"] == b"", watch["quiet_err"][:400]


def test_f2_a_state_change_is_journaled_once_naming_lane_and_state(watch):
    assert watch["t_seen"] is not None, "beta's change was never journaled"
    new = _lines(watch["final"][len(watch["quiet"]):])
    assert new and len(new) <= 2, new
    assert any("beta" in ln and "down" in ln for ln in new), new
    assert not any(re.search(r"\b(alpha|gamma)\b", ln) for ln in new), new


def _quiet_sleeps(watch):
    return [v for t, v in watch["sleeps"] if t < watch["t_change"]]


def test_f2_poll_backs_off_while_quiet_and_stays_under_the_cap(watch):
    qs = _quiet_sleeps(watch)
    assert len(qs) >= 3, f"only {len(qs)} waits in {QUIET_S}s of quiet: {qs}"
    assert abs(qs[0] - BASE_S) < 1e-6, f"the first wait is not SQUAD_WHO_INTERVAL: {qs}"
    assert all(b >= a for a, b in zip(qs, qs[1:])), f"waits shrank with nothing changing: {qs}"
    assert max(qs) >= MAX_S - 1e-6, f"the poll never backed off to the cap: {qs}"
    assert all(v <= MAX_S + 1e-6 for v in watch["sleeps_all"]), f"a wait past the cap: {watch['sleeps_all']}"


def test_f2_poll_drops_back_to_the_base_interval_on_a_change(watch):
    qs = _quiet_sleeps(watch)
    assert qs and max(qs) >= MAX_S - 1e-6, f"never backed off, so nothing to drop back from: {qs}"
    after = [v for t, v in watch["sleeps"] if t > watch["t_change"]]
    assert len(after) >= 2, after
    # the pass under way at the change may still sleep the backed-off wait; the pass that
    # saw beta go down waits SQUAD_WHO_INTERVAL again
    assert any(abs(v - BASE_S) < 1e-6 for v in after[:2]), f"no reset to the base interval: {after}"


def test_f2_default_cap_keeps_changes_within_a_minute():
    """T15: every lane's state within one minute. The cap's default must leave room for a pass."""
    src = SQUAD.read_text()
    m = re.findall(r"\$\{SQUAD_WHO_MAX_INTERVAL:-([0-9.]+)\}", src)
    assert m, "no SQUAD_WHO_MAX_INTERVAL default in squad/squad"
    assert all(0 < float(x) <= 50 for x in m), m
