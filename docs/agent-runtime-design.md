# Agent Runtime — design

*Draft v0.1, 2026-07-29. Uncommitted on purpose: committing to master deploys.
Inputs: operator's three threads (2026-07-29) + channel reconnaissance in
`#agent-runtime` from spike, reliable-ai, features-json and mcp-hub-dev-vm-1 —
every constraint below cites a measured specimen, not a principle.*

## One line

An agent stops being a folder on a box and becomes a **schedulable unit** —
`repo@commit + task + memory + history + identity` — that can be **placed** on
a substrate (worktree, container, remote host), **run**, and **reclaimed**.

## Vocabulary (operator-ratified, 2026-07-29)

| term | means | key property |
|---|---|---|
| **seat** | folder + identity + memory + history on one machine | *the* unit of agenthood; one folder = one seat; memory is keyed on the folder path, so it belongs to the seat and nothing else |
| **repo** | the git project (`org/repo`) | many seats can check out one repo |
| **workspace** | a `.code-workspace` file — a *list* of seats | a view; owns nothing, copies nothing; typing one (squad_workspaces) makes it a squad |
| **squad** | broadcast circle derived from squad-typed workspaces | membership is per (seat, squad); scoping is delivery walls, not confidentiality |
| **listing** | a seat appearing in a workspace | cheap: same mind, more ears (multi-squad = one seat, several listings) |
| **clone** | a *new seat* forked from an existing one via transport/duplicate | own memory + history from the fork point, suffixed identity, self-identifying marker |
| **twin** | seats of the same repo on *different machines* | independent histories, reconciled by the memory-sync ceremony |
| **unit** | a task whose completion criteria are mechanically verifiable | what the runtime places on a seat |

The two distinctions that carry the weight: **listing vs clone** — subscribe an
existing mind vs create a new one; **clone vs twin** — a deliberate fork vs
same-repo peers that grew apart naturally. A workspace can never duplicate a
seat's memory, because memory is keyed on the seat's folder path; only making
a new folder (a clone) forks it.

## Why this is a continuation, not a leap

`squad transport` already moves all five components of a unit, and it needs
`transport-recv` at the destination because the source structurally cannot
compute the destination's facts (paths, hostname, derived name). transport-recv
**is the edge daemon in embryo** — currently invoked over SSH instead of
resident. The differentiator we own is carrying **memory, history and identity**
with the code; container scheduling itself is a solved problem (Nomad/k8s) we
must not rebuild.

## The organizing finding (reconnaissance, 2026-07-29)

**Everyone has dispatch; nobody has acceptance.**

- **features-json**: a *feature* (not a file) is unit-of-work shaped — id,
  routing tags, phase ordering, file scope, all ajv-enforced at a real intake
  gate (pre-commit + three `f4-local.js` call sites, verified at `ec8eb19`).
  But `status`/`tests_pass` are **self-asserted** and `validation`
  (test_command + success_criteria) is **optional**: an agent can write
  `status: completed` and satisfy the schema.
- **reliable-ai** (verified against a fresh test run): rubrics (50 tests), an
  HTTP enforcement server (35 tests), a featurelist completion state machine
  that travels **in-tree** with the repo. But *"nothing in reliable-ai today
  proves execution from independent enumeration"* — the completion gate trusts
  the runner's report. **The enforcement layer is tested; the evidence layer is
  the gap.**
- **spike**: six constraints (below), each from a fleet specimen ≤14 days old,
  all of which are demands on exactly that missing evidence layer.

So the runtime's core deliverable is not scheduling. It is the **evidence
contract**.

## Founding principles

1. **The brief is the gate.** (features-json, from `f4-local.js:3173`.) One
   artifact teaches the generator *and* admits its output — spec and acceptance
   test as the same object cannot drift apart. Whatever briefs an ephemeral
   agent must be what admits its result. Two artifacts drift; one cannot.
2. **A unit of work is the thing whose completion criteria can be mechanically
   verified.** (reliable-ai.) If a unit can't state its completion criteria,
   it isn't a unit yet.
3. **Reported state is never authoritative state.** `status: completed` is a
   *report*; acceptance verifies against the substrate.

## The evidence contract (spike's six, adopted verbatim as requirements)

1. **A success report must prove the work ran.** An assertion over an empty
   set is a hard error, not a pass. (Specimen: a held-out test green while
   asserting nothing — 202 accepted, write suppressed, verifier true over an
   empty range.)
2. **Nothing may assert its own state.** Reclaim and existence are verified by
   enumerating the substrate, never by reading a record that says "reclaimed".
   (Specimen: pod serving HTTP 200 while the registry held its death fact.)
