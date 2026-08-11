# Wave 2 verification bar

**STATUS 2026-08-11: WAVE 2 COMPLETE — merged and deployed as `156d378`, all
22 bar items MET, live bars run on prod from a re-registered session. Nothing
outstanding.**

Committed BEFORE the build (the meta-rule: the build checks against a committed
bar; it does not write its own exam afterwards). Every item ends the wave as
**MET** (evidence named), **NOT-MET** (named), or **DROPPED**
(operator-approved). No silent scope narrowing.

Disciplines binding every item — unchanged from Wave 1, which earned them:
**enforces-not-checks** · **mutation gate** (each enforcement names a mutation
and the test that kills it) · **deliberate negative** (every refusal exercised
in the state that provokes it) · **absence ≠ health** · **never measure through
your own deploy**. Two Wave-1 lessons carried in: **CI on the branch is part of
the bar, not a formality** (its first run found 17 environment couplings), and
**check the instrument exists before trusting its silence**.

Scope note: W2 is structural debt, not bleeding defects. Its riskiest item
(W2.1) touches broadcast delivery — the 2026-07-27 squadless-broadcast incident
class — so the incident suite is a GATE, not a nice-to-have.

## W2.1 squad registry coherence

Design: `squad_members` stays the fact (it alone decides broadcast audience);
`api_squads` becomes lazily auto-upserted on any membership write, so
"exists for comms, unknown to the runtime" stops being representable. The
upsert helper lives in `server.py` — the import direction is api_v1 → server.

| # | Item | Status |
|---|---|---|
| A1 | 🔴 GATE: `test_broadcast_scope.py` (27 tests, the 2026-07-27 incident) passes **UNCHANGED** — no edits to that file are permitted by this wave | **MET** — 27/27, file untouched (ac477ff) |
| A2 | Cross-surface round-trip, which has ZERO coverage today: a squad created via `set_squads`/`register` is visible in `GET /api/v1/squads`, accepts member-PUT without 404, and composes into a capsule | **MET** — TestCrossSurface (register/set_squads → GET /squads, member-PUT, compose); failed pre-fix |
| A3 | `set_squads` naming an ARCHIVED squad is refused, naming the archive; `register` naming one drops that squad with a notice in its result (refusing the whole register would break reconnects — the asymmetry is deliberate and tested both ways) | **MET** — TestArchivedAsymmetry, both branches + refusal-changes-nothing |
| A4 | Preserved behaviours, one test each: archived-name reservation; `squads rm` without `--purge` keeps the broadcast audience; empty squads stay legal | **MET** — TestPreserved (reservation, rm-keeps-audience, empty squads, empty-preserves) |
| A5 | `purge_expired_memberships` is called on every NEW read path introduced here (six call sites exist today, one is tested — each delivery-relevant site gains a lapsed-loan test) | **MET** — no NEW read path was added by W2.1 or W2.2 (checked, not assumed), so the first clause is vacuous; the second clause was real work and is done. `test_lapsed_loan_delivery.py` covers the three sites where a survivor is a **wrongly-delivered message** rather than a wrong number on a screen: the live-push audience, the Stop-hook catch-up, and capsule composition. The other three sites are operator-facing reads already covered at the helper and via the REST members list |
| A6 | `source` is backfilled on register/set_squads writes (today `''`), so membership provenance stops being half-wired | **MET** — TestProvenance (source stamped by both writers) |

## W2.2 placements move

