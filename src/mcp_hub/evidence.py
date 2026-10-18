"""Lane evidence — what each lane last said it was doing (F14, v2-fleet).

The brain lane answers "where's X at?" for the operator in under two seconds
by READING, instead of DMing the lane and waiting 30-90s. This module is the
hub half: at every Stop the hook records the lane's last substantive reply,
and `evidence(lane)` / `evidence_all()` serve it back on request.

What this is, and the four things it is deliberately not:

- EVIDENCE, not status. The hub stores what it SAW — the reply text, when,
  at which commit, under which grade. Merging that into claims (progress /
  blocker / next) is the brain's job (F15), served under the brain's name.
  A summary served from here would carry the hub's authority for a guess,
  which is the line `resolve_status` already refuses to cross.
- PULL-ONLY. Nothing here pushes, wakes or lands in anyone's context. A
  store that interrupted would be the inbox noise it exists to replace.
- NOT AN AGENT RITUAL. The hook derives the record from the transcript it
  already holds; agents write nothing. A per-reply demand on agents is the
  DECISION-card failure this is the alternative to.
- NEVER STALE-AS-CURRENT. Past EVIDENCE_STALE_SECONDS the text is withheld
  and the lane reads "not reporting". An instrument that stopped being
  written must not be read as a measurement — the fleet-snapshot rule
  (fleet_tree.FLEET_STALE_SECONDS), applied to a slower clock.

"Substantive" is decided from the transcript, mechanically: a turn the
operator started counts, and so does any turn that called a tool. A turn
woken by the hub or the Stop hook that only REPLIED — "nothing new, card
still waiting" — does not overwrite the record. Without that rule a lane's
real blocker is replaced by its next inbox acknowledgement within minutes
(measured on mcp-hub-dev-vm-1, 2026-09-28 → 10-06: most replies were acks).
Those turns still refresh `last_seen_at`, so "alive but quiet" and "gone"
read differently.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Any

# How old evidence may get before it reads "not reporting". A lane idle
# overnight is genuinely not reporting — the operator asking "where's X at?"
# at 09:00 must hear that X last spoke at 22:00, not hear 22:00's words as
# now. Six hours covers a working session's gaps without carrying a day.
EVIDENCE_STALE_SECONDS = 6 * 3600

# Stored text cap. The record is evidence for the brain's summariser and for
# a voice read-out, not an archive: the full reply lives only in the lane's
# own transcript. Clipping is FLAGGED, never silent: a clipped record that
# reads as whole puts a weaker claim in the lane's mouth.
EVIDENCE_MAX_CHARS = 4000

# Same window as receipts.TRANSCRIPT_WINDOW, for the same reason: a long
# tool-heavy turn pushes its own start out of a small tail.
TRANSCRIPT_WINDOW = 4 * 1024 * 1024

# Prompts that the machinery, not the operator, put into the conversation.
_MACHINE_PREFIXES = (
    ("Stop hook feedback", "stop-hook"),
    ("<channel ", "hub-wake"),
    ("<task-notification>", "task"),
    ("<local-command", "local-command"),
    ("Caveat: The messages below were generated", "local-command"),
)


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS lane_evidence (
            agent        TEXT PRIMARY KEY,
            project      TEXT NOT NULL DEFAULT '',
            text         TEXT NOT NULL DEFAULT '',
            clipped      INTEGER NOT NULL DEFAULT 0,
            captured_at  REAL NOT NULL DEFAULT 0,
            commit_sha   TEXT NOT NULL DEFAULT '',
            trigger      TEXT NOT NULL DEFAULT '',
            tool_calls   INTEGER NOT NULL DEFAULT 0,
            attribution  TEXT NOT NULL DEFAULT '',
            last_seen_at REAL NOT NULL DEFAULT 0
        )
        """
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Client side — read the turn that just ended out of the transcript tail.
# ---------------------------------------------------------------------------


