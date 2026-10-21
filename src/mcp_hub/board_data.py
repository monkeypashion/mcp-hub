"""Live fleet data for the Squad Board panel, and terminal theme detection.

The board's contract (squad:board): renderers consume `squad board --json`,
never re-scrape panes — duplicated scraping logic is how `who` and `heal`
drifted apart. This module honours that seam and ADDS ONLY what the JSON does
not carry, by reading the same documented caches the text board reads:

    ~/.mcp-hub/board-usage.cache     agent␟today␟last_hour   (output tokens)
    ~/.mcp-hub/board-recap.cache     agent␟epoch␟flag␟text   (newest /recap)
    ~/.mcp-hub/fleet-board.json      daemons' fleet snapshot (bio next:, wakeable)
    ~/.mcp-hub/decisions-open.json   open DECISION cards     (authoritative hands)

Every reader tolerates an absent or malformed file: a cache is an instrument,
and a missing instrument must read as "not reporting", never as a crash — the
panel this feeds is the operator's overview and it must degrade to a plain
settings panel when squad or the caches are not there.
"""
from __future__ import annotations

import json
import os
import re
import select
import subprocess
import time
from pathlib import Path
from typing import Any

US = "\x1f"

# Mirror of squad's WAIT_ON_OP_RE (the 🙋 phrase gate). Read from the
# environment when squad exported it — one pattern, one owner — with the
# squad:260 value as fallback for launches outside a squad shell.
_WAIT_RE_FALLBACK = (
    r"(action is yours|your (direct )?(word|nod|call|decision|window|go[- ]?ahead)"
    r"|waiting (on|for) (you|the operator)|blocked on (you|the operator)"
    r"|awaiting (your|the operator)|need(s|ing)? (your|the operator)"
    r"|WAITING[ -]ON[ -]OPERATOR|\bDECISION\b.{0,16}ASK:)"
)


def _wait_re() -> re.Pattern[str]:
    return re.compile(os.environ.get("WAIT_ON_OP_RE") or _WAIT_RE_FALLBACK, re.I)


def _read_lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _age_h(age: float) -> str:
    age = max(0, int(age))
    if age < 3600:
        return f"{age // 60}m"
    if age < 86400:
        return f"{age // 3600}h"
    return f"{age // 86400}d"


def collect(squad_bin: str, home: Path | None = None,
            now: float | None = None) -> dict[str, Any]:
    """One snapshot of everything the board shows, merged per agent.

    Returns {"agents": {name: {...}}, "counts": {...}, "error": str|None}.
    Per-agent keys: state, hub, model, ctx, waiting_seconds, action, question,
    dirty, unpushed, branch, usage_today, usage_hour, wakeable,
    next {source, age, hand, text} | None.
    """
    home = home or Path.home()
    now = now or time.time()
    cache = home / ".mcp-hub"

    error: str | None = None
    scan: list[dict[str, Any]] = []
    unmanaged: list[dict[str, Any]] = []
    try:
        proc = subprocess.run(
            [squad_bin, "board", "--json"],
            capture_output=True, text=True, timeout=20,
        )
        if proc.returncode == 0:
            doc = json.loads(proc.stdout)
            scan = doc.get("agents", [])
            # Box-wide inventory: tmux sessions outside the roster, every
            # socket. An older squad emits no key — that is "not measured",
            # and it stays an empty list here rather than growing a second
            # meaning; the tree's group simply doesn't appear.
            unmanaged = [
                u for u in doc.get("unmanaged") or []
                if isinstance(u, dict) and u.get("session")
            ]
        else:
            error = (proc.stderr or "squad board failed").strip().splitlines()[-1][:120]
    except FileNotFoundError:
        error = "squad not found — live board data unavailable"
    except subprocess.TimeoutExpired:
        error = "squad board timed out"
    except (ValueError, OSError) as exc:
        error = str(exc)[:120]

    usage: dict[str, tuple[int, int]] = {}
    for line in _read_lines(cache / "board-usage.cache"):
        parts = line.split(US)
        if len(parts) >= 3:
            try:
                usage[parts[0]] = (int(parts[1] or 0), int(parts[2] or 0))
            except ValueError:
                continue

    recaps: dict[str, tuple[int, int, str]] = {}
    for line in _read_lines(cache / "board-recap.cache"):
        parts = line.rstrip("\n").split(US, 3)
        try:
            if len(parts) == 4:
                recaps[parts[0]] = (int(parts[1] or 0), int(parts[2] or 0), parts[3])
            elif len(parts) == 3:  # pre-flag cache row — unflagged, never dropped
                recaps[parts[0]] = (int(parts[1] or 0), 0, parts[2])
        except ValueError:
            continue

    fleet: dict[str, dict[str, Any]] = {}
    snap = _read_json(cache / "fleet-board.json")
    for a in snap.get("agents", []) if isinstance(snap.get("agents"), list) else []:
        if isinstance(a, dict) and a.get("name"):
            fleet[a["name"]] = a

    # Open cards are AUTHORITATIVE hands when the cache is fresh; a stale cache
    # applies no labels — never claim "no card" from an instrument that is not
    # reporting (squad:board, 2026-07-26).
    cards: dict[str, dict[str, Any]] = {}
    dec_fresh = False
    dsnap = _read_json(cache / "decisions-open.json")
    try:
        dec_fresh = now - float(dsnap.get("ts") or 0) < 300
    except (TypeError, ValueError):
        dec_fresh = False
    if dec_fresh:
        for c in dsnap.get("cards", []) if isinstance(dsnap.get("cards"), list) else []:
            if isinstance(c, dict) and c.get("agent"):
                cards[c["agent"]] = c

    wre = _wait_re()
    agents: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for row in scan:
        name = row.get("agent", "")
        if not name:
            continue
        order.append(name)
        utot, uhr = usage.get(name, (0, 0))
        fl = fleet.get(name, {})
        rec: dict[str, Any] = {
            **row,
            "usage_today": utot,
            "usage_hour": uhr,
            "wakeable": bool(fl.get("wakeable", True)),
            "next": None,
        }
        # Precedence mirrors board_next_data: hub card > bio next: > recap.
        card = cards.get(name)
        if card:
            txt = re.sub(r"[\x00-\x1f\x7f]", " ", str(card.get("raw") or ""))
            txt = " ".join(txt.split())[:480]
            age = max(0.0, now - float(card.get("submitted_at") or now))
            rec["next"] = {"source": "hub", "age": _age_h(age), "hand": True,
                           "text": txt, "ask": str(card.get("ask") or ""),
                           "net": card.get("net_score")}
        elif fl.get("next"):
            hand = bool(wre.search(fl["next"])) and not dec_fresh
            rec["next"] = {"source": "bio", "age": "", "hand": hand,
                           "text": str(fl["next"])[:300 if hand else 120]}
        elif name in recaps:
            ep, flag, txt = recaps[name]
            # a recap hand counts only while the agent is actually idle — a
            # working agent has moved past its own ask
            hand = bool(flag) and not dec_fresh and row.get("state") == "idle"
            m = re.search(r"(?:(?<=[.!?] )|^)((?:Next|Waiting|Blocked|DECISION)\b.*)", txt)
            if m:
                txt = m.group(1)
            rec["next"] = {"source": "recap", "age": _age_h(now - ep), "hand": hand,
                           "text": txt[:300 if hand else 120]}
        agents[name] = rec

    counts = {
        "waiting": sum(1 for a in agents.values() if a.get("state") == "waiting"),
        "working": sum(1 for a in agents.values() if a.get("state") == "working"),
        "idle": sum(1 for a in agents.values() if a.get("state") == "idle"),
        "down": sum(1 for a in agents.values() if a.get("state") == "down"),
        "hands": sum(1 for a in agents.values()
                     if (a.get("next") or {}).get("hand")),
    }
    return {"agents": agents, "order": order, "counts": counts,
            "unmanaged": unmanaged, "error": error}


