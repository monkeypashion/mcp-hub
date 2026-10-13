"""The injections ledger — every automated keystroke, recorded before it lands.

v2-fleet r5 F4 (j6-honest-counts). The load meter's `typed_turns` has always
been an UPPER BOUND, and its own source string said so: a `squad send-keys`
injection lands in a lane's transcript in exactly the same shape as a line
Tim typed — same `type: "user"`, same `origin.kind: "human"`. So every count
of "what Tim typed" silently included the fleet's own nudges and asks, and
the instrument was measuring itself.

An instrument that cannot tell its own events apart from the ones it is
measuring does not read high by a known margin; it reads WRONG in the one
direction nobody checks, because the fleet's own traffic rises exactly when
the fleet is busiest.

This is the missing fact: one row per typed-in line, written by the door that
types it, BEFORE the first keystroke.

🔴 THE TEXT IS NEVER STORED. Only its length and its sha256. A lane's pane
carries secrets (tokens pasted by hand, a `cmd` line with a credential in
it), and this file is world-readable state in `~/.mcp-hub`. The sha256 is
enough to MATCH a transcript turn, which is the whole job; it is not enough
to read one back. Same rule as a brief: never put a secret where the ledger
can hold it, and here the ledger structurally cannot.

⚠️ A FAILED WRITE NEVER STOPS THE SEND. The ledger is an instrument, not a
gate. A nudge that does not reach a deaf lane because its bookkeeping file
was read-only would be an outage caused by a measurement.

Rows older than `RETENTION_DAYS` are pruned when a door appends — the same
place the file is already being written, so the pruning cannot drift out of
step with the growth.
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

RETENTION_DAYS = 14
RETENTION_SECONDS = RETENTION_DAYS * 86400

# How close a ledger row must sit before a transcript turn for the two to be
# the same event. A door writes its row, sends the literal, sleeps ~0.2 s and
# sends Enter; the client then stamps the turn. 5 s covers a wedged pane and
# a slow write without reaching the NEXT turn, which on a live lane is
# minutes away. Read by the load meter, defined here with the writer.
MATCH_WINDOW_SECONDS = 5.0

# Every door that types. Named here so the meter and the ledger cannot
# disagree about what a door is, and so a new one is a one-line change in
# one file rather than a string nobody validates.
DOORS = ("nudge", "cmd", "model", "key", "answer", "compaction_type",
         "edge_prompt", "edge_interrupt")

# The verbs Tim's cockpit buttons call (mcp-hub's Term 1). They are never
# refused for a pane in use — his own hand is the thing the refusal protects
# the pane FROM — but they are still recorded, because the load meter must
# leave out a turn that squad typed whoever pressed the button.
HAND_DOORS = ("cmd", "model", "key", "answer")

# The doors that type no LINE: a key name, a menu digit, an Escape. They are
# keystrokes, not prompts, so the client records no user turn for them and
# there is nothing for a sha256 to agree with — ever.
#
# ⭐ THEY ARE THEIR OWN KIND, NOT AN UNMATCHED ROW. The meter's
# `injection_unmatched` flag exists to say "a door typed and the keystroke
# did not land, or this ledger belongs to another box's transcripts". An
# Escape row carries neither fault, so flagging it would fill the flag with
# rows that can never match and make the one case it is FOR unreadable.
TEXTLESS_DOORS = ("key", "answer", "edge_interrupt")


def roster_path() -> Path:
    """`$SQUAD_CONF`, default `~/.config/squad/squad.conf` — squad's own."""
    override = os.environ.get("SQUAD_CONF")
    return Path(override) if override else Path.home() / ".config" / "squad" / "squad.conf"


def project_folder(worktree: str) -> str:
    """A worktree path as the Claude Code client names its transcript folder.

    `~` first (squad's `untilde`: only a LEADING tilde, against $HOME), then
    every character that is not `[A-Za-z0-9]` replaced by `-`. So
    `/home/monke/.mcp-hub/hooks-clone` ->
    `-home-monke--mcp-hub-hooks-clone`: the doubled `-` is the dot, which is
    why `tr '/' '-'` is not the rule — a lane whose worktree holds a dot
    would map to a folder that does not exist and read as never injected.
    """
    wt = worktree.strip()
    if wt.startswith("~"):
        wt = str(Path.home()) + wt[1:]
    return re.sub(r"[^A-Za-z0-9]", "-", wt)


def lane_folders(path: Path | None = None) -> dict[str, str]:
    """`{roster agent name: transcript folder}` — the ledger's lane mapping.

    ⭐ WHY THE ROSTER. A ledger `lane` is the name the DOOR knows: squad
    writes the agent name (`ledger_note "$a" ...`), the edge writes the tmux
    session name — and squad names each lane's session for its roster agent,
    so those are the same string. The load meter keys a lane by its
    TRANSCRIPT FOLDER, which the client derives from the worktree. Field 2 of
    the roster row is that worktree, so the roster is the one local file that
    joins the two, with no hub, no tmux and no live process to ask.

    Read with squad's own rule (`rows`: comments and blank lines are not
    rows; `field "$a" 2`: the FIRST row whose first field is the name). An
    unreadable roster is an empty mapping, never an error: a reading that
    refused without one would be unobtainable on a box that has no squad, and
    a row whose `lane` already IS a folder name still matches directly.
    """
    p = Path(path) if path else roster_path()
    out: dict[str, str] = {}
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = line.split("|")
        if len(parts) < 2:
            continue
        agent, worktree = parts[0].strip(), parts[1].strip()
        if not agent or not worktree or agent in out:
            continue   # first row wins, exactly as `field` resolves it
        out[agent] = project_folder(worktree)
    return out


