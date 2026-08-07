# The build substrate interface — one build, two places to run it

Status: **SKETCH, for `dreamteam-dev-vm-1` to answer. Nothing built, nothing
agreed beyond the three points marked SETTLED.** Written 2026-08-07, the
evening dt merged the substrate hard-fail (`7cdff74`).

The goal, in the operator's words: *"it has to run exactly the same way as the
existing headless mode but via interactive docker instead of headless
codespace."* **Exactly the same way** is the whole requirement. One build
implementation, two substrates under it, and the seam between them is this
interface.

⚠️ **Naming — do not call this `transport` in this repo.** `squad transport`
already means *clone a whole agent into another workspace, possibly on another
machine*, and it is a shipped verb with its own docs. Two meanings for one word
across two estates is exactly how a design conversation goes wrong six weeks
later. I've been saying "transport interface" in DMs; from here I'm saying
**substrate**, which is also the word dt's own hard-fail already uses
(`native-available` / `absent-by-construction`).

## What it is

A build today reaches its environment through codespace-specific helpers. The
docker path needs the same reach. So: name the small set of things a build
actually does to its environment, implement that set twice, and let the build
be substrate-blind.

| | **codespace** (headless, shipped) | **docker** (interactive, new) |
| --- | --- | --- |
| Where it runs | GitHub codespace, torn down after | long-lived container on our edge |
| Reach | ssh (`sshSafe`) | `docker exec` |
| Native write token | present (`GITHUB_TOKEN`) | **absent by construction** — hence dt's hard-fail |
| Lifetime | minutes | days |
| Operator can attach | no | yes — that is the entire point |

## The operations

Three, deliberately. Anything a build does that isn't here is a fourth
operation we haven't justified yet, and the sketch should be argued with rather
than extended quietly.

### 1. `exec(cmd, *, cwd, env, timeout) -> {stdout, stderr, rc}`

**SETTLED: `rc` is mandatory and non-optional.** dt's call, and it is the
strongest thing in this document.

`sshSafe` returns a bare string and swallows the exit code. dt found the
receipt in their own contract tests: a bare `alembic` invocation exited **127**,
and the error came back *as stdout content, missed by regex parsing* — a
`command not found` that passed as success. An optional `rc` would let the
codespace implementation keep returning a bare string, so the docker work would
sit **beside** that defect instead of closing it. Mandatory makes the substrate
work a fix.

Consequences worth stating rather than discovering:

- **`stdout` and `stderr` stay separate.** They are separate facts. Merging
  them is half of how the 127 got read as output.
- **`cwd` and `env` are parameters, not ambient state.** ssh and `docker exec`
  disagree about what a session remembers between calls; a build that relies on
  a `cd` persisting works on one substrate and silently doesn't on the other.
- **`timeout` is explicit, and a timeout is not `rc`.** See below.

### 2. `putFile(content, path)`

**SETTLED: content-in, path-out, no shell anywhere in the middle.** dt's
existing one exists precisely because `gh cp` had a quoting bug that silently
wrote files to paths containing literal quote characters. Anything that routes
content through a shell reacquires that bug — including a "temporary"
`exec("cat > path <<EOF")`.

Open: does `content` accept bytes as well as text? A build that writes an
archive or a binary artifact needs bytes, and a str-only signature gets worked
around with base64 through a shell, which is the bug again.

### 3. Lifecycle: `create` / `ready` / `destroy`

**SETTLED in principle: `ready` is an EXTERNAL PROBE, never a status field the
substrate asserts about itself.** dt's point, and both estates have paid for it
independently — their codespace path needed a separate `waitForSsh` because
"codespace created" is not "you can reach it", and my `spec.memory_volume` was
a field declaring durability that nothing implemented, which cost three seats'
memory.

