"""Is this lane in conversation with Tim? (v2-fleet r5 F6, j6-conversation-exemption)

A hold placed by a MECHANISM — a brake, the hibernation scanner — must not
stop a lane in the middle of a conversation: he asks a question, the boundary
stop lands on the answer, and the pane he is typing into is gone. squad's
enforcement pass asks this module, once per pass, about its owner-held lanes:

    python -m mcp_hub.conversation <lane>[=<worktree>] ... [--injections PATH]
                                   [--window SECONDS] [--in-use LANE ...]

and reads back one flat line per lane asked, in the order asked:

    <lane> <1|0> <conversation_end epoch, 0 when there is no typed turn>

so it never needs jq to learn whether Tim is talking. A bare `<lane>` takes
its worktree from squad's roster (field 2), the same file and rule squad
reads.

In conversation = a TYPED, NON-INJECTED turn within the window
(MCP_HUB_CONVERSATION_SECONDS, default 1800), or a pane in use. Its END is
that turn plus the window: the moment the hard-stop clock runs from, so a lane
held at 12:00 that he then talked to until 12:20 has not spent twenty minutes
ignoring the hold.

🔴 AN INJECTED TURN IS NOT A CONVERSATION. A heal nudge or a `squad cmd` line
lands in the transcript in exactly the shape of a line Tim typed, so without
F4's ledger (mcp_hub.injections) a lane could keep ITSELF exempt from the
brake by being nudged. A turn is injected when a ledger row for its lane with
the same sha256 was written 0..5 s BEFORE it — rows are written before the
keystroke, so one that follows the turn describes a different event.

Fails towards KEEPING THE LANE UP: with no ledger nothing can be shown to be
injected, every typed turn counts, and the mechanism waits.

Only the transcript TAIL is read (TAIL_BYTES), split on "\\n" alone: a record
is one "\\n"-terminated line, and `str.splitlines()` would also break at the
raw U+2028/U+2029/U+0085 JSON allows inside a string.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_WINDOW_SECONDS = 1800.0
TAIL_BYTES = 256 * 1024
# A ledger row is written BEFORE the keystroke; the turn lands within this.
INJECTION_WINDOW_SECONDS = 5.0


def window_seconds() -> float:
    try:
        return float(os.environ.get("MCP_HUB_CONVERSATION_SECONDS")
                     or DEFAULT_WINDOW_SECONDS)
    except ValueError:
        return DEFAULT_WINDOW_SECONDS


def project_dirname(path: str) -> str:
    """The folder Claude Code names a worktree by under ~/.claude/projects:
    every character that is not [A-Za-z0-9] becomes '-'. The cli's
    `_claude_project_dirname` rule, kept identical (a separators-only rule
    points a dotted worktree at a folder that does not exist)."""
    return re.sub(r"[^A-Za-z0-9]", "-", path.rstrip("/\\"))


def transcript_dir(worktree: str, home: Path | str | None = None) -> Path:
    home = Path(home) if home is not None else Path.home()
    if worktree == "~" or worktree.startswith("~/"):
        worktree = str(home) + worktree[1:]
    return home / ".claude" / "projects" / project_dirname(worktree)


def _sha256(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _instant(rec: Any) -> float | None:
    """A transcript record's `timestamp` as epoch seconds, None if absent."""
    value = rec.get("timestamp") if isinstance(rec, dict) else rec
    if not isinstance(value, str) or not value.strip():
        return None
    s = value.strip()
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _text_of(message: Any) -> str:
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(p.get("text", "")) for p in content
                         if isinstance(p, dict) and p.get("type") == "text")
    return ""


def _is_typed(rec: dict[str, Any]) -> bool:
    """A turn a person typed: a user record whose origin is `human`. A hub
    wake (`channel`), a tool result, a sidechain or a meta line is not."""
    if rec.get("type") != "user":
        return False
    if rec.get("isSidechain") or rec.get("isMeta") or rec.get("isCompactSummary"):
        return False
    if "toolUseResult" in rec:
        return False
    msg = rec.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, list) and any(
            isinstance(p, dict) and p.get("type") == "tool_result" for p in content):
        return False
    origin = rec.get("origin")
    return isinstance(origin, dict) and origin.get("kind") == "human"


class Ledger:
    """F4's injection ledger, indexed by (lane, sha256) → row times."""

    def __init__(self, rows: list[dict[str, Any]]):
        self._index: dict[tuple[str, str], list[float]] = {}
        for row in rows:
            try:
                key = (str(row["lane"]), str(row["sha256"]))
                self._index.setdefault(key, []).append(float(row["ts"]))
            except (KeyError, TypeError, ValueError):
                continue

    @classmethod
    def load(cls, path: Path | str | None = None) -> Ledger:
        """The ledger at `path` (default: injections' own). Absent or
        unreadable is an EMPTY ledger: every turn then reads as typed, which
        keeps the lane up — the safe way round for an exemption."""
        try:
            from mcp_hub import injections

            return cls(injections.read_rows(path))
        except Exception:  # noqa: BLE001
            return cls([])

    def injected(self, names: tuple[str, ...], sha: str, at: float) -> bool:
        for name in names:
            for ts in self._index.get((name, sha), ()):
                if 0.0 <= at - ts <= INJECTION_WINDOW_SECONDS:
                    return True
        return False


