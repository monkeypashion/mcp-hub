"""Hub CLI — utility commands for agents.

Currently provides one subcommand:

    mcp-hub stop-hook --name=<agent> [--project=<proj>] [--hub-url=<url>]

Designed to be wired into an agent's `~/.claude/settings.json` Stop hook so
queued hub messages surface automatically at every turn boundary, plus a
re-register call if the agent has drifted off the wake path (e.g., after a
hub redeploy wiped the in-memory session registry).

The hook protocol contract:
    - Hook process exits 0
    - If we want Claude to take an extra turn to process content, write JSON
      to stdout: `{"decision": "block", "reason": "<text>"}`. Claude treats
      `reason` as a new prompt and continues.
    - If we want Stop to proceed normally, write nothing.

Fail-open philosophy: any hub error (unreachable, timeout, malformed
response) MUST result in writing nothing and exiting 0. The hook should
NEVER block an agent's Stop because of hub flakiness.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime as _dt
import hashlib
import json
import os
import pathlib
import platform
import re
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import replace
from typing import Any

# Fallback is the Tailscale-only prod endpoint — the public FQDN was
# deliberately cut 2026-05-29 (a domain 404 is correct, not an outage), but
# this default still pointed at it, so a fresh machine without MCP_HUB_URL
# would aim at a dead endpoint. Every fleet machine is on the tailnet.
#
# Kept as a separate literal because DEFAULT_HUB_URL folds the env var in at
# IMPORT time, which makes it useless for saying where a URL came from: unset
# MCP_HUB_URL after import and DEFAULT_HUB_URL still holds the env value. The
# settings panel reports provenance, so it needs the un-overridden default to
# compare against.
# The one voice port, every container (docs/seat-voice.md). Imported lazily in
# the command itself; named here so the parser help can state it.
_VOICE_PORT = 6981

BUILTIN_HUB_URL = "http://100.109.6.114:8090/mcp"
DEFAULT_HUB_URL = os.environ.get("MCP_HUB_URL", BUILTIN_HUB_URL)

# Windows: a console-subsystem child (git, tasklist, python) launched from a
# window-less parent ALLOCATES A NEW VISIBLE CONSOLE. Our hooks run at every
# turn boundary and shell out (git for identity derivation, tasklist for the
# daemon singleton, python -m for the daemon spawn) — without this flag the
# operator's desktop flashes console windows on every Stop (observed live on
# fireblade 2026-07-18). No-op on POSIX.
_NO_WINDOW_FLAG = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

# Marker file each project uses to declare its agent identity to the hub. Lets
# a single global Stop hook (in ~/.claude/settings.json) work across the whole
# fleet — the cli reads cwd from the hook's stdin payload, looks here, and
# uses the values it finds. Projects without this file silently no-op.
AGENT_MARKER_PATH = pathlib.Path(".claude") / "hub-agent.json"

# Seat supervisor: at most one re-register nudge per this many seconds. A
# nudge needs a turn to run and a further daemon cycle (60s) to show up in
# the status cache, so anything faster types into the pane while the last
# one is still in flight.
SUPERVISOR_COOLDOWN = 180.0


# ---------------------------------------------------------------------------
# Hub interaction (thin wrapper over the MCP client)
# ---------------------------------------------------------------------------


# -- DECISION card harvesting (stop-hook leg of the triage machinery) --------
#
# The agent authors the card as the END of its waiting turn (hub-instructions
# convention); this hook is the courier that never forgets: card in last
# turn -> decision_put (hand up fleet-wide within seconds); no card in last
# turn -> decision_clear (an answered/moved-on ask self-withdraws). Only the
# model can AUTHOR a card; everything after authorship is machinery.

_DECISION_BLOCK_RE = re.compile(r"\*{0,2}DECISION\*{0,2}\b.*\Z", re.S)
_WAITING_ON_OP_RE = re.compile(
    r"(waiting (on|for) (you|the operator)|blocked on (you|the operator)"
    r"|awaiting (your|the operator)|need(s|ing)? (your|the operator)"
    r"|your (word|nod|call|decision|go[- ]?ahead)|action is yours)",
    re.I,
)

# The waiting-language check is a pattern over prose, and prose fights back
# (item 30, 2026-07-27): "Nothing waiting on you" contains "waiting on you",
# and a turn that QUOTES the trigger phrase — a bug report about this very
# nag — reads identically to a turn that uses it. A regex cannot see context,
# so the two carve-outs below remove the contexts where the phrase provably
# is NOT an ask: mentioned text (quotes / code spans) and negated fragments.
_MENTION_SPAN_RE = re.compile(
    r"```.*?```"            # fenced code block
    r"|`[^`\n]*`"           # inline code
    r'|"[^"\n]{1,160}"'     # straight-quoted span
    r"|“[^”\n]{1,160}”",    # curly-quoted span
    re.S,
)
# A negation earlier in the SAME fragment flips the match's meaning. The
# fragment ends at any clause punctuation: "nothing is blocked, but I'm
# waiting on your call" must still read as an ask.
_NEGATED_BEFORE_RE = re.compile(
    r"\b(?:nothing|no|not|none|nor|never|nobody|without|isn'?t|aren'?t)\b"
    r"[^.!?,;:\n]{0,40}$",
    re.I,
)


def _closing_section(turn_text: str) -> str:
    """The turn's last two non-empty paragraph blocks — where the DECISION
    convention puts asks. Two blocks, not one, so a sign-off line after the
    ask doesn't hide it."""
    blocks = [b for b in turn_text.split("\n\n") if b.strip()]
    return "\n\n".join(blocks[-2:])


def _waiting_analysis(turn_text: str) -> tuple[bool, str, str]:
    """(genuine, reason, phrase) for the waiting-language check.

    reason: 'no_match' (regex never fired — the uninteresting bulk),
    'match' (genuine ask language), or which carve-out suppressed it
    ('suppressed_body_only' / 'suppressed_mention' /
    'suppressed_negation'). The split exists for telemetry: the item-30
    fix shipped on anecdotes ("it nagged fo four times"), and whether the
    carve-outs actually cut the false-positive rate should be a query over
    a log, not another anecdote.

    Only the CLOSING SECTION is eligible to fire the nag. The convention
    puts asks at the end of the turn, and the body is where the fourth
    false-positive mechanism lives (dt, 2026-07-27): retrospective
    self-reference — "that's indistinguishable from me still waiting on
    you" describing an already-resolved block, in a turn whose closing
    line said all-clear. A body mention is telemetry, not a nag."""
    raw = _WAITING_ON_OP_RE.search(turn_text)
    if not raw:
        return False, "no_match", ""
    closing = _closing_section(turn_text)
    if not _WAITING_ON_OP_RE.search(closing):
        return False, "suppressed_body_only", raw.group(0)
    prose = _MENTION_SPAN_RE.sub(" ", closing)
    negated = None
    for m in _WAITING_ON_OP_RE.finditer(prose):
        if _NEGATED_BEFORE_RE.search(prose, 0, m.start()):
            negated = m
            continue
        return True, "match", m.group(0)
    if negated is not None:
        return False, "suppressed_negation", negated.group(0)
    return False, "suppressed_mention", raw.group(0)


def _reads_as_waiting_on_operator(turn_text: str) -> bool:
    """True when the turn genuinely reads as waiting on the operator —
    _WAITING_ON_OP_RE minus the two false-positive contexts confirmed in
    the field (item 30): use/mention, and negation."""
    return _waiting_analysis(turn_text)[0]


def _log_nag_event(agent_name: str, outcome: str, phrase: str) -> None:
    """Append one telemetry record for a raw-match turn. Fail-open: the
    log must never block a Stop.

    Only turns where the bare regex fired are logged (tens per day per
    box, so no rotation), and the outcome says how the turn resolved:
    card_filed / decided (authoring worked), nagged (delivered),
    suppressed_negation / suppressed_mention (carve-out held it),
    suppressed_grace (declined last Stop, not re-pressured). The
    false-positive rate of the nag is a query over this file."""
    try:
        path = _state_dir() / "card-nag-log.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": round(time.time(), 1),
                "agent": agent_name,
                "outcome": outcome,
                "phrase": phrase,
            }, ensure_ascii=False) + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# ACTIVITY RECORD — bar 47's second instrument (g#24)
# ---------------------------------------------------------------------------
#
# ⭐⭐ WHY THIS EXISTS: bar 47 asks whether stop-hook drains and heartbeats cost
# ZERO model turns, measured as "a lane with nothing to do shows zero turns for
# that hour". Measured 2026-09-04, the transcript CANNOT answer that: a drain
# that surfaces nothing writes NOTHING — the hook's output reaches the
# transcript only as part of a turn. All 323 zero-turn hours found in 139
# transcripts were therefore VACUOUS: they satisfy the wording while proving
# nothing, because "drains are free" and "no drain happened" look identical.
# A schema check confirmed no hub table records a drain either (wake_log is
# wakes, delivery_receipts are renders).
#
# ⇒ So the counter has to be written where the drain HAPPENS, by the process
# that performs it, whether or not anything surfaces. A counter that cannot
# register success cannot report failure.
#
# ⚠️ THIS RECORDS THE ACT, NEVER THE COST. It says a drain occurred at a time,
# and whether it surfaced anything; the turn count still comes from the
# transcript. Closing bar 47 is the CROSS-REFERENCE of the two — hours where
# this log shows activity AND the transcript shows zero turns — and neither
# file can close it alone. Nothing here should ever be read as "drains are
# free"; it is the half of the measurement that was missing.
def _log_activity_event(agent_name: str, kind: str, **fields: object) -> None:
    """Append one activity record. Fail-open: it must never block a Stop.

    `kind` is "drain" (one per stop-hook invocation) or "beat" (one per agent
    per UTC hour — see `_heartbeat_loop`; a per-beat line would be 1440/day
    per lane and the measurement is hourly anyway). Volume is otherwise the
    same order as `card-nag-log.jsonl`, so it is unrotated for the same
    reason.
    """
    try:
        path = _state_dir() / "activity-log.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {"ts": round(time.time(), 1), "agent": agent_name, "kind": kind}
        record.update(fields)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _log_beat_if_new_hour(agent_name: str, last_hour: str) -> str:
    """Record that heartbeats were alive in this UTC hour; return the bucket.

    Pure decision, extracted from `_heartbeat_loop` so the once-per-hour rule
    is pinned by a test rather than asserted in a comment — the loop itself
    needs a live MCP session to reach.
    """
    hour_now = time.strftime("%Y-%m-%dT%H", time.gmtime())
    if hour_now != last_hour:
        _log_activity_event(agent_name, "beat", hour=hour_now)
    return hour_now


def _card_nag_grace(agent_name: str, would_nag: bool) -> bool:
    """One nag, then one turn of grace. Returns whether the nag may fire.

    A nag re-delivered at the very next Stop reads as "file a card to make
    it stop", which manufactures exactly the hollow cards the convention
    bans (fo's report, item 30) — the agent already saw the nag and chose
    prose; repeating it within the same episode changes nothing but the
    pressure. The flag file remembers "nagged last Stop"; a Stop that finds
    it suppresses the repeat and clears it, and any nag-free Stop (card
    filed, ask answered, phrasing gone) also clears it, so a NEW episode
    two turns later nags again. Fail-open toward nagging: state trouble
    must never silence the courier entirely."""
    flag = _state_dir() / f"card-nag.{agent_name}"
    try:
        if not would_nag:
            flag.unlink(missing_ok=True)
            return False
        if flag.exists():
            flag.unlink(missing_ok=True)
            return False
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.write_text("1", encoding="utf-8")
    except OSError:
        pass
    return would_nag


def _read_last_assistant_text(transcript_path: str | None) -> str:
    """Text of the LAST assistant message in the transcript, '' on any
    problem. Reads only the file tail — transcripts grow to tens of MB and
    this runs at every Stop."""
    if not transcript_path:
        return ""
    try:
        with open(transcript_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 262144))
            tail = f.read().decode("utf-8", errors="ignore")
    except OSError:
        return ""
    for line in reversed(tail.splitlines()):
        if '"assistant"' not in line or '"text"' not in line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue  # first line of the tail window may be cut mid-record
        if rec.get("type") != "assistant":
            continue
        content = (rec.get("message") or {}).get("content") or []
        texts = [b.get("text", "") for b in content
                 if isinstance(b, dict) and b.get("type") == "text"]
        text = "\n".join(t for t in texts if t)
        if text.strip():
            return text
    return ""


# 🔴 THE MARKER MUST OWN ITS LINE (card #943, 2026-09-04).
#
# The first form of this pattern had no line anchor, so it matched the token
# wherever it appeared — including quoted mid-sentence, inside backticks, and
# inside a NEGATION. It closed cards with whatever followed the colon, so a
# lane writing "I have emitted no `DECIDED:` line." closed its own card with
# the verdict "` line.". That fabricated 15 in-pane operator verdicts across
# 7 lanes between 2026-07-27 and 2026-09-04 — 39 days — and every one of them
# was harvested from a sentence whose whole point was that no verdict was
# being recorded. It hid that long because the corrupted row was never in
# anybody's authorisation chain: every lane keyed authorisation to the
# console or the pane, never to hub card state.
#
# Anchoring alone is not enough, because a line may legitimately BEGIN with
# the token while discussing it. So three independent gates stand here, and
# EVERY ONE FAILS TOWARD NOT CLOSING — the cost of missing a real verdict is
# that the agent restates it; the cost of inventing one is a forged consent
# record that reads exactly like a real one.
# Three exact forms, every one of them BALANCED. The loose `\*{0,2}` form
# accepted `DECIDED:** and stop` — consuming an unbalanced bold-close as if
# it were the marker's own, and handing back the remainder as a verdict.
# Unbalanced emphasis around the token is a tell that the line was torn out
# of something, so it is refused rather than repaired.
_DECIDED_RE = re.compile(
    r"^[ \t]{0,3}(?:\*\*DECIDED:\*\*|\*\*DECIDED\*\*:|DECIDED:)"
    r"[ \t]*(.+?)[ \t]*$"
)

# The scrape signature. A verdict harvested from mid-sentence begins with the
# punctuation that closed the quotation it was torn out of — "` line.",
# "** and nothing else", "' to the card". A real verdict starts with a word.
_FRAGMENT_START_RE = re.compile(r"^[\s`'\"*)\]}>,;:.!?\u2018\u2019\u201c\u201d]")


def _extract_decided(turn_text: str) -> str:
    """The `**DECIDED:** <verdict>` closing marker, '' if none.

    The agent that received an in-pane answer records the verdict itself (it
    just understood and acted on it) — machinery only ships; last one wins.

    Returns the verdict with a RECEIPT appended naming the mechanism and
    quoting the line it was read from. A hook-made close and a hand-made one
    used to be the same row, which is why a fabricated verdict was
    indistinguishable from a real one for 39 days; the receipt makes every
    automatic close falsifiable from the record alone, without needing the
    transcript it was scraped from.
    """
    text = (turn_text or "").rstrip()
    if not text:
        return ""

    lines = text.splitlines()
    # Gate 1: the marker is a CLOSING marker — the convention, and the hook's
    # own prompt, put it at the end of the turn. A match anywhere else is
    # prose about the marker, not a verdict. This is the gate that would have
    # stopped all fifteen on its own.
    last = ""
    last_idx = -1
    for idx in range(len(lines) - 1, -1, -1):
        if lines[idx].strip():
            last, last_idx = lines[idx], idx
            break
    if last_idx < 0:
        return ""

    match = _DECIDED_RE.match(last)
    if not match:
        return ""

    # Gate 2: not inside a fenced code block — a fence shows the marker as an
    # EXAMPLE, which is the most natural way to write about it.
    if sum(1 for ln in lines[:last_idx] if ln.lstrip().startswith("```")) % 2:
        return ""

    verdict = match.group(1).strip()
    # Gate 3: a verdict is a verdict, not the back half of a sentence about
    # one. Leading punctuation is the tell, and a candidate with no letter in
    # it says nothing a reader could act on.
    if not verdict or _FRAGMENT_START_RE.match(verdict):
        return ""
    if not any(ch.isalnum() for ch in verdict):
        return ""

    return f"{verdict}\n[auto-closed by stop-hook · read from: {last.strip()!r}]"


def _extract_decision_card(turn_text: str) -> str:
    """The DECISION card from a turn's text, '' if none. The convention puts
    the card at the END of the turn, so we take from the last DECISION token
    to end-of-text; a card without an ASK: field is not a card."""
    matches = list(_DECISION_BLOCK_RE.finditer(turn_text))
    if not matches:
        return ""
    card = matches[-1].group(0).strip()
    if "ASK:" not in card:
        return ""
    # cap before shipping — a convention-breaking turn (card then ramble)
    # must not put a novel on the wire; the hub caps again defensively
    return card[:4096]


async def _query_hub(
    hub_url: str, agent_name: str, project: str = "", card: str = "",
    decided: str = "", rendered_refs: str = "",
) -> tuple[str, str, bool]:
    """Connect to the hub, return (dm_text, broadcast_text, is_online).

    - `dm_text` is the rendered output of `get_messages` (empty if no unread).
    - `broadcast_text` is the rendered output of `get_broadcasts_for_agent`,
      which atomically returns broadcasts since the agent's per-agent cursor
      and advances the cursor (so subsequent calls don't re-deliver). Empty
      string if no unseen broadcasts.
    - `is_online` is True when the agent's name appears in `list_agents`
      (status='online' / 🟢). Deliberately NOT keyed on the ⚡ marker — see
      the inline note below on why ⚡ is the wrong signal for the rebind nag.

    On any error, raises — the caller is responsible for fail-open handling.
    """
    # Lazy import so missing-deps doesn't break --help / arg parsing
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async with streamablehttp_client(_ephemeral_hub_url(hub_url), timeout=10) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # bind=False on the calls below: this client is the Stop hook's
            # ephemeral streamablehttp_client — its session_id is DELETEd
            # when the `async with` block exits. Letting the hub auto-bind
            # the agent's name to this short-lived session would clobber
            # the agent's real (long-lived) wake target. The hub's
            # touch_session honours bind=False and skips the binding.
            #
            # mark_idle=True: Stop hook fires at end of turn, which is the
            # idle transition for the agent. The hub uses this flag for the
            # Case 1 wake-on-low-prio path — a peer's low-prio DM to an
            # idle recipient fires a wake (drain-batched) instead of just
            # queuing.
            # compact=True: summarise anything the agent already saw live and
            # cap the bulk (see get_messages). Retried without the flag so a
            # newer CLI still works against a hub that predates it — during a
            # deploy the two versions coexist for a few minutes, and a hard
            # failure there would silently stop surfacing messages entirely.
            # rendered_refs is the delivery-receipt report (card #56): the
            # message ids this agent's own transcript proves rendered, or
            # "none" for an explicit empty report. It rides both drain calls
            # so either alone lands the record. The version-skew fallbacks
            # peel arguments newest-first — a deploy briefly pairs a newer
            # CLI with an older hub, and a hard failure here would silently
            # stop surfacing messages entirely.
            msg_args = {
                "agent_name": agent_name,
                "bind": False,
                "mark_idle": True,
                "compact": True,
            }
            if rendered_refs:
                msg_args["rendered_refs"] = rendered_refs
            try:
                messages_result = await session.call_tool("get_messages", msg_args)
            except Exception:  # noqa: BLE001
                try:
                    msg_args.pop("rendered_refs", None)
                    messages_result = await session.call_tool(
                        "get_messages", msg_args
                    )
                except Exception:  # noqa: BLE001
                    msg_args.pop("compact")
                    messages_result = await session.call_tool(
                        "get_messages", msg_args
                    )
            # compact=True mirrors the DM economy onto broadcasts (they were
            # the unclipped half of the Stop-hook context tax).
            bc_args = {"agent_name": agent_name, "bind": False, "compact": True}
            if rendered_refs:
                bc_args["rendered_refs"] = rendered_refs
            try:
                broadcasts_result = await session.call_tool(
                    "get_broadcasts_for_agent", bc_args
                )
            except Exception:  # noqa: BLE001
                try:
                    bc_args.pop("rendered_refs", None)
                    broadcasts_result = await session.call_tool(
                        "get_broadcasts_for_agent", bc_args
                    )
                except Exception:  # noqa: BLE001
                    bc_args.pop("compact")
                    broadcasts_result = await session.call_tool(
                        "get_broadcasts_for_agent", bc_args
                    )
            # DECISION card leg. Precedence: a DECIDED marker (the agent
            # recording the in-pane verdict it just received) closes the
            # open card WITH the verdict; else a card in the last turn ->
            # put (upserts); else -> clear (idempotent, verdict-less
            # withdrawal). Fail-soft: an older hub without these tools must
            # not break message surfacing (version skew during deploys).
            card_notice = ""
            try:
                if decided:
                    await session.call_tool(
                        "decision_resolve",
                        {"from_agent": agent_name, "verdict": decided},
                    )
                elif card:
                    await session.call_tool(
                        "decision_put",
                        {"from_agent": agent_name, "card": card,
                         "project": project or ""},
                    )
                else:
                    # The clear response is the owner-notice channel: a
                    # cardless turn against an open card comes back as
                    # "card #N kept open (n/3)" / "marked STALE" — surfaced
                    # to the agent instead of counting silent strikes
                    # (2026-07-27: dt only discovered a lost ask by
                    # defensively polling the board).
                    clear_result = await session.call_tool(
                        "decision_clear", {"from_agent": agent_name},
                    )
                    card_notice = _extract_text(clear_result)
            except Exception:  # noqa: BLE001
                pass

            agents_result = await session.call_tool("list_agents", {})

    messages_text = _extract_text(messages_result)
    broadcasts_text = _extract_text(broadcasts_result)
    agents_text = _extract_text(agents_result)

    # Is the agent shown as online? list_agents (default include_offline=False)
    # lists ONLY agents with status='online', so the name appearing at all
    # means they're connected to this hub instance (PR #3's 🟢 semantics).
    #
    # We deliberately do NOT key on the ⚡ marker. Since PR #3, ⚡ means
    # "push-deliverable RIGHT NOW" — an open GET /mcp stream. A perfectly
    # healthy agent lacks ⚡ while idle between turns, and the Stop hook fires
    # exactly at that idle transition. Keying the rebind nag on ⚡ produced a
    # false "you're not bound, re-register" alarm on every Stop for every idle
    # agent — a fleet-wide loop that register() couldn't clear (re-binding
    # doesn't reopen the GET stream at idle). Online (🟢) is the correct
    # "is this agent bound?" signal; ⚡ is not.
    is_online = f"**{agent_name}**" in agents_text

    return messages_text, broadcasts_text, is_online, card_notice


def _extract_text(call_tool_result: Any) -> str:
    """Pull the text payload out of an MCP call_tool result."""
    if call_tool_result is None:
        return ""
    content = getattr(call_tool_result, "content", None)
    if content is None and isinstance(call_tool_result, list):
        content = call_tool_result
    if content is None:
        return ""
    for block in content:
        text = getattr(block, "text", None)
        if text is not None:
            return text
    return ""


# ---------------------------------------------------------------------------
# Deferred low-priority surfacing (bar 47)
# ---------------------------------------------------------------------------
#
# A Stop-hook block is not free: emitting it continues the session, and that
# continuation IS a model turn. Measured on one mcp-hub session, 2026-09-01:
# 18 hook fires, 13 of them costing a turn, against 7 genuine operator
# prompts — the hook spent twice the model turns its operator did, most of
# them on a flapping `[low]` monitor.
#
# `low` already means "must never interrupt" on the hub side (card #73 gave
# it no backstop wake). The Stop hook was surfacing it at every turn boundary
# anyway, which put the interrupt back one layer down.
#
# So: a drain carrying ONLY low-priority items is spooled to a file and the
# hook returns None — zero turns. THE SPOOL IS NOT A DELIVERY MECHANISM. It
# is a holding pen whose contents ride the NEXT block, whatever triggers it.
# Nothing is delivered by the file being written; something is delivered when
# the next block prints it.
#
# THAT ORDERING IS THE WHOLE SAFETY ARGUMENT. The hub marked these messages
# read during the drain, so a spool that could be silently forgotten would
# recreate the PR #8 silent-loss bug exactly — "push success ≠ seen", one
# layer out. Two guarantees close it:
#   1. every later block prepends and clears the spool, so any traffic at all
#      flushes it; and
#   2. DEFER_MAX_SECONDS forces a block on its own, so a lane that receives
#      nothing else NEVER holds a spool indefinitely. That is the hub's own
#      HOLD_MAX_SECONDS reasoning at the client: batching may delay a
#      message, never strand it.
# Anything unparseable, any read/write failure, any doubt → block as before.
# The failure direction is a wasted turn, never a lost message.

DEFER_MAX_SECONDS = 1800.0  # 30 min: a spool this old blocks on its own

# A rendered line's priority tag, anchored the way receipts.py anchors refs:
# `[HH:MM:SS] **sender** ·grade ⟨ref⟩ [low]: body`. No tag means NORMAL —
# get_messages only tags non-normal priorities — so an untagged line is
# exactly the case that must still interrupt.
_MSG_LINE_RE = re.compile(
    r"^\[\d{2}:\d{2}:\d{2}\] \*\*[^*]+\*\*(?: ·[\w-]+)* ⟨[^⟩]+⟩(?: \[(\w+)\])?:",
    re.MULTILINE,
)


_LIVE_CLAIM_LINE_RE = re.compile(
    r"^\[\d{2}:\d{2}:\d{2}\] \*\*[^*]+\*\*(?: ·[\w-]+)* ⟨[^⟩]+⟩(?: \[\w+\])?: "
    r"\(already delivered live — ",
)


# The section headers `_spool_append` writes into the blob, and the one the
# render puts back on top of a taken spool. They are STRUCTURE, not content:
# a spool of already-delivered lines is still entirely already-delivered, and
# reading these as unknown shapes is what made the first version of this guard
# a no-op on the one path that leaked (2026-09-04). Matched exactly — anything
# else bolded is still an unknown line and still blocks.
_SPOOL_SECTION_RE = re.compile(
    r"^\*\*(?:Direct messages|Broadcasts \(since you last looked\)|"
    r"Held low-priority items \(deferred so they cost no turn "
    r"of their own\)):\*\*$"
)


def _all_already_delivered(*texts: str) -> bool:
    """True iff every line of the drain is one the hub says already rendered
    live in this lane's context.

    Bar 47 (g#24): such a drain re-prints what the agent has already read, and
    it still arrives as a BLOCKING hook error — so it costs exactly one model
    turn to acknowledge something with nothing to act on. Measured 2026-09-04:
    12 of 12 drains across three lanes were entirely this.

    Nothing can be lost here, which is what separates it from the low-priority
    spool: an "already delivered live" line IS the hub's statement that the
    full text already reached this context, and the hub marked it read when it
    rendered it. There is no only-copy to keep.

    Fails OPEN in every uncertain direction — an unparseable line, a body
    continuation, a render shape we do not know, no message lines at all: the
    caller blocks and the agent sees whatever it was. Only an affirmative
    reading with every line accounted for suppresses.
    """
    seen = False
    for text in texts:
        for raw in (text or "").splitlines():
            line = raw.strip()
            if not line:
                continue
            if _SPOOL_SECTION_RE.match(line):
                continue
            if _MSG_LINE_RE.match(line):
                if not _LIVE_CLAIM_LINE_RE.match(line):
                    return False
                seen = True
                continue
            # The render's own trailing note — "(1 already surfaced live —
            # shortened to save context, and now marked read. …)". Anything
            # else is a full body's continuation line, or a shape this parser
            # does not know; either way, block.
            if line.startswith("(") and line.endswith(")"):
                continue
            return False
    return seen


def _all_low_priority(*texts: str) -> bool:
    """True iff every rendered message line present is tagged `[low]`.

    Fails CLOSED in every uncertain direction: no parseable line at all
    (an unrecognised render, a footer-only drain) returns False, so the
    caller blocks and the agent sees whatever it was. Only an affirmative
    all-low reading defers.
    """
    seen = False
    for text in texts:
        for m in _MSG_LINE_RE.finditer(text or ""):
            seen = True
            if (m.group(1) or "normal").lower() != "low":
                return False
    return seen


def _defer_spool(agent_name: str) -> pathlib.Path:
    safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in agent_name)
    return _state_dir() / f"deferred-{safe}.txt"


def _spool_read(agent_name: str) -> tuple[str, float]:
    """Spooled text and the age in seconds of the OLDEST entry (0.0 if none)."""
    path = _defer_spool(agent_name)
    try:
        text = path.read_text(encoding="utf-8")
        age = max(0.0, time.time() - path.stat().st_mtime_ns / 1e9)
    except OSError:
        return "", 0.0
    if not text.strip():
        return "", 0.0
    # mtime tracks the LAST append; the first line carries the first append's
    # clock so the bound measures how long the OLDEST item has waited — a
    # steady trickle of low traffic must not keep resetting its own deadline.
    first = text.split("\n", 1)[0]
    if first.startswith("#spooled-at "):
        try:
            age = max(0.0, time.time() - float(first.split(" ", 1)[1]))
        except ValueError:
            pass
    return text, age


def _spool_append(agent_name: str, text: str) -> bool:
    """Add a deferred drain to the spool. False if it could not be written —
    the caller then blocks instead, because an unwritten spool is a lost
    message."""
    path = _defer_spool(agent_name)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        if not existing.strip():
            existing = f"#spooled-at {time.time()}\n"
        with path.open("w", encoding="utf-8") as fh:
            fh.write(existing.rstrip("\n") + "\n" + text.rstrip("\n") + "\n")
        return True
    except OSError:
        return False


def _spool_take(agent_name: str) -> str:
    """Return the spooled text and clear it. Cleared only after a successful
    read, so a failure leaves the spool for the next block rather than
    dropping it."""
    text, _ = _spool_read(agent_name)
    if not text:
        return ""
    try:
        _defer_spool(agent_name).unlink()
    except OSError:
        pass
    return "\n".join(
        ln for ln in text.splitlines() if not ln.startswith("#spooled-at ")
    ).strip()


# ---------------------------------------------------------------------------
# Hook output building
# ---------------------------------------------------------------------------


def build_hook_response(
    *,
    agent_name: str,
    project: str | None,
    messages_text: str,
    broadcasts_text: str = "",
    is_online: bool,
    stop_hook_active: bool = False,
    card_nag: bool = False,
    card_notice: str = "",
    held_notice: str = "",
    defer_low: bool = False,
) -> dict[str, Any] | None:
    """Decide whether to emit a hook block and what the reason should be.

    Returns the JSON payload Claude Code expects, or None to mean "no block,
    let Stop proceed normally."

    A block is emitted whenever there's anything actionable:
      - Queued DMs (with discipline reminder)
      - Unseen broadcasts (with discipline reminder; same gating rule —
        urgent always responds, related/important inline, FYI noted-and-defer)
      - Agent genuinely offline / absent from list_agents (rebind hint, with
        or without other content). NOTE: this keys on online status (🟢), NOT
        the ⚡ wake-marker — an idle-but-online agent is not nagged, since it
        legitimately lacks ⚡ between turns. Keying on ⚡ caused a fleet-wide
        false-rebind loop after PR #3 tightened ⚡ to "deliverable now".

    Online agent with empty inbox AND no unseen broadcasts → return None,
    Stop proceeds normally. This is the steady-state happy path: most Stop
    fires are no-op when the agent is up-to-date.

    `stop_hook_active` is Claude Code's flag for "this Stop is firing because
    a prior Stop-hook block already fired". It's a loop backstop: a re-fire
    has no fresh content (DMs were marked read, the broadcast cursor advanced
    on the first fire), so a content-less block would re-emit forever. When
    it's set and there's nothing new to surface, we let Stop proceed.
    """
    # Bar 47: a spool held from earlier low-only drains rides THIS block,
    # whatever triggered it. Taken before the has_content decision so spooled
    # text alone can justify a block (the DEFER_MAX_SECONDS path), and so no
    # branch below can return None while the spool still holds anything.
    deferred = ""
    spool_age = 0.0
    if defer_low and agent_name:
        _, spool_age = _spool_read(agent_name)

    has_messages = bool(messages_text.strip())
    has_broadcasts = bool(broadcasts_text.strip())
    has_content = has_messages or has_broadcasts

    # Defer only a drain that is ENTIRELY low-priority, with nothing else
    # owed. A card nag, an owner notice or an offline agent are corrections,
    # not traffic — they interrupt regardless of what else is queued.
    if (
        defer_low
        and agent_name
        and has_content
        and is_online
        and not card_nag
        and not card_notice.strip()
        and not held_notice.strip()
        and spool_age < DEFER_MAX_SECONDS
        and _all_low_priority(messages_text, broadcasts_text)
    ):
        blob = "\n".join(
            p for p in (
                "**Direct messages:**\n" + messages_text.strip()
                if has_messages else "",
                "**Broadcasts (since you last looked):**\n"
                + broadcasts_text.strip() if has_broadcasts else "",
            ) if p
        )
        if _spool_append(agent_name, blob):
            return None
        # Could not write the spool → fall through and block. The hub has
        # already marked these read; the ONLY other copy is this text.

    if defer_low and agent_name:
        deferred = _spool_take(agent_name)
        if deferred:
            has_content = True

    # Bar 47 (g#24; deputy ruling on #54, 2026-09-04): a drain whose every
    # item is already-delivered carries nothing new, so suppress the BLOCK.
    # Only the block — the hub already marked these read when it rendered them
    # live, so there is nothing to spool and nothing to lose. One unclaimed
    # line makes the whole drain block exactly as before, which is how
    # anything genuinely new still surfaces.
    #
    # The spool is INCLUDED in the reading (2026-09-04). The first version
    # skipped the guard whenever a spool had been taken — "a held spool always
    # wins: it is the only copy of its contents" — and that is right for a
    # genuine held low message and wrong for this one: spooled lines that are
    # themselves `(already delivered live — …)` are the hub stating the full
    # text already reached this context, so the guard's own "there is no
    # only-copy to lose" argument covers them too. Both survivors of the first
    # version leaked through exactly this path. A spool holding ONE genuine low
    # item still reads False and still blocks, carrying the whole spool with it.
    #
    # `_spool_take` above has already unlinked the spool, so suppressing here
    # discards it — deliberately, and only on an affirmative all-delivered
    # reading of every line in it.
    if has_content and _all_already_delivered(
        messages_text, broadcasts_text, deferred
    ):
        has_messages = has_broadcasts = has_content = False
        deferred = ""

    # An open card makes the "you have no card" nag factually wrong — the
    # right prompt is the notice ("card #N still open / STALE"), which the
    # server returns from the same decision_clear that used to count silent
    # strikes. Live specimen 2026-07-27: the nag told this repo's own agent
    # to file a card while its #79 sat open (and then evaporated).
    card_notice = card_notice.strip()
    if card_notice:
        card_nag = False

    # Loop backstop: never re-block a Stop that's only firing because a prior
    # block fired, when there's nothing new to surface. Guards against any
    # content-less block (rebind nag, card nag, card notice) wedging the
    # agent in a re-block loop, independent of the online/⚡ fix above. The
    # card nag in particular gets exactly ONE shot per natural Stop: if the
    # agent's nag-response turn still lacks a card, we let it go rather than
    # loop.
    if stop_hook_active and not has_content and not held_notice.strip():
        return None

    # No work needed: online + nothing queued + no correction owed.
    # (Online — not ⚡ — is the gate: an idle agent legitimately lacks ⚡
    # between turns.)
    held_notice = held_notice.strip()

    # A HELD LANE ALWAYS SURFACES, even on the happy path — empty inbox,
    # online, nothing owed. Being stopped is the one thing an agent must not
    # learn by having its pane disappear, and this is the last turn boundary
    # it gets. It also outranks the loop backstop below for the same reason:
    # a re-fired Stop on a held lane is still a lane about to be stopped.
    if not has_content and is_online and not card_nag and not card_notice \
            and not held_notice:
        return None

    parts: list[str] = []

    if held_notice:
        parts.extend([held_notice, ""])

    if has_content:
        parts.append("📬 Auto-checked at Stop boundary — queued items below:")
        if has_messages:
            parts.extend(["", "**Direct messages:**", messages_text.strip()])
        if has_broadcasts:
            parts.extend(["", "**Broadcasts (since you last looked):**", broadcasts_text.strip()])
        if deferred:
            parts.extend([
                "",
                "**Held low-priority items (deferred so they cost no turn "
                "of their own):**",
                deferred,
            ])

    if not is_online:
        rebind_args = [f'name="{agent_name}"']
        if project:
            rebind_args.append(f'project="{project}"')
        rebind_call = f"register({', '.join(rebind_args)})"

        if has_content:
            warning = (
                f"⚠️ Your hub session isn't showing as online in "
                f"list_agents (likely after a hub redeploy or a dropped "
                f"connection). Call `{rebind_call}` to re-register "
                f"before processing the queue."
            )
        else:
            warning = (
                f"⚠️ Auto-checked at Stop boundary: your hub session "
                f"isn't showing as online in list_agents (likely after a "
                f"hub redeploy or a dropped connection). No queued items "
                f"to process. Call `{rebind_call}` to re-register, then "
                f"continue what you were doing."
            )
        if has_content:
            parts.extend(["", warning])
        else:
            parts.append(warning)

    # Card nag — the authoring-compliance leg of the DECISION convention:
    # last turn reads as waiting-on-operator but carries no card. One line,
    # delivered at the exact moment of the miss; the loop backstop above
    # guarantees it can't repeat within one natural Stop.
    if card_nag:
        parts.extend([
            "",
            (
                "🙋 Your last message reads as waiting on the operator but has "
                "no DECISION card — end your reply with the DECISION block "
                "(**DECISION** / **ASK:** / **WHY:** / **VALUE:** …[n/10] / "
                "**RISK:** …[n/10]) so the ask reaches their triage board."
            ),
        ])

    # Owner notice — the anti-silent-removal leg (2026-07-27): the agent
    # hears its card's state from the same round-trip that used to count
    # strikes in silence. Answered in-pane is the common case the reminder
    # exists for; a card nobody remembers is closed by DECIDED, not by decay.
    if card_notice:
        parts.extend([
            "",
            (
                f"📌 {card_notice}\n"
                "If the operator already answered in-pane, end your reply "
                "with **DECIDED:** <their verdict> (this closes YOUR OPEN "
                "card — don't name a different #id in the line). If you're "
                "still waiting, nothing is needed: the ask stays on the "
                "board."
            ),
        ])

    # (The old always-appended "Discipline reminder" footer is gone: it cost
    # ~230 bytes x every content-bearing Stop x every agent — ~20% of all
    # stop-hook bytes fleet-wide on 2026-07-26 — repeating guidance that
    # already lives in the hub's connect-time instructions.)

    return {"decision": "block", "reason": "\n".join(parts)}


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------


def _read_hook_stdin() -> dict[str, Any]:
    """Read the JSON payload Claude Code sends to hooks on stdin.

    Returns {} on any error (no input, malformed JSON, no stdin attached).
    Callers should treat absent fields as "unknown" — the CLI is designed
    to no-op rather than fail when context is missing.
    """
    try:
        if sys.stdin.isatty():
            return {}
        raw = sys.stdin.read()
        if not raw.strip():
            return {}
        return json.loads(raw)
    except Exception:  # noqa: BLE001
        return {}