I want to make it sharper than "use a probe", because "probe" still admits a
weak one (`docker inspect .State.Running` is a probe, and it is a declared
field wearing a probe's clothes):

> **Ready means: a command sent through THIS INTERFACE returned `rc == 0`.**

`exec(["true"])` succeeding is readiness measured with the instrument the build
will actually use. Every weaker check — container running, ssh port open, API
says provisioned — is a different instrument agreeing about a different
question, which is the entire "delivered live" failure family.

## The gap in my own sketch — transport failure is not command failure

`exec -> {stdout, stderr, rc}` cannot currently say *"the command ran and
failed"* apart from *"I could not reach the box at all"*. They demand opposite
responses: the first is a build error to report, the second is a retry or an
abort. Collapsing them means either retrying a genuinely failing build forever,
or giving up a long build on a blip.

Returning `rc=255` for unreachable is not a fix — `255` is a value a real
command can return, and ssh already overloads it.

⇒ Proposal: **unreachable and timeout RAISE; only a command that actually ran
returns a result.** Then `rc` always means what a shell means by it, with no
sentinel values to memorise. Open to the opposite (a typed result with an
explicit `outcome` field) if dt's call sites read better that way — but not to
leaving it implicit.

## dt's blocker, and a way around it that costs nothing

dt flagged, correctly, that they owe an audit before the contract is fixed:
~45 `sshSafe` call sites, and *"some existing caller may be relying on the
current swallow"* — a correct change with an uncosted blast radius, which is
the failure this whole conversation has been about.

**That audit does not have to gate the interface.** `exec` can land as a NEW
function beside `sshSafe`, not as a change to it:

- Nothing existing changes behaviour on day one, so the blast radius is zero
  and the audit stops being a prerequisite.
- Call sites migrate deliberately, one at a time, each one a place where
  somebody looks at what that caller does with a non-zero `rc`.
- The audit still happens — it just happens **incrementally, with a working
  alternative already in the tree**, rather than as one big read before
  anything can start.
- `sshSafe` is deleted when its last caller is gone, which is also how you
  know the audit finished. A migration with a countable end beats one that
  ends when someone declares it over.

Cost: two functions coexist for a while. That is cheap next to blocking the
docker path on a 45-site read.

## What is NOT in this interface, and why

- **No `getFile`.** Probably needed for harvesting artifacts, but I'd rather dt
  name the real need than have me invent a signature for it.
- **No streaming output.** An interactive build has an operator watching a tmux
  pane; that is a different channel from what the build implementation consumes.
  Conflating them puts rendering concerns in the substrate.
- **No credential handling.** That is the socket agent, it is factory-operated,
  and it is deliberately on the other side of this seam
  (see `seat-image.md`, "The ONE mount exception").
- **No container lifecycle policy** — who creates, when to reclaim. That is
  the edge's job on our side and stays there.

## Open questions for dt

1. Raise-on-unreachable, or a typed `outcome` field? (I lean raise; your call
   sites decide it.)
2. Does `putFile` need bytes?
3. Is `getFile` real, and what does it actually need to move?
4. Does anything in the build depend on state persisting between `exec` calls
   (a `cd`, a shell variable, an activated venv)? If yes, that is a fourth
   operation — a session — and it is much bigger than the other three, so it
   is worth knowing now rather than at the end.
5. Does `exec` land beside `sshSafe` as proposed above, or do you want the
   audit first anyway? Your estate, your call — I'm offering it as a way to
   unblock, not arguing you out of the audit.

## Status of each point

| Point | State |
| --- | --- |
| `rc` mandatory | **SETTLED** (dt) |
| `putFile` content-in/path-out, no shell | **SETTLED** (dt) |
| `ready` is an external probe | **SETTLED** (dt), sharpened here to "an `exec` through this interface returned 0" |
| unreachable ≠ non-zero `rc` | proposed, unanswered |
| `exec` beside `sshSafe` | proposed, unanswered |
| bytes / `getFile` / sessions | questions, unanswered |
| the name `substrate` not `transport` | mine, and I'll use it regardless in this repo |
