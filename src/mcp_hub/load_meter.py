"""The load meter — what the fleet costs Tim, read daily (F1 + F4, v2-fleet).

v2's changes exist to take load off the one human in the loop. Whether they
did is a number, and this module is the only place that number is made:

- `typed_turns`   — lines Tim typed into a lane's pane, and DMs the voice
                    relay carried from him;
- `reaching_tim`  — replies that came back to him, by pane and by relay;
- `wait_*`        — how long he sat between a lane's last reply and his
                    next line;

beside `operator_wakes` / `hub_wakes` / `no_origin` (the wake traffic those
numbers have to be read against) and, with the injections ledger,
`injected_turns` — lines the fleet typed itself (see `injections.py`).

A reading holds a baseline, one measure set per closed UTC day after it, and
a before/after pair per delivered change. Every number names its source.

The rules that keep it honest, each one a way an earlier cut flattered the
changes it measured:

- READ-ONLY, AND OUTSIDE THE HUB. It reads a COPY of the hub database,
  opened `mode=ro&immutable=1`: `mode=ro` alone still grows -wal/-shm beside
  a WAL-format copy. It never selects a message body (what leaves prod-1 is
  a body-less projection), it is never imported by the server — a side job
  must not be able to take the hub down with it — and it is reached only as
  the `load-meter` subcommand of the hub's CLI.
- UNREAD IS NOT ZERO. Each source has a coverage interval: the wake snapshot
  answers for `since .. taken_at`, the hub copy for everything up to the
  moment it was taken (`--hub-db-taken-at`, default its mtime), and that
  copy's wake_log only for the WAKE_LOG_DAYS before that, because the hub
  prunes it. A window not wholly inside its source's coverage reads null
  with a `missing` reason naming the gap — never 0, because 0 is the number
  a change gets congratulated for. A window inside coverage with nothing in
  it is a real 0.
- A STOPPED SOURCE IS NOT A SAVING. A before/after pair straddling a
  declared source stop is flagged `source_stopped` ("not comparable").
- A COUNT IS THIS HOST'S COUNT. Transcript counts name the host and the
  directory they read; a dev-vm-1 count is dev-vm-1's lanes, not the fleet.
- NO TEXT. A reading carries counts and sources, never a message or a turn.
- `typed_turns` from the pane is an UPPER BOUND, and says so: a `squad
  send-keys` line lands in exactly the shape Tim's own does. The ledger
  narrows it; without the ledger `injected_turns` is null, never 0.

Transcript files are split on "\\n" alone, never `str.splitlines()`: the
client leaves U+2028/U+2029/U+0085 raw inside JSON strings, and splitlines()
would cut such a turn in two and lose it (and its sha, so no ledger match).
"""

from __future__ import annotations

import argparse
import bisect
import json
import os
import re
import socket
import sqlite3
import statistics
import sys
import urllib.parse
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import injections

VOICE_RELAY_AGENT = "hub-voice-bridge"

# A baseline shorter than a week reads one weekday pattern as "normal".
MIN_BASELINE_DAYS = 7

# The hub prunes wake_log; a copy answers for this many days before it was taken.
WAKE_LOG_DAYS = 14

# The daily record keeps the newest days only, and says when it dropped some.
MAX_DAILY_DAYS = 60

# A ledger row is written before its keystroke; the turn it causes lands
# within a few seconds of it, never before it.
INJECTION_WINDOW_SECONDS = 5.0

DAY = timedelta(days=1)
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

Window = tuple  # (start datetime, end datetime), half-open: [start, end)


# --- time -------------------------------------------------------------------

