# Seat control + live view — one design (console card #144)

**Status: DESIGN, operator review pending. Nothing here is built.**

The console's Squads tab gains the cockpit's powers over a seat — watch it
work live, interrupt it, send it a prompt, answer its dialogs, restart or
stop it — **without the console ever holding a shell or a key to any
machine**. The one idea, borrowed from the placements pattern that already
drives this fleet: **the console records INTENT; the machine that owns the
seat carries it out and reports what it OBSERVED.**

## Why not a remote shell

Every control here is tmux keystrokes or docker verbs on the seat's machine.
Giving the console a path that executes those directly would make the
console a shell multiplexer holding power over every machine — one
compromised console away from the fleet. Intent records invert that: the
console can only *ask*, each machine's own edge (authenticated by its own
machine token) *acts*, and the record carries what actually happened. The
console's writes are operator-token gated, same as every /api/v1 mutation.

## Control leg — seat actions as records

New surface: `POST /api/v1/seats/<identity>/actions` (operator token).

```json
{ "kind": "interrupt | prompt | answer | restart | stop",
  "args": { "text": "...", "mode": "resume|fresh", "answer": "yes|no|always" } }
```

One row per action: `id, seat, kind, args, requested_at, requested_by,
status (pending | done | failed | refused | expired), observed (json),
pane_after (text)`.

- **The edge realizes actions** for seats placed on its machine, on the same
  timer + doorbell that realize placements (~1s with the doorbell): interrupt
  = Escape to the seat's tmux; prompt = literal text + Enter; stop/restart =
  the placement verbs it already owns (restart "fresh" = container cycle;
  restart "resume" = cycle + `--continue`, which the seat image must be
  taught — noted as the one image change this design needs).
- **`answer` is fail-closed, exactly like the cockpit**: the edge parses the
  visible dialog options from a pane capture first, and REFUSES with the
  pane text when it cannot read a dialog — a blind keypress wearing the
  word "answer" is the guests-menu lesson and it is not negotiable.
- **Every outcome carries a pane capture** (`pane_after`), so the console
  shows what the seat looked like after the action — observed, not assumed.
- **Intent expires.** A pending action older than its TTL (default 120s)
  goes `expired` and never fires: an interrupt written during a stall must
  not land minutes later in the middle of healthy work. Stale intent is a
  new hazard class this design refuses up front.
- **One pending action per seat** (upsert semantics): mashing the button
  re-states, never queues five interrupts.

## Watch leg — the live view

`POST /api/v1/seats/<identity>/watch` (operator token) declares a viewer,
with the open-now pattern's 180s window: while any viewer is declared, the
seat's edge streams pane captures (`tmux capture-pane`, ~2s cadence) to
`POST /api/v1/seats/<identity>/view`; the console reads
`GET /api/v1/seats/<identity>/view` (long-poll/SSE relay from the hub).

- **View-on-demand, never always-on**: no viewer declared → the edge sends
  nothing. A fleet permanently streaming every pane is cost and exposure
  with no reader.
- **The pane is the view** — the same text I read when the operator asked
  "is it stuck": current step, tool calls, subagent activity, dialogs. No
  transcript parsing, no new instrumentation in the seat; what the cockpit
  operator sees is what the console operator sees.
- Pane text can contain anything the seat printed. It is shown to the
  operator (who already owns everything the seat does) — but it is stored
  ring-buffer-only (latest N captures), never as a durable transcript: the
  memory volume already owns durable history.

## Phasing (the operator's "not all straight away")

1. **Phase 1: watch + prompt + interrupt** — the three the operator reached
   for today, and the smallest honest loop (see it stuck → nudge it → see
   the result).
2. **Phase 2: answer** (needs the fail-closed dialog parser ported from the
   cockpit) **+ restart resume/fresh** (needs the seat image's `--continue`
   leg). Stop needs nothing — it already exists as a placement verb the
   console routes today.

## What this deliberately does not do

- No arbitrary keystroke pass-through, no shell, no exec API — the verb set
  is closed, each verb individually reviewable.
- No cockpit replacement: the cockpit stays the on-machine surface; this is
  the same *vocabulary* reaching the console through records.
- No agent-writable surface: seats cannot write actions at each other;
  operator token only, same stance as feature-set registration.