| # | Item | Status |
|---|---|---|
| B1 | Positive control first: an ordinary create still succeeds. Then move REFUSES when another live placement exists for the seat (the double-placement collision capsule-place already warns about) | **MET** — positive control first; collision refused with the other placement NAMED, not counted; a `reclaimed` row deliberately does not block (else a seat is unmovable after its first move) |
| B2 | A docker seat with no `memory_volume` is refused without `--no-harvest` — a silent move there loses everything the agent learned | **MET** — refused with the exit named; `--no-harvest` accepts the loss; worktree seats not blocked (positive control) |
| B3 | Move pre-checks the TARGET machine's `edge_last_run` (the W1.2 column) and refuses a machine whose edge is not reporting — the W2.2→W1.2 dependency, now satisfied | **MET, and exceeded** — BOTH ends checked. The bar names the destination; the SOURCE is what actually hangs the wait (A offline ⇒ the reclaim is never observed complete), so checking only the destination would satisfy the letter and still strand the operator. `stale`/`never`/no-record refuse; `failed` warns and proceeds — a measurement is not blindness, and refusing it would make the fleet unmovable exactly when a seat needs to come off a sick box |
| B4 | The wait-gate observes `reclaim.destroy == done` before creating on B (identity collision impossible by construction); `--timeout` exits resumably, naming the manual two-phase path | **MET** — the fake reclaim PROGRESSES (a fake reporting done on poll 1 would test nothing); the test asserts the create landed on the poll that saw `destroy done`, not the first. Timeout exits **2**, distinct from 1/0, writes nothing to B, and prints the two-phase path. A vanished row counts as complete |
| B5 | Leftovers report suppressed for a move EXCEPT the machine-A roster row — a seat declaration outliving its placement is what lets an agent move machines at all | **MET** — asserted positively (roster row present) AND negatively (no "seat declaration" / "seats rm" line) |
| B6 | Named limitation, stated in output and here: the edge runs harvest→verify→destroy unconditionally, so a harvest that fails does not stop destroy. Gating that is a separate deferred decision | **MET** — a harvest reporting `failed` is named in the output with the reason the destroy went ahead anyway |

## W2.3 brief/input secret-scan + spec validation

Making a promise true: `cli.py:1983` already asserts "the refusal below is
worth its false positives" — and no refusal exists anywhere in `src/`.
Declared-is-not-enforced, in our own code.