def parse_instant(text: str) -> datetime:
    """An ISO-8601 instant as an aware UTC datetime; naive reads as UTC."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError(f"not an instant: {text!r}")
    s = text.strip()
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_day(text: str) -> datetime:
    """A YYYY-MM-DD day as its UTC midnight."""
    if not isinstance(text, str) or not _DAY_RE.match(text.strip()):
        raise ValueError(f"not a YYYY-MM-DD day: {text!r}")
    return datetime.strptime(text.strip(), "%Y-%m-%d").replace(tzinfo=timezone.utc)


def stamp(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def closed_days(start: datetime, now: datetime) -> list[datetime]:
    """Every UTC day from `start` that has wholly ended by `now`. The open
    day is absent even a second before it closes: half a day beside whole
    ones reads as a quiet day."""
    days, day = [], start
    while day + DAY <= now:
        days.append(day)
        day += DAY
    return days


def _epochs(window: Window) -> tuple[float, float]:
    return window[0].timestamp(), window[1].timestamp()


class Coverage:
    """The interval a source can answer for. A window not wholly inside it
    is UNREAD; `gap()` says which boundary it fell outside."""

    def __init__(self, label: str, start: datetime | None = None,
                 end: datetime | None = None, start_text: str | None = None,
                 end_text: str | None = None):
        self.label = label
        self.start, self.end = start, end
        self.start_text = start_text or (stamp(start) if start else "")
        self.end_text = end_text or (stamp(end) if end else "")

    def covers(self, window: Window) -> bool:
        a, b = window
        return ((self.start is None or a >= self.start)
                and (self.end is None or b <= self.end))

    def gap(self, window: Window) -> str:
        a, b = window
        parts = []
        if self.start is not None and a < self.start:
            parts.append(f"starts before {self.label} begins at {self.start_text}")
        if self.end is not None and b > self.end:
            parts.append(f"ends after {self.label} ends at {self.end_text}")
        return (f"window {stamp(a)}..{stamp(b)} " + " and ".join(parts)
                + ": not read, not 0")


# --- the hub copy -----------------------------------------------------------

def open_hub_ro(path: Path | str) -> sqlite3.Connection:
    """The hub copy, read-only AND immutable: `mode=ro` alone still writes
    -wal/-shm beside a WAL-format copy; `immutable=1` tells SQLite nothing
    else touches the file, so it takes no lock and makes no sidecar. A
    missing file is an error, never a new empty database."""
    quoted = urllib.parse.quote(os.path.abspath(str(path)))
    return sqlite3.connect(f"file:{quoted}?mode=ro&immutable=1", uri=True)


class HubMessages:
    """What the meter needs from the hub copy, read once: relay DM instants
    and (if the table exists) wake_log rows. Never a body — the projection
    shipped off prod-1 has none, and the reading must carry none."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        if not self.path.is_file():
            raise FileNotFoundError(f"{self.path}: no such file")
        conn = open_hub_ro(self.path)
        try:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")}
            if "messages" not in tables:
                raise sqlite3.DatabaseError("no messages table")
            cols = {r[1] for r in conn.execute("PRAGMA table_info(messages)")}
            need = {"ts", "from_agent", "to_agent"}
            if not need <= cols:
                raise sqlite3.DatabaseError(
                    f"messages lacks {', '.join(sorted(need - cols))}")
            dm = "to_agent IS NOT NULL AND to_agent != ''"
            if "channel" in cols:
                dm += " AND (channel IS NULL OR channel = '')"
            from_voice, to_voice = [], []
            for ts, sender, to in conn.execute(
                    f"SELECT ts, from_agent, to_agent FROM messages "
                    f"WHERE (from_agent = ? OR to_agent = ?) AND {dm}",
                    (VOICE_RELAY_AGENT, VOICE_RELAY_AGENT)):
                if ts is None:
                    continue
                if sender == VOICE_RELAY_AGENT and to != VOICE_RELAY_AGENT:
                    from_voice.append(float(ts))
                elif to == VOICE_RELAY_AGENT and sender != VOICE_RELAY_AGENT:
                    to_voice.append(float(ts))
            self.from_voice, self.to_voice = sorted(from_voice), sorted(to_voice)
            self.wake_rows: list[tuple[float, str]] | None = None
            if "wake_log" in tables:
                self.wake_rows = sorted(
                    (float(ts), str(reason)) for ts, reason in conn.execute(
                        "SELECT ts, reason FROM wake_log WHERE ts IS NOT NULL"))
        finally:
            conn.close()

    @staticmethod
    def _count(stamps: list[float], window: Window) -> int:
        a, b = _epochs(window)
        return bisect.bisect_left(stamps, b) - bisect.bisect_left(stamps, a)

    def relay(self, window: Window) -> tuple[int, int]:
        """(DMs from the relay to a lane, DMs from a lane to the relay)."""
        return self._count(self.from_voice, window), self._count(self.to_voice, window)

    def wakes(self, window: Window, reason: str) -> int:
        if self.wake_rows is None:
            raise LookupError("the hub copy has no wake_log table")
        a, b = _epochs(window)
        return sum(1 for ts, r in self.wake_rows if a <= ts < b and r == reason)


# --- the wake snapshot ------------------------------------------------------

