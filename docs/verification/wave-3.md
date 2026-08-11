# Wave 3 verification bar — the intent plane

**STATUS 2026-08-11: bar committed, build not started. Branch `wave-3`, which
also carries Wave 2's held-back `ca0dabe`.**

Committed BEFORE the build (the meta-rule: the build checks against a committed
bar; it does not write its own exam afterwards). Every item ends the wave as
**MET** (evidence named), **NOT-MET** (named), or **DROPPED**
(operator-approved). No silent scope narrowing.

## Scope — operator-set, 2026-08-11

🔴 **BACKEND ONLY. API first; UI comes later.** No board work, no rendering, no
cockpit affordance in this wave. The lineage graph is **data** — nodes, edges,
and a query API over them — not a picture.

One consequence, recorded because it changes a deliverable: the plan said
artifact types that set no edges are "marked lineage-blind **on the surface**".
With no UI, that becomes a **field in the API response**, not a glyph. Better
anyway — machine-readable beats human-readable for a fact another tool must act
on, and it means the marking is testable without a terminal.

## Disciplines binding every item

Unchanged from Waves 1–2, which earned them: **enforces-not-checks** ·
**mutation gate** (each enforcement names a mutation and the test that kills
it) · **deliberate negative** (every refusal exercised in the state that
provokes it) · **positive control** (prove the harness can see a success before
trusting any refusal) · **absence ≠ health** · **never measure through your own
deploy** · **CI green on the branch is part of the bar**.

Carried in from Wave 2's close, each of which cost something to learn:

- 🔴 **Commit before mutating.** A mutation harness reverting with
  `git checkout --` destroyed uncommitted work; tracked ≠ committed.
- 🔴 **No module-scope timestamps in fixtures.** `FRESH = time.time()` against a
  120s window and a 7-minute suite passed 23/23 alone and 9/23 in the suite.
  Parametrize lists are worse — they evaluate at COLLECTION time.
- 🔴 **Run the named mutations at close; do not trust the ledger.** Doing this
  found a test that claimed to plant guard-failing content and planted
  `brief="fine"`.
- 🔴 **A surviving mutation questions the TEST first.** It may be a vacuous
  test, a correct redundancy, or a broken mutation — three different verdicts,
  and only one is a code gap.
- 🔴 **Enumerate before you count.** An "empty fleet-wide" claim that cost this
  wave a day of re-derivation came from one database of eight. Any claim about
  a population must name the population it covered.

## The wave's own hazard, stated up front

This wave builds the layer that answers *"is this work item done?"* — so a
wrong answer here is not a bug, it is **a confident false report about
delivery**. Two failure shapes are specifically in scope and each gets a
refusal rather than a best guess:

- **Resolving to the wrong thing.** RA's `load()` silently collapsed duplicate
  feature ids last-wins, so a stored ref to the shadowed record still
  *resolved* — just to the wrong feature. Resolution succeeded, so nothing
  surfaced. Fixed at RA's end (`95d3cac`), and this layer must not re-create it.
- **Mirroring a claim instead of observing it.** `feature_outcomes` derives
  status from the authored document, so it agrees with that document exactly
  when the document is wrong. It is therefore NOT a valid completion target at
  any row count — which is why W3.5 refuses rather than resolves.

## W3.1 Lineage — a TRIPLE-shaped edge store

**Design changed 2026-08-11 on the operator's question ("will it support RDF
triples?"), before any code — the original spec was `thread_id` + `parent_id`
columns.** Recording why, because the reasoning is the load-bearing part:

`parent_id` is a **single typed edge**. It can express "replied to" and nothing
else. The stated goal is the flow of *messages, decisions, status and progress*
between agents — which is inherently multi-predicate: `supersedes`, `cites`,
`authored-by`, `answers`, `derived-from`. A one-edge model forces every new
relationship to become a new column, and the storage shape is the expensive
thing to change once consumers read it.

⇒ **Storage is `(subject, predicate, object)` — the triple shape.** What it
deliberately does NOT adopt is the rest of RDF: no SPARQL, no ontology
reasoning, no IRI dereferencing. Those are a research project; the shape is
free. **RDF/JSON-LD becomes a SERIALIZATION concern** (a triple maps to one
directly), so interop stays available without buying the machinery now.

