# MCP Hub — Open Cleanup Items, Current State & Lessons

_Last updated: 2026-06-01, after a long debugging session. Read this before touching the hub again._

> ⚠️ **Coolify auto-deploys on every push to `master`.** A merge — even a docs-only
> change — rebuilds the image and **restarts the hub, which wipes all bindings and
> forces the whole fleet to reconnect**. Do NOT merge hub changes into a busy fleet.
> Batch changes, deploy ONE thing in a quiet window. (This doc lives on a branch
> precisely to avoid that.)

## Current deployed state (master @ fb20d90)

Shipped 2026-06-01, in order:
- **PR #4 (3d27489)** — stop-hook (`cli.py`): the "re-register" nag now gates on `🟢`
  online, not `⚡`, and honors `stop_hook_active`. Fixed the false-rebind loop that
  PR #3's truthful-`⚡` introduced. Client-side (editable install) — no hub deploy.
- **PR #5 (accc028)** — reaper "keep-deliverable": the reaper KEEPS a binding past the
  60-min activity timeout if `_can_deliver_push` says it's deliverable. Prevents the
  idle→offline dropout. ⚠️ **Partly a mistake** — see Open Item 1.
- **PR #6 (dd4b7fb)** — `GET /health` returns `{status, service, version, commit,
  agents_bound}`. `curl http://100.109.6.114:8090/health`. `commit` shows `"unknown"`
  until the Coolify `GIT_SHA`/`SOURCE_COMMIT` build-arg is wired (PR #7 branch has the
  Dockerfile side, unmerged).
- **PR #8 (769f4f3)** — **the big reliability fix**: `send` no longer marks a DM
  `read` on push success. The inbox is the source of truth; `get_messages` marks read
  only on a genuine pull. Makes the hub churn-resilient — a push that fails or
  false-positives can no longer lose a message.

Net: messaging is reliable (no loss, churn-resilient) and idle agents stay online.
Two open warts below.

## Open Item 1 — Precise live-vs-zombie stream detection (HARD; the core problem)

**Symptom it causes:** a dead Claude Code session whose GET /mcp stream lingers still
passes `_can_deliver_push` (the gate only checks "is a GET listener registered," not
"is it the live stream the client reads from"). So it shows `🟢⚡` falsely, and with
PR #5 it is never reaped (no self-heal). Pushes to it don't surface (but post-PR #8
the message is NOT lost — it waits in the inbox).

**Why it's hard:**
- The old reaper used a server-initiated `ping` for liveness. It was REMOVED because
  Claude Code's MCP client often does NOT answer pings even on a healthy connection →
  false negatives → it dropped live agents. Do not naively re-add ping-based reaping.
- The GET-listener check (`_can_deliver_push`, `server.py`) is the best available
  signal but false-POSITIVES on stale/zombie streams.
- Reconnect churn (hub restarts, /compact, network blips) leaves stale listeners the
  SDK doesn't always clean up.

**Safe interim (low-risk, doable anytime):** add a generous hard-cap to the reaper —
keep deliverable idle bindings, but reap one with ZERO activity for a long ceiling
(e.g. 6h) regardless of the gate. Can't drop a normally-active agent; bounds how long
a true zombie can linger. Caps the presence-lie, doesn't eliminate it.

**Proper fix:** a reliable "is this the live stream" signal. Needs experimentation
against REAL Claude Code sessions (can't be fully reproduced in unit tests). Candidate
directions: verify the bound session's write_stream IS the current GET transport;
detect socket close without the per-tool-call flapping that made `__aexit__`
subscription unusable; or a lightweight app-level liveness the client actually answers.

## Open Item 2 — De-dup without loss (recommend: LEAVE IT)

PR #8 stopped marking DMs read on push, which fixed silent loss but **re-introduced
possible duplicate delivery** (a live-surfaced push may also appear on the next inbox
pull). This is exactly what commit `236e502` was added to stop — and `236e502` is what
caused the loss. **"Proper dedup" is a trap:** to skip a message in the inbox you'd
mark it "surfaced-on-push," but push success ≠ surfaced, so you reopen the loss hole.
The only safe dedup is client-side (not ours). **Recommendation: leave the benign
duplicate. Do not re-add server-side dedup.**

## Hard-won lessons (the expensive ones)

1. **`push success != recipient saw it`.** `push_channel` returning True only means the
   notification was written to the bound stream. Never mark a message read / consume it
   on push. The inbox is the source of truth.
2. **Don't thrash deploys.** Every `master` merge auto-redeploys → wipes bindings →
   reconnect churn → stale streams. Repeated redeploys today caused the churn that
   turned a latent bug into a fleet-wide outage. Batch; deploy once; quiet window.
3. **Get the evidence before theorizing.** The decisive datum was the hub's
   `docker logs` (push outcomes + bind diagnostics), pulled by vps-admin. Time was
   wasted on three wrong hypotheses (per-session "wedge"; mark-read as the *root* vs
   the loss *mechanism*; misreading a manual nudge as a push). The "worked for weeks?"
   and "how did vps ping you?" questions were what cracked it.
4. **SEND is outbound (always works); RECEIVE/surface needs the live channel path.**
   An agent replying to you proves *its* outbound + *your* receive — not its receive.

## Debugging playbook — "agents aren't receiving messages"

1. `curl http://100.109.6.114:8090/health` — hub up? what commit? `agents_bound`?
2. `list_agents(include_offline=true)` — who's `🟢`/`⚡`/`⚫`?
3. Send a test DM to a known-good agent (FJ was reliable) — does it ack live? Isolates
   hub-wide vs per-agent.
4. If suspicious, get **`docker logs`** on the mcp-hub Coolify container: grep
   `push .*(gated|send failed)`, `reaper:`, `bind-diag`. 0 "gated" lines + no surfacing
   = the gate is false-positiving (Open Item 1).
5. Remember post-PR #8: messages are NOT lost — worst case delivered late via inbox
   pull / on relaunch. A relaunch gives an agent a clean stream (clears Open Item 1
   zombies for that agent).