| # | Item | Status |
|---|---|---|
| C1 | ONE server-side validator reached from ALL FOUR seat-writing routes (`POST /seats`, `PATCH` merge, `clone`, `_mint_capsule_seats`) — a fourth path must not be able to re-introduce the split (W1.1's `seat_collision` is the precedent) | **MET** — spec_guard reached from POST/PATCH/clone/_mint_capsule_seats (be4947e) |
| C2 | Secret patterns refused with the pattern NAMED and the match never echoed; deliberate negatives per pattern | **MET** — 9 patterns × fires + never-echoes, parametrized; positive control included |
| C3 | Inputs filename rules mirror `seat.py:307-342` server-side — today that check is container-side only, so a crafted spec via REST bypasses it entirely | **MET** — test_POST_refuses_an_escaping_input_filename (was container-side only) |
| C4 | Legacy content survives: clone/mint do NOT re-validate (a pre-existing spec must stay cloneable); PATCH validates only `incoming`'s non-None keys | **MET** — legacy content planted via direct DB write, cloned OK, same content refused on fresh create |
| C5 | `_read_brief_and_inputs` gains tests for all five untested refusal branches, including the duplicate-basename collision (a silent-loss shape) | **MET** — all five branches incl. duplicate-basename |

## W2.4 stop-hook shadow-mode

| # | Item | Status |
|---|---|---|
Design: the server's `(already delivered live — …)` line is an INFERENCE
(`pushed_gen == gen_now and not wake_render_unproven`). Shadow mode observes
the other side from the transcript the hook already holds and records
disagreements in two directions: **false_compaction** (claimed live, no render
— the harmful one: the body was shortened AND marked read) and
**double_surface** (reprinted in full though it did render — the context tax
the 2026-08-09 investigation predicted).

The observation is narrow on purpose. A `<channel …>` tag appears in three
measured record types — `queue-operation`, `attachment`, and `user`. Only
`user` is a render; the others prove the notification reached the client,
which is the very thing in question.

| # | Item | Status |
|---|---|---|
| D1 | ZERO behaviour change: rendering is byte-identical with shadow-mode on (asserted, extending `test_stop_hook_compact.py`) | **MET** — differential test runs the hook shadow-on vs shadow-off, asserts identical stdout; return value discarded at the call site |
| D2 | A synthetic disagreement is captured in `~/.mcp-hub/shadow-surface.jsonl`; the log caps rather than growing forever | **MET** — both directions captured; cap keeps the most RECENT (asserted, not just the count); positive control asserts agreement records nothing |
| D3 | No hub write, no new network call — the hook already holds `transcript_path` locally and the diagnostic stays local | **MET** — enforced by poisoning `socket.socket`/`create_connection` and asserting the disagreement still lands (a swallowed hub call would return empty) |
| D4 | *(added during build)* The instrument abstains where it cannot support a verdict: a handle under 12 chars records `unmatchable` rather than guessing | **MET** — a short handle would match some render on nearly any turn |

## W2.5 sandbox premise + seat audit

| # | Item | Status |
|---|---|---|
| E1 | A spec mounting `/var/run/docker.sock` is REFUSED, naming the premise it violates ("the container IS the sandbox" holds only without it) — tested edge-side AND hub-side | **MET** — refused at write time AND materialize time (2f23bc4); edge test failed pre-fix |
| E2 | Host-sensitive paths refused unless an explicit `unsafe_volumes` flag is set, which the hub RECORDS and a surface shows — an escape hatch nobody can see is the shape this wave exists to remove | **MET** — host system paths refused; named volumes/ordinary paths pass (positive control) |
| E3 | The audit table (scope × write × still-effective, citation-backed) lands in `docs/seat-image.md`; every "still effective" row backed by a citation or a probe, never by memory | **MET** — audit table in docs/seat-image.md, every row cited; seat writes none of the six moved keys |

## W2.6 wave close

| # | Item | Status |
|---|---|---|
| F1 | Full suite + ruff green on the branch; CI green on the branch head before merge | **MET** — 1906 passed locally (6m22s), ruff clean, CI green on the branch head. ⚠️ The first full-suite run failed **14 of 23** move tests that passed alone: `FRESH = time.time()` at module scope against a 120s staleness window and a 7-minute suite. A fixture that decays reports the clock, not the code — timestamps are built at call time now, and the parametrize list that baked them at COLLECTION time builds its machines inside the test |
| F2 | Every named mutation applied, verified failing against its named test, reverted; ledger below filled | **MET** — all 30 applied and verified, N1–N12 included (they had been *named* in their own commits, not run here; F2 says applied, so they were run). Three genuine survivors recorded with reasons (N19, N28 defence-in-depth; N9 a vacuous test, now fixed). Two apparent survivors were **broken mutations of mine** — N12's replacement expression evaluated back to the original tuple and the first N9 attempt never switched to the merged spec. Both kill their tests once written correctly, which is its own lesson: a mutation that does not apply looks exactly like a control that does not work |
| F3 | Live bars after the deploy settles, from a re-registered session (never through our own deploy): squad created via MCP visible to the runtime on prod; a real `placements move` between the two live machines; a brief carrying a fake secret refused by prod | **MET** — all three, on prod `156d378`, from a re-registered session. Details below |

Note: this wave's deploy also carried `88b7022` (Wave 1's L1/L2 bar statuses),
held back deliberately so a docs-only commit did not cost the fleet a rebind.
This file's own F3 update is held back for the same reason.

### The live bars, as run (prod `156d378`, 2026-08-11)

**Deploy verified before anything was measured**, per *never measure through
your own deploy*: `/health` went `f1e9549` uptime 29092s → `156d378` uptime
44s, and `agents_bound` 13 → 2. Both facts matter — a commit change alone
would not distinguish a redeploy from a restart, and the binding drop is the
cost this wave was batched to pay once.