⭐ It also composes with W3.2 rather than sitting beside it: **subjects and
objects are REFS**, so the same versioned envelope that identifies an
`ra.feature/1` item identifies a hub message or a decision. One identity
mechanism, not two — and a scheme registered for refs is immediately usable as
a graph node with no extra work.

⇒ **Predicates come from a registered vocabulary and an unknown one is
REFUSED**, exactly as an unknown scheme is (B3) and exactly as status
resolution refuses (E1). Same fail-closed rule in all three places, which is
also FJ's insistence that a resolution name its vocabulary.

`thread_id` survives as a denormalized grouping column for the hot
"conversation" query — a graph walk per Stop hook would be absurd — but it is a
CACHE of the edges, never the truth, and a test asserts the two agree.

| # | Item | Status |
|---|---|---|
| A1 | Migration adds a `lineage_edges` table `(subject, predicate, object, ts, source)` plus the `thread_id` grouping column, idempotent-by-exception like every prior ALTER. An artifact with no edges reads as **lineage-blind**, never as a root | pending |
| A2 | Positive control first: a normal send/post/broadcast still delivers unchanged, with the incident suite (`test_broadcast_scope.py`, 27 tests) passing **UNCHANGED** — no edits to that file permitted by this wave | pending |
| A3 | Auto-inference tested per artifact type; a type that sets NO edges is reported `lineage_blind: true` in the API, never silently defaulted to a root node | pending |
| A4 | Subjects and objects are REFS using W3.2's envelope — the same identity mechanism, asserted by a test that a `test.dummy/1` ref can be a graph node with zero core changes | pending |
| A5 | An unknown predicate is REFUSED naming the registered vocabulary; a dangling object ref and a self-edge are refused at WRITE time, not returned to the caller to trip over | pending |
| A6 | `thread_id` is a cache, not the truth: a test asserts it agrees with the edges, and disagreement is detectable rather than silently trusted | pending |
| A7 | Backfill honesty: existing rows are NOT invented into a thread. "No edge recorded" and "edge is the root" are distinguishable in the API | pending |
| A8 | RDF/JSON-LD export of a subgraph round-trips subject/predicate/object without loss — serialization only; no SPARQL, no ontology, no IRI resolution in this wave (stated limit, not a gap) | pending |

### How edges get POPULATED — the three paths, and the one that stays unbuilt

Operator's question, 2026-08-11: *what triggers the information to be
recorded?* The split below **is** the design, so it is part of the bar.

**① AUTO — what the hub knows with certainty at write time.** No caller
change, no inference. `AUTHORED-BY` (from `from_agent`, already gated by
`_attribution`), `ADDRESSED-TO` (the routing decision just made), `RESOLVES`
(`decision_resolve` closes the agent's one open card — the hub owns that
lifecycle), `SUPERSEDES` (`decision_put` upserts; the hub did the replacing),
`DELIVERED-IN` (drain-batch wake grouping).

**② DECLARED — what only the sender knows.** An optional `in_reply_to=<ref>`
on `send`/`post`/`broadcast`. **Omitted means lineage-blind, never root.**

**🔴 ③ NEVER INFERRED — and this is the load-bearing refusal.** The hub cannot
know what a DM replies to: there is no reply-to today, and it sees an ordered
stream, not a tree. *"Probably answers the previous message from the other
party"* is a heuristic that is wrong whenever an agent answers an older
message, bundles several, or replies to a broadcast mid-DM. **A guessed causal
edge is the `feature_outcomes` disease in new clothing** — a record that mirrors
a plausible story instead of observing one, confidently wrong exactly where the
chain matters. It stays unbuilt, and a test asserts no such inference happens.

⚠️ **Accepted consequence, stated now rather than discovered at review: the
graph starts SPARSE.** Decisions get rich lineage immediately; messages get
parentage only as agents adopt `in_reply_to`. Sparse-and-true beats
dense-and-invented — but the sparseness must be VISIBLE, not assumed, hence A9.

| # | Item | Status |
|---|---|---|
| A9 | The API reports lineage COVERAGE — what fraction of artifacts carry declared edges — so a thin graph reads as *thinly populated*, never as *thinly connected*. Absence ≠ health, applied to our own new surface | pending |
| A10 | 🔴 Deliberate negative: a DM with no `in_reply_to`, sent immediately after another agent's DM, produces **NO parent edge**. Mutation: add a "previous message from the other party" heuristic → this test fails | pending |