3. **Reclaim by artifact with a refcount, never by owner.** Ephemeral
   containers share more than expected: networks, volumes, caches, quota, DBs.
   (Specimen: 8 projects on one GCP tenant; per-project reap = destroy on
   delete #1, orphan seven.) Cheapest now, dearest to retrofit.
4. **Liveness = progress, not process-alive; budget in money, not only
   wall-clock.** (Specimen: tool-calls stopped, cost still ticking; 300s
   zombie threshold validated empirically.)
5. **Kill has a loud terminal "could not reclaim" state.** SIGTERM → SIGKILL →
   force-resolve-as-orphan; the third outcome pages someone, never resolves
   quietly.
6. **The blindness test.** Every probe runs beside a positive control it is
   *known* to detect. A reclaim-checker that has never reported a live leftover
   is not yet a checker.

## The unit lifecycle

```
DEFINE   brief = spec + mechanically-executable acceptance (one artifact)
PLACE    edge daemon materializes: worktree/container, repo@commit, memory
         seed, assigned identity, budget (money + wall-clock + progress SLA)
RUN      progress signal measured (not process-alive); enforcement hooks
         (reliable-ai HTTP server) gate actions live
ACCEPT   the brief's own gate admits the output, verified against substrate
         enumeration — reported status is input, never verdict
RECLAIM  = HARVEST + VERIFY + DESTROY
         harvest: memory delta exported to hub staging (work product —
                  a clone whose learnings die with the container is the
                  vacuous-green of scheduling); history preserved
         verify:  substrate enumerated empty, positive control ran
         destroy: refcounted artifacts released; identity retired
         third outcome "could not reclaim" is terminal and loud
```

## Identity

- **Assigned over derived.** Derivation (git remote + hostname) collides or
  churns in containers. Precedent exists and is shipped: the per-worktree
  suffix registry in `~/.mcp-hub/config.json` is already an assigned override
  on top of derivation. Generalize it; `mcp-hub identity --cwd` remains the
  single read path.
- **Retirement is required.** The hub cannot delete messages or channels
  today; cycling ephemeral identities without retirement/GC leaks forever.
  Retention has two customers now: messages and identities.

## Edge daemon — **authoritative-and-reconciling. Decided.**

Two independent same-day measurements, same conclusion from both directions:

- Hub-side: url-rebind provably cannot rescue a client that never reconnected
  (prod-1 access log, 2026-07-29 — zero stamped requests in the window; the
  sweep had nothing to bind).
- Seat-side: twelve compelled re-registers in one session, none initiated or
  predictable from inside the seat (spike).

A design where the edge needs the hub reachable inherits all of that churn.
So: the hub holds **desired state**; each box's daemon **reconciles** toward
it and keeps working through hub outages. Evolves from the heartbeat daemon,
which already owns `~/.mcp-hub/` state, a pidfile singleton, and a
deliverability-verified liveness loop.

Verb set (v0): `place(unit, substrate)` · `status()` (enumerated, not
recorded) · `harvest(unit)` · `reclaim(unit)` · `abandon(unit)` (the loud
third state).

## Memory across clones

Exists, shipped: export/import/verify ceremony (hash-compared convergence
witness; proven 36-file run) and the clone-authority marker (proven
2026-07-26). Conceded gaps, both promoted to requirements by ephemerality:

- transport-recv must write the clone marker **automatically** (a clone is
  honest only if its creator remembered — defect, not chore).
- Reconciliation is manual/operator-invoked; the harvest step makes it part of
  the lifecycle instead. reliable-ai's memory-sync skill is the
  fork-reconciliation skeleton — extension, not greenfield.

## What must change in neighbouring components (owners, not us)

- **features-json** (their proposal, wants operator word + RA review): make
  `validation` required; treat `status`/`tests_pass` as reported-only. Also
  unenforced-prose rules ("at least one test file", "valid DAG") need real
  checks somewhere.
- **reliable-ai**: evidence layer (execution-proof from enumeration, progress
  signal, money budget) — stated by RA as a requirement on their harness, not
  a feature of it. RA participates via reviewable artifacts per their
  operator-ratified scope policy.
- **board_data / cockpit** (dev's question, operator decision): are ephemeral
  agents shown on the operator's board or excluded? Enrolment makes them
  appear today.

## Staged path

- **Slice 0 — the virtual squad, worktree substrate, no containers.**
  Clone spike + features-json + reliable-ai into a new squad workspace on
  dev-vm-1 (24/7 box; 849G free, 31G RAM verified; `dt-audit-repro` is
  dreamteam's postgres — untouchable). Hand-rolled on today's tools —
  `squad duplicate`/`transport`, suffix per clone, `squad_workspaces` entry.
  This is simultaneously the **first live test of multi-squad membership**
  (single-squad derivation is live; a second membership has never fired —
  dev has pre-written verification queries).
  *First workload: the cross-squad settings/board testing already owed.*
- **Slice 1 — edge daemon v0.** Heartbeat daemon grows `place`/`status`/
  `harvest`/`reclaim` for the worktree substrate; hub gets desired-state
  records. Pays for itself standalone: kills toolchain-guessing in
  `transport --host`, gives the cockpit a truthful machine list.
- **Slice 2 — container substrate.** Same verbs, docker backend. Not before
  the evidence contract works on worktrees — a container that can't prove its
  work ran is just a faster way to produce vacuous greens.
- **Acceptance test for the whole project: v1 recreates v0 without hands.**
  The squad that builds the runtime is re-placed *by* the runtime.

## Non-goals

- Rebuilding container scheduling (Nomad/k8s exist).
- Building toward RSI. Operator setting: early, half an eye. The sober
  connection (spike): an unattended environment **is** an evaluator — a sloppy
  one silently corrupts every measurement inside it. Bounded, reclaimable,
  observable is correct engineering *and* the safety-relevant part; that is
  where the overlap ends.

## Open questions

1. Board visibility of ephemeral agents (operator).
2. Retention/GC design — messages, channels, retired identities (hub lane).
3. Unit granularity is settled in principle (a feature, per FJ+RA); the
   schema changes are FJ's lane and need operator word.
4. What `f4-local` actually does downstream with self-asserted
   `tests_pass`/`status` — decides how bad the self-assertion is today
   (dreamteam's lane, flagged by FJ).
