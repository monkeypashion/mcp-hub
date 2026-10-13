"""Is a lane in CONVERSATION with Tim right now?

v2-fleet r5 F6 (j6-conversation-exemption). A hold placed by a MECHANISM — a
brake, the hibernation scanner — must not stop a lane in the middle of a
conversation with Tim. The lane disappears mid-sentence, he is talking to a
pane that no longer exists, and the only party who could have told him is
the thing that killed it.

⭐ ONLY A HOLD WITH AN `owner` IS DEFERRED, and that asymmetry is the whole
rule. An owner names the mechanism that placed it; a hold with none was
placed BY HAND, which is Tim's own act — deferring that would be the tool
overruling the person it is protecting him from. The decision lives in
squad (`hold_enforce_one`); this module answers only the question of fact.

🔴 AN INJECTED TURN IS NOT A CONVERSATION. A `squad cmd` line and a heal
nudge land in a transcript in exactly the shape of a line Tim typed, so
without the F4 ledger the fleet's own nudges would read as Tim talking —
and a lane could keep ITSELF exempt from the brake by being nudged. The
ledger is consulted for every candidate turn; a turn it marks injected is
skipped and the search continues backwards.

Reached as `python -m mcp_hub.conversation <lane>=<worktree> ...`, from
squad, ONCE PER PASS. Deliberately not a new `mcp-hub` verb: server.py is
frozen for r5, and a fact squad reads off local files needs no hub.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from mcp_hub import injections

# His number. A lane Tim typed to inside this window is still his
# conversation even while he reads the answer — the gap between a question
# and the next question is not the end of the talk.
DEFAULT_WINDOW_SECONDS = 1800.0

# Only the tail of a transcript can hold the newest turn, and a long-running
# lane's session file reaches tens of megabytes. 256 KiB is ~250 turns of
# ordinary traffic: far past the newest one, and a bounded read is what keeps
# a 46-lane pass inside its CPU budget.
TAIL_BYTES = 256 * 1024

HUMAN_ORIGIN = "human"


def window_seconds() -> float:
    try:
        return float(os.environ.get("MCP_HUB_CONVERSATION_SECONDS")
                     or DEFAULT_WINDOW_SECONDS)
    except (TypeError, ValueError):
        return DEFAULT_WINDOW_SECONDS


def project_dirname(worktree: str) -> str:
    """The lane's transcript directory name, as the client encodes it.

    Same rule as `cli._claude_project_dirname`: every character that is not
    [A-Za-z0-9] becomes '-', so a worktree with a dot in its path maps to
    the folder the client actually writes. Stated again here rather than
    imported because importing `cli` would pull the whole command surface
    into a module squad forks per PASS, and this is the only part needed.
    """
    return re.sub(r"[^A-Za-z0-9]", "-", str(worktree).rstrip("/\\"))


def transcript_dir(worktree: str, home: Path | None = None) -> Path:
    home = home or Path(os.environ.get("HOME") or Path.home())
    return home / ".claude" / "projects" / project_dirname(worktree)


def newest_transcript(worktree: str, home: Path | None = None) -> Path | None:
    """The lane's live session file: the most recently MODIFIED one.

    Not the newest name — session ids are uuids and sort arbitrarily, so a
    name order would read a resumed lane's oldest session as its current one.
    """
    d = transcript_dir(worktree, home)
    try:
        files = [p for p in d.glob("*.jsonl") if p.is_file()]
    except OSError:
        return None
    if not files:
        return None
    return max(files, key=lambda p: p.stat().st_mtime)


def tail_lines(path: Path, limit: int = TAIL_BYTES) -> list[str]:
    """The last `limit` bytes as lines, newest last.

    The first line is dropped whenever the read started mid-file: it is half
    a record, and json would reject it anyway — but saying so here is what
    stops a reader wondering whether a torn line meant a torn transcript.
    """
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > limit:
                fh.seek(size - limit)
                raw = fh.read()
                cut = raw.find(b"\n")
                raw = raw[cut + 1:] if cut >= 0 else b""
            else:
                raw = fh.read()
    except OSError:
        return []
    return raw.decode("utf-8", errors="replace").splitlines()


def _turn_text(rec: dict) -> str:
    """The text of a user turn, as the ledger's sha256 was taken over it."""
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text") or "" for b in content
                       if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _is_typed(rec: dict) -> bool:
    if rec.get("type") != "user":
        return False
    if rec.get("isSidechain") or rec.get("isMeta") or rec.get("isCompactSummary"):
        return False
    if rec.get("toolUseResult") is not None:
        return False
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, list) and any(
            isinstance(b, dict) and b.get("type") == "tool_result"
            for b in content):
        return False
    origin = rec.get("origin")
    return isinstance(origin, dict) and origin.get("kind") == HUMAN_ORIGIN


