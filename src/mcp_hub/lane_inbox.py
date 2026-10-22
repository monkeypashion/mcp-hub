"""The lane inbox: where an automated door leaves what it refused to type
(v2-fleet r5 F5, j6-no-pane-injection).

An automated door — heal's nudge, the compaction ask — must not type into a
pane a person is using. Refusing to type is only half the rule: the item it
would have typed is not dropped, it is left here, and the lane's own Stop
hook blocks with it at the next turn boundary. That is the one moment the
lane can read something without anybody's input box being in the way.

    $MCP_HUB_STATE_DIR/lane-inbox/<lane>/<time>-<door>-<pid>.txt

One file per item, so a door writing while the hook drains never races a
rewrite, and the hook clears exactly the files it delivered — never the
directory, which would lose an item written between its read and its block.
Filenames start with a zero-padded clock, so a sorted listing IS the
chronology.

squad reaches this as `python -m mcp_hub.lane_inbox put <lane> <door> [text]`
(text from stdin when omitted or "-"), so the sentence the lane reads
(`HEADER`) is defined once, here.
"""
from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

# What the lane is told about every item. Without it a nudge reads as a line
# somebody typed, and the lane answers it as if a person had.
HEADER = ("📥 Left in your lane inbox instead of being typed into your pane "
          "(a person was using it) — nobody typed this:")


def _state_dir() -> Path:
    override = os.environ.get("MCP_HUB_STATE_DIR")
    return Path(override) if override else Path.home() / ".mcp-hub"


def _safe(name: str) -> str:
    return "".join(c if (c.isalnum() or c in "-_.") else "_" for c in name) or "_"


def inbox_dir(lane: str) -> Path:
    return _state_dir() / "lane-inbox" / _safe(lane)


def waiting(lane: str, door: str) -> list[Path]:
    """The items from `door` still waiting for `lane`, matched on the door
    field of <time>-<door>-<pid>[-n].txt exactly — never as a prefix of
    another door's name. [] on any trouble."""
    item = re.compile(rf"^[0-9.]+-{re.escape(_safe(door))}-[0-9]+(?:-[0-9]+)?\.txt$")
    return [p for p, _t in peek(lane) if item.match(p.name)]


def put(lane: str, door: str, text: str, now: float | None = None) -> bool:
    """Leave one item for `lane`. False when it could not be written — the
    caller has still refused to type, and must say the item did not land
    rather than pretend it did.

    A door that already has an item waiting for this lane gets no second
    one (r11 F45): the lane will read the first at its next Stop, and N
    copies of one nudge drained as one block say nothing the first did not.
    That is still True — the item IS waiting; `waiting()` tells them apart."""
    now = time.time() if now is None else now
    try:
        if waiting(lane, door):
            return True
        d = inbox_dir(lane)
        d.mkdir(parents=True, exist_ok=True)
        stem = f"{now:020.6f}-{_safe(door)}-{os.getpid()}"
        target = d / f"{stem}.txt"
        n = 0
        while target.exists():
            n += 1
            target = d / f"{stem}-{n}.txt"
        tmp = d / f".{target.name}.tmp"
        tmp.write_text(f"{HEADER} [{door}]\n{text.rstrip()}\n", encoding="utf-8")
        tmp.replace(target)
        return True
    except Exception:  # noqa: BLE001
        return False


def peek(lane: str) -> list[tuple[Path, str]]:
    """Every item waiting for `lane`, oldest first, as (path, text). Does NOT
    consume: a read-then-crash must not swallow the one copy. [] on any
    trouble."""
    try:
        d = inbox_dir(lane)
        if not d.is_dir():
            return []
        out = []
        for p in sorted(d.iterdir()):
            if p.name.startswith(".") or not p.is_file():
                continue
            try:
                out.append((p, p.read_text(encoding="utf-8", errors="replace")))
            except OSError:
                continue
        return out
    except Exception:  # noqa: BLE001
        return []


def clear(paths: list[Path]) -> None:
    """Remove exactly the items that went out, and nothing else."""
    for p in paths:
        try:
            Path(p).unlink(missing_ok=True)
        except OSError:
            pass


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) < 3 or argv[0] != "put":
        print("usage: python -m mcp_hub.lane_inbox put <lane> <door> [text|-]",
              file=sys.stderr)
        return 2
    lane, door = argv[1], argv[2]
    text = " ".join(argv[3:]) if len(argv) > 3 and argv[3:] != ["-"] else \
        sys.stdin.read()
    if not text.strip():
        print("lane_inbox: nothing to leave", file=sys.stderr)
        return 2
    if waiting(lane, door):
        print(f"lane_inbox: {lane} already has a {door} item waiting — "
              "not leaving a second one", file=sys.stderr)
        return 0
    if not put(lane, door, text):
        print(f"lane_inbox: could not write {lane}'s inbox", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
