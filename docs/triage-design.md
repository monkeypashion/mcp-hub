# Decision cards — the operator-triage currency

*Design note, 2026-07-26. Status: phase 1 SHIPPED (hub store + stop-hook
ingestion + answer leg). Owner: mcp-hub lane. Operator + pm iterate here.*

## The idea (operator's, verbatim intent)

Every input that needs the operator's judgement — an agent's ask, a
suggestion-service item, a test-fleet finding — is converted into ONE
currency: the DECISION card. Triage machinery then ranks, presents, and
routes answers for cards, regardless of where they came from.

## The card (authoring format, v2)

```
**DECISION**
**ASK:** <what you want, one sentence>
**WHY:** <one sentence>
**VALUE:** <what it buys, one sentence> [<v>/10]
**RISK:** <what it costs if wrong, one sentence> [<r>/10]
**TAGS:** deploy, spend, security, design, ops   (optional)
```

- One sentence per field; a field that wraps twice has failed.
- **Two-scale scoring**: VALUE and RISK are scored separately (0–10);
  **net = value − risk is COMPUTED by the hub**, never asserted — an
  author argues the components, the machinery does the arithmetic.
  Legacy single `**SCORE:**` cards still parse (score becomes net).
- Tags: small controlled vocabulary, lowercase. Most metadata is
  **derived, not declared** (agent, project, timestamps, source) — never
  ask a model to hand-tag what the machinery already knows.

## Storage

`decisions` table in the hub's SQLite (`/data/mcp-hub.db`) — see
`init_db()` in `server.py`. Cards are stored PARSED (plus raw text), so
triage ranks without re-parsing prose. `status`: `open → decided`
(operator answered) or `open → withdrawn` (agent moved on).

**One open card per agent** (the "one live DECISION at a time"
convention): `decision_put` upserts the agent's open card, so a restated
ask updates in place — no duplicates, age preserved from first submission.

## Ingestion — two edges, zero model compliance beyond authoring

1. **Agents (stop hook)**: the hook reads the transcript tail at every
   Stop. Card at the end of the last turn → `decision_put` (hand up
   fleet-wide within seconds of the turn ending). No card in the last
   turn → `decision_clear` (withdrawn). Authoring is hardened by the
   **card nag**: waiting-on-operator language with no card earns a
   one-line correction at that exact Stop (one shot — the
   `stop_hook_active` backstop prevents loops).
2. **Services (API)**: call `decision_put(source="api", ...)` directly.
   Service cards are NEVER auto-withdrawn by stop-hook clears — only the
   submitting service (or an operator answer) closes them.

Discipline: cards are for decisions the operator must make. FYIs and
tasks are not cards — a queue only stays valuable if everything in it
genuinely needs the operator.

## The answer leg (hand-down)

- **Explicit** (triage front door): `decision_answer(decision, card_id |
  agent, note)` closes the card AND DMs the verdict to the asker over the
  normal wake path. No relays, and the decided row is the paper trail.
- **Ambient** (zero ceremony): operator answers in the agent's pane → the
  agent's reply turn carries no card → its stop hook withdraws it.
  Whichever happens first wins.

## Sensitivity

Hand-up: turn end → stop hook (~1–2s) → hub record; display beats add a
few seconds. Hand-down: immediate on `decision_answer`; one agent-turn
after an in-pane answer.

## Shipped alongside (same batch — the context-tax bundle)

- Live push renders (channel tags) clip at 700 chars — measured
  2026-07-26 as the dominant context tax (840KB/day fleet-wide, 3.4× the
  stop-hook path, zero economy until now). Full text stays in the inbox
  (`get_history` is the lossless path, as ever).
- Stop-hook "Discipline reminder" footer removed (~20% of hook bytes).
- Sender verbosity advisory: >1.5KB message with no summary-shaped first
  line → one 📏 line in the SENDER's tool result only.

## Phases ahead (not built, in rough order)

1. **Fleet-wide display from the hub**: board/statusline read
   `decision_list` instead of (only) local transcript harvest — remote
   boxes' hands become visible; local harvest stays as fallback.
2. **Cockpit answer UI**: cards rendered in NEEDS YOU with yes/no/defer
   actions wired to `decision_answer`.
3. **Project assignment**: nullable project/initiative field + tag.
4. **Score calibration**: track per-agent yes-rate vs claimed net;
   surface systematic over/under-claiming.
5. **Service onboarding**: suggestion service, test fleet, factory
   services submit via `decision_put(source="api")` with their own
   tag conventions.