# ---------------------------------------------------------------------------
# Derived identity — the canonical way a clone knows who it is.
#
# Identity is DERIVED, not configured, so two clones of the same repo are
# structurally guaranteed to agree on `project` while never colliding on
# `name`:
#   project = "<org>/<repo>"        parsed from `git remote get-url origin`
#                                   (URL path only — SSH aliases like
#                                   git@github-monkeypashion:org/repo.git and
#                                   https://github.com/org/repo.git resolve
#                                   identically)
#   name    = "<repo>-<hostname>"   unique per clone/machine
#
# Participation is opt-in via a machine-local config (~/.mcp-hub/config.json,
# {"projects": ["org/repo", ...]}) — NOT a file committed to the repo. A
# committed marker is repo-global when identity must be clone-local; that's
# what made clones fight over one identity.
#
# The sanitization rule here is mirrored in ~/.claude/statusline-command.js —
# keep them in lockstep or the statusline can't find the status cache file.
# ---------------------------------------------------------------------------

_HUB_CONFIG_PATH = pathlib.Path.home() / ".mcp-hub" / "config.json"


def _load_hub_config() -> dict[str, Any]:
    """Read the machine-local hub config. {} on any error (fail-open)."""
    try:
        data = json.loads(_HUB_CONFIG_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _sanitize_ident(raw: str) -> str:
    """Canonical agent-name sanitization: lowercase, non [a-z0-9_-] → '-'.

    Mirrored in statusline-command.js — change both or neither.
    """
    return "".join(
        c if (c.isalnum() or c in "-_") else "-" for c in raw.lower()
    ).strip("-")


def _norm_path(p: str) -> str:
    """Absolute, symlink-resolved, trailing-separator-free path for keying."""
    try:
        return os.path.realpath(os.path.abspath(p)).rstrip("/\\")
    except OSError:
        return os.path.abspath(p).rstrip("/\\")


def _workspace_suffix(cwd: str) -> str | None:
    """Per-worktree identity suffix from ~/.mcp-hub/config.json, or None.

    Two clones of ONE repo on ONE machine derive the same name — `repo` comes
    from the git remote and `host` from the machine, so nothing in the
    derivation can tell them apart. Renaming the directory doesn't help. That
    made a transported clone indistinguishable from its source: `squad ls`
    reported it offline (no status file under the roster name) while the
    statusline inside it reported the SOURCE's status (same derived name).
    Two readouts of one agent, neither true.

    `workspaces` maps an absolute worktree path to a suffix:

        {"workspaces": {"/home/me/Projects/xport/mcp-hub": "xport"}}

    Machine-local and keyed by path, so it is clone-local BY CONSTRUCTION —
    unlike the old committed marker, it cannot be dragged to another clone.
    Absent → unchanged derivation, so the existing fleet keeps its names.

    🔴 THE ENTRY COVERS THE WHOLE WORKTREE, NOT JUST ITS TOP FOLDER (card
    #432). This was exact path equality, while the derivation around it
    resolves org/repo from the git remote — and git walks UP from cwd. So a
    session one level down still derived the repo's name but lost the suffix,
    and TWO clones of one repo collapsed onto ONE bare name the moment cwd
    was a subdirectory. That is precisely the collision the suffixes exist to
    prevent, rebuilt one directory below them. Sessions sit in subdirectories
    routinely; the registered path is the exception, not the rule.

    A descendant matches on PATH COMPONENTS, never on a string prefix, so
    `/a/b` does not claim `/a/bc`. When several entries match, the LONGEST
    (most specific) wins — that is what makes a nested worktree, e.g. a
    submodule inside a clone that has its own entry, keep its own suffix
    rather than inheriting its parent's.

    An entry that normalises to the empty string (`"/"`) is skipped: it would
    match every path on the machine and silently rename the whole fleet from
    one stray character, which no legitimate config is asking for.

    Mirrored in statusline-command.js — change both or neither.
    """
    table = _load_hub_config().get("workspaces")
    if not isinstance(table, dict):
        return None
    target = _norm_path(cwd)
    best_len = -1
    best_suffix: str | None = None
    for path, suffix in table.items():
        if not (isinstance(path, str) and isinstance(suffix, str) and suffix.strip()):
            continue
        root = _norm_path(path)
        if not root:
            continue
        if target == root or target.startswith(root + os.sep):
            if len(root) > best_len:
                best_len = len(root)
                best_suffix = suffix.strip()
    return best_suffix


def _resolve_squads(cwd: str) -> list[str]:
    """Squads this worktree belongs to, from WORKSPACE TYPE. Never raises.

    A `.code-workspace` file is typed by listing it here with the squad it
    names:

        {"squad_workspaces": {"/home/me/Projects/dreamteam.code-workspace":
                              "dreamteam"}}

    A FACULTY workspace needs no entry at all — it is an assembly of unrelated
    agents gathered for convenience, and confers no membership. Faculty is the
    absence of a squad, not a kind of one, so "not listed" is exactly the right
    representation and there is nothing to keep in sync.

    An agent in three squad workspaces is in three squads: that is where
    multi-membership comes from, and why nothing here needs a per-agent list.
    Put the folder in the workspace and the membership follows — one
    bookkeeping step, not two.

    Only workspace files named in the config are read, so this never searches
    the filesystem and cannot be surprised by a stray file. A workspace that has
    been deleted or moved is skipped rather than fatal: losing a squad is a
    smaller failure than refusing to register at all.
    """
    table = _load_hub_config().get("squad_workspaces")
    if not isinstance(table, dict):
        return []
    target = _norm_path(cwd)
    found: set[str] = set()
    for ws_path, squad in table.items():
        if not (isinstance(ws_path, str) and isinstance(squad, str) and squad.strip()):
            continue
        for folder in _workspace_folders(ws_path):
            if _norm_path(folder) == target:
                found.add(squad.strip())
                break
    return sorted(found)


def _workspace_folders(ws_path: str) -> list[str]:
    """Absolute folder paths listed in a .code-workspace file. [] on any error.

    These files are JSONC — VSCode tolerates comments and trailing commas, and
    the operator's are hand-formatted, so a strict json.loads fails on real
    ones. Comments are stripped before parsing rather than the file being
    rewritten: transport already learned that lesson the other way round, where
    a load-and-dump would have destroyed the formatting.

    Relative folder paths are resolved against the workspace file's directory,
    which is how VSCode itself interprets them.
    """
    try:
        raw = pathlib.Path(ws_path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    # Strip // and /* */ comments outside string literals, then trailing commas.
    out, i, n, in_str, esc = [], 0, len(raw), False, False
    while i < n:
        ch = raw[i]
        if in_str:
            out.append(ch)
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
        elif raw.startswith("//", i):
            i = raw.find("\n", i)
            if i == -1:
                break
        elif raw.startswith("/*", i):
            j = raw.find("*/", i + 2)
            i = n if j == -1 else j + 2
        else:
            out.append(ch)
            i += 1
    text = re.sub(r",(\s*[}\]])", r"\1", "".join(out))
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return []
    folders = data.get("folders") if isinstance(data, dict) else None
    if not isinstance(folders, list):
        return []
    base = pathlib.Path(ws_path).parent
    paths = []
    for entry in folders:
        p = entry.get("path") if isinstance(entry, dict) else None
        if isinstance(p, str) and p:
            paths.append(str(p if os.path.isabs(p) else (base / p)))
    return paths


def _parse_org_repo(url: str) -> tuple[str, str] | None:
    """Parse (org, repo) from a git remote URL, ignoring the host entirely.

    Handles scp-like (git@host:org/repo.git), ssh:// and https:// forms. The
    host is deliberately not inspected so SSH aliases (git@github-monkeypashion:...)
    and canonical hosts (github.com) yield the same org/repo. For nested paths
    (GitLab subgroups) the last two segments win.
    """
    s = url.strip().removesuffix("/").removesuffix(".git")
    if not s:
        return None
    if "://" in s:
        rest = s.split("://", 1)[1]
        path = rest.split("/", 1)[1] if "/" in rest else ""
    elif ":" in s:
        path = s.split(":", 1)[1]
    else:
        path = s
    parts = [p for p in path.strip("/").split("/") if p]
    if len(parts) < 2:
        return None
    return parts[-2], parts[-1]


def _git_remote_url(cwd: str) -> str | None:
    """`git remote get-url origin` for cwd, or None (no git / no origin)."""
    try:
        out = subprocess.run(
            ["git", "-C", cwd, "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=_NO_WINDOW_FLAG,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    url = out.stdout.strip()
    return url or None


def _derive_agent_identity(cwd: str | None) -> tuple[str | None, str | None]:
    """Derive (name, project) for cwd, gated on the machine-local opt-in list.

    Returns (None, None) unless cwd is a git repo whose origin org/repo
    appears in ~/.mcp-hub/config.json's "projects" list. Never raises.
    """
    if not cwd:
        return None, None
    url = _git_remote_url(cwd)
    if not url:
        return None, None
    parsed = _parse_org_repo(url)
    if not parsed:
        return None, None
    org, repo = parsed
    project = f"{org}/{repo}"
    opted_in = _load_hub_config().get("projects")
    if not isinstance(opted_in, list) or project not in opted_in:
        return None, None
    host = _sanitize_ident(platform.node() or "unknown-host")
    suffix = _workspace_suffix(cwd)
    raw = f"{repo}-{host}-{suffix}" if suffix else f"{repo}-{host}"
    name = _sanitize_ident(raw) or None
    return name, project


def onboard_command(args: argparse.Namespace) -> int:
    """`mcp-hub onboard` — opt the cwd's repo into hub participation.

    Cross-platform (this is the Windows story; Linux squad hosts can use
    `squad add`, which does the same opt-in). Derives org/repo from the git
    remote, appends it to ~/.mcp-hub/config.json, prints the derived
    identity. Idempotent. This is the ONLY per-repo step a machine needs —
    the hooks + Stop-hook self-heal handle daemon + register from the next
    turn/relaunch onward.
    """
    cwd = args.path or os.getcwd()
    url = _git_remote_url(cwd)
    if not url:
        print(f"!! {cwd} is not a git repo with an 'origin' remote", file=sys.stderr)
        return 1
    parsed = _parse_org_repo(url)
    if not parsed:
        print(f"!! couldn't parse org/repo from remote URL: {url}", file=sys.stderr)
        return 1
    org, repo = parsed
    project = f"{org}/{repo}"
    cfg = _load_hub_config()
    projects = cfg.get("projects")
    if not isinstance(projects, list):
        projects = []
    if project in projects:
        print(f"already opted in: {project}")
    else:
        projects.append(project)
        cfg["projects"] = projects
        _HUB_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        _HUB_CONFIG_PATH.write_text(
            json.dumps(cfg, indent=2) + "\n", encoding="utf-8"
        )
        print(f"opted in: {project}  ({_HUB_CONFIG_PATH})")
    host = _sanitize_ident(platform.node() or "unknown-host")
    print(f"derived identity: name={_sanitize_ident(f'{repo}-{host}')}  project={project}")
    marker = pathlib.Path(cwd) / AGENT_MARKER_PATH
    if marker.exists():
        print(
            f"note: legacy marker {marker} still present — derived identity "
            "overrides it, delete at leisure"
        )
    print(
        "next: relaunch this repo's Claude Code session (or just finish a "
        "turn — the Stop hook self-heals the daemon and prompts register)"
    )
    return 0


# ---------------------------------------------------------------------------
# Memory export/import — move Claude memory files between paired clones.
#
# Twin clones (same derived project, different machines) each keep their
# Claude memory under ~/.claude/projects/<encoded-cwd>/memory — and the
# encoding is machine-specific because it's derived from the clone's absolute
# path. The hub stages files keyed on the SHARED project, so the transfer is:
#   source machine:  mcp-hub memory-export   (push files, notify twins)
#   dest machine:    mcp-hub memory-import   (pull files, merge MEMORY.md)
# Filenames are preserved verbatim; MEMORY.md (the index Claude loads each
# session) is merged, never clobbered — that's what makes imported memories
# picked up seamlessly on the next session.
# ---------------------------------------------------------------------------


def _claude_project_dirname(cwd: str) -> str:
    """Encode an absolute project path the way Claude Code names its
    per-project state dir: every path separator (and drive colon) becomes
    '-'. Examples:
      /home/monke/Projects/code/monkeypashion/mcp-hub
        -> -home-monke-Projects-code-monkeypashion-mcp-hub
      D:\\Projects\\code\\monkeypashion\\mcp-hub
        -> D--Projects-code-monkeypashion-mcp-hub
    """
    return "".join("-" if c in "/\\:" else c for c in cwd.rstrip("/\\"))


def _claude_memory_dir(cwd: str) -> pathlib.Path:
    """The Claude Code memory dir for the project at `cwd` on THIS machine."""
    return (
        pathlib.Path.home() / ".claude" / "projects"
        / _claude_project_dirname(cwd) / "memory"
    )


def _is_safe_memory_filename(name: str) -> bool:
    return bool(name) and "/" not in name and "\\" not in name and name not in (".", "..")


def _merge_memory_index(
    local_index: str, staged_index: str, mem_dir: pathlib.Path
) -> tuple[str, int]:
    """Merge a twin's exported MEMORY.md into the local one.

    Returns (merged_text, lines_added). Local lines are never removed or
    reordered — we only APPEND staged index lines whose linked memory file
    (a) isn't already referenced locally and (b) actually exists in mem_dir
    (i.e. was imported, not skipped). Keeps the index truthful either way.
    """
    additions: list[str] = []
    for line in staged_index.splitlines():
        m = re.search(r"\]\(([^)]+\.md)\)", line)
        if not m:
            continue
        linked = m.group(1)
        if f"({linked})" in local_index:
            continue  # already indexed locally
        if not (mem_dir / linked).exists():
            continue  # don't index files that weren't imported
        additions.append(line)
    if not additions:
        return local_index, 0
    body = local_index.rstrip("\n")
    merged = (body + "\n" if body else "") + "\n".join(additions) + "\n"
    return merged, len(additions)


async def _memory_export(hub_url: str, name: str, project: str, cwd: str) -> int:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    mem_dir = _claude_memory_dir(cwd)
    if not mem_dir.is_dir():
        print(f"!! no memory dir on this machine: {mem_dir}", file=sys.stderr)
        return 1
    files = sorted(p for p in mem_dir.glob("*.md") if p.is_file())
    if not files:
        print(f"nothing to export — no .md files in {mem_dir}")
        return 0

    async with streamablehttp_client(_ephemeral_hub_url(hub_url), timeout=15) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()

            async def call(tool: str, args: dict[str, Any]) -> str:
                return _extract_text(await session.call_tool(tool, args)) or ""

            for p in files:
                result = await call("memory_put", {
                    "project": project,
                    "filename": p.name,
                    "content": p.read_text(encoding="utf-8"),
                    "from_agent": name,
                })
                print(f"  {p.name}: {result}")

            # Twin notification — reuse send()'s full wake semantics rather
            # than duplicating delivery logic hub-side.
            twins_text = await call(
                "list_twins", {"project": project, "exclude_agent": name}
            )
            twins = [t for t in twins_text.splitlines() if t.strip()]
            for twin in twins:
                await call("send", {
                    "from_agent": name,
                    "to": twin,
                    "message": (
                        f"🧠 Memory snapshot published for {project}: "
                        f"{len(files)} file(s) exported by {name}. Run "
                        "`mcp-hub memory-import` in your clone to pull it "
                        "(existing local files are kept; --force overwrites; "
                        "MEMORY.md is merged)."
                    ),
                    "priority": "normal",
                })
            print(
                f"exported {len(files)} file(s) from {mem_dir}\n"
                f"notified {len(twins)} twin(s): {', '.join(twins) or '(none online)'}"
            )
    return 0


async def _memory_import(
    hub_url: str,
    project: str,
    cwd: str,
    *,
    force: bool,
    dry_run: bool,
    replace_index: bool = False,
) -> int:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    mem_dir = _claude_memory_dir(cwd)

    async with streamablehttp_client(_ephemeral_hub_url(hub_url), timeout=15) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()

            async def call(tool: str, args: dict[str, Any]) -> str:
                return _extract_text(await session.call_tool(tool, args)) or ""

            listing = await call("memory_list", {"project": project})
            entries = [ln.split("\t") for ln in listing.splitlines() if ln.strip()]
            if not entries:
                print(f"nothing staged on the hub for {project}")
                return 0

            imported: list[str] = []
            skipped: list[str] = []
            identical = 0
            staged_index: str | None = None
            for parts in entries:
                fname = parts[0]
                staged_hash = parts[4] if len(parts) >= 5 else None
                if not _is_safe_memory_filename(fname):
                    skipped.append(f"{fname} (unsafe name)")
                    continue
                if fname == "MEMORY.md":
                    staged_index = await call(
                        "memory_get", {"project": project, "filename": fname}
                    )
                    continue  # merged below, never bulk-written
                target = mem_dir / fname
                if target.exists() and not force:
                    # Distinguish a harmless already-in-sync skip from a real
                    # divergence — "40 skipped" on a clean re-sync used to
                    # read as alarming when everything actually matched.
                    if staged_hash is not None and _text_digest(
                        target.read_text(encoding="utf-8")
                    ) == staged_hash:
                        identical += 1
                    else:
                        skipped.append(f"{fname} (DIFFERS from local; --force to overwrite)")
                    continue
                if dry_run:
                    imported.append(f"{fname} (dry-run)")
                    continue
                content = await call(
                    "memory_get", {"project": project, "filename": fname}
                )
                mem_dir.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
                imported.append(fname)

    # MEMORY.md handling — the index Claude loads each session.
    # Default: MERGE (append staged lines whose linked file isn't already
    # referenced locally; never remove or reorder local lines) — right for a
    # first import into a machine with its own memories.
    # --replace-index: adopt the staged index VERBATIM — the reconciliation
    # return-leg, where the curated canonical index (possibly restructured)
    # must replace the local one for the fleet to converge.
    merged_lines = 0
    index_replaced = False
    if staged_index and not dry_run:
        index_path = mem_dir / "MEMORY.md"
        if replace_index:
            mem_dir.mkdir(parents=True, exist_ok=True)
            index_path.write_text(staged_index, encoding="utf-8")
            index_replaced = True
        else:
            local_index = (
                index_path.read_text(encoding="utf-8") if index_path.exists() else ""
            )
            merged, merged_lines = _merge_memory_index(
                local_index, staged_index, mem_dir
            )
            if merged_lines:
                mem_dir.mkdir(parents=True, exist_ok=True)
                index_path.write_text(merged, encoding="utf-8")

    for f in imported:
        print(f"  + {f}")
    for s in skipped:
        print(f"  - {s}")
    index_note = (
        "MEMORY.md REPLACED with canonical index"
        if index_replaced
        else f"MEMORY.md lines merged: {merged_lines}"
    )
    print(
        f"{'DRY RUN — ' if dry_run else ''}new: {len(imported)}, "
        f"identical: {identical}, differs/skipped: {len(skipped)}, "
        f"{index_note} ({mem_dir})"
    )
    if imported and not dry_run:
        print("imported memories are live from the next Claude session in this repo")
    return 0


def _text_digest(text: str) -> str:
    """Truncated sha256 of TEXT content — mirrors the server's memory_list
    hash. Computed on decoded text (not raw bytes) so CRLF/LF differences
    between Windows and Linux disks never cause false mismatches: read_text
    normalizes newlines identically on both sides."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


async def _memory_verify(hub_url: str, project: str, cwd: str) -> int:
    """Compare local memory files against the hub's staged set by hash.

    Exit 0 = every staged file exists locally with identical content (the
    convergence proof). Local files NOT in the staged set are reported as
    extras (informational — they don't fail verification, but after a full
    reconciliation ceremony there should be none)."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    mem_dir = _claude_memory_dir(cwd)

    async with streamablehttp_client(_ephemeral_hub_url(hub_url), timeout=15) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("memory_list", {"project": project})
            listing = _extract_text(result) or ""

    staged: dict[str, str] = {}
    for ln in listing.splitlines():
        parts = ln.split("\t")
        if len(parts) >= 5:
            staged[parts[0]] = parts[4]

    if not staged:
        print(f"nothing staged on the hub for {project} — nothing to verify")
        return 1

    ok, missing, differs = [], [], []
    for fname, remote_hash in sorted(staged.items()):
        target = mem_dir / fname
        if not target.exists():
            missing.append(fname)
            continue
        local_hash = _text_digest(target.read_text(encoding="utf-8"))
        (ok if local_hash == remote_hash else differs).append(fname)

    local_files = (
        {p.name for p in mem_dir.glob("*.md")} if mem_dir.is_dir() else set()
    )
    extras = sorted(local_files - set(staged))

    for f in missing:
        print(f"  ✗ missing locally: {f}")
    for f in differs:
        print(f"  ✗ differs: {f}")
    for f in extras:
        print(f"  · local-only (not staged): {f}")
    print(
        f"identical: {len(ok)}/{len(staged)}"
        + (" ✓" if len(ok) == len(staged) else " ✗")
        + f", local extras: {len(extras)}  ({mem_dir})"
    )
    return 0 if len(ok) == len(staged) else 1


def memory_verify_command(args: argparse.Namespace) -> int:
    resolved = _resolve_for_memory(args)
    if resolved is None:
        return 1
    _name, project, cwd = resolved
    print(f"verifying local memory against staged set for {project}")
    return asyncio.run(_memory_verify(args.hub_url, project, cwd))


def _resolve_for_memory(args: argparse.Namespace) -> tuple[str, str, str] | None:
    """(name, project, cwd) for memory commands — derived identity only.
    Prints guidance and returns None when the repo isn't onboarded."""
    cwd = args.path or os.getcwd()
    name, project = _derive_agent_identity(cwd)
    if name is None or project is None:
        print(
            f"!! {cwd} has no derived hub identity — run `mcp-hub onboard` "
            "in the repo first (memory transfer pairs clones by their "
            "derived project).",
            file=sys.stderr,
        )
        return None
    return name, project, cwd


def memory_export_command(args: argparse.Namespace) -> int:
    resolved = _resolve_for_memory(args)
    if resolved is None:
        return 1
    name, project, cwd = resolved
    print(f"exporting memory for {project} as {name}")
    return asyncio.run(_memory_export(args.hub_url, name, project, cwd))


def memory_import_command(args: argparse.Namespace) -> int:
    resolved = _resolve_for_memory(args)
    if resolved is None:
        return 1
    _name, project, cwd = resolved
    print(f"importing memory for {project}")
    return asyncio.run(
        _memory_import(
            args.hub_url,
            project,
            cwd,
            force=args.force,
            dry_run=args.dry_run,
            replace_index=args.replace_index,
        )
    )


# ---------------------------------------------------------------------------
# Transport: move an agent's CONVERSATION HISTORY to a new project path.
#
# Memory files are path-LOCATED but not path-ENCODED — copying them to the
# destination's encoded dir is enough. Transcripts are different: they embed
# the old absolute path in structural fields, ~2,200 times in a single day's
# session. Those must be rewritten or the transported agent carries live
# pointers back into the SOURCE agent's state (verified: 536 of them aimed at
# the source's own memory dir, where a rewind would have written).
#
# Message content is NEVER rewritten. It records what happened on a machine
# that genuinely had that path; changing it would forge the transcript.
# ---------------------------------------------------------------------------

# The transcript is written by JS JSON.stringify: compact separators, non-ASCII
# emitted literally. Match it exactly or every line silently reformats into a
# semantically-identical but byte-different file.
_JS_JSON = {"separators": (",", ":"), "ensure_ascii": False}

# ---- field classification --------------------------------------------------
# The completeness guard used to key on the SOURCE path: "did any reference to
# where this came from survive outside a content field". That cannot see a THIRD
# path — one that is neither source nor destination — so a guard reporting
# "complete" was asserting a property it had not checked. Measured 2026-07-27.
#
# Classifying FIELDS instead of matching a string fixes the class rather than the
# instance: a coupling nobody has thought of yet lands in none of these buckets
# and trips the guard, instead of needing a fourth special case each time.

# CONTENT — the historical record. A path here is what genuinely happened on a
# machine that genuinely had it, so these stay byte-exact. Never re-keyed.
# `lastPrompt` and `content` are here because they hold TEXT the human or model
# wrote. Note what putting them here also fixes: a slash command like "/compact"
# is indistinguishable from an absolute path by shape, so any pattern-matching
# approach flags prose. Classifying the field is the only thing that can tell
# "text that happens to start with a slash" from "a pointer at a directory".
_HISTORY_CONTENT_FIELDS = (
    "message", "toolUseResult", "attachment", "lastPrompt", "content",
)

# ENVIRONMENT — absolute paths that describe the BOX, not the worktree: a hook
# command names a binary on whichever machine ran it. Re-keying these would be
# wrong (they are configuration, not location) and refusing on them would block
# every transport, since one is always present.
_HISTORY_ENV_FIELDS = ("hookInfos",)

# LOCATED — the field's value IS the session's location, so after a re-key it must
# name the destination and nothing else.
_HISTORY_LOCATED_FIELDS = ("cwd",)

# Anything that looks like an absolute filesystem path, POSIX or Windows.
_ABS_PATH_RE = re.compile(r"^(?:/[^/\s]|[A-Za-z]:[\\/])")


def _path_under(path: str, base: str) -> bool:
    """Is `path` `base` itself, or inside it? Boundary-aware, NOT a substring
    test — `<base>-2` is a sibling worktree, not a child, and the fan-out
    genuinely creates those when two clones of one repo land together.
    """
    base = base.rstrip("/")
    return path == base or path.startswith(base + "/")


def _history_unclassified_paths(obj: Any, dest: str) -> list[str]:
    """Dotted paths of structural fields carrying a path this module has not
    classified — the check that survives a coupling nobody predicted.

    Deliberately keyed on the FIELD, not on any particular path string, which is
    what the old source-only guard could not do. A LOCATED field naming anything
    but the destination counts too: that is exactly the third-path case, where a
    real repo that is neither source nor destination stayed pointed at.
    """
    found: list[str] = []

    def walk(o: Any, prefix: str, root: str) -> None:
        if isinstance(o, str):
            if not _ABS_PATH_RE.match(o):
                return
            if root in _HISTORY_CONTENT_FIELDS or root in _HISTORY_ENV_FIELDS:
                return
            if root in _HISTORY_LOCATED_FIELDS:
                # A session may be launched in a SUBDIRECTORY of its worktree, so
                # "inside the destination" is the invariant, not "equal to it".
                # Demanding equality contradicted the re-key branch, which
                # preserves subdirs — one of them had to yield.
                if not _path_under(o, dest):
                    found.append(prefix)
                return
            found.append(prefix)
        elif isinstance(o, dict):
            for k, v in o.items():
                child = f"{prefix}.{k}" if prefix else str(k)
                # KEYS as well as values: trackedFileBackups is a dict keyed BY
                # absolute path, and a guard that exists for couplings nobody
                # predicted cannot be blind to half the structure it walks.
                if isinstance(k, str):
                    walk(k, child, root or str(k))
                walk(v, child, root or str(k))
        elif isinstance(o, list):
            for v in o:
                walk(v, prefix + "[]", root)

    walk(obj, "", "")
    return found


def _rekey_deep(obj: Any, old: tuple[str, str], new: tuple[str, str]) -> Any:
    """Repath every string AND every dict key in a structural subtree."""
    def sub(s: str) -> str:
        return s.replace(old[0], new[0]).replace(old[1], new[1])

    if isinstance(obj, str):
        return sub(obj)
    if isinstance(obj, list):
        return [_rekey_deep(v, old, new) for v in obj]
    if isinstance(obj, dict):
        return {
            (sub(k) if isinstance(k, str) else k): _rekey_deep(v, old, new)
            for k, v in obj.items()
        }
    return obj


def _history_stale_fields(
    obj: Any, old: tuple[str, str], new: tuple[str, str] | None = None
) -> list[str]:
    """Dotted paths of every field still referencing the source path.

    `new` is the destination pair, and it is needed because the default
    destination for a same-machine transport is a SIBLING of the source:
    the original owns the canonical path, so the clone lands at
    `<repo>-<label>`, which contains `<repo>` as a prefix. A plain substring
    test reads the correctly-rewritten `cwd` as a leak and the whole transcript
    is refused — silently shipping a clone with no conversation history while
    the transport reports success. Mask the legitimate destination references
    first, then ask whether anything STILL points at the source.

    A boundary check would not do: in the ENCODED dirname form the separator is
    itself '-', so `…-demo-app` inside `…-demo-app-sidecar` looks like a
    path-boundary match. Masking is the question actually being asked.
    """
    found: list[str] = []

    def stale(s: Any) -> bool:
        if not isinstance(s, str):
            return False
        if new is not None:
            for ref in new:
                if ref:
                    s = s.replace(ref, "\x00")
        return old[0] in s or old[1] in s

    def walk(o: Any, prefix: str) -> None:
        if isinstance(o, str):
            if stale(o):
                found.append(prefix)
        elif isinstance(o, dict):
            for k, v in o.items():
                if stale(k):
                    found.append(prefix)
                walk(v, f"{prefix}.{k}" if prefix else str(k))
        elif isinstance(o, list):
            for v in o:
                walk(v, prefix + "[]")

    walk(obj, "")
    return found


def _rekey_transcript(text: str, old_cwd: str, new_cwd: str) -> tuple[str, dict[str, int]]:
    """Re-key one transcript. Returns (new_text, stats).

    FOUR structural couplings, all established empirically:
      1. top-level  cwd
      2. file-history-delta.trackingPath
      3. file-history-delta.backup.realParentDir
      4. file-history-snapshot.snapshot.trackedFileBackups — a dict KEYED by
         absolute path, each value carrying its own realParentDir

    (4) is the one that bites: the FIRST file-history-snapshot line in a
    transcript has an empty trackedFileBackups, so sampling one line "proves"
    the type carries no paths. It doesn't.

    Two DIFFERENT guards, because they catch different failures:
      - faithfulness  — nothing outside the named fields changed. Checked
        against our own field list, so it can never catch a field we forgot.
      - completeness  — every surviving reference sits in a content field.
        This is the only thing that catches a MISSED field, and the caller
        must refuse to write when it trips.
    """
    old = (_claude_project_dirname(old_cwd), old_cwd)
    new = (_claude_project_dirname(new_cwd), new_cwd)
    stats = {"lines": 0, "cwd": 0, "tracking": 0, "realparent": 0,
             "snapshot": 0, "unparseable": 0, "roundtrip_mismatch": 0,
             "content_touched": 0, "completeness_violations": 0,
             "unclassified_paths": 0}
    out: list[str] = []

    for raw in text.splitlines():
        stats["lines"] += 1
        if not raw.strip():
            out.append(raw)
            continue
        try:
            d = json.loads(raw)
        except json.JSONDecodeError:
            stats["unparseable"] += 1
            out.append(raw)              # never drop a line we can't parse
            continue

        if json.dumps(d, **_JS_JSON) != raw:
            stats["roundtrip_mismatch"] += 1
        before = json.loads(json.dumps(d, **_JS_JSON))

        # `cwd` is the session's LOCATION, and in the clone's copy every record's
        # location is the clone. Rewriting only the source path left a THIRD path
        # untouched whenever one existed — measured 2026-07-27, 109 records naming
        # a different real clone of the same repo, because this seat's recorded
        # cwd is one tree while it works in another. A transcript is per-project,
        # so any cwd in it named that project somewhere; pointing them all at the
        # destination is the same claim the source-path rewrite already makes,
        # applied completely instead of partially.
        cwd_val = d.get("cwd")
        if isinstance(cwd_val, str) and cwd_val and not _path_under(cwd_val, new[1]):
            # Under the source ⇒ keep the subdirectory (a session can be launched
            # below its worktree root). Anything else is a third path, and the
            # clone's copy should name the clone. Boundary-aware, so a sibling
            # `<old>-2` is treated as the third path it is, not as the source.
            d["cwd"] = (
                new[1] + cwd_val[len(old[1].rstrip("/")):]
                if _path_under(cwd_val, old[1]) else new[1]
            )
            stats["cwd"] += 1

        # Same reasoning as the snapshot journal above: a delta names a backup
        # of a file on the SOURCE machine. Neutralise the pointers instead of
        # aiming them somewhere — whether that somewhere exists or not.
        if d.get("type") == "file-history-delta":
            if isinstance(d.get("trackingPath"), str):
                d["trackingPath"] = ""
                stats["tracking"] += 1
            bk = d.get("backup")
            if isinstance(bk, dict) and isinstance(bk.get("realParentDir"), str):
                bk["realParentDir"] = ""
                stats["realparent"] += 1

        # file-history is a machine-LOCAL undo journal, not conversation. It
        # points at backup files that do not exist at the destination, so
        # re-keying it produced dangling pointers at best — and at worst live
        # ones: paths that were harmless on the source can name something real
        # on the destination. Measured on the first cross-machine transport:
        # 1,899 structural references to a path that was merely an install copy
        # here and is the RECEIVING agent's live worktree there. A rewind in the
        # transported session could have written into it.
        #
        # So for transport we DROP the journal rather than rewrite it. Nothing
        # is lost that exists at the far end, and no pointer can aim at a tree
        # we do not own. Re-keying a path only makes sense when the thing it
        # names travels; these do not.
        if d.get("type") == "file-history-snapshot":
            snap = d.get("snapshot")
            if isinstance(snap, dict) and snap.get("trackedFileBackups"):
                snap["trackedFileBackups"] = {}
                stats["snapshot"] += 1

        after = json.loads(json.dumps(d, **_JS_JSON))

        def blank(o: dict[str, Any]) -> str:
            o = json.loads(json.dumps(o, **_JS_JSON))
            o.pop("cwd", None)
            if o.get("type") == "file-history-delta":
                o.pop("trackingPath", None)
                if isinstance(o.get("backup"), dict):
                    o["backup"].pop("realParentDir", None)
            if o.get("type") == "file-history-snapshot":
                if isinstance(o.get("snapshot"), dict):
                    o["snapshot"].pop("trackedFileBackups", None)
            return json.dumps(o, sort_keys=True, **_JS_JSON)

        if blank(before) != blank(after):
            stats["content_touched"] += 1

        for field in _history_stale_fields(after, old, new):
            root = field.split(".")[0].split("[")[0]
            # ENVIRONMENT is exempt here too, or "never re-keyed, never refused"
            # is only half true. A hook command that lives INSIDE the transported
            # worktree contains the source path by construction — which is the
            # mcp-hub agent itself, i.e. the one most likely to be migrated when
            # a machine is retired. It refused, and squad reads a refusal as
            # "transported WITHOUT history" while reporting success.
            if root not in _HISTORY_CONTENT_FIELDS and root not in _HISTORY_ENV_FIELDS:
                stats["completeness_violations"] += 1

        # The same question asked of FIELDS rather than of one path string, so a
        # third path — or a coupling nobody has classified — cannot pass by being
        # neither source nor destination.
        stats["unclassified_paths"] += len(_history_unclassified_paths(after, new[1]))

        out.append(json.dumps(d, **_JS_JSON))

    return "\n".join(out) + ("\n" if out else ""), stats


def _ephemeral_hub_url(url: str) -> str:
    """Strip the ?agent= identity from a hub URL before an EPHEMERAL client
    connects with it.

    The parameter exists so the hub can auto-rebind an agent's interactive
    session at the transport layer (deploys stop being fleet events). The
    cli's own short-lived clients — stop-hook, heartbeat daemon, memory
    scripts — connect with the same configured URL, and a binding claimed by
    a session that is DELETEd seconds later is the classic clobbered-wake-
    target bug (why bind=False exists). The hub's interactive-client gate is
    the second layer; this strip means the claim never even reaches it."""
    try:
        from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
        parts = urlsplit(url)
        if "agent=" not in (parts.query or ""):
            return url
        query = [(k, v) for k, v in parse_qsl(parts.query) if k != "agent"]
        return urlunsplit(parts._replace(query=urlencode(query)))
    except Exception:  # noqa: BLE001
        return url


def _hub_config_candidates(cwd: str) -> list[tuple[pathlib.Path, list[str]]]:
    """Where a seat's hub URL may be stamped, in precedence order: the
    repo-scoped `.mcp.json`, then ~/.claude.json's PER-PROJECT override.

    The user-global `mcpServers.hub` is DELIBERATELY not a candidate. It is
    shared by every seat on the box, and stamping one seat's identity into
    it made every reconnecting agent on dev-vm-1 announce itself as that
    seat — the 2026-07-27 cross-delivery incident (dt received the hub
    maintainer's DMs within the hour; fb measured the poisoned file). An
    identity stamp belongs only in scopes that denote ONE seat."""
    return [
        (pathlib.Path(cwd) / ".mcp.json", ["mcpServers", "hub"]),
        (pathlib.Path.home() / ".claude.json", ["projects", cwd, "mcpServers", "hub"]),
    ]


def _dig(data: Any, path: list[str]) -> Any:
    for key in path:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def hibernate_command(args: argparse.Namespace) -> int:
    """One hibernate pass — `mcp-hub hibernate` (bar 59).

    Query the console for candidates, park them, re-park the ones still
    listed, release the ones that are not. The whole rule lives in
    `mcp_hub.hibernate`; this is the door.
    """
    from mcp_hub.hibernate import ConsoleAPI, HubHolds, scan
    from mcp_hub.operator_api import TOKEN_FILE, api_base, resolve_token

    # ⛔ This read $MCP_HUB_OPERATOR_TOKEN until 2026-09-05 and would have
    # refused on every correctly-configured box. The seat-action route checks
    # the MANAGEMENT API token ($MCP_HUB_API_TOKEN, else ~/.mcp-hub/api.token)
    # — `resolve_token` is that resolver and the one the rest of the CLI uses.
    # The other name grades operator-named SENDS. Two secrets, one word.
    token = args.token or resolve_token()
    if not token:
        # A pass with no token would refuse every write one at a time and
        # report a list of API errors. Say the one true thing instead.
        print(
            f"hibernate: no hub API token — set MCP_HUB_API_TOKEN, write "
            f"{TOKEN_FILE}, or pass --token. The seat-action route is "
            "operator-principal only (a machine token gets 403): a machine "
            "token here would let a seat park its neighbours.",
            file=sys.stderr,
        )
        return 2

    # 🔴 SAFE BY DEFAULT, after this verb parked four live lanes at 01:0xZ on
    # 2026-09-05 while I was probing its no-token refusal. `--token ""` is
    # falsy, so it fell through to `resolve_token()`, this box HAS the API
    # token in ~/.mcp-hub/api.token, and a command I believed could not write
    # held vps-hetzner, dreamteam, features-json and reliable-ai. Released
    # within the minute, owner-scoped.
    # ⚠️ Arming a scanner is a DELIBERATE ACT and must not be the default a
    # typo reaches. A bare `mcp-hub hibernate` now REPORTS; only `--arm`
    # writes. --dry-run stays as the explicit spelling of the same thing.
    if not args.arm:
        args.dry_run = True
    rep = scan(
        ConsoleAPI(base_url=args.console_url),
        # The hub URL points at /mcp for MCP clients; /api/v1 lives beside
        # it. Passing the MCP url straight through 404s on every seat read
        # and the pass reports "the seat list could not be read" — true, and
        # for a reason nobody would guess from the message.
        HubHolds(base_url=api_base(args.hub_url), token=token),
        thread=args.thread,
        dry_run=args.dry_run,
    )
    if not args.arm:
        print("hibernate: REPORT ONLY — nothing was written. "
              "Pass --arm to place and release holds.")
    print(rep.line())
    w = "would " if rep.dry else ""
    for lane in rep.held:
        print(f"  {w}park    {lane}" if rep.dry else f"  parked   {lane}")
    for lane in rep.re_held:
        print(f"  {w}re-hold {lane}" if rep.dry else f"  re-held  {lane}")
    for lane in rep.released:
        print(f"  would release {lane}" if rep.dry else f"  released {lane}")
    for lane in rep.refused:
        print(f"  REFUSED  {lane}")
    # 🔴 A pass that could not ask is not a quiet fleet, and the exit code is
    # the only part of this a timer reads.
    return 0 if rep.asked else 1


def edge_command(args: argparse.Namespace) -> int:
    """One edge reconcile pass — `mcp-hub edge apply`."""
    from mcp_hub.edge import (
        EDGE_ENV_FILE,
        HubAPI,
        apply_env_file,
        edge_apply,
        load_env_file,
        plan,
    )

    Path = pathlib.Path

    # Seat credentials, the same file the systemd unit loads via
    # `EnvironmentFile=-%h/.mcp-hub/edge-env`. A shell inherits nothing from
    # that unit, so before this the SAME command built a live seat from the
    # timer and an auth-dead one from a terminal — the container came up,
    # `docker ps` showed it, and it had exited 42 at its own door. A pass
    # whose result depends on how it was invoked is not a reconcile.
    supplied = apply_env_file(os.environ, load_env_file(EDGE_ENV_FILE))
    if supplied:
        # Names only. The values are the one thing on this box that must
        # never reach a log, a journal or a transcript.
        print(f"edge: loaded {', '.join(supplied)} from {EDGE_ENV_FILE}")

    machine = args.machine or _sanitize_ident(platform.node() or "unknown-host")
    # The FILE is the third source, and in practice the only one that matters:
    # `machines enrol` and `machines rotate` both write it, and a systemd timer
    # has no --token to pass and inherits no environment. Without this the two
    # halves never met — rotating a token achieved nothing and the timer was a
    # no-op that reported "no machine token" into the journal every 2 minutes.
    token = args.token or os.environ.get("MCP_HUB_MACHINE_TOKEN", "")
    if not token:
        from mcp_hub.operator_api import MACHINE_TOKEN_FILE
        try:
            token = MACHINE_TOKEN_FILE.read_text(encoding="utf-8").strip()
        except OSError:
            token = ""
    if not token:
        from mcp_hub.operator_api import MACHINE_TOKEN_FILE
        print(f"edge: no machine token — run `mcp-hub machines enrol` (writes "
              f"{MACHINE_TOKEN_FILE}), or pass --token / "
              "$MCP_HUB_MACHINE_TOKEN", file=sys.stderr)
        return 2

    # The hub URL points at /mcp for MCP clients; the API lives beside it.
    base = args.hub_url.rsplit("/mcp", 1)[0]
    api = HubAPI(base_url=base, token=token)

    def runner(cmd: list[str], cwd: str | None = None) -> tuple[int, str]:
        exe = _resolve_tool(cmd[0]) if cmd else None
        if exe is None:
            # A raw FileNotFoundError traceback names a symptom, not a cause,
            # and this runs from the heal timer where nobody reads tracebacks.
            return 127, (
                f"{cmd[0]}: not found — looked on PATH and at "
                f"{_TOOL_PATHS.get(cmd[0], '(no fixed location)')}. "
                "A non-interactive shell (ssh, systemd timer, cron) does not "
                "get ~/.local/bin on PATH."
            )
        try:
            proc = subprocess.run(
                [exe, *cmd[1:]], capture_output=True, text=True, cwd=cwd,
                timeout=600,
            )
        except (OSError, subprocess.SubprocessError) as e:
            return 127, f"{cmd[0]}: {type(e).__name__}: {e}"
        return proc.returncode, (proc.stdout + proc.stderr)

    scan_dirs = [Path(d) for d in (args.scan_dir or [])] or [
        Path.home() / "Projects",
        Path.home(),
    ]

    if args.action == "watch":
        # The doorbell runs THE SAME PASS the timer runs — one dispatch path,
        # two triggers. A second implementation here could drift from the one
        # that actually reconciles, and the drift would only show under load.
        from mcp_hub.edge import EnumerationFailed as _EF
        from mcp_hub.edge import push_failure, watch_forever

        def one_pass(reason: str) -> None:
            try:
                summary = edge_apply(
                    api, machine=machine, runner=runner, scan_dirs=scan_dirs
                )
            except _EF as e:
                # The failure must reach the hub, not just this journal —
                # a pass that dies before push_status otherwise reads as a
                # quiet healthy machine (the five-day shape).
                push_failure(api, machine, str(e))
                print(f"edge: {e}", file=sys.stderr, flush=True)
                return
            print(
                f"edge apply ({reason}): {summary['placements']} placement(s), "
                f"{len(summary['actions'])} action(s)", flush=True
            )

        print(f"edge watch: {machine} — doorbell open; the timer remains the "
              "floor, so a dead stream costs latency, never work", flush=True)
        watch_forever(base, token, machine, one_pass)
        return 0

    if args.dry_run:
        placements = api.pull_placements(machine)
        rc, out = runner(["squad", "ls"])
        if rc != 0:
            # Same rule as the real pass: a dry run that cannot see the
            # substrate must not print a plan, because the plan would be
            # computed against an empty set it mistook for an observation.
            print(f"edge: cannot enumerate this machine — {out.strip()[:300]}",
                  file=sys.stderr)
            return 1
        enrolled = {}
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1] in ("up", "down"):
                enrolled[parts[0]] = parts[1] == "up"
        local = {
            p["seat"]: {
                "materialized": p["seat"] in enrolled,
                "running": enrolled.get(p["seat"], False),
            }
            for p in placements
        }
        actions = plan(placements, local)
        print(f"edge apply --dry-run: {len(placements)} placement(s), would run:")
        for a in actions:
            print(f"  {a['op']:12s} {a['seat']}" + (
                f"  ({a['reason']})" if a.get("reason") else ""
            ))
        if not actions:
            print("  nothing — desired state already holds")
        return 0

    from mcp_hub.edge import EnumerationFailed, push_failure
    try:
        summary = edge_apply(
            api, machine=machine, runner=runner, scan_dirs=scan_dirs
        )
    except EnumerationFailed as e:
        # Same rule as the watch path: the failure reaches the hub, or a
        # dead edge reads as a quiet healthy machine.
        push_failure(api, machine, str(e))
        print(f"edge: {e}", file=sys.stderr)
        return 1
    print(
        f"edge apply: {summary['placements']} placement(s), "
        f"{len(summary['actions'])} action(s), "
        f"{summary['observed_reported']} observed report(s), "
        f"{summary['workspaces_reported']} workspace(s) discovered"
    )
    for a in summary["actions"]:
        detail = a.get("reason") or a.get("deferred") or f"rc={a.get('rc')}"
        print(f"  {a['op']:12s} {a['seat']}  {detail}")
    return 0


def rebind_url_command(args: argparse.Namespace) -> int:
    """Stamp ?agent=<derived name> into this seat's hub URL — the per-seat
    rollout leg of transport-level auto-rebind."""
    cwd = args.cwd or os.getcwd()
    name, _project = _derive_agent_identity(cwd)
    if not name:
        print(f"no derived identity for {cwd} (not opted in?) — nothing written")
        return 1
    mcp_json = None
    data: Any = None
    hub_path: list[str] = []
    for candidate, path in _hub_config_candidates(cwd):
        try:
            candidate_data = json.loads(candidate.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            continue
        hub_entry = _dig(candidate_data, path)
        if isinstance(hub_entry, dict) and hub_entry.get("url"):
            mcp_json, data, hub_path = candidate, candidate_data, path
            break
    if mcp_json is None:
        looked = ", ".join(str(c) for c, _ in _hub_config_candidates(cwd))
        print(f"no hub server URL found (looked in: {looked}) — nothing written")
        return 1
    hub = _dig(data, hub_path)
    url = hub["url"]
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query) if k != "agent"]
    query.append(("agent", name))
    new_url = urlunsplit(parts._replace(query=urlencode(query)))
    if new_url == url:
        print(f"already stamped: {url}")
        return 0
    where = f"{mcp_json}:{'.'.join(hub_path)}"
    if args.dry_run:
        print(f"would rewrite {where}: {url} -> {new_url}")
        return 0
    # Mutate in place — these files carry unrelated state (~/.claude.json
    # holds every project's settings), so only the one key changes.
    hub["url"] = new_url
    mcp_json.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"stamped {where}: {new_url}")
    print("(takes effect at the session's next MCP reconnect or relaunch)")
    return 0


def _workspace_scan_dirs() -> list[pathlib.Path]:
    return [pathlib.Path.home() / "Projects", pathlib.Path.home()]


def _workspace_listings(path: pathlib.Path) -> list[str]:
    """The folder paths a workspace file lists, or [] if it won't parse.

    Comments are stripped the same way `edge.discover_workspaces` strips
    them: these files are JSONC by convention and hand-edited in practice.
    """
    try:
        raw = re.sub(r"//[^\n]*", "", path.read_text(encoding="utf-8"))
        return [f.get("path", "") for f in json.loads(raw).get("folders", [])]
    except (OSError, json.JSONDecodeError):
        return []


def refuse_unhonoured_dry_run(
    args: argparse.Namespace, honoured: tuple[str, ...]
) -> bool:
    """True when --dry-run was passed to an action that does not implement it.

    🔴 A --dry-run that is silently IGNORED is worse than one that does not
    exist. Measured against PRODUCTION 2026-08-09: `seats add --dry-run`
    really created the seat and printed the SAME success line a real add
    prints, so nothing in the output said a write had happened — the next
    command failed with 409 and that was the only signal. `seats rm --dry-run`
    DELETED by the identical omission.

    These parsers share one --dry-run across every action, so scoping it in a
    help string ("update/clone: …") is documentation, not a guard. This is the
    guard, and it is fail-CLOSED: an action absent from `honoured` refuses,
    so a NEW write verb added later cannot inherit the silent-write bug — the
    worst it can do is decline a flag it never implemented.

    Read-only actions belong in `honoured`: for `list`, a dry run and a real
    run are the same act, so the flag is honoured trivially rather than
    ignored.
    """
    if not getattr(args, "dry_run", False):
        return False
    action = getattr(args, "action", "") or ""
    if action in honoured:
        return False
    print(
        f"--dry-run is not implemented for '{action}' — it is honoured for: "
        f"{', '.join(honoured)}.\nRefusing rather than writing: a dry run "
        "that performs the real act is the worst of both.",
        file=sys.stderr,
    )
    return True


def machines_command(args: argparse.Namespace, api: Any = None) -> int:
    """Machine enrolment — `mcp-hub machines list|enrol`.

    Enrolment had no verb at all, so the 2026-07-30 rollout was done with raw
    curl, and the machine token it returns ONCE was lost to a shell pipeline.
    Persisting the token is therefore the first thing this does with it.
    """
    from mcp_hub.operator_api import (
        MACHINE_TOKEN_FILE,
        ApiUnavailable,
        OperatorApi,
        api_base,
        write_machine_token,
    )

    if api is None:
        api = OperatorApi(api_base(args.hub_url))
    name = args.name or _sanitize_ident(platform.node() or "unknown-host")

    if args.action == "list":
        try:
            machines = api.list_machines()
        except ApiUnavailable as e:
            print(str(e), file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(machines, indent=2))
            return 0
        if not machines:
            print("no machines enrolled")
            return 0
        for m in machines:
            seen = m.get("last_seen")
            when = f"last seen {int(time.time() - seen)}s ago" if seen else "never seen"
            print(f"{m['name']:<20} {m.get('os', ''):<8} {when}")
        return 0

    if args.action == "rm":
        # Retiring a BOX, not a seat. The hub archives the row rather than
        # deleting it — placements and status reports name this machine, and
        # history that points at a machine nobody can look up is history
        # nobody can read.
        #
        # Named explicitly, never defaulted to this host: `enrol` defaults to
        # the local hostname because you can only enrol the box you are on,
        # but you retire a machine precisely when you are NOT on it (it is
        # dead, or being decommissioned), so a default here would retire the
        # wrong one on a bare `machines rm`.
        if not args.name:
            print("name the machine to retire — no default, because you "
                  "retire a box from somewhere else and a default would "
                  "retire THIS one", file=sys.stderr)
            return 1
        try:
            api.delete_machine(args.name)
        except ApiUnavailable as e:
            print(str(e), file=sys.stderr)
            return 1
        print(f"machine {args.name} retired — its token no longer works, and "
              f"its history stays readable under that name")
        print("any placements on it are NOT destroyed: reclaim them first if "
              "the box is gone, or they will sit pending-edge forever")
        return 0

    if args.action == "rotate":
        # The recovery path for a token that was never saved. Overwrites by
        # design — the whole point is that the file on disk is stale or
        # missing — so it does NOT honour the enrol-time --force guard.
        dest = pathlib.Path(args.token_file) if args.token_file \
            else MACHINE_TOKEN_FILE
        try:
            rec = api.rotate_machine_token(name)
        except ApiUnavailable as e:
            msg = str(e)
            if "404" in msg:
                print(f"machine '{name}' is not enrolled — `mcp-hub machines "
                      "enrol` first (rotate replaces a credential, it does not "
                      "create one)", file=sys.stderr)
                return 1
            print(msg, file=sys.stderr)
            return 1
        token = rec.get("token", "")
        if not token:
            print(f"the hub rotated '{name}' but returned no token — the old "
                  "one is now INVALID and the new one is unrecoverable",
                  file=sys.stderr)
            return 1
        where = write_machine_token(token, dest)   # persist BEFORE printing
        digest = hashlib.sha256(token.encode()).hexdigest()[:12]
        print(f"rotated '{name}' — the previous token is now invalid")
        print(f"machine token written to {where} (mode 0600, sha256 {digest})")
        if args.print_token:
            print(f"token: {token}")
        return 0

    # -- enrol ------------------------------------------------------------
    dest = pathlib.Path(args.token_file) if args.token_file else MACHINE_TOKEN_FILE
    if dest.exists() and not args.force:
        print(f"{dest} already exists — refusing to overwrite a machine token "
              "that may be the only copy (pass --force if you mean it)",
              file=sys.stderr)
        return 1
    try:
        rec = api.enrol_machine(name, args.os, None)
    except ApiUnavailable as e:
        msg = str(e)
        if "409" in msg:
            print(f"machine '{name}' is already enrolled. The hub stores only a "
                  "token HASH and has no rotation endpoint, so its original "
                  "token cannot be re-issued — the operator token works for "
                  "the machine endpoints in the meantime.", file=sys.stderr)
            return 1
        print(msg, file=sys.stderr)
        return 1

    token = rec.get("token", "")
    if not token:
        print(f"enrolled '{name}' but the hub returned no token — nothing to "
              "save, and it cannot be requested again", file=sys.stderr)
        return 1
    where = write_machine_token(token, dest)      # persist BEFORE printing
    digest = hashlib.sha256(token.encode()).hexdigest()[:12]
    print(f"enrolled '{name}' ({rec.get('os', '')})")
    print(f"machine token written to {where} (mode 0600, sha256 {digest})")
    if args.print_token:
        print(f"token: {token}")
    else:
        print("(pass --print-token to display it; it cannot be retrieved later)")
    return 0


# A brief travels through the hub's database as text, so it has a size the
# operator should hear about rather than discover. Docker's own env limit is
# the real ceiling (~128KB for the whole environment); this leaves generous
# room for it plus everything else the seat carries.
MAX_BRIEF_BYTES = 64 * 1024
MAX_INPUT_BYTES = 256 * 1024


def _read_brief_and_inputs(
    args: argparse.Namespace,
) -> tuple[str, dict[str, str], str]:
    """`--brief`/`--input` → (brief, {name: content}, error).

    ⚠️ THE CONTROL PLANE HOLDS NO SECRETS. Everything here is stored in the
    hub's SQLite in plaintext and readable by anything holding the operator
    token — the same reason `--env-from-host` passes a NAME and never a value.
    A brief is meant to be a question and a spec; it must never be a place
    someone pastes a key, so the refusal below is worth its false positives.
    """
    from mcp_hub.spec_guard import scan_secret

    brief = getattr(args, "brief", "") or ""
    if brief.startswith("@"):
        path = pathlib.Path(brief[1:]).expanduser()
        try:
            brief = path.read_text(encoding="utf-8")
        except OSError as e:
            return "", {}, f"cannot read brief from {path}: {e}"
    if len(brief.encode("utf-8")) > MAX_BRIEF_BYTES:
        return "", {}, (
            f"brief is {len(brief.encode('utf-8'))} bytes, over the "
            f"{MAX_BRIEF_BYTES} limit — put the bulk in `--input` files and "
            f"keep the brief the instruction that points at them"
        )

    inputs: dict[str, str] = {}
    for raw in (getattr(args, "input", None) or []):
        path = pathlib.Path(raw).expanduser()
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return "", {}, (
                f"{path} is not UTF-8 text. Inputs travel as text through the "
                f"hub; ship binary material by mounting a volume instead."
            )
        except OSError as e:
            return "", {}, f"cannot read input {path}: {e}"
        if len(content.encode("utf-8")) > MAX_INPUT_BYTES:
            return "", {}, (
                f"{path} is over the {MAX_INPUT_BYTES}-byte input limit — "
                f"mount a volume for material this size"
            )
        if path.name in inputs:
            # Two files of one name would silently become one, and the agent
            # would work from whichever won without ever knowing the other
            # existed.
            return "", {}, (
                f"two inputs are both named '{path.name}' — they land in one "
                f"directory, so rename one"
            )
        found = scan_secret(content, f"input '{path.name}'")
        if found:
            return "", {}, found
        inputs[path.name] = content
    # THE REFUSAL THE DOCSTRING ABOVE HAS PROMISED SINCE BRIEFS SHIPPED. It
    # did not exist: the invariant was documented and enforced nowhere (W2.3).
    # The hub validates too — this is not the gate, it is the fast, friendly
    # half of it, so an operator hears about a pasted key before a round trip.
    found = scan_secret(brief, "brief")
    if found:
        return "", {}, found
    return brief, inputs, ""


def seats_command(args: argparse.Namespace, api: Any = None) -> int:
    """Seats — WHAT may run. `mcp-hub seats list|add|rm`."""
    from mcp_hub.operator_api import ApiUnavailable, OperatorApi, api_base

    if api is None:
        api = OperatorApi(api_base(args.hub_url))
    machine = args.machine or _sanitize_ident(platform.node() or "unknown-host")
    if refuse_unhonoured_dry_run(args, ("list", "logs", "add", "rm",
                                        "update", "clone")):
        return 1

    try:
        if args.action == "list":
            rows = api.list_seats()
            if args.json:
                print(json.dumps(rows, indent=2))
                return 0
            if not rows:
                print("no seats declared — nothing can be placed until one is")
                return 0
            for s in rows:
                print(f"{s['identity']:<34} {s.get('machine', ''):<16} "
                      f"{s.get('class', ''):<8} {s.get('folder', '')}")
            return 0

        if args.action == "add":
            # A container is named by its IMAGE; only a worktree unit needs a
            # folder on this host — or a repo. Demanding either of nginx would
            # be a lie: an inference server or a web app has no git remote,
            # and the image IS the thing that says what will run.
            # EVERY missing requirement at once. Reporting them one at a time
            # makes the operator fix one to discover the next, which for a
            # two-field refusal is two round trips for no reason.
            problems = []
            if not args.image:
                if not args.folder:
                    problems.append(
                        "--folder — the path on that machine; without it there "
                        "is nothing to enrol")
                # A folder with NO git remote is a first-class agent here
                # (`squad add-folder`), so a repo is not required — but the
                # name has to come from somewhere, and deriving it from a
                # basename is the drift that makes a clone's statusline read
                # `hub ?`. Name it explicitly instead.
                if not args.repo and not args.want_identity:
                    problems.append(
                        "--repo or --identity — repo is only the source of a "
                        "derived NAME, so a folder with no git remote just "
                        "needs naming")
            if problems:
                print("cannot declare this seat:", file=sys.stderr)
                for p in problems:
                    print(f"  {p}", file=sys.stderr)
                return 1
            # HEADLESS as a first-class flag, not tribal --env knowledge. The
            # checks mirror the runtime gates (seat-entry's door, the edge's
            # materialize skip) at the EARLIEST moment — declaration — where
            # the fix is one flag away instead of a dead container later.
            # getattr throughout: hand-built Namespaces predate these flags.
            mode = getattr(args, "mode", "") or ""
            if mode == "headless":
                refuse = ""
                if not args.image:
                    refuse = ("headless is a container mode — name an --image "
                              "(SEAT_MODE means nothing to a worktree seat)")
                elif getattr(args, "agent", None) and getattr(
                        args, "prompt", ""):
                    # ⚠️ This used to refuse headless+pod OUTRIGHT, mirroring
                    # the runtime door. When that door narrowed to "the PROMPT
                    # is what cannot address N agents" (briefs are per-agent
                    # and always worked for pods), a declaration-time check
                    # left as-is would refuse a placement the runtime now
                    # accepts — the mirror lying about the thing it mirrors.
                    # Narrowed in step, deliberately, not by coincidence.
                    refuse = ("--prompt is single-valued and this is a pod — "
                              "one prompt cannot address several agents. Use "
                              "--brief, which every inhabitant reads")
                elif getattr(args, "agent", None) and not getattr(
                        args, "brief", ""):
                    refuse = ("a headless pod needs --brief — every agent "
                              "needs an instruction, and one with none runs a "
                              "turn that does nothing and exits, which reads "
                              "as a crash")
                elif (not getattr(args, "agent", None)
                        and not getattr(args, "prompt", "")
                        and not getattr(args, "brief", "")):
                    refuse = ("headless needs --prompt or --brief — a "
                              "one-shot claude with no instruction does "
                              "nothing and exits, which reads as a crash")
                elif not args.memory_volume:
                    refuse = ("headless needs --memory-volume — the result "
                              "artifact is written there because it survives "
                              "NOTHING else (docker logs die with rm, "
                              "exec-harvest refuses on an exited container)")
                if refuse:
                    print(refuse, file=sys.stderr)
                    return 1
            spec: dict[str, Any] = {}
            if args.image:
                spec["image"] = args.image
                env: dict[str, str] = dict(
                    kv.split("=", 1) for kv in (args.env or []) if "=" in kv)
                if mode:
                    env["SEAT_MODE"] = mode
                if getattr(args, "prompt", ""):
                    env["SEAT_PROMPT"] = args.prompt
                if getattr(args, "timeout", None) is not None:
                    env["SEAT_TIMEOUT"] = str(args.timeout)
                if env:
                    spec["env"] = env
                if args.port:
                    spec["ports"] = list(args.port)
                if args.volume:
                    spec["volumes"] = list(args.volume)
                if args.network:
                    spec["network"] = args.network
                if args.env_from_host:
                    spec["env_from_host"] = list(args.env_from_host)
                if args.memory_volume:
                    spec["memory_volume"] = args.memory_volume
                if args.command:
                    spec["command"] = shlex.split(args.command)
                # A POD: several agents in one container
                # (docs/n-seats-per-container.md). Declared here rather than
                # inferred, because how many agents a container holds is not
                # something any other field implies.
                # getattr, because callers build this Namespace by hand —
                # the API tests do, and a new flag must not break a caller
                # that predates it.
                if getattr(args, "agent", None):
                    spec["agents"] = _parse_pod_agents(args.agent)
                    if getattr(args, "pod_squad", ""):
                        spec["squad"] = args.pod_squad
                brief, inputs, err = _read_brief_and_inputs(args)
                if err:
                    print(err, file=sys.stderr)
                    return 1
                if brief:
                    spec["brief"] = brief
                if inputs:
                    spec["inputs"] = inputs
            what = f"docker ({args.image})" if args.image else "worktree"
            if args.dry_run:
                # The identity is deliberately NOT predicted here. The hub
                # ASSIGNS it (runtime design: a container's hostname must
                # never name a seat), so re-deriving it client-side would be a
                # second implementation free to disagree with the first — and
                # a dry run that prints a name the real add then does not use
                # is its own small lie.
                print(f"would declare a seat on {machine} [{what}]")
                for label, val in (("repo", args.repo),
                                   ("folder", args.folder),
                                   ("identity", args.want_identity),
                                   ("class", args.klass),
                                   ("launch args", args.launch_args)):
                    if val:
                        print(f"  {label:<12} {val}")
                if spec:
                    print(f"  {'spec keys':<12} {', '.join(sorted(spec))}")
                if not args.want_identity:
                    print("  identity is ASSIGNED by the hub, so it is not "
                          "predicted here")
                return 0
            rec = api.create_seat(args.repo, machine, args.folder,
                                  args.want_identity, args.launch_args,
                                  args.klass, spec)
            print(f"seat {rec['identity']} declared on {rec.get('machine', '')}"
                  f"  [{what}]")
            print("it will not run until it is PLACED: "
                  f"mcp-hub placements set --seat {rec['identity']} "
                  f"--machine {rec.get('machine', '')}")
            return 0

        if args.action == "logs":
            return _seat_logs(args, api, machine)

        if args.action == "update":
            if not args.identity:
                print("name the seat to update", file=sys.stderr)
                return 1
            brief, inputs, err = _read_brief_and_inputs(args)
            if err:
                print(err, file=sys.stderr)
                return 1
            spec: dict[str, Any] = {}
            if brief:
                spec["brief"] = brief
            if inputs:
                spec["inputs"] = inputs
            for flag, key in (("prompt", "SEAT_PROMPT"),):
                if getattr(args, flag, ""):
                    spec.setdefault("env", {})[key] = getattr(args, flag)
            if getattr(args, "timeout", None) is not None:
                spec.setdefault("env", {})["SEAT_TIMEOUT"] = str(args.timeout)
            if not spec and not args.launch_args:
                print("nothing to change — pass --brief, --input, --prompt, "
                      "--timeout or --launch-args", file=sys.stderr)
                return 1
            if args.dry_run:
                print(f"would update {args.identity}: "
                      f"{', '.join(sorted(spec)) or 'launch args'}")
                return 0
            api.update_seat(args.identity, spec or None,
                            args.launch_args or None)
            changed = ", ".join(sorted(spec)) or "launch args"
            print(f"seat {args.identity} updated ({changed})")
            # The edit changes the DECLARATION; a running container still
            # holds the old brief. Saying so is the difference between an
            # operator who restarts it and one who waits for a change that
            # will never arrive.
            print("the running container still has the OLD brief — "
                  "reclaim and re-place it to pick this up")
            return 0

        if args.action == "clone":
            if not args.identity:
                print("name the seat to clone", file=sys.stderr)
                return 1
            if not args.clone_suffix:
                print("--as <suffix> required — the clone needs its own "
                      "identity, or it would collide with the original",
                      file=sys.stderr)
                return 1
            if args.dry_run:
                print(f"would clone {args.identity} → "
                      f"{args.identity}-{args.clone_suffix}")
                return 0
            rec = api.clone_seat(args.identity, args.clone_suffix,
                                 args.machine or "")
            print(f"seat {rec['identity']} cloned from {args.identity}")
            print("it is a DECLARATION, not a running thing — place it:\n"
                  f"  mcp-hub placements set --seat {rec['identity']} "
                  f"--machine {rec.get('machine', '')}")
            return 0

        if args.action == "restore":
            if not args.identity:
                print("name the seat to restore", file=sys.stderr)
                return 1
            rec = api.restore_seat(args.identity)
            print(f"seat {rec['identity']} restored — exactly as archived "
                  "(archive freezes; restore reconstructs nothing)")
            return 0

        if not args.identity:
            print("name the seat to archive", file=sys.stderr)
            return 1
        purge = getattr(args, "purge", False)
        if purge and not getattr(args, "yes", False):
            # Nothing dies unnamed: the destructive verb states what it will
            # destroy and waits for the explicit confirmation.
            print(f"--purge DELETES seat '{args.identity}' outright (the "
                  "event trail keeps the death-fact; the declaration is "
                  "gone). Re-run with --yes to confirm.", file=sys.stderr)
            return 1
        if args.dry_run:
            # This branch used to fall straight through to delete_seat, so
            # `seats rm --dry-run` ARCHIVED the seat. A dry run that deletes
            # is the worst instance of the whole class.
            if purge:
                print(f"would PURGE seat {args.identity} — row deleted, "
                      "death-fact kept; refused while any placement row "
                      "still references it")
            else:
                print(f"would archive seat {args.identity} "
                      "(the worktree is untouched; refused if it still has "
                      "active placements)")
            return 0
        if purge:
            api.delete_seat(args.identity, purge=True)
            print(f"seat {args.identity} purged — the death-fact survives "
                  "in its event trail")
            return 0
        api.delete_seat(args.identity)
        print(f"seat {args.identity} archived (the worktree is untouched; "
              f"`mcp-hub seats restore {args.identity}` undoes this)")
        return 0
    except ApiUnavailable as e:
        msg = str(e)
        if "409" in msg and args.action == "rm":
            print(f"{args.identity} still has active placements — reclaim them "
                  "first, or the fleet keeps placements naming a seat that is "
                  "gone", file=sys.stderr)
            return 1
        print(msg, file=sys.stderr)
        return 1


def _seat_logs(args: argparse.Namespace, api: Any, machine: str) -> int:
    """What a seat has printed. `mcp-hub seats logs <identity>`.

    🔴 The dead end this closes: a seat could be declared, placed, realized and
    reclaimed without the operator ever having a way to READ WHAT IT SAID. For
    an interactive seat you could attach; for a headless one — the whole point
    of which is that nobody is watching — the output existed only in the
    container's log and the container was then destroyed by reclaim. Work with
    no retrievable result is work that did not happen.

    Machine-local by necessity and it SAYS SO. `docker logs` can only be run
    where the container is, and the honest failure is "that seat is on
    dev-vm-1, run it there" — not an empty result that reads like a seat which
    printed nothing. Guessing would be worse than refusing: an operator who
    believes a seat produced no output stops looking.
    """
    if not args.identity:
        print("name the seat (mcp-hub seats list)", file=sys.stderr)
        return 1
    placements = [p for p in api.list_placements()
                  if p.get("seat") == args.identity]
    if not placements:
        print(f"{args.identity} has no placement — it was declared but never "
              f"placed, so nothing has ever run and there is no output.\n"
              f"  mcp-hub placements set --seat {args.identity} "
              f"--machine {machine}", file=sys.stderr)
        return 1
    here = [p for p in placements if p.get("machine") == machine]
    if not here:
        where = sorted({p.get("machine", "?") for p in placements})
        print(f"{args.identity} runs on {', '.join(where)}, not {machine}. "
              f"Logs come from docker on the machine that holds the "
              f"container, so run this there:\n"
              f"  ssh {where[0]} mcp-hub seats logs {args.identity}",
              file=sys.stderr)
        return 1
    argv = ["docker", "logs"]
    if str(args.tail).lower() != "all":
        argv += ["--tail", str(args.tail)]
    if args.follow:
        argv.append("--follow")
    argv.append(args.identity)
    try:
        probe = subprocess.run(
            ["docker", "ps", "-a", "--filter", f"name=^{args.identity}$",
             "--format", "{{.Names}}"],
            capture_output=True, text=True,
        )
        if probe.returncode == 0 and args.identity not in \
                probe.stdout.split():
            # The container is GONE — reclaimed, or removed by hand. For a
            # headless seat that is the NORMAL end state, and its output was
            # written to the memory volume before exit precisely so this
            # moment still has an answer. Read it from there, through the
            # seat's own image (guaranteed pullable here: the seat ran here).
            return _seat_logs_artifact(api, args.identity)
        # Streamed, not captured: --follow must reach the operator live, and a
        # long log should not be buffered whole before its first line shows.
        return subprocess.run(argv).returncode
    except FileNotFoundError:
        print("docker is not on PATH here — this is the machine that holds "
              "the container, so it should be", file=sys.stderr)
        return 1


def _seat_logs_artifact(api: Any, identity: str) -> int:
    """A reclaimed headless seat's output, read from its memory volume.

    The volume is the ONE place the result survives the container: docker
    logs die with `docker rm`, and reclaim's exec-harvest cannot touch an
    exited container (both measured 2026-08-08). Read via a throwaway
    container on the seat's own image — the volume's mountpoint under
    /var/lib/docker is root-owned, so the docker daemon is the only reader
    this user actually has.

    Every dead end names its cause: "no artifact" without saying WHY reads
    like a seat that printed nothing, and an operator who believes that
    stops looking.
    """
    from mcp_hub.seat import HEADLESS_RESULTS_SUBDIR

    spec = {}
    for s in api.list_seats():
        if s.get("identity") == identity:
            spec = s.get("spec") or {}
            break
    memvol = str(spec.get("memory_volume") or "")
    image = str(spec.get("image") or "")
    if not memvol:
        print(f"{identity}: container is gone and the seat was declared "
              f"WITHOUT a memory volume — so there is no artifact, not "
              f"because it printed nothing but because nothing durable "
              f"existed to write to. (Headless seats now refuse to start "
              f"like this; this one predates that or never ran.)",
              file=sys.stderr)
        return 1
    if not image:
        print(f"{identity}: container is gone and its spec names no image — "
              f"nothing to read the volume with.", file=sys.stderr)
        return 1
    vol = memvol.split(":")[0]
    # The volume mounts AT ~/.claude, so from the volume's root the artifact
    # is under seat-results/ directly — no .claude prefix.
    base = f"/artifact/{HEADLESS_RESULTS_SUBDIR}/{identity}"
    script = (
        f'if [ ! -d "{base}" ]; then '
        f'echo "no headless artifact on volume \'{vol}\' — the seat never '
        f'ran to completion on an image that writes one" >&2; exit 9; fi; '
        f'cat "{base}/output.log" 2>/dev/null; '
        f'if [ -f "{base}/result.json" ]; then '
        f'echo; echo "--- result.json ---"; cat "{base}/result.json"; fi'
    )
    return subprocess.run(
        ["docker", "run", "--rm", "-v", f"{vol}:/artifact:ro",
         "--entrypoint", "sh", image, "-c", script]
    ).returncode


def _report_leftovers(api: Any, seat: str, machine: str) -> None:
    """After a reclaim, NAME what still describes the seat.

    🔴 Destroying the container removes ONE of five records. The other four —
    the seat declaration, the roster row, the workspace registration and the
    workspace file — survive by design, and nothing used to say so. The
    operator deleted four containers, watched the board still show them, and
    reasonably asked why they had not cleaned themselves up.

    ⚠️ This deliberately does NOT cascade. A seat OUTLIVING its placement is
    the whole point of splitting them — it is what lets an agent move machines
    without being re-declared — and the roster row and workspace file belong to
    that machine and to the operator, not to the hub. A hub that reached across
    and deleted operator-owned files because a container stopped would be worse
    than one that leaves them.

    ⇒ So: keep every layer's autonomy, remove the surprise. Turn a five-step
    ritual you had to KNOW into one the tool tells you.
    """
    if not seat:
        return
    todo: list[str] = []
    try:
        if any(s.get("identity") == seat for s in (api.list_seats() or [])):
            todo.append(f"seat declaration     mcp-hub seats rm {seat}")
    except Exception:  # noqa: BLE001 — advice must never fail the command
        pass
    try:
        reg = api.get_registry() or {}
        for w in reg.get("definitions", []):
            if machine and w.get("machine") not in ("", machine):
                continue
            if any(seat in str(x) for x in (w.get("listings") or [])):
                todo.append(
                    f"workspace {w['name']!r}    mcp-hub workspaces remove {w['name']}")
    except Exception:  # noqa: BLE001
        pass
    # Always named: this tool cannot see another machine's roster file, so
    # silence here would read as "nothing left" — the exact wrong inference.
    where = f" on {machine}" if machine else ""
    todo.append(f"roster row{where}   squad rm {seat}   (run there)")
    print(f"\n  the container will be gone; these still describe {seat}:")
    for line in todo:
        print(f"    {line}")


def _placements_unplace(args: argparse.Namespace, api: Any) -> int:
    """Drop a placement row and touch NOTHING on the machine.

    🔴 The gap this closes: a placement could not be un-made. `DELETE` on the
    API means *reclaim* — harvest, verify, destroy — which for a worktree seat
    runs `squad rm`, unenrolling the agent and opting its repo out of the hub.
    So the only way to stop the hub scheduling a seat was to demolish the
    agent, and a test placement written against a real roster agent could not
    be tidied away at all (2026-08-09).

    They are different intents and now they are different verbs:
      reclaim  — "this seat is finished"      → substrate destroyed
      unplace  — "the hub should stop caring" → substrate untouched

    Unplacing does NOT stop a running agent — it removes the policy, not the
    process — so a seat last observed RUNNING is refused without --yes. That
    leaves it alive and unmanaged, which is a legitimate thing to want (it is
    how you hand an agent back to `squad`) but never a thing to do by
    accident.
    """
    if not args.target:
        print("name the placement to unplace (mcp-hub placements list)",
              file=sys.stderr)
        return 1
    row = next((p for p in (api.list_placements() or [])
                if p.get("id") == args.target), None)
    if row is None:
        print(f"no placement '{args.target}'", file=sys.stderr)
        return 1
    seat = row.get("seat", "")
    machine = row.get("machine", "")
    observed = (row.get("observed") or {}).get("state") or "unknown"
    if args.dry_run:
        print(f"would unplace {args.target} ({seat} on {machine}) — the hub "
              f"forgets it; the substrate is untouched (last seen {observed})")
        return 0
    if observed == "running" and not args.yes:
        print(f"{seat} was last observed RUNNING on {machine}. Unplacing "
              "removes the policy, not the process — it would keep running "
              "with nothing scheduling it.\nStop it first:\n"
              f"  mcp-hub placements set {args.target} stopped\n"
              "or re-run with --yes to abandon it deliberately.",
              file=sys.stderr)
        return 1
    api.unplace_placement(args.target)
    print(f"{args.target} unplaced — {seat} is no longer scheduled on "
          f"{machine}; nothing there was changed")
    if observed == "running":
        print(f"⚠️  it was last seen RUNNING and is now unmanaged — "
              f"`squad stop {seat}` on {machine} if that is not what you want")
    return 0


MOVE_POLL_SECONDS = 5.0
MOVE_TIMEOUT_SECONDS = 300


def _placements_move(args: argparse.Namespace, api: Any) -> int:
    """Move a seat from one machine to another.

    An ORCHESTRATION, not a PATCH. `machine` is immutable on a placement and
    deliberately so: the naive alternative — create the same seat on B — is
    not a move at all, it is TWO live placements for one identity, both
    registering, the last one silently owning the wake binding. That is the
    collision `capsules place` already refuses by name, and nothing stopped
    you reaching it one placement at a time.

    So the sequence is reclaim-then-create, gated on OBSERVED completion:
      1. refuse a second live placement for the seat (the collision above)
      2. refuse a docker seat with no memory_volume unless --no-harvest —
         a silent move there loses everything the agent learned
      3. refuse a machine whose edge is not REPORTING, both ends
      4. reclaim on A, then wait until A's edge reports destroy done
      5. create on B only then — identity collision impossible by construction

    Exit codes are distinct because the middle one is resumable: 0 moved,
    1 refused (nothing written), 2 timed out (A was reclaimed, B was not
    created — finish by hand, and the message says how).
    """
    from mcp_hub.fleet_tree import _edge_state

    if not args.target:
        print("name the placement to move (mcp-hub placements list)",
              file=sys.stderr)
        return 1
    dst = getattr(args, "to", "") or ""
    if not dst:
        print("--to <machine> names where it should run instead",
              file=sys.stderr)
        return 1

    rows = api.list_placements() or []
    row = next((p for p in rows if p.get("id") == args.target), None)
    if row is None:
        print(f"no placement '{args.target}'", file=sys.stderr)
        return 1
    seat = row.get("seat", "")
    src = row.get("machine", "")
    substrate = row.get("substrate", "worktree")

    if dst == src:
        print(f"{seat} is already placed on {dst} — nothing to move",
              file=sys.stderr)
        return 1

    # 1. The collision this verb exists to make unreachable.
    others = [p for p in rows
              if p.get("seat") == seat and p.get("id") != args.target
              and p.get("desired") != "reclaimed"]
    if others:
        where = ", ".join(f"{p['id']} on {p['machine']}" for p in others)
        print(f"{seat} already has another live placement ({where}). Two live "
              f"placements for one identity means two containers registering, "
              f"and the last one silently owns the wake binding.\nResolve that "
              f"first:  mcp-hub placements reclaim <id> --yes   (or unplace)",
              file=sys.stderr)
        return 1

    # 2. Harvest is the whole reason a move is not a delete-and-recreate.
    spec: dict = {}
    try:
        spec = next((s.get("spec") or {} for s in (api.list_seats() or [])
                     if s.get("identity") == seat), {})
    except Exception:  # noqa: BLE001 — a missing seat is caught below
        spec = {}
    no_harvest = getattr(args, "no_harvest", False)
    if substrate == "docker" and not spec.get("memory_volume") and not no_harvest:
        print(f"{seat} is a docker seat with no memory_volume, so reclaim has "
              f"nothing to harvest — moving it now would destroy everything "
              f"the agent learned, silently.\nEither give it a volume first, "
              f"or accept the loss with --no-harvest.", file=sys.stderr)
        return 1

    # 3. Edge health, BOTH ends. The bar names the destination; the source is
    # what actually hangs the wait below — machine A offline means the reclaim
    # is never observed complete and step 4 can only time out. Checking one
    # would satisfy the letter of the bar and still strand the operator.
    machines = {}
    try:
        machines = {m.get("name"): m for m in (api.list_machines() or [])}
    except Exception:  # noqa: BLE001
        machines = {}
    now = time.time()
    for label, name in (("destination", dst), ("source", src)):
        state = _edge_state(machines.get(name), now)
        if state in (None, "never", "stale"):
            why = {
                None: f"the hub has no machine record for '{name}'",
                "never": f"'{name}' has never reported an edge pass",
                "stale": f"'{name}' has not reported an edge pass recently",
            }[state]
            print(f"{label} edge is not reporting — {why}.\nA move is only as "
                  f"real as the edge that realizes it; check "
                  f"`systemctl --user status mcp-hub-edge.timer` on {name} "
                  f"(that is nearly always where the fault is).",
                  file=sys.stderr)
            return 1
    failing = [n for n in (dst, src)
               if _edge_state(machines.get(n), now) == "failed"]

    if args.dry_run:
        print(f"would move {seat}: {src} -> {dst} ({substrate})")
        print(f"  1. reclaim {args.target} on {src} — harvest, verify, DESTROY")
        print(f"  2. wait for {src}'s edge to report destroy done "
              f"(up to {args.timeout}s)")
        print(f"  3. create a placement for {seat} on {dst}")
        if failing:
            print(f"  ⚠ edge is FAILING on {', '.join(failing)} — reporting, "
                  f"but reporting failure")
        return 0

    if not args.yes:
        print(f"moving {seat} RECLAIMS it on {src} first — harvest, verify, "
              f"then DESTROY the substrate. Re-run with --yes", file=sys.stderr)
        return 1

    if failing:
        print(f"⚠️  edge is FAILING on {', '.join(failing)} — it is reporting, "
              f"so this is a measurement rather than blindness, but the move "
              f"may not converge.")

    # 4. Reclaim, then wait for the edge's own absence verdict. Never for
    # `desired`, which would let the move mark itself done by wanting to.
    api.reclaim_placement(args.target)
    print(f"{args.target}: reclaim requested on {src} — waiting for its edge "
          f"to harvest, verify and destroy (polling every "
          f"{int(MOVE_POLL_SECONDS)}s, up to {args.timeout}s)")

    deadline = time.time() + args.timeout
    harvest_state = ""
    while True:
        cur = next((p for p in (api.list_placements() or [])
                    if p.get("id") == args.target), None)
        if cur is None:
            # The row is gone: nothing on A still claims the seat, which is
            # the condition step 5 needs.
            print(f"  {args.target} is gone from the hub — treating the "
                  f"reclaim as complete")
            break
        reclaim = cur.get("reclaim") or {}
        harvest_state = reclaim.get("harvest", "") or harvest_state
        if reclaim.get("destroy") == "done":
            print("  destroy reported done")
            break
        if time.time() >= deadline:
            print(f"\ntimed out after {args.timeout}s waiting for {src} to "
                  f"finish the reclaim (harvest={reclaim.get('harvest','?')} "
                  f"verify={reclaim.get('verify','?')} "
                  f"destroy={reclaim.get('destroy','?')}).\n"
                  f"NOTHING was created on {dst}, so this is resumable, not "
                  f"broken — the seat is mid-reclaim on {src} and creating it "
                  f"on {dst} now is the collision this verb refuses.\n"
                  f"Finish by hand once {src}'s edge has run:\n"
                  f"  mcp-hub placements list          # confirm destroy done\n"
                  f"  mcp-hub placements set --seat {seat} --machine {dst} "
                  f"--substrate {substrate}", file=sys.stderr)
            return 2
        print(f"  reclaim in progress "
              f"(harvest={reclaim.get('harvest','?')} "
              f"verify={reclaim.get('verify','?')} "
              f"destroy={reclaim.get('destroy','?')})")
        time.sleep(MOVE_POLL_SECONDS)

    # 5. Only now can B be created without an identity collision.
    rec = api.create_placement(seat, dst, substrate, "running")
    print(f"{rec['id']}: {seat} placed on {rec['machine']} -> running")
    print(f"moved {seat}: {src} -> {dst}")
    print("written to the hub. Nothing has happened on the destination yet — "
          f"{dst}'s `edge apply` realizes it and reports what it OBSERVED.")
    if substrate == "docker" and not no_harvest:
        print(f"  memory is staged hub-side by PROJECT, so re-attach it there:"
              f"\n    mcp-hub memory-import      (run on {dst})")

    # B6, named rather than discovered: the edge runs harvest -> verify ->
    # destroy unconditionally, so a harvest that failed did NOT stop the
    # destroy. Gating that changes reclaim semantics for every caller and is
    # deferred to its own decision; until then the risk is stated here.
    if harvest_state and harvest_state not in ("done", "pending"):
        print(f"\n⚠️  the harvest phase reported '{harvest_state}'. The edge "
              f"runs harvest -> verify -> destroy unconditionally, so the "
              f"destroy went ahead regardless — check what landed before "
              f"relying on {seat}'s memory on {dst}.")

    # B5: the seat declaration and workspace SHOULD outlive a move — that is
    # what makes moving machines possible at all. Only the source roster row
    # is genuinely left behind, so only that is reported.
    print(f"\n  one leftover on {src}:\n"
          f"    roster row   squad rm {seat}   (run there)")
    return 0


def placements_command(args: argparse.Namespace, api: Any = None) -> int:
    """Placements — WHERE a seat runs. `mcp-hub placements list|set|reclaim`.

    This is the verb that makes the fleet drivable from any node: it writes
    DESIRED state to the hub and returns. The named machine's `edge apply`
    is what makes it true, and `status` reports which of those has happened.
    """
    from mcp_hub.operator_api import ApiUnavailable, OperatorApi, api_base

    if api is None:
        api = OperatorApi(api_base(args.hub_url))
    if refuse_unhonoured_dry_run(
        args, ("list", "set", "reclaim", "unplace", "move")
    ):
        return 1

    try:
        if args.action == "list":
            rows = api.list_placements()
            if args.json:
                print(json.dumps(rows, indent=2))
                return 0
            if not rows:
                print("no placements — nothing is scheduled anywhere")
                return 0
            pending = 0
            for p in rows:
                obs = (p.get("observed") or {}).get("state") or "—"
                status = p.get("status", "")
                pending += status == "pending-edge"
                print(f"{p['id']:<14} {p['seat']:<30} {p['machine']:<16} "
                      f"want {p['desired']:<9} saw {obs:<9} {status}")
            if pending:
                # The single most likely cause, named rather than left to be
                # rediscovered: `edge apply` is a one-shot and nothing runs it
                # by default.
                print(f"\n{pending} pending-edge — no edge pass has reported "
                      "since these were written. Check that `mcp-hub edge "
                      "apply` actually runs on those machines.")
            return 0

        if args.action == "reclaim":
            if not args.target:
                print("name the placement to reclaim", file=sys.stderr)
                return 1
            if args.dry_run:
                print(f"would reclaim {args.target} "
                      "(harvest memory, verify, then DESTROY the substrate)")
                return 0
            if not args.yes:
                print("reclaim HARVESTS then DESTROYS the substrate; "
                      "re-run with --yes", file=sys.stderr)
                return 1
            seat_name, machine = "", ""
            for p in (api.list_placements() or []):
                if p.get("id") == args.target:
                    seat_name = p.get("seat", "")
                    machine = p.get("machine", "")
                    break
            api.reclaim_placement(args.target)
            print(f"{args.target}: reclaim requested — the machine's next edge "
                  "pass harvests memory, verifies, then destroys")
            _report_leftovers(api, seat_name, machine)
            return 0

        if args.action == "unplace":
            return _placements_unplace(args, api)

        if args.action == "move":
            return _placements_move(args, api)

        # -- set --------------------------------------------------------
        # `--seat` (create) and a placement id (amend) are two different
        # modes, and the id used to WIN SILENTLY: `set running --seat X`
        # bound "running" to `target`, so it read as "amend the placement
        # whose id is 'running'" and dropped --seat/--machine on the floor
        # without a word (measured 2026-08-09).
        # ORDER MATTERS: the mis-bind is a strict sub-case of "both modes
        # given", so the generic refusal would fire first and hide the one
        # message that actually explains what happened.
        if args.target in ("running", "stopped", "ran"):
            print(f"'{args.target}' is a desired state, not a placement id — "
                  f"argparse bound it to the id because it followed the "
                  f"action.\nUse:  mcp-hub placements set --seat <seat> "
                  f"--machine <machine> --desired {args.target}",
                  file=sys.stderr)
            return 1
        if args.target and args.seat:
            print("pass a placement id OR --seat, not both — an id amends an "
                  "existing placement, --seat creates a new one",
                  file=sys.stderr)
            return 1
        # Both spellings converge; disagreement is refused rather than
        # resolved by precedence, because picking one silently is how the
        # wrong state gets written. getattr: callers build this Namespace by
        # hand (the API tests do) and predate the flag.
        desired_flag = getattr(args, "desired_flag", None)
        if desired_flag and args.desired and desired_flag != args.desired:
            print(f"--desired {desired_flag} contradicts the positional "
                  f"'{args.desired}' — pass one", file=sys.stderr)
            return 1
        desired = desired_flag or args.desired or "running"
        if desired not in ("running", "stopped", "ran"):
            print("desired must be running, stopped, or ran — `ran` is the "
                  "headless ask: run ONCE, ever, and never restart (reclaim "
                  "is its own verb, because it destroys)", file=sys.stderr)
            return 1
        if args.target:
            if args.dry_run:
                print(f"would set {args.target} -> {desired}")
                return 0
            rec = api.set_placement(args.target, desired)
            print(f"{rec['id']}: {rec['seat']} on {rec['machine']} -> {desired}")
        else:
            if not args.seat:
                print("pass a placement id, or --seat with --machine to create one",
                      file=sys.stderr)
                return 1
            machine = args.machine or _sanitize_ident(
                platform.node() or "unknown-host")
            if args.dry_run:
                print(f"would place {args.seat} on {machine} "
                      f"({args.substrate}) -> {desired}")
                return 0
            rec = api.create_placement(args.seat, machine, args.substrate, desired)
            print(f"{rec['id']}: {rec['seat']} placed on {rec['machine']} -> {desired}")
        print("written to the hub. Nothing has happened yet — that machine's "
              "`edge apply` realizes it and reports what it OBSERVED.")
        return 0
    except ApiUnavailable as e:
        print(str(e), file=sys.stderr)
        return 1


def _capsule_attach(args: argparse.Namespace, api: Any) -> int:
    """Make a placed capsule OPENABLE on this machine.

    `place` writes desired state and the edge makes containers exist; neither
    gives the operator a way in. This adds the roster row and the workspace
    folder entry per seat, so the cockpit shows a tab that attaches to the
    container — the same tab any other agent gets.

    It must run ON the machine hosting the seats: the folders, the roster and
    the hostname are all destination facts, which is the same reason
    `transport-recv` exists rather than the source doing the wiring.

    `squad` stays the only writer of the roster and of workspace files. A
    second writer of a hand-formatted JSONC file is how comments get destroyed,
    and a second writer of squad.conf is how two views of the fleet come to
    disagree.
    """
    from mcp_hub.seat import (
        ATTACH_ENROL,
        ATTACH_PRESENT,
        ATTACH_REFUSE,
        capsule_attach_plan,
    )

    if not args.target:
        print("name the capsule to attach (see `mcp-hub capsules list`)",
              file=sys.stderr)
        return 1
    rec = next((c for c in api.list_capsules() if c["id"] == args.target), None)
    if rec is None:
        print(f"no capsule {args.target}", file=sys.stderr)
        return 1
    seats = (rec.get("manifest") or {}).get("seats") or []
    machine = _sanitize_ident(platform.node() or "unknown-host")
    enrolled = {r["agent"] for r in _roster_all()}
    plan = capsule_attach_plan(seats, machine, enrolled, os.path.isdir)
    if not plan:
        print(f"{args.target}: the capsule froze no seats")
        return 0

    ws = getattr(args, "workspace", None)
    squad_bin = _resolve_tool("squad") or "squad"
    refused = [p for p in plan if p[1] == ATTACH_REFUSE]
    todo = [p for p in plan if p[1] == ATTACH_ENROL]

    # ONE execution path, two RENDERINGS. The cockpit needs the plan as data —
    # a UI parsing rendered lines is how the board came to attribute agents by
    # repo basename — but a json branch that also re-implemented the enrol loop
    # would be a second path free to drift from the guarded one, which is
    # exactly how merging a safe and an unsafe variant drops the guard.
    as_json = getattr(args, "json", False)
    if as_json:
        print(json.dumps({
            "capsule": args.target,
            "machine": machine,
            "workspace": ws,
            "dry_run": bool(args.dry_run),
            "plan": [{"identity": i, "action": a, "folder": d}
                     for i, a, d in plan],
            # Named separately because "nothing to enrol" and "refused, so
            # nothing was written" are opposite outcomes that both leave the
            # enrol list empty.
            "refused": [{"identity": i, "reason": d} for i, a, d in refused],
            "enrol": [{"identity": i, "folder": d} for i, _a, d in todo],
        }, indent=2))
    else:
        for ident, action, detail in plan:
            mark = {ATTACH_ENROL: "enrol ", ATTACH_PRESENT: "have  ",
                    ATTACH_REFUSE: "REFUSE"}.get(action, "skip  ")
            print(f"  {mark} {ident:<28} {detail}")
    if refused:
        # A missing bind-mount source is the FIRST of the six gates between a
        # running container and an agent on the hub, and docker hides it by
        # creating the directory as root. Refusing the whole attach keeps the
        # capsule all-or-nothing rather than half-wired.
        #
        # Shared by BOTH renderings on purpose: this goes to stderr, so stdout
        # stays pure JSON for the cockpit while the operator running it by hand
        # still gets the sentence. An `if as_json: return 1` short-circuit here
        # was measured to change nothing — a mutation test kept passing with it
        # gutted, which is the definition of a line that isn't doing work.
        print(f"\n{len(refused)} seat(s) refused — nothing was written. Their "
              "work folders must exist first (docker would create them as "
              "root and the seat runs as uid 1000).", file=sys.stderr)
        return 1
    if args.dry_run:
        if not as_json:
            print(f"\ndry run — {len(todo)} seat(s) would be enrolled")
        return 0
    if not todo:
        if not as_json:
            print("\nnothing to do — every seat is already enrolled")
        return 0
    if ws:
        ws = str(pathlib.Path(ws).expanduser())
        if not os.path.exists(ws):
            subprocess.run([squad_bin, "ws-new", ws], check=False)
    for ident, _action, folder in todo:
        argv = [squad_bin, "add-container", ident, folder, ident]
        if ws:
            argv += ["--to", ws]
        r = subprocess.run(argv, capture_output=True, text=True)
        out = (r.stdout or r.stderr).strip().splitlines()
        if not as_json:
            print(f"  {ident}: {out[0] if out else 'enrolled'}")
    if not as_json:
        print(f"\n{len(todo)} seat(s) enrolled. Open {ws or 'the workspace'} "
              "on this machine and each tab attaches to its container.")
    return 0


def parse_until(spec: str, now: float | None = None) -> float:
    """`+7d` / `+12h` / `+90m` / `2026-09-01` → a unix deadline. 0 for empty.

    Relative forms exist because that is how the need is actually expressed —
    "lend me Alice for the week" — and a wrong absolute date is silent where a
    wrong relative one is not. Raises ValueError with the accepted forms named,
    so a typo cannot be read as "no deadline": defaulting a malformed deadline
    to 0 would turn a loan into a permanent membership, which is the exact
    outcome the deadline exists to prevent.
    """
    spec = (spec or "").strip()
    if not spec:
        return 0.0
    now = time.time() if now is None else now
    if spec.startswith("+"):
        unit, body = spec[-1].lower(), spec[1:-1]
        mult = {"m": 60, "h": 3600, "d": 86400, "w": 604800}.get(unit)
        if mult is None or not body.strip():
            raise ValueError(
                f"cannot read '{spec}' as a duration — use +90m, +12h, +7d "
                f"or +2w"
            )
        try:
            n = float(body)
        except ValueError:
            raise ValueError(
                f"cannot read '{spec}' as a duration — '{body}' is not a "
                f"number"
            ) from None
        if n <= 0:
            raise ValueError(f"'{spec}' is not in the future")
        return now + n * mult
    try:
        # Midnight ENDING that day, so `--until 2026-09-01` includes the 1st.
        # The other reading silently shortens every loan by a day, and the
        # operator only finds out when someone stops hearing a squad.
        d = _dt.datetime.strptime(spec, "%Y-%m-%d")
    except ValueError:
        raise ValueError(
            f"cannot read '{spec}' as a deadline — use +7d, +12h, +90m or "
            f"YYYY-MM-DD"
        ) from None
    return d.timestamp() + 86400


def _fmt_until(expires: float, now: float | None = None) -> str:
    """A deadline as time REMAINING. An absolute timestamp makes the reader do
    the subtraction, and the question is always 'how long left'."""
    if not expires:
        return ""
    left = expires - (time.time() if now is None else now)
    if left <= 0:
        return "expired"
    for n, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if left >= n:
            return f"{left / n:.0f}{unit} left"
    return "<1m left"


def squads_command(args: argparse.Namespace, api: Any = None) -> int:
    """Squads — WHO a team is. `mcp-hub squads list|create|fork|merge|...`.

    🔴 The gap this closes, found by the operator asking the seven
    team-assembly scenarios against the CLI: five of them needed to read or
    change squad membership, and NONE of them could. `/api/v1/squads` and its
    members routes had been complete since the runtime shipped; the only CLI
    door was a side-effect flag on `capsules compose --register`. Membership
    was otherwise something only an agent could do TO ITSELF, via the MCP
    `set_squads` — so the operator's own team structure was the one thing the
    operator's CLI could not touch.
    """
    from mcp_hub.operator_api import ApiUnavailable, OperatorApi, api_base

    if api is None:
        api = OperatorApi(api_base(args.hub_url))
    act = args.action

    # The two spellings converge here, so nothing downstream learns there were
    # ever two. Order preserved, duplicates dropped — naming a seat twice is a
    # typo, not a request to add it twice.
    named = list(args.members or [])
    named += [s.strip() for s in (getattr(args, "members_flag", "") or "")
              .split(",") if s.strip()]
    seen: set[str] = set()
    args = argparse.Namespace(**{**vars(args), "members": [
        s for s in named if not (s in seen or seen.add(s))]})

    def _need(value: str, what: str) -> bool:
        if not value:
            print(what, file=sys.stderr)
            return True
        return False

    try:
        if act == "list":
            rows = api.list_api_squads()
            if args.json:
                print(json.dumps(rows, indent=2))
                return 0
            if not rows:
                print("no squads registered for management.\n"
                      "  mcp-hub squads create <name>")
                return 0
            for s in rows:
                print(f"{s['name']:<24} {s.get('member_count', 0):>3} member(s)"
                      f"  {s.get('description', '')}")
            return 0

        if act == "create":
            if _need(args.name, "name the squad to create"):
                return 1
            if args.dry_run:
                print(f"would create squad '{args.name}'")
                return 0
            api.create_api_squad(args.name, args.description)
            print(f"squad '{args.name}' created — it has no members yet:\n"
                  f"  mcp-hub squads add {args.name} <seat> [<seat>...]")
            return 0

        if act == "members":
            if _need(args.name, "name the squad"):
                return 1
            rows = api.list_squad_members(args.name)
            if args.json:
                print(json.dumps(rows, indent=2))
                return 0
            if not rows:
                print(f"'{args.name}' has no members — a broadcast to it "
                      f"reaches nobody")
                return 0
            for m in rows:
                flags = [x for x in (
                    "muted" if m.get("muted") else "",
                    _fmt_until(m.get("expires") or 0),
                ) if x]
                suffix = f"  ({', '.join(flags)})" if flags else ""
                print(f"{m['seat']:<34} {m.get('source', ''):<10}{suffix}")
            return 0

        if act in ("add", "remove"):
            if _need(args.name, "name the squad"):
                return 1
            if not args.members:
                print(f"name the seat(s) to {act}", file=sys.stderr)
                return 1
            expires = parse_until(args.until) if act == "add" else 0.0
            if args.dry_run:
                until = f" until {_fmt_until(expires)}" if expires else ""
                print(f"would {act} {', '.join(args.members)} "
                      f"{'to' if act == 'add' else 'from'} "
                      f"'{args.name}'{until}")
                return 0
            for seat in args.members:
                if act == "add":
                    api.add_squad_member(
                        args.name, seat, expires,
                        source="loan" if expires else "cli")
                    note = f" ({_fmt_until(expires)})" if expires else ""
                    print(f"{seat} → {args.name}{note}")
                else:
                    api.remove_squad_member(args.name, seat)
                    print(f"{seat} ✗ {args.name}")
            if act == "add" and expires:
                # Said out loud because a deadline that is merely RECORDED is
                # the failure mode this feature was built to avoid.
                print("\nthe loan ends by itself — after that they stop "
                      "hearing this squad on every delivery path, with no "
                      "action from you")
            return 0

        if act == "rename":
            if _need(args.name, "name the squad") or _need(
                    args.to, "--to <new-name> required"):
                return 1
            if args.dry_run:
                print(f"would rename '{args.name}' → '{args.to}'")
                return 0
            api.rename_api_squad(args.name, args.to)
            print(f"'{args.name}' → '{args.to}' — memberships and queued "
                  f"broadcasts moved with it")
            return 0

        if act == "rm":
            if _need(args.name, "name the squad to archive"):
                return 1
            if args.dry_run:
                print(f"would archive '{args.name}'"
                      + (" and drop its memberships" if args.purge else ""))
                return 0
            api.delete_api_squad(args.name, args.purge)
            # Two different acts, two different sentences. Purging while
            # printing the ARCHIVE message made the operator verify by hand
            # that the purge had happened at all (#168, 2026-08-26) — a
            # success string describing a weaker act than the one performed
            # is a lying receipt.
            if args.purge:
                print(f"squad '{args.name}' PURGED — runtime record and "
                      f"memberships removed, the name is free for reuse; "
                      f"message history is KEPT and stays readable")
            else:
                print(f"squad '{args.name}' archived — its message history "
                      f"is KEPT and stays readable under that name")
                print("memberships were left in place (--purge drops them)")
            return 0

        if act == "fork":
            return _squads_fork(args, api)
        return _squads_merge(args, api)
    except ValueError as e:            # a bad --until
        print(str(e), file=sys.stderr)
        return 1
    except ApiUnavailable as e:
        print(str(e), file=sys.stderr)
        return 1


def _squads_fork(args: argparse.Namespace, api: Any) -> int:
    """A topic splits: take some of a squad into a NEW one.

    THE SPIKE TEAM, as an operator actually describes it — "pull three of
    dreamteam onto this question". Naming no members forks the whole squad,
    which is the other real case (a squad splitting in two).

    The source is left ALONE. A fork that also removed the members would make
    'lend three people to a spike' impossible to express, and that is the more
    common need by far; leaving is a separate, deliberate `squads remove`.
    """
    if not args.name:
        print("name the squad to fork FROM", file=sys.stderr)
        return 1
    if not args.to:
        print("--to <new-squad> required — a fork needs somewhere to go",
              file=sys.stderr)
        return 1
    existing = {m["seat"] for m in api.list_squad_members(args.name)}
    if not existing:
        print(f"'{args.name}' has no members to fork", file=sys.stderr)
        return 1
    chosen = list(args.members) if args.members else sorted(existing)
    # Refuse rather than silently fork a subset of what was asked for: a
    # mistyped identity would otherwise produce a spike team quietly missing
    # the one person it was assembled for.
    unknown = [s for s in chosen if s not in existing]
    if unknown:
        print(f"not in '{args.name}': {', '.join(unknown)}\n"
              f"members are: {', '.join(sorted(existing))}", file=sys.stderr)
        return 1
    expires = parse_until(args.until)
    if args.dry_run:
        print(f"would fork {len(chosen)} of {len(existing)} member(s) from "
              f"'{args.name}' into '{args.to}':")
        for s in chosen:
            print(f"  {s}")
        if expires:
            print(f"  ...as a loan, {_fmt_until(expires)}")
        print(f"'{args.name}' would keep all {len(existing)} — a fork COPIES")
        return 0
    from mcp_hub.operator_api import ApiUnavailable
    try:
        api.create_api_squad(args.to, f"forked from {args.name}")
    except ApiUnavailable as e:
        if "409" not in str(e):
            raise
    for s in chosen:
        api.add_squad_member(args.to, s, expires,
                             source=f"fork:{args.name}")
    print(f"'{args.to}': {len(chosen)} member(s) forked from '{args.name}'"
          + (f", {_fmt_until(expires)}" if expires else ""))
    for s in chosen:
        print(f"  {s}")
    print(f"\n'{args.name}' is unchanged — a fork COPIES. Freeze and run the "
          f"new one with:\n"
          f"  mcp-hub capsules compose --squad {args.to} --register")
    return 0


def _squads_merge(args: argparse.Namespace, api: Any) -> int:
    """Two threads converge: fold one squad into another.

    The source is archived by default, because a merge that leaves both
    running is how a fleet ends up broadcasting to a squad nobody remembers is
    still alive. `--keep-source` is there for the case where it IS deliberate.
    """
    if not args.name:
        print("name the squad to merge FROM", file=sys.stderr)
        return 1
    if not args.into:
        print("--into <squad> required — name the squad that SURVIVES",
              file=sys.stderr)
        return 1
    if args.into == args.name:
        print("a squad cannot be merged into itself", file=sys.stderr)
        return 1
    src = {m["seat"] for m in api.list_squad_members(args.name)}
    dst = {m["seat"] for m in api.list_squad_members(args.into)}
    moving = sorted(src - dst)
    already = sorted(src & dst)
    if args.dry_run:
        print(f"would move {len(moving)} member(s) from '{args.name}' into "
              f"'{args.into}'")
        for s in moving:
            print(f"  {s}")
        for s in already:
            print(f"  {s}  (already there)")
        print(f"'{args.name}' would be "
              + ("KEPT" if args.keep_source else "archived"))
        return 0
    for s in moving:
        api.add_squad_member(args.into, s, 0.0, source=f"merge:{args.name}")
    # Deliberately permanent (expires=0): a loan that survived into the merged
    # squad would end there too, silently removing someone from a squad they
    # were merged into rather than lent to.
    if not args.keep_source:
        api.delete_api_squad(args.name, purge=True)
    print(f"'{args.into}': {len(moving)} moved in, {len(already)} already "
          f"there ({len(dst | src)} total)")
    print(f"'{args.name}': "
          + ("kept as-is" if args.keep_source
             else "archived — its history stays readable under that name"))
    return 0


def capsules_command(args: argparse.Namespace, api: Any = None) -> int:
    """Capsules — a whole SQUAD on docker. `mcp-hub capsules list|compose|place`.

    The machinery has been on the hub since the runtime shipped; what was
    missing was a verb, so the only way to start a squad on containers was
    curl with the operator token. A capability reachable only by curl is
    half-delivered.

    compose FREEZES a squad (every member's seat spec as it is now) and
    place writes one docker placement PER SEAT. Both are bookkeeping: the
    named machine's edge is what makes any of it true.
    """
    from mcp_hub.operator_api import ApiUnavailable, OperatorApi, api_base

    if api is None:
        api = OperatorApi(api_base(args.hub_url))
    # compose and place are NOT honoured: neither can be previewed without
    # asking the hub what it would freeze/place, and inventing a local guess
    # would be the second implementation this file keeps refusing to write.
    # Refusing is the honest half — `capsules compose --dry-run` used to
    # compose for real.
    if refuse_unhonoured_dry_run(args, ("list", "rm", "attach")):
        return 1

    try:
        if args.action == "list":
            rows = api.list_capsules()
            if args.json:
                print(json.dumps(rows, indent=2))
                return 0
            if not rows:
                print("no capsules — compose one to freeze a squad you can "
                      "place on any machine")
                return 0
            for c in rows:
                seats = len((c.get("manifest") or {}).get("seats") or [])
                print(f"{c['id']:<16} {c['squad']:<20} {seats:>2} seat(s)  "
                      f"{c.get('created', '')}")
            return 0

        if args.action == "rm":
            # 🔴 The gap the operator found by asking the right question:
            # "through the CLI, can we support any container or workspace or
            # squad scenario?" Every other registry could be emptied — seats
            # archive, placements reclaim, workspaces remove — and capsules
            # could only ever GROW. The server has had DELETE /capsules/{id}
            # all along; nothing reached it.
            if not args.target:
                print("name the capsule to remove (mcp-hub capsules list)",
                      file=sys.stderr)
                return 1
            if args.dry_run:
                print(f"would remove capsule {args.target} "
                      "(placements it already made are untouched)")
                return 0
            api.delete_capsule(args.target)
            # Said explicitly because the opposite is the natural fear, and it
            # is what would stop someone tidying: a capsule is a SNAPSHOT, not
            # a live link. `place` copies the manifest into per-seat
            # placements and nothing refers back, so removing one takes away
            # the ability to re-place that snapshot and nothing else.
            print(f"capsule {args.target} removed — anything it already "
                  "placed keeps running; you can no longer re-place THIS "
                  "snapshot (compose a fresh one from the live squad)")
            return 0

        if args.action == "compose":
            if not args.squad:
                print("--squad required — a capsule freezes ONE squad",
                      file=sys.stderr)
                return 1
            if args.register:
                # Explicit, never silent: a squad can exist for MESSAGING
                # (squad_members, who hears a broadcast) and be unknown to
                # the MANAGEMENT registry (api_squads, what the runtime can
                # place). Composing then 404s with the members sitting right
                # there. Registering is a real act, so it is a real flag.
                try:
                    api.create_api_squad(args.squad)
                    print(f"registered squad '{args.squad}' for management")
                except ApiUnavailable as e:
                    # 409 = already registered, which is success for our
                    # purposes; anything else is a real failure.
                    if "409" not in str(e):
                        raise
            rec = api.create_capsule(args.squad)
            seats = (rec.get("manifest") or {}).get("seats") or []
            print(f"{rec['id']}: froze squad '{rec['squad']}' — "
                  f"{len(seats)} seat(s)")
            for s in seats:
                image = (s.get("spec") or {}).get("image") or "—"
                print(f"  {s['identity']:<30} {image}")
            # Same trap as `seats add`: "composed" reads as "started".
            print("\nnothing is running — a capsule is INERT. Place it:\n"
                  f"  mcp-hub capsules place {rec['id']} --machine <machine>")
            return 0

        if args.action == "attach":
            return _capsule_attach(args, api)

        # -- place ------------------------------------------------------
        if not args.target:
            print("name the capsule to place (see `mcp-hub capsules list`)",
                  file=sys.stderr)
            return 1
        if not args.machine:
            # No default: placing a whole squad on the wrong box because a
            # hostname was assumed is not a mistake worth making convenient.
            print("--machine required — placing a squad is not a guess",
                  file=sys.stderr)
            return 1
        label = getattr(args, "as_label", "")
        rec = api.place_capsule(args.target, args.machine, label)
        ids = rec.get("placements") or []
        seats = rec.get("seats") or []
        print(f"{args.target}: {len(ids)} placement(s) written on "
              f"{args.machine}")
        for pid, seat in zip(ids, seats or [""] * len(ids)):
            print(f"  {pid}  {seat}")
        if label:
            # The label as the HUB sanitized it, not as it was typed: it is
            # lowercased and stripped of anything tmux would read as a
            # separator, so echoing the input would send the operator looking
            # for `-takeB` when the seats are named `-takeb`.
            actual = seats[0].rsplit("-", 1)[-1] if seats else label
            print(f"\nfresh identities under '-{actual}' — this is a SECOND "
                  f"squad, not the same one moved. Every pod inhabitant was "
                  f"re-identified too, so nothing collides on the hub.")
        print("\nnothing has happened yet — that machine's `edge apply` "
              "realizes them and reports what it OBSERVED.")
        return 0
    except ApiUnavailable as e:
        msg = str(e)
        if "409" in msg and args.action == "place" and not getattr(
                args, "as_label", ""):
            # The refusal already names the flag; repeating the whole command
            # saves the operator composing it from the prose.
            print(msg, file=sys.stderr)
            print(f"\n  mcp-hub capsules place {args.target} --machine "
                  f"{args.machine} --as <label>", file=sys.stderr)
            return 1
        print(msg, file=sys.stderr)
        return 1


def workspaces_command(args: argparse.Namespace, api: Any = None) -> int:
    """The workspace registry from the command line.

    `api` is injected by tests so none of this needs a socket.
    """
    from mcp_hub.edge import discover_workspaces
    from mcp_hub.operator_api import ApiUnavailable, OperatorApi, api_base
    from mcp_hub.workspace_data import collect_workspaces

    args.machine_given = args.machine is not None
    machine = args.machine or _sanitize_ident(platform.node() or "unknown-host")
    if api is None:
        api = OperatorApi(api_base(args.hub_url))
    scan_dirs = [pathlib.Path(d) for d in (getattr(args, "scan_dir", None) or [])] \
        or _workspace_scan_dirs()
    if refuse_unhonoured_dry_run(args, ("list", "register", "remove")):
        return 1

    if args.action == "remove":
        return _workspaces_remove(args, api, machine)

    if args.action == "list":
        data = collect_workspaces(api, scan_dirs, machine)
        if args.json:
            print(json.dumps(data, indent=2))
            return 0
        if data["note"]:
            print(data["note"])
        if not data["rows"]:
            print("no workspaces found anywhere")
            return 0
        # Grouped by machine, same shape as the board's `w` view — this is
        # meant to be the same picture in a different medium, not a second
        # dialect of it.
        name_w = min(max((len(r["name"]) for r in data["rows"]), default=8), 22)
        here = data.get("this_machine")
        machine = object()
        from mcp_hub.fleet_tree import _edge_state
        now = time.time()
        for r in data["rows"]:
            if r["machine"] != machine:
                machine = r["machine"]
                suffix = "  · this machine" if machine == here else "  · remote"
                # The edge's self-report (W1.2) — its own vocabulary, never
                # shared with daemon-snapshot staleness. `ok`/no-claim render
                # nothing; the header carries only exceptional facts.
                estate = _edge_state(
                    (data.get("machine_info") or {}).get(machine), now
                )
                edge_bit = {
                    "failed": "  ⚠ edge FAILING",
                    "stale": "  ⚠ edge not reporting",
                    "never": "  · no edge yet",
                }.get(estate or "", "")
                print(f"{machine or '(machine unknown)'}{suffix}{edge_bit}")
            reg = {True: "✔ hub", False: "✗ hub", None: "? hub"}[r["registered"]]
            disk = "✔ disk" if r["on_disk"] else "✗ disk"
            open_now = "● open" if r["open_now"] else "      "
            if r["error"]:
                tail = f"⚠ {r['error']}"
            elif not r["on_disk"]:
                tail = "ghost — registered, no file"
            elif r["registered"] is False:
                tail = f"not registered   {r['path']}"
            else:
                tail = r["path"]
            if r["squad"]:
                tail = f"{tail}  [{r['squad']}]"
            print(f"  {r['name']:<{name_w}}  {reg:<6} {disk:<7} {open_now}  {tail}")
        return 0

    # -- register ---------------------------------------------------------
    if args.paths:
        targets = [pathlib.Path(p) for p in args.paths]
        missing = [str(p) for p in targets if not p.is_file()]
        if missing:
            print(f"no such workspace file: {', '.join(missing)}", file=sys.stderr)
            return 1
    elif args.all:
        targets = [pathlib.Path(w["path"]) for w in discover_workspaces(scan_dirs)]
    else:
        print("name the workspace files to register, or pass --all",
              file=sys.stderr)
        return 1
    if not targets:
        print("no .code-workspace files found on this machine — nothing to register")
        return 0

    try:
        existing = {
            (w.get("machine", ""), w.get("name", "")) for w in api.list_workspaces()
        }
    except ApiUnavailable as e:
        print(str(e), file=sys.stderr)
        return 1

    created, skipped, failed = [], [], []
    for path in targets:
        name = path.name.removesuffix(".code-workspace")
        # A definition with no machine is fleet-wide, so it satisfies this
        # machine's row too — checking both stops a second register from
        # silently duplicating what an earlier machine-less one already covers.
        if (machine, name) in existing or ("", name) in existing:
            skipped.append(name)
            continue
        listings = _workspace_listings(path)
        if args.dry_run:
            created.append(f"{name} ({len(listings)} folder(s))")
            continue
        try:
            api.create_workspace(name, machine, args.squad, listings)
            created.append(f"{name} ({len(listings)} folder(s))")
        except ApiUnavailable as e:
            failed.append(f"{name}: {e}")

    verb = "would register" if args.dry_run else "registered"
    for n in created:
        print(f"{verb}: {n}")
    for n in skipped:
        print(f"already registered, left alone: {n}")
    for n in failed:
        print(f"FAILED {n}", file=sys.stderr)
    print(f"{len(created)} {verb}, {len(skipped)} already there, {len(failed)} failed")
    return 1 if failed else 0


def _workspaces_remove(args: argparse.Namespace, api: Any, machine: str) -> int:
    """Drop DEFINITIONS. Deletes nothing on any disk.

    Named rather than id'd, because the id is a database detail the operator
    never sees — the board and `list` both speak names. A name that matches on
    two machines is refused rather than resolved: deleting the wrong machine's
    definition is silent, and the fix is one `--machine` away.
    """
    from mcp_hub.operator_api import ApiUnavailable

    if not args.paths:
        print("name the workspaces to remove (as `workspaces list` shows them)",
              file=sys.stderr)
        return 1
    try:
        known = api.list_workspaces()
    except ApiUnavailable as e:
        print(str(e), file=sys.stderr)
        return 1

    targets, missing, ambiguous = [], [], []
    for name in args.paths:
        # Tolerate a path or a filename: the board shows names, but muscle
        # memory from `register` will paste a path.
        name = name.rsplit("/", 1)[-1].removesuffix(".code-workspace")
        hits = [w for w in known if w.get("name") == name]
        if args.machine_given:
            hits = [w for w in hits if w.get("machine") == machine]
        if not hits:
            missing.append(name)
        elif len(hits) > 1:
            where = ", ".join(sorted(h.get("machine") or "(any)" for h in hits))
            ambiguous.append(f"{name} — defined on {where}; "
                             "name one with --machine")
        else:
            targets.append(hits[0])

    for n in missing:
        print(f"no definition named {n} — nothing to remove", file=sys.stderr)
    for n in ambiguous:
        print(f"AMBIGUOUS {n}", file=sys.stderr)
    if ambiguous:
        return 1
    if not targets:
        return 1 if missing else 0

    # The tense has to follow what will ACTUALLY happen, not just --dry-run:
    # without --yes this preview is followed by a refusal, and printing
    # "removing:" above it describes an act that is about to not occur.
    will_write = args.yes and not args.dry_run
    for w in targets:
        where = w.get("machine") or "(any machine)"
        print(f"{'removing' if will_write else 'would remove'}: "
              f"{w['name']}  [{where}]  {len(w.get('listings') or [])} folder(s)")
    if args.dry_run:
        return 0
    if not args.yes:
        # There is no archive for workspace definitions — DELETE is a real
        # delete, unlike seats, which the hub only marks archived.
        print("this cannot be undone (the hub archives seats, not workspaces); "
              "re-run with --yes", file=sys.stderr)
        return 1

    removed, failed = [], []
    for w in targets:
        try:
            api.delete_workspace(w["id"])
            removed.append(w["name"])
        except ApiUnavailable as e:
            failed.append(f"{w['name']}: {e}")
    for n in removed:
        print(f"removed definition: {n}")
    for n in failed:
        print(f"FAILED {n}", file=sys.stderr)
    if removed:
        print("the FILES are untouched — `squad teardown workspace <file> "
              "--remove-workspace` is the other half")
    return 1 if failed else 0


def identity_command(args: argparse.Namespace) -> int:
    """Print the derived identity for a worktree — the ONE source of truth.

    squad used to derive the roster name itself from `basename "$dest"` while
    this module derives it from the git remote. Clone a repo into a
    differently-named directory and the two disagree, so the daemon writes
    status under one name while squad looks for the other and the statusline
    reads `hub ?` forever. Anything that needs the name asks here instead.

    Bypasses the opt-in gate (`--any`) when a caller needs the name for a
    worktree it is about to opt in — otherwise this returns nothing for a
    repo that hasn't been enrolled yet, which is exactly the transport case.
    """
    cwd = args.cwd or os.getcwd()
    name, project = _derive_agent_identity(cwd)
    if name is None:
        # The marker, same order the hooks use (derived wins, marker
        # fallback). A CONTAINER has no git remote and is deliberately not
        # opted in, so its ASSIGNED marker is the only identity it has —
        # and this command is what CLAUDE.md tells everything to ask.
        # Without this it answered "nothing" for every containerized seat.
        name, project = _discover_agent_from_marker(cwd)
    if name is None and args.any:
        url = _git_remote_url(cwd)
        parsed = _parse_org_repo(url) if url else None
        if parsed:
            org, repo = parsed
            project = f"{org}/{repo}"
            host = _sanitize_ident(platform.node() or "unknown-host")
            suffix = _workspace_suffix(cwd)
            raw = f"{repo}-{host}-{suffix}" if suffix else f"{repo}-{host}"
            name = _sanitize_ident(raw) or None
    if name is None:
        # Say WHY, on stderr. Silence here is indistinguishable from a
        # missing subcommand — the first containerized seat ran this, saw
        # exit 1 with no output, and reported that the CLI has no identity
        # verb at all. stdout stays empty (callers capture it as the name)
        # and the exit code stays 1 (callers branch on it); only the
        # explanation is new, and squad's single call site already
        # discards stderr.
        print(
            f"no identity for {cwd} — not a git repo with an origin, or its "
            f"org/repo is not in ~/.mcp-hub/config.json 'projects', and no "
            f".claude/hub-agent.json marker is present. Use --any to derive "
            f"a name for a repo that is not opted in yet.",
            file=sys.stderr,
        )
        return 1
    if args.json:
        print(json.dumps({"name": name, "project": project, "cwd": cwd}))
    else:
        print(name)
    return 0


# Honours $SQUAD_CONF exactly as `squad` does. Two readers of one file must
# agree on WHICH file, or a sandboxed run reads the real roster — and the only
# way to exercise the settings TUI's write path without touching the live fleet
# is to point both at a throwaway copy.
SQUAD_CONF = pathlib.Path(
    os.environ.get("SQUAD_CONF") or (pathlib.Path.home() / ".config" / "squad" / "squad.conf")
)


def _roster_all() -> list[dict[str, str]]:
    """Every roster row, in file order: [{agent, worktree, args, klass}].

    File order is deliberate — it is the order `squad` lists agents in and the
    order the cockpit's tabs appear, so a settings view that re-sorted would
    disagree with both for no reason.
    """
    try:
        text = SQUAD_CONF.read_text(encoding="utf-8")
    except OSError:
        return []
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        f = line.split("|")
        if not f[0].strip() or len(f) < 2 or not f[1].strip():
            continue
        worktree = f[1].strip()
        if worktree.startswith("~"):
            worktree = str(pathlib.Path.home()) + worktree[1:]
        rows.append({
            "agent": f[0].strip(),
            "worktree": worktree,
            "args": f[3].strip() if len(f) > 3 else "",
            "klass": (f[4].strip() if len(f) > 4 else "") or "squad",
        })
    return rows


def _agents_in_workspace(ws_path: str | None) -> list[dict[str, str]]:
    """Roster rows whose worktree is a folder of this workspace.

    The SAME rule squad's ws_agents() uses, and the same rule the cockpit uses
    to decide which tabs to open — folder membership, not the workspace's name.
    A third spelling of "which agents are in this workspace" is how teardown and
    the tab list would come to disagree about what they are acting on.

    No workspace (a bare shell) ⇒ the whole roster. The caller says so in the
    header rather than letting an unscoped list look like a scoped one.
    """
    rows = _roster_all()
    if not ws_path:
        return rows
    folders = {_norm_path(f) for f in _workspace_folders(ws_path)}
    if not folders:
        return rows
    return [r for r in rows if _norm_path(r["worktree"]) in folders]


def _roster_row(agent: str) -> dict[str, str]:
    """One agent's roster row: {worktree, args, klass}. {} if absent.

    Third reader of squad.conf, after `squad` itself and the cockpit extension.
    The alternative — shelling out to `squad` — costs a subprocess for four
    fields of a pipe-delimited file, and the extension already set the
    precedent. Field order is squad's: agent|worktree|?|args|class.
    """
    try:
        text = SQUAD_CONF.read_text(encoding="utf-8")
    except OSError:
        return {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        f = line.split("|")
        if f[0].strip() != agent:
            continue
        worktree = f[1].strip() if len(f) > 1 else ""
        if worktree.startswith("~"):
            worktree = str(pathlib.Path.home()) + worktree[1:]
        return {
            "worktree": worktree,
            "args": f[3].strip() if len(f) > 3 else "",
            # squad: anything that is not exactly "faculty" is squad-class, so a
            # typo'd class must never silently stop an agent being started.
            "klass": (f[4].strip() if len(f) > 4 else "") or "squad",
        }
    return {}


def _known_workspace_files() -> list[pathlib.Path]:
    """Every .code-workspace this machine might list an agent in.

    Same two directories the cockpit's transport picker enumerates, plus any
    file already named in squad_workspaces — a squad workspace kept somewhere
    unusual is still the one that decides membership, so it must not be the one
    we fail to look at.
    """
    home = pathlib.Path.home()
    found: dict[str, pathlib.Path] = {}
    for d in (home / "Projects", home):
        try:
            for p in sorted(d.glob("*.code-workspace")):
                found[_norm_path(str(p))] = p
        except OSError:
            continue
    table = _load_hub_config().get("squad_workspaces")
    if isinstance(table, dict):
        for ws in table:
            if isinstance(ws, str):
                found.setdefault(_norm_path(ws), pathlib.Path(ws))
    return list(found.values())


def _workspaces_listing(cwd: str) -> list[pathlib.Path]:
    target = _norm_path(cwd)
    return [
        ws for ws in _known_workspace_files()
        if any(_norm_path(f) == target for f in _workspace_folders(str(ws)))
    ]


def _launch_flags(launch_args: str) -> dict[str, bool]:
    """comms/resume from the roster's launch args.

    Mirrors the cockpit's launchStateOf(). Comms is TWO conditions, not one: the
    channels flag must be present AND name the hub server, because the flag can
    carry other servers and an agent pointed at one of those is not reachable
    for wake.
    """
    has_channels = bool(
        re.search(r"(^|\s)--(dangerously-load-development-)?channels(\s|$)", launch_args)
    )
    return {
        "comms": has_channels and "hub" in launch_args,
        "resume": bool(re.search(r"(^|\s)--continue(\s|$)", launch_args)),
    }


def _launch_pair(launch_args: str, flag: str) -> str:
    """Value of a value-taking launch flag, or "" when absent.

    Read POSITIONALLY, the same way `squad launch` writes it: the value of
    --model is arbitrary (an alias, a full id) so no pattern describes it, and
    the token after the flag is the only thing that identifies it.
    """
    tokens = launch_args.split()
    for i, tok in enumerate(tokens[:-1]):
        if tok == flag:
            return tokens[i + 1]
    return ""


# How each editable row is changed, declared HERE rather than in the cockpit.
#
# A row carries `edit`: the choices, and the argv template that applies one. The
# extension renders and runs it without knowing what a squad or a launch flag
# is, which is what lets a web UI reuse the same model — and, more immediately,
# stops the panel offering an edit the underlying verb cannot perform.
#
# A row with no `edit` key is READ-ONLY, and that is the common case: identity is
# derived, "would derive as" is the output of a calculation, and squad
# membership comes from declaring a workspace — editing it per agent would
# create the second source of truth this design exists to avoid.
_EDIT_ONOFF = ["on", "off"]
_EDIT_MODELS = ["default", "opus", "opus[1m]", "claude-opus-4-8", "fable", "sonnet", "haiku"]
_EDIT_EFFORTS = ["default", "low", "medium", "high", "xhigh", "max"]


def _edit(kind: str, choices: list[str], argv: list[str],
          binary: str = "squad", applies: str = "next restart") -> dict[str, Any]:
    """`argv` is a command with {} standing in for the chosen value.

    `applies` is shown to the operator after the change lands, and the two
    values are not interchangeable: a launch flag does nothing until the agent
    restarts, while a mute takes effect on the hub at once. A panel that said
    "applied" for both would be wrong half the time about whether the thing you
    just changed is actually in force.
    """
    return {"kind": kind, "choices": choices, "argv": argv,
            "bin": binary, "applies": applies}


def _settings_model(cwd: str) -> dict[str, Any] | None:
    """Everything the settings panel shows, with each value's SOURCE.

    Provenance is not decoration. These settings have genuinely different
    scopes — a squad usually comes from a workspace, comms is per agent, the hub
    URL is per machine — and a panel that showed only values would be unable to
    answer the one question worth asking before you change something: does this
    affect this agent, or everyone on the box?

    Read-only, and deliberately assembled HERE rather than in the extension, so
    a web UI can render the same model without reimplementing any of it.
    """
    name, project = _derive_agent_identity(cwd)
    if name is None:
        return None

    row = _roster_row(name)
    worktree = row.get("worktree") or cwd
    flags = _launch_flags(row.get("args", ""))
    launch_model = _launch_pair(row.get("args", ""), "--model")
    launch_effort = _launch_pair(row.get("args", ""), "--effort")
    workspaces = _workspaces_listing(worktree)
    derived = _resolve_squads(worktree)

    # What the HUB believes, which is what actually governs delivery. The local
    # config only says what it WOULD derive at the next register.
    live: dict[str, Any] = {}
    try:
        snap = json.loads(_status_cache_path(name).read_text(encoding="utf-8"))
        if isinstance(snap, dict):
            live = snap
    except (OSError, ValueError):
        live = {}
    live_squads = live.get("squads") if isinstance(live.get("squads"), list) else None
    muted = set(live.get("muted") or [])

    if derived:
        ws_names = ", ".join(w.name for w in workspaces)
        member_source = f"from {ws_names}" if ws_names else "derived from workspace type"
    else:
        # The gap worth surfacing: a membership nothing regenerates. It
        # survives because register() treats empty squads as "preserve".
        member_source = "set on this agent — no workspace declares it"

    # One row PER SQUAD, not one row listing them, because mute is per (agent,
    # squad): a single row could show the memberships but could never offer the
    # edit, and an agent in two squads routinely wants one of them quiet.
    squad_rows: list[dict[str, Any]] = []
    if live_squads is None:
        squad_rows.append({"label": "Squads", "value": "unknown",
                           "source": "no status snapshot yet — is the agent running?"})
    elif not live_squads:
        squad_rows.append({"label": "Squads", "value": "— none —",
                           "source": "no squad workspace lists this worktree"})
    else:
        for s in live_squads:
            squad_rows.append({
                "label": s,
                "value": "muted" if s in muted else "hearing",
                "source": member_source,
                # Mute only. JOINING and LEAVING are deliberately absent:
                # membership derives from declaring a workspace as a squad, and
                # a per-agent override here would be a second source of truth
                # that silently disagrees with the workspace. Attention is per
                # agent; membership is not.
                # Same words as the VALUE. A control whose options are
                # spelled differently from the state it displays cannot show
                # the current setting as selected — one vocabulary per
                # decision.
                "edit": _edit("mute", ["hearing", "muted"],
                              ["mute", "--agent", name, "--squad", s, "--state", "{}"],
                              binary="mcp-hub", applies="immediately"),
            })

    ws_value = ", ".join(w.name for w in workspaces) or "— none —"
    # Decided by comparing the VALUE to the un-overridden default, not by
    # reading the env var: MCP_HUB_URL is consumed at import, so "is it set
    # right now" answers a different question from "where did this URL come
    # from" — and only the second is what the row claims to say.
    hub_url = os.environ.get("MCP_HUB_URL") or DEFAULT_HUB_URL
    hub_src = "built-in default" if hub_url == BUILTIN_HUB_URL else "MCP_HUB_URL"
    onoff = {True: "on", False: "off"}

    return {
        "agent": name,
        "cwd": cwd,
        "sections": [
            {
                "title": "IDENTITY",
                "note": "",
                "rows": [
                    {"label": "Name", "value": name,
                     "source": "derived from repo + hostname"},
                    {"label": "Project", "value": project or "—",
                     "source": "derived from git remote"},
                    {"label": "Worktree", "value": worktree,
                     "source": "roster" if row else "not enrolled with squad"},
                    {"label": "Workspaces", "value": ws_value,
                     "source": f"appears in {len(workspaces)}"},
                ],
            },
            {
                "title": "SQUADS",
                "note": "decides who hears its broadcasts, and whose it hears",
                "rows": squad_rows + [
                    {"label": "Would derive as",
                     "value": ", ".join(derived) or "— none —",
                     "source": "from squad_workspaces, at next register"},
                ],
            },
            {
                "title": "LAUNCH",
                "note": "applies at next restart, not to the running session",
                "rows": [
                    {"label": "Comms (hub wake)", "value": onoff[flags["comms"]],
                     "source": "set on this agent",
                     "edit": _edit("comms", _EDIT_ONOFF, ["comms", "{}", name])},
                    {"label": "Resume on restart", "value": onoff[flags["resume"]],
                     "source": "set on this agent",
                     "edit": _edit("resume", _EDIT_ONOFF, ["resume", "{}", name])},
                    {"label": "Model", "value": launch_model or "default",
                     "source": "set on this agent" if launch_model else
                     "claude's own default — no --model in the launch args",
                     "edit": _edit("model", _EDIT_MODELS,
                                   ["launch", "model", name, "{}"])},
                    {"label": "Effort", "value": launch_effort or "default",
                     "source": "set on this agent" if launch_effort else
                     "claude's own default — no --effort in the launch args",
                     "edit": _edit("effort", _EDIT_EFFORTS,
                                   ["launch", "effort", name, "{}"])},
                    {"label": "Launch args", "value": row.get("args") or "—",
                     "source": "roster"},
                ],
            },
            {
                "title": "THIS MACHINE",
                "note": "applies to every agent here",
                "rows": [
                    {"label": "Hub URL", "value": hub_url, "source": hub_src},
                    {"label": "Enrolment", "value": row.get("klass") or "—",
                     "source": "roster — faculty is never auto-started by `squad up`"},
                    {"label": "Opted-in projects",
                     "value": ", ".join(_load_hub_config().get("projects") or []) or "— none —",
                     "source": str(_HUB_CONFIG_PATH)},
                ],
            },
        ],
    }


async def _mute_squad(hub_url: str, name: str, squad: str, muted: bool) -> str:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async with streamablehttp_client(_ephemeral_hub_url(hub_url), timeout=15) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return _extract_text(await session.call_tool(
                "mute_squad", {"name": name, "squad": squad, "muted": muted}
            )) or ""


async def _set_focus(hub_url: str, name: str, minutes: int, reason: str) -> str:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async with streamablehttp_client(
        _ephemeral_hub_url(hub_url), timeout=15
    ) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return _extract_text(await session.call_tool(
                "focus",
                {"agent_name": name, "minutes": minutes, "reason": reason},
            )) or ""


async def _send_dm(hub_url: str, frm: str, to: str, message: str,
                   priority: str, in_reply_to: str) -> str:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async with streamablehttp_client(
        _ephemeral_hub_url(hub_url), timeout=15
    ) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return _extract_text(await session.call_tool(
                "send",
                {"from_agent": frm, "to": to, "message": message,
                 "priority": priority, "in_reply_to": in_reply_to},
            )) or ""


def send_command(args: argparse.Namespace) -> int:
    """Send one DM from the shell.

    A CLI verb because squad — which is bash — needs to write rows through
    doors that are hub DMs (the compaction door, card #53). Shelling out to
    python from bash for every row would put an inline script in the hot
    path of a sweep; this keeps one client and one place to change it.

    Reads the body from `--message`, or from STDIN when it is `-`, so a
    multi-line row does not have to survive shell quoting.
    """
    name = args.from_agent
    if not name:
        cwd = args.cwd or os.getcwd()
        name, _project = _derive_agent_identity(cwd)
        if not name:
            print(f"no derived identity for {cwd} — pass --from", file=sys.stderr)
            return 1
    body = args.message
    if body == "-":
        body = sys.stdin.read()
    if not body.strip():
        print("refusing to send an empty message", file=sys.stderr)
        return 1
    try:
        reply = asyncio.run(_send_dm(
            args.hub_url, name, args.to, body, args.priority, args.in_reply_to,
        ))
    except Exception as exc:  # noqa: BLE001
        print(f"!! send failed: {exc}", file=sys.stderr)
        return 1
    print(reply or f"sent to {args.to}")
    return 0


def focus_command(args: argparse.Namespace) -> int:
    """Do-not-disturb for one agent, for a bounded time.

    A CLI verb because the cockpit shells out rather than calling MCP tools,
    and because the operator is often the one who knows an agent is about to
    be busy in a way the hub cannot see.
    """
    name = args.agent
    if not name:
        cwd = args.cwd or os.getcwd()
        name, _project = _derive_agent_identity(cwd)
        if not name:
            print(f"no derived identity for {cwd} — pass --agent", file=sys.stderr)
            return 1
    minutes = 0 if args.off else args.minutes
    try:
        reply = asyncio.run(_set_focus(args.hub_url, name, minutes, args.reason))
    except Exception as exc:  # noqa: BLE001
        print(f"!! focus failed: {exc}", file=sys.stderr)
        return 1
    print(reply or f"{name}: focus {'off' if minutes <= 0 else f'{minutes}m'}")
    return 0


def mute_command(args: argparse.Namespace) -> int:
    """Silence one squad's broadcasts for one agent, without leaving it.

    Exists as a CLI verb because the cockpit cannot call MCP tools — it shells
    out. Deliberately does NOT join or leave: membership derives from declaring
    a workspace as a squad, and a second way to set it would disagree with the
    workspace sooner or later. Attention is per agent; membership is not.
    """
    muted = args.state == "muted"
    try:
        reply = asyncio.run(_mute_squad(args.hub_url, args.agent, args.squad, muted))
    except Exception as exc:  # noqa: BLE001
        print(f"!! mute failed: {exc}", file=sys.stderr)
        return 1
    print(reply or f"{args.agent}: {args.squad} {'muted' if muted else 'unmuted'}")
    _record_mute_in_cache(args.agent, args.squad, muted)
    return 0


def _record_mute_in_cache(agent: str, squad: str, muted: bool) -> None:
    """Write the mute we just made into the cached snapshot.

    This used to DELETE the file, on the reasoning that the daemon is the only
    honest author of a cache. That was wrong in the way that matters: the
    daemon rewrites it about once a minute, so between the write and the next
    beat every reader — statusline and settings panel alike — reported the
    squad as `unknown`. The operator changed a value and watched it become
    unknown (2026-07-28).

    "We do not know" is a strictly worse answer than the state we just
    successfully applied. Only this one field is touched, and only after the
    hub confirmed the write, so the copy stays faithful; the daemon overwrites
    it wholesale on its next beat regardless.
    """
    path = _status_cache_path(agent)
    try:
        snap = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(snap, dict):
            return
    except (OSError, ValueError):
        return          # nothing cached yet: leave it to the daemon
    current = [s for s in (snap.get("muted") or []) if isinstance(s, str)]
    if muted and squad not in current:
        current.append(squad)
    elif not muted and squad in current:
        current = [s for s in current if s != squad]
    snap["muted"] = sorted(current)
    try:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(snap), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


# Where the panel shells out to. Resolved absolutely because a VSCode
# extension host does not inherit an interactive shell's PATH.
#
# The panel itself lives in settings_app.py, on Textual. It replaced a
# hand-rolled curses version whose key handling I wrote myself — and one
# line of it, binding ESC to quit, made every arrow key exit the program,
# because VSCode sends arrows as `ESC [ B` and the leading byte arrives as
# 27. Keyboard, focus and mouse are a solved problem; they were not mine to
# solve, and solving them badly cost the operator an evening.
SQUAD_BIN = str(pathlib.Path.home() / ".local" / "bin" / "squad")
MCP_HUB_BIN = str(pathlib.Path.home() / ".local" / "bin" / "mcp-hub")

# The same two tools, found the same way in every kind of shell. `squad` on
# PATH is an INTERACTIVE-shell assumption: ssh commands, systemd timers and
# cron get a bare PATH without ~/.local/bin, and `edge apply` — which is meant
# to run from the heal timer — died there on a FileNotFoundError traceback.
_TOOL_PATHS = {"squad": SQUAD_BIN, "mcp-hub": MCP_HUB_BIN}


def _resolve_tool(name: str) -> str | None:
    """Absolute path for a known tool: its install location, else PATH."""
    fixed = _TOOL_PATHS.get(name)
    if fixed and os.path.exists(fixed):
        return fixed
    return shutil.which(name)


def settings_command(args: argparse.Namespace) -> int:
    """One agent's settings, or an interactive panel over a workspace's agents."""
    if getattr(args, "tui", False):
        workspace = getattr(args, "workspace", None)
        agents = _agents_in_workspace(workspace)
        if not agents:
            where = workspace or "this machine"
            print(f"no roster agents in {where}", file=sys.stderr)
            return 1
        try:
            from .settings_app import SettingsApp
        except ImportError:
            print("the settings panel needs textual:  pip install textual",
                  file=sys.stderr)
            return 1
        from .board_data import collect, terminal_prefers_dark
        # Ask the terminal BEFORE the app owns the tty — OSC 11 needs a quiet
        # line, and Textual's raw-mode setup would eat the reply.
        dark = terminal_prefers_dark()
        def _workspaces():
            from mcp_hub.operator_api import OperatorApi, api_base
            from mcp_hub.workspace_data import collect_workspaces

            host = _sanitize_ident(platform.node() or "unknown-host")
            return collect_workspaces(
                OperatorApi(api_base(DEFAULT_HUB_URL)),
                scan_dirs=[pathlib.Path.home() / "Projects", pathlib.Path.home()],
                this_machine=host,
            )

        def _placements() -> list:
            """Desired state for the whole fleet, so the board can show which
            seats are scheduled and which are waiting on an edge that is not
            running. Failure is swallowed by the caller — this is a dashboard.
            """
            from mcp_hub.operator_api import OperatorApi, api_base

            return OperatorApi(api_base(DEFAULT_HUB_URL)).list_placements()

        def _seats() -> list:
            """What may run, fleet-wide. The board needs it to draw containers:
            a seat with an image IS a container, and its record carries the
            host folder that a remote agent's presence row does not.
            """
            from mcp_hub.operator_api import OperatorApi, api_base

            return OperatorApi(api_base(DEFAULT_HUB_URL)).list_seats()

        def _machine_agents() -> dict:
            """Each machine's roster, so a REMOTE row is attributed by its real
            worktree instead of by repo name. A machine missing from this map
            has not reported one; the tree falls back for that machine alone.
            """
            from mcp_hub.operator_api import OperatorApi, api_base

            return OperatorApi(api_base(DEFAULT_HUB_URL)).machine_agents()

        def _presence_ping(workspace_path: str) -> None:
            """Tell the hub this workspace is open in front of a human.

            Only the board can know this — no scan infers it — so if this
            never fires, the manager's third column is dead weight. Errors
            are the caller's to swallow; this is a dashboard.
            """
            from mcp_hub.operator_api import OperatorApi, api_base

            host = _sanitize_ident(platform.node() or "unknown-host")
            OperatorApi(api_base(DEFAULT_HUB_URL)).push_status(
                host, {"workspace_open": workspace_path}
            )

        def _fleet_snapshot() -> dict:
            """The daemons' fleet snapshot — every agent the hub knows, not
            just this box's roster. Read from the local cache rather than the
            network: it is on the board's poll path, and a missing cache must
            read as "not reporting", which `fleet_tree` decides from its `ts`.
            """
            try:
                return json.loads(
                    (pathlib.Path.home() / ".mcp-hub" / "fleet-board.json")
                    .read_text(encoding="utf-8")
                )
            except Exception:  # noqa: BLE001 — absent or malformed: no claim
                return {"ts": 0, "agents": []}

        SettingsApp(
            agents,
            scoped_to=workspace,
            model_for=_settings_model,
            squad_bin=SQUAD_BIN,
            hub_bin=MCP_HUB_BIN,
            board_for=lambda: collect(SQUAD_BIN),
            # Same call that produced the opening roster, on the board's own
            # tick: squad.conf is a file another pane writes, so enrolment has
            # to reach a board that is already open.
            roster_for=lambda: _agents_in_workspace(workspace),
            dark=dark,
            workspaces_for=_workspaces,
            presence_ping=_presence_ping,
            fleet_for=_fleet_snapshot,
            listings_for=lambda p: _workspace_listings(pathlib.Path(p)),
            placements_for=_placements,
            seats_for=_seats,
            machine_agents_for=_machine_agents,
            this_machine=_sanitize_ident(platform.node() or "unknown-host"),
        ).run()
        return 0
    cwd = args.cwd or os.getcwd()
    model = _settings_model(cwd)
    if model is None:
        print(f"no derived hub identity for {cwd} (not opted in?)", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(model, indent=2))
        return 0
    print(f"Settings — {model['agent']}")
    for section in model["sections"]:
        note = f"   ({section['note']})" if section["note"] else ""
        print(f"\n{section['title']}{note}")
        width = max(len(r["label"]) for r in section["rows"])
        for r in section["rows"]:
            print(f"  {r['label']:<{width}}  {r['value']}")
            print(f"  {'':<{width}}  \033[2m{r['source']}\033[0m")
    return 0


def transport_history_command(args: argparse.Namespace) -> int:
    """Copy + re-key every transcript from one project path to another.

    `--out-dir` writes the re-keyed transcripts somewhere other than this
    machine's own Claude state dir. That's what makes a CROSS-MACHINE
    transport possible: the re-key must happen where the source transcripts
    live, but the result belongs in the DESTINATION's encoded dir on another
    box — so we re-key into a staging dir here and ship that.
    """
    src_dir = pathlib.Path.home() / ".claude" / "projects" / _claude_project_dirname(args.from_cwd)
    dst_dir = (
        pathlib.Path(args.out_dir)
        if getattr(args, "out_dir", None)
        else pathlib.Path.home() / ".claude" / "projects" / _claude_project_dirname(args.to_cwd)
    )
    if not src_dir.is_dir():
        print(f"!! no history for {args.from_cwd} (looked in {src_dir})", file=sys.stderr)
        return 1
    files = sorted(p for p in src_dir.glob("*.jsonl") if p.is_file())
    if not files:
        print(f"no transcripts in {src_dir}")
        return 0

    if not args.dry_run:
        dst_dir.mkdir(parents=True, exist_ok=True)

    failed = 0
    for src in files:
        text, stats = _rekey_transcript(
            src.read_text(encoding="utf-8"), args.from_cwd, args.to_cwd
        )
        flag = ""
        if stats["completeness_violations"]:
            # A structural field still points at the source: we missed a
            # coupling. Writing would hand the clone live pointers into the
            # source agent's state. Refuse — loudly.
            flag = "  ✗ REFUSED (structural leak — a path field was missed)"
            failed += 1
        elif stats["unclassified_paths"]:
            # A structural field carries a path this module has not classified,
            # so nobody has decided whether it should travel, be neutralised, or
            # be left alone. Undecided is not the same as safe.
            flag = ("  ✗ REFUSED (unclassified path field — "
                    f"{stats['unclassified_paths']} occurrence(s))")
            failed += 1
        elif stats["content_touched"]:
            flag = "  ✗ REFUSED (would alter message content)"
            failed += 1
        elif not args.dry_run:
            (dst_dir / src.name).write_text(text, encoding="utf-8")
        print(
            f"  {src.name}: {stats['lines']} lines, "
            f"cwd={stats['cwd']} tracking={stats['tracking']} "
            f"realparent={stats['realparent']} snapshot={stats['snapshot']}"
            f"{flag}"
        )
        if stats["roundtrip_mismatch"]:
            print(
                f"    !! {stats['roundtrip_mismatch']} lines did not round-trip "
                "byte-exactly — serializer mismatch",
                file=sys.stderr,
            )

    verb = "would transport" if args.dry_run else "transported"
    print(f"{verb} {len(files) - failed}/{len(files)} transcript(s) -> {dst_dir}")
    return 1 if failed else 0


def _discover_agent_from_marker(cwd: str | None) -> tuple[str | None, str | None]:
    """LEGACY fallback: read identity from `<cwd>/.claude/hub-agent.json`.

    Deprecated in favour of derived identity (_derive_agent_identity) — a
    committed marker is shared by every clone of the repo, which makes clones
    collide into one hub identity. Kept as a fallback so not-yet-migrated
    agents keep working; remove once the fleet is on derived identity.

    The marker file shape:
        {"name": "dreamteam-lead", "project": "dreamteam"}

    Returns (name, project) — either or both may be None if the marker
    doesn't exist or is malformed. The cli silently no-ops in that case;
    not every project on the system is a hub agent, and most aren't.
    """
    if not cwd:
        return None, None
    marker = pathlib.Path(cwd) / AGENT_MARKER_PATH
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None, None
    name = data.get("name")
    project = data.get("project")
    return (
        name if isinstance(name, str) and name else None,
        project if isinstance(project, str) and project else None,
    )


def _resolve_agent_identity(
    args: argparse.Namespace,
    payload: dict[str, Any] | None = None,
) -> tuple[str | None, str | None]:
    """Resolve which agent this hook invocation is for.

    Resolution order:
      1. Explicit --name (and --project) on the CLI — overrides everything.
         Useful for tests, manual checks, non-standard setups.
      2. Derived identity — org/repo from the cwd's git remote + hostname,
         gated on the ~/.mcp-hub/config.json opt-in list. The canonical
         path: clone-local name, repo-global project, nothing committed.
      3. LEGACY: marker file at <cwd>/.claude/hub-agent.json — kept so
         not-yet-migrated agents keep working.
      4. Nothing — return (None, None) and the cli will silently no-op.

    Derived wins over the marker: a repo that still carries a committed
    marker must not drag a migrated machine back into the shared identity.

    `payload` is the already-parsed Stop-hook stdin. Callers that also need
    other stdin fields (e.g. `stop_hook_active`) must read stdin ONCE and
    pass it in — stdin is not re-readable, so a second `_read_hook_stdin()`
    would return {}. If None, we read it here.
    """
    if args.name:
        return args.name, args.project

    if payload is None:
        payload = _read_hook_stdin()
    cwd = payload.get("cwd")
    name, project = _derive_agent_identity(cwd)
    if name is not None:
        return name, project
    return _discover_agent_from_marker(cwd)


def stop_hook_command(args: argparse.Namespace) -> int:
    """Run the stop-hook subcommand. Always returns 0 (fail-open)."""
    # Read stdin ONCE — both agent identity (cwd marker) and the
    # stop_hook_active loop-backstop flag come from this single payload.
    payload = _read_hook_stdin()
    name, project = _resolve_agent_identity(args, payload)
    if name is None:
        # No identity resolved — this project isn't onboarded as a hub agent.
        # Silent no-op: most projects on the box aren't hub agents and the
        # global Stop hook fires in all of them. We don't want noise.
        return 0

    # True when this Stop is firing because a prior Stop-hook block fired.
    stop_hook_active = bool(payload.get("stop_hook_active"))

    # Self-heal the keep-alive daemon before anything else — this runs even if
    # the hub is unreachable (the daemon will retry-connect on its own), so a
    # dead/absent daemon is revived at the next turn regardless of hub health.
    _ensure_daemon_alive(name, args.hub_url)

    # HOLD, turn-boundary leg (#318). This Stop IS the boundary the
    # operator's ruling names, and this hook is the only thing that can see
    # one — so it stamps the moment and squad stops the lane on its next
    # pass. It does NOT stop the lane itself: it runs inside the process it
    # would be killing, and this path must return 0 whatever happens.
    #
    # Local file reads only. No hub round-trip: the hold already travelled
    # hub -> edge -> mirror, and making every turn boundary on every lane
    # wait on the network would put the hub in the path of every turn end.
    from mcp_hub import hold as _hold

    held_notice = ""
    try:
        _entry = _hold.held_entry(name)
        if _entry:
            _hold.stamp_boundary(name)
            held_notice = _hold.hook_notice(name, _entry)
        else:
            # Not held (or released, or expired): drop any stamp, or it
            # would outlive its hold and make the NEXT one look as though
            # it had already reached a boundary.
            _hold.clear_boundary(name)
    except Exception:  # noqa: BLE001 — a hold must never break a turn end
        held_notice = ""

    # DECISION card leg: harvest the card (or its absence) from the turn
    # that just ended, ship it with the same hub round-trip below. Waiting
    # language without a card earns the one-shot authoring nag.
    last_turn = _read_last_assistant_text(payload.get("transcript_path"))
    card = _extract_decision_card(last_turn)
    decided = "" if card else _extract_decided(last_turn)
    genuine, reason, phrase = (
        _waiting_analysis(last_turn) if last_turn else (False, "no_match", "")
    )
    card_nag = not card and not decided and genuine
    # Grace bookkeeping runs on every natural Stop — a nag-free Stop must
    # CLEAR the flag, not just a nagging one set it. Backstop Stops
    # (stop_hook_active) are skipped: they are the same natural turn, and
    # skipping them also keeps the telemetry at one record per turn.
    if not stop_hook_active:
        card_nag = _card_nag_grace(name, card_nag)
        if reason != "no_match":
            outcome = ("card_filed" if card else "decided" if decided else
                       reason if not genuine else
                       "nagged" if card_nag else "suppressed_grace")
            _log_nag_event(name, outcome, phrase)

    # Delivery-receipt report (card #56): what this agent's own transcript
    # proves rendered. Defensive like everything else on this path — a scan
    # failure reports "none", which just means fuller reprints this drain.
    from mcp_hub import receipts

    try:
        rendered_report = receipts.encode_report(
            receipts.rendered_message_ids(payload.get("transcript_path"))
        )
    except Exception:  # noqa: BLE001
        rendered_report = "none"

    try:
        messages_text, broadcasts_text, is_online, card_notice = asyncio.run(
            _query_hub(args.hub_url, name, project or "", card, decided,
                       rendered_report)
        )
    except Exception as exc:  # noqa: BLE001
        # Fail open — never block the agent on hub flakiness.
        # Still RECORDED: a drain that could not reach the hub surfaced
        # nothing and cost no turn, which is exactly the case the transcript
        # cannot see. `error` lets the analysis exclude it deliberately
        # rather than by never having heard of it.
        _log_activity_event(name, "drain", surfaced=False, error=True,
                            session=str(payload.get("session_id") or ""),
                            backstop=stop_hook_active)
        print(f"[mcp-hub stop-hook] hub query failed: {exc!r}", file=sys.stderr)
        return 0

    # Shadow mode: compare the hub's "already delivered live" inference against
    # what the transcript shows actually rendered. Diagnostic only — the return
    # value is deliberately discarded, so the rendering below is byte-identical
    # whether this runs or not. Local file reads; no hub write, no network.
    from mcp_hub import shadow

    shadow.run_shadow(name, messages_text, payload.get("transcript_path"))

    response = build_hook_response(
        agent_name=name,
        project=project,
        messages_text=messages_text,
        broadcasts_text=broadcasts_text,
        is_online=is_online,
        stop_hook_active=stop_hook_active,
        card_nag=card_nag,
        card_notice=card_notice,
        held_notice=held_notice,
        # Opt-IN, and off by default: this is the one path that can decide
        # not to show an agent something the hub has already marked read, so
        # it ships dark and is turned on per box once its spool is trusted.
        defer_low=os.environ.get("MCP_HUB_DEFER_LOW", "").lower()
        in ("1", "true", "yes"),
    )

    # 🔴 LOGGED ON BOTH BRANCHES, AND THE `None` ONE IS THE POINT. A drain
    # that surfaces nothing prints nothing, blocks nothing and leaves no
    # transcript entry — it is invisible to every other instrument, which is
    # precisely why bar 47 could not be measured. Recording only the noisy
    # branch would rebuild the same blind spot in a new file.
    _log_activity_event(name, "drain", surfaced=response is not None,
                        session=str(payload.get("session_id") or ""),
                        backstop=stop_hook_active)

    if response is None:
        return 0  # No block — Stop proceeds normally

    print(json.dumps(response))
    return 0


# ---------------------------------------------------------------------------
# session-start subcommand — inject "register now" into the agent's first turn
# ---------------------------------------------------------------------------


def session_start_command(args: argparse.Namespace) -> int:
    """Emit SessionStart hook JSON instructing the agent to call register()
    immediately. This makes the agent ⚡ from session start without operator
    nudging — closes the chicken-and-egg gap where the heartbeat daemon
    can't bring an agent online (it doesn't bind, by design), only sustain
    a binding once one exists.

    Reads `<cwd>/.claude/hub-agent.json` for identity, same pattern as
    stop-hook. Silent no-op if no marker (project isn't a hub agent —
    don't spam the SessionStart of unrelated projects with register
    instructions).

    Output format: SessionStart hooks support JSON with
    `hookSpecificOutput.additionalContext` (or plain `additionalContext`)
    that gets injected into the agent's context before the first turn.
    Claude reads it and acts on it proactively.
    """
    # Read stdin ONCE and pass it in — it is not re-readable, and the squad
    # resolution below needs the SAME cwd the identity was derived from.
    payload = _read_hook_stdin()
    name, project = _resolve_agent_identity(args, payload)
    if name is None:
        return 0  # no marker → silent no-op

    project_str = f', project="{project}"' if project else ""
    # Squads ride the SAME path as name and project: derived here, injected
    # into the register call, so no agent has to learn anything new. Empty is
    # omitted rather than sent as "" — register treats empty as "no opinion"
    # and preserves, so sending it explicitly would be a no-op that only
    # made the instruction noisier.
    squads = _resolve_squads(payload.get("cwd") or os.getcwd())
    squads_str = f', squads="{",".join(squads)}"' if squads else ""
    register_call = (
        f'mcp__hub__register(name="{name}"{project_str}{squads_str})'
    )

    context = (
        f"You are the hub agent **{name}**"
        + (f" (project: {project})" if project else "")
        + ". Your first action this session is to register on the hub for "
        + "channel-push wake.\n"
        + f"1. Call `{register_call}` now.\n"
        + "2. If the hub's tools aren't available yet, the `hub` MCP server is "
        + "simply still connecting — this is common right after a resume. Do "
        + "NOT conclude the hub is down and do NOT just hold: wait for it to "
        + "finish connecting, then register. If a `WaitForMcpServers` tool is "
        + "available, use it to wait for the `hub` server; otherwise pause "
        + "briefly and retry. The connection usually settles within ~30s.\n"
        + "3. Once register succeeds you are bound for wake and the heartbeat "
        + "daemon keeps the binding alive across idle periods. Until you "
        + "register, your inbox is queue-only (messages wait for the next Stop "
        + "hook auto-pull) and your statusline shows `hub ✖ REGISTER`."
    )

    output = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    }
    print(json.dumps(output))
    return 0


def session_rewake_command(args: argparse.Namespace) -> int:
    """Companion to `session-start`: writes the register instruction to
    stderr and exits with code 2 to trigger Claude Code's asyncRewake
    behaviour. The intent is to fire an unprompted first turn at session
    start so Claude calls register() before the operator has typed
    anything.

    This is empirically untested in this codebase — the docs are ambiguous
    about whether asyncRewake fires from a cold session start (no prior
    turns to "wake" from) or only between turns of an active conversation.
    If it doesn't fire, this is a no-op; the synchronous `session-start`
    hook's additionalContext still drives the register on first prompt.

    Reads `<cwd>/.claude/hub-agent.json`; silent no-op (exit 0) if no
    marker found, so non-hub projects don't get spurious wake events.
    """
    payload = _read_hook_stdin()
    name, project = _resolve_agent_identity(args, payload)
    if name is None:
        return 0  # no marker → exit 0, no wake

    project_str = f', project="{project}"' if project else ""
    # Same derivation as session-start — see the note there.
    squads = _resolve_squads(payload.get("cwd") or os.getcwd())
    squads_str = f', squads="{",".join(squads)}"' if squads else ""
    register_call = (
        f'mcp__hub__register(name="{name}"{project_str}{squads_str})'
    )
    msg = (
        f"Hub auto-register: call `{register_call}` now to bind your "
        f"interactive MCP session for channel-push wake. The heartbeat "
        f"daemon (separate process) will then keep your binding alive."
    )
    print(msg, file=sys.stderr)
    return 2  # asyncRewake trigger


# ---------------------------------------------------------------------------
# heartbeat-daemon subcommand
# ---------------------------------------------------------------------------


HEARTBEAT_INTERVAL_SECONDS = 60
HEARTBEAT_RETRY_DELAY_SECONDS = 60
# Statusline cache is refreshed more often than the heartbeat so register/
# offline transitions show up promptly (not up to a full heartbeat late).
STATUS_REFRESH_SECONDS = 15


# ---------------------------------------------------------------------------
# Singleton enforcement — stop the daemon leak.
#
# Each SessionStart spawns a heartbeat daemon. On POSIX the OS reaps it when
# the parent Claude Code process dies; on Windows it does NOT — so every
# session restart (and every SessionStart-resume, which fires on each hub
# reconnect) leaves the old daemon running AND adds a new one. Observed
# 2026-05-29: ~12 daemons accumulated on one machine, and pre-cutover ones
# kept pinging the dead public hub URL.
#
# We enforce one-daemon-per-agent with an atomic pidfile claim:
#   - First daemon for an agent atomically creates the pidfile (O_EXCL) and
#     runs the heartbeat loop.
#   - Any later daemon finds the pidfile, sees a live daemon already owns it,
#     and EXITS immediately instead of looping forever. No accumulation.
#   - If the pidfile is stale (owner dead/crashed, or garbage), the newcomer
#     removes it and claims it — self-healing.
#
# Design choices and why:
#   * "Old wins" (incumbent keeps running, newcomer exits) rather than
#     "new wins (kill incumbent)". The daemon is FUNGIBLE — it only calls
#     heartbeat(agent_name), which refreshes whatever binding currently
#     exists for that agent, regardless of which process sends it. So one
#     surviving daemon per agent is fully sufficient even across session
#     restarts. Old-wins also means we NEVER terminate another process —
#     zero risk of killing a PID-reused stranger — and the atomic O_EXCL
#     create makes it race-safe (two near-simultaneous daemons can't both
#     win). New-wins-by-kill had a kill-war race and PID-reuse hazard.
#   * Tradeoff: a config change (e.g. the MCP_HUB_URL cutover) is NOT
#     auto-adopted while an old-config daemon is still alive — the newcomer
#     with the new config exits. Mitigation: a one-off `Stop-Process` sweep
#     of the daemons on the rare config change (operationally cheap; we did
#     exactly this for the 2026-05-29 cutover). Frequency strongly favours
#     this: the leak/accumulation happens on every restart; config changes
#     are rare and operator-driven.
#   * A parent-death watch can't fix the Windows leak: the daemon's parent is
#     the `mcp-hub.exe` console-script launcher, which leaks alongside the
#     python daemon when Claude Code dies, so the parent PID stays "alive".
#
# See project_heartbeat_daemon_leak memory.
# ---------------------------------------------------------------------------

# Stable per-agent pidfile directory. Deliberately NOT tempfile.gettempdir():
# that honours TMPDIR/TEMP, which Claude Code overrides for its subprocesses
# (observed: ...\Temp\claude). A daemon spawned by the SessionStart hook could
# then land on a different temp dir than another context, so the pidfiles
# wouldn't find each other and dedup would silently fail. The home dir is
# invariant across however the daemon gets spawned.
def _state_dir() -> pathlib.Path:
    """Per-box daemon state directory — heartbeat pidfiles, statusline cache,
    the hub-reconnect stamp, and the seen-boot nonce all live here.

    Honors `$MCP_HUB_STATE_DIR` (default `~/.mcp-hub`). The override exists so
    the disruption/stamp/nonce machinery is testable in ISOLATION without
    redirecting the whole HOME — without it, automated tests around heal/stamp
    behaviour clobber the real fleet state (flagged by mcp-hub-fireblade while
    building the false-positive repro). Read at call-time so a test can set the
    env after import. The home dir is otherwise invariant across however the
    daemon gets spawned, which is what the per-box singleton dedup relies on."""
    override = os.environ.get("MCP_HUB_STATE_DIR")
    return pathlib.Path(override) if override else (pathlib.Path.home() / ".mcp-hub")


def _heartbeat_pidfile(agent_name: str) -> pathlib.Path:
    """Stable per-agent pidfile path under ~/.mcp-hub.

    Per-agent (not global) so each agent on a shared machine keeps its own
    single daemon — the claim only ever considers the same agent's daemon.
    """
    safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in agent_name)
    return _state_dir() / f"heartbeat-{safe}.pid"


