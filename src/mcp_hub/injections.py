"""The injections ledger — one row per line the fleet types into a pane (F4, v2-fleet).

`typed_turns` counts user turns whose `origin.kind` is "human". A line that
`squad send-keys` (or the edge's seat prompt) types into a lane lands in the
transcript in exactly that shape, so every count of "what Tim typed" carried
the fleet's own nudges and asks — and carried MORE of them exactly when the
fleet was busiest. That is not a known margin; it is wrong in the one
direction nobody checks.

This ledger is the missing fact. Every typing door writes one row BEFORE its
first keystroke:

    {"ts", "lane", "door", "by", "chars", "sha256"}

and the load meter matches a row to the typed turn that follows it within a
few seconds, counting that turn as injected rather than typed.

What the ledger deliberately is not:

- NEVER THE TEXT. Only its length and sha256 (UTF-8). The pane carries
  secrets — a token pasted by hand, a `cmd` line with a credential — and this
  file sits in `~/.mcp-hub`, so it must be structurally unable to hold one.
  The CLI takes the text on STDIN for the same reason: argv is readable in
  `ps` by everything on the box.
- NOT A GATE. `record()` never raises and never blocks a send. A lane left
  un-nudged because its bookkeeping file was read-only would be an outage
  caused by a measurement.
- NOT TRUSTED FOR `by`. Whether a row is Tim's hand (a cockpit button) or a
  timer reaching for a pane is decided by the door, not by the caller.
- APPEND-ONLY. Two doors write at once (squad nudging one lane while the edge
  prompts another); a read-rewrite-replace per append would lose whichever
  row lost the race. Only a prune — a row genuinely past RETENTION_DAYS —
  rewrites the file, under a lock.

Records are "\\n"-terminated lines and are split on "\\n" alone, never with
`str.splitlines()`: the writer leaves U+2028/U+2029/U+0085 raw (lane, door
and by are caller strings), and splitlines() would cut such a row in two.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

try:  # POSIX only; on Windows an append is still one write, just unlocked
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

# How long a row is kept. The meter reads at most a daily window plus the
# before/after spans it is asked about; two weeks covers a reading taken a
# week late. Pruned on append, so the file cannot grow without bound.
RETENTION_DAYS = 14

LEDGER_NAME = "injections.jsonl"

# Every door that types into a pane. A door not listed here is refused by
# the CLI: an unknown name would be a row the meter cannot account for.
DOORS = ("nudge", "cmd", "model", "key", "answer", "compaction_type",
         "edge_prompt", "edge_interrupt")

# Tim's own cockpit buttons. Their rows say `by: hand` whatever the caller
# passed: a caller that got it wrong would make the hand/automation split
# unreadable for every later row.
HAND_DOORS = ("cmd", "key", "model", "answer")

# Doors that send a keystroke, not a line: a key name, a menu digit, an
# Escape. The client records no user turn for them, so they can never match
# one — the meter counts them as their own kind instead of flagging them.
TEXTLESS_DOORS = ("key", "answer", "edge_interrupt")

ROW_KEYS = ("ts", "lane", "door", "by", "chars", "sha256")


def state_dir() -> Path:
    """`$MCP_HUB_STATE_DIR`, else `~/.mcp-hub` — the cli's own rule."""
    override = os.environ.get("MCP_HUB_STATE_DIR")
    if override:
        return Path(override)
    return Path(os.path.expanduser("~")) / ".mcp-hub"


def ledger_path() -> Path:
    return state_dir() / LEDGER_NAME