class Snapshot:
    """The operator plane's hourly wake summary: rows of
    (hour_utc, recipient, reason, held_max, count) from `since` to `taken_at`."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        doc = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(doc, dict):
            raise ValueError("not a JSON object")
        self.source = str(doc.get("source") or "unnamed")
        self.since_text, self.taken_text = doc.get("since"), doc.get("taken_at")
        since, taken = parse_instant(self.since_text), parse_instant(self.taken_text)
        cols = list(doc.get("columns") or [])
        try:
            ih, ir, ic = cols.index("hour_utc"), cols.index("reason"), cols.index("count")
        except ValueError as e:
            raise ValueError(f"columns lack hour_utc/reason/count: {cols}") from e
        self.rows: list[tuple[float, str, float]] = []
        for row in doc.get("rows") or []:
            self.rows.append((parse_instant(row[ih]).timestamp(), str(row[ir]),
                              float(row[ic])))
        self.coverage = Coverage(
            f"the wake snapshot ({self.source}, taken {self.taken_text})",
            start=since, end=taken, start_text=self.since_text, end_text=self.taken_text)

    def wakes(self, window: Window, reason: str) -> float:
        a, b = _epochs(window)
        total = sum(c for h, r, c in self.rows if a <= h < b and r == reason)
        return int(total) if float(total).is_integer() else total


# --- the transcripts --------------------------------------------------------

def _text_of(message: Any) -> str:
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(p.get("text", "")) for p in content
                         if isinstance(p, dict) and p.get("type") == "text")
    return ""


def _is_tool_result(rec: dict[str, Any]) -> bool:
    if "toolUseResult" in rec:
        return True
    content = (rec.get("message") or {}).get("content") if isinstance(
        rec.get("message"), dict) else None
    return isinstance(content, list) and any(
        isinstance(p, dict) and p.get("type") == "tool_result" for p in content)


def _instant(value: Any) -> float | None:
    try:
        return parse_instant(value).timestamp()
    except (TypeError, ValueError):
        return None


class Transcripts:
    """Every lane's sessions under a transcripts root (`<lane>/<session>.jsonl`),
    as events `[ts, kind, sha256]` in file order.

    kind: "typed" (origin.kind human), "injected" (a typed turn the ledger
    accounts for), "hub_wake" (origin.kind channel), "no_origin", "other",
    or "assistant". Sidechains, meta lines, compaction summaries and tool
    results are not turns at all.
    """

    def __init__(self, root: Path | str):
        self.root = Path(root)
        if not self.root.is_dir():
            raise FileNotFoundError(f"{self.root}: not a directory")
        self.lanes: list[str] = []
        self.sessions: list[tuple[str, list[list[Any]]]] = []
        self.textless: Counter = Counter()
        for lane_dir in sorted(p for p in self.root.iterdir() if p.is_dir()):
            files = sorted(lane_dir.glob("*.jsonl"))
            if not files:
                continue
            self.lanes.append(lane_dir.name)
            for f in files:
                try:
                    self.sessions.append((lane_dir.name, self._events(f)))
                except OSError:
                    continue
        self._facts: dict[str, Any] | None = None

    @staticmethod
    def _events(path: Path | str) -> list[list[Any]]:
        raw = Path(path).read_bytes().decode("utf-8", errors="replace")
        events: list[list[Any]] = []
        for line in raw.split("\n"):  # never splitlines(): see the module doc
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue  # a torn or foreign line
            if not isinstance(rec, dict):
                continue
            kind = rec.get("type")
            if kind not in ("user", "assistant"):
                continue
            if rec.get("isSidechain") or rec.get("isMeta") or rec.get("isCompactSummary"):
                continue
            ts = _instant(rec.get("timestamp"))
            if ts is None:
                continue
            if kind == "assistant":
                events.append([ts, "assistant", None])
                continue
            if _is_tool_result(rec):
                continue
            origin = rec.get("origin")
            okind = origin.get("kind") if isinstance(origin, dict) else None
            if not okind:
                events.append([ts, "no_origin", None])
            elif okind == "human":
                events.append([ts, "typed", injections.sha256(_text_of(rec.get("message")))])
            elif okind == "channel":
                events.append([ts, "hub_wake", None])
            else:
                events.append([ts, "other", None])
        return events

    def apply_injections(self, rows: list[dict[str, Any]],
                         mapping: dict[str, str] | None = None) -> list[dict[str, Any]]:
        """Move each typed turn a ledger row accounts for to "injected".

        A row matches the earliest still-typed turn in ITS lane (its own name,
        or the transcript folder the roster maps it to) with the same sha256,
        0..INJECTION_WINDOW_SECONDS after it. One row accounts for one turn.
        Rows from keystroke doors (key, answer, edge_interrupt) type no line
        and are counted apart in `textless`. Returns the rows that matched
        nothing, in ledger order.
        """
        mapping = mapping or {}
        index: dict[tuple[str, str], list[list[Any]]] = {}
        for lane, events in self.sessions:
            for ev in events:
                if ev[1] == "typed":
                    index.setdefault((lane, ev[2]), []).append(ev)
        for evs in index.values():
            evs.sort(key=lambda e: e[0])
        self.textless = Counter()
        unmatched: set[int] = set()

        def when(item: tuple[int, dict[str, Any]]) -> float:
            ts = item[1].get("ts")
            return float(ts) if isinstance(ts, (int, float)) and not isinstance(ts, bool) else 0.0

        for i, row in sorted(enumerate(rows), key=when):
            door = row.get("door")
            if door in injections.TEXTLESS_DOORS:
                self.textless[door] += 1
                continue
            ts, lane, sha = row.get("ts"), row.get("lane"), row.get("sha256")
            if (isinstance(ts, bool) or not isinstance(ts, (int, float))
                    or not isinstance(lane, str) or not isinstance(sha, str)):
                unmatched.add(i)
                continue
            names = [lane]
            if mapping.get(lane) and mapping[lane] != lane:
                names.append(mapping[lane])
            best = None
            for name in names:
                for ev in index.get((name, sha), ()):
                    if ev[1] == "typed" and 0 <= ev[0] - ts <= INJECTION_WINDOW_SECONDS:
                        if best is None or ev[0] < best[0]:
                            best = ev
                        break
            if best is None:
                unmatched.add(i)
            else:
                best[1] = "injected"
        self._facts = None
        return [r for i, r in enumerate(rows) if i in unmatched]

    def _build(self) -> dict[str, Any]:
        """Each session walked once into time-sorted facts; a window is then
        two bisects, so sixty daily windows cost no more than one walk."""
        facts: dict[str, list[tuple[float, str, float]]] = {
            k: [] for k in ("typed", "injected", "reaching", "hub_wakes", "no_origin", "waits")}
        for lane, events in self.sessions:
            last_reply = None
            run_typed = None   # the typed turn whose reply has not yet been seen
            for ts, kind, _sha in events:
                if kind == "assistant":
                    last_reply = ts
                    if run_typed is not None:
                        facts["reaching"].append((run_typed, lane, 1.0))
                        run_typed = None
                elif kind == "typed":
                    facts["typed"].append((ts, lane, 1.0))
                    if last_reply is not None:
                        facts["waits"].append((ts, lane, ts - last_reply))
                    last_reply, run_typed = None, ts
                elif kind == "injected":
                    facts["injected"].append((ts, lane, 1.0))
                    last_reply, run_typed = None, None
                elif kind == "hub_wake":
                    facts["hub_wakes"].append((ts, lane, 1.0))
                elif kind == "no_origin":
                    facts["no_origin"].append((ts, lane, 1.0))
        built = {}
        for k, rows in facts.items():
            rows.sort(key=lambda r: r[0])
            built[k] = ([r[0] for r in rows], rows)
        return built

    def read(self, window: Window) -> dict[str, Any]:
        if self._facts is None:
            self._facts = self._build()
        a, b = _epochs(window)
        out: dict[str, Any] = {}
        for k, (stamps, rows) in self._facts.items():
            part = rows[bisect.bisect_left(stamps, a):bisect.bisect_left(stamps, b)]
            if k == "waits":
                out[k] = [r[2] for r in part]
            elif k == "no_origin":
                out[k] = len(part)
            else:
                per = {lane: 0 for lane in self.lanes}
                for r in part:
                    per[r[1]] = per.get(r[1], 0) + 1
                out[k] = per
        return out


def _load_transcripts(root: Path | str) -> Transcripts:
    return Transcripts(root)


def _textless_note(ledger: dict[str, Any]) -> str:
    """Accounts for the rows the reading did not match: keystroke doors type
    no line, so they can neither match a turn nor be flagged for missing one."""
    seen = ledger.get("textless") or {}
    doors = ", ".join(injections.TEXTLESS_DOORS)
    head = (f"rows from keystroke doors ({doors}) type no line, so they match "
            f"no turn and are neither counted nor flagged; such rows read: ")
    if not seen:
        return head + "none."
    return head + ", ".join(f"{d} x{n}" for d, n in sorted(seen.items())) + "."


# --- the changes file -------------------------------------------------------

def load_changes(path: Path | str | None) -> dict[str, list[dict[str, Any]]]:
    """{"changes": [...], "source_stops": [...]} from a changes file, either
    shape: r1's bare list of changes, or the document with source stops.
    Raises ValueError naming the changes file for anything unreadable — a
    half-read changes file would silently drop a change's before/after."""
    if path is None:
        return {"changes": [], "source_stops": []}
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ValueError(f"changes file {path} is unreadable: {e}") from e
    if isinstance(doc, list):
        doc = {"changes": doc, "source_stops": []}
    if not isinstance(doc, dict):
        raise ValueError(f"changes file {path}: neither a list nor an object")
    changes, stops = doc.get("changes") or [], doc.get("source_stops") or []
    if not isinstance(changes, list) or not isinstance(stops, list):
        raise ValueError(f"changes file {path}: changes and source_stops must be lists")
    out_c, out_s = [], []
    for i, c in enumerate(changes):
        if not isinstance(c, dict) or not isinstance(c.get("change_id"), str) \
                or not c["change_id"].strip():
            raise ValueError(f"changes file {path}: changes[{i}] has no change_id")
        try:
            at = parse_instant(c.get("at"))
        except (TypeError, ValueError) as e:
            raise ValueError(f"changes file {path}: changes[{i}] ({c['change_id']}) "
                             f"has no readable `at`") from e
        out_c.append({**c, "at": stamp(at), "_at": at, "delivered": bool(c.get("delivered"))})
    for i, s in enumerate(stops):
        if not isinstance(s, dict) or not isinstance(s.get("measure"), str) \
                or not s["measure"].strip():
            raise ValueError(f"changes file {path}: source_stops[{i}] needs a measure")
        try:
            at = parse_instant(s.get("stopped_at"))
        except (TypeError, ValueError) as e:
            raise ValueError(f"changes file {path}: source_stops[{i}] needs a "
                             f"readable stopped_at") from e
        out_s.append({"measure": s["measure"], "stopped_at": stamp(at), "_at": at})
    out_c.sort(key=lambda c: c["_at"])
    out_s.sort(key=lambda s: s["_at"])
    return {"changes": out_c, "source_stops": out_s}


