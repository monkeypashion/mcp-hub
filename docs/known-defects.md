# Known defects

Defects measured but not yet fixed. Each entry names the **must-fire** condition
a fix has to satisfy, so a later change can be judged against the defect rather
than against someone's memory of it.

## 1. `tests/test_seat_hold.py` fails OPEN onto the shared fleet socket

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

**Must-fire:** on a tree without `d3c76a0`, the test SKIPS with a named reason and
exits non-silently. It never starts a seat on a socket it did not create.

Class: the negative path that becomes the act — the same shape as `--token ""`
reaching the real token and parking four live lanes.

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

### 2a. `_daemon_alive_for` cannot tell WHICH agent a live daemon serves

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

⚠️ **Still OPEN after the §2 fix.** `_still_owns_singleton` re-reads the same
PID-only pidfile, so it inherits this blind spot exactly: a recycled PID that
lands on another lane's daemon reads as "a live owner" and would make the
rightful daemon stand down. Closing it needs the pidfile FORMAT to change
(`<pid> <agent>`), which touches `_release_singleton`, `_daemon_alive_for` and
the tests that assert the file's exact contents — deliberately not bundled into
the §2 fix.

*Reported by `slipstream-dev-vm-1`, who measured the nameless daemon and asked
rather than deleting the pidfile. Their lane was being served correctly — the
suppressed self-heal there is right, not a defect.*