def _is_live_daemon(pid: int) -> bool:
    """True if `pid` is a live process that looks like a heartbeat daemon.

    Conservative: returns False whenever identity is uncertain (so a recycled
    PID belonging to an unrelated process is treated as 'not a daemon' and the
    newcomer takes over the stale pidfile rather than deferring to a stranger).
    Note this function never kills anything — it's purely a liveness/identity
    probe for the claim logic.
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        # Windows os.kill(pid, 0) is not a safe liveness probe (non-CTRL
        # signals unconditionally TerminateProcess the target), so use
        # tasklist and verify the PID exists AND its image is python/launcher.
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                timeout=5,
                creationflags=_NO_WINDOW_FLAG,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return False
        low = out.lower()
        return str(pid) in out and ("python" in low or "mcp-hub" in low)
    # POSIX: signal 0 is a real liveness probe.
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Alive but owned by another user — not our daemon.
        return False
    except OSError:
        return False
    # When /proc is available (Linux), confirm it's actually a heartbeat
    # daemon. Elsewhere (e.g. macOS) trust liveness — the leak this guards
    # against is Windows-only anyway.
    cmdline = pathlib.Path(f"/proc/{pid}/cmdline")
    try:
        data = cmdline.read_bytes().replace(b"\x00", b" ").decode("utf-8", "replace")
    except (FileNotFoundError, OSError):
        return True
    return "heartbeat-daemon" in data


def _claim_singleton(agent_name: str, *, getpid=os.getpid) -> pathlib.Path | None:
    """Try to become the sole heartbeat daemon for `agent_name`.

    Returns the pidfile path if we won the claim (caller should run the loop),
    or None if a live daemon already owns it (caller should exit immediately).

    Race-safe via atomic O_EXCL create. If an existing pidfile is stale
    (owner dead, or garbage contents), it's removed and the claim retried.
    """
    pidfile = _heartbeat_pidfile(agent_name)
    try:
        pidfile.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        # Can't create the state dir — fail open: run unguarded rather than
        # refuse to heartbeat. Worst case is the pre-fix behaviour (possible
        # duplicate), never a missing heartbeat.
        return pidfile

    # Bounded retry: each iteration either wins the create, defers to a live
    # owner, or clears one stale pidfile and loops. A handful of iterations is
    # plenty; the cap just prevents a pathological spin.
    for _ in range(10):
        try:
            fd = os.open(str(pidfile), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                prev = int(pidfile.read_text(encoding="utf-8").strip())
            except (FileNotFoundError, ValueError, OSError):
                prev = None
            if prev is not None and prev != getpid() and _is_live_daemon(prev):
                return None  # a live daemon already owns this agent — stand down
            # Stale/garbage/own-PID — drop it and retry the atomic create.
            try:
                pidfile.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                return None  # lost the race to another claimant — stand down
            continue
        else:
            with os.fdopen(fd, "w") as f:
                f.write(str(getpid()))
            return pidfile
    # Couldn't settle the claim (extreme contention) — fail open and run.
    return pidfile


def _release_singleton(pidfile: pathlib.Path, *, getpid=os.getpid) -> None:
    """Remove the pidfile on clean exit, but only if it still names us — so we
    never delete a successor daemon's claim."""
    try:
        owner = int(pidfile.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError, OSError):
        return
    if owner == getpid():
        try:
            pidfile.unlink()
        except OSError:
            pass


