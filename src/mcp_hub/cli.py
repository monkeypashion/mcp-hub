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
import hashlib
import json
import os
import pathlib
import platform
import re
import subprocess
import sys
import time
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


_DECIDED_RE = re.compile(r"\*{0,2}DECIDED:\*{0,2}\s*(.+?)\s*$", re.M)


def _extract_decided(turn_text: str) -> str:
    """The `**DECIDED:** <verdict>` closing marker, '' if none. The agent
    that received an in-pane answer records the verdict itself (it just
    understood and acted on it) — machinery only ships; last one wins."""
    matches = list(_DECIDED_RE.finditer(turn_text))
    return matches[-1].group(1).strip() if matches else ""


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
    decided: str = "",
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
            msg_args = {
                "agent_name": agent_name,
                "bind": False,
                "mark_idle": True,
                "compact": True,
            }
            try:
                messages_result = await session.call_tool("get_messages", msg_args)
            except Exception:  # noqa: BLE001
                msg_args.pop("compact")
                messages_result = await session.call_tool("get_messages", msg_args)
            # compact=True mirrors the DM economy onto broadcasts (they were
            # the unclipped half of the Stop-hook context tax). Same
            # version-skew fallback as get_messages above: during a deploy a
            # newer CLI may hit an older hub that rejects the flag.
            bc_args = {"agent_name": agent_name, "bind": False, "compact": True}
            try:
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
    has_messages = bool(messages_text.strip())
    has_broadcasts = bool(broadcasts_text.strip())
    has_content = has_messages or has_broadcasts

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
    if stop_hook_active and not has_content:
        return None

    # No work needed: online + nothing queued + no correction owed.
    # (Online — not ⚡ — is the gate: an idle agent legitimately lacks ⚡
    # between turns.)
    if not has_content and is_online and not card_nag and not card_notice:
        return None

    parts: list[str] = []

    if has_content:
        parts.append("📬 Auto-checked at Stop boundary — queued items below:")
        if has_messages:
            parts.extend(["", "**Direct messages:**", messages_text.strip()])
        if has_broadcasts:
            parts.extend(["", "**Broadcasts (since you last looked):**", broadcasts_text.strip()])

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
                "with **DECIDED:** <their verdict>. If you're still waiting, "
                "restate the card to keep it fresh."
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

    Mirrored in statusline-command.js — change both or neither.
    """
    table = _load_hub_config().get("workspaces")
    if not isinstance(table, dict):
        return None
    target = _norm_path(cwd)
    for path, suffix in table.items():
        if isinstance(path, str) and isinstance(suffix, str) and suffix.strip():
            if _norm_path(path) == target:
                return suffix.strip()
    return None


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
        print("", end="")
        return 1
    if args.json:
        print(json.dumps({"name": name, "project": project, "cwd": cwd}))
    else:
        print(name)
    return 0


SQUAD_CONF = pathlib.Path.home() / ".config" / "squad" / "squad.conf"


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
                "value": "muted" if s in muted else "hearing it",
                "source": member_source,
                # Mute only. JOINING and LEAVING are deliberately absent:
                # membership derives from declaring a workspace as a squad, and
                # a per-agent override here would be a second source of truth
                # that silently disagrees with the workspace. Attention is per
                # agent; membership is not.
                "edit": _edit("mute", ["hear", "mute"],
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


def mute_command(args: argparse.Namespace) -> int:
    """Silence one squad's broadcasts for one agent, without leaving it.

    Exists as a CLI verb because the cockpit cannot call MCP tools — it shells
    out. Deliberately does NOT join or leave: membership derives from declaring
    a workspace as a squad, and a second way to set it would disagree with the
    workspace sooner or later. Attention is per agent; membership is not.
    """
    muted = args.state == "mute"
    try:
        reply = asyncio.run(_mute_squad(args.hub_url, args.agent, args.squad, muted))
    except Exception as exc:  # noqa: BLE001
        print(f"!! mute failed: {exc}", file=sys.stderr)
        return 1
    print(reply or f"{args.agent}: {args.squad} {'muted' if muted else 'unmuted'}")
    # The hub is the record; the status cache is a stale copy the statusline
    # and this panel both read. Left alone it keeps showing the old state until
    # the daemon's next beat, which reads as "the click did nothing".
    _invalidate_status_cache(args.agent)
    return 0


def _invalidate_status_cache(agent: str) -> None:
    """Drop the cached snapshot so the next read comes from the hub.

    Deleting rather than editing: this file is written by the daemon from a
    live query, and hand-patching one field here would make it a second author
    of a cache whose whole value is being a faithful copy.
    """
    try:
        _status_cache_path(agent).unlink()
    except OSError:
        pass


def settings_command(args: argparse.Namespace) -> int:
    """Read-only: every setting that governs one agent, and where it came from."""
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

    try:
        messages_text, broadcasts_text, is_online, card_notice = asyncio.run(
            _query_hub(args.hub_url, name, project or "", card, decided)
        )
    except Exception as exc:  # noqa: BLE001
        # Fail open — never block the agent on hub flakiness.
        print(f"[mcp-hub stop-hook] hub query failed: {exc!r}", file=sys.stderr)
        return 0

    response = build_hook_response(
        agent_name=name,
        project=project,
        messages_text=messages_text,
        broadcasts_text=broadcasts_text,
        is_online=is_online,
        stop_hook_active=stop_hook_active,
        card_nag=card_nag,
        card_notice=card_notice,
    )

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

    Returns {online, wakeable, fleet_wakeable, fleet_total} where online/
    wakeable are this agent's own state and the fleet_* are totals across all
    listed (online) agents.
    """
    fleet_total = 0
    fleet_wakeable = 0
    self_online = False
    self_wakeable = False
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
    return {
        "online": self_online,
        "wakeable": self_wakeable,
        "fleet_wakeable": fleet_wakeable,
        "fleet_total": fleet_total,
    }


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
    mute.add_argument("--state", required=True, choices=["hear", "mute"])
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
    if args.subcommand == "mute":
        return mute_command(args)
    if args.subcommand == "transport-history":
        return transport_history_command(args)
    if args.subcommand == "rebind-url":
        return rebind_url_command(args)

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
