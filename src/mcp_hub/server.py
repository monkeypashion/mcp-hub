"""
MCP Hub — inter-agent messaging server.

A lightweight message broker that lets multiple Claude sessions
discover each other and exchange messages in real time.

Supports direct messages, broadcast channels, and agent presence.
Backed by SQLite for persistence across restarts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, NamedTuple

import anyio
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.streamable_http import GET_STREAM_KEY
from pydantic import BaseModel

from .session_registry import SessionRegistry, live_server_sessions

logger = logging.getLogger(__name__)


def _resolve_commit() -> str:
    """Best-effort git SHA of the running code, for the /health endpoint.

    Resolution order: MCP_HUB_GIT_SHA env (baked at build time via the
    Dockerfile ARG) → read the repo's .git directly (the image ships the
    source incl. .git, so this works even when the env isn't set) →
    'unknown'. Pure-Python git read so the slim image needs no git binary.
    """
    sha = os.environ.get("MCP_HUB_GIT_SHA")
    if sha and sha.strip() and sha.strip() != "unknown":
        return sha.strip()
    try:
        git_dir = Path(__file__).resolve().parents[2] / ".git"
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref:"):
            return head  # detached HEAD — already a raw SHA
        ref = head.split(":", 1)[1].strip()
        loose = git_dir / ref
        if loose.exists():
            return loose.read_text(encoding="utf-8").strip()
        packed = git_dir / "packed-refs"
        if packed.exists():
            for line in packed.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.endswith(" " + ref):
                    return line.split(" ", 1)[0]
    except Exception:  # noqa: BLE001
        pass
    return "unknown"


class _ChannelNotification(BaseModel):
    """MCP notification matching Claude Code's experimental claude/channel protocol.

    Sent on `send`/`broadcast` so the recipient's Claude Code session wakes
    (even from idle) and processes the message immediately, instead of needing
    to be prompted to poll get_messages.
    """

    method: str = "notifications/claude/channel"
    params: dict[str, Any]


class _PushOutcome(NamedTuple):
    """Result of a fanned-out channel push.

    `delivered` — reached ≥1 live session (drives woke-reporting / wake-ack).
    `primary`   — the PRIMARY session specifically got it (the ONLY signal that
                  may gate the compact-render generation stamp, which is keyed
                  to the primary's stream). See push_channel for why.
    """

    delivered: bool
    primary: bool


# Allowed priority values for send/broadcast. The hub uses priority to decide
# whether to fire the channel-push wake; senders are responsible for picking
# the right level so receivers aren't interrupted by FYIs while focused.
#   - "low":    queue-only by default; for DMs ONLY, fires wake when the
#               recipient is currently idle (Case 1 — see send() body).
#               Channel posts and broadcasts at low stay queue-only
#               regardless of recipient state.
#   - "normal": inbox + channel push (default). Wake on receipt.
#   - "urgent": inbox + channel push, with priority="urgent" in the rendered
#               tag's meta so receivers can visually flag it.
_VALID_PRIORITIES = {"low", "normal", "urgent"}
_NO_WAKE_PRIORITIES = {"low"}

# Stop-hook (compact) rendering budget. The Stop hook fires at EVERY turn
# boundary and its output lands verbatim in the agent's context, so an
# unbounded dump is a recurring context tax paid by every agent all day.
# Render this many bodies in full; summarise the rest. "Full" is itself
# clipped: the 2-message budget was designed for backlog floods, but the
# common case turned out to be ONE long DM per Stop (operator, 2026-07-25),
# which sailed through the budget at 2-3KB a pop. Nothing is dropped —
# clipped bodies point at get_history for the full text.
COMPACT_FULL_MESSAGES = 2
COMPACT_FULL_BODY_CHARS = 700
COMPACT_SUMMARY_CHARS = 220


def _clip(body: str, limit: int = COMPACT_FULL_BODY_CHARS) -> str:
    """Clip `body` to `limit` chars at a line boundary where possible.
    Returns the body unchanged when it already fits."""
    if len(body) <= limit:
        return body
    cut = body[:limit]
    # Prefer breaking at the last newline inside the window so the clip
    # doesn't end mid-sentence more than it has to; fall back to a hard cut.
    nl = cut.rfind("\n")
    if nl > limit // 2:
        cut = cut[:nl]
    return cut.rstrip() + " […clipped]"


_TLDR_BODY_CHARS = 1500
_TLDR_FIRST_LINE_CHARS = 200


_TLDR_MARKER_RE = re.compile(r"^\s*(\*{0,2})(tl;?dr|summary)\b", re.I)


def _verbosity_advisory(message: str) -> str:
    """One advisory line back to the SENDER of a long message that doesn't
    lead with a summary. Correction at the moment of the offense — visible
    only in the sender's own tool result, never to recipients. The clip
    protects readers; this trains writers.

    An explicit TL;DR/Summary marker counts as compliance regardless of how
    long that line runs — the first version keyed on line length alone and
    flagged a message that literally began "TL;DR:" (fb-wsl, 2026-07-26).
    A false positive in a training signal teaches people to ignore it."""
    if len(message) <= _TLDR_BODY_CHARS:
        return ""
    first = message.strip().splitlines()[0] if message.strip() else ""
    if _TLDR_MARKER_RE.match(first):
        return ""
    if len(first) <= _TLDR_FIRST_LINE_CHARS:
        return ""
    return (
        "\n📏 Advisory: long message with no leading TL;DR — live renders "
        "clip at 700 chars, so put a 1-2 line summary first next time."
    )


def _clip_push(body: str) -> str:
    """Clip a body for LIVE push rendering (channel tags). The push path had
    NO economy at all while the poll path got two rounds of it — measured
    2026-07-26: 840KB/day of unclipped live tags vs 244KB via the Stop hook,
    3.4x, because fan-out multiplies every byte by the bound-agent count.
    Nothing is dropped: the inbox/history row keeps the full body (this is
    also the existing lossless path for whitespace-significant payloads —
    rendered tags were never byte-trustworthy)."""
    clipped = _clip(body)
    if clipped is not body:
        clipped += " (full text: get_history)"
    return clipped


# Card field lines: **VALUE:** <sentence> [7/10] — score in [n/10] at the end
# (also tolerates (n/10) and bare n/10). Legacy single **SCORE:** n/10 cards
# parse too; net = value - risk only when BOTH component scores present.
_CARD_FIELD_RE = re.compile(
    r"\*{0,2}(ASK|WHY|VALUE|RISK|SCORE|TAGS|NET):\*{0,2}\s*(.*?)(?=\s*\*{0,2}(?:ASK|WHY|VALUE|RISK|SCORE|TAGS|NET):|\Z)",
    re.S | re.I,
)
_CARD_SCORE_RE = re.compile(r"[\[\(]?\s*(\d{1,2})\s*/\s*10\s*[\]\)]?\s*$")


def parse_decision_card(raw: str) -> dict:
    """Parse a DECISION card's fields out of its raw text. Tolerant by
    design: unknown/missing fields parse to empty, never raise — a card that
    fails to parse still stores raw and flags the hand."""
    fields: dict = {"ask": "", "why": "", "value_text": "", "risk_text": "",
                    "value_score": None, "risk_score": None,
                    "net_score": None, "tags": ""}
    for m in _CARD_FIELD_RE.finditer(raw):
        label = m.group(1).upper()
        text = " ".join(m.group(2).split())
        if label in ("VALUE", "RISK"):
            score = None
            sm = _CARD_SCORE_RE.search(text)
            if sm:
                score = min(10, int(sm.group(1)))
                text = text[: sm.start()].rstrip(" -—·")
            fields[label.lower() + "_text"] = text
            fields[label.lower() + "_score"] = score
        elif label == "SCORE":
            # legacy single-score card: keep as net when components absent
            sm = _CARD_SCORE_RE.search(text)
            if sm and fields["net_score"] is None:
                fields["net_score"] = min(10, int(sm.group(1)))
        elif label == "TAGS":
            fields["tags"] = ",".join(
                t.strip().lower() for t in text.split(",") if t.strip()
            )
        elif label == "NET":
            # Author-asserted total, for PANE display only — the hub always
            # recomputes net from the components; a fumbled author sum must
            # not override arithmetic.
            pass
        else:
            fields[label.lower()] = text
    if fields["value_score"] is not None and fields["risk_score"] is not None:
        fields["net_score"] = fields["value_score"] - fields["risk_score"]
    return fields


def _summarise(body: str, limit: int = 120) -> str:
    """First line of `body`, clipped to `limit` chars. Never empty for a
    non-empty body — the point is a recognisable handle, not a précis."""
    first = body.strip().splitlines()[0] if body.strip() else ""
    first = first.strip()
    if len(first) <= limit:
        return first
    return first[: limit - 1].rstrip() + "…"

# Case 1 — wake-on-low-prio for idle DM recipients.
#
# Liveness gate: we use the registry binding itself as the "is this agent
# alive" signal — not a separate is_idle decay timer. Reasoning:
#   - The heartbeat daemon refreshes _last_activity every 60s, keeping the
#     binding alive while the agent's Claude Code process is up.
#   - If the process dies, the daemon dies (process-tree reap), heartbeats
#     stop, and the activity-based reaper drops the binding within ~60 min.
#   - push() to an unbound name returns False, so a wake never actually
#     fires for a "dead but flag-stuck-on" agent.
# This means is_idle=1 is meaningful indefinitely as long as the agent is
# bound. An earlier draft used a 30-min decay on last_idle_at; that was
# over-defensive and caused genuinely-idle agents to stop receiving
# Case 1 wakes after 30 min of operator inactivity.

# Single hard-coded broadcast channel. We deliberately don't expose multi-
# channel admin (create_channel / list_channels / per-channel ACLs / etc.)
# — kept the model collapsed to "DMs + one global broadcast" so we can't
# accumulate dozens of half-used channels via typos. The DB column stays
# generic in case we ever re-introduce channels; this is just the slot
# every broadcast goes into today.
_BROADCAST_CHANNEL = "general"

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

DB_PATH = Path("mcp-hub.db")
_local = threading.local()


def _get_db(db_path: Path = DB_PATH) -> sqlite3.Connection:
    """Get a thread-local SQLite connection, keyed by db_path.

    Caching has to be path-aware: production runs one server with one DB,
    so a single cached connection works there — but in tests where each
    test uses its own tmp_path DB, sharing one connection would silently
    leak state across tests (every call to _get_db on a different path
    would return the FIRST path's connection, since the SQLite file
    actually open is whatever was opened first).
    """
    if not hasattr(_local, "conns"):
        _local.conns = {}
    key = str(db_path)
    conn = _local.conns.get(key)
    if conn is None:
        conn = sqlite3.connect(key, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        _local.conns[key] = conn
    return conn


def init_db(db_path: Path = DB_PATH) -> None:
    """Create tables if they don't exist."""
    conn = _get_db(db_path)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS agents (
            name        TEXT PRIMARY KEY,
            project     TEXT NOT NULL DEFAULT '',
            bio         TEXT NOT NULL DEFAULT '',
            status      TEXT NOT NULL DEFAULT 'online',
            registered  REAL NOT NULL,
            last_seen   REAL NOT NULL,
            meta        TEXT NOT NULL DEFAULT '{}'
        );

        CREATE TABLE IF NOT EXISTS channels (
            name        TEXT PRIMARY KEY,
            created_by  TEXT NOT NULL,
            created_at  REAL NOT NULL,
            description TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS messages (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          REAL NOT NULL,
            from_agent  TEXT NOT NULL,
            to_agent    TEXT,
            channel     TEXT,
            body        TEXT NOT NULL,
            read        INTEGER NOT NULL DEFAULT 0,
            priority    TEXT NOT NULL DEFAULT 'normal'
        );

        CREATE INDEX IF NOT EXISTS idx_msg_to ON messages(to_agent, read);
        CREATE INDEX IF NOT EXISTS idx_msg_channel ON messages(channel, ts);
        CREATE INDEX IF NOT EXISTS idx_msg_ts ON messages(ts);

        -- Memory transfer store: a staging area for exporting a clone's
        -- Claude memory files to its twins (same derived project, other
        -- machines). NOT the system of record — the files' home remains
        -- each machine's ~/.claude/projects/<dir>/memory. Last-write-wins
        -- per (project, filename).
        CREATE TABLE IF NOT EXISTS memory_files (
            project      TEXT NOT NULL,
            filename     TEXT NOT NULL,
            content      TEXT NOT NULL,
            updated_ts   REAL NOT NULL,
            origin_agent TEXT NOT NULL,
            PRIMARY KEY (project, filename)
        );

        -- DECISION cards: the operator-triage currency (2026-07-26 design).
        -- One OPEN card per agent (the "one live DECISION at a time"
        -- convention) — decision_put upserts the open card, so a restated
        -- ask updates in place instead of duplicating. Derived metadata is
        -- filled server-side; agent-authored fields come from the card
        -- text. status: open | decided | withdrawn.
        CREATE TABLE IF NOT EXISTS decisions (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            agent        TEXT NOT NULL,
            project      TEXT NOT NULL DEFAULT '',
            source       TEXT NOT NULL DEFAULT 'stop-hook',
            submitted_at REAL NOT NULL,
            updated_at   REAL NOT NULL,
            raw          TEXT NOT NULL,
            ask          TEXT NOT NULL DEFAULT '',
            why          TEXT NOT NULL DEFAULT '',
            value_text   TEXT NOT NULL DEFAULT '',
            risk_text    TEXT NOT NULL DEFAULT '',
            value_score  INTEGER,
            risk_score   INTEGER,
            net_score    INTEGER,
            tags         TEXT NOT NULL DEFAULT '',
            status       TEXT NOT NULL DEFAULT 'open',
            decided_at   REAL,
            decision     TEXT NOT NULL DEFAULT '',
            decision_note TEXT NOT NULL DEFAULT '',
            clear_strikes INTEGER NOT NULL DEFAULT 0,
            stale        INTEGER NOT NULL DEFAULT 0
        );
    """)
    conn.commit()

    # Migrate: strike counter for existing decisions tables (deployed before
    # the 3-strike withdrawal rule).
    try:
        conn.execute(
            "ALTER TABLE decisions ADD COLUMN "
            "clear_strikes INTEGER NOT NULL DEFAULT 0"
        )
        conn.commit()
    except sqlite3.OperationalError:
        pass  # Column already exists

    # Migrate: coverage-gap tracking (2026-07-27 — fo's five-lane false alarm:
    # a disconnected client produces an agent that BELIEVES it has seen
    # everything; "delivery and awareness of non-delivery are different
    # events, and only the hub can supply the second"). offline_since stamps
    # when an agent's binding died; on the next coming-online the hub counts
    # what arrived in the gap and get_messages surfaces it ONCE.
    for ddl in (
        "ALTER TABLE agents ADD COLUMN offline_since REAL",
        "ALTER TABLE agents ADD COLUMN gap_notice TEXT NOT NULL DEFAULT ''",
        # Attribution grading (item 34): 'session-verified' when the calling
        # session was bound to exactly the asserted from_agent; 'asserted'
        # otherwise (ephemeral/unbound callers — stop-hook, daemons). The
        # ledger reads attribution strength instead of presuming it.
        "ALTER TABLE messages ADD COLUMN attribution TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE decisions ADD COLUMN attribution TEXT NOT NULL DEFAULT ''",
    ):
        try:
            conn.execute(ddl)
            conn.commit()
        except sqlite3.OperationalError:
            pass  # Column already exists

    # Migrate: stale flag. Three cardless turns stopped meaning "withdraw"
    # on 2026-07-27 — ~25 asks evaporated unanswered in one day because
    # strikes measure the SENDER's turn rate, nothing else (pm's finding:
    # the harder a blocked lane works, the faster it loses its ask). A
    # stale card stays status='open' — visible to the operator and every
    # existing open-reader — just demoted in sort order. Only an operator
    # answer, an agent DECIDED, or supersession by a new ask closes a card.
    try:
        conn.execute(
            "ALTER TABLE decisions ADD COLUMN stale INTEGER NOT NULL DEFAULT 0"
        )
        conn.commit()
    except sqlite3.OperationalError:
        pass  # Column already exists

    # Migrate: add bio column for existing databases
    try:
        conn.execute("ALTER TABLE agents ADD COLUMN bio TEXT NOT NULL DEFAULT ''")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # Column already exists

    # Migrate: record WHICH binding generation a message was pushed to, so the
    # Stop hook can tell "you already saw this live" from "this may have gone
    # into a dead stream". Empty string = never pushed live (or pushed by a
    # pre-migration hub) -> always rendered in full.
    try:
        conn.execute(
            "ALTER TABLE messages ADD COLUMN pushed_gen TEXT NOT NULL DEFAULT ''"
        )
        conn.commit()
    except sqlite3.OperationalError:
        pass  # Column already exists

    # Migrate: add priority column for existing databases
    try:
        conn.execute(
            "ALTER TABLE messages ADD COLUMN priority TEXT NOT NULL DEFAULT 'normal'"
        )
        conn.commit()
    except sqlite3.OperationalError:
        pass  # Column already exists

    # Migrate: add per-agent broadcast cursor. Stop hooks surface unseen
    # broadcasts via this cursor so drifted agents catch up on broadcast
    # history they missed while unbound. New rows default to 0 (will be
    # bumped to current-max on register for first-time agents). Existing
    # rows: bump them to current-max here so we don't firehose them with
    # historical broadcasts they already lived through.
    try:
        conn.execute(
            "ALTER TABLE agents ADD COLUMN last_broadcast_seen_id "
            "INTEGER NOT NULL DEFAULT 0"
        )
        # Catch existing agents up to "now" so the first Stop hook fire
        # post-migration doesn't dump every broadcast in the feed.
        conn.execute(
            "UPDATE agents SET last_broadcast_seen_id = ("
            "  SELECT COALESCE(MAX(id), 0) FROM messages WHERE channel = ?"
            ")",
            (_BROADCAST_CHANNEL,),
        )
        conn.commit()
    except sqlite3.OperationalError:
        pass  # Column already exists

    # Migrate: TEAM — the squad an agent belongs to, and the unit a broadcast
    # is scoped to.
    #
    # NOT the project, and not the org. Measured 2026-07-27: one squad's
    # investigation spanned dreamteam-ai-labs/{pm,factory-operations,dreamteam,
    # spike} AND monkeypashion/vps-hetzner — four projects across two orgs,
    # collaborating legitimately. Scoping by project would have severed them
    # from each other; scoping by org would have put vps with mcp-hub, i.e.
    # joined to the agents that should have been excluded and cut off from the
    # squad it actually works with. Both cut across the real boundary.
    #
    # The real unit is the WORKSPACE — it is already what squad scopes tabs,
    # transport and teardown by — but workspace FILES are machine-local and
    # their names differ per machine, so the name cannot be the identifier. An
    # explicit team is the smallest thing that survives crossing machines.
    #
    # Empty means "no declared team", which deliberately behaves exactly as
    # today: an agent with no team broadcasts fleet-wide, because a group we
    # cannot name is not a group we may silently exclude people from.
    try:
        conn.execute("ALTER TABLE agents ADD COLUMN team TEXT NOT NULL DEFAULT ''")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # Column already exists

    # Migrate: AUDIENCE — who a broadcast was for, recorded ON THE ROW at send
    # time rather than recomputed from the sender later. A sender's team can
    # change; a message's audience must not move retroactively, or the Stop-hook
    # catch-up would show an agent history it was never party to (or hide
    # history it was). Empty = fleet-wide, which is what every pre-existing row
    # was, so the default is also the correct backfill.
    try:
        conn.execute("ALTER TABLE messages ADD COLUMN audience TEXT NOT NULL DEFAULT ''")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # Column already exists

    # Migrate: add idle-tracking columns. Used by the Case 1 wake-on-low-prio
    # path: low-prio DMs to an idle recipient fire wake (so soft asks
    # surface immediately) while staying queue-only for running recipients
    # (no interrupt to active work). is_idle is set true by the Stop hook
    # at turn end and cleared by any identifying tool call (touch_session).
    # last_idle_at decays the flag — if a session crashed without firing
    # the Stop-hook un-idle, we treat is_idle=1 with last_idle_at older
    # than IDLE_DECAY_SECONDS as "presumed dead" and don't wake on low.
    for col_sql in (
        "ALTER TABLE agents ADD COLUMN is_idle INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE agents ADD COLUMN last_idle_at REAL NOT NULL DEFAULT 0",
    ):
        try:
            conn.execute(col_sql)
            conn.commit()
        except sqlite3.OperationalError:
            pass  # Column already exists



# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _log_bind_diagnostic(source: str, name: str, session: Any) -> None:
    """One-line dump of clientInfo + FULL capabilities on every bind decision.

    Read prod logs for these lines to see exactly what each kind of client
    advertises. This found the clientInfo.name discriminator that now gates
    the touch bind (see is_interactive_client) — and it stays wide on
    purpose: the last usable signal (experimental capabilities) vanished
    silently and took a week of archaeology to notice. Logging the whole
    handshake surface means the NEXT vanished-or-renamed signal shows up in
    the same day's logs, not as retrospective guesswork. Rejections are
    logged too (source=touch_session-skipped), so a future client rename
    appears as a stream of skipped binds naming the new string.
    """
    try:
        params = getattr(session, "client_params", None)
        client_info = None
        capabilities = None
        if params is not None:
            ci = getattr(params, "clientInfo", None)
            if ci is not None:
                client_info = (
                    f"{getattr(ci, 'name', '?')}/"
                    f"{getattr(ci, 'version', '?')}"
                )
            capabilities = getattr(params, "capabilities", None)
        logger.info(
            "bind-diag source=%s name=%s sid=%x clientInfo=%s capabilities=%s",
            source, name, id(session), client_info, capabilities,
        )
    except Exception:  # noqa: BLE001
        # Diagnostic must never break a real bind path.
        logger.debug("bind-diag failed", exc_info=True)


def is_channel_capable(session: Any) -> bool:
    """True if `session`'s client declares the claude/channel experimental
    capability.

    ⚠️ RETURNS FALSE FOR EVERY CLIENT, AND IS NO LONGER WIRED ANYWHERE.
    Superseded at the touch-bind site by is_interactive_client (clientInfo
    discriminator). RETAINED AS EVIDENCE, NOT AS A UTILITY — do not wire it
    back in anywhere: the record below is why no client-declared capability
    predicate can ever work in this protocol. Read this before using it,
    extending it, or "fixing" #17 with it.

    What it was written for: every Stop hook (cli.py) spawns a fresh
    streamablehttp_client to call get_messages / get_broadcasts_for_agent.
    That bare client is torn down when the hook process exits, so without a
    gate its identifying tool calls hit `touch_session`, overwrite the agent's
    real wake-binding with the ephemeral session_id, and leave the binding
    pointing at a dead session — silently breaking wake on every Stop-hook
    fire.

    Why it does not discriminate: `claude/channel` is a SERVER-declared
    capability in this protocol, not a client-declared one. Clients check
    whether the SERVER declared it (Claude Code filters connected servers on
    `capabilities.experimental["claude/channel"]` and errors with "server did
    not declare claude/channel capability"); the hub declares it in
    create_server, which is why pushes render at all. Clients never send it,
    so this predicate is unsatisfiable as written.

    Established 2026-07-25 by two independent methods:
      * a localhost listener captured the raw `initialize` from a real
        2.1.220 — BOTH channels-loaded and flagless arms byte-identical:
        capabilities={"roots":{"listChanged":true},"elicitation":{}}, no
        experimental dict in either;
      * string extraction from the 2.1.219 and 2.1.220 client bundles found
        identical channel-capability counts, so no client version ever
        "dropped" it — and the only `experimental: { 'claude/channel': {} }`
        occurrences are documentation examples aimed at server authors.

    Consequences, all load-bearing:
      * The touch gate below has rejected 100% of sessions since it was wired
        2026-07-18. It appeared to fix the clobbering bug because blocking
        every bind necessarily blocks the ephemeral ones too — it never
        distinguished them. Tool-call drift self-heal is therefore dead, and
        register() (which has NO such gate) is the only reason any agent in
        the fleet is bound at all.
      * DO NOT gate register() on this — it would unbind the entire fleet.
        That is the parked #17, and it must not be revived in this form.
      * Client-side capability declaration is not available as evidence for
        anything, including the compact "already delivered live" claim. Use
        BEHAVIOURAL evidence instead (see has_pending_wake_ack) or inspect the
        agent's launch args out-of-band (squad's has_comms()).

    Kept rather than deleted because the gate's blanket-reject happens to
    preserve the property it was added for; replacing it needs a predicate
    that actually discriminates, which is a design task, not a cleanup.
    """
    params = getattr(session, "client_params", None)
    if params is None:
        return False
    caps = getattr(params, "capabilities", None)
    if caps is None:
        return False
    experimental = getattr(caps, "experimental", None) or {}
    return "claude/channel" in experimental


# ALLOWLIST, deliberately — an unknown future client name must default to
# REJECTED (no bind), never accepted. The fail direction is safe: a predicate
# that never matches leaves us exactly where the dead capability gate did
# (touch_session never binds, register carries every binding) — no
# regression, just no repair. Do NOT "fix" a non-matching name by flipping
# this to a denylist; read the observed name out of the bind-diag logs and
# add it here explicitly.
INTERACTIVE_CLIENT_NAMES = frozenset({"claude-code"})


def is_interactive_client(session: Any) -> bool:
    """True if `session` belongs to a real interactive Claude Code client —
    the only kind of session that may own an agent's wake binding via
    touch_session.

    The discriminator is `clientInfo.name`, MEASURED not assumed
    (2026-07-26, both directions):

      * Real Claude Code 2.1.220 (localhost capture rig, raw `initialize`):
        clientInfo = {"name": "claude-code", "title": "Claude Code",
        "version": "2.1.220", ...} — `name` is exactly "claude-code".
      * Every ephemeral utility client we spawn (stop-hook, heartbeat
        daemon, memory-export twin-notify, scripts): cli.py never passes
        client_info, so the mcp SDK default applies — verified live:
        DEFAULT_CLIENT_INFO = name='mcp' version='0.1.0'. "mcp" is a
        known-real rejected value, not a hypothetical.

    Rules, agreed with fireblade-wsl before building:
      * Match NAME only, never version — 219→220-style churn must not
        unbind the fleet.
      * Gate touch_session ONLY, never register(). If clientInfo.name ever
        changes, a touch-only gate degrades to exactly today's survivable
        state (register carries the bindings); a register gate would
        silently unbind the entire fleet.
      * clientInfo is client-asserted (spoofable) — same trust level as
        everything else on this hub. And claude-code ≠ flag-loaded: a
        flagless interactive session binds-but-deaf, the same exposure
        register() already has; wake-ack strikes keep the claims honest.
        Composition: clientInfo gates BINDING, strikes gate CLAIMS.
    """
    params = getattr(session, "client_params", None)
    if params is None:
        return False
    ci = getattr(params, "clientInfo", None)
    if ci is None:
        return False
    return getattr(ci, "name", None) in INTERACTIVE_CLIENT_NAMES


# ---------------------------------------------------------------------------
# URL-identity capture (auto-rebind, 2026-07-27)
# ---------------------------------------------------------------------------

# A seat's .mcp.json hub URL may carry ?agent=<name>. Claude Code's transport
# layer reconnects on its own after a hub restart — no agent turn involved —
# and every request it sends carries that query string. Capturing it onto the
# transport gives the rebind sweep a name to bind the new session to, which
# is what turns a deploy from a fleet event (every seat drops, gets nagged,
# burns a re-register turn) into a non-event.
#
# Same monkey-patch discipline as session_registry's __aexit__ hook:
# idempotent, process-global, one seam.

_URL_AGENT_ATTR = "_hub_url_agent"
_URL_SEEN_ATTR = "_hub_url_seen"
_handle_request_patched = False


def _ensure_url_identity_patched() -> None:
    global _handle_request_patched
    if _handle_request_patched:
        return
    from urllib.parse import parse_qs

    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

    original = StreamableHTTPSessionManager.handle_request

    async def patched_handle_request(self, scope, receive, send):  # type: ignore[no-untyped-def]
        try:
            qs = parse_qs((scope.get("query_string") or b"").decode("latin-1"))
            agent = (qs.get("agent") or [""])[0]
            if agent:
                sid = ""
                for k, v in scope.get("headers") or ():
                    if k.lower() == b"mcp-session-id":
                        sid = v.decode("latin-1")
                        break
                # The FIRST initialize POST has no session id yet — the
                # follow-up requests (initialized notification, GET stream)
                # arrive within a second and do, so association lags birth
                # by one round-trip at most.
                if sid:
                    transport = getattr(self, "_server_instances", {}).get(sid)
                    if transport is not None:
                        setattr(transport, _URL_AGENT_ATTR, agent)
                        # Recency stamp: the sweep must track the CURRENT
                        # transport, not the first one it saw. Claude Code
                        # cycles sessions silently; an abandoned transport
                        # stays warm server-side with its GET key registered,
                        # so "bound + deliverable" can outlive the client's
                        # actual attachment (proven 2026-07-27 18:22: the
                        # shipped seat went deaf 30 min after its one-shot
                        # bind while ⚡ showed green).
                        setattr(transport, _URL_SEEN_ATTR, time.time())
        except Exception:  # noqa: BLE001
            logger.exception("url-identity capture failed; request unaffected")
        return await original(self, scope, receive, send)

    StreamableHTTPSessionManager.handle_request = patched_handle_request  # type: ignore[method-assign]
    _handle_request_patched = True


_ensure_url_identity_patched()


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

def create_server(db_path: Path = DB_PATH, host: str = "0.0.0.0", port: int = 8080) -> FastMCP:
    """Create the MCP Hub server."""
    init_db(db_path)

    # Startup presence reset — a fresh server instance has ZERO live sessions
    # (a restart/redeploy drops every client connection and wipes the in-memory
    # registry). The DB `status` field is persistent, though, and is only
    # flipped to 'offline' by an explicit unregister() — so without this reset
    # every agent that was online before the restart lingers as a stale 🟢
    # 'online' forever, even though nothing is connected. Mark everyone offline
    # at boot; register() flips them back to 'online' as they reconnect. This
    # makes 🟢 mean "connected to THIS instance" rather than "ever registered".
    _boot_conn = _get_db(db_path)
    # Stamp the gap start for agents this restart just disconnected — the
    # redeploy is the single biggest producer of silent coverage gaps, and
    # COALESCE keeps an earlier (deeper) gap start if one is already open.
    _boot_conn.execute(
        "UPDATE agents SET offline_since = COALESCE(offline_since, ?) "
        "WHERE status = 'online'", (time.time(),)
    )
    _boot_conn.execute("UPDATE agents SET status = 'offline'")
    _boot_conn.commit()

    mcp = FastMCP(
        name="mcp-hub",
        host=host,
        port=port,
        instructions=(
            "MCP Hub — inter-agent messaging.\n\n"
            "Three message primitives:\n"
            "- send(from, to, message, priority) — to one specific agent\n"
            "- post(from, channel, message, priority) — to a named channel\n"
            "- broadcast(from, message, priority) — to the whole fleet\n\n"
            "Priority is one of low|normal|urgent. Default is normal. "
            "Use 'low' for FYIs / status updates / EOD recaps that the recipient "
            "doesn't need to act on now — the hub queues these without firing a "
            "channel-push wake. Use 'normal' when you're waiting on the recipient. "
            "Use 'urgent' sparingly — it should mean 'blocking on you' or "
            "'production incident'.\n\n"
            "After register() the hub binds your MCP session for channel-push wake. "
            "Use list_agents() to see who's online — the ⚡ marker indicates a live, "
            "ping-verified wakeable session.\n\n"
            "Discipline — handling auto-surfaced queued items:\n"
            "Stop hooks (per agent's settings.json) auto-pull queued DMs and unseen "
            "broadcasts at every Stop boundary. When queued items surface, evaluate "
            "relevance to current work before context-switching:\n"
            "- Urgent (priority=urgent): always respond.\n"
            "- Related/important to current work: respond inline.\n"
            "- Unrelated low/normal: note in one line ('saw your DM, will follow up'); "
            "continue current work; fold them in at a natural break.\n"
            "Don't deeply context-switch on FYI / low-priority items.\n\n"
            "Discipline — decision asks (operator convention, 2026-07-26):\n"
            "When you need an operator decision, END your turn with this "
            "block — one field per line, bold labels, nothing after it:\n"
            "**DECISION**\n"
            "**ASK:** <what you want, one sentence>\n"
            "**WHY:** <one sentence>\n"
            "**VALUE:** <what it buys, one sentence> [<v>/10]\n"
            "**RISK:** <what it costs if wrong, one sentence> [<r>/10]\n"
            "**NET:** <v minus r, e.g. +5>\n"
            "**TAGS:** <optional, comma-separated: deploy, spend, security, "
            "design, ops>\n"
            "One sentence per field — the operator reads label, then "
            "sentence, at speed; a field that wraps twice has failed. Score "
            "VALUE and RISK separately, honestly, 0-10 each; the hub "
            "computes net = value - risk and the triage queue sorts by it — "
            "there is no single SCORE to assert; write NET yourself for the "
            "pane reader, the hub recomputes and its arithmetic wins. Your "
            "Stop hook ships the card to the hub automatically (hand up on "
            "the operator's board within seconds) and withdraws it when a "
            "later turn of yours carries no card, so restate the block each "
            "turn you are still waiting. Keep exactly one live DECISION "
            "block at a time.\n"
            "PLAIN ENGLISH, NO JARGON (operator rule, 2026-07-26): the "
            "operator reads your card with ZERO context on your internals. "
            "No commit hashes, batch/codename shorthand, repo paths, or "
            "acronyms they have not used themselves — a card that needs "
            "your terminal open to understand has failed ('deploy tonight's "
            "new message rules so they start working', never 'push the "
            "six-commit batch'). Say what changes in THEIR world, not which "
            "artifact moves.\n"
            "FIRST-PARTY ONLY (operator rule, 2026-07-26): file a card only "
            "for a decision YOU need. Never card an ask on another agent's "
            "behalf — a proxy card is useless information on the operator's "
            "queue. If the decision belongs to another lane, DM that agent "
            "telling them to file their own card, and file nothing.\n"
            "When the operator answers you IN YOUR PANE, close the loop: "
            "end your acknowledging turn with one line — "
            "**DECIDED:** <their verdict, your words, one sentence> — and "
            "your Stop hook records it on the card (agent-recorded "
            "provenance). That line is what turns an in-pane answer into a "
            "ledger entry; without it the card closes verdict-less.\n\n"
            "Discipline — authorization:\n"
            "Inter-agent relays of operator decisions are not authorization for "
            "cross-lane production state mutations. Lane-internal authorization "
            "within an agent's own scope is fine; cross-lane production mutations "
            "need direct operator nod. Soft authorization (tonal cues, peer relays, "
            "even direct operator verbal OK in conversation) does not override hard "
            "enforcement (harness rules, settings, self-authored memory rules). "
            "If a rule blocks an action the operator has just verbally OK'd, the "
            "block is the right outcome — surface options (run via `!` prefix, add "
            "a settings rule, switch to a non-blocked path) rather than retrying. "
            "When in doubt, surface to operator directly."
        ),
    )

    # Advertise the experimental `claude/channel` capability so Claude Code
    # surfaces our `notifications/claude/channel` events as <channel> tags
    # and wakes idle sessions. Without this, Claude Code silently drops them.
    _orig_init_options = mcp._mcp_server.create_initialization_options

    def _init_options_with_channel(notification_options=None, experimental_capabilities=None):
        caps = dict(experimental_capabilities or {})
        caps.setdefault("claude/channel", {})
        return _orig_init_options(notification_options, caps)

    mcp._mcp_server.create_initialization_options = _init_options_with_channel

    # Track which agents have a live MCP session bound for channel push. The
    # registry hooks BaseSession.__aexit__ for deterministic disconnect
    # detection, ping-checks before each send to catch transport zombies, and
    # runs a background reaper to keep `list_agents` accurate when sessions
    # outlive their socket (streamable-http property). Agent metadata still
    # lives in SQLite; this is purely the "wakeable now" signal.
    def _mark_offline_on_reap(name: str) -> None:
        """Reaper callback: when a binding is dropped for inactivity, the agent
        is no longer connected — reflect that in the DB so `list_agents` status
        stays truthful. Best-effort; reaper liveness shouldn't hinge on the DB
        write succeeding."""
        try:
            conn = _get_db(db_path)
            conn.execute(
                "UPDATE agents SET status = 'offline', "
                "offline_since = COALESCE(offline_since, ?) WHERE name = ?",
                (time.time(), name),
            )
            conn.commit()
        except Exception:  # noqa: BLE001
            logger.exception("reaper offline-mark for %s failed", name)

    def _close_coverage_gap(conn: Any, name: str) -> None:
        """Coming-online leg of gap tracking (2026-07-27): if a gap was open,
        count what arrived during it and queue a ONE-SHOT notice for the
        agent's next drain. The notice converts a silent gap into a known
        gap — the agent can choose to get_history() instead of trusting a
        queue it had no reason to doubt. Redelivery is NOT needed (DMs queue
        and the broadcast cursor catches up); awareness is the missing half.
        Best-effort: never worth failing a register."""
        try:
            row = conn.execute(
                "SELECT offline_since FROM agents WHERE name = ?", (name,)
            ).fetchone()
            since = row["offline_since"] if row else None
            if not since:
                return
            # from_agent != name: you cannot miss your own traffic. The very
            # first live firing of this notice (2026-07-27, on the seat that
            # shipped it) counted exactly one "missed" message — the author's
            # own deploy broadcast, sent during their own binding gap.
            n = conn.execute(
                "SELECT COUNT(*) AS n FROM messages WHERE ts > ? AND "
                "from_agent != ? AND (to_agent = ? OR channel = ?)",
                (since, name, name, _BROADCAST_CHANNEL),
            ).fetchone()["n"]
            notice = ""
            if n:
                t1 = time.strftime("%H:%M", time.gmtime(since))
                t2 = time.strftime("%H:%M", time.gmtime())
                notice = (
                    f"⚠️ Coverage gap: your binding was down {t1}–{t2} UTC "
                    f"and {n} message(s) arrived in that window. They are in "
                    "your queue/cursor, but anything you reasoned about "
                    "during the gap may be missing context — "
                    "get_history() holds the record."
                )
            conn.execute(
                "UPDATE agents SET offline_since = NULL, gap_notice = ? "
                "WHERE name = ?", (notice, name),
            )
            conn.commit()
        except Exception:  # noqa: BLE001
            logger.exception("coverage-gap close for %s failed", name)

    # liveness_probe is a late-binding lambda: `_can_deliver_push` is defined
    # further down in this scope, but the reaper only invokes the probe long
    # after create_server() has finished, so the name resolves fine at call
    # time. The reaper uses it to spare still-deliverable idle bindings from
    # the activity-timeout drop (a `--channels` session's live connection is
    # its own heartbeat — no daemon needed).
    def _on_wake_dead(name: str) -> None:
        """Wake-ack callback: pushed wakes produced no ack before the strike
        limit. The binding is already dropped and on_reap marked them
        offline; queue guidance for their next Stop-hook pull.

        The guidance names BOTH classes behind this one signal instead of
        prescribing a relaunch for either (2026-07-27: 11 drops in a day —
        most were healthy mid-turn sessions, one was a genuinely push-deaf
        seat, and the old wording spent an operator relaunch on a healthy
        session while telling the deaf one nothing it could verify). Only
        the agent can see which class it's in — whether pushes render live
        in its context — so the message hands it the discriminator, not a
        verdict."""
        try:
            conn = _get_db(db_path)
            conn.execute(
                "INSERT INTO messages (ts, from_agent, to_agent, body, priority) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    time.time(),
                    "hub",
                    name,
                    (
                        "⚠️ Wake-ack check: the hub pushed wakes to your "
                        "session and saw no message-drain before the strike "
                        "limit, so your binding was dropped (you'll show "
                        "offline). This one signal has TWO causes, and only "
                        "you can see which applies — check whether hub "
                        "messages have been arriving LIVE in your context "
                        "(as <channel> tags mid-turn or idle wakes): "
                        "(1) they have → FALSE ALARM: you were simply deep "
                        "in a turn without calling hub tools; register() "
                        "and carry on, nothing is broken. "
                        "(2) nothing has arrived live all session — "
                        "everything reaches you only via Stop-hook pulls "
                        "like this one → your receive stream is genuinely "
                        "dead: register() rebinds but cannot revive it; "
                        "RELAUNCH your Claude session (--continue keeps "
                        "context; squad hosts: squad restart <you>). "
                        "If unsure: register(), then watch the next push — "
                        "a healthy stream shows it to you live."
                    ),
                    "normal",
                ),
            )
            conn.commit()
        except Exception:  # noqa: BLE001
            logger.exception("wake-dead guidance DM for %s failed", name)

    registry = SessionRegistry(
        on_reap=_mark_offline_on_reap,
        on_wake_dead=_on_wake_dead,
        liveness_probe=lambda session: _can_deliver_push(session),
    )
    # Exposed for main() so it can spawn the reaper alongside the server.
    mcp._hub_registry = registry  # type: ignore[attr-defined]

    def _drop_context_probe(name: str, session: Any) -> str:
        """Snapshot the external discriminators a wake-ack drop needs to
        arrive pre-classified (2026-07-27 four-class forensics, by hand,
        once): was the recipient idle, and is their transport still
        push-deliverable right now. Called under the registry lock — must
        not call registry methods (act_ago/bind_src come from the registry
        itself); DB and transport-manager access take no registry lock."""
        parts: list[str] = []
        try:
            row = _get_db(db_path).execute(
                "SELECT is_idle FROM agents WHERE name = ?", (name,)
            ).fetchone()
            parts.append(f"idle={row['is_idle'] if row else '?'}")
        except Exception:  # noqa: BLE001
            parts.append("idle=?")
        parts.append(
            f"deliverable={_can_deliver_push(session) if session else '?'}"
        )
        return " ".join(parts)

    registry.context_probe = _drop_context_probe

    async def _url_rebind_sweep() -> None:
        """Bind URL-identified sessions without an agent turn — the
        auto-rebind half of making deploys non-events.

        Every ~10s: for each live streamable-http transport whose requests
        carried ?agent=<name>, find its ServerSession (write-stream identity,
        same traversal as _can_deliver_push) and bind it when ALL of:
          * the session passes the interactive-client gate — the stop-hook /
            heartbeat ephemeral clients connect with the same URL, and
            binding one would clobber the real wake target (the exact bug
            bind=False exists to prevent); the cli also strips the param,
            so this gate is the second layer of a two-layer defence;
          * the name exists in the agents DB (no phantom bindings — same
            discipline as touch_session);
          * the agent is currently UNBOUND, or bound to a session that is no
            longer push-deliverable. A live deliverable binding is never
            fought — register() and touch_session own that path.
        Binding marks the agent online and closes its coverage gap, so the
        fleet returns ⚡ from a deploy with zero agent involvement."""
        # DISARMED 2026-07-27 ~19:00 pending re-arm via MCP_HUB_URL_REBIND=1.
        # The stamp was removed from every config, but every LONG-RUNNING
        # client on dev-vm-1 loaded the poisoned URL at startup and keeps
        # announcing it from process memory on every reconnect — and a
        # reconnecting session is UNOWNED for the seconds before its agent
        # re-registers, which is exactly the window the cross-identity guard
        # cannot cover (post-deploy, pm's tagged-but-unowned transport was
        # bound under the maintainer's name and became the second sink).
        # With zero legitimately-stamped seats, ANY ?agent= is stale poison;
        # the sweep stays dark until stamps are re-issued per-seat with
        # relaunches coordinated, then re-armed explicitly via the env var.
        if os.environ.get("MCP_HUB_URL_REBIND") != "1":
            logger.info("url-rebind sweep DISARMED (MCP_HUB_URL_REBIND != 1)")
            return
        while True:
            try:
                await anyio.sleep(10.0)
                try:
                    manager = mcp.session_manager
                except RuntimeError:
                    continue
                instances = getattr(manager, "_server_instances", None)
                if not instances:
                    continue
                # None-guard on BOTH sides of the identity match: any session
                # and any transport lacking _write_stream would otherwise meet
                # at id(None) and bind a name to an ARBITRARY session —
                # cross-agent wake delivery, the worst failure this feature
                # can produce (2026-07-27 18:22: a DM to this repo's
                # maintainer surfaced in dreamteam's context).
                # _can_deliver_push has carried this exact guard all along.
                sessions_by_stream = {
                    id(ws): s
                    for s in live_server_sessions()
                    if (ws := getattr(s, "_write_stream", None)) is not None
                }
                conn = _get_db(db_path)
                # Group candidates per name and pick the BEST — newest
                # recency stamp, GET-holders (live wake channels) always
                # outranking transports without one. Binding once and
                # standing pat re-created the zombie class this feature
                # was meant to retire: the client cycles its wake channel
                # silently, the abandoned transport keeps its GET key, and
                # the one-shot binding stays "deliverable" while routing
                # pushes into a stream nobody reads (2026-07-27 18:22, the
                # shipped seat itself). URL identity is therefore
                # CONTINUOUSLY authoritative: whenever the newest
                # GET-holding transport differs from the bound session,
                # rebind — an idle agent's client refreshes its wake
                # channel without ever making a tool call, and the sweep
                # must follow it.
                best: dict[str, tuple[tuple[int, float], Any]] = {}
                for transport in list(instances.values()):
                    name = getattr(transport, _URL_AGENT_ATTR, "")
                    if not name:
                        continue
                    t_ws = getattr(transport, "_write_stream", None)
                    session = (
                        sessions_by_stream.get(id(t_ws))
                        if t_ws is not None else None
                    )
                    if session is None or not is_interactive_client(session):
                        continue
                    # Hard invariant, independent of any matching bug: a
                    # session that already owns a DIFFERENT identity is never
                    # bound to this name. Whatever goes wrong upstream, a
                    # wake for X must not land in Y's context.
                    owners = registry.names_for_session(session)
                    if owners and name not in owners:
                        logger.warning(
                            "url-rebind: REFUSING %s -> session owned by %s "
                            "(cross-identity guard)", name, sorted(owners),
                        )
                        continue
                    has_get = GET_STREAM_KEY in getattr(
                        transport, "_request_streams", {}
                    )
                    seen = getattr(transport, _URL_SEEN_ATTR, 0.0)
                    rank = (1 if has_get else 0, seen)
                    if name not in best or rank > best[name][0]:
                        best[name] = (rank, session)
                for name, (_rank, session) in best.items():
                    current = registry.get(name)
                    if current is session:
                        continue
                    row = conn.execute(
                        "SELECT name FROM agents WHERE name = ?", (name,)
                    ).fetchone()
                    if row is None:
                        continue
                    was_bound = current is not None
                    registry.bind(name, session, source="url")
                    conn.execute(
                        "UPDATE agents SET status = 'online', last_seen = ? "
                        "WHERE name = ?", (time.time(), name),
                    )
                    conn.commit()
                    _close_coverage_gap(conn, name)
                    logger.info(
                        "url-rebind: %s %s to current transport identity "
                        "(no agent turn)",
                        "re-pointed" if was_bound else "bound", name,
                    )
            except anyio.get_cancelled_exc_class():
                raise
            except Exception:  # noqa: BLE001
                logger.exception("url-rebind sweep iteration failed")

    # Exposed for main() to run alongside the reaper.
    mcp._hub_url_rebind_sweep = _url_rebind_sweep  # type: ignore[attr-defined]

    def _attribution(ctx: Context | None, from_agent: str) -> tuple[str, str]:
        """Verify-when-bound (item 34, fo's accidental impersonation): the
        transport's own binding is the one identity signal that is not
        caller-asserted, so use it where it exists.

        Returns (grade, error). grade is 'session-verified' when the calling
        session is bound to exactly the asserted identity, 'asserted' when
        the caller is unbound/ephemeral (stop-hook, daemons — unchanged
        trust, by design). error is non-empty only when the session OWNS a
        different identity than it asserts — the one case that is provably a
        mis-attribution at the tool boundary."""
        if ctx is None:
            return "asserted", ""
        try:
            names = registry.names_for_session(ctx.session)
        except Exception:  # noqa: BLE001
            return "asserted", ""
        if not names:
            return "asserted", ""
        if from_agent in names:
            return "session-verified", ""
        return "asserted", (
            f"REFUSED: this session is bound to "
            f"{' / '.join(sorted(names))} but asserted "
            f"from_agent='{from_agent}' — identity mismatch. If the swap was "
            "accidental (inverted from/to arguments), re-issue with the "
            "correct from_agent; the record was not written."
        )

    # Exposed for tests — call_tool can't inject a Context, so the gate's
    # logic is verified directly against seeded registry bindings.
    mcp._hub_attribution = _attribution  # type: ignore[attr-defined]

    # Plain HTTP health/version probe: `curl http://<hub>/health` returns the
    # running git commit so the deployed version is verifiable without
    # guessing (the hub otherwise speaks only MCP at /mcp). Registered on the
    # streamable-http Starlette app via FastMCP's custom_route.
    from starlette.requests import Request
    from starlette.responses import JSONResponse

    @mcp.custom_route("/health", methods=["GET"])
    async def health(_request: Request) -> JSONResponse:
        try:
            from . import __version__ as pkg_version
        except Exception:  # noqa: BLE001
            pkg_version = "?"
        return JSONResponse(
            {
                "status": "ok",
                "service": "mcp-hub",
                "version": pkg_version,
                "commit": _resolve_commit(),
                "agents_bound": len(registry.names()),
            }
        )

    def touch_session(name: str, ctx: Context | None) -> None:
        """Auto-bind the agent's session if a Context is available.

        Called from every tool that identifies the calling agent (by `from_agent`,
        `agent_name`, etc.). The point: any tool call from an agent's main
        session refreshes their registry binding. Drift across redeploys is
        invisible — agents come back ⚡ on their next tool call without
        needing an explicit register(), without operator nudging.

        Only binds names that exist in the DB. Stops typos and made-up names
        from creating phantom bindings. The DB row is the source of truth
        for "this is a real agent"; the registry is the operationally-live
        slice of that truth.

        Also clears `is_idle` — a tool call from the agent's main session
        means they're in a turn, not idle. Guard with `is_idle = 1` so the
        UPDATE only fires when state actually changes (negligible perf, but
        cleaner audit trail).

        Diagnostic: logs the client's clientInfo + experimental capabilities
        on every bind. Used to find a reliable signal for distinguishing
        long-lived Claude Code interactive sessions from ephemeral utility
        clients (the Stop hook's streamablehttp_client) so we can later gate
        the bind on it.
        """
        if ctx is None or not name:
            return
        # THE GATE (wired 2026-07-18 on capabilities; repaired 2026-07-26 to
        # the clientInfo discriminator — backlog 23c(b)). Intent: only a real
        # interactive Claude Code session may own the bind, so an ephemeral
        # utility client (memory-export's twin-notify, any CLI calling
        # send/post/broadcast as an agent) can't re-point the agent's wake
        # target at a session that dies when the process exits.
        #
        # History: the original is_channel_capable gate checked a capability
        # clients never declare, so it rejected 100% of sessions for a week —
        # it "worked" by blocking ephemeral clients along with everyone else,
        # and tool-call drift self-heal was silently dead the whole time
        # (register() carried every binding in the fleet). The repair also
        # restores the ack path: a bound agent's ordinary post-wake tool call
        # now clears its wake expectation, so the compact "already delivered
        # live" annotation fires only when TRUE (the 23c double-surface
        # mechanism).
        #
        # A rejected session also must NOT clear is_idle: a CLI call is not
        # the agent's interactive turn. Rejections are logged with the
        # observed clientInfo so a future client rename shows up same-day in
        # the logs instead of as silent fleet-wide bind failure.
        if not is_interactive_client(ctx.session):
            _log_bind_diagnostic("touch_session-skipped", name, ctx.session)
            return
        conn = _get_db(db_path)
        row = conn.execute(
            "SELECT 1 FROM agents WHERE name = ?", (name,)
        ).fetchone()
        if row is None:
            return
        _log_bind_diagnostic("touch_session", name, ctx.session)
        registry.bind(name, ctx.session)
        # Clear is_idle — agent is in an interactive turn now. Conditional
        # on is_idle=1 to skip the no-op UPDATE for the steady-state path.
        result = conn.execute(
            "UPDATE agents SET is_idle = 0 WHERE name = ? AND is_idle = 1",
            (name,),
        )
        if result.rowcount > 0:
            conn.commit()

    def _can_deliver_push(session: Any) -> bool:
        """True if `session`'s streamable-http transport is in the manager's
        active set AND has the GET /mcp listener (`GET_STREAM_KEY`) currently
        registered. False means the notification would be silently dropped.

        Why this gate exists: `ServerSession.send_notification` writes to the
        session's internal write_stream, which always succeeds (no exception)
        regardless of client state. The streamable-http transport's
        message_router then routes server-initiated notifications to
        `GET_STREAM_KEY`. If the client hasn't opened a GET /mcp stream (or
        has closed it on /compact / network drop / disconnect), the router
        logs "Request stream _GET_stream not found" at DEBUG and drops the
        message. We run with event_store=None, so no replay buffer either.

        Without this gate, push_channel returns True for a silent black-hole
        — the hub reports `woke N/M` lies and `⚡` no longer means wakeable.
        Reproduced empirically against the SDK in
        tests/test_streamable_silent_drop.py.

        Returns True (pass-through) if we can't introspect — e.g. the
        session_manager isn't initialized (stdio/test mode) or the bound
        session isn't a streamable-http one. In those cases the previous
        behavior is preserved.
        """
        try:
            manager = mcp.session_manager
        except RuntimeError:
            return True
        instances = getattr(manager, "_server_instances", None)
        if not instances:
            return True
        session_write = getattr(session, "_write_stream", None)
        if session_write is None:
            return True
        for transport in instances.values():
            if getattr(transport, "_write_stream", None) is session_write:
                return GET_STREAM_KEY in getattr(transport, "_request_streams", {})
        # Session is bound but its transport is no longer in the manager's
        # active set — the underlying session_id has been DELETEd/crashed.
        return False

    def _stamp_pushed(conn: Any, ids: list[int], agent: str) -> None:
        """Record the recipient's CURRENT binding generation on messages we
        just pushed live to them.

        Read back by get_messages(compact=True): if the token still matches at
        pull time, the agent is holding the same stream that received the push,
        so the Stop hook can summarise instead of reprinting the whole body.
        A mismatch (rebind / unbind / hub restart) reprints in full — that's
        the case where the push may have died in a stale stream, and silently
        summarising it away would recreate the message-loss bug of PR #8.

        Fail-soft: this is a display optimisation, never worth failing a send.
        """
        gen = registry.generation(agent)
        if not gen or not ids:
            return
        try:
            placeholders = ",".join("?" * len(ids))
            conn.execute(
                f"UPDATE messages SET pushed_gen = ? WHERE id IN ({placeholders})",
                [gen, *ids],
            )
            conn.commit()
        except Exception:  # noqa: BLE001
            logger.debug("pushed_gen stamp failed for %s", agent, exc_info=True)

    async def push_channel(agent: str, content: str, meta: dict[str, str]) -> _PushOutcome:
        """Push a channel notification to `agent` via the live session registry.

        Returns a `_PushOutcome(delivered, primary)`:
        - `delivered` — reached at least one live session. Drives the "woke"
          reporting and the wake-ack expectation. False means the recipient is
          offline / unbound / every session was a zombie. Either way the caller
          has already persisted the message in SQLite, so False is not message
          loss — the recipient picks it up on next register() / get_messages().
        - `primary` — the PRIMARY session specifically got the live push. ONLY
          this gates the compact-render generation stamp (`_stamp_pushed`),
          because that token is the primary's: stamping it when an EXTRA (not
          the primary) delivered would let the primary conversation summarise a
          message it never received — the "push success ≠ seen" mistake in a
          new disguise.
        """
        # Fan the wake out to EVERY live session bound to this name. One
        # derived identity can carry more than one conversation (e.g. a tmux
        # session AND a Co-work session in the same repo on the same host both
        # derive the same name) — before multi-session support, the second
        # register() evicted the first and only one conversation ever woke.
        #
        # Delivery per session gates on the same silent-drop check as a 1:1
        # push: `_can_deliver_push` is False when a binding exists but its
        # transport has no GET /mcp listener (closed on /compact, cycled out,
        # never reopened after a redeploy), where the send would succeed into
        # a black hole and the hub would lie in its `woke` count.
        #
        # Primary vs extras differ ONLY in lifecycle on failure: a dead extra
        # is pruned right here (extras get opportunistic verification only),
        # while the primary is left untouched for the reaper / wake-ack paths
        # that own its lifecycle — dropping it on a transient push failure is
        # the exact mistake the mark-read-on-push incident warned against.
        sessions = registry.sessions(agent)
        if not sessions:
            return _PushOutcome(False, False)  # never bound
        notification = _ChannelNotification(params={"content": content, "meta": meta})
        primary, extras = sessions[0], sessions[1:]

        primary_delivered = False
        if _can_deliver_push(primary):
            if await registry.push(agent, notification):
                primary_delivered = True
        else:
            logger.info(
                "push %s: primary gated — no GET /mcp listener "
                "(would be silent drop)", agent,
            )
        delivered = primary_delivered
        for extra in extras:
            if not _can_deliver_push(extra):
                registry.unbind_session(agent, extra)
                continue
            if await registry.push_to_session(agent, extra, notification):
                delivered = True
            else:
                registry.unbind_session(agent, extra)

        if delivered:
            # Arm the wake-ack expectation: a delivered wake must produce
            # SOME agent activity (bind or message drain) within the ack
            # window, else the stream is presumed dead behind the live
            # binding — the one state transport introspection can't see.
            registry.expect_wake_ack(agent)
        return _PushOutcome(delivered, primary_delivered)

    # -- Presence --

    @mcp.tool()
    def register(
        name: str, project: str = "", bio: str = "", meta: str = "{}",
        team: str = "", ctx: Context | None = None,
    ) -> str:
        """Register this agent session with the hub.

        Call this when your session starts. Sets you as 'online' and binds
        your MCP session so the hub can push messages to you via the
        `claude/channel` capability — if your Claude Code was launched with
        `--channels` (or `--dangerously-load-development-channels`), incoming
        messages will surface in your context without polling.

        Args:
            name: Your agent name (e.g. 'dreamteam-lead', 'reliable-ai-dev').
            project: Project you're working on (e.g. 'dreamteam', 'mcp-hub').
            team: The squad you belong to (e.g. 'dreamteam'). Broadcasts are
                  scoped to a team by default, so this decides who hears you
                  and whose chatter reaches you. NOT the project — one squad
                  routinely spans several projects and even several orgs.
                  Empty preserves any stored value and means "no declared
                  team", which behaves exactly as before: fleet-wide.
            bio: Short description of your role/skills so other agents know what you do.
            meta: Optional JSON metadata about this agent.
        """
        now = time.time()
        conn = _get_db(db_path)

        # NOTE: no one-agent-per-project dedup here, deliberately. Multiple
        # clones of the same repo register distinct derived names
        # (<repo>-<hostname>) under one shared project — that's how they
        # discover each other. The old dedup silently remapped any new name
        # onto an existing online agent for the project, collapsing clones
        # into a single identity (and hijacking its wake binding).

        # For first-time registrations, set the broadcast cursor to the
        # current max so they start "from now" instead of getting firehosed
        # with historical broadcasts from before they existed. Re-registers
        # of existing agents preserve their cursor (no-op in the UPDATE
        # branch — last_broadcast_seen_id is omitted from the SET list).
        max_broadcast_id = conn.execute(
            "SELECT COALESCE(MAX(id), 0) AS m FROM messages WHERE channel = ?",
            (_BROADCAST_CHANNEL,),
        ).fetchone()["m"]

        # team follows the same rule as bio: an empty value on re-register
        # PRESERVES what is stored, so an agent that hasn't learned to send one
        # yet cannot silently drop itself out of its squad on a reconnect.
        #
        # The cost of that rule, stated plainly because it is a real gap: there
        # is currently NO way to clear a team once set. Empty means "no opinion",
        # so it cannot also mean "remove me". Un-teaming needs an explicit tool
        # (or a sentinel value) and neither exists yet — a first team assignment
        # is effectively one-way until one does. Deliberately not built here:
        # the client side that would set a team in the first place isn't built
        # either, so nothing on the hub can reach this state yet.
        conn.execute(
            """INSERT INTO agents (name, project, bio, status, registered,
                                   last_seen, meta, last_broadcast_seen_id, team)
               VALUES (?, ?, ?, 'online', ?, ?, ?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET
                   project=excluded.project,
                   bio=CASE WHEN excluded.bio = '' THEN agents.bio ELSE excluded.bio END,
                   team=CASE WHEN excluded.team = '' THEN agents.team ELSE excluded.team END,
                   status='online',
                   last_seen=excluded.last_seen,
                   meta=excluded.meta""",
            (name, project, bio, now, now, meta, max_broadcast_id, team),
        )
        conn.commit()
        _close_coverage_gap(conn, name)

        # Bind the current MCP session so we can push channel notifications.
        # Re-registering from a new session replaces the old binding atomically.
        #
        # DELIBERATELY UNGATED — do not add is_interactive_client (or any
        # client-declared predicate) here. register() is the one binding path
        # that must never depend on a client-asserted string: if
        # clientInfo.name ever changes, a gated touch_session degrades to
        # today's survivable state, but a gated register() would silently
        # unbind the entire fleet. That is parked #17, refused three times
        # now; the exposure (an ephemeral/headless session calling register
        # as an agent) is accepted and kept honest by wake-ack strikes.
        if ctx is not None:
            _log_bind_diagnostic("register", name, ctx.session)
            registry.bind(name, ctx.session)

        # Count unread messages for this agent
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM messages WHERE to_agent = ? AND read = 0",
            (name,),
        ).fetchone()
        unread = row["cnt"] if row else 0

        result = f"Registered as '{name}'"
        if project:
            result += f" (project: {project})"

        # Twin pairing: clones of one repo derive the same project, so
        # "who else is this repo on another machine?" is a pure query.
        # (This is the same lookup the old one-agent-per-project dedup ran —
        # now it introduces the clones to each other instead of collapsing
        # them into one identity.)
        if project:
            twins = conn.execute(
                "SELECT name FROM agents WHERE project = ? AND name != ? "
                "AND status = 'online' ORDER BY name",
                (project, name),
            ).fetchall()
            if twins:
                result += (
                    "\n👥 Paired clones of this project online: "
                    + ", ".join(t["name"] for t in twins)
                    + " — same repo on another machine; DM them to coordinate."
                )

        if unread > 0:
            result += f"\n📬 You have {unread} unread message(s). Call get_messages() to read them."
        return result

    @mcp.tool()
    def update_bio(name: str, bio: str, ctx: Context | None = None) -> str:
        """Update your bio so other agents know what you do.

        Args:
            name: Your agent name.
            bio: Short description of your role, skills, or current focus.
        """
        conn = _get_db(db_path)
        row = conn.execute("SELECT 1 FROM agents WHERE name = ?", (name,)).fetchone()
        if not row:
            return f"Agent '{name}' not found. Register first with register()."
        conn.execute("UPDATE agents SET bio = ? WHERE name = ?", (bio, name))
        conn.commit()
        touch_session(name, ctx)
        return f"Bio updated for '{name}'."

    @mcp.tool()
    def unregister(name: str, ctx: Context | None = None) -> str:
        """Mark an agent as offline.

        Args:
            name: The agent name to take offline.
        """
        # A bound session may only take ITSELF offline (verify-when-bound —
        # unregistering someone else is the destructive form of fo's
        # accidental-inversion class). Ephemeral/unbound callers unchanged.
        _grade, attr_err = _attribution(ctx, name)
        if attr_err:
            return attr_err
        conn = _get_db(db_path)
        # Deliberate departure: no coverage gap to report on return.
        conn.execute(
            "UPDATE agents SET status = 'offline', offline_since = NULL, "
            "gap_notice = '' WHERE name = ?", (name,))
        conn.commit()
        return f"'{name}' is now offline."

    @mcp.tool()
    def list_agents(include_offline: bool = False) -> str:
        """List all registered agents.

        Args:
            include_offline: Include agents that have disconnected.
        """
        conn = _get_db(db_path)
        if include_offline:
            rows = conn.execute("SELECT * FROM agents ORDER BY last_seen DESC").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM agents WHERE status = 'online' ORDER BY last_seen DESC"
            ).fetchall()

        if not rows:
            return "No agents registered."

        lines = []
        for r in rows:
            status = "🟢" if r["status"] == "online" else "⚫"
            # ⚡ marks agents we can wake RIGHT NOW — not merely "has a registry
            # binding". A bound session whose GET /mcp listener is gone (closed
            # on /compact, cycled out, or never reopened after a hub redeploy)
            # would silently drop a push, so it is NOT wakeable even though it's
            # bound. `_can_deliver_push` is the same gate push_channel uses, so
            # ⚡ now means exactly "a push would land". A bound-but-undeliverable
            # agent shows 🟢 without ⚡ — the visible "needs relaunch" signal.
            # ⚡ means "a wake would land on ≥1 live session RIGHT NOW". With
            # multi-session, more than one conversation can share this derived
            # name (e.g. tmux + Co-work) and a wake fans out to all of them —
            # so we probe EVERY session and show the count of genuinely-
            # deliverable ones (⚡×2). Counting raw membership instead would
            # let a silently-dead extra (pruned only at push time) inflate the
            # number into a wakeability lie — the exact thing ⚡ exists to
            # prevent. Zero deliverable → no ⚡ (bound-but-unreachable = the
            # visible "needs relaunch" signal), unchanged for single sessions.
            wakeable = sum(
                1 for s in registry.sessions(r["name"]) if _can_deliver_push(s)
            )
            if wakeable > 1:
                wake = f" ⚡×{wakeable}"
            elif wakeable == 1:
                wake = " ⚡"
            else:
                wake = ""
            # 💤 marks agents currently idle (Stop hook flipped them at last
            # turn end, no tool call has cleared it since). Combined with
            # ⚡, this is the state where a low-prio DM fires a live wake
            # via Case 1. No decay timer — the binding itself is the
            # liveness gate; if the agent's process is gone, the binding
            # gets reaped and ⚡ disappears.
            idle = " 💤" if r["is_idle"] else ""
            line = f"{status} **{r['name']}**{wake}{idle}"
            if r["project"]:
                line += f" ({r['project']})"
            if r["bio"]:
                line += f" — {r['bio']}"
            lines.append(line)
        return "\n".join(lines)

    # -- Direct messaging --

    @mcp.tool()
    async def send(
        from_agent: str, to: str, message: str, priority: str = "normal",
        ctx: Context | None = None,
    ) -> str:
        """Send a direct message to another agent.

        Priority controls whether the recipient is woken from idle:

        - "normal" (default): wake on receipt + persist to inbox.
        - "low": queue-only when the recipient is in a turn (don't interrupt
          focused work). Wake when the recipient is idle (Case 1 — soft asks
          should still reach idle agents without operator-in-the-loop).
          Wake delivery is drain-batched: ALL queued unread DMs surface in
          one channel event so a flurry of low-prio sends doesn't wake the
          recipient repeatedly.
        - "urgent": wake + persist + flag as urgent in the rendered <channel>
          tag's meta so the recipient can visually triage. Use sparingly —
          urgent should mean "blocking on you" or "production incident".

        Args:
            from_agent: Your agent name (must be registered).
            to: Target agent name.
            message: The message body.
            priority: One of "low" | "normal" | "urgent". Defaults to "normal".
        """
        if priority not in _VALID_PRIORITIES:
            return (
                f"Invalid priority '{priority}'. "
                f"Use one of: {sorted(_VALID_PRIORITIES)}."
            )

        now = time.time()
        conn = _get_db(db_path)

        # Verify-when-bound BEFORE touch_session: an inverted from/to call
        # doesn't just mis-write a record — the touch below would also bind
        # the caller's session to the ASSERTED name, hijacking the named
        # agent's wake target (fo's 2026-07-27 specimen did exactly this).
        grade, attr_err = _attribution(ctx, from_agent)
        if attr_err:
            return attr_err

        # Auto-bind sender's session — any tool call refreshes the binding
        # so drift across redeploys self-heals without explicit register().
        touch_session(from_agent, ctx)

        # Update sender's last_seen
        conn.execute(
            "UPDATE agents SET last_seen = ? WHERE name = ?", (now, from_agent)
        )
        cursor = conn.execute(
            "INSERT INTO messages (ts, from_agent, to_agent, body, priority, "
            "attribution) VALUES (?, ?, ?, ?, ?, ?)",
            (now, from_agent, to, message, priority, grade),
        )
        message_id = cursor.lastrowid
        conn.commit()

        # Low-priority — Case 1 path. Check recipient's idle state.
        # is_idle=1 means the recipient's last Stop hook fired (turn-end
        # transition). If the recipient is also bound, the wake will land;
        # if not bound, push() will return False and we fall back to the
        # queued path below. Either way it's correct — the binding check
        # is the real liveness gate.
        if priority in _NO_WAKE_PRIORITIES:
            recipient_row = conn.execute(
                "SELECT is_idle FROM agents WHERE name = ?",
                (to,),
            ).fetchone()
            recipient_is_idle = bool(
                recipient_row and recipient_row["is_idle"]
            )
            if not recipient_is_idle:
                return (
                    f"Message queued for '{to}' (priority={priority}; no wake "
                    f"— recipient is in a turn or not registered)."
                )

            # Drain batch: pull ALL unread DMs for the recipient (including
            # the one we just inserted), deliver in one channel event, then
            # mark them all read in one commit. Avoids wake-storming when
            # multiple low-prio sends land in quick succession against an
            # idle recipient.
            unread = conn.execute(
                """SELECT id, ts, from_agent, body, priority FROM messages
                   WHERE to_agent = ? AND read = 0 ORDER BY ts ASC""",
                (to,),
            ).fetchall()
            if not unread:  # defensive — should always include our insert
                unread = [{"id": message_id, "ts": now, "from_agent": from_agent,
                           "body": message, "priority": priority}]

            content_lines = []
            for r in unread:
                ts = time.strftime("%H:%M:%S", time.localtime(r["ts"]))
                prio = r["priority"]
                prio_tag = f" [{prio}]" if prio != "normal" else ""
                content_lines.append(
                    f"[{ts}] DM from {r['from_agent']}{prio_tag}: "
                    f"{_clip_push(r['body'])}"
                )
            content = "\n".join(content_lines)

            outcome = await push_channel(
                agent=to,
                content=content,
                meta={
                    "from_agent": from_agent,
                    "kind": "dm",
                    "priority": "low",
                    "drain_batch": "true" if len(unread) > 1 else "false",
                },
            )

            if outcome.delivered:
                # Do NOT mark the batch read on push. push_channel returning
                # True only means the notification was written to the bound
                # stream — not that the recipient surfaced it (a stale/
                # non-surfacing stream still reports deliverable, which silently
                # destroyed messages). The inbox is the source of truth;
                # get_messages marks read only when the recipient genuinely
                # pulls. We still clear is_idle (recipient is taking a turn) —
                # which also prevents a re-push storm: subsequent low-prio sends
                # see not-idle and queue instead of re-pushing the batch.
                conn.execute(
                    "UPDATE agents SET is_idle = 0 WHERE name = ?", (to,)
                )
                conn.commit()
                # Stamp ONLY if the primary got it — the generation token is the
                # primary's stream. An extra-only delivery must fall through to
                # a full reprint (fail-safe), never a summary.
                if outcome.primary:
                    _stamp_pushed(conn, [r["id"] for r in unread], to)
                return (
                    f"Message sent to '{to}' (priority={priority}; idle wake "
                    f"fired, drain batch of {len(unread)} item(s))."
                )
            return (
                f"Message queued for '{to}' (priority={priority}; idle-wake "
                f"push failed, will surface via Stop-hook auto-pull)."
            )

        outcome = await push_channel(
            agent=to,
            content=f"DM from {from_agent}: {_clip_push(message)}",
            # `source` is reserved by Claude Code's channel layer (it's the
            # channel server's name, "hub"). Use `from_agent` to avoid a
            # duplicate `source=` attribute on the rendered <channel> tag.
            meta={"from_agent": from_agent, "kind": "dm", "priority": priority},
        )

        # Stamp only on PRIMARY delivery (the token is the primary's stream) —
        # an extra-only delivery fails safe to a full reprint.
        if outcome.primary:
            _stamp_pushed(conn, [message_id], to)

        # Do NOT mark the message read on push success. push_channel returning
        # True only means the notification was written to the bound stream — NOT
        # that the recipient actually surfaced it. A stale or non-surfacing
        # stream still reports deliverable, so marking read here destroyed
        # messages silently (recipient never saw it, yet it vanished from the
        # inbox). The inbox is the source of truth: the row stays unread until
        # the recipient genuinely pulls it via get_messages (Stop-hook auto-pull
        # or explicit). Worst case, a live-surfaced push is seen once more on the
        # next inbox pull — a harmless duplicate, vs. the silent loss this fixes.

        return (
            f"Message sent to '{to}' (priority={priority})."
            if outcome.delivered
            else (
                f"Message sent to '{to}' (priority={priority}; recipient "
                f"offline — will see on next register/get_messages)."
            )
        ) + _verbosity_advisory(message)

    # -- Broadcast --

    @mcp.tool()
    async def broadcast(
        from_agent: str, message: str, priority: str = "normal",
        scope: str = "team", ctx: Context | None = None,
    ) -> str:
        """Post a broadcast every agent will see.

        Broadcasts are global — they hit every connected agent regardless
        of which channels they're paying attention to. Use this when the
        message is for the whole fleet ("hub redeploying in 5 min";
        "found a bug in shared infra"; "EOD"). For topical conversation
        scoped to a subset of activity, use `post` to a named channel
        instead. For a single recipient, use `send`.

        Priority controls whether currently-connected agents are woken
        from idle on receipt:

        - "normal" (default): wake every connected agent. Use for things
          everyone should see now.
        - "low": persist to the broadcast feed only; do NOT wake anyone.
          Use for EOD recaps, status updates, FYIs — anything that doesn't
          need immediate attention. Agents pick it up via `get_broadcasts`
          when they next look. Strongly preferred for informational
          broadcasts to avoid distracting focused work.
        - "urgent": wake every connected agent with priority="urgent"
          surfaced in the rendered tag's meta so receivers can visually
          triage. Use sparingly — urgent should mean "everyone needs to
          stop what they're doing."

        Args:
            from_agent: Your agent name.
            message: The message body.
            priority: One of "low" | "normal" | "urgent". Defaults to "normal".
            scope: "team" (default) reaches only agents in your team; "fleet"
                  reaches everyone. Default changed 2026-07-27: an untargeted
                  fleet-wide default meant one squad's multi-turn investigation
                  woke every agent on the hub, and three copies of an
                  uninvolved agent answered into a lane whose context they did
                  not hold. A sender with NO declared team still reaches
                  everyone — a group we cannot name is not one we may silently
                  exclude people from.
        """
        if priority not in _VALID_PRIORITIES:
            return (
                f"Invalid priority '{priority}'. "
                f"Use one of: {sorted(_VALID_PRIORITIES)}."
            )
        if scope not in ("team", "fleet"):
            return f"Invalid scope '{scope}'. Use 'team' or 'fleet'."

        now = time.time()
        conn = _get_db(db_path)

        # Verify-when-bound before the touch (see send() for why the order
        # matters — a mismatched assert must not rebind the named agent).
        grade, attr_err = _attribution(ctx, from_agent)
        if attr_err:
            return attr_err

        # The audience is resolved HERE and stored on the row, so it can never
        # move later. An unknown sender, or one with no team, gets '' = fleet.
        # Sits below the attribution gate deliberately: a refused caller must
        # not reach the DB at all. It is a read, so it cannot rebind — the gate
        # only has to precede touch_session, which it still does.
        sender = conn.execute(
            "SELECT team FROM agents WHERE name = ?", (from_agent,)
        ).fetchone()
        sender_team = (sender["team"] if sender else "") or ""
        audience = sender_team if scope == "team" else ""

        # Auto-bind sender's session for drift self-heal.
        touch_session(from_agent, ctx)

        conn.execute(
            "UPDATE agents SET last_seen = ? WHERE name = ?", (now, from_agent)
        )
        cursor = conn.execute(
            "INSERT INTO messages (ts, from_agent, channel, body, priority, "
            "attribution, audience) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (now, from_agent, _BROADCAST_CHANNEL, message, priority, grade, audience),
        )
        broadcast_id = cursor.lastrowid

        # Always advance the sender's broadcast cursor past their own message.
        # Without this, the sender sees their own broadcast surfaced on their
        # next Stop-hook auto-pull (annoying — they wrote it).
        conn.execute(
            "UPDATE agents SET last_broadcast_seen_id = MAX(last_broadcast_seen_id, ?) "
            "WHERE name = ?",
            (broadcast_id, from_agent),
        )
        conn.commit()

        # Low-priority broadcasts go to the feed only; no wake. Recipients
        # see them via Stop-hook auto-pull on next turn — don't advance
        # any recipient cursors.
        if priority in _NO_WAKE_PRIORITIES:
            return (
                f"Broadcast posted (priority={priority}; no wake — "
                f"agents will see it via get_broadcasts())."
            )

        # BOTH delivery paths must filter or the fix is cosmetic. A broadcast
        # reaches an agent live here, AND via the Stop-hook cursor catch-up in
        # get_broadcasts_for_agent. Several of the messages that caused
        # 2026-07-27's cross-lane replies arrived through the SECOND path.
        recipients = [a for a in registry.names() if a != from_agent]
        if audience:
            in_team = {
                r["name"]
                for r in conn.execute(
                    "SELECT name FROM agents WHERE team = ?", (audience,)
                ).fetchall()
            }
            recipients = [a for a in recipients if a in in_team]

        # Parallel fan-out. Each push_channel involves an async send through
        # the recipient's MCP write stream; serializing them means broadcast
        # latency is the SUM of recipient send times. Task-group fan-out
        # drops it to ≈ the MAX (slowest single recipient). Results land in
        # a dict keyed by agent name — anyio task groups run cooperatively
        # on one event loop, so concurrent writes to the dict are safe.
        push_results: dict[str, _PushOutcome] = {}

        async def _push_one(agent: str) -> None:
            push_results[agent] = await push_channel(
                agent=agent,
                content=f"BROADCAST from {from_agent}: {_clip_push(message)}",
                meta={
                    "from_agent": from_agent,
                    "kind": "broadcast",
                    "priority": priority,
                },
            )

        async with anyio.create_task_group() as tg:
            for agent in recipients:
                tg.start_soon(_push_one, agent)

        # Advance each recipient's cursor ONLY when the PRIMARY session got the
        # live push. The cursor is per-agent (one row), so marking it "seen"
        # silences the Stop-hook catch-up for EVERY session under that name —
        # advancing it on an extra-only delivery would let the primary (or a
        # gated session) permanently miss a broadcast it never received. Tying
        # the advance to the primary mirrors the DM generation stamp: worst
        # case a session that DID see it live sees it once more via Stop hook
        # (a harmless dup), never silent loss. Single-session is unchanged
        # (primary == the only session).
        successes = [a for a, o in push_results.items() if o.primary]
        if successes:
            placeholders = ",".join("?" * len(successes))
            conn.execute(
                f"UPDATE agents SET last_broadcast_seen_id = "
                f"MAX(last_broadcast_seen_id, ?) WHERE name IN ({placeholders})",
                (broadcast_id, *successes),
            )
            conn.commit()

        # Honest woke count — anyone we delivered a live wake to (primary or an
        # extra), distinct from the cursor-advance set above.
        woke = sum(1 for o in push_results.values() if o.delivered)
        return (
            f"Broadcast posted (priority={priority}; "
            f"woke {woke}/{len(recipients)} connected agents)."
        ) + _verbosity_advisory(message)

    # -- Channels (topical, named, posted-to via `post`) ---------------------

    @mcp.tool()
    def create_channel(name: str, created_by: str, description: str = "") -> str:
        """Create a named channel for topical posts.

        Channels are for grouping conversation by topic (e.g. "deploys",
        "qa", "research"). Posts to a channel still reach every connected
        agent today (we don't have per-channel subscriptions yet) but they
        carry the channel as a label so retrospective queries can scope
        cleanly.

        Note: the name `"general"` is reserved for the global broadcast feed
        (use `broadcast` for that). Other names can be anything reasonable.

        Args:
            name: Channel name (e.g. 'deploys', 'qa', 'chat').
            created_by: Your agent name.
            description: What this channel is for.
        """
        if name == _BROADCAST_CHANNEL:
            return (
                f"'{_BROADCAST_CHANNEL}' is reserved as the global broadcast "
                f"feed — use broadcast() instead of post()."
            )
        now = time.time()
        conn = _get_db(db_path)
        try:
            conn.execute(
                "INSERT INTO channels (name, created_by, created_at, description) "
                "VALUES (?, ?, ?, ?)",
                (name, created_by, now, description),
            )
            conn.commit()
            return f"Channel '{name}' created."
        except sqlite3.IntegrityError:
            return f"Channel '{name}' already exists."

    @mcp.tool()
    def list_channels() -> str:
        """List all named channels.

        The global broadcast feed is not a channel and is not listed here —
        it's always available via broadcast() / get_broadcasts().
        """
        conn = _get_db(db_path)
        rows = conn.execute(
            "SELECT * FROM channels WHERE name != ? ORDER BY name",
            (_BROADCAST_CHANNEL,),
        ).fetchall()
        if not rows:
            return "No channels. Create one with create_channel()."
        lines = []
        for r in rows:
            line = f"**#{r['name']}**"
            if r["description"]:
                line += f" — {r['description']}"
            lines.append(line)
        return "\n".join(lines)

    @mcp.tool()
    async def post(
        from_agent: str,
        channel: str,
        message: str,
        priority: str = "normal",
        ctx: Context | None = None,
    ) -> str:
        """Post a message to a named channel.

        The channel must already exist (use `create_channel` first). Same
        priority semantics as `broadcast`: "low" persists to channel
        history without firing wake; "normal" wakes every connected agent;
        "urgent" wakes with the priority surfaced in the rendered tag.

        For global messages every agent should see, use `broadcast`. For
        a single recipient, use `send`.

        Args:
            from_agent: Your agent name.
            channel: Channel name (must exist; not "general").
            message: The message body.
            priority: One of "low" | "normal" | "urgent". Defaults to "normal".
        """
        if priority not in _VALID_PRIORITIES:
            return (
                f"Invalid priority '{priority}'. "
                f"Use one of: {sorted(_VALID_PRIORITIES)}."
            )
        if channel == _BROADCAST_CHANNEL:
            return (
                f"'{_BROADCAST_CHANNEL}' is the global broadcast feed — "
                f"use broadcast() instead of post()."
            )

        now = time.time()
        conn = _get_db(db_path)

        # Verify channel exists. Posts to non-existent channels are rejected
        # (vs auto-creating) so typos don't accumulate phantom channels.
        row = conn.execute("SELECT 1 FROM channels WHERE name = ?", (channel,)).fetchone()
        if not row:
            return f"Channel '{channel}' not found. Create it with create_channel()."

        # Verify-when-bound before the touch (see send() for the ordering).
        grade, attr_err = _attribution(ctx, from_agent)
        if attr_err:
            return attr_err

        # Auto-bind sender's session for drift self-heal.
        touch_session(from_agent, ctx)

        conn.execute(
            "UPDATE agents SET last_seen = ? WHERE name = ?", (now, from_agent)
        )
        conn.execute(
            "INSERT INTO messages (ts, from_agent, channel, body, priority, "
            "attribution) VALUES (?, ?, ?, ?, ?, ?)",
            (now, from_agent, channel, message, priority, grade),
        )
        conn.commit()

        if priority in _NO_WAKE_PRIORITIES:
            return (
                f"Posted to #{channel} (priority={priority}; no wake — "
                f"agents will see it via get_channel_messages())."
            )

        recipients = [a for a in registry.names() if a != from_agent]

        # Parallel fan-out — same rationale as broadcast(). Posts have no
        # per-recipient cursor to advance, so the post-loop simply counts
        # successful pushes.
        push_results: dict[str, _PushOutcome] = {}

        async def _push_one(agent: str) -> None:
            push_results[agent] = await push_channel(
                agent=agent,
                content=f"#{channel} from {from_agent}: {_clip_push(message)}",
                meta={
                    "from_agent": from_agent,
                    "kind": "post",
                    "channel": channel,
                    "priority": priority,
                },
            )

        async with anyio.create_task_group() as tg:
            for agent in recipients:
                tg.start_soon(_push_one, agent)

        woke = sum(1 for o in push_results.values() if o.delivered)
        return (
            f"Posted to #{channel} (priority={priority}; "
            f"woke {woke}/{len(recipients)} connected agents)."
        ) + _verbosity_advisory(message)

    @mcp.tool()
    def get_channel_messages(
        channel: str,
        limit: int = 20,
        since_minutes: int = 60,
        since_id: int = 0,
        from_agent: str = "",
        format: str = "text",
    ) -> str:
        """Get recent messages from a named channel.

        For the global broadcast feed, use `get_broadcasts` instead.

        Filtering:
          - Default: returns the last `since_minutes` minutes of messages.
          - When `since_id > 0`: returns messages with id strictly greater
            than `since_id` and `since_minutes` is ignored. Use this for
            cursor-based extraction (each call passes the max(id) seen so
            far; loss-less on retries since duplicates are excluded by id).
          - When `from_agent` is set, results are restricted to messages
            posted by that agent. Useful for "show me what I've already
            contributed to this channel" before re-posting (dedup pattern
            for re-asks). Composes with both `since_id` and `since_minutes`.

        Format:
          - "text" (default): chat-style render, one line per message:
            `[hh:mm:ss] **from** [priority]: body`. For human reading.
          - "json": JSON array of `{id, ts, from_agent, body, priority}`
            records. For programmatic consumption (e.g. extraction
            pipelines that need stable message identity).

        Args:
            channel: Channel name.
            limit: Max messages to return.
            since_minutes: Window in minutes (only applied when since_id is 0).
            since_id: Message-id cursor; when > 0, returns messages with id
                      greater than this and ignores `since_minutes`.
            from_agent: If set, only return messages from this agent name.
            format: "text" (default) or "json".
        """
        if format not in ("text", "json"):
            return f"Invalid format '{format}'. Use 'text' or 'json'."

        conn = _get_db(db_path)
        if since_id > 0:
            if from_agent:
                rows = conn.execute(
                    """SELECT id, ts, from_agent, body, priority FROM messages
                       WHERE channel = ? AND id > ? AND from_agent = ?
                       ORDER BY id ASC LIMIT ?""",
                    (channel, since_id, from_agent, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT id, ts, from_agent, body, priority FROM messages
                       WHERE channel = ? AND id > ?
                       ORDER BY id ASC LIMIT ?""",
                    (channel, since_id, limit),
                ).fetchall()
        else:
            cutoff = time.time() - (since_minutes * 60)
            if from_agent:
                rows = conn.execute(
                    """SELECT id, ts, from_agent, body, priority FROM messages
                       WHERE channel = ? AND ts > ? AND from_agent = ?
                       ORDER BY ts ASC LIMIT ?""",
                    (channel, cutoff, from_agent, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT id, ts, from_agent, body, priority FROM messages
                       WHERE channel = ? AND ts > ?
                       ORDER BY ts ASC LIMIT ?""",
                    (channel, cutoff, limit),
                ).fetchall()

        if format == "json":
            return json.dumps([
                {
                    "id": r["id"],
                    "ts": r["ts"],
                    "from_agent": r["from_agent"],
                    "body": r["body"],
                    "priority": r["priority"],
                }
                for r in rows
            ])

        if not rows:
            return ""

        lines = []
        for r in rows:
            ts = time.strftime("%H:%M:%S", time.localtime(r["ts"]))
            prio = r["priority"] if r["priority"] != "normal" else ""
            prio_tag = f" [{prio}]" if prio else ""
            lines.append(f"[{ts}] **{r['from_agent']}**{prio_tag}: {r['body']}")
        return "\n".join(lines)

    # -- Reading messages --

    @mcp.tool()
    def get_messages(
        agent_name: str,
        limit: int = 20,
        bind: bool = True,
        mark_idle: bool = False,
        compact: bool = False,
        ctx: Context | None = None,
    ) -> str:
        """Get unread direct messages for this agent. Marks them as read.

        Args:
            agent_name: Your agent name.
            limit: Max messages to return.
            bind: If True (default), refresh the agent's wake-binding to the
                  calling session — this is the drift self-heal property
                  for normal interactive use. The Stop hook utility
                  (mcp-hub stop-hook) passes bind=False because its
                  streamablehttp_client is ephemeral: binding to it would
                  overwrite the agent's real wake target with a session
                  that's about to be DELETEd, silently breaking wake.
            mark_idle: If True, set the agent's is_idle flag (used by the
                  Case 1 wake-on-low-prio path so a low-prio DM to an idle
                  recipient fires a wake). The Stop hook passes True
                  because end-of-turn IS the idle transition. Default False
                  for ordinary callers — they're in an active turn, not
                  idle.
        """
        now = time.time()
        conn = _get_db(db_path)

        # Capture BEFORE the drain-ack below clears it: was a delivered wake
        # still awaiting an INDEPENDENT ack when this drain began? If so, the
        # recipient's stream never proved it rendered — a half-dead deaf-⚡
        # binding (bound + ⚡ after a redeploy reconnect, before a process
        # relaunch) passes every server-side deliverability check yet shows the
        # agent nothing (proven live: fireblade, Windows, 2026-07-23). The
        # compact "already delivered live" claim MUST fail safe to a full
        # reprint in that state, or every post-redeploy wave truncates messages
        # on a false "you saw this". push success ≠ render; the drain itself is
        # NOT render evidence — it's how a deaf agent DISCOVERS what it missed.
        wake_render_unproven = registry.has_pending_wake_ack(agent_name)

        # Draining messages is a wake-ack regardless of bind: the agent's
        # process is demonstrably alive and reading — even the Stop hook's
        # bind=False pull proves the wake pipeline's PURPOSE (delivery) was
        # served. Without this, an agent that answers a wake using only
        # non-hub tools would be falsely struck when its Stop hook fires.
        registry.wake_ack(agent_name)

        # Auto-bind caller's session for drift self-heal.
        if bind:
            touch_session(agent_name, ctx)

        # Mark agent idle when the Stop hook calls (end of turn = idle).
        # Only update if the agent row exists; touching a non-agent name
        # silently no-ops (consistent with touch_session's discipline).
        if mark_idle:
            conn.execute(
                "UPDATE agents SET is_idle = 1, last_idle_at = ? "
                "WHERE name = ?",
                (now, agent_name),
            )

        # Update last_seen
        conn.execute("UPDATE agents SET last_seen = ? WHERE name = ?", (now, agent_name))

        # One-shot coverage-gap notice: queued at coming-online, delivered on
        # the first drain after, then cleared. Delivered even with an empty
        # queue — "nothing waiting" is exactly the claim a gap undermines.
        gap_row = conn.execute(
            "SELECT gap_notice FROM agents WHERE name = ? AND gap_notice != ''",
            (agent_name,),
        ).fetchone()
        gap_notice = gap_row["gap_notice"] if gap_row else ""
        if gap_notice:
            conn.execute(
                "UPDATE agents SET gap_notice = '' WHERE name = ?",
                (agent_name,),
            )
            conn.commit()

        rows = conn.execute(
            """SELECT id, ts, from_agent, body, priority, pushed_gen FROM messages
               WHERE to_agent = ? AND read = 0
               ORDER BY ts ASC LIMIT ?""",
            (agent_name, limit),
        ).fetchall()

        if not rows:
            return gap_notice

        # Mark as read
        ids = [r["id"] for r in rows]
        placeholders = ",".join("?" * len(ids))
        conn.execute(f"UPDATE messages SET read = 1 WHERE id IN ({placeholders})", ids)
        conn.commit()

        # compact mode (Stop hook): two independent economies.
        #  1. ALREADY-SEEN — the message was pushed to the very binding the
        #     agent still holds, so it was rendered live in their context.
        #     Reprinting the whole body is pure duplication; one line is
        #     enough to confirm it's now marked read.
        #  2. BULK CAP — beyond the first few, bodies are summarised. A
        #     Stop-hook dump of a dozen long messages costs more context than
        #     the messages are worth, and the agent can always pull the full
        #     text with get_messages().
        # Both degrade to full text whenever there's any doubt: no message is
        # ever dropped, only ever shortened.
        gen_now = registry.generation(agent_name)
        lines: list[str] = []
        seen_live = 0
        capped = 0
        clipped = 0
        full_budget = COMPACT_FULL_MESSAGES
        for r in rows:
            ts = time.strftime("%H:%M:%S", time.localtime(r["ts"]))
            # Show priority tag for non-normal messages so retrospective
            # readers can triage without losing the cue from the live wake.
            prio = r["priority"] if r["priority"] != "normal" else ""
            prio_tag = f" [{prio}]" if prio else ""
            body = r["body"]
            if compact:
                pushed_gen = r["pushed_gen"] if "pushed_gen" in r.keys() else ""
                # "already delivered live" needs BOTH: the push hit the binding
                # the agent still holds (generation match) AND that binding's
                # render is not in doubt (no wake left unacked before this
                # drain). The second gate is the deaf-⚡ fix — without it, a
                # bound-but-non-rendering stream gets its messages truncated on
                # a false live-delivery claim (worst fleet-wide right after a
                # redeploy). Doubt → fall through to full text.
                if (
                    pushed_gen and gen_now and pushed_gen == gen_now
                    and not wake_render_unproven
                ):
                    seen_live += 1
                    lines.append(
                        f"[{ts}] **{r['from_agent']}**{prio_tag}: "
                        f"(already delivered live — {_summarise(body)})"
                    )
                    continue
                if full_budget > 0:
                    full_budget -= 1
                    clipped_body = _clip(body)
                    if clipped_body is not body:
                        clipped += 1
                    body = clipped_body
                else:
                    capped += 1
                    body = _summarise(body, COMPACT_SUMMARY_CHARS)
            lines.append(f"[{ts}] **{r['from_agent']}**{prio_tag}: {body}")
        if compact and (seen_live or capped or clipped):
            # Point at get_history, NOT get_messages: this very call marked
            # these rows read, so a follow-up get_messages returns nothing.
            # (The first version of this footer said get_messages — advice
            # that the read-semantics test in this repo already disproved.)
            what = []
            if seen_live:
                what.append(f"{seen_live} already surfaced live")
            if capped:
                what.append(f"{capped} past the {COMPACT_FULL_MESSAGES}-message cap")
            if clipped:
                what.append(f"{clipped} clipped at {COMPACT_FULL_BODY_CHARS} chars")
            lines.append(
                f"({' and '.join(what)} — shortened to save context, and now "
                f"marked read. Full text: get_history('{agent_name}'))"
            )
        if gap_notice:
            lines.insert(0, gap_notice)
        return "\n".join(lines)

    @mcp.tool()
    def get_broadcasts(limit: int = 20, since_minutes: int = 60) -> str:
        """Get recent broadcasts.

        Args:
            limit: Max messages to return.
            since_minutes: Only show messages from the last N minutes.
        """
        cutoff = time.time() - (since_minutes * 60)
        conn = _get_db(db_path)
        rows = conn.execute(
            """SELECT ts, from_agent, body, priority FROM messages
               WHERE channel = ? AND ts > ?
               ORDER BY ts ASC LIMIT ?""",
            (_BROADCAST_CHANNEL, cutoff, limit),
        ).fetchall()

        if not rows:
            return ""

        lines = []
        for r in rows:
            ts = time.strftime("%H:%M:%S", time.localtime(r["ts"]))
            prio = r["priority"] if r["priority"] != "normal" else ""
            prio_tag = f" [{prio}]" if prio else ""
            lines.append(f"[{ts}] **{r['from_agent']}**{prio_tag}: {r['body']}")
        return "\n".join(lines)

    @mcp.tool()
    def get_broadcasts_for_agent(
        agent_name: str,
        limit: int = 50,
        bind: bool = True,
        compact: bool = False,
        ctx: Context | None = None,
    ) -> str:
        """Get broadcasts this agent hasn't seen yet, and advance their cursor.

        Used by Stop hooks (and any future "catch up since I was away" flow):
        atomically returns broadcasts with id > the agent's
        last_broadcast_seen_id, then bumps the cursor to the max id returned.
        Same semantics as get_messages for DMs — read marks as seen, so the
        same call repeated quickly returns nothing new.

        Without this primitive, broadcasts would silently bypass drifted
        agents (their session isn't bound, channel push doesn't reach them,
        and the Stop hook only checked DM inbox). Now they catch up.

        Args:
            agent_name: Your agent name (must be registered).
            limit: Max broadcasts to return.
            bind: If True (default), refresh the agent's wake-binding to
                  the calling session. The Stop hook utility passes
                  bind=False because its streamablehttp_client is ephemeral
                  and binding to it would clobber the agent's real wake
                  target. See note on get_messages for full rationale.
            compact: Stop-hook economy, mirroring get_messages: the first
                  COMPACT_FULL_MESSAGES bodies are clipped at
                  COMPACT_FULL_BODY_CHARS, the rest summarised to one line.
                  Broadcasts turned out to be the unclipped half of the
                  Stop-hook context tax (operator, 2026-07-26: multi-KB
                  fleet broadcasts landing whole in every idle agent's
                  context). No already-seen-live leg here: a broadcast row
                  is shared by all recipients, so it can't carry a
                  per-recipient pushed generation. Nothing is dropped,
                  only shortened — the footer points at the full text.
        """
        conn = _get_db(db_path)
        # Broadcast drain is a wake-ack too — same rationale as get_messages.
        registry.wake_ack(agent_name)
        row = conn.execute(
            "SELECT last_broadcast_seen_id FROM agents WHERE name = ?",
            (agent_name,),
        ).fetchone()
        if row is None:
            # Unregistered agent — nothing to return; they'll get a fresh
            # cursor when they call register().
            return ""

        # Auto-bind caller's session for drift self-heal.
        if bind:
            touch_session(agent_name, ctx)

        cursor = row["last_broadcast_seen_id"]

        # Second delivery path — see the note in broadcast(). An agent catches
        # up on fleet-wide rows (audience = '') plus its own team's. The cursor
        # still advances past everything, so a filtered-out row is never
        # re-offered: it was not for this agent, at the time it was sent.
        my_team = conn.execute(
            "SELECT team FROM agents WHERE name = ?", (agent_name,)
        ).fetchone()
        my_team = (my_team["team"] if my_team else "") or ""
        rows = conn.execute(
            """SELECT id, ts, from_agent, body, priority FROM messages
               WHERE channel = ? AND id > ?
                 AND (audience = '' OR audience = ?)
               ORDER BY id ASC LIMIT ?""",
            (_BROADCAST_CHANNEL, cursor, my_team, limit),
        ).fetchall()

        if not rows:
            return ""

        # Advance cursor to the max id we're returning. Atomic with the read
        # — if the agent's Stop hook crashes after this commit, the cursor
        # is already advanced, mirroring how get_messages marks DMs read on
        # consume.
        max_id = max(r["id"] for r in rows)
        conn.execute(
            "UPDATE agents SET last_broadcast_seen_id = ? WHERE name = ?",
            (max_id, agent_name),
        )
        conn.commit()

        lines = []
        capped = 0
        clipped = 0
        full_budget = COMPACT_FULL_MESSAGES
        for r in rows:
            ts = time.strftime("%H:%M:%S", time.localtime(r["ts"]))
            prio = r["priority"] if r["priority"] != "normal" else ""
            prio_tag = f" [{prio}]" if prio else ""
            body = r["body"]
            if compact:
                if full_budget > 0:
                    full_budget -= 1
                    clipped_body = _clip(body)
                    if clipped_body is not body:
                        clipped += 1
                    body = clipped_body
                else:
                    capped += 1
                    body = _summarise(body, COMPACT_SUMMARY_CHARS)
            lines.append(f"[{ts}] **{r['from_agent']}**{prio_tag}: {body}")
        if compact and (capped or clipped):
            # Point at get_history('#general'), not get_broadcasts_for_agent:
            # this very call advanced the cursor, so a repeat returns nothing.
            # (Same read-semantics trap the get_messages footer already hit.)
            what = []
            if capped:
                what.append(f"{capped} past the {COMPACT_FULL_MESSAGES}-message cap")
            if clipped:
                what.append(f"{clipped} clipped at {COMPACT_FULL_BODY_CHARS} chars")
            lines.append(
                f"({' and '.join(what)} — shortened to save context, and the "
                f"cursor is advanced. Full text: get_history('#general'))"
            )
        return "\n".join(lines)

    # -- Decision cards (operator-triage currency, 2026-07-26) ---------------
    #
    # The card is authored by the agent (or a service) — the hub stores it
    # PARSED so triage machinery can rank cards without re-parsing prose.
    # One OPEN card per agent: put() upserts, so a restated ask updates in
    # place. Lifecycle: open -> decided (operator answered, asker DM'd) or
    # withdrawn (agent moved on — its next turn carried no card).
    # NOTE: none of these tools call touch_session — decision_put/clear
    # arrive from the Stop hook's EPHEMERAL client by design.

    @mcp.tool()
    def decision_put(
        from_agent: str,
        card: str,
        project: str = "",
        source: str = "stop-hook",
        tags: str = "",
        ctx: Context | None = None,
    ) -> str:
        """Submit (or restate) a DECISION card for the operator's triage queue.

        Args:
            from_agent: The asking agent (or service) name.
            card: The raw DECISION card text (ASK/WHY/VALUE/RISK/[TAGS] block).
            project: Project the ask belongs to (derived where possible).
            source: 'stop-hook' (harvested from a turn) or 'api' (services).
            tags: Extra comma-separated tags, merged with the card's TAGS line.
        """
        now = time.time()
        # A card is an ask in the OPERATOR's queue under the asker's name —
        # exactly the record class the attribution gate exists for. The
        # stop-hook's ephemeral client stays 'asserted' (unbound, by design).
        grade, attr_err = _attribution(ctx, from_agent)
        if attr_err:
            return attr_err
        # Defensive size cap: the harvester takes DECISION→end-of-turn, so a
        # convention-breaking turn (card followed by a ramble) could ship a
        # novel. The queue is for one-glance asks; the ledger keeps raw
        # bounded. 4KB is ~8x a well-formed card.
        card = card[:4096]
        f = parse_decision_card(card)
        all_tags = ",".join(sorted({
            *(t for t in f["tags"].split(",") if t),
            *(t.strip().lower() for t in tags.split(",") if t.strip()),
        }))
        conn = _get_db(db_path)
        open_row = conn.execute(
            "SELECT id, ask FROM decisions WHERE agent = ? AND status = 'open' "
            "AND source = ?",
            (from_agent, source),
        ).fetchone()
        # A DIFFERENT ask is a new card, not a restatement — supersede the
        # old row instead of overwriting it, or ask A's history vanishes
        # from the ledger the moment the agent moves on to ask B. "Different"
        # is token-overlap, not equality: agents rephrase when restating,
        # and exact-match would churn a superseded row per rewording.
        if open_row:
            old = set((open_row["ask"] or "").lower().split())
            new = set(f["ask"].lower().split())
            different = (
                bool(old) and bool(new)
                and len(old & new) / len(old | new) < 0.5
            )
            if different:
                conn.execute(
                    "UPDATE decisions SET status='superseded', decided_at=? "
                    "WHERE id=?",
                    (now, open_row["id"]),
                )
                open_row = None
        if open_row:
            conn.execute(
                """UPDATE decisions SET updated_at=?, raw=?, ask=?, why=?,
                   value_text=?, risk_text=?, value_score=?, risk_score=?,
                   net_score=?, tags=?, clear_strikes=0, stale=0,
                   attribution=?,
                   project=CASE WHEN ?='' THEN project ELSE ? END
                   WHERE id=?""",
                (now, card, f["ask"], f["why"], f["value_text"], f["risk_text"],
                 f["value_score"], f["risk_score"], f["net_score"], all_tags,
                 grade, project, project, open_row["id"]),
            )
            conn.commit()
            return f"Decision card #{open_row['id']} updated (net={f['net_score']})."
        cur = conn.execute(
            """INSERT INTO decisions (agent, project, source, submitted_at,
               updated_at, raw, ask, why, value_text, risk_text, value_score,
               risk_score, net_score, tags, attribution)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (from_agent, project, source, now, now, card, f["ask"], f["why"],
             f["value_text"], f["risk_text"], f["value_score"],
             f["risk_score"], f["net_score"], all_tags, grade),
        )
        conn.commit()
        return f"Decision card #{cur.lastrowid} opened (net={f['net_score']})."

    @mcp.tool()
    def decision_clear(from_agent: str, source: str = "stop-hook",
                       ctx: Context | None = None) -> str:
        """Register a cardless turn against the agent's open card; mark it
        STALE after 3 consecutive ones — never withdraw it.

        Withdrawal-at-3-strikes was the 2026-07-26 fix for instant
        evaporation; on 2026-07-27 it proved to be the same defect at a
        different threshold: ~25 asks auto-withdrew unanswered in one day,
        because strikes measure the SENDER's turn rate and nothing else —
        "the harder a blocked lane works, the faster it loses the ask it
        is blocked on" (pm). Worst on live investigations, where turn rate
        rises BECAUSE the ask got more urgent (dt's #101). So 3 strikes now
        DEMOTES instead of removing: stale=1, status stays 'open', the card
        stays on the operator's board sorted last. Only an operator answer,
        an agent DECIDED (decision_resolve), or supersession by a new ask
        closes a card — an unanswered ask is impossible to lose.

        The return string is the owner-notice channel: the Stop hook
        surfaces it to the agent, because the other 2026-07-27 lesson was
        that silent state changes leave agents waiting on an operator who
        has nothing in front of them.

        Only touches cards of the given source — an api-submitted service
        card is not the agent's to mark."""
        _grade, attr_err = _attribution(ctx, from_agent)
        if attr_err:
            return attr_err
        conn = _get_db(db_path)
        row = conn.execute(
            "SELECT id, ask, clear_strikes, stale FROM decisions "
            "WHERE agent=? AND status='open' AND source=?",
            (from_agent, source),
        ).fetchone()
        if row is None:
            return ""
        ask = (row["ask"] or "")[:80]
        if row["stale"]:
            return f"Card #{row['id']} is STALE on the board: {ask}"
        strikes = (row["clear_strikes"] or 0) + 1
        if strikes >= 3:
            conn.execute(
                "UPDATE decisions SET stale=1, clear_strikes=? WHERE id=?",
                (strikes, row["id"]),
            )
            conn.commit()
            return (
                f"Card #{row['id']} marked STALE after {strikes} cardless "
                f"turns — still on the operator's board, sorted last: {ask}"
            )
        conn.execute(
            "UPDATE decisions SET clear_strikes=? WHERE id=?",
            (strikes, row["id"]),
        )
        conn.commit()
        return f"Card #{row['id']} kept open (cardless turn {strikes}/3): {ask}"

    @mcp.tool()
    def decision_resolve(from_agent: str, verdict: str, source: str = "stop-hook",
                         ctx: Context | None = None) -> str:
        """Close the agent's own open card WITH the verdict it just received
        in-pane — the smart half of answer capture (operator, 2026-07-26:
        "rather than relying on some flaky auto capture"). The agent that
        processed the operator's answer records it: its closing turn ends
        with `**DECIDED:** <verdict>` and the Stop hook ships it here. The
        verdict is agent-recorded, so it is stored with that provenance —
        distinct from a decision_answer verdict typed by the operator."""
        _grade, attr_err = _attribution(ctx, from_agent)
        if attr_err:
            return attr_err
        conn = _get_db(db_path)
        cur = conn.execute(
            "UPDATE decisions SET status='decided', decided_at=?, "
            "decision='in-pane', decision_note=? "
            "WHERE agent=? AND status='open' AND source=?",
            (time.time(), f"[agent-recorded] {verdict}", from_agent, source),
        )
        conn.commit()
        return f"Card resolved: {verdict}" if cur.rowcount else ""

    @mcp.tool()
    def decision_list(status: str = "open", limit: int = 50,
                      format: str = "text") -> str:
        """List decision cards, best net score first (age as tiebreak).

        Args:
            status: 'open' (default), 'decided', 'withdrawn', 'superseded',
                or 'all'.
            limit: Max cards.
            format: 'text' for human triage, 'json' for machinery.
        """
        conn = _get_db(db_path)
        where = "" if status == "all" else "WHERE status = ?"
        args: tuple = () if status == "all" else (status,)
        rows = conn.execute(
            f"""SELECT * FROM decisions {where}
                ORDER BY stale ASC, net_score IS NULL, net_score DESC,
                         submitted_at ASC
                LIMIT ?""",
            (*args, limit),
        ).fetchall()
        if format == "json":
            return json.dumps([dict(r) for r in rows])
        if not rows:
            return f"No {status} decision cards."
        now = time.time()
        lines = []
        for r in rows:
            age = int(now - r["submitted_at"])
            age_h = (f"{age // 60}m" if age < 3600 else
                     f"{age // 3600}h" if age < 86400 else f"{age // 86400}d")
            net = f"net {r['net_score']:+d}" if r["net_score"] is not None else "net ?"
            tags = f" [{r['tags']}]" if r["tags"] else ""
            # A mixed-status listing must SAY which rows are history — an
            # unlabeled superseded row is indistinguishable from a live ask,
            # and a reader tallying "open" cards from an `all` view counts
            # ledger rows as queue (measured 2026-07-27: 25 of 28 rows in an
            # `all` render were closed, none said so).
            status_tag = "" if r["status"] == "open" else f" · {r['status'].upper()}"
            if r["status"] == "open" and r["stale"]:
                # Demoted, not gone: the sender kept taking turns without
                # restating, so it sorts last — but an unanswered ask never
                # leaves the board (2026-07-27 evaporation incident).
                status_tag = " · STALE"
            closure = ""
            if r["status"] == "decided":
                closure = f"\n   -> {r['decision']} {r['decision_note']}".rstrip()
            elif r["status"] != "open" and (r["decision_note"] or r["decision"]):
                closure = f"\n   -> {r['decision_note'] or r['decision']}"
            lines.append(
                f"#{r['id']} {net} · {r['agent']} · {age_h}{tags}{status_tag}\n"
                f"   ASK: {r['ask'] or _summarise(r['raw'])}" + closure
            )
        return "\n".join(lines)

    @mcp.tool()
    async def decision_answer(
        decision: str,
        card_id: int = 0,
        agent: str = "",
        note: str = "",
    ) -> str:
        """Answer a decision card — the operator's leg. Closes the card and
        DMs the verdict to the asker (wake path included), so the answer
        travels without a relay.

        Args:
            decision: 'yes' | 'no' | 'defer' (free text allowed).
            card_id: The card to answer; 0 = use `agent`'s open card.
            agent: Alternative target: this agent's open card.
            note: Optional context for the asker.
        """
        conn = _get_db(db_path)
        row = conn.execute(
            "SELECT * FROM decisions WHERE id = ? AND status = 'open'",
            (card_id,),
        ).fetchone() if card_id else conn.execute(
            "SELECT * FROM decisions WHERE agent = ? AND status = 'open' "
            "ORDER BY updated_at DESC",
            (agent,),
        ).fetchone()
        if row is None:
            return "No matching open decision card."
        conn.execute(
            "UPDATE decisions SET status='decided', decided_at=?, decision=?, "
            "decision_note=? WHERE id=?",
            (time.time(), decision, note, row["id"]),
        )
        body = (
            f"🎯 DECISION ANSWERED ({decision.upper()}): {row['ask'] or 'your open card'}"
            + (f" — {note}" if note else "")
        )
        cur = conn.execute(
            "INSERT INTO messages (ts, from_agent, to_agent, body, priority) "
            "VALUES (?, 'operator', ?, ?, 'normal')",
            (time.time(), row["agent"], body),
        )
        msg_id = cur.lastrowid
        conn.commit()
        outcome = await push_channel(
            agent=row["agent"],
            content=f"DM from operator: {body}",
            meta={"from_agent": "operator", "kind": "dm", "priority": "normal"},
        )
        if outcome.primary:
            _stamp_pushed(conn, [msg_id], row["agent"])
        return (
            f"Card #{row['id']} decided: {decision}. Asker "
            f"{'woken live' if outcome.delivered else 'will see it on next pull'}."
        )

    # -- History --

    @mcp.tool()
    def get_history(agent_or_channel: str, limit: int = 50) -> str:
        """Get message history for an agent (DMs sent/received) or a channel.

        Args:
            agent_or_channel: Agent name or channel name (prefix with # for channels).
            limit: Max messages to return.
        """
        conn = _get_db(db_path)

        if agent_or_channel.startswith("#"):
            channel = agent_or_channel[1:]
            rows = conn.execute(
                """SELECT ts, from_agent, body FROM messages
                   WHERE channel = ? ORDER BY ts DESC LIMIT ?""",
                (channel, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT ts, from_agent, to_agent, channel, body FROM messages
                   WHERE from_agent = ? OR to_agent = ?
                   ORDER BY ts DESC LIMIT ?""",
                (agent_or_channel, agent_or_channel, limit),
            ).fetchall()

        if not rows:
            return "No message history."

        lines = []
        for r in rows:
            ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["ts"]))
            if "to_agent" in r.keys() and r["to_agent"]:
                lines.append(f"[{ts}] {r['from_agent']} → {r['to_agent']}: {r['body']}")
            elif "channel" in r.keys() and r["channel"]:
                lines.append(f"[{ts}] {r['from_agent']} → #{r['channel']}: {r['body']}")
            else:
                lines.append(f"[{ts}] {r['from_agent']}: {r['body']}")
        lines.reverse()
        return "\n".join(lines)

    # -- Utility --

    @mcp.tool()
    def ping(from_agent: str, ctx: Context | None = None) -> str:
        """Heartbeat — updates your last_seen timestamp and refreshes your
        session binding.

        Args:
            from_agent: Your agent name.
        """
        now = time.time()
        conn = _get_db(db_path)
        touch_session(from_agent, ctx)
        conn.execute("UPDATE agents SET last_seen = ? WHERE name = ?", (now, from_agent))
        conn.commit()
        return f"pong ({time.strftime('%H:%M:%S')})"

    @mcp.tool()
    def heartbeat(agent_name: str) -> str:
        """Out-of-session liveness signal from the agent's heartbeat daemon.

        The daemon (spawned by an async SessionStart hook) calls this every
        ~60s to prove the agent's Claude Code process is still alive. Use
        case: keep `_last_activity` fresh so the reaper doesn't drop a
        healthy idle agent who hasn't called the hub in a while.

        Crucially this does NOT bind. Binding the daemon's ephemeral
        streamablehttp_client would clobber the agent's real wake target —
        same wake-clobber bug we fixed for the Stop hook with bind=False.
        Instead `heartbeat` only refreshes the timestamp on an EXISTING
        binding; if the agent isn't bound, the heartbeat is a no-op (the
        agent's interactive session is responsible for register()-binding
        first; the daemon just keeps it alive thereafter).

        Args:
            agent_name: The agent name from the project's hub-agent.json
                marker. Daemon reads it and passes it here.
        """
        # Every reply carries this hub PROCESS's nonce so the heartbeat daemon
        # can detect a genuine hub RESTART (nonce changed across a reconnect →
        # every wake stream is dead → stamp for squad-heal) vs a mere blip or a
        # reaper-dropped binding (nonce unchanged → hub never restarted → must
        # NOT stamp). This replaces the old "no binding ⇒ restarted" inference,
        # which false-positived and mass-restarted the fleet on a wifi flap
        # (2026-07-20; reproved 2026-07-23). Present on ALL return paths —
        # including "no binding" — because the daemon needs the nonce exactly
        # when the binding is gone. Structured `hub_boot=<id>` so the daemon
        # parses a token, not prose.
        boot_tag = f" [hub_boot={registry.boot_id}]"
        outcome = registry.heartbeat_touch(agent_name)
        if outcome == "unbound":
            return f"heartbeat ignored — '{agent_name}' has no binding{boot_tag}"
        if outcome == "undeliverable":
            # Binding exists but a push would not land (stale after a client
            # reconnect). NOT refreshed — repeated failures drop the binding
            # so the agent's offline status becomes truthful and the Stop-hook
            # nag drives a re-register. See SessionRegistry.heartbeat_touch.
            return (
                f"heartbeat noted — '{agent_name}' binding is not "
                "push-deliverable; not refreshed (drops after "
                f"{registry.UNDELIVERABLE_BEATS_TO_DROP} consecutive misses)"
                f"{boot_tag}"
            )
        if outcome == "dropped":
            return (
                f"heartbeat: dropped stale binding for '{agent_name}' "
                "(undeliverable); agent marked offline — the interactive "
                f"session must register() to rebind{boot_tag}"
            )
        # refreshed — keep last_seen in sync for list_agents ordering.
        conn = _get_db(db_path)
        conn.execute(
            "UPDATE agents SET last_seen = ? WHERE name = ?",
            (time.time(), agent_name),
        )
        conn.commit()
        return f"heartbeat ok ({time.strftime('%H:%M:%S')}){boot_tag}"

    @mcp.tool()
    def list_twins(project: str, exclude_agent: str = "") -> str:
        """List online agents on `project` — the paired clones of one repo
        across machines (same derived org/repo, different <repo>-<hostname>
        names). Returns one name per line, or '' if none.

        Args:
            project: The derived project key, e.g. 'monkeypashion/mcp-hub'.
            exclude_agent: Optional name to omit (usually the caller).
        """
        conn = _get_db(db_path)
        rows = conn.execute(
            "SELECT name FROM agents WHERE project = ? AND name != ? "
            "AND status = 'online' ORDER BY name",
            (project, exclude_agent),
        ).fetchall()
        return "\n".join(r["name"] for r in rows)

    @mcp.tool()
    def memory_put(project: str, filename: str, content: str, from_agent: str = "") -> str:
        """Stage one Claude memory file for transfer to this project's twins.

        The hub is a TRANSFER store, not the system of record — the file's
        home remains each machine's ~/.claude/projects/<dir>/memory. One row
        per (project, filename), last write wins. Driven by
        `mcp-hub memory-export`; twins pull with `mcp-hub memory-import`.

        Args:
            project: Derived project key ('org/repo').
            filename: Memory file name, preserved verbatim (e.g. 'topic.md').
            content: Full file content.
            from_agent: Exporting agent's name (provenance).
        """
        if not project or not filename:
            return "memory_put requires project and filename"
        if "/" in filename or "\\" in filename or filename in (".", ".."):
            return f"invalid filename '{filename}' — bare names only"
        conn = _get_db(db_path)
        conn.execute(
            """INSERT INTO memory_files (project, filename, content, updated_ts, origin_agent)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(project, filename) DO UPDATE SET
                   content=excluded.content,
                   updated_ts=excluded.updated_ts,
                   origin_agent=excluded.origin_agent""",
            (project, filename, content, time.time(), from_agent),
        )
        conn.commit()
        return f"staged {project}/{filename} ({len(content)} chars)"

    @mcp.tool()
    def memory_list(project: str) -> str:
        """List memory files staged for `project` (one per line:
        `filename\\tsize\\torigin_agent\\tupdated_iso\\tsha256_16`), or '' if
        none. The truncated content hash lets clients verify local files
        against the staged set without downloading them (memory-verify)."""
        conn = _get_db(db_path)
        rows = conn.execute(
            "SELECT filename, content, origin_agent, updated_ts "
            "FROM memory_files WHERE project = ? ORDER BY filename",
            (project,),
        ).fetchall()
        lines = []
        for r in rows:
            digest = hashlib.sha256(r["content"].encode("utf-8")).hexdigest()[:16]
            lines.append(
                f"{r['filename']}\t{len(r['content'])}\t{r['origin_agent']}\t"
                + time.strftime(
                    "%Y-%m-%dT%H:%M:%S", time.localtime(r["updated_ts"])
                )
                + f"\t{digest}"
            )
        return "\n".join(lines)

    @mcp.tool()
    def memory_get(project: str, filename: str) -> str:
        """Fetch one staged memory file's content (raw). Empty string if the
        (project, filename) pair isn't staged."""
        conn = _get_db(db_path)
        row = conn.execute(
            "SELECT content FROM memory_files WHERE project = ? AND filename = ?",
            (project, filename),
        ).fetchone()
        return row["content"] if row else ""

    @mcp.tool()
    def hub_status() -> str:
        """Get hub statistics — agents online, channels, message counts."""
        conn = _get_db(db_path)
        agents = conn.execute("SELECT COUNT(*) as c FROM agents WHERE status='online'").fetchone()
        channels = conn.execute("SELECT COUNT(*) as c FROM channels").fetchone()
        messages = conn.execute("SELECT COUNT(*) as c FROM messages").fetchone()
        unread = conn.execute("SELECT COUNT(*) as c FROM messages WHERE read=0").fetchone()
        return (
            f"Agents online: {agents['c']}\n"
            f"Channels: {channels['c']}\n"
            f"Total messages: {messages['c']}\n"
            f"Unread: {unread['c']}"
        )

    # ------------------------------------------------------------------
    # Timing wrapper around tool dispatch
    # ------------------------------------------------------------------
    # Logs `tool=<name> ms=<float>` at INFO for every tool call. One
    # wrapper here covers all tools without per-tool decoration. Lets
    # us see in journalctl exactly where time is going on the hub side
    # — useful both for ongoing observability and for diagnosing
    # operator-reported "calling hub..." latency. Negligible overhead
    # (one perf_counter + one log line per call).
    _orig_call_tool = mcp._tool_manager.call_tool

    async def _timed_call_tool(name, arguments, *args, **kwargs):
        t0 = time.perf_counter()
        try:
            return await _orig_call_tool(name, arguments, *args, **kwargs)
        finally:
            duration_ms = (time.perf_counter() - t0) * 1000
            logger.info("tool=%s ms=%.1f", name, duration_ms)

    mcp._tool_manager.call_tool = _timed_call_tool

    return mcp


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

_CLI_SUBCOMMANDS = {
    "stop-hook",
    "session-start",
    "session-rewake",
    "heartbeat-daemon",
    "onboard",
    "memory-export",
    "memory-import",
    "memory-verify",
    "transport-history",
    "identity",
    "rebind-url",
}


def main():
    # Subcommand dispatch — `mcp-hub stop-hook ...` etc. delegate to the
    # client CLI module. Bare `mcp-hub [--transport ... etc.]` runs the
    # server, preserving backward compatibility with existing invocations
    # (e.g. the Dockerfile CMD).
    import sys as _sys
    if len(_sys.argv) > 1 and _sys.argv[1] in _CLI_SUBCOMMANDS:
        from .cli import main as cli_main
        _sys.exit(cli_main(_sys.argv[1:]))

    parser = argparse.ArgumentParser(
        prog="mcp-hub",
        description="Inter-agent messaging hub for Claude sessions",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "streamable-http"],
        default=os.environ.get("MCP_TRANSPORT", "stdio"),
        help="MCP transport (default: stdio, or $MCP_TRANSPORT)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("PORT", "8080")),
        help="Port for SSE/HTTP transport (default: 8080, or $PORT)",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("HOST", "0.0.0.0"),
        help="Host to bind (default: 0.0.0.0, or $HOST)",
    )
    parser.add_argument(
        "--db",
        default="mcp-hub.db",
        help="SQLite database path (default: mcp-hub.db)",
    )
    args = parser.parse_args()

    global DB_PATH
    DB_PATH = Path(args.db)

    server = create_server(DB_PATH, host=args.host, port=args.port)

    if args.transport == "streamable-http":
        # streamable-http sessions can outlive their underlying socket
        # (StreamableHTTPSessionManager keeps them warm by session-id, not
        # by connection). The reaper sweeps zombies so `list_agents` ⚡
        # stays honest. Run it as a sibling task to uvicorn so both share
        # one event loop and shutdown cancels both.
        import anyio

        registry = server._hub_registry  # type: ignore[attr-defined]

        async def run_with_reaper() -> None:
            async with anyio.create_task_group() as tg:
                tg.start_soon(registry.run_reaper)
                tg.start_soon(server._hub_url_rebind_sweep)  # type: ignore[attr-defined]
                try:
                    await server.run_streamable_http_async()
                finally:
                    tg.cancel_scope.cancel()

        anyio.run(run_with_reaper)
    else:
        # stdio / sse: session is process-bound; the lifecycle hook is
        # sufficient and no reaper is needed.
        server.run(transport=args.transport)


if __name__ == "__main__":
    main()