def _daemon_alive_for(agent_name: str) -> bool:
    """True if a live heartbeat daemon currently owns `agent_name`'s pidfile.

    Cheap check (reuses the singleton's liveness probe) so the Stop hook can
    decide whether to self-heal a missing/dead daemon at a turn boundary.
    """
    pidfile = _heartbeat_pidfile(agent_name)
    try:
        prev = int(pidfile.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError, OSError):
        return False
    return _is_live_daemon(prev)


def _spawn_daemon_detached(agent_name: str, hub_url: str) -> None:
    """Launch `heartbeat-daemon` fully detached so it outlives the short-lived
    Stop hook that spawns it.

    Cross-platform: POSIX uses a new session (setsid) so the daemon isn't
    killed when the hook returns; Windows uses DETACHED_PROCESS. Invoked via
    `python -m mcp_hub.cli` (not a PATH lookup for the console script) so it
    works from any venv layout. The singleton claim inside the daemon makes
    this safe to call redundantly — a second daemon stands down at once.
    """
    exe = sys.executable
    if os.name == "nt":
        # pythonw.exe is GUI-subsystem so it never allocates a console. Plain
        # python.exe is console-subsystem, and DETACHED_PROCESS makes Windows give
        # the detached child a fresh VISIBLE console — a window that flashes on
        # every daemon (re)spawn. pythonw suppresses it. Fall back if absent.
        _pyw = pathlib.Path(sys.executable).with_name("pythonw.exe")
        if _pyw.exists():
            exe = str(_pyw)
    cmd = [
        exe, "-m", "mcp_hub.cli", "heartbeat-daemon",
        "--name", agent_name, "--hub-url", hub_url,
    ]
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = (
            getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | _NO_WINDOW_FLAG
        )
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(cmd, **kwargs)  # noqa: S603 — fire-and-forget self-heal


