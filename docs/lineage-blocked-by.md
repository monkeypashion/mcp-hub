# BLOCKED-BY — a forward-looking lineage predicate (design)

**Status: DESIGN, commissioned 2026-08-26 (squad-proxy, on the operator's
forward-path thread). Nothing here is built.**

One new registered predicate: `blocked-by`, subject-is-blocked-by-object —
"this cannot start until that clears". Everything else in the vocabulary
records a past fact; this is the first edge about the future, and that one
property drives every decision below.

## Why it exists

The operator's corrected requirement (2026-08-26, verbatim): *"I care much
more about looking forward and seeing a well defined path than looking
back … I do want to understand relationships but only in the name of
quality and speed of production."* Today the console shows steps in a
running order someone typed, with a flag for what is waiting. It cannot say
step 4 is stuck **because** step 2 never finished, because nothing records
that. One predicate turns a sequence into a path.

## The hard constraint first: this edge can become FALSE

Every current predicate (authored-by, addressed-to, replies-to, resolves,
supersedes) records a past fact that stays true forever. "Cannot start
until that clears" stops being true the moment it clears — and the hub
**refuses to infer completion** (`resolve_status`'s whole design: a store
that copies a claim agrees with it exactly when it is wrong), so it cannot
infer un-blocking either. Without an explicit clearing mechanism every
blockage is a **fossil pointing forward**: the path view shows work stuck
behind things that finished long ago — confidently wrong in exactly the
direction the operator cares about, and strictly worse than the
sequence-with-a-flag it replaces.

So: **a blocked-by edge is not one fact, it is a pair of declarations with
a lifecycle** — `declared` → `cleared` — and the design treats the clear
as first-class, not as cleanup.

## Authority — who may declare

**Only the lane that OWNS the blocked work, about its own work, and the
same authority clears it.** Self-reported, `source=declared`, weighted
below hub-witnessed — exactly `in_reply_to`'s shape.

The refused alternative: letting any lane assert blocked-by about anyone's
work turns the path view into a surface where one lane can paint another
stuck. A wrong "you are blocked" written by a third party is not lineage,
it is an accusation with a timestamp.

The operator (operator token, `/api/v1`) may declare or clear on any
subject — the board is his; that write is `source=operator`.

## Transport — which verb carries it

**No raw edge-write API** stays true. The declaration rides existing
verbs as a parameter, the way `in_reply_to` does:

- `send` / `post` / `broadcast` gain `blocked_by="<subject-ref>|<object-ref>"`
  (the message is the *witness*; the edge lands between the two refs it
  names, not on the message).
- `decision_put` gains the same — a card that says "waiting on X" can
  declare it as data in the same breath.
- Clearing is the same parameter with a `clear:` prefix
  (`blocked_by="clear:<subject-ref>|<object-ref>"`), same authority check.

**Malformed or unresolvable refs refuse the send loudly** — the
`in_reply_to` precedent verbatim. A silently dropped edge lies by
omission; a declaration against a ref nobody can resolve is a typo wearing
a path's clothes.

## Lifecycle rules

1. A cleared edge is **kept, marked cleared** (`cleared_at`,
   `cleared_by`) — never deleted. History stays queryable; the path view
   simply stops routing around it. (A vanished edge is indistinguishable
   from one never declared — the seat-actions `expired` lesson.)
2. **Re-declaring an existing live edge is idempotent** (upsert, seat-action
   style): mashing "still blocked" must not accumulate five edges.
3. Declaring blocked-by on a NEW object while an old edge stands does NOT
   supersede it — work can genuinely wait on two things. Each clears
   independently.
4. **Staleness is rendered, never resolved away.** `get_lineage` output
   carries `declared_at` on every live blocked-by edge; the console renders
   the age ("blocked by X — declared 6d ago"). An old uncleared edge is a
   *visible* question for its owner, not a hidden falsehood — the same
   move as the usage file's `resets_at`: the reader is given the clock,
   the store never guesses.

## What this deliberately does not do

- **No inference.** The hub never writes or clears a blocked-by edge from
  observed behaviour, message traffic, or a target's apparent completion.
  Declared in, declared out.
- **No status claims.** `resolve_status` still refuses. A live blocked-by
  edge means "its owner has not said otherwise", never "the hub knows X is
  unfinished".
- **No general extensibility.** This is ONE registered predicate, argued;
  the vocabulary stays closed ("a vocabulary nobody can extend by typo is
  the point").

## Build shape (when approved)

Schema: `cleared_at REAL`, `cleared_by TEXT` columns on the existing edge
table (nullable — NULL means live; the absent-vs-empty rule as ever).
Parser + authority check in the three verbs; `get_lineage` gains
`include_cleared=` (default false for the path view, true for history).
Tests-first; the mutants that matter: authority bypass (third-party
declaration accepted), clear-forgets-history (DELETE instead of mark),
staleness hidden (declared_at dropped from output), silent refusal on a
bad ref.