def tail_lines(path: Path | str) -> list[str]:
    """The complete lines in the last TAIL_BYTES of a transcript.

    Split on "\\n" only, never `str.splitlines()`. When the read starts
    mid-file its first piece is a torn line and is dropped; the trailing
    empty piece after the final "\\n" is dropped too. [] on any trouble."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            start = max(0, size - TAIL_BYTES)
            fh.seek(start)
            raw = fh.read()
    except OSError:
        return []
    pieces = raw.decode("utf-8", errors="ignore").split("\n")
    if start > 0 and pieces:
        pieces = pieces[1:]
    if pieces and pieces[-1] == "":
        pieces = pieces[:-1]
    return pieces


def newest_transcript(worktree: str, home: Path | str | None = None) -> Path | None:
    """The session file modified LAST — session ids are uuids and sort
    arbitrarily, so a name order would read a resumed lane's oldest session
    as its current one."""
    try:
        files = [p for p in transcript_dir(worktree, home).glob("*.jsonl")
                 if p.is_file()]
        return max(files, key=lambda p: p.stat().st_mtime) if files else None
    except OSError:
        return None


def last_typed_turn(lane: str, worktree: str, ledger: Ledger,
                    home: Path | str | None = None) -> float:
    """When this lane's last typed, non-injected turn landed; 0.0 if none.

    Walks the newest transcript's tail backwards, past injected turns, to
    the first real one. A torn or non-JSON line is skipped."""
    path = newest_transcript(worktree, home)
    if path is None:
        return 0.0
    names = (lane, project_dirname(worktree))
    for line in reversed(tail_lines(path)):
        if '"user"' not in line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict) or not _is_typed(rec):
            continue
        at = _instant(rec)
        if at is None:
            continue
        if ledger.injected(names, _sha256(_text_of(rec.get("message"))), at):
            continue
        return at
    return 0.0


def read(lanes: list[tuple[str, str]], *, now: float | None = None,
         window: float | None = None, home: Path | str | None = None,
         ledger_path: Path | str | None = None,
         in_use: set[str] | frozenset[str] = frozenset()) -> list[dict[str, Any]]:
    """One verdict per (lane, worktree), in order: `in_conversation` and
    `conversation_end` (last typed turn + window; 0 when there is none, so
    the hard-stop clock falls back to the hold's own held_at). A pane in use
    is conversation, ending no earlier than now."""
    now = time.time() if now is None else now
    window = window_seconds() if window is None else float(window)
    ledger = Ledger.load(ledger_path)
    out = []
    for lane, worktree in lanes:
        last = last_typed_turn(lane, worktree, ledger, home) if worktree else 0.0
        end = last + window if last else 0.0
        talking = bool(last) and end > now
        if lane in in_use:
            talking, end = True, max(end, now)
        out.append({"lane": lane, "in_conversation": talking,
                    "conversation_end": end})
    return out


def _roster_worktrees() -> dict[str, str]:
    """{agent: worktree} from squad's roster — the first row naming each."""
    try:
        from mcp_hub import injections

        text = injections.roster_path().read_text(encoding="utf-8",
                                                  errors="replace")
    except Exception:  # noqa: BLE001
        return {}
    out: dict[str, str] = {}
    for line in text.split("\n"):
        if re.match(r"^[ \t]*(#|$)", line):
            continue
        fields = line.split("|")
        if len(fields) >= 2 and fields[1] and fields[0] not in out:
            out[fields[0]] = fields[1]
    return out


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    specs: list[str] = []
    ledger: str | None = None
    window: float | None = None
    in_use: set[str] = set()
    it = iter(argv)
    for arg in it:
        if arg == "--injections":
            ledger = next(it, None)
        elif arg == "--window":
            try:
                window = float(next(it, ""))
            except ValueError:
                window = None
        elif arg == "--in-use":
            v = next(it, None)
            if v:
                in_use.add(v)
        else:
            specs.append(arg)
    roster: dict[str, str] | None = None
    lanes: list[tuple[str, str]] = []
    for spec in specs:
        if "=" in spec:
            lane, worktree = spec.split("=", 1)
        else:
            if roster is None:
                roster = _roster_worktrees()
            lane, worktree = spec, roster.get(spec, "")
        lanes.append((lane, worktree))
    for v in read(lanes, window=window, ledger_path=ledger, in_use=in_use):
        print(f"{v['lane']} {1 if v['in_conversation'] else 0} "
              f"{v['conversation_end']:.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
