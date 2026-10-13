"""The lane inbox — where an AUTOMATED door leaves what it would have typed.

v2-fleet r5 F5 (j6-no-pane-injection). Nothing automated types into a pane
Tim is using. But "refused" must not mean "dropped": the nudge and the
compaction ask are things the lane needs to hear, and the only reason they
were ever typed is that typing was the one channel that reached a lane
nobody was reading.

A lane that has somebody attached to it is reading its own Stop drains, so
there IS another channel. This is it: the door writes a file, and the lane's
next stop hook blocks with it and clears it.

🔴 THE KEYS ARE NOT SENT, AND NOTHING ELSE CHANGES. A refused nudge is not
charged against the nudge budget and arms no relaunch strike (see squad's
heal arm) — the lane was never asked anything, so it cannot have failed to
answer. Charging it would spend a budget on a turn nobody spent.

⚠️ Cleared only AFTER the block is out. The item is deleted on the strength
of a block this process has already decided to print, never on the strength
of having read it — a read-then-crash would otherwise swallow the one copy.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

# One line per item, so a reader can tell a left-for-you note from the lane's
# own queued traffic. The text after it is the door's own copy, verbatim:
# re-wording it here would mean two copies of every nudge to keep in step.
HEADER = ("📥 Left for you rather than typed into your pane (a viewer was "
          "attached and active — nothing was sent to the keyboard):")


def state_dir() -> Path:
    override = os.environ.get("MCP_HUB_STATE_DIR")
    return Path(override) if override else Path.home() / ".mcp-hub"


def _safe(agent: str) -> str:
    return "".join(c if (c.isalnum() or c in "-_") else "-"
                   for c in agent.lower()).strip("-") or "unnamed"


def inbox_dir(agent: str) -> Path:
    return state_dir() / "lane-inbox" / _safe(agent)


def put(agent: str, door: str, text: str, now: float | None = None) -> bool:
    """Leave one item for `agent`. True when it landed.

    Fail-open for the caller: a door that cannot write here has still
    REFUSED to type, which is the half of the rule that protects the pane.
    It says so out loud instead (squad prints the write failure).
    """
    now = time.time() if now is None else now
    try:
        d = inbox_dir(agent)
        d.mkdir(parents=True, exist_ok=True)
        name = f"{now:.3f}-{_safe(door)}.txt"
        (d / name).write_text(f"{HEADER}\n{text}\n", encoding="utf-8")
        return True
    except OSError:
        return False


def peek(agent: str) -> list[tuple[Path, str]]:
    """Every item waiting, oldest first, as (path, text).

    Sorted by NAME, which carries the timestamp: a directory listing's order
    is not a chronology, and two items delivered out of order would read as
    the ask answering the nudge.
    """
    try:
        items = sorted(p for p in inbox_dir(agent).iterdir() if p.is_file())
    except OSError:
        return []
    out = []
    for p in items:
        try:
            out.append((p, p.read_text(encoding="utf-8", errors="replace")))
        except OSError:
            continue
    return out


def clear(paths: list[Path]) -> None:
    """Drop exactly the items that went out in a block — never the directory.

    An item a door wrote between the read and the block is NOT in `paths`
    and survives to the next Stop. Clearing the whole directory would lose
    it, silently, in the one window where losing it is most likely.
    """
    for p in paths:
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass


def main(argv: list[str] | None = None) -> int:
    """`python -m mcp_hub.lane_inbox put --lane L --door D`, text on stdin.

    squad's door reaches this rather than writing the file itself, so the
    HEADER above is defined ONCE. Two copies of a sentence the lane reads is
    two things to keep in step, and the one that drifts is the copy nobody
    is looking at.

    Text on stdin for the same reason the ledger takes it there: argv is
    readable in `ps`, and a compaction ask carries the lane's token count
    while a nudge carries nothing worth publishing either way.
    """
    import argparse
    import sys

    ap = argparse.ArgumentParser(prog="python -m mcp_hub.lane_inbox")
    sub = ap.add_subparsers(dest="verb", required=True)
    put_p = sub.add_parser("put", help="leave one item for a lane")
    put_p.add_argument("--lane", required=True)
    put_p.add_argument("--door", required=True)
    args = ap.parse_args(argv)
    try:
        text = sys.stdin.read()
    except OSError:
        text = ""
    # The door sends a line; the heredoc that carried it here added the
    # newline, and keeping it would put a blank line in the lane's block.
    text = text[:-1] if text.endswith("\n") else text
    if not text.strip():
        print("nothing to leave (empty text)", file=sys.stderr)
        return 1
    return 0 if put(args.lane, args.door, text) else 1


if __name__ == "__main__":  # pragma: no cover — squad's entry point
    raise SystemExit(main())