def _instant(rec: dict) -> float | None:
    raw = rec.get("timestamp")
    if not isinstance(raw, str):
        return None
    text = raw.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).timestamp()
    except (ValueError, TypeError):
        return None


class Ledger:
    """The F4 injections ledger, indexed for "was this turn typed by squad?".

    Keyed by (lane, sha256) so a match costs a dict lookup rather than a scan
    of the whole ledger per candidate turn.
    """

    def __init__(self, rows: list[dict] | None):
        self.readable = rows is not None
        self.by_key: dict[tuple[str, str], list[float]] = {}
        for row in rows or []:
            try:
                ts = float(row.get("ts"))
            except (TypeError, ValueError):
                continue
            key = (str(row.get("lane") or ""), str(row.get("sha256") or ""))
            self.by_key.setdefault(key, []).append(ts)

    @classmethod
    def load(cls, path: Path | None = None) -> Ledger:
        try:
            return cls(injections.read_rows(path))
        except OSError:
            # NOT an empty ledger. Without the ledger nothing can be shown to
            # be injected, so every turn reads as typed — the direction that
            # keeps a lane in conversation and so keeps it UP, which is the
            # safe way round for an exemption.
            return cls(None)

    def injected(self, lane: str, text: str, when: float) -> bool:
        rows = self.by_key.get((lane, injections.sha256(text)))
        if not rows:
            return False
        return any(0 <= when - ts <= injections.MATCH_WINDOW_SECONDS
                   for ts in rows)


def last_typed_turn(lane: str, worktree: str, ledger: Ledger,
                    home: Path | None = None) -> float | None:
    """When Tim last typed into this lane, or None.

    Walks the tail BACKWARDS and stops at the first turn that is typed and
    not injected: the answer is always near the end, and a forward scan would
    parse the whole tail of every lane to learn the same thing.
    """
    path = newest_transcript(worktree, home)
    if path is None:
        return None
    for line in reversed(tail_lines(path)):
        line = line.strip()
        if not line or '"user"' not in line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict) or not _is_typed(rec):
            continue
        when = _instant(rec)
        if when is None:
            continue
        if ledger.injected(lane, _turn_text(rec), when):
            continue
        return when
    return None


def read(lanes: list[tuple[str, str]], *, now: float | None = None,
         window: float | None = None, home: Path | None = None,
         ledger_path: Path | None = None) -> list[dict]:
    """One verdict per lane: {lane, in_conversation, conversation_end}.

    `conversation_end` is the last typed turn plus the window — the moment
    the conversation is over, which is what the hard-stop clock runs from.
    0 means no typed turn was found at all, and the clock then falls back to
    the hold's own `held_at`, exactly as before this feature.
    """
    now = time.time() if now is None else now
    window = window_seconds() if window is None else window
    ledger = Ledger.load(ledger_path)
    out = []
    for lane, worktree in lanes:
        typed = last_typed_turn(lane, worktree, ledger, home)
        end = (typed + window) if typed is not None else 0.0
        out.append({
            "lane": lane,
            "in_conversation": bool(typed is not None and now < end),
            "conversation_end": round(end, 3),
        })
    return out


def _spec(raw: str) -> tuple[str, str]:
    lane, _, worktree = raw.partition("=")
    return lane, worktree


def main(argv: list[str] | None = None) -> int:
    """`<lane> <0|1> <conversation_end>` per line, one line per lane asked.

    A flat, whitespace-separated shape on purpose: squad reads it with `read`
    in a `while` loop and must not need jq to learn whether Tim is talking.
    """
    ap = argparse.ArgumentParser(
        prog="python -m mcp_hub.conversation",
        description="Which of these lanes is in conversation with Tim")
    ap.add_argument("lanes", nargs="*", metavar="LANE=WORKTREE")
    ap.add_argument("--window", type=float, default=None,
                    help="conversation window in seconds "
                         "(default: $MCP_HUB_CONVERSATION_SECONDS or 1800)")
    ap.add_argument("--injections", default=None,
                    help="ledger to read (default: $MCP_HUB_STATE_DIR/injections.jsonl)")
    ap.add_argument("--json", action="store_true",
                    help="emit the verdicts as a JSON list instead")
    args = ap.parse_args(argv)
    specs = [_spec(raw) for raw in args.lanes if raw]
    rows = read(specs, window=args.window,
                ledger_path=Path(args.injections) if args.injections else None)
    if args.json:
        print(json.dumps(rows))
        return 0
    for row in rows:
        print(f"{row['lane']} {1 if row['in_conversation'] else 0} "
              f"{row['conversation_end']}")
    return 0


if __name__ == "__main__":  # pragma: no cover — squad's entry point
    raise SystemExit(main(sys.argv[1:]))