def _ensure_daemon_alive(agent_name: str, hub_url: str) -> None:
    """Self-heal the keep-alive daemon: if none is running for `agent_name`,
    spawn one (detached).

    Called from the Stop hook so a crashed or never-started daemon is revived
    at the next turn boundary instead of leaving the agent without ⚡ keep-alive
    until a full session relaunch. Idempotent by construction — the singleton
    caps it at one daemon per agent on every platform, so a redundant call is a
    no-op. Fail-open: any error here must never disturb the Stop hook.
    """
    try:
        if _daemon_alive_for(agent_name):
            return
        _spawn_daemon_detached(agent_name, hub_url)
    except Exception as exc:  # noqa: BLE001
        print(f"[mcp-hub stop-hook] daemon self-heal failed: {exc!r}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Status cache — powers the hub segment in the Claude Code statusline.
#
# The statusline command (a fast Node script in ~/.claude) must NOT do a hub
# round-trip on every refresh — that's slow and hammers the hub. Instead the
# heartbeat daemon, which already holds an open MCP session and runs every
# minute even while the agent is idle, writes a tiny per-agent JSON snapshot of
# wakeability here. The statusline just reads that file (instant, no network,
# no Python spawn). The daemon survives a stream death (it's what keeps the
# binding 🟢), so it keeps reporting "this window went unwakeable" — exactly the
# failure we want surfaced. Staleness is the reader's job: if `ts` is older than
# a few heartbeat intervals, the daemon has stopped and the snapshot is suspect.
# ---------------------------------------------------------------------------


def _parse_status_from_agents(agents_text: str, agent_name: str) -> dict[str, Any]:
    """Parse `list_agents` rendered output into a wakeability snapshot.

    Each online agent is one line shaped like:
        🟢 **name** ⚡ 💤 (project) — bio...
    We read only the head (before the ` — ` bio separator) for markers so a
    bio that happens to contain ⚡/🟢/`**` can't skew the counts.

    Returns {online, wakeable, fleet_wakeable, fleet_total, focus_until} where
    online/wakeable/focus_until are this agent's own state and the fleet_* are
    totals across all listed (online) agents.

    🔕 is carried as an ABSOLUTE EXPIRY, never as the rendered "28m" or a bool.
    Three reasons, and they are the same ones that made the hub store an expiry
    rather than a flag:
      - a snapshot is up to a heartbeat old, so a stored countdown would be
        shown stale — an expiry lets every reader compute the truth itself;
      - focus that lapses while the daemon is dead then reads as OVER rather
        than frozen at whatever minute the last beat caught. A silencer that
        outlives its own expiry on screen is worse than one that isn't shown;
      - it survives the reader having no clock skew opinion: `remaining <= 0`
        is the same verdict everywhere.
    """
    fleet_total = 0
    fleet_wakeable = 0
    self_online = False
    self_wakeable = False
    self_focus_until = 0.0
    for line in agents_text.splitlines():
        head = line.split("—", 1)[0]
        if "🟢" not in head:
            continue  # not an agent row
        fleet_total += 1
        is_wakeable = "⚡" in head
        if is_wakeable:
            fleet_wakeable += 1
        if f"**{agent_name}**" in head:
            self_online = True
            self_wakeable = is_wakeable
            secs = _parse_focus_remaining(head)
            self_focus_until = (time.time() + secs) if secs > 0 else 0.0
    return {
        "online": self_online,
        "wakeable": self_wakeable,
        "fleet_wakeable": fleet_wakeable,
        "fleet_total": fleet_total,
        "focus_until": self_focus_until,
    }


def _parse_focus_remaining(head: str) -> float:
    """Seconds left on `🔕 45m` / `🔕 2h10m`, or 0 when the marker is absent.

    Mirrors server._fmt_minutes, which is the only writer of this text. Kept
    strict: an unrecognised shape returns 0 (no focus shown) rather than a
    guess, because inventing a duration would put a silencer on screen that
    the hub never reported.
    """
    m = re.search(r"🔕\s*(?:(\d+)h)?(\d+)m", head)
    if not m:
        return 0.0
    hours = int(m.group(1) or 0)
    return (hours * 60 + int(m.group(2))) * 60.0


def _status_cache_path(agent_name: str) -> pathlib.Path:
    """Per-agent status snapshot path under ~/.mcp-hub (alongside pidfiles)."""
    safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in agent_name)
    return _state_dir() / f"status-{safe}.json"


