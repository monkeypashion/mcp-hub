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

## 2. The heartbeat singleton is claimed once and never re-validated

**Measured 2026-09-19 on dev-vm-1.** Two live heartbeat daemons served one agent:

```
pid 2528703  started 19:18:16  cwd .../slipstream-testlane  holds NO pidfile
pid  219469  started 21:13:21  cwd .../slipstream-testlane  holds the pidfile
```

The newcomer took the claim two hours after the incumbent started, and the
incumbent is still running. `_claim_singleton` is consulted once at startup; the
loop never re-checks that it still owns its pidfile, so a daemon that loses the
file (a lane restart removing and recreating it) never notices. *Mechanism is a
HYPOTHESIS; the two live daemons are measured.*

Why it matters: an unclaimed daemon still beats, and the heartbeat contract is
that a beat must never keep a dead binding warm.

**Must-fire:** a daemon whose pidfile no longer names it stands down at the next
beat, and says so.

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

*Reported by `slipstream-dev-vm-1`, who measured the nameless daemon and asked
rather than deleting the pidfile. Their lane was being served correctly — the
suppressed self-heal there is right, not a defect.*
