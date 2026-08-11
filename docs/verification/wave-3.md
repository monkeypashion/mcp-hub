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

## The queries the graph must answer — written BEFORE the schema is trusted

**Added by the 2026-08-11 design review (operator: "get this design absolutely
nailed"), which found the original design had a storage shape but no query
list — a graph not validated against its queries is a guess with good
vocabulary.** Derived from the operator's original ask: *the flow of messages,
decisions, status and progress between agents, visible as a lineage record.*

| # | Query | Demands |
|---|---|---|
| Q1 | How did this decision come about? | backward walk — index on **object** |
| Q2 | What happened as a result of X? | forward walk — index on **subject** |
| Q3 | Show the thread containing message M | connected walk over reply edges |
| Q4 | What did agent X contribute to Y? | index on **(predicate, subject)** |
| Q5 | Is item W done, and how do we know? | W3.5's refusal today |
| Q6 | How much traffic carries lineage? | the A9 coverage stat |
| Q7 | Which hub artifacts relate to ra.feature F? | external refs as nodes |

The API deliverable is shaped by these: a **bounded subgraph walk** (around a
ref, depth-limited, direction- and predicate-filtered) — never a whole-graph
dump, which answers none of the questions above and invites O(everything)
reads.

### Review verdicts on the five contested decisions (2026-08-11, Fable pass)

1. **Node identity = ref envelope: KEPT, STRENGTHENED.** Hub-native schemes
   (`hub.msg/1`, `hub.decision/1`, `hub.agent/1`) are registered FIRST, so the
   hub dogfoods its own envelope on every artifact; `ra.feature/1` is the
   fourth scheme. The mechanism demonstrably privileges nobody — the strongest
   form of the no-regrets constraint.
2. **`thread_id` denormalized cache: DROPPED — its justification was FALSE.**
   "A graph walk per Stop hook would be absurd" — but the Stop hook never
   walks the graph; no hot path does. Thread queries (Q3) are low-frequency
   and `WITH RECURSIVE` over an indexed edge table serves them at this hub's
   scale. The cache defended a nonexistent hot path at the price of this
   month's most-repeated failure mode: a copy drifting from the truth.
3. **`in_reply_to`: KEPT — but unusable as first specced.** An agent can only
   reply to a ref IT HAS SEEN, and nothing printed refs anywhere. Adoption is
   a property of the SURFACES, not the senders — hence A6 below.
   `DELIVERED-BEFORE` observational edges were considered and REJECTED:
   mechanical ordering read as causation is the feature_outcomes mirror with a
   politer name.
4. **RDF exporter: proposed DROPPED (operator approval pending).** The triple
   shape answers the capability question permanently; the exporter itself has
   no consumer today and was reviewer-invented scope.
5. **Never-infer: SURVIVES adversarial read.** Every auto edge is an act the
   hub itself performed. The soft spot — a DECLARED reply to a message the
   agent was never sent — is legitimate (get_history exists), so it is not
   refused; the edge's `source` column (`auto`|`declared`) carries the trust
   distinction to consumers instead.

Plus two items the query list forced: **edges are append-only and OUTLIVE
their artifacts** (the `api_seat_events` death-fact pattern — a purged
artifact's edges are history, and a dangling object ref MEANS "artifact gone");
and **no raw edge-write API exists in this wave** — auto edges ride existing
verbs, declared edges ride `in_reply_to` on existing verbs under the existing
`_attribution` gate. No new write surface.

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
| A1 | Migration adds a `lineage_edges` table `(subject, predicate, object, ts, source)` — **no `thread_id` column anywhere** (review verdict 2: the cache defended a nonexistent hot path). Indexes serve Q1/Q2/Q4: subject, object, (predicate, subject). Idempotent-by-exception like every prior ALTER | pending |
| A2 | Positive control first: a normal send/post/broadcast still delivers unchanged, with the incident suite (`test_broadcast_scope.py`, 27 tests) passing **UNCHANGED** — no edits to that file permitted by this wave | pending |
| A3 | Auto-inference tested per artifact type; a type that sets NO edges is reported `lineage_blind: true` in the API, never silently defaulted to a root node | pending |
| A4 | Subjects and objects are REFS in **canonical string form** — amended pre-build from sorted-key JSON to a URI-style form `scheme/ver?k=v&k=v` (keys sorted, values percent-encoded): refs are COPY-PASTED by agents into tool parameters, and JSON's nested-quote escaping there is an error factory, while rendered-tag bytes multiply by fan-out. ONE encoder, one parser, used for storage, display and input alike. Deliberate negative: the same ref with two field orders lands as ONE node, not two | pending |
| A5 | An unknown predicate is REFUSED naming the registered vocabulary; a self-edge is refused at WRITE time. A dangling object ref is legal ONLY as the death-fact of a purged artifact — edges are APPEND-ONLY and outlive their artifacts (`api_seat_events` precedent); purging an artifact never cascades into the graph | pending |
| A6 | 🔴 Refs are VISIBLE where messages are read — the rendered `<channel>` tag, `get_messages`, `get_history` each carry the message's ref. Deliberate negative: an agent replies with `in_reply_to` copied from its own rendered tag, and the edge lands. Without this the declared path is a parameter with no discoverable values, and the graph stays sparse by construction | pending |
| A7 | Backfill honesty: existing rows are NOT invented into threads. "No edge recorded" and "edge is the root" are distinguishable in the API; thread membership (Q3) is DERIVED at query time via a bounded recursive walk — cycle-free by A5's write refusals, depth-capped defensively anyway | pending |
| A8 | The read API is a bounded subgraph walk — around a ref, depth-limited, direction- and predicate-filterable. A whole-graph dump is not offered; it answers none of Q1–Q7 and invites O(everything) reads | pending |
| A11 | No raw edge-write API exists: auto edges ride existing verbs, declared edges ride `in_reply_to` on send/post/broadcast under `_attribution`. Test: the route table exposes no standalone edge writer | pending |
| A-RDF | RDF/JSON-LD exporter — **DROPPED, operator-approved 2026-08-11** ("so long as we are capturing RDF triple style we can always create an exporter later" — condition holds: storage IS `(subject, predicate, object)`, so a future exporter is a serializer over existing data, no migration). Revisit when a consumer exists | **DROPPED** |

### How edges get POPULATED — the three paths, and the one that stays unbuilt

Operator's question, 2026-08-11: *what triggers the information to be
recorded?* The split below **is** the design, so it is part of the bar.

**① AUTO — what the hub knows with certainty at write time.** No caller
change, no inference. `AUTHORED-BY` (from `from_agent`, already gated by
`_attribution`), `ADDRESSED-TO` (the routing decision just made), `RESOLVES`
(`decision_resolve` closes the agent's one open card — the hub owns that
lifecycle), `SUPERSEDES` (`decision_put` upserts; the hub did the replacing).
⚠️ `DELIVERED-IN` (drain-batch grouping) is DROPPED with its reason named: a
drain batch has no identity — no artifact exists to be the object of the edge,
and minting a synthetic one to carry a mechanical grouping is exactly the
dense-but-meaningless shape verdict 3 rejected. Revisit if wake events ever
become artifacts.

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
| B5 | ⭐ HUB-NATIVE SCHEMES FIRST (review verdict 1): `hub.msg/1`, `hub.decision/1`, `hub.agent/1` register through the SAME adapter interface before any external scheme — the hub dogfoods its own envelope on every artifact, and `ra.feature/1` arrives as the fourth scheme, demonstrably unprivileged | pending |
| B6 | Scheme version ≠ item version pin, kept apart by construction: `ra.feature/1` names the CONTRACT version; a ref carrying a `version` field pinning the ITEM is refused (FJ rule 2). A test exercises the confusable case: envelope with scheme `ra.feature/1` AND an item `version` key → refused naming rule 2 | pending |

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

**UNBLOCKED 2026-08-11: FJ pasted §4 verbatim, re-verified byte-identical from
`04a4255` through current head `7b2e0eb` — cite `7b2e0eb` in every test.** The
rules, exactly as received:

1. ⛔ A ref missing either half of the pair. No key-only, no id-only.
2. ⛔ A ref carrying `version` as a pin. Display-only or absent.
3. ⛔ A ref resolving status against any repo copy. Intent ≠ state.
4. ⛔ A ref that does not name which document and which status vocabulary it read.
5. ⛔ An unknown `feature_set_key` — fail closed. Unknown means *unknown*; it
   must never be read as *new* and must never auto-mint a lineage.
6. ⚠️ A pair resolving to more than one feature must fail loudly, not pick one —
   uniqueness is unenforced upstream (§1.2), so the hub is the first place that
   can notice.

⚠️ **FJ's fit-check, adopted before build: rule 4's subject is the RESOLVED
ANSWER, not the ref.** A ref names a work item; the *resolution* is what must
declare document + vocabulary. Rule 4 therefore lives on the **response side**
(W3.5 E2), not in the envelope schema — building it into the ref would have
put the rule in a place it cannot do its job. Rule 4 is also **two-part**
(document AND vocabulary — implementable as one check that silently loses
half) and there are **FOUR** status vocabularies, not three (`FeatureStatus`
adds `skipped`).

| # | Item | Status |
|---|---|---|
| D1 | All six rules implemented verbatim, each test citing `7b2e0eb` | pending |
| D2 | One deliberate negative per rule, exercised in the state that provokes it. Rule 4's negatives are TWO — missing document, missing vocabulary — since a single combined check can pass while enforcing half the rule | pending |
| D3 | Rule 5 gets an explicit never-infers test: an unknown `feature_set_key` neither resolves as new NOR auto-mints lineage — both halves asserted | pending |
| D4 | Rule 6's deliberate negative uses a document with duplicate ids planted directly (RA's `load()` now refuses them at construction, so the state is built the way W1.1 built API-unreachable states — stated in the docstring) | pending |

## W3.5 Status resolution — fails closed

Per FJ's reframe, adopted as the design: **"no blessed target exists" is a
buildable state.** Refusing to resolve is correct behaviour today, so the
refusal ships now and the resolver arrives later as a *registered target*
rather than a rewrite.

| # | Item | Status |
|---|---|---|
| E1 | "Is this feature done" REFUSES while no target is registered, naming why — never infers from the authored document | pending |
| E2 | The refusal names document AND vocabulary once a target exists; a resolution attempt lacking either is refused | pending |
| E3 | 🔴 `feature_outcomes` is NOT registerable as a target by accident: a target whose writer derives status from the input document is refused with that reason named. Evidence on file — it inserts `COALESCE(v_feature->>'status','not_attempted')` from `p_features_json`. ⚠️ Standing condition from FJ: when dt's repair repopulates the table, rows appearing is NOT the registration signal — **dt's mirror-detector passing is** | pending |
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
