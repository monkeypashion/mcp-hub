"""The daily load meter (v2-fleet F1, j6-load-meter-daily).

A read-only job OUTSIDE the hub. It reads three sources — the hub database, a
snapshot of the hub's wake ledger, and the lanes' transcript logs — and writes
one reading: the three load measures over the baseline, one measure set per
closed UTC day since the baseline, plus a before/after reading per delivered
v2 change. Every number in the reading names the source it came from, and a
number that could not be read is `null` with a `missing` line saying why.
**Never zero**: a zero is a measurement, and reporting an unreadable source as
"no load" is how a meter starts lying in the one direction nobody checks.

It lives in mcp-hub's code because the measures come from mcp-hub's own files,
but the hub SERVER never imports it: the meter is reached only as
`mcp-hub load-meter` (`mcp_hub.cli`), so a bug here cannot take the hub down.

Read-only means read-only. The hub snapshot is opened `mode=ro&immutable=1`
(`open_hub_ro`): a copy taken with sqlite's backup API keeps the live hub's
WAL header, and `mode=ro` ALONE makes sqlite create `-wal`/`-shm` beside it on
first read — writing two files next to the evidence it was told not to touch
(features-json's r1 finding L2).

⭐ COVERAGE IS PART OF THE NUMBER (r4). None of the three sources covers all
time: a wake snapshot covers `since .. taken_at`, a copy of the hub database
covers everything up to the moment it was taken, and the hub's `wake_log` is
pruned, so a copy answers for the 14 days before that and no further. A window
that is not wholly inside a source's coverage is NOT a quiet window — it is an
unread one, and it reads `null` with a `missing` line naming the gap. The
difference matters most where it is least visible: a before/after pair whose
"after" fell past the snapshot read 0 wakes on r3 and would have been reported
as the saving the change was built to produce.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sqlite3
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

DESCRIPTION = (
    "Record one reading of Tim's load: the three measures over the baseline, "
    "one set per closed UTC day since it, and a before/after reading per "
    "delivered v2 change. Read-only — it opens the hub snapshot immutable and "
    "writes nothing but --out."
)

# The baseline has to be long enough to average over a week's rhythm (a
# weekend reads as a quiet fleet) and has to PREDATE the first v2 change, or
# it is not a baseline, it is a mid-flight reading.
MIN_BASELINE_DAYS = 7

# The lane that carries Tim's voice to and from the fleet. Messages from it are
# Tim speaking; messages to it are a lane's answer reaching him.
VOICE_RELAY_AGENT = "hub-voice-bridge"

# A user turn carries its cause in origin.kind. Only "human" is Tim at a
# keyboard; "channel" is the hub waking the lane; a turn with no origin at all
# is counted as neither and reported on its own, because guessing its cause
# would quietly inflate whichever measure the guess favours.
HUMAN_ORIGIN = "human"
CHANNEL_ORIGIN = "channel"

# How far back a copy of the hub database can answer for wakes. The hub prunes
# `wake_log`, so rows older than this are absent from a healthy copy exactly
# as they are absent from a broken one — and a count over a window the table
# no longer covers is a measurement of the retention policy.
WAKE_LOG_DAYS = 14

# The daily array is a rolling record, not an archive: 60 closed days is two
# months of rhythm, and the days that fall off say so in a flag rather than
# vanishing quietly.
MAX_DAILY_DAYS = 60


# ----------------------------------------------------------------------------
# read-only access
# ----------------------------------------------------------------------------

def open_hub_ro(path) -> sqlite3.Connection:
    """Open a hub database (or a backup-API copy of one) strictly read-only.

    `mode=ro` refuses writes; `immutable=1` additionally promises sqlite the
    file cannot change, so it neither takes locks nor tries to recover a WAL —
    which is what stops a WAL-format snapshot growing `-wal`/`-shm` sidecars
    the moment it is read.
    """
    uri = "file:" + quote(os.path.abspath(os.fspath(path))) + "?mode=ro&immutable=1"
    return sqlite3.connect(uri, uri=True)


# ----------------------------------------------------------------------------
# time
# ----------------------------------------------------------------------------

def parse_instant(text) -> datetime:
    """An ISO-8601 instant, 'Z' included, as an aware UTC datetime."""
    s = str(text).strip()
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_day(text) -> datetime:
    """A YYYY-MM-DD day boundary (00:00 UTC)."""
    return datetime.strptime(str(text).strip(), "%Y-%m-%d").replace(tzinfo=timezone.utc)


def stamp(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _in(window, when: datetime) -> bool:
    start, end = window
    return start <= when < end


def _maybe_instant(text):
    """An instant, or None when there is nothing parseable to read."""
    if text in (None, ""):
        return None
    try:
        return parse_instant(text)
    except (ValueError, TypeError):
        return None


# ----------------------------------------------------------------------------
# coverage — what interval a source can answer for
# ----------------------------------------------------------------------------

class Coverage:
    """The interval one source answers for, and the gap when it does not.

    `start`/`end` are instants or None for unbounded. `covers(window)` is the
    whole test a measure needs; `gap(window)` is the sentence that goes in
    `missing` when it fails — it names the boundary VERBATIM as the source
    spells it, so the reading can be checked against the file it came from.
    """

    def __init__(self, label: str, start=None, end=None,
                 start_text: str = "", end_text: str = ""):
        self.label = label
        self.start = start
        self.end = end
        self.start_text = start_text or (stamp(start) if start else "")
        self.end_text = end_text or (stamp(end) if end else "")

    def covers(self, window) -> bool:
        w0, w1 = window
        if self.start is not None and w0 < self.start:
            return False
        if self.end is not None and w1 > self.end:
            return False
        return True

    def gap(self, window) -> str:
        w0, w1 = window
        reasons = []
        if self.start is not None and w0 < self.start:
            reasons.append(f"it begins before that source starts ({self.start_text})")
        if self.end is not None and w1 > self.end:
            reasons.append(f"it ends after that source stops ({self.end_text})")
        return (f"{self.label} covers {self.start_text or 'the beginning'} .. "
                f"{self.end_text or 'now'}; the window {stamp(w0)} .. {stamp(w1)} "
                f"is not inside it ({'; '.join(reasons)}) — not read, and not 0")


# ----------------------------------------------------------------------------
# measures
# ----------------------------------------------------------------------------

def measure(value, source, missing=None, per_lane=None) -> dict:
    """One number and the source that produced it.

    A null value MUST carry `missing`; a real value must NOT (the contract
    forbids it both ways, so "0 with an excuse attached" cannot be written).
    """
    m = {"value": value, "source": source}
    if value is None:
        m["missing"] = missing or "not read"
    elif per_lane is not None:
        m["per_lane"] = per_lane
    return m


class Transcripts:
    """The lanes' transcript logs, parsed once into timed events.

    Events per session file, in time order, each tagged with its lane (the
    directory under the transcripts root). Sidechain, meta, compact-summary
    and tool-result lines are not turns and are dropped here, once, so no
    measure can forget to drop them.
    """

    def __init__(self, root: Path):
        self.root = root
        self.lanes: list[str] = []
        self.sessions: list[tuple[str, list[tuple[datetime, str]]]] = []
        lanes = set()
        for path in sorted(root.rglob("*.jsonl")):
            rel = path.relative_to(root)
            lane = rel.parts[0] if len(rel.parts) > 1 else path.stem
            lanes.add(lane)
            events = self._events(path)
            if events:
                self.sessions.append((lane, events))
        self.lanes = sorted(lanes)

    @staticmethod
    def _events(path: Path) -> list[tuple[datetime, str]]:
        out = []
        for raw in path.read_text(errors="replace").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except ValueError:
                continue  # a torn or non-JSON line is not a turn
            if not isinstance(rec, dict):
                continue
            kind = _event_kind(rec)
            if kind is None:
                continue
            when = rec.get("timestamp")
            if not when:
                continue
            try:
                out.append((parse_instant(when), kind))
            except (ValueError, TypeError):
                continue
        out.sort(key=lambda e: e[0])
        return out

    def read(self, window) -> dict:
        """Count the window's turns and measure the waits Tim's lanes sat in."""
        typed = {lane: 0 for lane in self.lanes}
        answers = {lane: 0 for lane in self.lanes}
        hub_wakes = {lane: 0 for lane in self.lanes}
        no_origin = {lane: 0 for lane in self.lanes}
        waits: list[float] = []
        wait_per_lane = {lane: 0.0 for lane in self.lanes}
        for lane, events in self.sessions:
            last_assistant = None   # end of the lane's last turn
            unanswered = False      # a typed turn is waiting for its answer
            for when, kind in events:
                if kind == "typed":
                    # The wait Tim's lane just ended: from the moment it
                    # stopped talking to the moment he typed again.
                    if last_assistant is not None and _in(window, when):
                        seconds = (when - last_assistant).total_seconds()
                        waits.append(seconds)
                        wait_per_lane[lane] += seconds
                    if _in(window, when):
                        typed[lane] += 1
                    unanswered = True
                elif kind == "assistant":
                    if unanswered:
                        if _in(window, when):
                            answers[lane] += 1
                        unanswered = False
                    last_assistant = when
                elif kind == "hub_wake":
                    if _in(window, when):
                        hub_wakes[lane] += 1
                elif kind == "no_origin":
                    if _in(window, when):
                        no_origin[lane] += 1
        return {
            "typed": typed,
            "answers": answers,
            "hub_wakes": hub_wakes,
            "no_origin": no_origin,
            "waits": waits,
            "wait_per_lane": {k: round(v) for k, v in wait_per_lane.items()},
        }


def _event_kind(rec: dict):
    """What this transcript line is, or None when it is not a turn at all."""
    if rec.get("isSidechain") or rec.get("isMeta") or rec.get("isCompactSummary"):
        return None
    kind = rec.get("type")
    if kind == "assistant":
        return "assistant"
    if kind != "user":
        return None
    if _is_tool_result(rec):
        return None  # the harness feeding a tool's output back in, not a turn
    origin = rec.get("origin")
    if not isinstance(origin, dict) or not origin.get("kind"):
        return "no_origin"
    if origin["kind"] == HUMAN_ORIGIN:
        return "typed"
    if origin["kind"] == CHANNEL_ORIGIN:
        return "hub_wake"
    return None  # a task notification and friends: a wake, but not the hub's


def _is_tool_result(rec: dict) -> bool:
    if rec.get("toolUseResult") is not None:
        return True
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, list):
        return any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
    return False


class HubMessages:
    """The hub's message rows, read through `open_hub_ro`.

    The copy that reaches this machine may be the BODY-LESS projection
    mcp-hub builds on prod-1 — `messages(ts, from_agent, to_agent, channel)`
    plus `wake_log` — so only those columns are ever selected, and a copy
    without `wake_log` at all reads as "no wake rows here", never as none.
    """

    def __init__(self, path):
        self.rows: list[tuple[float, str, str, str]] = []
        self.wake_rows: list[tuple[float, str]] | None = None
        conn = open_hub_ro(path)
        try:
            cur = conn.execute(
                "SELECT ts, from_agent, to_agent, channel FROM messages")
            self.rows = [(r[0], r[1] or "", r[2] or "", r[3] or "") for r in cur]
            try:
                cur = conn.execute("SELECT ts, reason FROM wake_log")
                self.wake_rows = [(r[0], r[1] or "") for r in cur]
            except sqlite3.Error:
                self.wake_rows = None   # the table is absent from this copy
        finally:
            conn.close()

    def relay(self, window, voice: str) -> tuple[int, int]:
        """(Tim speaking through the relay, answers reaching him through it)."""
        start, end = (w.timestamp() for w in window)
        out = inbound = 0
        for ts, frm, to, channel in self.rows:
            if ts is None or not (start <= ts < end) or channel:
                continue
            if frm == voice and to and to != voice:
                out += 1
            elif to == voice and frm != voice:
                inbound += 1
        return out, inbound

    def wakes(self, window, reason: str) -> int:
        start, end = (w.timestamp() for w in window)
        total = 0
        for ts, row_reason in self.wake_rows or []:
            if ts is None or not (start <= ts < end):
                continue
            if row_reason == reason:
                total += 1
        return total


class WakeSnapshot:
    """A snapshot of the hub's wake ledger, taken outside the meter.

    Column-driven: the reading names the snapshot's own `source`/`taken_at`, so
    a number can be traced back to the file it was read from. `since` and
    `taken_at` are also its COVERAGE — the snapshot says nothing at all about
    an hour outside them.
    """

    def __init__(self, path):
        doc = json.loads(Path(path).read_text())
        columns = doc.get("columns") or []
        self.source = str(doc.get("source") or "unnamed")
        self.taken_at = str(doc.get("taken_at") or "unknown")
        self.since = str(doc.get("since") or "")
        self.rows = [dict(zip(columns, row)) for row in doc.get("rows") or []]

    def coverage(self) -> Coverage:
        return Coverage(
            f"the wake snapshot ({self.source}, taken {self.taken_at})",
            start=_maybe_instant(self.since), end=_maybe_instant(self.taken_at),
            start_text=self.since or "", end_text=self.taken_at)

    def wakes(self, window, reason: str) -> int:
        total = 0
        for row in self.rows:
            when = row.get("hour_utc")
            if not when:
                continue
            try:
                hour = parse_instant(when)
            except (ValueError, TypeError):
                continue
            if _in(window, hour) and row.get("reason") == reason:
                total += int(row.get("count") or 0)
        return total


class Sources:
    """The three sources, their coverage, and where they were read.

    One object so every measure set — the baseline's, each day's and each
    change's two — is built from the same facts, and so a coverage rule can
    never be applied in one place and forgotten in another.
    """

    def __init__(self, hub, snapshot, transcripts, *, voice, transcripts_path,
                 hub_taken_at, host=None):
        self.hub = hub
        self.snapshot = snapshot
        self.transcripts = transcripts
        self.voice = voice
        self.transcripts_path = str(transcripts_path)
        self.host = host or socket.gethostname()
        self.hub_taken_at = hub_taken_at
        taken = stamp(hub_taken_at) if hub_taken_at else ""
        self.hub_cov = Coverage(f"the hub copy at {hub['path']} (taken {taken})",
                                end=hub_taken_at, end_text=taken)
        wake_start = (hub_taken_at - timedelta(days=WAKE_LOG_DAYS)) if hub_taken_at else None
        self.wake_cov = Coverage(
            f"the hub copy's wake_log at {hub['path']} (the {WAKE_LOG_DAYS} days "
            f"before {taken})", start=wake_start, end=hub_taken_at, end_text=taken)
        self.snap_cov = (snapshot["data"].coverage() if snapshot["read"]
                         else Coverage(f"the wake snapshot at {snapshot['path']}"))

    # -- the pane (transcripts) --------------------------------------------
    @property
    def pane_label(self) -> str:
        """Term 3: a transcript count is this HOST's, read from this dir.

        A count read on one machine is not the fleet's count — dev-vm-1 holds
        dev-vm-1's lanes. Naming the host and the directory in the source is
        what stops a per-box number being quoted as a fleet-wide one.
        """
        return f"transcripts on {self.host} at {self.transcripts_path}"

    def pane(self, window):
        return self.transcripts["data"].read(window) if self.transcripts["read"] else None

    # -- the hub copy ------------------------------------------------------
    def relay(self, window):
        """(out, in, missing) — the relay counts, or why they were not read."""
        if not self.hub["read"]:
            return None, None, self.hub["missing"]
        if not self.hub_cov.covers(window):
            return None, None, self.hub_cov.gap(window)
        out, inbound = self.hub["data"].relay(window, self.voice)
        return out, inbound, ""

    # -- operator wakes: the snapshot, else the copy's wake_log ------------
    def operator_wakes(self, window) -> tuple[object, str, str]:
        """(value, source, missing) for the window's operator wakes.

        The snapshot is preferred where it covers the window (it is the
        operator plane's own summary); past its `taken_at` the copy's
        `wake_log` answers for the rows it still holds. Neither covering is
        NOT zero: the reason names both boundaries, so the next reading can
        be taken with a source that reaches.
        """
        label = f"{stamp(window[0])} .. {stamp(window[1])}"
        snap_source = (f"wake snapshot ({self.snapshot['label']}): rows with "
                       f"reason=\"operator\" in {label}, counts summed")
        log_source = (f"hub copy wake_log ({self.hub['path']}): rows with "
                      f"reason=\"operator\" in {label}, counted")
        why = []
        if self.snapshot["read"]:
            if self.snap_cov.covers(window):
                return self.snapshot["data"].wakes(window, "operator"), snap_source, ""
            why.append(self.snap_cov.gap(window))
        else:
            why.append(self.snapshot["missing"])
        if not self.hub["read"]:
            why.append(self.hub["missing"])
        elif self.hub["data"].wake_rows is None:
            why.append(f"the hub copy at {self.hub['path']} carries no wake_log table")
        elif not self.wake_cov.covers(window):
            why.append(self.wake_cov.gap(window))
        else:
            return self.hub["data"].wakes(window, "operator"), log_source, ""
        return None, snap_source, "; and ".join(w for w in why if w)


def measure_set(window, src: Sources) -> dict:
    """The seven measures over one window, each naming its source."""
    label = f"{stamp(window[0])} .. {stamp(window[1])}"
    pane = src.pane(window)
    gone = src.transcripts["missing"]
    relay_out, relay_in, relay_gone = src.relay(window)
    wake_value, wake_source, wake_gone = src.operator_wakes(window)

    typed_source = (
        f'{src.pane_label}: user turns with origin.kind="{HUMAN_ORIGIN}" in {label} '
        "(sidechain, meta, compact-summary and tool-result lines are not turns) "
        "— an UPPER BOUND on what Tim typed, since a `squad send-keys` injection "
        "lands in a lane's transcript in exactly the same shape"
    )
    relay_source = (
        f"hub db messages: DMs from {src.voice} to a lane in {label} — Tim speaking "
        "through the voice relay"
    )
    answer_source = (
        f"{src.pane_label}: the lane's first assistant turn after each typed turn in "
        f"{label} — the answer that reaches Tim in the pane"
    )
    inbound_source = (
        f"hub db messages: DMs addressed to {src.voice} in {label} — a lane's answer "
        "reaching Tim through the voice relay"
    )
    wait_source = (
        f"{src.pane_label}: each wait from a lane's last assistant turn to Tim's next "
        f"typed turn in the same session, ending in {label}"
    )
    hub_wake_source = (
        f'{src.pane_label}: user turns with origin.kind="{CHANNEL_ORIGIN}" in {label} '
        "— a hub DM or broadcast that woke the lane rather than Tim"
    )
    no_origin_source = (
        f"{src.pane_label}: user turns in {label} with no origin (not counted) as "
        "typed turns or as hub wakes — the harness recorded no origin.kind, so "
        "the cause of the turn is unknown and is reported rather than guessed"
    )

    waits = pane["waits"] if pane else None
    return {
        "typed_turns": {
            "pane": measure(
                sum(pane["typed"].values()) if pane else None, typed_source,
                gone, pane["typed"] if pane else None),
            "voice_relay": measure(relay_out, relay_source, relay_gone),
        },
        "reaching_tim": {
            "pane": measure(
                sum(pane["answers"].values()) if pane else None, answer_source,
                gone, pane["answers"] if pane else None),
            "voice_relay": measure(relay_in, inbound_source, relay_gone),
        },
        "wait_total_seconds": {
            "pane": measure(
                round(sum(waits)) if waits is not None else None, wait_source,
                gone, pane["wait_per_lane"] if pane else None),
        },
        "wait_median_seconds": {
            # No waits is NOT a wait of zero: an empty median is undefined, and
            # a 0 here would read as "Tim never waited" on a silent week.
            "pane": measure(
                round(statistics.median(waits)) if waits else None,
                wait_source.replace("each wait", "the median wait"),
                gone if waits is None else f"no wait ended in {label}"),
        },
        "operator_wakes": measure(wake_value, wake_source, wake_gone),
        "hub_wakes": measure(
            sum(pane["hub_wakes"].values()) if pane else None, hub_wake_source,
            gone, pane["hub_wakes"] if pane else None),
        "no_origin": measure(
            sum(pane["no_origin"].values()) if pane else None, no_origin_source,
            gone, pane["no_origin"] if pane else None),
    }


# ----------------------------------------------------------------------------
# inputs
# ----------------------------------------------------------------------------

def load_input(name: str, path, loader) -> dict:
    """Load one source, recording WHY it could not be read rather than failing.

    One unreadable source must not cost the reading the other two: the measures
    it feeds go null-with-a-reason and the rest are still measured.
    """
    rec = {"name": name, "path": str(path), "read": False, "missing": "", "data": None,
           "label": str(path)}
    try:
        rec["data"] = loader(path)
        rec["read"] = True
    except (OSError, ValueError, TypeError, sqlite3.Error) as exc:
        rec["missing"] = f"{name} unreadable at {path}: {type(exc).__name__}: {exc}"
    return rec


def read_inputs(args) -> list[dict]:
    hub = load_input("hub_db", args.hub_db, HubMessages)
    snapshot = load_input("snapshot", args.snapshot, WakeSnapshot)
    if snapshot["read"]:
        snapshot["label"] = (f"{snapshot['data'].source}, taken "
                             f"{snapshot['data'].taken_at}")
    transcripts = load_input("transcripts", args.transcripts, _load_transcripts)
    return [hub, snapshot, transcripts]


def _load_transcripts(path) -> Transcripts:
    root = Path(path)
    if not root.is_dir():
        raise OSError(f"no such transcripts directory: {root}")
    return Transcripts(root)


def hub_db_taken_at(args) -> datetime | None:
    """When the hub copy stopped being able to answer for anything.

    `--hub-db-taken-at` when given, else the file's own mtime — a copy taken
    by `sqlite3 .backup` or by the projection job carries the moment it was
    written, and that moment IS its coverage end. None only when there is no
    file to ask (an unreadable hub names its own reason instead).
    """
    if getattr(args, "hub_db_taken_at", None):
        return parse_instant(args.hub_db_taken_at)
    try:
        return datetime.fromtimestamp(os.stat(args.hub_db).st_mtime, timezone.utc)
    except OSError:
        return None


def load_changes(path) -> dict:
    """{changes: [...], source_stops: [...]} from the changes file.

    Both shapes are accepted: a bare list of changes (r1's) and the document
    `{"changes": [...], "source_stops": [{"measure", "stopped_at"}]}` the
    fleet's own `v2-fleet/changes.json` now carries.
    """
    if not path:
        return {"changes": [], "source_stops": []}
    doc = json.loads(Path(path).read_text())
    rows = doc.get("changes") if isinstance(doc, dict) else doc
    out = []
    for row in rows or []:
        out.append({
            "change_id": str(row["change_id"]),
            "at": parse_instant(row["at"]),
            "delivered": bool(row.get("delivered", False)),
        })
    out.sort(key=lambda c: c["at"])
    stops = []
    for row in (doc.get("source_stops") if isinstance(doc, dict) else None) or []:
        stops.append({
            "measure": str(row["measure"]),
            "stopped_at": parse_instant(row["stopped_at"]),
        })
    stops.sort(key=lambda s: s["stopped_at"])
    return {"changes": out, "source_stops": stops}


# ----------------------------------------------------------------------------
# the day-by-day record
# ----------------------------------------------------------------------------

def closed_days(start: datetime, now: datetime) -> list[datetime]:
    """Every UTC day from `start` whose 24 hours have fully elapsed at `now`.

    The OPEN day is left out on purpose: half a day beside sixty whole ones
    reads as a quiet day, and a dip on the newest point is exactly what a
    load meter is looked at for.
    """
    day = start.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    out = []
    while day + timedelta(days=1) <= now:
        out.append(day)
        day += timedelta(days=1)
    return out


# ----------------------------------------------------------------------------
# the CLI surface
# ----------------------------------------------------------------------------

def add_arguments(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    parser.add_argument("--hub-db", required=True,
                        help="Hub database (or an immutable snapshot of one) to read")
    parser.add_argument("--snapshot", required=True,
                        help="JSON snapshot of the hub's wake ledger (operator plane)")
    parser.add_argument("--transcripts", required=True,
                        help="Root of the lanes' transcript logs (<lane>/<session>.jsonl)")
    parser.add_argument("--out", required=True,
                        help="Where to write the reading (JSON, contract/load-reading.schema.json)")
    parser.add_argument("--baseline-start", required=True, metavar="YYYY-MM-DD",
                        help=f"First day of the baseline ({MIN_BASELINE_DAYS} days or more, "
                             "all of them before the first change)")
    parser.add_argument("--baseline-end", required=True, metavar="YYYY-MM-DD",
                        help="Day the baseline ends (exclusive)")
    parser.add_argument("--changes", default=None,
                        help="JSON list of v2 changes [{change_id, at, delivered}] "
                             "to read a before/after for, or a document with "
                             "`changes` and `source_stops`")
    parser.add_argument("--now", default=None, metavar="ISO8601",
                        help="The instant of this reading (default: now, UTC)")
    parser.add_argument("--hub-db-taken-at", default=None, metavar="ISO8601",
                        help="When the hub copy was taken — its coverage ends there "
                             "(default: the file's mtime). A window past it reads "
                             "null with a reason, never 0")
    parser.add_argument("--voice-agent", default=VOICE_RELAY_AGENT,
                        help=f"Hub name of the voice relay (default: {VOICE_RELAY_AGENT})")
    return parser


def run(args) -> int:
    now = parse_instant(args.now) if args.now else datetime.now(timezone.utc)
    try:
        start, end = parse_day(args.baseline_start), parse_day(args.baseline_end)
    except ValueError as exc:
        print(f"!! --baseline-start/--baseline-end must be YYYY-MM-DD: {exc}", file=sys.stderr)
        return 2
    days = (end - start).days

    # ⭐ REFUSE BEFORE WRITING ANYTHING. A reading whose baseline is too short,
    # or overlaps a change, is not a weaker reading — it is a number that will
    # be quoted as the "before" long after the caveat is forgotten.
    if days < MIN_BASELINE_DAYS:
        print(f"!! baseline is {days} day(s) ({args.baseline_start} .. {args.baseline_end}): "
              f"at least {MIN_BASELINE_DAYS} days are required, so a weekend cannot "
              f"read as a quiet fleet — nothing written", file=sys.stderr)
        return 2

    try:
        doc = load_changes(args.changes)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(f"!! --changes unreadable: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    changes, stops = doc["changes"], doc["source_stops"]

    if changes and end > changes[0]["at"]:
        first = changes[0]
        print(f"!! the baseline must end at or before the first v2 change "
              f"({first['change_id']} at {stamp(first['at'])}): --baseline-end "
              f"{args.baseline_end} is after it, so the baseline would measure a "
              f"fleet already changed — nothing written", file=sys.stderr)
        return 2

    try:
        taken = hub_db_taken_at(args)
    except (ValueError, TypeError) as exc:
        print(f"!! --hub-db-taken-at must be an ISO-8601 instant: {exc}", file=sys.stderr)
        return 2

    hub, snapshot, transcripts = read_inputs(args)
    src = Sources(hub, snapshot, transcripts, voice=args.voice_agent,
                  transcripts_path=args.transcripts, hub_taken_at=taken)
    baseline = (start, end)
    reading = {
        "generated_at": stamp(now),
        "baseline": {"start": args.baseline_start, "end": args.baseline_end, "days": days},
        "measures": measure_set(baseline, src),
        "daily": [],
        "changes": [],
        "flags": [],
        "inputs": [],
    }

    # --- one measure set per closed day since the baseline ----------------
    wanted = closed_days(end, now)
    dropped = wanted[:-MAX_DAILY_DAYS] if len(wanted) > MAX_DAILY_DAYS else []
    for day in wanted[len(dropped):]:
        reading["daily"].append({
            "day": day.date().isoformat(),
            "measures": measure_set((day, day + timedelta(days=1)), src),
        })
    if dropped:
        reading["flags"].append({
            "code": "daily_truncated",
            "detail": (f"{len(dropped)} day(s) older than the newest {MAX_DAILY_DAYS} "
                       f"({dropped[0].date().isoformat()} .. "
                       f"{dropped[-1].date().isoformat()}) are not in `daily`: it holds "
                       f"the last {MAX_DAILY_DAYS} closed days, and the days it dropped "
                       "are said out loud rather than quietly missing"),
        })

    span = timedelta(days=days)
    for change in changes:
        at = change["at"]
        windows = {"before": (at - span, at), "after": (at, at + span)}
        row = {"change_id": change["change_id"], "at": stamp(at),
               "before": None, "after": None}
        for side, window in windows.items():
            # An incomplete window is NOT a reading. Half an "after" window
            # compared against a full baseline is a measurement of the calendar.
            if window[1] <= now:
                row[side] = measure_set(window, src)
            elif change["delivered"]:
                reading["flags"].append({
                    "code": f"missing_{side}",
                    "change_id": change["change_id"],
                    "detail": (f"the {days}-day {side} window for {change['change_id']} "
                               f"({stamp(window[0])} .. {stamp(window[1])}) has not closed "
                               f"at {stamp(now)}: no {side} reading yet, read again once it has"),
                })
        reading["changes"].append(row)

        # ⭐ A SOURCE THAT STOPPED IS NOT A CHANGE THAT WORKED (Term 2).
        # operator-console stopped on 8 Oct; every "after" window straddling
        # that date reads fewer operator wakes, and the drop belongs to the
        # console going away, not to the change. The pair is flagged NOT
        # COMPARABLE and no saving is read off it.
        if row["before"] is not None and row["after"] is not None:
            lo, hi = windows["before"][0], windows["after"][1]
            for stop in stops:
                if lo <= stop["stopped_at"] < hi:
                    reading["flags"].append({
                        "code": "source_stopped",
                        "change_id": change["change_id"],
                        "detail": (f"not comparable: source stopped — {stop['measure']} "
                                   f"stopped at {stamp(stop['stopped_at'])}, inside "
                                   f"{change['change_id']}'s before/after span "
                                   f"({stamp(lo)} .. {stamp(hi)}). The two readings "
                                   "measure different fleets, so the difference is "
                                   "not a measure of this change"),
                    })

    for rec in (hub, snapshot, transcripts):
        row = {"name": rec["name"], "path": rec["path"], "read": rec["read"]}
        if not rec["read"]:
            row["missing"] = rec["missing"]
        reading["inputs"].append(row)

    out = Path(args.out)
    out.write_text(json.dumps(reading, indent=2) + "\n")
    unread = [r["name"] for r in reading["inputs"] if not r["read"]]
    print(f"load reading written: {out}")
    print(f"baseline {args.baseline_start} .. {args.baseline_end} ({days} days), "
          f"{len(reading['daily'])} day(s), {len(reading['changes'])} change(s), "
          f"{len(reading['flags'])} flag(s)"
          + (f", UNREAD: {', '.join(unread)}" if unread else ""))
    return 0
