"""Shadow-mode diagnostic for the Stop hook's compact rendering.

The hub decides that a message was "already delivered live" by INFERENCE:
`pushed_gen == gen_now and not wake_render_unproven` (`server.py`, get_messages
compact branch). That inference is unverified — nothing has ever compared it
against what the agent's context actually received. Two failures hide behind
it, in opposite directions:

- **False compaction** — the hub claims live delivery, the render never
  happened. The message is reduced to one line AND marked read, so the full
  text now only exists in `get_history`. This is the harmful direction.
- **Double surface** — the hub reprints in full something that DID render
  live. Pure context tax; the parked 2026-08-09 investigation
  (a session resume minting a new binding generation) predicts exactly this.

This module observes the other side of that inference from the transcript the
hook already holds, and records disagreements. It is DIAGNOSTIC ONLY:

- no hub write, no network call of any kind — the transcript is a local file;
- no effect on what the hook renders (the caller discards the return value);
- every entry point is wrapped by `run_shadow`, which swallows everything. A
  diagnostic that can break message surfacing is worse than no diagnostic.

The observation is deliberately narrow. A pushed message appears in a
transcript several times — as queue plumbing (`"operation": "enqueue"` /
`"remove"`, and `queued_command` attachments) and, IF it actually reached the
conversation, as a `type: "user"` record whose content is the `<channel …>`
tag. Only the last one is evidence of a render. Counting the queue records
would make a queued-but-never-rendered message look delivered, which is
precisely the state this exists to catch.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

# Tail window for the transcript scan. Larger than the 256KiB
# `_read_last_assistant_text` uses: that one wants the last assistant turn,
# this one wants every channel render of the turn that just ended, and a
# drain-batch of long DMs is easily bigger than one turn's prose.
TRANSCRIPT_WINDOW = 512 * 1024

# Keep the last N records. The log is a diagnostic that runs at EVERY Stop of
# every agent on the box; unbounded, it is a slow disk leak nobody is watching.
LOG_CAP = 500

# A handle shorter than this is not evidence of anything — "ok" or "done"
# would match some unrelated render on almost any turn. Below the floor we
# record the comparison as unmatchable rather than inventing a verdict.
MIN_HANDLE = 12
MATCH_PREFIX = 60

# `[15:39:03] **mcp-hub-dev-vm-1** ⟨hub.msg/1?id=7⟩ [low]: body…` — the
# get_messages render. The ⟨ref⟩ segment is W3's lineage handle and OPTIONAL:
# transcripts predating the wave carry lines without it, and a parser that
# demanded it would go silently blind on exactly the history it audits.
_RENDERED_RE = re.compile(
    r"^\[(?P<ts>\d{2}:\d{2}:\d{2})\] \*\*(?P<agent>[^*]+)\*\*"
    r"(?: ⟨(?P<ref>[^⟩]+)⟩)?"
    r"(?P<prio> \[[a-z]+\])?: (?P<rest>.*)$"
)
# The claim this module exists to check, as the server writes it.
_LIVE_CLAIM_RE = re.compile(r"^\(already delivered live — (?P<handle>.*)\)$")
_CHANNEL_FROM_RE = re.compile(r'<channel\b[^>]*\bfrom_agent="(?P<agent>[^"]*)"')


def _norm(text: str) -> str:
    """Whitespace-normalised, for comparing two different clippings of one
    body (`_summarise` at 120 chars vs `_clip_push` at 700)."""
    return " ".join(text.split())


def parse_rendered_messages(messages_text: str) -> list[dict]:
    """The per-message verdicts in a `get_messages` render.

    Returns one dict per message line: `ts`, `agent`, `claimed_live` (the hub
    asserted it already surfaced), and `handle` (the summary it claimed, or
    the body's first line when it printed in full).
    """
    out: list[dict] = []
    for line in (messages_text or "").splitlines():
        m = _RENDERED_RE.match(line)
        if not m:
            continue  # header, footer, or a continuation line of a full body
        rest = m.group("rest").strip()
        claim = _LIVE_CLAIM_RE.match(rest)
        out.append({
            "ts": m.group("ts"),
            "agent": m.group("agent").strip(),
            "claimed_live": bool(claim),
            "handle": (claim.group("handle") if claim else rest).strip(),
        })
    return out


def observed_renders(
    transcript_path: str | None, window: int = TRANSCRIPT_WINDOW
) -> list[dict]:
    """Channel pushes that actually RENDERED into the conversation.

    Only `type: "user"` records count. The queue-lifecycle records carry
    identical content and prove only that the hub's notification arrived at
    the client — not that the agent ever saw it, which is the whole question.
    """
    if not transcript_path:
        return []
    try:
        with open(transcript_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - window))
            tail = f.read().decode("utf-8", errors="ignore")
    except OSError:
        return []

    renders: list[dict] = []
    for line in tail.splitlines():
        # Prefilter on the channel tag ONLY, then decide on the parsed record.
        # Measured shapes carrying a `<channel …>` tag: `queue-operation`
        # (enqueue/remove), `attachment` (queued_command), and `user` (the
        # render). Prefiltering on a `"user"` substring would reject the first
        # two before the type check ever ran, leaving the check untestable
        # against real data — grep deciding what parse was supposed to.
        if "<channel" not in line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue  # the first line of the window is usually cut mid-record
        if rec.get("type") != "user":
            continue
        content = (rec.get("message") or {}).get("content")
        if isinstance(content, list):
            content = "\n".join(
                b.get("text", "") for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            )
        if not isinstance(content, str) or "<channel" not in content:
            continue
        m = _CHANNEL_FROM_RE.search(content)
        renders.append({"agent": (m.group("agent") if m else ""),
                        "text": _norm(content)})
    return renders


def compare(rendered: list[dict], observed: list[dict]) -> list[dict]:
    """Disagreements between the hub's inference and the transcript.

    `false_compaction` — claimed live, no render observed. `double_surface` —
    printed in full, but a render WAS observed. Everything else agrees and is
    not recorded: this log is for the exceptions, not for traffic.
    """
    out: list[dict] = []
    for msg in rendered:
        handle = _norm(msg["handle"])
        if len(handle) < MIN_HANDLE:
            # Deliberately not a verdict. A handle this short cannot
            # distinguish a match from a coincidence, and a diagnostic that
            # guesses is worse than one that abstains.
            out.append({"kind": "unmatchable", "agent": msg["agent"],
                        "ts": msg["ts"], "claimed_live": msg["claimed_live"],
                        "handle": handle})
            continue
        needle = handle[:MATCH_PREFIX].rstrip("…").rstrip()
        seen = any(
            needle in r["text"]
            and (not r["agent"] or not msg["agent"] or r["agent"] == msg["agent"])
            for r in observed
        )
        if msg["claimed_live"] and not seen:
            out.append({"kind": "false_compaction", "agent": msg["agent"],
                        "ts": msg["ts"], "handle": handle,
                        "observed_renders": len(observed)})
        elif not msg["claimed_live"] and seen:
            out.append({"kind": "double_surface", "agent": msg["agent"],
                        "ts": msg["ts"], "handle": handle[:MATCH_PREFIX]})
    return out


def record(entries: list[dict], path: Path, cap: int = LOG_CAP) -> None:
    """Append `entries` to the shadow log, then trim to the last `cap` lines."""
    if not entries:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    # Trim after writing rather than before: the cap is a disk bound, not a
    # transaction, and a rewrite that fails must not cost the new records.
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        if len(lines) > cap:
            path.write_text("\n".join(lines[-cap:]) + "\n", encoding="utf-8")
    except OSError:
        pass


def shadow_log_path() -> Path:
    return Path.home() / ".mcp-hub" / "shadow-surface.jsonl"


def run_shadow(
    agent_name: str,
    messages_text: str,
    transcript_path: str | None,
    path: Path | None = None,
) -> list[dict]:
    """Run the comparison and record disagreements. Returns them for tests.

    Never raises. The caller is the Stop hook, whose one job is surfacing
    messages; this is an observation riding along, and an observation that can
    break its host is a defect, not a diagnostic.
    """
    try:
        rendered = parse_rendered_messages(messages_text)
        if not rendered:
            return []
        entries = compare(rendered, observed_renders(transcript_path))
        if not entries:
            return []
        stamp = time.time()
        for e in entries:
            e["agent_name"] = agent_name
            e["logged_at"] = stamp
        record(entries, path or shadow_log_path())
        return entries
    except Exception:  # noqa: BLE001
        return []
