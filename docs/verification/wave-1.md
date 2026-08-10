# Wave 1 verification bar

Committed BEFORE the build starts (the meta-rule: the build checks against a
committed bar; it does not write its own exam afterwards). Every item ends the
wave as **MET** (evidence link), **NOT-MET** (named), or **DROPPED**
(operator-approved). No silent scope narrowing.

Disciplines binding every item — from this estate's recorded instrument
failures: **enforces-not-checks** (a checkable-but-optional control is not
delivered); **mutation gate** (each new enforcement names a mutation and the
test that kills it — ledger at the bottom); **deliberate negative** (every
refusal is exercised in the state that provokes it); **never measure through
your own deploy**; **absence ≠ health** (every new surface is tested in its
blind state).

## W1.0 infrastructure

| # | Item | Status |
|---|---|---|
| I1 | `tests/conftest.py` autouse guards: state-dir redirect + daemon-spawn no-op + `api_v1._watchers` clear — full suite green with them global | **MET** — 1810 green with global guards (full suite, wave close) |
| I2 | `.github/workflows/ci.yml` runs pytest + ruff on branches/PRs; green on `wave-1` before merge. Residual recorded: Coolify deploys on master push regardless — CI is a pre-merge gate, not a deploy gate | **MET** — CI green on fb70574 (and its first run caught 17 real environment couplings, fixed at the seams) |
| I3 | This document committed before W1.1–W1.3 code | **MET** — committed 7acd2dd, before W1.1–W1.3 code |

## W1.1 seat lifecycle

| # | Item | Status |
|---|---|---|
| A1 | Creation paths ×3 (`api_v1.py:828`, `:936`, `:1495`) colliding with an ARCHIVED row → 409 naming the archived seat and the `restore`/`rm --purge` exits. Each test fails pre-fix (today's text lies: "already exists") | **MET** — test_seat_lifecycle.py::TestHonest409s (3 paths + positive control; failed pre-fix) |
| A2 | `seats restore` round-trips (archive → restore → identical in `seats list`, placements accepted); restoring a live seat and a nonexistent seat are distinct tested refusals | **MET** — TestRestore (round-trip, live/nonexistent refusals, changed-world re-validation) |
| A3 | Machine pull (`:604`): archived seat's spec absent EXCEPT for `desired='reclaimed'` placements (harvest needs the spec — destroy is by name). Archived+non-reclaimed state constructed via direct DB writes (API-unreachable, stated in docstring); archive-mid-reclaim scenario tested as the reachable case. Exclusion test fails pre-fix | **MET** — TestMachinePull (direct-DB construction + archive-mid-reclaim; exclusion failed pre-fix) |
| A4 | Archive fires `notify_machine`; `rm --purge` refused while ANY placement row references the seat (not `active_placements` — it excludes reclaimed rows) | **MET** — TestDoorbellsAndPurge (symmetric doorbells; raw-count purge; event trail + death-fact) |
| L1 | Live (post-deploy, settled): `/health` sha matches push; the 6 orphaned prod placements resolve to named states; tombstone symptom reproduced-then-cleared via `seats restore` on a test seat | pending deploy — run after merge settles, never through our own deploy |

Named residual (deliberate): an archived seat already materialized with
`desired='running'` keeps being started by name — state is API-unreachable,
legacy-only; the doorbell + `:604` fix stop new instances forming.

## W1.2 machinery health

| # | Item | Status |
|---|---|---|
| B1 | Every edge pass lands `{ts, result, placements, actions, errors}` on the machine row; board machine node + `workspaces list` render it (fleet_tree pure-data tests + label-width guard reused) | **MET** — TestProducer + TestHubStorage + TestSurfaces (board label, workspaces list) |
| B2 | BLIND: producer stopped → edge-age reads not-reporting within window; heartbeat staleness and edge staleness are DISTINCT phrases, tested to disagree in both directions | **MET** — TestDerivedState (instruments disagree both directions; local machine shows own dead edge) |
| B3 | A failing pass — including `EnumerationFailed`, today stderr-only — reaches the hub as `result: failed` via the except-path POST (both `apply` and `watch`; reporter cannot die of its own report). Fails pre-fix | **MET** — TestFailurePath (apply AND watch except paths; reporter cannot die of its own report; failed pre-fix) |
| B4 | No silent drops on the status route: the already-sent `"seats"` key is handled or explicitly logged; unknown keys logged. Named after the shape that hid this channel | **MET** — test_unstored_payload_keys_are_named_never_silently_dropped |
| L2 | Live: fireblade + dev-vm-1 render edge health truthfully; one deliberately broken unit shows as broken | pending deploy |

## W1.3 from_agent

| # | Item | Status |
|---|---|---|
| C0 | Instrument first: live_hub-based harness drives real MCP sessions **with `client_info.name="claude-code"`** (stock client is pre-refused by `is_interactive_client` — without the spoof every bind test is vacuous against unfixed code). Positive control: harness proves it CAN bind before any refusal is trusted. Call-site ledger recounted | **MET** — test_from_agent_gates.py real-socket harness, claude-code spoof load-bearing (own control), positive controls; test_url_rebind VACUOUS notice superseded |
| C1 | A BOUND session asserting a from_agent it doesn't own is refused at every call site (wire-tested via C0), including the four newly gated (`memory_put` — empty assertion skipped, `create_channel`, `subscribe_channel`, `ping`); the new four fail pre-fix | **MET** — TestBoundRefusals (5 writers; new four failed pre-fix) |
| C2 | A session bound to Y asserting/pinging as X is refused. An UNBOUND session's assertion remains accepted BY DESIGN (reconnects; register ungated) — stated, not implied | **MET** — bound-to-Y refused; unbound-accepted-BY-DESIGN has its own test |
| C3 | No regression: own-name re-register; heartbeat daemon's unbound calls; multi-name sessions | **MET** — TestHonestBoundary (empty-provenance guard, own-name re-register, multi-name) |
| C4 | A register() displacing a live ping-verified binding produces the displaced-binding notice. Full prevention (per-agent tokens) is a named deferred decision — this bar is visibility, not prevention | **MET** — TestDisplacementNotice (live displacement noticed; dead-session reconnect silent) |

## Mutation ledger (filled at wave close)

| Mutation | Killed by |
|---|---|
| M1 seat_collision archived branch dropped | TestHonest409s::test_seats_add_names_the_archived_row_and_the_exits |
| M2 :604 reclaimed-harvest exception dropped | TestMachinePull::test_reclaimed_placement_still_carries_the_spec |
| M3 purge count → active_placements semantics | TestDoorbellsAndPurge::test_purge_refused_while_any_placement_row_references |
| M4 archive doorbell removed | TestDoorbellsAndPurge::test_archive_and_restore_both_ring_the_doorbell |
| M5 producer's "edge" key dropped | TestProducer::test_edge_apply_sends_its_own_verdict |
| M6 apply-except failure report removed | TestFailurePath::test_apply_except_path_posts_failed |
| M7 status route silent-drop restored | TestHubStorage::test_unstored_payload_keys_are_named_never_silently_dropped |
| M8 ping's _attribution gate removed | TestBoundRefusals::test_ping_refuses_and_does_not_bind |
| M9 displacement deliverability condition dropped | TestDisplacementNotice::test_reconnect_after_death_is_silent |