# ---- terminal theme detection ----------------------------------------------
#
# The panel runs in VSCode terminals on several devices, some light, some
# dark, and a white panel on a dark terminal is exactly the mismatch the
# operator flagged. Nothing in the environment states the theme, but the
# terminal itself will: OSC 11 asks for the background colour and xterm.js
# (VSCode), tmux (passthrough) and every serious emulator answer it.

def _luminance(spec: str) -> float | None:
    """rgb:RRRR/GGGG/BBBB (any 1-4 hex digits per channel) -> 0.0..1.0."""
    m = re.search(r"rgb:([0-9a-fA-F]{1,4})/([0-9a-fA-F]{1,4})/([0-9a-fA-F]{1,4})", spec)
    if not m:
        return None
    chans = [int(c, 16) / (16 ** len(c) - 1) for c in m.groups()]
    r, g, b = chans
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _query_osc11(timeout: float = 0.4) -> str | None:
    """Ask the controlling terminal its background colour. None if it won't say."""
    import termios
    import tty
    try:
        fd = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)
    except OSError:
        return None
    try:
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            os.write(fd, b"\x1b]11;?\x1b\\")
            buf = b""
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                r, _, _ = select.select([fd], [], [], deadline - time.monotonic())
                if not r:
                    break
                buf += os.read(fd, 256)
                # reply ends ST (ESC \) or BEL
                if b"\x07" in buf or b"\x1b\\" in buf[2:]:
                    break
            return buf.decode("ascii", "replace") if b"rgb:" in buf else None
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
    except (OSError, ValueError):
        return None
    finally:
        os.close(fd)


def terminal_prefers_dark(env: dict[str, str] | None = None,
                          query=_query_osc11) -> bool | None:
    """True dark, False light, None unknown (caller picks its default).

    Order: explicit SQUAD_THEME (the escape hatch and the test seam) →
    OSC 11 (the terminal's own answer — the only source that tracks a VSCode
    theme change) → COLORFGBG (rxvt convention, often stale, last resort).
    """
    env = env if env is not None else dict(os.environ)
    forced = (env.get("SQUAD_THEME") or "").strip().lower()
    if forced in {"dark", "light"}:
        return forced == "dark"
    reply = query()
    if reply:
        lum = _luminance(reply)
        if lum is not None:
            return lum < 0.5
    fgbg = env.get("COLORFGBG", "")
    if ";" in fgbg:
        bg = fgbg.rsplit(";", 1)[-1].strip()
        if bg.isdigit():
            return int(bg) < 7 or int(bg) == 8
    return None