# --- the command ------------------------------------------------------------

def add_arguments(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    parser.add_argument("--hub-db", required=True,
                        help="A COPY of the hub database (opened read-only and immutable)")
    parser.add_argument("--hub-db-taken-at", default=None,
                        help="When the copy was taken (ISO-8601; default: its mtime). "
                             "The copy answers for nothing after it")
    parser.add_argument("--snapshot", required=True,
                        help="The hub's wake snapshot (JSON: since, taken_at, columns, rows)")
    parser.add_argument("--transcripts", default=str(Path("~/.claude/projects").expanduser()),
                        help="Transcripts root, one folder per lane (default ~/.claude/projects)")
    parser.add_argument("--injections", default=None,
                        help="Injections ledger (default $MCP_HUB_STATE_DIR/injections.jsonl)")
    parser.add_argument("--out", required=True, help="Where to write the reading (JSON)")
    parser.add_argument("--baseline-start", required=True, help="First baseline day, YYYY-MM-DD")
    parser.add_argument("--baseline-end", required=True,
                        help="Baseline end day, YYYY-MM-DD (exclusive)")
    parser.add_argument("--changes", default=None,
                        help="Delivered changes and declared source stops (JSON)")
    parser.add_argument("--now", default=None,
                        help="The reading's present (ISO-8601; default: now)")
    return parser


def hub_db_taken_at(args: argparse.Namespace) -> datetime | None:
    """`--hub-db-taken-at`, else the copy's mtime. Never `now`: that would
    promise the reading covers hours the file cannot hold."""
    if args.hub_db_taken_at:
        return parse_instant(args.hub_db_taken_at)
    try:
        return datetime.fromtimestamp(os.stat(args.hub_db).st_mtime, timezone.utc)
    except OSError:
        return None


def _measure(value: Any, source: str, missing: str | None = None,
             per_lane: dict[str, int] | None = None) -> dict[str, Any]:
    if value is None:
        return {"value": None, "source": source, "missing": missing or "not read"}
    out: dict[str, Any] = {"value": value, "source": source}
    if per_lane is not None:
        out["per_lane"] = per_lane
    return out


class _Reading:
    """The sources, opened once, and the one function that turns a window
    into a measure set."""

    def __init__(self, args: argparse.Namespace, now: datetime,
                 taken: datetime | None):
        self.now = now
        self.host = socket.gethostname()
        self.inputs: list[dict[str, Any]] = []
        self.flags: list[dict[str, Any]] = []

        # the hub copy
        self.hub_path = str(args.hub_db)
        self.hub: HubMessages | None = None
        self.hub_why = ""
        try:
            self.hub = HubMessages(args.hub_db)
            self.inputs.append({"name": "hub_db", "path": self.hub_path, "read": True})
        except (OSError, sqlite3.Error) as e:
            self.hub_why = f"hub copy {self.hub_path} not read ({e})"
            self.inputs.append({"name": "hub_db", "path": self.hub_path, "read": False,
                                "missing": self.hub_why})
        taken = taken or now
        self.hub_cov = Coverage(f"the hub copy (taken {stamp(taken)})", end=taken)
        self.wake_cov = Coverage(
            f"the hub copy's wake_log (the {WAKE_LOG_DAYS} days before the copy "
            f"was taken, {stamp(taken)})",
            start=taken - timedelta(days=WAKE_LOG_DAYS), end=taken)

        # the wake snapshot
        self.snap_path = str(args.snapshot)
        self.snap: Snapshot | None = None
        self.snap_why = ""
        try:
            self.snap = Snapshot(args.snapshot)
            self.inputs.append({"name": "snapshot", "path": self.snap_path, "read": True})
        except (OSError, ValueError, TypeError, KeyError, IndexError) as e:
            self.snap_why = f"wake snapshot {self.snap_path} not read ({type(e).__name__}: {e})"
            self.inputs.append({"name": "snapshot", "path": self.snap_path, "read": False,
                                "missing": self.snap_why})

        # the transcripts
        self.tr_path = str(args.transcripts)
        self.tr: Transcripts | None = None
        self.tr_why = ""
        try:
            self.tr = _load_transcripts(args.transcripts)
            self.inputs.append({"name": "transcripts", "path": self.tr_path, "read": True})
        except OSError as e:
            self.tr_why = (f"transcripts {self.tr_path} on {self.host} not read ({e}): "
                           f"unread, not 0")
            self.inputs.append({"name": "transcripts", "path": self.tr_path, "read": False,
                                "missing": self.tr_why})
        self.where = f"on {self.host} at {self.tr_path}"

        # the injections ledger
        self.ledger_path = str(args.injections or injections.ledger_path())
        self.roster = str(injections.roster_path())
        self.ledger: dict[str, Any] = {"read": False, "path": self.ledger_path,
                                       "roster": self.roster, "textless": {}}
        try:
            rows = injections.read_rows(self.ledger_path)
            self.ledger["read"] = True
        except OSError as e:
            self.ledger["missing"] = (f"no injections ledger at {self.ledger_path} ({e}): "
                                      f"injected turns unknown, not 0")
            rows = []
        if self.ledger["read"] and self.tr is not None:
            mapping = injections.lane_folders(self.roster)
            left = self.tr.apply_injections(rows, mapping)
            self.ledger["textless"] = dict(self.tr.textless)
            by_lane: dict[str, list[dict[str, Any]]] = {}
            for row in left:
                by_lane.setdefault(str(row.get("lane")), []).append(row)
            for lane, lrows in sorted(by_lane.items()):
                folder = mapping.get(lane)
                shown = folder and folder != lane
                named = lane + (f" (transcript folder {folder})" if shown else "")
                doors = ", ".join(sorted({str(r.get("door")) for r in lrows}))
                self.flags.append({"code": "injection_unmatched", "detail": (
                    f"{len(lrows)} injections-ledger row(s) for {named}, door(s) {doors}, "
                    f"matched no typed turn 0-{INJECTION_WINDOW_SECONDS:g} s after them "
                    f"in the transcripts {self.where}: counted nothing")})

    # -- one window -> one measure set

    def measures(self, window: Window) -> dict[str, Any]:
        where = self.where
        pane_src = {
            "typed": (f"pane: user turns with origin.kind \"human\" in the transcripts "
                      f"{where} — an upper bound: a `squad send-keys` line lands in the "
                      f"same shape" + ("" if not self.ledger["read"] else
                                       f", less the turns the injections ledger "
                                       f"{self.ledger_path} accounts for")),
            "reaching": (f"pane: typed turns answered by at least one assistant turn "
                         f"before the next typed or injected turn, in the transcripts {where}"),
            "wait_total": (f"pane: seconds from a lane's last assistant turn to the next "
                           f"typed turn, summed, in the transcripts {where}"),
            "wait_median": (f"pane: median seconds from a lane's last assistant turn to "
                            f"the next typed turn, in the transcripts {where}"),
            "hub_wakes": (f"pane: user turns with origin.kind \"channel\" (hub pushes) in "
                          f"the transcripts {where}"),
            "no_origin": (f"pane: user turns with no origin (not counted) as typed or as a "
                          f"wake, in the transcripts {where}"),
        }
        hub_from = f"hub copy {self.hub_path}"
        relay_in_src = f"{hub_from}: DMs from {VOICE_RELAY_AGENT} to a lane (Tim, by voice)"
        relay_out_src = f"{hub_from}: DMs from a lane to {VOICE_RELAY_AGENT} (to Tim, by voice)"

        if self.tr is None:
            got = None
            why = self.tr_why
            typed_p = _measure(None, pane_src["typed"], why)
            reach_p = _measure(None, pane_src["reaching"], why)
            wait_t = _measure(None, pane_src["wait_total"], why)
            wait_m = _measure(None, pane_src["wait_median"], why)
            hub_w = _measure(None, pane_src["hub_wakes"], why)
            no_o = _measure(None, pane_src["no_origin"], why)
        else:
            got = self.tr.read(window)
            typed_p = _measure(sum(got["typed"].values()), pane_src["typed"],
                               per_lane=got["typed"])
            reach_p = _measure(sum(got["reaching"].values()), pane_src["reaching"],
                               per_lane=got["reaching"])
            waits = got["waits"]
            total = sum(waits)
            wait_t = _measure(int(total) if float(total).is_integer() else total,
                              pane_src["wait_total"])
            if waits:
                med = statistics.median(waits)
                wait_m = _measure(int(med) if float(med).is_integer() else med,
                                  pane_src["wait_median"])
            else:
                wait_m = _measure(None, pane_src["wait_median"],
                                  "no wait in this window: a median of nothing is not 0")
            hub_w = _measure(sum(got["hub_wakes"].values()), pane_src["hub_wakes"],
                             per_lane=got["hub_wakes"])
            no_o = _measure(got["no_origin"], pane_src["no_origin"])

        # the relay, inside the hub copy's coverage only
        if self.hub is None:
            relay_in = _measure(None, relay_in_src, self.hub_why)
            relay_out = _measure(None, relay_out_src, self.hub_why)
        elif not self.hub_cov.covers(window):
            why = self.hub_cov.gap(window)
            relay_in = _measure(None, relay_in_src, why)
            relay_out = _measure(None, relay_out_src, why)
        else:
            n_in, n_out = self.hub.relay(window)
            relay_in = _measure(n_in, relay_in_src)
            relay_out = _measure(n_out, relay_out_src)

        return {
            "typed_turns": {"pane": typed_p, "voice_relay": relay_in},
            "reaching_tim": {"pane": reach_p, "voice_relay": relay_out},
            "wait_total_seconds": {"pane": wait_t},
            "wait_median_seconds": {"pane": wait_m},
            "operator_wakes": self._operator_wakes(window),
            "hub_wakes": hub_w,
            "no_origin": no_o,
            "injected_turns": self._injected(got),
        }

    def _operator_wakes(self, window: Window) -> dict[str, Any]:
        """The snapshot where it covers the window — it is the operator
        plane's own summary, and two sources must not disagree about one
        window — otherwise the copy's wake_log, otherwise unread."""
        reasons = []
        if self.snap is None:
            reasons.append(self.snap_why)
        elif self.snap.coverage.covers(window):
            s = self.snap
            # The file's NAME, not its path: the path is in `inputs`, and a
            # directory name must not decide which source a reader sees here.
            return _measure(s.wakes(window, "operator"), (
                f"wake snapshot {s.path.name} (source {s.source}, "
                f"{s.since_text}..{s.taken_text}): rows with reason \"operator\""))
        else:
            reasons.append(self.snap.coverage.gap(window))
        wl_src = f"hub copy {self.hub_path}, wake_log rows with reason \"operator\""
        if self.hub is None:
            reasons.append(f"{self.hub_why}, so its wake_log was not read either")
        elif self.hub.wake_rows is None:
            reasons.append(f"hub copy {self.hub_path} has no wake_log table")
        elif self.wake_cov.covers(window):
            return _measure(self.hub.wakes(window, "operator"), wl_src)
        else:
            reasons.append(self.wake_cov.gap(window))
        return _measure(None, wl_src if self.hub is not None else
                        f"wake snapshot {self.snap_path}, else the hub copy's wake_log",
                        "; ".join(reasons))

    def _injected(self, got: dict[str, Any] | None) -> dict[str, Any]:
        src = (f"injections ledger {self.ledger_path}: a typed turn 0-"
               f"{INJECTION_WINDOW_SECONDS:g} s after a row for its lane with the same "
               f"sha256, in the transcripts {self.where}; agent names joined to "
               f"transcript folders through the squad roster {self.roster}; "
               + _textless_note(self.ledger))
        if not self.ledger["read"]:
            return _measure(None, src, self.ledger["missing"])
        if got is None:
            return _measure(None, src, self.tr_why)
        return _measure(sum(got["injected"].values()), src, per_lane=got["injected"])


def _refuse(message: str) -> int:
    print(f"load-meter: {message}", file=sys.stderr)
    return 2


def _write_atomic(out: Path, reading: dict[str, Any]) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f".{out.name}.tmp-{os.getpid()}")
    try:
        tmp.write_text(json.dumps(reading, indent=2, ensure_ascii=False) + "\n",
                       encoding="utf-8")
        os.replace(tmp, out)
    finally:
        if tmp.exists():
            tmp.unlink()


