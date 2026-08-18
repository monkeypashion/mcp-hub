"""Delivery receipts — WHICH messages provably rendered into a context.

The transcript is the only place render-truth exists: a pushed message
appears there as queue plumbing (the enqueue/remove queue-operation pair)
and — only if it actually reached the conversation — as a `type: "user"`
record whose content is the `<channel …>` tag (idle-wake delivery) or as a
`queued_command` attachment spliced into the chain (mid-turn delivery; see
the inline note). The Stop hook scans its own transcript tail with this
module and reports the message ids it finds to the hub, which stores them
per (message_id, agent). That record is what lets the drain compact a
message to one line as a FACT ("this body entered this context") instead of
the binding-generation inference it replaces — an inference measured wrong
in both directions (113 double surfaces, 76 false compactions in
shadow-surface.jsonl when this shipped).

This is deliberately a separate module from `shadow.py`, which reads the
same transcript: shadow is a diagnostic whose contract is "can never affect
what the hook renders", while this module's output DECIDES what the hub
compacts. A shared helper would couple the two contracts; the ~15 shared
lines of tail-reading are cheaper than the coupling.

Extraction is anchored, not substring: a ref is counted only where a RENDER
puts one — the head of a live `<channel>` tag (`DM from X ⟨ref⟩:`) or the
head of a drain/batch line (`[HH:MM:SS] **X** ⟨ref⟩`). A ref merely QUOTED
in prose ("re your ⟨hub.msg/1?id=7⟩ …") must never mint a receipt: a false
receipt truncates a message its recipient never saw, which is the harmful
direction this whole design exists to close. The residual risk — a body that
pastes a full render line at start-of-line — costs a one-line summary whose
ref still names the full text in get_history, never a loss.
"""

from __future__ import annotations

import json
import os
import re

# The scan wants every channel render of the turn that just ended. Shadow's
# 512KiB proved too small against a real transcript: one long tool-heavy
# turn wrote ~1MiB after its channel renders, pushing them out of the tail —
# and a receipt missed to the window means a full reprint at the next drain.
# 4MiB covers any plausible turn for a few ms of local read; a render older
# than even that reprints in full — the safe direction (duplication, never
# loss).
TRANSCRIPT_WINDOW = 4 * 1024 * 1024

# The two positions a render puts a ref, and nothing else:
#   DM from alice ⟨hub.msg/1?id=7⟩: …            (live push tag head)
#   BROADCAST from alice ⟨hub.msg/1?id=7⟩: …     (live push tag head)
#   #general post from alice ⟨hub.msg/1?id=7⟩: … (live push tag head)
#   [15:39:03] **alice** ⟨hub.msg/1?id=7⟩ …      (drain / drain-batch line)
# Both anchored to line start so a ref cited mid-prose never matches. The
# `<channel …>` opener may share the head's line or precede it — both shapes
# occur in real transcripts, so the anchor tolerates an optional tag prefix.
_TAG_HEAD_RE = re.compile(
    r"^(?:<channel\b[^>]*>\s*)?"
    r"(?:DM|BROADCAST|.{0,80}? post) from \S+ ⟨hub\.msg/1\?id=(\d+)⟩",
    re.MULTILINE,
)
_DRAIN_LINE_RE = re.compile(
    r"^(?:<channel\b[^>]*>\s*)?"
    r"\[\d{2}:\d{2}:\d{2}\] \*\*[^*]+\*\* ⟨hub\.msg/1\?id=(\d+)⟩",
    re.MULTILINE,
)


def _tail(transcript_path: str | None, window: int) -> str:
    if not transcript_path:
        return ""
    try:
        with open(transcript_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - window))
            return f.read().decode("utf-8", errors="ignore")
    except OSError:
        return ""


def rendered_message_ids(
    transcript_path: str | None, window: int = TRANSCRIPT_WINDOW
) -> list[int]:
    """Message ids whose full render provably entered this transcript.

    Only `type: "user"` records count — the queue-lifecycle records carry
    identical content and prove only that the notification reached the
    client, not that the agent ever saw it (shadow.py's founding
    distinction). Never raises: any unreadable or unparseable state returns
    fewer receipts, and fewer receipts means fuller reprints, never loss.
    """
    ids: set[int] = set()
    for line in _tail(transcript_path, window).splitlines():
        if "<channel" not in line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue  # first line of the window is usually cut mid-record
        if rec.get("isSidechain"):
            # A subagent's context is not the agent's context.
            continue
        content: object = None
        if rec.get("type") == "user":
            content = (rec.get("message") or {}).get("content")
        elif rec.get("type") == "attachment":
            # The mid-turn render path, verified against a live transcript
            # (msg 13239, 2026-08-18): a message arriving during a turn is
            # recorded as `queue-operation` enqueue (queued), `remove`
            # (dequeued) and then a `queued_command` ATTACHMENT spliced into
            # the conversation chain — the attachment is the record of the
            # content entering context, and no `type: "user"` record ever
            # follows. Counting only user records would double-print every
            # mid-turn arrival, the dominant class on a busy lane. The bare
            # enqueue/remove pair still mints nothing: queued is not seen.
            att = rec.get("attachment") or {}
            if att.get("type") == "queued_command":
                content = att.get("prompt")
        if isinstance(content, list):
            content = "\n".join(
                b.get("text", "") for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            )
        if not isinstance(content, str) or "<channel" not in content:
            continue
        for pattern in (_TAG_HEAD_RE, _DRAIN_LINE_RE):
            for m in pattern.finditer(content):
                ids.add(int(m.group(1)))
    return sorted(ids)


def encode_report(ids: list[int]) -> str:
    """The wire form of a receipt report for the drain tools'
    `rendered_refs` argument.

    NEVER empty: `""` is the old-client sentinel that keeps the hub on its
    legacy generation-inference, so an explicit "nothing rendered" must be
    distinguishable from "client too old to report". That distinction is the
    whole zero-flag-day migration.
    """
    return ",".join(str(i) for i in ids) if ids else "none"
