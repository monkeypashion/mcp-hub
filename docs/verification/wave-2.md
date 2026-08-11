# Wave 2 verification bar

**STATUS 2026-08-11: 4 of 5 items landed — W2.1 `ac477ff`, W2.3 `be4947e`,
W2.5 `2f23bc4`, W2.4 (this commit). REMAINING: W2.2 move verb, then W2.6
close. Branch `wave-2`, which also carries Wave 1's held-back `88b7022`.**

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
| A5 | `purge_expired_memberships` is called on every NEW read path introduced here (six call sites exist today, one is tested — each delivery-relevant site gains a lapsed-loan test) | pending — no NEW read path was added by W2.1; revisit if W2.2 adds one |
| A6 | `source` is backfilled on register/set_squads writes (today `''`), so membership provenance stops being half-wired | **MET** — TestProvenance (source stamped by both writers) |

## W2.2 placements move

| # | Item | Status |
|---|---|---|
| B1 | Positive control first: an ordinary create still succeeds. Then move REFUSES when another live placement exists for the seat (the double-placement collision capsule-place already warns about) | pending |
| B2 | A docker seat with no `memory_volume` is refused without `--no-harvest` — a silent move there loses everything the agent learned | pending |
| B3 | Move pre-checks the TARGET machine's `edge_last_run` (the W1.2 column) and refuses a machine whose edge is not reporting — the W2.2→W1.2 dependency, now satisfied | pending |
| B4 | The wait-gate observes `reclaim.destroy == done` before creating on B (identity collision impossible by construction); `--timeout` exits resumably, naming the manual two-phase path | pending |
| B5 | Leftovers report suppressed for a move EXCEPT the machine-A roster row — a seat declaration outliving its placement is what lets an agent move machines at all | pending |
| B6 | Named limitation, stated in output and here: the edge runs harvest→verify→destroy unconditionally, so a harvest that fails does not stop destroy. Gating that is a separate deferred decision | pending |

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
| F1 | Full suite + ruff green on the branch; CI green on the branch head before merge | pending |
| F2 | Every named mutation applied, verified failing against its named test, reverted; ledger below filled | pending |
| F3 | Live bars after the deploy settles, from a re-registered session (never through our own deploy): squad created via MCP visible to the runtime on prod; a real `placements move` between the two live machines; a brief carrying a fake secret refused by prod | pending |

Note: this wave's deploy also carries `88b7022` (Wave 1's L1/L2 bar statuses),
held back deliberately so a docs-only commit did not cost the fleet a rebind.

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
| N9 PATCH validates merged spec not incoming | TestRoutesEnforce::test_PATCH_validates_only_what_was_SENT |
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

**Two mutations survived their first named test, and both were tests of mine
claiming more than they proved — not code defects.** This is the half of a
mutation gate that earns it:

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
