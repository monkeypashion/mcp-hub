# Known defects

Each entry names the **must-fire** condition a fix has to satisfy, so a later
change can be judged against the defect rather than against someone's memory of
it. Fixed entries are kept, not deleted — the must-fire is what a regression
would be caught by.

| # | Defect | State |
|---|---|---|
| 1 | `tests/test_seat_hold.py` fails open onto the shared fleet socket | ✅ fixed 2026-09-19 |
| 2 | Heartbeat singleton claimed once, never re-validated | ✅ fixed 2026-09-19 |
| 2a | A PID alone cannot say whose daemon it is | ✅ fixed 2026-09-19 |

## 1. `tests/test_seat_hold.py` fails OPEN onto the shared fleet socket — ✅ FIXED

**Measured 2026-09-18.** On a tree that does not carry `d3c76a0` ("the hold suite
was starting real seats on the fleet's own socket"), running
`tests/test_seat_hold.py` creates REAL tmux sessions named `lane-a` on the
**shared fleet socket**, running `claude --continue`. It is a test whose safety
depends on a commit it does not require.

This was found the safe way round: the split branch `09cfb6b` is cut from
`origin/master`, which predates `d3c76a0`, so the suite was run with
`--ignore=tests/test_seat_hold.py` and the resulting `2530 passed` was reported
with the deselection stated on its face — it is NOT a clean full suite.

⚠️ **Do not close this hole by running the test.** On a master-based tree that run
is a live-fleet act. The fix belongs on the TEST, never on whichever branch
happens to carry `d3c76a0`.

⚠️ **The fix below is on `feat/hold-kind-and-owner`, not on master.** Until it
lands, `pytest tests/test_seat_hold.py` on a master-based tree is still the
live-fleet act described above — the ✅ in the table is the state of THIS branch.
The verification runs quoted below were all made on a tree carrying both
`d3c76a0` and the fix, and the socket snapshot on either side of the full suite
is what says so.

**Must-fire:** a `squad` subprocess started by this file cannot reach the fleet's
tmux socket or a real `tmux`, and a test that forgets to contain itself FAILS
rather than acting.

⚠️ **The must-fire was reworded 2026-09-19, and the original was wrong.** It read
"on a tree without `d3c76a0`, the test SKIPS with a named reason". No change to
this file can satisfy that: `_stub_bin` and `_env` live IN the file, so a tree
without `d3c76a0` has an older copy with no guard to skip on — and a tree without
the fix below has no fix either. The clause named a tree, when the invariant is
about a subprocess. The second sentence, "never starts a seat on a socket it did
not create", was the real criterion all along and is what the fix is judged
against.

Class: the negative path that becomes the act — the same shape as `--token ""`
reaching the real token and parking four live lanes.

**Measured, not assumed (2026-09-19).** `squad/squad:17` reads
`SOCK="${SQUAD_SOCKET:-squad}"`, and `_env` never set it. Logging the stub's own
argv through a full `squad start lane-a`:

```
without SQUAD_SOCKET:  -L squad          <- the live fleet's socket
with    SQUAD_SOCKET:  -L squad-test-<h> <- a socket this test owns
```

and the calls on that socket are `new-session -d -s lane-a` followed by
`send-keys -t lane-a -l claude --continue`. That is the act, not a risk of it.

(Field 5 of the conf — `squad` in `lane-a|<dir>||--continue|squad` — is the lane
CLASS, read at `squad/squad:3518`, not the socket. An earlier note of mine said
otherwise; the socket only ever came from the environment.)

**Fixed 2026-09-19** with two containments and one rule that enforces them:

| containment | what it stops |
|---|---|
| `SQUAD_SOCKET` set to a per-tmpdir private name in `_env` | a session appearing on the fleet's socket |
| the stub `tmux` first on `PATH` (`_stub_bin`, unchanged) | a real `claude --continue` existing at all |
| **`contain_squad_subprocesses`**, an **autouse** fixture hooking `subprocess.Popen.__init__` | a test that forgets either one |

The guard is the actual fix. Both containments are properties of one helper, and
the defect was never that the helper was wrong — it was that using the helper is a
CONVENTION, and a test added later can build its own env and miss it in silence.
The fixture refuses any spawn naming `squad/squad` whose env does not name a
private socket, or whose `PATH` resolves `tmux` outside the test's own tmpdir.
Forgetting now costs an assertion at the moment of the call, before the process
starts.

It hooks `Popen.__init__` rather than `subprocess.run`, because `run`, `call`,
`check_call` and `check_output` all funnel through `Popen` — one hook covers
every door, including a bare `Popen`, which is the door a `run`-shaped guard
would have missed (`test_the_guard_catches_a_bare_Popen_too`). Patching the
METHOD rather than rebinding the name keeps `isinstance(x, subprocess.Popen)`
true for the rest of the process while the fixture is installed.

**The residue is still on the box.** A session named `lane-a`, created
**2026-09-06 19:10:51 UTC**, is still on the fleet socket — a real seat a green
suite left behind fourteen days ago, in a tmpdir that no longer exists. It is
present in the before-snapshot of the verification run below and unchanged by it.
Left in place deliberately: killing a session this file did not create is a
live-fleet act, and it is the clearest evidence the defect was never theoretical.

**Verified on the full suite, nothing deselected.** `2841 passed in 861.95s`
(2835 + the 6 tests added here), `ruff check src tests` clean. The tmux socket
list was snapshotted on either side: identical, only `squad`, and no
`squad-test-*` socket was ever created — the stub means no real tmux runs, so
the private socket never has to exist. The fleet's own session list went 27 → 29,
and both additions (`features-json-dev-vm-1` at 01:01:40, `reliable-ai-dev-vm-1`
at 01:01:46) are the bar 69 brake releasing two held lanes at an hour boundary
83 minutes AFTER the suite ended at 23:38:33 — not this suite. Attribution
checked rather than assumed; a count alone would have read as two new seats.

**Non-vacuousness was run, both ways.** Deleting `SQUAD_SOCKET` from `_env` fails
`test_squad_refuses_to_start_a_held_lane` in 1.4s with the guard's message and no
subprocess started; demoting the fixture off `autouse` fails
all four guard tests, one of them on the bare-`Popen` door. The guard tests invoke
`/nonexistent/bash` on purpose, so that if the guard ever stops firing they raise
`FileNotFoundError` instead of running squad for real — which is what they did in
the demoted run. `test_the_guard_lets_a_contained_run_through` is the negative
control against a guard that simply refuses everything.

## 2. The heartbeat singleton is claimed once and never re-validated — ✅ FIXED

**Measured 2026-09-19 on dev-vm-1.** Two live heartbeat daemons served one agent:

```
pid 2528703  started 19:18:16  cwd .../slipstream-testlane  holds NO pidfile
pid  219469  started 21:13:21  cwd .../slipstream-testlane  holds the pidfile
```

The newcomer took the claim two hours after the incumbent started, and the
incumbent is still running.

**Persistence — now VERIFIED in code, no longer a hypothesis.** `_claim_singleton`
is called exactly once, at `cli.py` in `heartbeat_daemon_command`, before the loop
is entered; the loop's `while True` never re-reads the pidfile, and
`_release_singleton` runs only in the caller's `finally`. So a daemon that loses
the file cannot notice, by construction.

**Origin — still a hypothesis.** What is NOT explained is how `219469` won a claim
while `2528703` was alive, since the claim is old-wins and stands down for a live
owner. The likeliest path: `_claim_singleton` has two *fail-open* returns — the
`mkdir` `OSError` branch and the retry-exhaustion branch — which return the
pidfile PATH without ever creating or writing the file. A daemon down either
branch runs holding no claim, which is exactly `2528703`'s observed state and
leaves the agent free for the next newcomer. Unproven: no instrument recorded
which branch ran. (A second, much narrower window exists — `O_EXCL` creates the
file EMPTY and the PID is written a few instructions later, so a reader inside
that window sees garbage and treats a live owner as stale. Microseconds wide, and
these two daemons started two hours apart, so it is not what happened here.)

Why it matters: an unclaimed daemon still beats, and the heartbeat contract is
that a beat must never keep a dead binding warm.

**Must-fire:** a daemon whose pidfile no longer names it stands down at the next
beat, and says so.

**Fixed 2026-09-19** by `_still_owns_singleton`, called once per beat **before**
`_call_heartbeat` — after the beat would be too late, the binding is already warm.
It resolves three ways, and the middle one is the point:

| pidfile state | action |
|---|---|
| names us | keep beating |
| **missing** | **re-claim it** — repairs a fail-open daemon, so the next newcomer finds a live owner and stands down instead of doubling up |
| names a live daemon | stand down, log to stderr, return without respawning |
| names a dead pid | take it over |

Re-claiming rather than standing down on a MISSING file is deliberate: standing
down there would kill a daemon nobody is competing for. It also closes the
hypothesised origin without needing to prove which branch produced it.

Covered by `test_loop_stands_down_when_another_daemon_owns_the_agent`
(tests/test_daemon_reexec.py), which asserts on the recorded tool calls, not just
the return value — standing down *after* beating would satisfy a return-value
assertion and still be the defect. The test is bounded by `asyncio.wait_for`
because without the fix the loop does not fail, it reconnects forever: with the
check removed the test times out, with it the test returns in ~1s. Both states
were run.

### 2a. `_daemon_alive_for` cannot tell WHICH agent a live daemon serves — ✅ FIXED

`_is_live_daemon` does more than a bare PID check — it reads `/proc/<pid>/cmdline`
and requires `heartbeat-daemon`. But a daemon launched **without `--name`** (the
normal SessionStart path: the console script `mcp-hub heartbeat-daemon`, which
derives identity from the cwd via `_resolve_agent_identity`) carries no agent name
in argv, so a recycled PID landing on a DIFFERENT lane's daemon satisfies the
check and suppresses that lane's self-heal indefinitely.

Not reproducible on demand — it needs a PID collision — but the check cannot
distinguish "my daemon" from "a daemon". An argv check cannot fix it: there is
nothing to read.

**Must-fire:** the pidfile carries the agent name alongside the PID, and the
liveness check compares the claim against what the holder says it is.

**Fixed 2026-09-19.** The claim is now `<pid> <agent> <cwd>`.

The agent name satisfies the must-fire and makes the file readable by a human,
but the **CWD is what the check actually compares**, because it is the input
identity was derived FROM and it reads back off the live holder with a single
`readlink` — no git, no subprocess, on a path the Stop hook takes at every turn
boundary. `_is_live_daemon(pid, expect_cwd)` now returns False for a live
heartbeat daemon running from a different directory, so a recycled PID no
longer suppresses a lane's self-heal.

Conservative on both unknowns, so nothing that used to be honoured stops being
honoured: a legacy claim (no recorded cwd) and an unreadable `/proc/<pid>/cwd`
(not Linux, or another user's process) both defer to the live owner exactly as
before.

⚠️ **Migration.** Every pidfile written before this commit holds a bare PID.
`_parse_claim` reads those as `(pid, None, None)` and they are honoured
unchanged; they upgrade the next time each daemon writes its own claim. Reading
them as garbage would have made every daemon on the fleet declare every claim
stale at once and take it over — mass duplication, i.e. §2 fleet-wide. Covered
by `test_parse_claim_accepts_legacy_pid_only_file`.

The identity comparison is asserted in BOTH directions
(`test_holder_cwd_matches_distinguishes_two_nameless_daemons`,
`test_recycled_pid_on_another_lanes_daemon_is_not_this_agents_owner`): a test
that only checks the mismatch would pass against a probe that rejects
everything.

*Reported by `slipstream-dev-vm-1`, who measured the nameless daemon and asked
rather than deleting the pidfile. Their lane was being served correctly — the
suppressed self-heal there is right, not a defect.*
