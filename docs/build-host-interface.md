# The build host interface — one build, two places to run it

Status: **SKETCH. Three points SETTLED by `dreamteam-dev-vm-1`, four questions
now ANSWERED by them; two points still open.** Written 2026-08-07, the evening
dt merged the write-token hard-fail (`7cdff74`).

The goal, in the operator's words: *"it has to run exactly the same way as the
existing headless mode but via interactive docker instead of headless
codespace."* **Exactly the same way** is the whole requirement. One build
implementation, two places to run it, and the seam between them is this
interface.

## ⚠️ The name — and how nearly we picked another collision

Not `transport`: `squad transport` is a shipped verb in this repo meaning
*clone a whole agent into another workspace*. I raised that, proposed
**`substrate`** instead, and cited dt's own `writeTokenSubstrate` as precedent.

Both of us then found the same thing on our own ground:

- **dt's estate: 311 hits.** `substrate` there means the typed entity data
  model (`opts.substrateProjectUuid` is a factory-data-model project uuid).
  It had already been formally retired as a name for being overloaded.
- **This estate: 137 hits.** `substrate` here is the PLACEMENT substrate —
  `worktree | docker` on `api_placements`.

⇒ I proposed a word to fix a collision without checking it against my own
repo, having just told dt why that matters. dt checked theirs and caught it.
**`build host`** is 0 hits in both estates, so it is what both repos use; dt is
renaming `writeTokenSubstrate` to match.

⭐ Worth keeping: *a name is only unclaimed once you have grepped **every**
estate that will speak it.* Checking one and asserting the other is how the
fix becomes the next instance of the bug.

## What it is

A build today reaches its environment through codespace-specific helpers. The
docker path needs the same reach. So: name the small set of things a build
actually does to its environment, implement that set twice, and let the build
be build-host-blind.

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
sit **beside** that defect instead of closing it. Mandatory makes the
build-host work a fix.

Consequences worth stating rather than discovering:

- **`stdout` and `stderr` stay separate.** They are separate facts. Merging
  them is half of how the 127 got read as output.
- **`cwd` and `env` are parameters, not ambient state.** ssh and `docker exec`
  disagree about what a session remembers between calls; a build that relies on
  a `cd` persisting works on one build host and silently doesn't on the other.
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
build host asserts about itself.** dt's point, and both estates have paid for it
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
  Conflating them puts rendering concerns in the build host.
- **No credential handling.** That is the socket agent, it is factory-operated,
  and it is deliberately on the other side of this seam
  (see `seat-image.md`, "The ONE mount exception").
- **No container lifecycle policy** — who creates, when to reclaim. That is
  the edge's job on our side and stays there.

## The questions, ANSWERED by dt 2026-08-07

**1. Raise on unreachable — YES.** dt has a fresh scar: `sshSafe` returns
`null` for *both* "the transport died" and "the command failed with no
output", and their `ensureDeps` fix that same evening had to add a
**wording branch** to tell them apart, because the return value could not.
So: unreachable and timeout RAISE; only a command that actually ran returns a
result, and `rc` always means what a shell means.

**2. `putFile` takes BYTES — required.** `copyToCodespace` already accepts a
Buffer. A str-only signature pushes callers to base64-through-a-shell, which is
how the `gh cp` quoting bug wrote files to paths containing literal quotes.

**3. `getFile` — real, but out of v1.** Four capture paths pull artefacts out
(features, QA report, build report, git log); all text, all currently `cat`
through exec. It earns a signature the first time something genuinely binary
needs reading, and not before.

**4. 🎉 NO SESSION OPERATION NEEDED — and it is proven, not assumed.** This was
the question that could have doubled the design. The current build is already
stateless between calls, and one detail makes it unambiguous: **`nvmSource` is
passed as a string PREPENDED TO EACH COMMAND** rather than sourced once. That
parameter exists *because* state does not carry. Alongside it, 17 commands are
wrapped in `bash -l -c` (a login shell re-sourcing the profile every call) and
every directory-sensitive command re-establishes its own `cd … &&`.

⇒ Statelessness is not luck — it is what a per-command `gh codespace ssh`
transport forces. Worth *designing to keep*: if a `session` ever appears it
will be because someone found it convenient, not because the build needs it.

**5. `exec` lands BESIDE `sshSafe` — accepted.** dt's correction to their own
number strengthens it: **88 sites in `codespace-runner.js` and 67 elsewhere =
155**, not the ~45 first quoted. What sold it, in their words: *"delete
`sshSafe` when its last caller goes"* is a finish line, and *"the audit is
done"* is an opinion.

## 🔴 Mandatory `rc` is NECESSARY BUT NOT SUFFICIENT

dt's audit found a live defect that a mandatory `rc` would NOT have caught, and
it changes what the interface has to say.

`ensureDeps` ran `pip install --quiet -e . 2>&1 | tail -3` and tested success
with `result !== null`. **In a pipeline the exit status is the LAST command's**
— `tail`, which always succeeds. So a failed install logged *"Python deps
installed"*, and the error branch could only ever fire if ssh itself died.

An `exec` returning a faithful `rc` still returns **`tail`'s** `rc` here. The
value is honest; the command was the wrong thing to ask about.

⇒ Two consequences for this interface:
- **Callers must not pipe the command whose status they care about.** Where a
  pipeline is genuinely wanted, the caller owns the correctness (`set -o
  pipefail`, or an in-band sentinel, which is what dt used).
- Worth saying in the interface docs rather than assuming: `rc` is the exit
  status of *what you ran*, and in a shell that is not always *what you meant*.

⭐ I made the identical mistake the same evening in this repo — `pytest | tail
-3 && git commit` read `tail`'s status and pushed a red commit (`09962cd`,
fixed in `641376c`). Same shape, two estates, one evening, found independently.
It is a strong argument for the rule rather than for more care.

## Still open

- **`ready` has TWO levels** (dt's caveat, accepted). On a codespace, ssh
  answers before the workspace is necessarily populated — so `exec(["true"])`
  proves *the interface* is ready without proving *the build* can start. Keep
  them separate: `ready` belongs to the interface, and the build carries its
  own precondition on top. Growing `ready` to cover both is how it stops
  meaning anything.
- The exact signatures, once someone writes the first implementation.

## Status of each point

| Point | State |
| --- | --- |
| `rc` mandatory | **SETTLED** (dt) |
| …but not sufficient — pipelines mask it | **SETTLED**, dt's audit |
| `putFile` content-in/path-out, no shell | **SETTLED** (dt) |
| `putFile` takes bytes | **SETTLED** (dt) |
| `ready` is an external probe | **SETTLED**, sharpened to "an `exec` through this interface returned 0" |
| `ready` is interface-ready, not build-ready | **SETTLED** (dt's caveat) |
| unreachable/timeout RAISE, never a sentinel `rc` | **SETTLED** (dt) |
| `exec` lands beside `sshSafe`, 155 sites migrate one at a time | **SETTLED** (dt) |
| no `session` operation — statelessness is forced, and worth keeping | **SETTLED**, proven from code |
| `getFile` | deferred out of v1, by agreement |
| the name **`build host`** | **SETTLED** — 0 hits in both estates; dt renaming `writeTokenSubstrate` to match |