## W3.2 Ref envelope + scheme registry

The operator's no-regrets constraint, and RA endorsed it from the far side:
*"I'd rather be one scheme among several than the assumed default."*

| # | Item | Status |
|---|---|---|
| B1 | Envelope is `{scheme, ...}`, versioned; core imports NO methodology semantics | pending |
| B2 | 🔴 THE NO-REGRETS TEST, asserted in CI rather than intended: a dummy `test.dummy/1` scheme registers through the adapter interface with **zero diffs to core files**. The test fails if adding a scheme requires touching core | pending |
| B3 | An unknown scheme is REFUSED, naming the registered schemes — never resolved by a default | pending |
| B4 | A malformed or version-less envelope is refused; `ra.feature/1` and a hypothetical `ra.feature/2` can coexist in the registry | pending |

## W3.3 `ra.feature/1` resolver

Gate CLOSED by RA at `95d3cac`; contract citable as
`reliable_ai.progress.features.FEATURE_ID_CONTRACT`.

| # | Item | Status |
|---|---|---|
| C1 | 🔴 The identifying pair is **`(feature_set_key, feature.id)`**, scoped not global. An envelope carrying an id ALONE is refused, not resolved — the same `feature.id` in two sets is two different features | pending |
| C2 | 🔴 Deliberate negative with a real name: **never derive `feature_set_key` from a repo path or name.** `dreamteam-analytics-service` carries `feature_set_key: "analytics-service"` — a live counter-example, used as the fixture | pending |
| C3 | `DuplicateFeatureIdError` (raised at `FeatureList.__init__`, not at an explicit `load()`) is a DISTINCT outcome from "ref not found" — a corrupt document must not be reported as a missing feature | pending |
| C4 | Positive control: a well-formed ref against a clean feature set resolves, so every refusal above is a contract verdict rather than an instrument failure | pending |

## W3.4 FJ's six refusal rules

⚠️ **BLOCKED ON INPUT, and named as such rather than started.** I have been
saying "implement verbatim" about a document I have never read — it lives in a
repo not on this box. Requested from FJ 2026-08-11. **No code until the exact
wording is in hand**; implementing a summary of a refusal contract is the
error this wave exists to prevent.

| # | Item | Status |
|---|---|---|
| D1 | All six rules implemented verbatim, each citing the contract commit in its test | pending — awaiting text |
| D2 | One deliberate negative per rule, exercised in the state that provokes it | pending — awaiting text |
| D3 | Rule 5 (unknown ⇒ fail closed, never infer) is the one W3.5 leans on; it gets an explicit test that inference does NOT happen | pending — awaiting text |

## W3.5 Status resolution — fails closed

Per FJ's reframe, adopted as the design: **"no blessed target exists" is a
buildable state.** Refusing to resolve is correct behaviour today, so the
refusal ships now and the resolver arrives later as a *registered target*
rather than a rewrite.

| # | Item | Status |
|---|---|---|
| E1 | "Is this feature done" REFUSES while no target is registered, naming why — never infers from the authored document | pending |
| E2 | The refusal names document AND vocabulary once a target exists; a resolution attempt lacking either is refused | pending |
| E3 | 🔴 `feature_outcomes` is NOT registerable as a target by accident: a target whose writer derives status from the input document is refused with that reason named. Evidence on file — it inserts `COALESCE(v_feature->>'status','not_attempted')` from `p_features_json` | pending |
| E4 | Registering a target is a deliberate act with a test proving an UNREGISTERED hub answers "unresolvable", not "not done" — those are different claims and conflating them is a false delivery report | pending |

## W3.6 Wave close

| # | Item | Status |
|---|---|---|
| F1 | Full suite + ruff green on the branch; CI green on the branch head before merge | pending |
| F2 | Every named mutation applied, verified failing against its named test, reverted; ledger filled. Mutations are RUN at close, not trusted from their commits | pending |
| F3 | Live bars after the deploy settles, from a re-registered session: lineage edges present on real hub traffic; an unknown scheme refused by prod; "is this feature done" refused by prod with no target registered | pending |

Note: this wave's deploy also carries `ca0dabe` (Wave 2's F3 statuses), held
back deliberately so a docs-only commit did not cost the fleet a rebind.

## Mutation ledger (filled at wave close)

| Mutation | Killed by |
|---|---|
| _(filled during the build)_ | |