def run(args: argparse.Namespace) -> int:
    """Take one reading. Exit 2, writing nothing, on any input that would
    make the reading a guess: a malformed day or instant, a baseline under
    MIN_BASELINE_DAYS or past the first delivered change, an unreadable
    changes file. An unreadable SOURCE is not a refusal: it is a reading
    with that source's numbers null and the reason beside them."""
    try:
        start, end = parse_day(args.baseline_start), parse_day(args.baseline_end)
    except ValueError as e:
        return _refuse(f"--baseline-start/--baseline-end must be YYYY-MM-DD: {e}")
    days = (end - start).days
    if days < MIN_BASELINE_DAYS:
        return _refuse(f"the baseline {args.baseline_start}..{args.baseline_end} is "
                       f"{days} day(s); a baseline needs at least {MIN_BASELINE_DAYS}")
    try:
        now = parse_instant(args.now) if args.now else datetime.now(timezone.utc)
    except ValueError as e:
        return _refuse(f"--now is not an ISO-8601 instant: {e}")
    try:
        taken = hub_db_taken_at(args)
    except ValueError as e:
        return _refuse(f"--hub-db-taken-at is not an ISO-8601 instant: {e}")
    try:
        doc = load_changes(args.changes)
    except ValueError as e:
        return _refuse(str(e))
    delivered = [c for c in doc["changes"] if c["delivered"]]
    if delivered and end > delivered[0]["_at"]:
        first = delivered[0]
        return _refuse(f"the baseline ends {stamp(end)}, after the first delivered change "
                       f"{first['change_id']} at {first['at']}: a baseline must end at or "
                       f"before the first change")

    rd = _Reading(args, now, taken)
    length = end - start
    changes_out = []
    for c in doc["changes"]:
        at = c["_at"]
        before_w, after_w = (at - length, at), (at, at + length)
        before = rd.measures(before_w) if before_w[1] <= now else None
        after = rd.measures(after_w) if c["delivered"] and after_w[1] <= now else None
        changes_out.append({"change_id": c["change_id"], "at": c["at"],
                            "before": before, "after": after})
        if not c["delivered"]:
            continue
        if before is None:
            rd.flags.append({"code": "missing_before", "change_id": c["change_id"],
                             "detail": f"the before window {stamp(before_w[0])}.."
                                       f"{stamp(before_w[1])} has not closed by "
                                       f"{stamp(now)}"})
        if after is None:
            rd.flags.append({"code": "missing_after", "change_id": c["change_id"],
                             "detail": f"the after window {stamp(after_w[0])}.."
                                       f"{stamp(after_w[1])} has not closed by "
                                       f"{stamp(now)}: no after reading yet"})
        if before is not None and after is not None:
            for stop in doc["source_stops"]:
                if before_w[0] <= stop["_at"] <= after_w[1]:
                    rd.flags.append({"code": "source_stopped", "change_id": c["change_id"],
                                     "detail": (
                        f"not comparable: source stopped — {stop['measure']} stopped "
                        f"at {stop['stopped_at']}, inside {c['change_id']}'s "
                        f"before/after span {stamp(before_w[0])}..{stamp(after_w[1])}; "
                        f"a difference in {stop['measure']} across it is the source "
                        f"going away, not the change")})

    days_list = closed_days(end, now)
    if len(days_list) > MAX_DAILY_DAYS:
        dropped = days_list[:-MAX_DAILY_DAYS]
        days_list = days_list[-MAX_DAILY_DAYS:]
        rd.flags.append({"code": "daily_truncated", "detail": (
            f"{len(dropped)} closed day(s) from {dropped[0].date().isoformat()} to "
            f"{dropped[-1].date().isoformat()} dropped: the daily record keeps the "
            f"newest {MAX_DAILY_DAYS}")})
    daily = [{"day": d.date().isoformat(), "measures": rd.measures((d, d + DAY))}
             for d in days_list]

    reading = {
        "generated_at": stamp(now),
        "baseline": {"start": start.date().isoformat(), "end": end.date().isoformat(),
                     "days": days},
        "measures": rd.measures((start, end)),
        "changes": changes_out,
        "flags": rd.flags,
        "inputs": rd.inputs,
        "daily": daily,
    }
    out = Path(args.out)
    _write_atomic(out, reading)
    print(f"load-meter: wrote {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """The `mcp-hub load-meter` entry the CLI delegates to."""
    parser = add_arguments(argparse.ArgumentParser(
        prog="mcp-hub load-meter", description=__doc__.split("\n", 1)[0]))
    return run(parser.parse_args(argv))