def _parse_squads_for_status(squads_text: str) -> dict[str, Any]:
    """Parse `list_squads(agent=...)` output into {squads, muted}.

    Reads what the HUB believes, not what the local config would derive. That
    distinction is the whole point of showing it: an agent whose workspace says
    'dreamteam' but which hasn't relaunched yet is still squadless on the hub,
    and the statusline must show the state that governs delivery rather than
    the one that will govern it after a restart.

    Rendered shape:
        <name> is in:
          dreamteam
          hublane  (muted)
    or a single line containing "belongs to no squad".
    """
    if "belongs to no squad" in squads_text:
        return {"squads": [], "muted": []}
    squads, muted = [], []
    for line in squads_text.splitlines()[1:]:
        entry = line.strip()
        if not entry:
            continue
        is_muted = entry.endswith("(muted)")
        name = entry.removesuffix("(muted)").strip()
        if name:
            squads.append(name)
            if is_muted:
                muted.append(name)
    return {"squads": squads, "muted": muted}


def _write_status_cache(
    agent_name: str, agents_text: str, squads_text: str = "",
) -> None:
    """Parse `agents_text` and atomically write this agent's status snapshot.

    Fail-soft by contract: a parse/write error must NEVER propagate into the
    heartbeat loop (a broken statusline cache is cosmetic; a broken heartbeat
    drops the binding). Atomic tmp+replace so the reader never sees a partial
    file.
    """
    try:
        status = _parse_status_from_agents(agents_text, agent_name)
        # Squads are OMITTED, not defaulted to [], when the caller had nothing
        # to pass — an older daemon or a hub without list_squads. The reader
        # must be able to tell "this agent is in no squad" from "this snapshot
        # doesn't know", because the first is a fact worth showing and the
        # second is a missing instrument. Defaulting to [] would render the
        # second as the first.
        if squads_text:
            status.update(_parse_squads_for_status(squads_text))
        status["agent"] = agent_name
        status["ts"] = int(time.time())
        path = _status_cache_path(agent_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(status), encoding="utf-8")
        tmp.replace(path)
    except Exception:  # noqa: BLE001
        pass


def _write_decisions_cache(decisions_json: str) -> None:
    """Atomically cache the hub's OPEN decision cards for local readers
    (squad board: hub-backed 🙋 vs legacy recap-flag disambiguation).
    All daemons on the box write the same file — last-write-wins is fine,
    they're all fetching the same global queue. Fail-soft like the status
    cache: this is display plumbing, never worth a dead heartbeat."""
    try:
        rows = json.loads(decisions_json) if decisions_json.strip() else []
        if not isinstance(rows, list):
            return
        path = _state_dir() / "decisions-open.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        # pid-suffixed tmp: every daemon on the box writes this file — a
        # shared tmp name would let two writers truncate each other mid-write
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(
            json.dumps({"ts": int(time.time()), "cards": rows}),
            encoding="utf-8",
        )
        tmp.replace(path)
    except Exception:  # noqa: BLE001
        pass


FLEET_BOARD_STALENESS_SECONDS = 45.0


def _parse_fleet_rows(agents_text: str) -> list[dict[str, Any]]:
    """Parse list_agents' rendered rows into structured records for the
    cockpit's Fleet view.

    Same head/bio split discipline as _parse_status_from_agents: markers are
    read only from the head (before the first em-dash) so a bio containing
    ⚡/🟢 can't skew the flags. The rendered format is ours (server.py
    list_agents) — if that ever changes, change this with it.

    `next` is the fleet-board convention: an agent that wants its "what's
    next" shown on every cockpit keeps a `next: <one line>` fragment in its
    bio (update_bio at milestones/sign-off). Absent → empty string, the view
    renders a dim placeholder.
    """
    rows: list[dict[str, Any]] = []
    for line in agents_text.splitlines():
        parts = line.split("—", 1)
        head = parts[0]
        bio = parts[1].strip() if len(parts) > 1 else ""
        if "🟢" not in head:
            continue  # not an agent row
        name_m = re.search(r"\*\*(.+?)\*\*", head)
        if not name_m:
            continue
        tail = head.split("**", 2)[-1]
        proj_m = re.search(r"\(([^)]*)\)", tail)
        sess_m = re.search(r"⚡×(\d+)", head)
        next_m = re.search(r"(?i)next:\s*(.+)$", bio)
        rows.append({
            "name": name_m.group(1).strip(),
            "project": proj_m.group(1) if proj_m else "",
            "wakeable": "⚡" in head,
            "idle": "💤" in head,
            "sessions": int(sess_m.group(1)) if sess_m else 1,
            "next": next_m.group(1).strip() if next_m else "",
        })
    return rows


def _fleet_board_path() -> pathlib.Path:
    return _state_dir() / "fleet-board.json"


def _write_fleet_board(agent_name: str, agents_text: str) -> None:
    """Write the box-wide fleet snapshot the cockpit's Fleet view renders.

    Rides the list_agents text the daemon already fetched for its own status
    cache — zero extra hub traffic. The staleness gate keeps N same-box
    daemons from rewriting an equivalent file every few seconds; last-writer-
    wins is safe because every writer parses the same source. Fail-soft like
    _write_status_cache: this file is cosmetic, the heartbeat is not.
    """
    try:
        path = _fleet_board_path()
        try:
            if time.time() - path.stat().st_mtime < FLEET_BOARD_STALENESS_SECONDS:
                return
        except OSError:
            pass  # no file yet — write it
        snap = {
            "ts": int(time.time()),
            "writer": agent_name,
            "agents": _parse_fleet_rows(agents_text),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(snap), encoding="utf-8")
        tmp.replace(path)
    except Exception:  # noqa: BLE001
        pass


def _mark_hub_disruption() -> None:
    """Record that the hub connection just broke, fleet-wide.

    A client's wake-receive stream does not survive a hub disconnect: after a
    redeploy / network flap / box sleep the agent reconnects and still reports
    ⚡, but pushes silently stop arriving — only a process relaunch reopens the
    stream (proven repeatedly; see the wake-stream memories). The daemon is the
    one component that *witnesses* the disconnect, so it leaves a breadcrumb
    `squad heal` can act on: any agent process older than this timestamp lived
    through the disruption and must be relaunched, not merely re-registered.

    One shared file (not per-agent) — a hub restart hits every agent, and the
    first daemon to notice speaks for the fleet. Fail-soft: never disturb the
    heartbeat loop.

    ONLY called when the hub has demonstrably LOST OUR BINDING after a
    connection break (see the caller) — i.e. the hub process itself restarted.
    It is deliberately NOT called for a bare transport error: a client-side
    network blip that the hub sat through leaves every binding intact, and
    stamping on those made a 5-second hiccup relaunch the entire squad
    (2026-07-20: a 15:46 blip restarted all 9 agents at 15:51, killing
    in-flight work and leaving one agent dead on a corrupted launch line).
    """
    try:
        path = _state_dir() / "hub-reconnect.stamp"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(str(int(time.time())), encoding="utf-8")
        tmp.replace(path)
    except Exception:  # noqa: BLE001
        pass


# The hub stamps every heartbeat reply with `hub_boot=<nonce>` — a fresh id per
# hub PROCESS. A change across a reconnect is POSITIVE evidence the hub
# restarted (so every wake stream is dead); an unchanged value means the hub
# sat there fine, whatever else broke. This replaces the old "no binding ⇒
# restarted" inference, which the reaper could satisfy without any restart —
# mass-restarting the fleet on a wifi flap (2026-07-20; reproved 2026-07-23).
_HUB_BOOT_RE = re.compile(r"hub_boot=([0-9a-f]+)")


def _parse_hub_boot(reply: str) -> str | None:
    """The hub's process nonce from a heartbeat reply, or None if the reply
    carries no marker (an OLD hub predating the nonce — no positive evidence)."""
    m = _HUB_BOOT_RE.search(reply or "")
    return m.group(1) if m else None


def _seen_boot_path() -> pathlib.Path:
    return _state_dir() / "hub-boot.seen"


def _read_seen_boot() -> str | None:
    try:
        val = _seen_boot_path().read_text(encoding="utf-8").strip()
        return val or None
    except Exception:  # noqa: BLE001
        return None


def _write_seen_boot(nonce: str) -> None:
    # Persisted to disk (not just process memory) so the comparison is "last
    # nonce this BOX saw", not "this PROCESS saw" — a hub restart that happens
    # while the daemon is itself down is then still caught on its next connect,
    # instead of becoming an invisible false-negative (mcp-hub-fireblade's
    # edge case 2). Shared per-box like the stamp; atomic tmp+replace so racing
    # daemons never write a torn value.
    try:
        path = _seen_boot_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(nonce, encoding="utf-8")
        tmp.replace(path)
    except Exception:  # noqa: BLE001
        pass


def _maybe_stamp_hub_restart(reply: str) -> bool:
    """Detect a genuine hub RESTART from a heartbeat reply's process nonce and
    leave the squad-heal breadcrumb if so. Returns True iff it stamped.

    Safe to call on EVERY heartbeat (the nonce is persisted per-box), so a
    restart during this daemon's own downtime is still caught on reconnect.
    Every branch fails toward NOT stamping — a false stamp mass-restarts the
    whole fleet, a missed one is per-agent-recoverable:
      * no nonce in reply (old hub)        -> no stamp, state untouched
      * no prior nonce on this box (first)  -> record baseline, no stamp
      * nonce unchanged                     -> no-op
      * nonce CHANGED                        -> STAMP + record the new nonce
    """
    curr = _parse_hub_boot(reply)
    if curr is None:
        return False
    prev = _read_seen_boot()
    if prev is None:
        _write_seen_boot(curr)  # first sighting on this box — baseline only
        return False
    if curr == prev:
        return False
    _mark_hub_disruption()
    _write_seen_boot(curr)
    return True