def sha256(text: str) -> str:
    """The hex sha256 of `text` as UTF-8 — the only trace of it the ledger keeps."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _by_for(door: str, by: str = "") -> str:
    """Who typed it, decided by the door: hand doors are Tim's, edge doors
    the edge's, the rest squad's. Only an unknown door falls back to `by`."""
    if door in HAND_DOORS:
        return "hand"
    if door.startswith("edge_"):
        return "edge"
    if door in DOORS:
        return "squad"
    return by or "squad"


def read_rows(path: Path | str | None = None) -> list[dict[str, Any]]:
    """Every readable row, in file order.

    An ABSENT ledger raises (OSError): "there is no ledger" must never read
    as "no door typed" — the caller decides, and the meter's answer is
    `missing`, never 0. A torn or non-JSON line is skipped.
    """
    p = Path(path) if path is not None else ledger_path()
    text = p.read_text(encoding="utf-8", errors="replace")
    rows = []
    for line in text.split("\n"):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _line(row: dict[str, Any]) -> str:
    return json.dumps(row, ensure_ascii=False) + "\n"


def _stale(row: dict[str, Any], cutoff: float) -> bool:
    """Past retention. An undatable row is KEPT: dropping what you cannot
    date is how a ledger loses its oldest evidence first."""
    ts = row.get("ts")
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        return False
    return ts < cutoff


def _locked_append_fd(p: Path) -> int:
    """An O_APPEND descriptor on the CURRENT file at `p`, exclusively locked.

    A prune replaces the file; a writer that waited on the old inode's lock
    would append to a file nobody reads any more, so the inode is checked
    after the lock is taken and the open retried.
    """
    for _ in range(20):
        fd = os.open(p, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        if fcntl is None:
            return fd
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            if os.fstat(fd).st_ino == os.stat(p).st_ino:
                return fd
        except OSError:
            pass
        os.close(fd)
    return os.open(p, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)


def record(lane: str, door: str, text: str, *, by: str = "",
           now: float | None = None, path: Path | str | None = None) -> bool:
    """Append one row for a line about to be typed. Never raises.

    Returns True when the row is on disk, False when it could not be written
    (the send goes ahead either way: the ledger is an instrument, not a gate).
    """
    try:
        ts = time.time() if now is None else float(now)
        row = {"ts": ts, "lane": str(lane), "door": str(door),
               "by": _by_for(str(door), by), "chars": len(text),
               "sha256": sha256(text)}
        p = Path(path) if path is not None else ledger_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        fd = _locked_append_fd(p)
        try:
            cutoff = ts - RETENTION_DAYS * 86400
            existing = read_rows(p) if os.fstat(fd).st_size else []
            if any(_stale(r, cutoff) for r in existing):
                # The one rewrite: under the lock, to a sibling, then replace.
                kept = [r for r in existing if not _stale(r, cutoff)] + [row]
                tmp = p.with_name(f".{p.name}.prune-{os.getpid()}")
                with open(tmp, "w", encoding="utf-8") as fh:
                    fh.write("".join(_line(r) for r in kept))
                os.chmod(tmp, 0o600)
                os.replace(tmp, p)
                return True
            data = _line(row)
            size = os.fstat(fd).st_size
            if size:
                # A torn last line must not swallow this row into itself.
                with open(p, "rb") as fh:
                    fh.seek(size - 1)
                    if fh.read(1) != b"\n":
                        data = "\n" + data
            os.write(fd, data.encode("utf-8"))
            return True
        finally:
            os.close(fd)  # releases the lock
    except Exception:  # noqa: BLE001 — never stop a send over bookkeeping
        return False


# --- the squad roster: a row's lane is an AGENT, the meter's is a FOLDER ----

def roster_path() -> Path:
    """squad's own roster: `$SQUAD_CONF`, else `~/.config/squad/squad.conf`."""
    override = os.environ.get("SQUAD_CONF")
    if override:
        return Path(override)
    return Path(os.path.expanduser("~")) / ".config" / "squad" / "squad.conf"


def transcript_folder(worktree: str) -> str:
    """The folder name the Claude Code client gives a worktree under
    `~/.claude/projects`: `~` expanded, then every character that is not
    [A-Za-z0-9] replaced by `-` (a dot shows as a second dash)."""
    if worktree == "~" or worktree.startswith("~/"):
        worktree = os.environ.get("HOME", os.path.expanduser("~")) + worktree[1:]
    return re.sub(r"[^A-Za-z0-9]", "-", worktree)


def lane_folders(conf: Path | str | None = None) -> dict[str, str]:
    """{agent name: transcript folder}, read with squad's own rules.

    squad's doors write the roster AGENT name and the edge writes the tmux
    session squad named for that agent; the meter keys a lane by its
    transcript folder. The roster row's field 2 (the worktree) joins them.
    `rows` drops comments and blank lines; `field "$a" 2` answers with the
    FIRST row naming the agent. No roster is an empty mapping, not an error:
    a box with no squad still takes a reading.
    """
    p = Path(conf) if conf is not None else roster_path()
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    out: dict[str, str] = {}
    for line in text.splitlines():
        if re.match(r"^[ \t]*(#|$)", line):
            continue
        fields = line.split("|")
        agent = fields[0]
        if agent in out or len(fields) < 2 or not fields[1]:
            continue
        out[agent] = transcript_folder(fields[1])
    return out


# --- the doors' command line ------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    """`python -m mcp_hub.injections --lane L --door D [--by B]`, text on stdin.

    The text is NEVER an argument (argv is public in `ps`). One trailing
    newline is stripped — a heredoc's, not the lane's.
    """
    ap = argparse.ArgumentParser(
        prog="python -m mcp_hub.injections",
        description="Append one injections-ledger row; the typed text is read from stdin.")
    ap.add_argument("--lane", required=True)
    ap.add_argument("--door", required=True)
    ap.add_argument("--by", default="")
    ap.add_argument("--path", default=None,
                    help=f"ledger (default $MCP_HUB_STATE_DIR/{LEDGER_NAME})")
    args = ap.parse_args(argv)
    if args.door not in DOORS:
        print(f"injections: unknown door {args.door!r} (known: {', '.join(DOORS)})",
              file=sys.stderr)
        return 2
    raw = sys.stdin.buffer.read().decode("utf-8", errors="replace")
    if raw.endswith("\n"):
        raw = raw[:-1]
    if not record(args.lane, args.door, raw, by=args.by, path=args.path):
        # Reported, never fatal to the door that called us.
        print("injections: ledger not written", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