def ledger_path() -> Path:
    """`$MCP_HUB_STATE_DIR/injections.jsonl`, default `~/.mcp-hub`."""
    override = os.environ.get("MCP_HUB_STATE_DIR")
    base = Path(override) if override else Path.home() / ".mcp-hub"
    return base / "injections.jsonl"


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _by_for(door: str, by: str) -> str:
    """`hand` for a cockpit button, the door's own actor otherwise.

    Forced rather than trusted: `by` is what distinguishes Tim pressing a
    button from a timer reaching for a pane, and a caller that passed the
    wrong one would make the distinction unreadable for every later row.
    """
    if door in HAND_DOORS:
        return "hand"
    return by or "squad"


def prune(rows: list[dict], now: float) -> list[dict]:
    """Rows inside the retention window, in order. A row with no readable
    `ts` is KEPT: it cannot be shown to be old, and dropping what you cannot
    date is how a ledger quietly loses its oldest evidence."""
    out = []
    for row in rows:
        try:
            ts = float(row.get("ts"))
        except (TypeError, ValueError):
            out.append(row)
            continue
        if now - ts <= RETENTION_SECONDS:
            out.append(row)
    return out


def read_rows(path: Path | None = None) -> list[dict]:
    """Every row, oldest first. Raises OSError when the ledger is unreadable —
    the caller decides whether that is `missing` or nothing at all, and this
    must never resolve an absent ledger to "no injections happened"."""
    p = Path(path) if path else ledger_path()
    rows = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue  # a torn line is not a row
        if isinstance(rec, dict):
            rows.append(rec)
    return rows


def _is_stale(path: Path, now: float) -> bool:
    """True when the ledger holds a row past the retention window.

    Read from the FRONT and stop at the first row inside the window: the file
    is append-ordered, so the only rows that can be old are its oldest, and
    on a ledger with nothing to prune this reads one line, not the day.
    """
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    ts = float(rec.get("ts"))
                except (ValueError, TypeError, AttributeError):
                    continue   # a torn line cannot be shown to be old
                return now - ts > RETENTION_SECONDS
    except OSError:
        return False
    return False


def record(lane: str, door: str, text: str, by: str = "squad",
           now: float | None = None, path: Path | None = None) -> bool:
    """Append one row for a send that is ABOUT TO HAPPEN. True when it landed.

    Called before the first keystroke, always. The ordering is the whole
    guarantee: a row written afterwards is absent for exactly the window in
    which the send can crash, and the turn it names then counts as typed.

    ⭐ IT APPENDS. One `O_APPEND` write of one line, to the same inode, so
    two doors writing at the same instant both keep their row. The first
    version read the file, rewrote it whole and replaced it: squad nudging
    one lane while the edge prompted another silently lost whichever row lost
    the race — a ledger that drops rows under load reports the fleet's own
    traffic as Tim's, which is the failure this feature exists to remove.
    The file is rewritten ONLY to prune, and only when something in it is
    actually past `RETENTION_DAYS`.
    """
    now = time.time() if now is None else now
    p = Path(path) if path else ledger_path()
    row = {
        "ts": round(float(now), 3),
        "lane": lane,
        "door": door,
        "by": _by_for(door, by),
        "chars": len(text),
        "sha256": sha256(text),
    }
    line = json.dumps(row, ensure_ascii=False) + "\n"
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        if _is_stale(p, now):
            # The one case that rewrites. Everything kept is written with the
            # new row in a single file, then moved into place, so a reader
            # never sees a ledger mid-prune.
            try:
                existing = read_rows(p)
            except OSError:
                existing = []
            kept = prune(existing, now)
            kept.append(row)
            tmp = p.with_suffix(".jsonl.tmp")
            tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n"
                                   for r in kept), encoding="utf-8")
            tmp.replace(p)
            return True
        with p.open("a", encoding="utf-8") as fh:
            fh.write(line)
        return True
    except OSError:
        return False


def main(argv: list[str] | None = None) -> int:
    """`python -m mcp_hub.injections --lane L --door D [--by B]`, text on stdin.

    ⭐ THE TEXT ARRIVES ON STDIN, never in argv. argv is readable in `ps` by
    anything on the box, and the one thing this module promises is that the
    typed text is not left lying around. Passing it as an argument would
    publish, to every process on the machine, exactly what the file refuses
    to store.
    """
    ap = argparse.ArgumentParser(
        prog="python -m mcp_hub.injections",
        description="Record one typed-in line in the injections ledger")
    ap.add_argument("--lane", required=True)
    ap.add_argument("--door", required=True, choices=list(DOORS))
    ap.add_argument("--by", default="squad")
    ap.add_argument("--path", default=None,
                    help="ledger to append to (default: $MCP_HUB_STATE_DIR/injections.jsonl)")
    args = ap.parse_args(argv)
    try:
        text = sys.stdin.read()
    except OSError:
        text = ""
    # The doors send a line, not a file: a trailing newline from the heredoc
    # that carried it here is the shell's, not the lane's, and counting it
    # would make every `chars` one too many and every sha256 unmatchable.
    text = text[:-1] if text.endswith("\n") else text
    record(args.lane, args.door, text, by=args.by,
           path=Path(args.path) if args.path else None)
    # ALWAYS 0. The caller is about to type; a non-zero exit here would, under
    # `set -e`, turn a bookkeeping failure into a lane that never got nudged.
    return 0


if __name__ == "__main__":  # pragma: no cover — the door's entry point
    raise SystemExit(main())