**L1 — a squad created via MCP is visible to the runtime.** `set_squads`
minted `w2-live-check`, which appeared immediately in `GET /api/v1/squads`
with `description: "auto-registered from set_squads:mcp-hub-fireblade-wsl"`
(A6's provenance, live), and `PUT /squads/w2-live-check/members/...` returned
**200** where it would have 404'd before. Unplanned corroboration: `dreamteam`
was already carrying `"auto-registered from register:reliable-ai-dev-vm-1"` —
another agent's ordinary reconnect had healed the incoherence unprompted.

**L2 — a real `placements move` between the two live machines.** A disposable
docker seat `w2-move-check` was placed on dev-vm-1 as `stopped`, then moved to
fireblade-wsl. The verb refused without `--yes`, dry-ran its three steps, then
requested the reclaim, printed one `reclaim in progress` poll, observed
**dev-vm-1's real edge** report `destroy` done, and only then created on
fireblade-wsl. Exit 0.
⚠️ **Stated limit, not implied coverage**: the seat was `stopped`, so no
container ever existed and the **harvest leg was a no-op**. What this proves is
the orchestration, the pre-checks and the wait-gate against real edges — not a
harvest of real data. Testing that destructively on the live estate was not
worth it, and the docker-substrate path is covered by the unit suite.

**L3 — a brief carrying a fake secret is refused by prod.** `POST /seats` with
`AKIAIOSFODNN7EXAMPLE` in the brief returned **422** naming the pattern ("an
AWS access key id"), naming the exit (`--env-from-host`), and **not echoing the
key**. Positive control: the same route returned **201** for the same seat with
a clean spec, so the 422 was the guard rather than a malformed payload.

**Both machines' edges were reporting `ok` within 30s** throughout — W1.2's
column doing real work, since it is what the move's pre-check reads.

**The estate was restored**: both placement rows unplaced, the seat purged
(exercising W1.1's `--purge` live — "the death-fact survives in its event
trail"), the test squad removed, and this agent's own `dreamteam` membership
put back. Verified after: 3 squads, 8 placements, 2 seats — the pre-test state.

## Mutation ledger (filled at wave close)

| Mutation | Killed by |
|---|---|
| N1 _ensure_api_squad removed from register | TestCrossSurface::test_a_squad_created_by_register_is_visible_to_the_runtime |
| N2 _ensure_api_squad removed from set_squads | TestCrossSurface::test_a_squad_created_by_set_squads_accepts_member_PUT |
| N3 set_squads archived-refusal branch dropped | TestArchivedAsymmetry::test_set_squads_REFUSES_an_archived_squad |
| N4 register refuses instead of dropping | TestArchivedAsymmetry::test_register_DROPS_an_archived_squad_with_a_notice |
| N5 source column dropped from either INSERT | TestProvenance::test_register_and_set_squads_stamp_their_source |
| N6 any _SECRET_PATTERNS entry deleted | test_each_pattern_is_refused_by_name[that case] |
| N7 match interpolated into the refusal | test_the_refusal_never_echoes_the_secret |
| N8 check_input_name call removed | TestRoutesEnforce::test_POST_refuses_an_escaping_input_filename |
| N9 PATCH validates merged spec not incoming | TestRoutesEnforce::test_PATCH_validates_only_what_was_SENT — ⚠️ SURVIVED until the test was fixed: it claimed to plant a brief that would fail today's guard and planted `brief="fine"`, so clean content passed either way. Now plants legacy content by direct DB write, with a control that the same content IS refused when sent |
| N10 clone re-validates the whole spec | TestRoutesEnforce::test_CLONE_does_not_re_validate_legacy_content |
| N11 check_volumes removed from the create branch | TestEdgeRefusesToMaterialize::test_a_legacy_docker_socket_spec_is_REFUSED_at_materialize |
| N12 docker.sock dropped from _FORBIDDEN_MOUNTS | TestVolumes::test_the_docker_socket_is_refused_naming_the_premise |
| N13 false_compaction branch dropped from compare() | TestDisagreements::test_a_live_claim_with_no_render_is_captured |
| N14 log trim removed from record() | TestDisagreements::test_the_log_caps |
| N15 `rec.get("type") != "user"` check dropped | TestParsing::test_an_ASSISTANT_quoting_a_channel_tag_is_not_a_render |
| N16 double_surface branch dropped | TestDisagreements::test_a_full_reprint_of_a_rendered_message_is_captured |
| N17 run_shadow's try/except removed | TestZeroBehaviourChange::test_a_RAISING_shadow_cannot_break_the_hook |
| N18 short-handle abstention removed | TestDisagreements::test_a_handle_too_short_to_match_abstains |
| N19 message-shape check weakened (top-level `content` accepted) | ⚠️ SURVIVES ALONE — killed only in combination with N15 (verified). Not a coverage gap: the queue-record case is rejected by the type check AND the shape check independently, so neither mutation alone changes the verdict. Recorded rather than credited to either. |

| N20 second-live-placement check dropped | TestCollision::test_a_second_live_placement_is_REFUSED |
| N21 memory_volume check dropped | TestHarvest::test_a_docker_seat_with_no_memory_volume_is_REFUSED |
| N22 destination edge check dropped | TestEdgeHealth::test_a_DESTINATION_whose_edge_is_not_reporting_is_refused |
| N23 source edge check dropped (destination only) | TestEdgeHealth::test_a_SOURCE_whose_edge_is_not_reporting_is_refused |
| N24 wait-gate short-circuited (create immediately) | TestWaitGate::test_B_is_created_ONLY_after_destroy_is_observed_done |
| N25 timeout falls through to create | TestWaitGate::test_a_timeout_exits_RESUMABLY_and_creates_NOTHING |
| N26 full `_report_leftovers` used instead of the roster line | TestAftermath::test_only_the_source_roster_row_is_reported_as_a_leftover |
| N27 harvest-failure report dropped | TestAftermath::test_a_failed_harvest_is_NAMED_because_destroy_ran_anyway |
| N28 purge removed from `broadcast()`'s recipient filter | ⚠️ SURVIVES ALONE — killed combined with N29 (verified). `broadcast()` resolves scope via `_squads_of` first, which already purges, so this site is defence-in-depth in the current flow. Recorded, not credited. |
| N29 purge removed from `_squads_of` (catch-up) | TestBroadcastDeliveryPaths::test_a_lapsed_loan_is_dropped_from_the_CATCH_UP_path |
| N30 purge removed from `compose_capsule` | TestCapsuleComposition::test_a_capsule_composed_after_a_loan_lapsed_does_not_RESURRECT_it |

**Three mutations survived their first named test (N15, N17, N28), and every
one was a test of mine claiming more than it proved — not a code defect.**
This is the half of a mutation gate that earns it:

- **N15** first ran against the queue-plumbing test, which the shape check
  rejects anyway, so dropping the type check changed nothing. The record that
  actually needs the type check is an **assistant message quoting a channel
  tag** — the agent's own words vouching for a delivery it never got. Not
  hypothetical: the first hand-grep during this build matched this repo's own
  `push_channel` source rather than any live push. New test added.
- **N17** first ran against a corrupt-transcript test that never raises
  (`observed_renders` already catches `OSError`/`ValueError`), so it never
  exercised the guard it claimed to pin. Replaced with an injected fault; the
  corrupt-transcript case is kept, renamed, and no longer takes the credit.
- **N28** revealed that `broadcast()`'s own purge is redundant: the scope
  resolution above it calls `_squads_of`, which purges first. The audience IS
  pinned — by N29 — and the extra call is defence-in-depth, which is what the
  helper's docstring asks for. Left in place and recorded as such.

The general shape, worth keeping: **a mutation that survives is a question
about the test, not automatically a hole in the code.** Two of these three
were real test defects; the third was a correct redundancy that no single
mutation can reach. Crediting an enforcement to a test that would pass without
it is how a suite comes to look green while proving nothing.