def _source_head(src_dir: pathlib.Path | None = None) -> str | None:
    """HEAD of the git checkout this module runs from, or None when that can't
    be established (non-editable install, git missing/slow, not a repo).

    None disables the staleness check entirely — fail-open, the daemon must
    never crash or churn over an unreadable tree. HEAD, not file mtime: mtime
    fires DURING a `git pull` while the tree is half-old/half-new (respawning
    then = a crash loop mid-deploy); HEAD moves once, atomically, after the
    checkout completes.
    """
    if src_dir is None:
        src_dir = pathlib.Path(__file__).resolve().parent
    try:
        out = subprocess.run(  # noqa: S603, S607
            ["git", "-C", str(src_dir), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=_NO_WINDOW_FLAG,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    head = out.stdout.strip()
    return head if out.returncode == 0 and head else None


async def _heartbeat_loop(hub_url: str, agent_name: str) -> bool:
    """Long-lived loop: connect to hub, ping `heartbeat(agent_name)` every
    HEARTBEAT_INTERVAL_SECONDS. On any connection error, sleep and reconnect.

    Returns True when the source checkout's HEAD has moved under the running
    process — the code on disk is newer than the code in memory, and the
    caller should respawn a successor from the new tree. A long-lived daemon
    that never re-reads its code is how a shipped fix sits inert fleet-wide:
    the singleton is old-wins, so across session relaunches the SURVIVING
    daemon is always the oldest one (2026-07-23: the hub-restart nonce
    detector shipped and not one daemon ran it). Checked once per heartbeat
    (~60s) — cheap, and a minute of lag on code adoption is nothing.

    Single MCP session is held open across heartbeats — this is the right
    shape because heartbeat doesn't bind, so the session lifetime is just
    a connection-pooling concern, not a wake-target concern.

    The status cache is refreshed every STATUS_REFRESH_SECONDS (snappy
    statusline) while heartbeat fires every HEARTBEAT_INTERVAL_SECONDS (binding
    keep-alive) — decoupled so a register/offline change shows up promptly
    instead of up to a full heartbeat late. list_agents failing is treated as a
    connection problem (propagates to the reconnect handler, same as a failed
    heartbeat); only the parse/write step is fail-soft (inside
    _write_status_cache).
    """
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    # Disruption detection is now driven by the hub's process nonce carried in
    # every heartbeat reply (see _maybe_stamp_hub_restart), NOT by inferring a
    # restart from "no binding". The old inference false-positived: a blip long
    # enough for the reaper to drop the binding made the post-reconnect beat
    # report "no binding" with the SAME hub still running, and squad heal then
    # mass-restarted the whole fleet for a wifi flap (2026-07-20; reproved
    # 2026-07-23). The nonce is checked every beat (not just after a witnessed
    # break) and persisted per-box, so a restart during the daemon's own
    # downtime is still caught on reconnect.
    baseline_head = _source_head()
    # Bar 47's beat half. One line per agent per UTC hour, not per beat: the
    # measurement is hourly, and 1440 lines a day per lane would need rotation
    # the nag-log pattern deliberately does without. In-process, so a daemon
    # respawn may re-log the hour it lands in — harmless, this is a PRESENCE
    # marker, never a count.
    beat_hour = ""
    while True:
        try:
            async with streamablehttp_client(
                _ephemeral_hub_url(hub_url), timeout=10
            ) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    # Force a heartbeat on the first tick after each (re)connect.
                    since_heartbeat = HEARTBEAT_INTERVAL_SECONDS
                    while True:
                        if since_heartbeat >= HEARTBEAT_INTERVAL_SECONDS:
                            hb = await session.call_tool(
                                "heartbeat", {"agent_name": agent_name}
                            )
                            _maybe_stamp_hub_restart(_extract_text(hb))
                            since_heartbeat = 0
                            beat_hour = _log_beat_if_new_hour(
                                agent_name, beat_hour)
                            if baseline_head is not None:
                                head_now = _source_head()
                                if (
                                    head_now is not None
                                    and head_now != baseline_head
                                ):
                                    print(
                                        "[mcp-hub heartbeat] source HEAD moved "
                                        f"({baseline_head[:8]} -> {head_now[:8]});"
                                        " respawning on the new tree",
                                        file=sys.stderr,
                                    )
                                    return True
                        agents_result = await session.call_tool("list_agents", {})
                        agents_text = _extract_text(agents_result)
                        # Squad membership for the statusline. Fail-soft: an
                        # older hub has no list_squads, and a missing squad
                        # segment is cosmetic where a broken heartbeat drops
                        # the binding.
                        squads_text = ""
                        try:
                            squads_text = _extract_text(
                                await session.call_tool(
                                    "list_squads", {"agent": agent_name}
                                )
                            )
                        except Exception:  # noqa: BLE001
                            pass
                        _write_status_cache(agent_name, agents_text, squads_text)
                        _write_fleet_board(agent_name, agents_text)
                        # Open decision cards → local cache, so the board can
                        # tell a hub-backed 🙋 from a legacy recap flag.
                        # Fail-soft (older hub lacks the tool).
                        try:
                            dl = await session.call_tool(
                                "decision_list",
                                # explicit high limit — the default (50)
                                # would silently drop queue overflow, and a
                                # silent cap reads as "covered everything"
                                {"status": "open", "format": "json",
                                 "limit": 500},
                            )
                            _write_decisions_cache(_extract_text(dl))
                        except Exception:  # noqa: BLE001
                            pass
                        await asyncio.sleep(STATUS_REFRESH_SECONDS)
                        since_heartbeat += STATUS_REFRESH_SECONDS
        except Exception as exc:  # noqa: BLE001
            # Connection / init / call failure — log and reconnect after a
            # delay. Fail-open: heartbeat outages don't crash the daemon. No
            # stamp here — the next heartbeat's nonce check decides whether the
            # hub actually restarted, which is robust to reaper-dropped
            # bindings that merely look like a restart.
            print(
                f"[mcp-hub heartbeat] connection error ({type(exc).__name__}: "
                f"{exc}); retrying in {HEARTBEAT_RETRY_DELAY_SECONDS}s",
                file=sys.stderr,
            )
            await asyncio.sleep(HEARTBEAT_RETRY_DELAY_SECONDS)


def heartbeat_daemon_command(args: argparse.Namespace) -> int:
    """Run the heartbeat-daemon subcommand. Long-running; only returns on
    KeyboardInterrupt or unrecoverable error.

    Designed to be spawned by an async SessionStart hook in
    ~/.claude/settings.json. On POSIX the OS reaps the daemon when Claude
    Code exits; on Windows it does not, so we enforce one-daemon-per-agent
    via `_claim_singleton`: the first daemon for an agent atomically claims a
    pidfile and runs; any later daemon sees a live owner and stands down
    instead of looping forever. This caps accumulation at one per agent. See
    the _claim_singleton block comment for the old-wins rationale.
    """
    name, _project = _resolve_agent_identity(args)
    if name is None:
        # Silent no-op — same fail-open contract as stop-hook. Lets the
        # global SessionStart hook fire in every project without
        # needing per-project opt-out for non-hub projects.
        return 0

    pidfile = _claim_singleton(name)
    if pidfile is None:
        # A live daemon already owns this agent — stand down rather than
        # leak a second one. This is the fix for the daemon accumulation.
        return 0
    respawn = False
    try:
        respawn = asyncio.run(_heartbeat_loop(args.hub_url, name))
    except KeyboardInterrupt:
        return 0
    finally:
        _release_singleton(pidfile)
    if respawn:
        # The finally above already released the pidfile, so the successor's
        # singleton claim finds it free (old-wins would otherwise make the
        # successor stand down against us). Brief grace so a checkout that
        # JUST finished moving HEAD has flushed the tree before the successor
        # imports from it. If the spawn fails, the Stop-hook self-heal
        # respawns a daemon at the agent's next turn boundary — fail-open.
        time.sleep(3)
        _spawn_daemon_detached(name, args.hub_url)
    return 0


# ---- seat-entry, shared by both shapes -------------------------------------
#
# 1:1 and N:1 differ only in HOW MANY inhabitants a container has, so they run
# the same three steps per agent — prepare, launch, supervise — rather than two
# implementations free to drift. Contract: docs/n-seats-per-container.md.


def _seat_prepare(contract: Any, workdir: pathlib.Path) -> tuple[Any, int | None]:
    """Clone if needed, re-derive the project, write the per-agent files.

    Returns (contract, exit_code). The contract comes BACK because the project
    is re-derived from the clone's origin, and it is frozen.
    """
    from mcp_hub.edge import seed_first_launch
    from mcp_hub.seat import (
        EXIT_CONTRACT,
        marker_content,
        mcp_json_content,
    )

    workdir.mkdir(parents=True, exist_ok=True)
    if contract.repo and not any(workdir.iterdir()):
        from mcp_hub.seat import SEAT_GITHUB_TOKEN, https_repo_url

        # The spec's URL is usually an ssh alias (`git@github-monkeypashion:`)
        # that exists only in ONE machine's ~/.ssh/config. Inside a container
        # it resolves to nothing, so it is normalised to github.com https,
        # which the token can actually authenticate.
        url = https_repo_url(contract.repo)
        if not url:
            print(
                f"seat-entry: REFUSED (contract): cannot read an org/repo out "
                f"of '{contract.repo}'. Refusing to guess — a wrong URL would "
                f"clone the WRONG repo under the right name, and nothing "
                f"downstream could tell.",
                file=sys.stderr, flush=True,
            )
            return contract, EXIT_CONTRACT
        if not (os.environ.get(SEAT_GITHUB_TOKEN) or "").strip():
            # At the door, loudly, like the Anthropic credential — not three
            # git errors deep. A container has no ssh key and no credential
            # store, so a private repo is simply unreachable without this.
            print(
                f"seat-entry: REFUSED (contract): {contract.identity} declares "
                f"a repo but {SEAT_GITHUB_TOKEN} is not set, and a container has no "
                f"ssh key or credential store to fall back on. Inject it the "
                f"same way as the Anthropic credential: add {SEAT_GITHUB_TOKEN} to "
                f"the edge host's ~/.mcp-hub/edge-env and name it in the "
                f"seat spec's env_from_host. The hub stores the NAME only.",
                file=sys.stderr, flush=True,
            )
            return contract, EXIT_CONTRACT
        clone = subprocess.run(
            ["git", "clone", url, str(workdir)],
            capture_output=True,
            text=True,
        )
        if clone.returncode != 0:
            # git echoes the URL it tried; with a credential HELPER (rather
            # than a token embedded in the URL) there is no secret in it.
            print(
                f"seat-entry: REFUSED (contract): clone of "
                f"{url} failed: {clone.stderr.strip()}",
                file=sys.stderr,
                flush=True,
            )
            return contract, EXIT_CONTRACT

    # Re-derive the project from the (possibly just-cloned) origin, exactly
    # the way the cli derives it everywhere else.
    origin = _git_remote_url(str(workdir))
    if origin and not _explicit_project(contract):
        parsed = _parse_org_repo(origin)
        if parsed:
            contract = replace(contract, project=f"{parsed[0]}/{parsed[1]}")

    # Identity marker — the ASSIGNED identity wins because the repo is NOT
    # opted into ~/.mcp-hub/config.json (derivation never applies).
    marker = workdir / AGENT_MARKER_PATH
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        json.dumps(marker_content(contract), indent=2), encoding="utf-8"
    )

    # .mcp.json generated from MCP_HUB_URL — never baked into the image.
    # PROJECT scope, never user scope: in a pod the HOME is shared, so an
    # `?agent=` stamp in a user-scope file would push one agent's DMs into
    # another's session — the 2026-07-27 misroute, which needed exactly a
    # shared file to happen.
    (workdir / ".mcp.json").write_text(
        json.dumps(mcp_json_content(contract), indent=2), encoding="utf-8"
    )

    # The BRIEF and its material. Written before launch so the first turn can
    # tell the agent to read a file that is already there.
    #
    # NEVER overwritten: a seat restarts (container restart, `edge apply`
    # re-realizing it) and re-runs this whole function. Clobbering BRIEF.md
    # would be tolerable; clobbering an INPUT the agent has been editing all
    # session would destroy work with no trace, which is why both go through
    # the same skip-if-present rule rather than only the one that obviously
    # matters.
    from mcp_hub.seat import brief_files

    for rel, content in brief_files(contract).items():
        path = workdir / rel
        if path.exists():
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    # Folder trust — the placement is the operator's explicit trust act.
    seed_first_launch(str(workdir))
    return contract, None


def _emit(chunk: bytes, prefix: str, lock: Any, pending: list[bytes],
          final: bool = False) -> None:
    """One agent's bytes onto the CONTAINER's shared stdout.

    Two modes, and the difference is load-bearing:

    * **No prefix (1:1)** — bytes go straight through, byte-exact and
      unbuffered. This is the shipped, container-proven path and the
      incrementality probe watches it; nothing here may add a layer.
    * **Prefixed (pod)** — N turns share one stdout, so raw writes would
      interleave mid-line and `docker logs` would be unreadable exactly when
      several agents are working, which is the whole point of a pod. Lines are
      therefore completed before they are written, tagged with the agent that
      produced them, under a lock.

    A LINE is the unit rather than a chunk: prefixing chunks would stamp the
    identity into the middle of sentences. The per-agent `output.log` stays
    byte-exact either way — the artifact is the clean copy, this is the
    human-readable multiplex.
    """
    if not prefix:
        if chunk:
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()
        return
    pending[0] += chunk
    if final:
        lines, pending[0] = (pending[0].split(b"\n") if pending[0] else []), b""
    else:
        parts = pending[0].split(b"\n")
        lines, pending[0] = parts[:-1], parts[-1]
    out = b"".join(f"[{prefix}] ".encode() + ln + b"\n"
                   for ln in lines if ln or not final)
    if not out:
        return
    with (lock or contextlib.nullcontext()):
        sys.stdout.buffer.write(out)
        sys.stdout.buffer.flush()


def _seat_headless(contract: Any, workdir: pathlib.Path, prefix: str = "",
                   lock: Any = None) -> tuple[int, dict]:
    """One-shot `claude -p`: teed to the memory volume, bounded, then exit.

    Container-proven 2026-08-08 (throwaway on :latest, trivial prompt,
    rc=0) — the "UNVERIFIED END TO END" warning this paragraph replaced is
    retired by that run, not by any test suite.

    Three properties, each bought by a measurement:

    * **TEE, never capture.** Output still reaches stdout AS IT ARRIVES —
      `docker logs -f` on a long errand must show progress, so
      `capture_output=True` (the obvious way to get bytes for the artifact)
      would silently regress live visibility exactly where watching matters
      most. And teed INCREMENTALLY, so a killed turn leaves its partial
      output behind — that partial is what diagnoses the hang.
    * **The artifact outlives the container.** stdout dies with `docker rm`,
      and reclaim's harvest (`docker exec`) REFUSES on an exited container
      (both measured) — so the result is written under ~/.claude, the one
      directory the memory volume makes durable, BEFORE the process exits.
      No volume mounted there → refused at the door instead (see the
      dispatch site): a headless seat whose result provably dies with it is
      a contract violation, same family as headless-without-a-prompt.
    * **Bounded.** Nobody watches a headless seat, so a hung turn would hold
      the container — and its `running` report — forever. SEAT_TIMEOUT
      (headless default 1800s) kills it and records 124, timeout(1)'s own
      word for it; the partial output.log is still written.
    """
    import threading

    from mcp_hub.seat import (
        EXIT_TIMEOUT,
        headless_result_paths,
        headless_verdict,
        launch_argv,
    )

    paths = headless_result_paths(str(pathlib.Path.home()), contract.identity)
    pathlib.Path(paths["output"]).parent.mkdir(parents=True, exist_ok=True)

    bound = f", timeout {contract.timeout}s" if contract.timeout else ""
    print(f"seat-entry: headless — one turn, then exit "
          f"({contract.identity}{bound})", flush=True)
    pending = [b""]

    # stderr stays inherited (straight to docker logs): merging it into
    # stdout would corrupt the JSON record result.json is parsed from.
    proc = subprocess.Popen(
        launch_argv(contract, str(workdir)), cwd=str(workdir),
        stdout=subprocess.PIPE,
    )
    timed_out = False

    def _expire() -> None:
        nonlocal timed_out
        timed_out = True
        proc.kill()

    timer = (threading.Timer(contract.timeout, _expire)
             if contract.timeout else None)
    if timer:
        timer.start()
    chunks: list[bytes] = []
    try:
        with open(paths["output"], "wb") as log:
            assert proc.stdout is not None
            while True:
                # read1, NOT read: on a BufferedReader, read(n) blocks until
                # n bytes OR EOF, so the tee would buffer a long errand's
                # output until 4096 bytes piled up or the turn ended — both
                # live properties gone while every terminating test stays
                # green (termination flushes; only a mid-run probe can tell
                # the two apart — see test_tee_is_incremental_mid_run).
                # Measured: read(4096) sat 2.5s on `echo FIRST; sleep 2.5;
                # echo SECOND` and returned both lines together; read1
                # returned FIRST at 0.00s. Caught by mcp-hub-fireblade-wsl
                # in review.
                chunk = proc.stdout.read1(4096)
                if not chunk:
                    break
                chunks.append(chunk)
                log.write(chunk)
                log.flush()
                _emit(chunk, prefix, lock, pending)
            _emit(b"", prefix, lock, pending, final=True)
        rc = proc.wait()
    finally:
        if timer:
            timer.cancel()
    if timed_out:
        rc = EXIT_TIMEOUT
        print(f"seat-entry: headless turn KILLED after {contract.timeout}s — "
              f"partial output is in the artifact", flush=True)

    output = b"".join(chunks).decode("utf-8", errors="replace")
    doc = headless_verdict(rc, timed_out, output)
    doc["identity"] = contract.identity
    pathlib.Path(paths["result"]).write_text(
        json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    pathlib.Path(paths["exit_code"]).write_text(f"{rc}\n", encoding="utf-8")

    print(f"seat-entry: headless turn finished rc={rc} — result at "
          f"{paths['result']}", flush=True)
    return rc, doc


def _seat_headless_pod(prepared: list) -> int:
    """N briefed agents, one turn each, one container exit code.

    The overnight-spike shape: "here is the question, come back to results."

    CONCURRENT, not sequential. A spike team's members are working on the same
    question independently, so serializing them would make a 3-agent pod take
    three times as long for no gain — and the timeout is per-agent, so a
    sequential pod's worst case is N×timeout, which is not a bound anyone
    would recognise as one. One credential serves all N (measured 2026-08-06:
    three concurrent seats on one OAuth token).

    Each agent writes its OWN artifact; the pod writes a summary beside them.
    The container's single exit code answers only "did every agent succeed",
    because that is the only question the reconciler can act on — everything
    else lives in the summary, where it can be read rather than inferred.
    """
    import threading

    from mcp_hub.seat import (
        EXIT_PARTIAL,
        headless_pod_summary_path,
        headless_pod_verdict,
    )

    lock = threading.Lock()
    results: dict[str, dict] = {}
    threads = []

    def _one(contract: Any, workdir: pathlib.Path) -> None:
        try:
            _rc, doc = _seat_headless(contract, workdir,
                                      prefix=contract.identity, lock=lock)
        except Exception as e:  # noqa: BLE001
            # One agent blowing up must not take the pod's OTHER results with
            # it — they ran, their artifacts are on disk, and losing the
            # summary would hide them. Recorded as a failure, loudly.
            doc = {"identity": contract.identity, "exit_code": -1,
                   "timed_out": False, "error": f"{type(e).__name__}: {e}"}
            print(f"seat-entry: {contract.identity} raised {e}",
                  file=sys.stderr, flush=True)
        results[contract.identity] = doc

    print(f"seat-entry: headless POD — {len(prepared)} agent(s), one turn "
          f"each, concurrently", flush=True)
    for _session, contract, workdir in prepared:
        t = threading.Thread(target=_one, args=(contract, workdir))
        t.start()
        threads.append(t)
    for t in threads:
        t.join()

    # Ordered by the manifest, not by finish time: a summary whose rows move
    # between runs is one nobody can diff.
    ordered = [results[c.identity] for _s, c, _w in prepared
               if c.identity in results]
    summary, rc = headless_pod_verdict(ordered)
    path = pathlib.Path(headless_pod_summary_path(str(pathlib.Path.home())))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")

    if rc == 0:
        print(f"seat-entry: pod complete — {summary['succeeded']}/"
              f"{summary['agents']} succeeded. Summary: {path}", flush=True)
    else:
        # NAMED, never counted. The fix is per-agent, so "2 failed" would send
        # the operator hunting through every agent directory to find which.
        print(f"seat-entry: pod PARTIAL (exit {EXIT_PARTIAL}) — "
              f"{summary['succeeded']}/{summary['agents']} succeeded; failed: "
              f"{', '.join(summary['failed']) or 'none'}"
              + (f"; timed out: {', '.join(summary['timed_out'])}"
                 if summary['timed_out'] else "")
              + f". Summary: {path}", file=sys.stderr, flush=True)
    return rc


def _seat_launch(contract: Any, workdir: pathlib.Path, session: str) -> int | None:
    """Start claude, answer the one dialog it may show, send the first turn."""
    from mcp_hub.seat import (
        EXIT_CONTRACT,
        first_turn_is_safe,
        first_turn_prompt,
        launch_argv,
        pane_is_settled,
        startup_dance_action,
    )

    launched = subprocess.run(
        launch_argv(contract, str(workdir), session=session),
        capture_output=True, text=True,
    )
    if launched.returncode != 0:
        print(
            f"seat-entry: launch failed ({session}): {launched.stderr.strip()}",
            file=sys.stderr,
            flush=True,
        )
        return 1

    # Launch dance — claude stops on the development-channels dialog and
    # WAITS (measured on the first live seat: container healthy, claude
    # parked, never an agent). squad answers this on the host; a container
    # has to answer it for itself.
    pane = ""
    for _ in range(25):
        time.sleep(1)
        pane = subprocess.run(
            ["tmux", "capture-pane", "-p", "-t", session],
            capture_output=True,
            text=True,
        ).stdout
        key = startup_dance_action(pane)
        if key:
            subprocess.run(["tmux", "send-keys", "-t", session, key])
            continue
        if pane_is_settled(pane):
            break

    # Never type into a dialog. The bypass-mode acceptance screen defaults
    # to "No, exit", so the first-turn Enter confirmed the seat's own death
    # — cleanly, exit 0, with nothing anywhere that looked wrong. A future
    # unknown dialog gets the same treatment: refuse LOUDLY with a code
    # `docker ps` can show, and print the pane so the screen that stopped
    # us is in the log rather than in a tmux buffer that dies with it.
    if not first_turn_is_safe(pane):
        print(
            f"seat-entry: REFUSED (contract): claude ({session}) is showing a "
            "dialog this seat does not know how to answer, and a blind "
            "keypress would confirm whatever its default row is. Pane "
            f"follows:\n{pane}",
            file=sys.stderr,
            flush=True,
        )
        return EXIT_CONTRACT
    print(f"seat-entry: claude is up ({session})", flush=True)

    # First turn. The register instruction rides in SessionStart's
    # additionalContext, which only a RUNNING TURN consumes — and a
    # container has no operator to type one. Without this the seat idles
    # forever: hooks fired, daemon alive, ~/.claude/projects/ empty,
    # never an agent (measured 2026-08-04).
    #
    # -l (literal) so nothing in the text is read as a key name; Enter is
    # a SEPARATE send-keys because -l would type the word "Enter".
    subprocess.run(
        ["tmux", "send-keys", "-t", session, "-l", first_turn_prompt(contract)]
    )
    time.sleep(1)  # let the TUI ingest the paste before submitting
    subprocess.run(["tmux", "send-keys", "-t", session, "Enter"])
    print(
        f"seat-entry: first turn sent ({session}) — the seat registers itself",
        flush=True,
    )
    return None


def _seat_supervise(seats: list[tuple[str, Any]]) -> int:
    """Outlive the sessions, and nudge any agent that falls off the hub.

    PID 1 must outlive the detached tmux sessions or docker reaps the
    container while claude runs. It exits when the LAST session ends, so a
    1:1 container still stops with its single seat.

    It also SUPERVISES: every hub deploy drops all bindings, and a
    human-driven agent re-registers at its next turn boundary because someone
    types. An idle container has no turn boundary, so it stays silently
    offline forever (measured: up 5 hours, healthy, absent from the hub).
    PID 1 is the container's substitute for an operator noticing.

    It supervises REGISTRATION, never session EXISTENCE (decision
    2026-08-07): a session that has gone stays gone. Recreating one would
    resurrect an agent the operator deliberately killed and leave them
    fighting the container's own init for the only per-agent control there
    is.
    """
    from mcp_hub.seat import (
        first_turn_is_safe,
        first_turn_prompt,
        needs_reregister,
    )

    # The FIRST TURN counts as the first nudge: it is a registration attempt,
    # so both the cooldown and the evidence gate run from it. Measured
    # otherwise — the supervisor fired 6s later on a status cache written
    # before the seat had registered, typing into a pane mid-registration.
    last_nudge = {session: time.time() for session, _ in seats}
    while True:
        alive = [
            (session, contract) for session, contract in seats
            if subprocess.run(
                ["tmux", "has-session", "-t", session], capture_output=True
            ).returncode == 0
        ]
        if not alive:
            return 0
        time.sleep(5)

        for session, contract in alive:
            # Rate-limited PER AGENT: one nudge per SUPERVISOR_COOLDOWN at
            # most. A re-register takes a turn to happen and a further daemon
            # cycle to show up in the status cache, so a faster loop would
            # type into the pane repeatedly while the first nudge was still
            # working.
            if time.time() - last_nudge[session] < SUPERVISOR_COOLDOWN:
                continue
            status_path = (
                pathlib.Path.home() / ".mcp-hub"
                / f"status-{contract.identity}.json"
            )
            try:
                status = json.loads(status_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                status = None
            if not needs_reregister(status, time.time(),
                                    after=last_nudge[session]):
                continue
            pane = subprocess.run(
                ["tmux", "capture-pane", "-p", "-t", session],
                capture_output=True, text=True,
            ).stdout
            # Same rule as the first turn: never type into a dialog, and never
            # interrupt a turn in progress — a nudge mid-work would be worse
            # than the offline state it is fixing.
            if not first_turn_is_safe(pane):
                continue
            subprocess.run(
                ["tmux", "send-keys", "-t", session, "-l",
                 first_turn_prompt(contract)]
            )
            time.sleep(1)
            subprocess.run(["tmux", "send-keys", "-t", session, "Enter"])
            last_nudge[session] = time.time()
            print(
                f"seat-entry: {contract.identity} was offline on the hub — "
                "re-register nudge sent",
                flush=True,
            )


def _explicit_project(contract: Any) -> bool:
    """Whether the project was ASSIGNED rather than defaulted to the identity.

    An explicit project outranks the origin, so re-deriving must not overwrite
    one. The identity default is what a derivation is allowed to replace.
    """
    return bool(contract.project) and contract.project != contract.identity


def _seat_home_setup() -> None:
    """The per-CONTAINER steps, which settle once however many agents share it.

    Theme, onboarding and the bypass acceptance are HOME-level, so a pod pays
    them once rather than N times — the three worst of the six gates between
    a running container and an agent on the hub are not multiplied by N.
    """
    from mcp_hub.seat import (
        SEAT_GITHUB_TOKEN,
        credential_helper_argv,
        hooks_settings_content,
    )

    # Git credential helper, once per container, only when a token is present.
    # It READS the env var at the moment git asks, so the token is never
    # written to disk — unlike a token embedded in the clone URL, which
    # persists verbatim in .git/config as remote.origin.url, survives the
    # container, shows up in `git remote -v`, and would be read back by our own
    # project derivation.
    if (os.environ.get(SEAT_GITHUB_TOKEN) or "").strip():
        subprocess.run(credential_helper_argv(), capture_output=True)

    # Hook settings: write only if absent. A memory_volume mounted at
    # ~/.claude may carry a previous seat's (identical) settings — or an
    # operator's deliberate ones, which are not ours to clobber.
    settings_path = pathlib.Path.home() / ".claude" / "settings.json"
    if settings_path.exists():
        print(
            f"seat-entry: {settings_path} exists — left untouched",
            file=sys.stderr,
            flush=True,
        )
    else:
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(
            json.dumps(hooks_settings_content(), indent=2), encoding="utf-8"
        )


def _seat_onboarding() -> None:
    """Tell claude its first-run wizard is done.

    A container's HOME is fresh, so without these claude opens the onboarding
    wizard and BLOCKS — a seat that looks perfectly healthy to `docker ps` and
    never becomes an agent. Merged into the same file seed_first_launch wrote
    (read-modify-write, never a fresh dict) so neither seed erases the other,
    which is why every seed runs before this.
    """
    from mcp_hub.seat import onboarding_state

    claude_json = pathlib.Path.home() / ".claude.json"
    try:
        state = json.loads(claude_json.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            raise ValueError("not an object")
    except (OSError, ValueError):
        state = {}
    # "2.1.221 (Claude Code)" -> "2.1.221"; unknown version still marks
    # onboarding done (a wrong-but-present version re-onboards once, an
    # absent key blocks forever). A MISSING binary is the extreme case of
    # unknown — tolerated for the same reason, and it opens no silent path:
    # in a container the claude launch itself fails loudly moments later,
    # while on a claude-less machine (CI runner) this function must not be
    # the thing that dies. Found by the first-ever bare-runner CI run.
    try:
        ver = subprocess.run(
            ["claude", "--version"], capture_output=True, text=True
        )
        version = (ver.stdout.strip().split() or ["0.0.0"])[0]
    except FileNotFoundError:
        version = "0.0.0"
    state.update(onboarding_state(version))
    claude_json.parent.mkdir(parents=True, exist_ok=True)
    claude_json.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _parse_pod_agents(specs: list[str]) -> list[dict[str, str]]:
    """`--agent identity[=repo[,squads]]` → the manifest's agents list.

    `=` rather than `:` as the separator, because every real repo value here
    is an ssh URL (`git@github-org:org/repo.git`) and splitting those on `:`
    would cut the URL in half — a separator that breaks on the ONLY values
    anyone passes is not a separator.
    """
    out = []
    for raw in specs:
        ident, _sep, rest = raw.partition("=")
        repo, _sep2, squads = rest.partition(",")
        row = {"identity": ident.strip()}
        if repo.strip():
            row["repo"] = repo.strip()
        if squads.strip():
            row["squads"] = squads.strip()
        out.append(row)
    return out


_VOICE_LOG = "/tmp/voice-client.log"  # noqa: S108 — in-container, readable by exec


def _seat_pulse() -> bool:
    """Start the container's OWN pulse server + null sink. Fail-open.

    🔴 WHY THIS EXISTS: the image installed `pulseaudio` and never ran it, so
    `voice-client` connected to the host, received audio, and had **nowhere to
    put it** — `pacat` and `arecord` both died with "Connection refused"
    (measured in a throwaway container, mcp-hub-dev-vm-1-general 2026-08-08).
    The client half was fail-open and silent, so a seat with completely broken
    audio started, registered and looked healthy.

    Deliberately EXPLICIT rather than via `autospawn`: this runs before the
    client, in a known order, from `/etc/pulse/seat-voice.pa` — which loads a
    null sink, because a container has no hardware and therefore no sink at
    all. Autospawn would produce a running server with nothing to play into.

    Returns whether a server is answering. Never raises: audio must not be able
    to fail a seat.
    """
    try:
        # Idempotent: seat-entry can run again in a recreated container, and a
        # second server would fail to bind rather than replace the first.
        if subprocess.run(["pactl", "info"], capture_output=True,
                          timeout=10).returncode == 0:
            return True
        subprocess.run(
            ["pulseaudio", "--daemonize=yes", "--exit-idle-time=-1",
             "-n", "--file=/etc/pulse/seat-voice.pa"],
            capture_output=True, timeout=30, check=False,
        )
        r = subprocess.run(["pactl", "info"], capture_output=True, timeout=10)
        if r.returncode == 0:
            print("seat-entry: /voice pulse server up (sink claude_mic)",
                  flush=True)
            return True
        # LOUD about a broken audio stack, while still not failing the seat.
        # Silence here is what turned a one-line diagnosis into a container
        # teardown; the seat survives either way.
        print("seat-entry: /voice UNAVAILABLE — pulse server did not start; "
              f"pactl says: {(r.stderr or b'').decode('utf-8', 'replace').strip()[:160]}",
              file=sys.stderr, flush=True)
        return False
    except Exception as exc:  # noqa: BLE001 — audio never fails a seat
        print(f"seat-entry: /voice UNAVAILABLE — pulse not started ({exc})",
              file=sys.stderr, flush=True)
        return False


def _seat_voice() -> None:
    """Start the container's /voice client, detached, fail-open.

    Every failure mode here resolves to "no audio": no binary, no gateway, no
    host listening. None of them may touch the seat's exit path, which is why
    nothing is awaited and every exception is swallowed.

    ⚠️ The client's stderr goes to a LOG, never to DEVNULL. It used to be
    discarded, so `voice-client`'s own "Connection refused" — the message that
    would have identified the missing pulse server immediately — was thrown
    away at the moment it was produced. Fail-open must not mean fail-silent.
    """
    if not _seat_pulse():
        # No server means the client has nowhere to play. Starting it anyway
        # would spin a process that can only fail, and say so nowhere.
        print("seat-entry: /voice client NOT started (no pulse server)",
              file=sys.stderr, flush=True)
        return
    try:
        log = open(_VOICE_LOG, "ab", buffering=0)  # noqa: SIM115 — outlives us
        subprocess.Popen(
            [sys.executable, "-m", "mcp_hub.cli", "voice-client"],
            stdout=log, stderr=log, start_new_session=True,
        )
        print(f"seat-entry: /voice client started (log {_VOICE_LOG})",
              flush=True)
    except Exception as exc:  # noqa: BLE001 — audio never fails a seat
        print(f"seat-entry: /voice not started ({exc})", file=sys.stderr, flush=True)


def voice_client_command(args: argparse.Namespace) -> int:
    """In-container: pull audio from the host and feed this container's sink.

    Runs as a child of seat-entry, and **must never be able to fail a seat**.
    Voice is a convenience; a seat with no microphone is a working seat, and a
    seat that refuses to start because the audio host is down is not.

    NOTE what this does NOT do: bind, listen, or accept. The container is a
    CLIENT only, which is what makes container-to-container injection have no
    path rather than a filtered one (docs/seat-voice.md — v2 died to a
    firewall rule that was present and inert).
    """
    import socket
    import subprocess as _sp

    from mcp_hub.voice import VOICE_CHANNELS, VOICE_RATE, default_gateway, open_stream

    # Identity in preference order, and the order is the point:
    #   SEAT_CONTAINER  the container's own name, injected for BOTH shapes
    #   SEAT_IDENTITY   the 1:1 seat (a pod has none)
    #   hostname        an ADOPTED container, created by something that is not
    #                   this edge and therefore carries neither of the above
    # The host authorises whatever arrives against the roster, so a name it
    # does not recognise simply gets no audio.
    seat = (args.seat
            or os.environ.get("SEAT_CONTAINER")
            or os.environ.get("SEAT_IDENTITY")
            or socket.gethostname()
            or "").strip()
    if not seat:
        print("voice: no seat identity — not connecting", file=sys.stderr)
        return 0
    try:
        route = pathlib.Path("/proc/net/route").read_text(encoding="utf-8")
    except OSError:
        print("voice: no /proc/net/route — not connecting", file=sys.stderr)
        return 0
    gw = default_gateway(route)
    if not gw:
        # No default route: dial nothing rather than guess. A guessed gateway
        # would send this seat's handshake somewhere unintended.
        print("voice: no default gateway — not connecting", file=sys.stderr)
        return 0
    try:
        sock = open_stream(gw, seat, port=args.port)
    except OSError as exc:
        # THE FAIL-CLOSED SIDE, and it is the good one: the host's PERMIT rule
        # missing means no audio, loudly and immediately, rather than silent
        # loss of an isolation property.
        print(f"voice: no audio ({gw}:{args.port}: {exc})", file=sys.stderr)
        return 0
    # pacat owns the decode: raw s16le at the fleet's fixed rate into this
    # container's OWN sink. Nothing here reaches the host's audio server.
    pac = _sp.Popen(
        # 🔴 --latency-msec IS NOT OPTIONAL. Without it PulseAudio picks its own
        # playback buffer, and its default is enormous: MEASURED in a live seat
        # at "Buffer Latency: 960000 usec" with a total sink latency of 1.02
        # SECONDS. The operator's first real use was "quite laggy, I had to
        # speak slowly" — that second was mostly this line.
        #
        # 80ms matches what the host side already uses for `parec` and for the
        # VBAN receptor's own `pacat`, so the whole chain now has one buffer
        # depth rather than three plus a default nobody chose.
        #
        # ⚠️ Do not raise it to "fix" a dropout. Buffering hides a stall by
        # delaying it, and this stream is deliberately lossy (see
        # voice.send_or_drop) precisely so that a slow seat loses audio rather
        # than accumulating it. Late audio is transcribed as though it were
        # current, which is worse than missing audio.
        ["pacat", "--playback", "--format=s16le",
         f"--rate={VOICE_RATE}", f"--channels={VOICE_CHANNELS}",
         "--latency-msec=80"],
        stdin=_sp.PIPE,
    )
    # 🔴 CONNECTED IS NOT STREAMING, and saying so cost days of silence.
    #
    # This line used to read "streaming from …" and printed HERE, before a
    # single byte had arrived. The host's identity gate refuses AFTER the
    # accept, by closing the connection — so a refused seat printed a success
    # message, exited the loop below on the first empty recv, and said nothing
    # else. Measured 2026-08-11: every containerised seat on dev-vm-1 had been
    # refused for days while its own log claimed it was streaming, and only
    # the HOST journal disagreed.
    #
    # Same family as the hub's own push-success-is-not-receipt bugs: a sender
    # reporting its own send is not evidence of delivery.
    print(f"voice: connected to {gw}:{args.port} as {seat} — awaiting audio",
          file=sys.stderr)
    received = 0
    try:
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            if received == 0:
                # FIRST FRAME. Only now is "streaming" a true statement.
                print(f"voice: streaming from {gw}:{args.port} as {seat}",
                      file=sys.stderr)
            received += len(chunk)
            if pac.stdin is None:
                break
            pac.stdin.write(chunk)
    except (OSError, BrokenPipeError):
        pass
    finally:
        with contextlib.suppress(Exception):
            sock.close()
        if received == 0:
            # The measured cause, named — but as the likely one, not the
            # certain one: a host restart mid-handshake looks identical from
            # here, and only the host knows which it was.
            print(
                "voice: NO AUDIO — the host accepted the connection and closed "
                "it without sending a frame. Most likely its identity gate "
                f"refused this container: it authorises {seat!r} against this "
                "machine's squad roster, and a seat materialized by `edge "
                "apply` is NOT enrolled by that act. Confirm on the host with "
                "`journalctl --user -u voice-host.service | grep REFUSED`.",
                file=sys.stderr)
        else:
            print(f"voice: stream ended after {received} bytes", file=sys.stderr)
        with contextlib.suppress(Exception):
            pac.terminate()
    return 0


def _voice_container_map(timeout: float = 5.0) -> dict[str, tuple[str, str]]:
    """Ask docker who is at which address, RIGHT NOW.

    Asked per connection rather than cached, because the answer is the
    authentication: docker's IP REUSE (observed here — one address held by two
    containers minutes apart) makes any stored mapping a misroute waiting to
    happen. A live answer cannot be stale, since the connection is already
    established and the address is therefore current by definition.

    Every failure returns {} — an empty map is refused by `authorised`, so a
    docker that will not answer means no audio rather than unchecked audio.
    """
    from mcp_hub.voice_host import parse_container_map

    fmt = ("{{.Id}}|{{.Name}}|{{.Config.Image}}|"
           "{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}")
    try:
        ids = subprocess.run(
            ["docker", "ps", "-q"], capture_output=True, text=True,
            timeout=timeout, check=False, creationflags=_NO_WINDOW_FLAG,
        ).stdout.split()
        if not ids:
            return {}
        out = subprocess.run(
            ["docker", "inspect", "--format", fmt, *ids], capture_output=True,
            text=True, timeout=timeout, check=False, creationflags=_NO_WINDOW_FLAG,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    return parse_container_map(out)


def _voice_roster(conf: str = "") -> set[str]:
    """The enrolled container seats on this machine, read fresh.

    LOCAL by requirement — the hub's seat list is an HTTP call, so authorising
    against it would make the operator's microphone depend on the hub being
    reachable and let a slow hub stall the accept loop.

    Read PER CONNECTION, never cached: a cached roster outlives a retirement,
    and the container that keeps receiving audio after being retired is the one
    nobody looks for. Re-reading a small local file costs nothing.

    Unreadable or missing -> empty set -> refused by `decide`.
    """
    from mcp_hub.voice_host import DEFAULT_SQUAD_CONF, parse_squad_roster

    path = conf or os.environ.get("SQUAD_CONF") or DEFAULT_SQUAD_CONF
    try:
        text = pathlib.Path(path).expanduser().read_text(encoding="utf-8")
    except OSError:
        return set()
    return parse_squad_roster(text)


def voice_host_command(args: argparse.Namespace) -> int:
    """Host-side /voice: serve the operator's microphone to seats that dial in.

    The container connects to US and names itself (docs/seat-voice.md). This
    side verifies the caller, then streams ONE WAY and never reads the socket
    again — so a seat has no channel back and injection has no path.

    ⚠️ Binds the docker gateway, NEVER the wildcard. ufw on this box permits
    everything inbound on `tailscale0`, so `0.0.0.0` would publish the
    operator's live microphone to every tailnet peer. The PERMIT rule this
    feature needs is not what would expose it — the pre-existing blanket
    tailscale allow is, which is why the bind address is a security control and
    lives in `voice_host.listen_address` with a test on it.
    """
    import socket
    import subprocess as _sp
    import threading

    from mcp_hub.voice import (
        HANDSHAKE_TIMEOUT_SECONDS,
        VOICE_CHANNELS,
        VOICE_RATE,
        FrameSender,
        parse_handshake,
    )
    from mcp_hub.voice_host import (
        HANDSHAKE_MAX_BYTES,
        MAX_STREAMS,
        decide,
        listen_address,
    )

    addr = listen_address(args.gateway)
    # Newest connection per seat wins: a restarted container's old socket can
    # linger half-open, and two live streams for one seat means neither of us
    # can say which is being heard.
    live: dict[str, Any] = {}
    lock = threading.Lock()

    def _read_handshake(sock: Any) -> str:
        """One short line, capped. Anything else is dropped without explanation.

        This listens where any container can reach it, so a peer that opens a
        connection and never sends a newline must not be able to grow our
        memory or hold a slot.
        """
        buf = b""
        sock.settimeout(HANDSHAKE_TIMEOUT_SECONDS)
        while b"\n" not in buf and len(buf) < HANDSHAKE_MAX_BYTES:
            try:
                more = sock.recv(64)
            except OSError:
                return ""
            if not more:
                return ""
            buf += more
        return parse_handshake(buf.split(b"\n")[0])

    def _serve(sock: Any, peer: str) -> None:
        claimed = _read_handshake(sock)
        if not claimed:
            # Silent TO THE PEER, loud in our own log: see below.
            print(f"voice-host: REFUSED {peer}: unparsable handshake",
                  file=sys.stderr, flush=True)
            with contextlib.suppress(Exception):
                sock.close()
            return
        ok, seat, why = decide(
            peer, claimed, _voice_container_map(), _voice_roster(args.squad_conf),
        )
        if why:
            # ⚠️ Every refusal is logged HERE and nothing is ever said back to
            # the peer. Silence towards a stranger on the bridge is right;
            # silence towards ourselves is how a control ends up fail-closed
            # and undiagnosable, which has cost this design a night already.
            verdict = "REFUSED" if not ok else "note"
            print(f"voice-host: {verdict} {peer} ({claimed!r}): {why}",
                  file=sys.stderr, flush=True)
        if not ok:
            with contextlib.suppress(Exception):
                sock.close()
            return
        with lock:
            prior = live.pop(seat, None)
            live[seat] = sock
        if prior is not None:
            with contextlib.suppress(Exception):
                prior.close()

        # A SIGKILLed container leaves TCP retrying for ~15 minutes, so without
        # keepalive we would happily drop audio into a corpse and "the seat is
        # deaf" would be indistinguishable from "the seat is gone".
        with contextlib.suppress(OSError):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            for opt, val in (("TCP_KEEPIDLE", 30), ("TCP_KEEPINTVL", 10),
                             ("TCP_KEEPCNT", 3)):
                if hasattr(socket, opt):
                    sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, opt), val)
        sock.setblocking(False)

        # One capture per connection. Concurrent capture from the monitor is
        # measured to work (two simultaneous readers, identical output), and a
        # capture per seat means a stalled seat cannot affect any other.
        try:
            cap = _sp.Popen(
                ["parec", "-d", args.source, "--format=s16le",
                 f"--rate={VOICE_RATE}", f"--channels={VOICE_CHANNELS}",
                 "--latency-msec=80"],
                stdout=_sp.PIPE, stderr=_sp.DEVNULL,
            )
        except OSError as exc:
            print(f"voice-host: no capture for {seat} ({exc})", file=sys.stderr)
            with contextlib.suppress(Exception):
                sock.close()
            return
        print(f"voice-host: streaming to {seat} ({peer})", file=sys.stderr, flush=True)
        sender = FrameSender(sock)
        stalled_since = time.time()
        try:
            while True:
                if cap.stdout is None:
                    break
                chunk = cap.stdout.read(640)      # 20ms at 16kHz mono s16
                if not chunk:
                    break
                if sender.send(chunk):
                    stalled_since = time.time()
                elif time.time() - stalled_since > args.dead_after:
                    # Sustained zero progress: the peer is gone or wedged.
                    # Reap it rather than dropping into it indefinitely.
                    print(f"voice-host: {seat} not draining — dropping",
                          file=sys.stderr, flush=True)
                    break
        except OSError:
            pass
        finally:
            with contextlib.suppress(Exception):
                cap.terminate()
            with contextlib.suppress(Exception):
                sock.close()
            with lock:
                if live.get(seat) is sock:
                    live.pop(seat, None)
            print(f"voice-host: {seat} disconnected", file=sys.stderr, flush=True)

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    # docker0 may not exist yet (boot ordering, a docker restart). Retry rather
    # than exit: a service that dies once leaves audio off until someone
    # notices, which is the silent-absence failure this design keeps meeting.
    while True:
        try:
            srv.bind((addr, args.port))
            break
        except OSError as exc:
            print(f"voice-host: waiting to bind {addr}:{args.port} ({exc})",
                  file=sys.stderr, flush=True)
            time.sleep(5.0)
    srv.listen(16)
    print(f"voice-host: listening {addr}:{args.port}, source {args.source}",
          file=sys.stderr, flush=True)
    try:
        while True:
            try:
                sock, peer = srv.accept()
            except OSError:
                continue
            with lock:
                too_many = len(live) >= MAX_STREAMS
            if too_many:
                # Stop accepting rather than fan the microphone into an
                # unbounded number of sockets.
                with contextlib.suppress(Exception):
                    sock.close()
                continue
            threading.Thread(target=_serve, args=(sock, peer[0]),
                             daemon=True).start()
    except KeyboardInterrupt:
        pass
    finally:
        with contextlib.suppress(Exception):
            srv.close()
    return 0


def seat_entry_command(args: argparse.Namespace) -> int:
    """PID 1 of mcp-hub-seat — validate, prepare, launch (docs/seat-image.md).

    Two shapes, one implementation: `SEAT_MANIFEST` present means a POD of N
    agents sharing this container's HOME and lifecycle
    (docs/n-seats-per-container.md); absent means today's 1:1 contract, which
    is a pod of one and runs the identical code.

    Refusals are LOUD and coded: 42 credential (the factory's auth-death
    code — never misread as a build failure), 43 any other contract
    violation. Everything before launch is idempotent so a container
    restart re-runs it safely.
    """
    from mcp_hub.seat import (
        EXIT_AUTH,
        EXIT_CONTRACT,
        SeatContractError,
        agent_contract,
        parse_pod_manifest,
        parse_seat_contract,
        pod_workspace,
        validate_seat_credentials,
    )

    # ONE credential for the container, however many agents share it.
    # Measured 2026-08-06: three seats concurrently on one OAuth token, all
    # ⚡ — N-in-one is that same account concurrency, not a new question.
    verdict = validate_seat_credentials(os.environ)
    if not verdict.ok:
        print(f"seat-entry: REFUSED (auth): {verdict.error}", file=sys.stderr, flush=True)
        return EXIT_AUTH

    try:
        pod = parse_pod_manifest(os.environ)
        if pod is None:
            contract = parse_seat_contract(os.environ)
            # A single seat is a pod of one; `seat` stays its session name so
            # the attach affordance and the launch dance are unchanged.
            plan = [(contract, pathlib.Path(
                args.workdir or (pathlib.Path.home() / "work")
            ).expanduser(), "seat")]
        else:
            root = pathlib.Path(
                args.workdir or (pathlib.Path.home() / "work")
            ).expanduser()
            plan = [
                (agent_contract(pod, a), root / a.identity, a.identity)
                for a in pod.agents
            ]
    except SeatContractError as e:
        print(f"seat-entry: REFUSED (contract): {e}", file=sys.stderr, flush=True)
        return EXIT_CONTRACT

    _seat_home_setup()

    prepared: list[tuple[str, Any, pathlib.Path]] = []
    for contract, workdir, session in plan:
        contract, rc = _seat_prepare(contract, workdir)
        if rc is not None:
            return rc
        prepared.append((session, contract, workdir))

    # After every seed, never before — see _seat_onboarding.
    _seat_onboarding()

    lane = {"oauth": "subscription OAuth", "api-key": "API key", "both": (
        "BOTH set — Claude Code's own hierarchy decides (unmeasured; "
        "see docs/seat-image.md)"
    )}[verdict.lane]
    who = (f"pod of {len(prepared)}: "
           + ", ".join(c.identity for _s, c, _w in prepared)) if pod else \
        f"{prepared[0][1].identity} (project {prepared[0][1].project})"
    print(
        f"seat-entry: {who} mode={prepared[0][1].mode} credential={lane}",
        flush=True,
    )

    if pod is not None:
        # The file that makes ONE Dev Containers window a squad view. Written
        # here, in the container, listing CONTAINER paths — nothing on the
        # host can see these folders.
        ws_name = f"{pod.squad or 'pod'}.code-workspace"
        (pathlib.Path(args.workdir or (pathlib.Path.home() / "work"))
         .expanduser() / ws_name).write_text(
            json.dumps(
                pod_workspace(pod, {c.identity: str(w)
                                    for _s, c, w in prepared}),
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"seat-entry: wrote {ws_name}", flush=True)

    if args.prepare_only:
        print("seat-entry: --prepare-only — not launching claude", flush=True)
        return 0

    # HEADLESS: one turn, no tmux, exit code passed through. The solo errand.
    #
    # Everything above this line is shared with the interactive path, which is
    # the whole reason the mode is a flag and not a fork — the credential
    # check, the clone, the marker, .mcp.json and the brief are identical, and
    # only the launch differs.
    #
    # Deliberately BEFORE _seat_voice: a headless seat has nobody to talk to,
    # and starting an audio client for it would be a running process serving
    # no one. It is also the launch dance's only true exception — there is no
    # TUI to show a dialog, so there is nothing to answer.
    #
    # Pods DID gain a headless mode (2026-08-08), exactly as the previous
    # comment here warned — so the first-agent-only dispatch it flagged has
    # become the real bug it predicted, and is fixed rather than re-annotated:
    # every agent runs, and the pod's own runner aggregates them.
    if prepared[0][1].mode == "headless":
        # The result artifact only survives reclaim if ~/.claude is a MOUNT
        # (the memory volume). Without one the result provably dies with the
        # container — `docker logs` die with `docker rm` and exec-harvest
        # refuses on an exited container, both measured 2026-08-08 — so the
        # placement is a contract violation, refused at the door like
        # headless-without-a-prompt. Loud beats a fix that only looks like
        # one: with no volume this seat would run, succeed, and leave nothing.
        state_dir = pathlib.Path.home() / ".claude"
        if not os.path.ismount(str(state_dir)):
            print(
                f"seat-entry: SEAT_MODE=headless but {state_dir} is not a "
                f"mount — this seat was placed without a memory volume, so "
                f"its result would die with the container. Place it with "
                f"--memory-volume <name> (mounted at {state_dir}).",
                file=sys.stderr, flush=True,
            )
            return EXIT_CONTRACT
        if len(prepared) > 1:
            return _seat_headless_pod(prepared)
        return _seat_headless(prepared[0][1], prepared[0][2])[0]

    # /voice, once per CONTAINER, before any agent starts. Detached and
    # deliberately unchecked: audio is a convenience and MUST NOT be able to
    # fail a seat. A seat with no microphone is a working seat; a seat that
    # refuses to start because the audio host is down is not.
    _seat_voice()

    for session, contract, workdir in prepared:
        rc = _seat_launch(contract, workdir, session)
        if rc is not None:
            return rc

    return _seat_supervise([(s, c) for s, c, _w in prepared])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcp-hub",
        description="MCP Hub — server + utility CLI",
    )
    sub = parser.add_subparsers(dest="subcommand")

    stop_hook = sub.add_parser(
        "stop-hook",
        help="Auto-check hub messages at Stop boundaries (for settings.json hooks)",
        description=(
            "Queries the hub for queued DMs to the active agent and emits "
            "Claude Code Stop hook JSON if any are pending. Designed to be "
            "wired into a global ~/.claude/settings.json Stop hook with no "
            "args — the cli auto-discovers agent identity from the project's "
            ".claude/hub-agent.json marker. Use explicit --name to override "
            "auto-discovery (e.g. for testing). Fail-open — never blocks Stop "
            "on hub errors or missing markers."
        ),
    )
    stop_hook.add_argument(
        "--name",
        default=None,
        help=(
            "Agent name on the hub. If omitted, auto-discovers from "
            "<cwd>/.claude/hub-agent.json via the cwd Claude Code passes to "
            "hooks on stdin."
        ),
    )
    stop_hook.add_argument(
        "--project",
        default=None,
        help="Project name (used in re-register hint when drifted)",
    )
    stop_hook.add_argument(
        "--hub-url",
        default=DEFAULT_HUB_URL,
        help=f"Hub MCP endpoint (default: {DEFAULT_HUB_URL}, or $MCP_HUB_URL)",
    )

    session_start = sub.add_parser(
        "session-start",
        help="Inject auto-register instruction into the agent's first turn "
             "(for SessionStart hooks)",
        description=(
            "Reads <cwd>/.claude/hub-agent.json and emits SessionStart hook "
            "JSON with `additionalContext` telling the agent to call "
            "register() at session start. Claude sees the context before its "
            "first turn and binds the hub session automatically. Silent "
            "no-op if no marker found."
        ),
    )
    session_start.add_argument(
        "--name",
        default=None,
        help="Agent name override (otherwise auto-discovered from marker).",
    )
    session_start.add_argument(
        "--project",
        default=None,
        help="Project name override (otherwise auto-discovered from marker).",
    )

    session_rewake = sub.add_parser(
        "session-rewake",
        help="Try to trigger an unprompted first turn via asyncRewake (for SessionStart hooks)",
        description=(
            "Companion to session-start. Writes the register instruction to "
            "stderr and exits with code 2 to trigger Claude Code's "
            "asyncRewake behaviour. If asyncRewake fires from a cold "
            "session start, Claude takes an unprompted first turn and "
            "calls register before the operator types anything. If it "
            "doesn't fire, this is a no-op; session-start's additionalContext "
            "still drives the register on first prompt."
        ),
    )
    session_rewake.add_argument(
        "--name",
        default=None,
        help="Agent name override (otherwise auto-discovered from marker).",
    )
    session_rewake.add_argument(
        "--project",
        default=None,
        help="Project name override (otherwise auto-discovered from marker).",
    )

    onboard = sub.add_parser(
        "onboard",
        help="Opt this repo into hub participation (derived identity; cross-platform)",
        description=(
            "Adds the cwd repo's <org>/<repo> (from `git remote get-url "
            "origin`) to ~/.mcp-hub/config.json's projects list and prints "
            "the derived identity (<repo>-<hostname>). Idempotent. The only "
            "per-repo step a machine needs — hooks and the Stop-hook "
            "self-heal take it from there."
        ),
    )
    onboard.add_argument(
        "--path",
        default=None,
        help="Repo path to onboard (default: current directory).",
    )

    mem_export = sub.add_parser(
        "memory-export",
        help="Push this clone's Claude memory files to the hub for its twins",
        description=(
            "Reads ~/.claude/projects/<this-clone>/memory, stages every .md "
            "file on the hub keyed by the derived project, and DMs each "
            "online twin (same project, other machines) to run "
            "memory-import. Filenames preserved verbatim."
        ),
    )
    mem_export.add_argument("--path", default=None, help="Repo path (default: cwd).")
    mem_export.add_argument(
        "--hub-url",
        default=DEFAULT_HUB_URL,
        help=f"Hub MCP endpoint (default: {DEFAULT_HUB_URL}, or $MCP_HUB_URL)",
    )

    mem_import = sub.add_parser(
        "memory-import",
        help="Pull twin-exported Claude memory files into this clone",
        description=(
            "Fetches the memory files staged for this repo's derived project "
            "and writes them into ~/.claude/projects/<this-clone>/memory as "
            "real local files (picked up by Claude next session). Existing "
            "local files are kept unless --force; MEMORY.md is merged, never "
            "clobbered."
        ),
    )
    mem_import.add_argument("--path", default=None, help="Repo path (default: cwd).")
    mem_import.add_argument(
        "--force", action="store_true",
        help="Overwrite local memory files that already exist.",
    )
    mem_import.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be imported without writing anything.",
    )
    mem_import.add_argument(
        "--replace-index", action="store_true",
        help=(
            "Adopt the staged MEMORY.md verbatim instead of merging — the "
            "reconciliation return-leg (accept the curated canonical index)."
        ),
    )
    mem_import.add_argument(
        "--hub-url",
        default=DEFAULT_HUB_URL,
        help=f"Hub MCP endpoint (default: {DEFAULT_HUB_URL}, or $MCP_HUB_URL)",
    )

    mem_verify = sub.add_parser(
        "memory-verify",
        help="Prove local memory matches the hub's staged set (hash compare)",
        description=(
            "Compares every staged file's hash against the local memory dir. "
            "Exit 0 only when all staged files exist locally with identical "
            "content — the convergence proof after a sync ceremony. Local "
            "files not in the staged set are reported as extras."
        ),
    )
    mem_verify.add_argument("--path", default=None, help="Repo path (default: cwd).")
    mem_verify.add_argument(
        "--hub-url",
        default=DEFAULT_HUB_URL,
        help=f"Hub MCP endpoint (default: {DEFAULT_HUB_URL}, or $MCP_HUB_URL)",
    )

    heartbeat = sub.add_parser(
        "heartbeat-daemon",
        help="Long-running per-minute heartbeat to the hub (for SessionStart hooks)",
        description=(
            "Long-lived daemon that pings the hub's heartbeat tool every "
            f"{HEARTBEAT_INTERVAL_SECONDS}s, proving the agent's Claude Code "
            "session is still alive. Designed to be spawned by an async "
            "SessionStart hook in ~/.claude/settings.json. Reads agent "
            "identity from <cwd>/.claude/hub-agent.json (same marker as "
            "stop-hook). Silent no-op if no marker found. Reconnects on "
            "transient hub errors."
        ),
    )
    heartbeat.add_argument(
        "--name",
        default=None,
        help=(
            "Agent name on the hub. If omitted, auto-discovers from "
            "<cwd>/.claude/hub-agent.json via the cwd Claude Code passes "
            "to hooks on stdin."
        ),
    )
    heartbeat.add_argument(
        "--project",
        default=None,
        help="Project name (currently informational; reserved for future use)",
    )
    heartbeat.add_argument(
        "--hub-url",
        default=DEFAULT_HUB_URL,
        help=f"Hub MCP endpoint (default: {DEFAULT_HUB_URL}, or $MCP_HUB_URL)",
    )

    ident = sub.add_parser(
        "identity",
        help="Print the derived agent name for a worktree (single source of truth)",
        description=(
            "Prints the derived agent name for a worktree. Anything that needs "
            "an agent's name should ask here rather than re-deriving it — "
            "squad and this module disagreeing is what makes a clone's "
            "statusline read `hub ?`."
        ),
    )
    ident.add_argument("--cwd", default=None, help="Worktree (default: current directory)")
    ident.add_argument("--json", action="store_true", help="Emit {name, project, cwd}")
    ident.add_argument(
        "--any",
        action="store_true",
        help="Derive even when the project isn't opted in yet (transport needs this)",
    )

    settings = sub.add_parser(
        "settings",
        help="Show every setting governing one agent, and where each came from",
        description=(
            "Read-only view of one agent's settings with the SOURCE of every "
            "value. The sources differ in scope — a squad usually comes from a "
            "workspace, comms is per agent, the hub URL is per machine — so a "
            "value on its own cannot answer whether changing it affects this "
            "agent or every agent on the box. Assembled here rather than in "
            "the cockpit extension so a web UI can render the same model."
        ),
    )
    settings.add_argument("--cwd", default=None, help="Worktree (default: current directory)")
    settings.add_argument("--json", action="store_true", help="Emit the panel model as JSON")
    settings.add_argument(
        "--tui", action="store_true",
        help="Interactive panel over every agent in the workspace (arrows, enter, mouse)",
    )
    settings.add_argument(
        "--workspace", default=None,
        help=(
            "A .code-workspace file; --tui scopes to the agents whose worktree "
            "is one of its folders — the same rule `squad`'s ws_agents and the "
            "cockpit's tab list use. Omitted: every agent on this machine."
        ),
    )

    board = sub.add_parser(
        "board",
        help="SQUAD BOARD — the live fleet and each agent's settings, one screen",
        description=(
            "The operator's view: who needs you, who is working, who is idle, "
            "each agent's blocking question with answer buttons, git and token "
            "usage — with the settings sheet underneath. This is `settings "
            "--tui` under the name the screen actually wears; both spellings "
            "stay because the cockpit predates the rename."
        ),
    )
    board.add_argument(
        "--workspace", default=None,
        help="A .code-workspace file to scope the roster to (see `settings --workspace`)",
    )

    mute = sub.add_parser(
        "mute",
        help="Silence (or unsilence) one squad's broadcasts for one agent",
        description=(
            "Mutes one squad for one agent without leaving it: membership and "
            "attention are different things, and leaving a squad to get quiet "
            "also removes the ability to address it. Joining and leaving are "
            "deliberately NOT here — membership derives from declaring a "
            "workspace as a squad, and a second way to set it would disagree "
            "with the workspace eventually."
        ),
    )
    mute.add_argument("--agent", required=True, help="Agent whose attention this is")
    mute.add_argument("--squad", required=True, help="Squad to silence or unsilence")
    mute.add_argument("--state", required=True, choices=["hearing", "muted"])
    mute.add_argument("--hub-url", default=DEFAULT_HUB_URL)

    xport_hist = sub.add_parser(
        "transport-history",
        help="Copy + re-key an agent's conversation history to a new path",
        description=(
            "Copies every transcript from one project path's Claude state dir "
            "to another's, rewriting the four structural path fields (cwd, "
            "file-history-delta.trackingPath, .backup.realParentDir, and "
            "file-history-snapshot.snapshot.trackedFileBackups) so the "
            "transported agent resumes at the new path. Message content is "
            "left byte-exact. Refuses to write any transcript where a "
            "structural field still points at the source."
        ),
    )
    xport_hist.add_argument("--from-cwd", required=True, help="Source worktree (absolute)")
    xport_hist.add_argument("--to-cwd", required=True, help="Destination worktree (absolute)")
    xport_hist.add_argument(
        "--dry-run", action="store_true", help="Report what would transfer; write nothing"
    )
    xport_hist.add_argument(
        "--out-dir",
        default=None,
        help=(
            "Write re-keyed transcripts here instead of this machine's Claude "
            "state dir. Used for cross-machine transport: re-key locally into a "
            "staging dir, then ship it to the destination box."
        ),
    )

    rebind = sub.add_parser(
        "rebind-url",
        help="Stamp ?agent=<derived name> into this repo's .mcp.json hub URL",
        description=(
            "Rewrites <cwd>/.mcp.json so the hub URL carries the seat's "
            "derived identity as a query parameter. The hub reads it at "
            "transport connect and auto-rebinds the session after every "
            "deploy/restart — no register turn, no nag, no interruption. "
            "The cli's own ephemeral clients strip the parameter before "
            "connecting, so stop-hook/daemon traffic can never claim the "
            "wake binding through it."
        ),
    )
    rebind.add_argument("--cwd", default=None, help="Worktree (default: current directory)")
    rebind.add_argument(
        "--dry-run", action="store_true", help="Print the rewritten URL; write nothing"
    )

    focus_p = sub.add_parser(
        "focus",
        help="Do not disturb — suppress an agent's wakes for a bounded time",
        description=(
            "The hub knows 'in a turn' and 'idle', and treats idle as safe to "
            "interrupt — so an agent babysitting a deploy looks exactly like "
            "one doing nothing. Focus is the third state. Nothing is dropped: "
            "messages queue and surface at the next turn boundary. `urgent` "
            "still gets through, and focus EXPIRES on its own, because a "
            "silencer you can leave on forever is a silent-drop bug."
        ),
    )
    send_p = sub.add_parser(
        "send",
        help="Send one DM to another agent (for scripts and sweeps)",
        description=(
            "The shell's door to a hub DM. Exists because squad is bash and "
            "needs to write rows through doors that are DMs — the compaction "
            "door (bar 53) writes one per step. Body may be '-' to read "
            "stdin, so a multi-line row need not survive shell quoting."
        ),
    )
    send_p.add_argument("--to", required=True, help="Recipient agent name")
    send_p.add_argument(
        "--message", required=True,
        help="Message body, or '-' to read it from stdin",
    )
    send_p.add_argument(
        "--from", dest="from_agent", default=None,
        help="Sender name (default: derived from --cwd)",
    )
    send_p.add_argument("--cwd", default=None, help="Worktree for identity derivation")
    send_p.add_argument(
        "--priority", default="normal", choices=["low", "normal", "urgent"],
    )
    send_p.add_argument(
        "--in-reply-to", dest="in_reply_to", default="",
        help="Ref of the message this answers (declared lineage + reply-wake)",
    )
    send_p.add_argument(
        "--hub-url", default=DEFAULT_HUB_URL,
        help="Hub base URL (default: $MCP_HUB_URL or built-in)",
    )

    focus_p.add_argument(
        "minutes", nargs="?", type=int, default=60,
        help="How long to stay focused (default 60, capped at 480)",
    )
    focus_p.add_argument("--off", action="store_true", help="End focus now")
    focus_p.add_argument("--reason", default="", help="Shown to anyone reaching you")
    focus_p.add_argument(
        "--agent", default=None,
        help="Agent name (default: derived from --cwd)",
    )
    focus_p.add_argument("--cwd", default=None, help="Worktree for identity derivation")
    focus_p.add_argument(
        "--hub-url", default=DEFAULT_HUB_URL,
        help="Hub base URL (default: $MCP_HUB_URL or built-in)",
    )

    machines = sub.add_parser(
        "machines",
        help="Machine enrolment on the hub — required before presence or edge",
        description=(
            "A machine must have a row on the hub before it can report "
            "anything: both the board's presence ping and `edge apply` 404 "
            "without one. `enrol` returns a machine token EXACTLY ONCE — the "
            "hub keeps only a hash — so this "
            "writes it to disk before printing anything."
        ),
    )
    machines.add_argument(
        "action", choices=["list", "enrol", "rotate", "rm"],
        help="list: enrolled machines · enrol: add this machine · "
             "rotate: issue a new token, invalidating the old one · "
             "rm: retire a machine (its token stops working)",
    )
    machines.add_argument(
        "name", nargs="?", default=None,
        help="Machine name (default: sanitized hostname)",
    )
    machines.add_argument("--os", default="linux", help="OS label for the record")
    machines.add_argument(
        "--token-file", default=None,
        help="Where to write the machine token (default: ~/.mcp-hub/machine.token)",
    )
    machines.add_argument(
        "--force", action="store_true",
        help="Overwrite an existing machine token file",
    )
    machines.add_argument(
        "--print-token", action="store_true",
        help="Also print the token (it cannot be retrieved later)",
    )
    machines.add_argument(
        "--hub-url", default=DEFAULT_HUB_URL,
        help="Hub base URL (default: $MCP_HUB_URL or built-in)",
    )
    machines.add_argument("--json", action="store_true", help="Machine-readable output")

    workspaces = sub.add_parser(
        "workspaces",
        help="The workspace registry: what this machine has, what the hub knows",
        description=(
            "`list` is the board's `w` view as text — every .code-workspace "
            "found here, merged with the hub's registry, three truth columns. "
            "`register` is the missing half: until a workspace is POSTed to "
            "/api/v1/workspaces it reads as an unregistered file, so a fresh "
            "hub makes every workspace you own look like drift. Registering "
            "is what makes a MISSING one mean something. `remove` drops a "
            "DEFINITION and touches no disk — the file is `squad teardown "
            "workspace`'s job, and doing only one of the two is exactly how "
            "you get a ghost row or a feral file."
        ),
    )
    workspaces.add_argument(
        "action", choices=["list", "register", "remove"],
        help="list: the merged view · register: define on the hub · "
             "remove: drop the definition (no file is deleted)",
    )
    workspaces.add_argument(
        "paths", nargs="*",
        help="register: workspace files · remove: workspace NAMES",
    )
    workspaces.add_argument(
        "--all", action="store_true",
        help="register: every .code-workspace discovered on this machine",
    )
    workspaces.add_argument(
        "--squad", default="",
        help="register: type these workspaces with a squad (must exist on the hub)",
    )
    workspaces.add_argument(
        "--machine", default=None,
        help="Machine name (default: sanitized hostname)",
    )
    workspaces.add_argument(
        "--hub-url", default=DEFAULT_HUB_URL,
        help="Hub base URL (default: $MCP_HUB_URL or built-in)",
    )
    workspaces.add_argument(
        "--scan-dir", action="append", default=None,
        help="Workspace scan dir (repeatable; default: ~/Projects and ~)",
    )
    workspaces.add_argument(
        "--dry-run", action="store_true",
        help="register/remove: print what would change; write nothing",
    )
    workspaces.add_argument(
        "--yes", action="store_true",
        help="remove: skip the confirmation (there is no undo — the hub has "
             "no archive for workspace definitions)",
    )
    workspaces.add_argument("--json", action="store_true", help="Machine-readable output")

    seats = sub.add_parser(
        "seats",
        help="Seats — WHAT may run, independent of which machine runs it",
        description=(
            "A seat is a unit of work with an identity; a placement says "
            "where it runs. Splitting them is what lets a seat move between "
            "machines without changing what it IS. Identity is assigned by "
            "the hub, never derived at the far end — a container's hostname "
            "must not be able to name a seat."
        ),
    )
    seats.add_argument(
        "action",
        choices=["list", "add", "rm", "restore", "logs", "update", "clone"],
        help=("list · add: declare a seat · rm: archive it (placements "
              "first; --purge deletes the row outright) · restore: the "
              "inverse of rm — an archived seat returns exactly as it was · "
              "logs: what it has printed (container gone → reads "
              "the headless result artifact from the memory volume) · "
              "update: edit it in place · clone: a second seat from it"),
    )
    seats.add_argument("identity", nargs="?", default=None,
                       help="rm/logs/update/clone: which seat")
    seats.add_argument(
        "--as", dest="clone_suffix", default="",
        help=("clone: the new seat is <identity>-<suffix>, with its pod "
              "inhabitants and memory volume re-identified to match"),
    )
    seats.add_argument("--repo", default="", help="add: <org>/<repo>")
    seats.add_argument("--machine", default=None,
                       help="add: which machine (default: this one)")
    seats.add_argument("--folder", default="", help="add: absolute path there")
    seats.add_argument("--identity", dest="want_identity", default="",
                       help="add: override the assigned identity")
    seats.add_argument("--launch-args", default="", help="add: claude launch args")
    seats.add_argument(
        "--image", default="",
        help="add: container image — makes this a DOCKER unit (a web app, an "
             "inference server, a squad seat); --folder is then not required",
    )
    seats.add_argument("--env", action="append", default=None, metavar="K=V",
                       help="add: container env (repeatable)")
    seats.add_argument("--port", action="append", default=None, metavar="H:C",
                       help="add: published port (repeatable)")
    seats.add_argument("--volume", action="append", default=None, metavar="S:D",
                       help="add: bind mount or volume (repeatable)")
    seats.add_argument("--network", default="", help="add: docker network")
    seats.add_argument(
        "--env-from-host", action="append", default=None, metavar="NAME",
        help="add: pass this variable through from the EDGE MACHINE's own "
             "environment. The hub stores the NAME only, never the value — so "
             "an API key never enters the control plane (repeatable)",
    )
    seats.add_argument(
        "--agent", action="append", default=None,
        metavar="IDENTITY[=REPO[,SQUADS]]",
        help="add: an agent INSIDE this container, making it a POD of several "
             "(docs/n-seats-per-container.md). Repeatable. Omit entirely for "
             "the ordinary one-container-one-agent shape",
    )
    seats.add_argument(
        "--pod-squad", default="",
        help="add: the squad a --agent pod belongs to; names the "
             ".code-workspace it writes inside itself",
    )
    seats.add_argument(
        "--memory-volume", default="",
        help="add: the volume holding this seat's Claude memory. Its PRESENCE "
             "is what makes reclaim harvest before destroying — a unit without "
             "one is a service, and has nothing to preserve",
    )
    seats.add_argument("--command", default="", help="add: override the image CMD")
    seats.add_argument(
        "--mode", default="", choices=["interactive", "headless"],
        help="add: headless = one `claude -p` turn, then exit — place it "
             "with `placements set ... ran`. Needs --prompt or --brief, and "
             "--memory-volume (the result artifact lives there and survives "
             "reclaim). Default: interactive",
    )
    seats.add_argument(
        "--prompt", default="",
        help="add (headless): the one turn's instruction. --brief stands in "
             "when this is empty (the seat is told to read BRIEF.md)",
    )
    seats.add_argument(
        "--timeout", type=int, default=None, metavar="SECONDS",
        help="add (headless): kill the turn after this long, recording exit "
             "124 with partial output kept (default 1800; 0 = unbounded — "
             "the one value where the flag disarms itself: nothing will "
             "ever reap a hung turn)",
    )
    seats.add_argument("--class", dest="klass", default="squad",
                       choices=["squad", "faculty"],
                       help="add: faculty seats are never auto-started by `up`")
    seats.add_argument(
        "--hub-url", default=DEFAULT_HUB_URL,
        help="Hub base URL (default: $MCP_HUB_URL or built-in)",
    )
    seats.add_argument("--json", action="store_true", help="Machine-readable output")
    seats.add_argument(
        "--brief", default="",
        help=("add: what this seat is FOR. `@path` reads a file. Lands as "
              "BRIEF.md in the workdir and the seat's first turn is told to "
              "read it — a brief nothing points at is never opened"),
    )
    seats.add_argument(
        "--input", action="append", default=None, metavar="PATH",
        help=("add: a UTF-8 file the seat should work from (repeatable). "
              "Lands in ./inputs/. Travels through the hub as text, so it is "
              "NOT for secrets and not for binaries — mount a volume for "
              "those"),
    )
    seats.add_argument(
        "--tail", default="200",
        help="logs: how many lines (default 200; `all` for everything)",
    )
    seats.add_argument("--follow", action="store_true",
                       help="logs: stream until interrupted")
    seats.add_argument("--dry-run", action="store_true",
                       help="add/rm/update/clone: print what would change, "
                            "write nothing. An action that does not implement "
                            "it REFUSES rather than acting")
    seats.add_argument(
        "--purge", action="store_true",
        help=("rm: DELETE the row instead of archiving. The death-fact "
              "survives in the seat's event trail, but the declaration is "
              "gone — needs --yes, and is refused while any placement row "
              "still references the seat"),
    )
    seats.add_argument(
        "--yes", action="store_true",
        help="rm --purge: confirm the deletion (nothing dies unnamed)",
    )

    placements = sub.add_parser(
        "placements",
        help="Placements — WHERE a seat runs, and whether it should be running",
        description=(
            "Desired state, written from ANY node. Nothing happens at the "
            "moment you write one: the named machine's `edge apply` pulls it, "
            "acts, and reports what it OBSERVED. So `status` here is the "
            "honest word — converged, diverged, or pending-edge, which means "
            "no edge has run since you asked. If placements sit pending "
            "forever, no edge is running on that box; that is the fact to "
            "check first, not the hub."
        ),
    )
    placements.add_argument(
        "action", choices=["list", "set", "reclaim", "unplace", "move"],
        help="list · set: running|stopped|ran · reclaim: harvest then DESTROY "
             "· unplace: drop the row, leave the substrate alone · move: "
             "reclaim on one machine, wait, then create on another",
    )
    placements.add_argument("target", nargs="?", default=None,
                            help="set/reclaim/unplace: placement id")
    placements.add_argument("desired", nargs="?", default=None,
                            help="set: running|stopped|ran (ran = headless: "
                                 "run once, never restart). Reachable only "
                                 "with a placement id — use --desired when "
                                 "creating with --seat")
    placements.add_argument(
        "--desired", dest="desired_flag", default=None,
        choices=["running", "stopped", "ran"],
        help="set: desired state as a FLAG. argparse cannot bind a trailing "
             "positional that follows an option, so `set --seat X ran` either "
             "errors or silently binds `ran` to the placement id — which made "
             "`ran` unreachable at creation time (measured 2026-08-09)",
    )
    placements.add_argument("--seat", default="", help="set: create for this seat")
    placements.add_argument("--machine", default=None, help="set: on this machine")
    placements.add_argument("--substrate", default="worktree",
                            choices=["worktree", "docker"],
                            help="set: worktree (tmux seat) or docker (container)")
    placements.add_argument(
        "--yes", action="store_true",
        help="reclaim: skip the confirmation — it DESTROYS the substrate. "
             "unplace: confirm abandoning a seat last observed RUNNING",
    )
    placements.add_argument("--dry-run", action="store_true",
                            help="print what would change; write nothing")
    placements.add_argument(
        "--to", default="",
        help="move: the machine to move the seat TO",
    )
    placements.add_argument(
        "--no-harvest", action="store_true",
        help="move: proceed even though there is nothing to harvest — "
             "accepts losing a docker seat's memory",
    )
    placements.add_argument(
        "--timeout", type=int, default=MOVE_TIMEOUT_SECONDS,
        help=f"move: seconds to wait for the source edge to finish the "
             f"reclaim (default {MOVE_TIMEOUT_SECONDS}). Exits resumably, "
             f"naming the manual two-phase path",
    )
    placements.add_argument(
        "--hub-url", default=DEFAULT_HUB_URL,
        help="Hub base URL (default: $MCP_HUB_URL or built-in)",
    )
    placements.add_argument("--json", action="store_true",
                            help="Machine-readable output")

    edge = sub.add_parser(
        "edge",
        help="Edge realizer: reconcile this machine toward the hub's desired state",
        description=(
            "One reconcile pass against /api/v1: pull this machine's "
            "placements (machine token), realize worktree-substrate ones via "
            "the squad verbs, then report observed state BY ENUMERATION plus "
            "every .code-workspace file discovered — the workspace registry's "
            "never-lose-track leg. Docker-substrate placements are realized "
            "via the DockerExecutor (observed by `docker ps -a` enumeration; "
            "an unreachable docker daemon refuses the pass rather than "
            "guessing). Run it from the edge timer for continuous "
            "reconciliation."
        ),
    )
    edge.add_argument(
        "action", choices=["apply", "watch"],
        help="apply: one reconcile pass · watch: hold the hub's doorbell "
             "open and run a pass the moment desired state changes "
             "(the timer stays underneath as the floor)")
    edge.add_argument(
        "--hub-url",
        default=DEFAULT_HUB_URL,
        help="Hub base URL (default: $MCP_HUB_URL or built-in)",
    )
    edge.add_argument(
        "--machine",
        default=None,
        help="Machine name as enrolled on the hub (default: sanitized hostname)",
    )
    edge.add_argument(
        "--token",
        default=None,
        help="Machine token (default: $MCP_HUB_MACHINE_TOKEN)",
    )
    edge.add_argument(
        "--scan-dir",
        action="append",
        default=None,
        help="Workspace scan dir (repeatable; default: ~/Projects and ~)",
    )
    edge.add_argument(
        "--dry-run",
        action="store_true",
        help="Pull, plan and print actions; execute nothing, report nothing",
    )

    hib = sub.add_parser(
        "hibernate",
        help="Park lanes that have nothing open, and release them when a bar lands",
        description=(
            "One scanner pass for bar 59: read the console's candidate list "
            "(GET /threads/{id}/hibernation-candidates — read-only; the "
            "console never holds anyone), place a `kind=hibernation` hold "
            "owned by `hibernation-scanner` on each candidate, RE-hold the "
            "ones still listed as a fresh entry after a fresh query (never "
            "an expiry bump), and release the ones that have dropped off — "
            "which is what 'release on bar assignment' looks like from "
            "here. An unreadable candidate list does NOTHING, not even "
            "releases: with no list every held lane looks like a "
            "non-candidate, and a blip would unpark the fleet."
        ),
    )
    hib.add_argument(
        "--thread", default="1",
        help="Console thread whose candidates to read (default: 1)")
    hib.add_argument(
        "--console-url", default=os.environ.get(
            "MCP_HUB_CONSOLE_URL", "http://127.0.0.1:8765"),
        help="Console base URL (default: $MCP_HUB_CONSOLE_URL or localhost:8765)")
    hib.add_argument(
        "--hub-url", default=DEFAULT_HUB_URL,
        help="Hub base URL (default: $MCP_HUB_URL or built-in)")
    hib.add_argument(
        "--token", default=None,
        help="Hub API token (default: $MCP_HUB_API_TOKEN, else ~/.mcp-hub/api.token)")
    hib.add_argument(
        "--arm", action="store_true",
        help=("Actually place and release holds. WITHOUT THIS THE PASS ONLY "
              "REPORTS — arming a scanner that parks live lanes is a "
              "deliberate act, not the default a typo reaches"))
    hib.add_argument(
        "--dry-run", action="store_true",
        help="Say what the pass would hold and release; write nothing")

    capsules = sub.add_parser(
        "capsules",
        help="A whole SQUAD on docker: freeze it, then place it on a machine",
        description=(
            "A capsule is a squad FROZEN — every member's seat spec as it is "
            "at compose time, so placing the same capsule twice puts up the "
            "same squad rather than whatever the roster says at the second "
            "moment. `place` writes one docker placement PER SEAT; nothing "
            "runs until that machine's edge pass realizes them."
        ),
    )
    capsules.add_argument(
        "action", choices=["list", "compose", "place", "attach", "rm"],
        help=("list · compose: freeze a squad · place: one placement per seat "
              "· attach: give this machine's seats a tab and a workspace "
              "· rm: forget a snapshot (what it already placed keeps running)"),
    )
    capsules.add_argument("target", nargs="?",
                          help="place/attach: which capsule")
    capsules.add_argument(
        "--workspace", default=None,
        help=("attach: the .code-workspace to list the seats in — created if "
              "absent, so the squad opens like any other"),
    )
    capsules.add_argument(
        "--dry-run", action="store_true",
        help="rm/attach: print what would change; write nothing. compose and "
             "place REFUSE it — neither can be previewed without asking the "
             "hub, and a local guess would be a second implementation",
    )
    capsules.add_argument("--squad", default=None, help="compose: which squad")
    capsules.add_argument(
        "--machine", default=None,
        help="place: which machine (no default — placing a squad is not a guess)",
    )
    capsules.add_argument(
        "--hub-url", default=DEFAULT_HUB_URL,
        help="Hub base URL (default: $MCP_HUB_URL or built-in)",
    )
    capsules.add_argument(
        "--register", action="store_true",
        help=("compose: register the squad for management first — a squad "
              "can exist for messaging and be unknown to the runtime"),
    )
    capsules.add_argument("--json", action="store_true",
                          help="Machine-readable output")
    capsules.add_argument(
        "--as", dest="as_label", default="",
        help=("place: place a SECOND copy under fresh identities "
              "(<seat>-<label>). Without it, placing an already-placed "
              "capsule is REFUSED — one identity, two containers"),
    )

    squads = sub.add_parser(
        "squads",
        help="Squads — WHO a team is: create, fork, merge, lend, retire",
        description=(
            "A squad is the team; a capsule is that team frozen; a placement "
            "is where a member runs. This verb owns the first of the three, "
            "and it is the one that had no CLI at all — membership was "
            "reachable only from inside an agent (the MCP set_squads) or by "
            "curl with the operator token, so the operator could not answer "
            "'who is in dreamteam' without asking an agent to answer it for "
            "them.\n\n"
            "Membership drives DELIVERY, never confidentiality: it decides "
            "who is woken and whose catch-up a broadcast lands in. Anyone can "
            "still read any squad's broadcasts by asking."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    squads.add_argument(
        "action",
        choices=["list", "create", "rm", "rename", "members",
                 "add", "remove", "fork", "merge"],
        help=("list · create · rm: archive · rename · members · add/remove: "
              "one seat · fork: subset into a NEW squad · merge: fold into "
              "another"),
    )
    squads.add_argument("name", nargs="?", help="which squad")
    # ⚠️ POSITIONAL SEATS MUST COME BEFORE ANY FLAG. argparse cannot bind a
    # trailing `nargs="*"` positional that appears AFTER an optional, in any
    # arrangement (measured: both `[name][members...]` and a single combined
    # list fail identically). So `squads fork dt --to spike alice bob` — the
    # order that reads most naturally, and the one this very file documented
    # first — dies with "unrecognized arguments". `--members` exists so that
    # phrasing has a working form rather than a footgun; found by smoke-testing
    # the verb against a live hub, which 1543 green unit tests did not.
    squads.add_argument("members", nargs="*",
                        help=("add/remove/fork: seat identities. Must come "
                              "BEFORE any flag — or use --members"))
    squads.add_argument(
        "--members", dest="members_flag", default="",
        help=("add/remove/fork: seat identities, comma-separated. The form "
              "that works after other flags"),
    )
    squads.add_argument("--to", dest="to", default="",
                        help="rename/fork: the new squad's name")
    squads.add_argument("--into", dest="into", default="",
                        help="merge: the squad to fold INTO (it survives)")
    squads.add_argument("--description", default="", help="create: what it is for")
    squads.add_argument(
        "--until", default="",
        help=("add/fork: make it a LOAN that ends by itself — `+7d`, `+12h`, "
              "`+90m` or `YYYY-MM-DD`. The deadline is enforced on every "
              "delivery path, not merely recorded"),
    )
    squads.add_argument(
        "--purge", action="store_true",
        help="rm: also drop memberships (message history is kept regardless)",
    )
    squads.add_argument(
        "--keep-source", action="store_true",
        help="merge: leave the source squad in place instead of archiving it",
    )
    squads.add_argument("--dry-run", action="store_true",
                        help="print what would change; write nothing")
    squads.add_argument(
        "--hub-url", default=DEFAULT_HUB_URL,
        help="Hub base URL (default: $MCP_HUB_URL or built-in)",
    )
    squads.add_argument("--json", action="store_true",
                        help="Machine-readable output")

    voice_client = sub.add_parser(
        "voice-client",
        help="Container-side /voice: pull audio from the host, feed the local sink",
        description=(
            "Runs INSIDE a seat container. Reads the default gateway from "
            "/proc/net/route, connects out, names this seat, and streams the "
            "operator's microphone into the container's own pulse sink. "
            "Never binds a port. Never fails a seat."
        ),
    )
    voice_client.add_argument("--seat", default="",
                              help="Override SEAT_IDENTITY (tests/manual runs)")
    voice_client.add_argument("--port", type=int, default=_VOICE_PORT,
                              help=f"Host voice port (default {_VOICE_PORT})")

    voice_host = sub.add_parser(
        "voice-host",
        help="Host-side /voice: serve the operator's mic to seats that dial in",
        description=(
            "Runs on the DOCKER HOST. Binds the bridge gateway (never the "
            "wildcard — ufw permits everything on tailscale0, so 0.0.0.0 "
            "would publish the microphone to the tailnet), verifies each "
            "caller against docker, then streams one way and never reads."
        ),
    )
    voice_host.add_argument("--port", type=int, default=_VOICE_PORT,
                            help=f"Listen port (default {_VOICE_PORT})")
    voice_host.add_argument("--gateway", default="",
                            help="Bind address (default: the docker bridge gateway)")
    voice_host.add_argument("--source", default="claude_mic.monitor",
                            help="Pulse source to capture (default claude_mic.monitor)")
    voice_host.add_argument("--squad-conf", default="",
                            help=(
                                "Roster deciding which containers are SEATS "
                                "(default $SQUAD_CONF or "
                                "~/.config/squad/squad.conf). Read per "
                                "connection; unreadable means no audio"
                            ))
    voice_host.add_argument("--dead-after", type=float, default=60.0,
                            help=(
                                "Seconds of zero progress before a peer is "
                                "reaped — TCP alone takes ~15 min to notice a "
                                "killed container (default 60)"
                            ))

    seat_entry = sub.add_parser(
        "seat-entry",
        help="Container entrypoint: validate the seat contract, prepare, launch claude",
        description=(
            "PID 1 of mcp-hub-seat. Validates the env against "
            "docs/seat-image.md (exit 42 = credential missing/implausible, "
            "43 = contract violation), clones SEAT_REPO into the workdir "
            "when empty, writes the identity marker + .mcp.json + hook "
            "settings, then runs claude under tmux (session 'seat'). "
            "Idempotent: a container restart re-runs it safely."
        ),
    )
    seat_entry.add_argument(
        "--workdir",
        default=None,
        help="Seat working directory (default: ~/work)",
    )
    seat_entry.add_argument(
        "--prepare-only",
        action="store_true",
        help="Validate + write files, but do not launch claude (tests/debug)",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    # Windows consoles default to cp1252, which can't encode ✓/⚡/emoji in
    # our output — an unhandled UnicodeEncodeError turned memory-verify's
    # result line into a traceback on fireblade (found by the first live
    # ceremony). Force UTF-8 on the std streams; errors='replace' so even a
    # truly broken console degrades to '?' instead of crashing. Fail-soft:
    # exotic stdout replacements without reconfigure() are left alone.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass

    parser = build_parser()
    args = parser.parse_args(argv)

    if args.subcommand == "stop-hook":
        return stop_hook_command(args)
    if args.subcommand == "session-start":
        return session_start_command(args)
    if args.subcommand == "session-rewake":
        return session_rewake_command(args)
    if args.subcommand == "heartbeat-daemon":
        return heartbeat_daemon_command(args)
    if args.subcommand == "onboard":
        return onboard_command(args)
    if args.subcommand == "memory-export":
        return memory_export_command(args)
    if args.subcommand == "memory-import":
        return memory_import_command(args)
    if args.subcommand == "memory-verify":
        return memory_verify_command(args)
    if args.subcommand == "identity":
        return identity_command(args)
    if args.subcommand == "settings":
        return settings_command(args)
    if args.subcommand == "board":
        # `board` IS the panel: settings --tui wearing the screen's real name.
        args.tui = True
        args.cwd = None
        args.json = False
        return settings_command(args)
    if args.subcommand == "mute":
        return mute_command(args)
    if args.subcommand == "transport-history":
        return transport_history_command(args)
    if args.subcommand == "rebind-url":
        return rebind_url_command(args)
    if args.subcommand == "edge":
        return edge_command(args)
    if args.subcommand == "hibernate":
        return hibernate_command(args)
    if args.subcommand == "workspaces":
        return workspaces_command(args)
    if args.subcommand == "seats":
        return seats_command(args)
    if args.subcommand == "placements":
        return placements_command(args)
    if args.subcommand == "capsules":
        return capsules_command(args)
    if args.subcommand == "squads":
        return squads_command(args)
    if args.subcommand == "machines":
        return machines_command(args)
    if args.subcommand == "send":
        return send_command(args)
    if args.subcommand == "focus":
        return focus_command(args)
    if args.subcommand == "voice-client":
        return voice_client_command(args)
    if args.subcommand == "voice-host":
        return voice_host_command(args)
    if args.subcommand == "seat-entry":
        return seat_entry_command(args)

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