def _prompt_text(content: Any) -> str | None:
    """The prompt text of a user record, or None for a tool result."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result"
               for b in content):
            return None
        return "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text")
    return None


def classify_trigger(prompt: str) -> str:
    stripped = prompt.lstrip()
    for prefix, kind in _MACHINE_PREFIXES:
        if stripped.startswith(prefix):
            return kind
    return "operator"


def read_turn(transcript_path: str | None) -> dict[str, Any] | None:
    """The turn that just ended: its final reply text, what started it, and
    how many tools it called. None when there is no reply text to record.

    Walks the tail BACKWARDS from the end to the record that opened the turn
    (a user record that is not a tool result). A turn longer than the window
    reads trigger "unknown"; it necessarily called tools to get that long, so
    the substantive verdict does not depend on the missing start.
    """
    if not transcript_path:
        return None
    try:
        with open(transcript_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - TRANSCRIPT_WINDOW))
            tail = f.read().decode("utf-8", errors="ignore")
    except OSError:
        return None
    text = ""
    tool_calls = 0
    trigger = "unknown"
    for line in reversed(tail.splitlines()):
        try:
            rec = json.loads(line)
        except ValueError:
            continue  # first line of the window may be cut mid-record
        kind = rec.get("type")
        content = (rec.get("message") or {}).get("content")
        if kind == "assistant" and isinstance(content, list):
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use":
                    tool_calls += 1
            if not text:
                texts = [b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text"]
                text = "\n".join(t for t in texts if t).strip()
        elif kind == "user":
            prompt = _prompt_text(content)
            if prompt is None:
                continue  # a tool result, mid-turn
            trigger = classify_trigger(prompt)
            break
    if not text:
        return None
    return {
        "text": text,
        "trigger": trigger,
        "tool_calls": tool_calls,
        "substantive": trigger == "operator" or tool_calls > 0,
    }


# ---------------------------------------------------------------------------
# Hub side — store and serve.
# ---------------------------------------------------------------------------


def record(
    conn: sqlite3.Connection, agent: str, *, text: str, substantive: bool,
    trigger: str, tool_calls: int, commit_sha: str, project: str,
    attribution: str, now: float | None = None,
) -> str:
    """Upsert one Stop's evidence. Every Stop refreshes last_seen_at; only a
    substantive one replaces the text."""
    now = time.time() if now is None else now
    conn.execute(
        "INSERT INTO lane_evidence (agent, last_seen_at) VALUES (?, ?) "
        "ON CONFLICT(agent) DO UPDATE SET last_seen_at = excluded.last_seen_at",
        (agent, now),
    )
    if substantive and text.strip():
        clipped = len(text) > EVIDENCE_MAX_CHARS
        conn.execute(
            """UPDATE lane_evidence SET text=?, clipped=?, captured_at=?,
               commit_sha=?, trigger=?, tool_calls=?, attribution=?,
               project=CASE WHEN ?='' THEN project ELSE ? END
               WHERE agent=?""",
            (text[:EVIDENCE_MAX_CHARS], int(clipped), now, commit_sha[:40],
             trigger[:32], int(tool_calls), attribution, project, project,
             agent),
        )
        conn.commit()
        return "recorded"
    conn.commit()
    return "seen"


def view(row: sqlite3.Row | None, agent: str,
         now: float | None = None) -> dict[str, Any]:
    """One lane's evidence as a structured record. `state` is one of:
    current, stale ("not reporting" — text withheld), none (never recorded).
    """
    now = time.time() if now is None else now
    if row is None or not row["captured_at"]:
        return {
            "agent": agent, "state": "none", "text": None,
            "last_seen_at": (row["last_seen_at"] or None) if row else None,
        }
    age = max(0.0, now - row["captured_at"])
    stale = age > EVIDENCE_STALE_SECONDS
    return {
        "agent": row["agent"],
        "state": "stale" if stale else "current",
        "text": None if stale else row["text"],
        "clipped": bool(row["clipped"]),
        "captured_at": row["captured_at"],
        "age_seconds": round(age),
        "last_seen_at": row["last_seen_at"] or None,
        "commit": row["commit_sha"],
        "trigger": row["trigger"],
        "tool_calls": row["tool_calls"],
        "grade": row["attribution"] or "ungraded",
        "project": row["project"],
    }


def _age(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"
    return f"{seconds // 86400}d{(seconds % 86400) // 3600}h"


def render(v: dict[str, Any], grade_tag: str, now: float | None = None) -> str:
    now = time.time() if now is None else now
    agent = v["agent"]
    if v["state"] == "none":
        seen = (f" (last Stop {_age(now - v['last_seen_at'])} ago, nothing "
                "substantive yet)") if v.get("last_seen_at") else ""
        return f"{agent} — NO EVIDENCE recorded{seen}."
    seen = ""
    if v.get("last_seen_at"):
        seen = f", last Stop {_age(now - v['last_seen_at'])} ago"
    head = (f"{agent}{grade_tag} — evidence {_age(v['age_seconds'])} old"
            f"{seen} · trigger {v['trigger']}, {v['tool_calls']} tool call(s)"
            + (f", commit {v['commit'][:12]}" if v["commit"] else ""))
    if v["state"] == "stale":
        return (f"{head}\nNOT REPORTING — older than the "
                f"{_age(EVIDENCE_STALE_SECONDS)} cutoff; text withheld so an "
                "old reply is never read as the lane's current state.")
    body = v["text"]
    if v["clipped"]:
        body += (f"\n[…clipped at {EVIDENCE_MAX_CHARS} chars — the rest is in the "
                 "lane's own transcript; ask the lane]")
    return f"{head}\n{body}"
