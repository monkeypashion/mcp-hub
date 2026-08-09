# Hub Management API — v1 design

*Draft v0.1, 2026-07-29. API-first: every endpoint states its functional aim,
independent of implementation difficulty. Companion to
`agent-runtime-design.md` — the resource model is that doc's ratified
vocabulary. Uncommitted (master commit = deploy).*

## Two surfaces, one store

The hub keeps its **MCP tool surface** (register/send/post/broadcast/…) — that
is the *conversation* surface, for agents inside sessions. This document adds
the **management surface**: `/api/v1`, plain JSON over HTTP, for operators,
CLIs, the future UI, and edge daemons. Both read and write the same store.
Nothing an agent does conversationally moves to REST; nothing an operator
manages should require an MCP session.

## The edge boundary (stubbed, deliberately)

The hub **never executes anything on a machine**. It stores *desired state*
and composes *artifacts*; a per-machine **edge daemon** pulls both and
reconciles reality toward them, reporting *observed state* back by
enumeration (never by echoing the record — evidence contract ②).

Until the edge daemon exists, every "pending-edge" record is realizable
manually with today's tooling (`squad transport`, `transport-recv`,
`squad start`) — the API is useful from day one as the system of record, and
the edge daemon replaces hands, not design.

States that cross the boundary are explicit in every schema:

```
desired   what the operator asked for            (hub-authoritative)
observed  what the edge last enumerated          (edge-authoritative, timestamped)
status    derived: pending-edge | reconciling | converged | diverged | orphaned
```

`orphaned` is the loud third state (evidence contract ⑤): the edge tried,
failed, and says so; it never resolves silently.

## Cross-cutting rules

- **Versioned**: everything under `/api/v1/`. JSON in, JSON out.
- **Auth**: bearer token. Two principals for v1: `operator` (full) and
  `machine:<name>` (its own placements/status only). The hub is
  tailnet-only today, which is the interim control; the token story ships
  with the API because DELETE exists. Full RBAC deferred.
- **IDs**: squads and seats are addressed by their *names* (already unique by
  construction); machines by hostname; capsules/placements by server-issued
  ids.
- **Deletes are two-step where the resource has history**: `DELETE` archives
  (hidden, retained); `DELETE ?purge=true` is the retention decision made
  explicit. This forces the message/channel/identity retention design the
  hub has dodged (backlog: hub can't delete anything today).
- **Every list endpoint filters**: `?machine=`, `?squad=`, `?repo=`,
  `?status=` as applicable.
- **Events**: `GET /api/v1/events` (SSE) — resource-change feed for UIs and
  edges. One stream, typed events, cursor resume via `Last-Event-ID`.

---

## Resources and endpoints

### 1. Machines — the boxes

*Functional aim: make machines first-class so placement has a target and
capability is declared, not guessed over SSH.*

| verb | path | aim |
|---|---|---|
| POST | `/machines` | Enroll a machine: hostname, OS, capabilities (`docker`, `worktree`, `tailnet-ip`), edge token issued here |
| GET | `/machines` | List machines with liveness (edge last-seen) and capability flags |
| GET | `/machines/{name}` | One machine: capabilities, placements summary, observed load |
| PATCH | `/machines/{name}` | Update capabilities/labels (e.g. `docker: true` after install) |
| DELETE | `/machines/{name}` | Retire: refuses while placements exist (refcount rule — evidence contract ③) |
| GET | `/machines/{name}/placements` | **Edge pull**: full desired state for this box |
| POST | `/machines/{name}/status` | **Edge push**: observed state, by enumeration (running containers, live seats, disk) |

### 2. Seats — the unit of agenthood

*Functional aim: a durable record of every seat — folder + identity + launch
config — where today this lives scattered across rosters, config.json files
and derivation rules.*

| verb | path | aim |
|---|---|---|
| POST | `/seats` | Define a seat: repo, machine, folder, identity (assigned or derive-rule), launch args, class (`squad`/`faculty`) |
| GET | `/seats` | List/filter seats fleet-wide |
| GET | `/seats/{name}` | Full seat record + presence (bound? wakeable? idle?) merged from the live registry |
| PATCH | `/seats/{name}` | Change launch config (model, effort, comms, args) — the API form of `squad launch`/`args` |
| DELETE | `/seats/{name}` | Retire a seat: provenance-gated like teardown (clones deletable; originals refuse without `?force`) |
| POST | `/seats/{name}/clone` | **The transport as API**: body names target machine/workspace/suffix; hub composes the artifact set (repo ref, memory export, re-keyed history spec, marker); realization = placement |
| GET | `/seats/{name}/status` | Presence detail: binding age, last heartbeat, idle flag — what the statusline reads, as JSON |

**`repo` is the source of a derived NAME, and nothing else** — in both the
worktree and the docker branch. Give an `identity` and it is not needed at
all. A worktree seat needs `machine` + `folder`; a docker seat needs
`machine` + an image.

This matters more than it sounds: a folder with **no git remote is a
first-class agent** here (`squad add-folder` — *"git optional"*), and most of
the on-demand roster is exactly that. On dev-vm-1, **13 of 15 faculty agents
are plain folders**, so requiring a repo meant the API could start almost none
of the agents a UI would exist to start (measured 2026-08-09). The edge picks
its materialize verb from what the seat has:

| seat has | edge runs |
|---|---|
| `repo` | `squad add <org>/<repo>` — clone/pull, then enrol |
| `folder` only | `squad add-folder <dir> --name <identity>` — enrol what is already there |
| neither | `skip`, with a reason — never a guess |

`--name` carries the hub's **assigned** identity. Without it `add-folder`
derives `<basename>-<hostname>`, which need not equal the seat — materialize
would "succeed" and the next `squad start <seat>` would fail on a name that is
not in the roster. Identity is assigned, never re-derived at the far end.

**Identity rule (from the runtime design):** seats created via API always get
**assigned** identities. Derivation remains a client-side convenience for
hand-made seats; the API never relies on a container's hostname.

### 3. Squads — the broadcast circles

*Functional aim: full lifecycle CRUD on squads as first-class entities.
Today a squad exists only as strings on agent rows; creation is implicit,
deletion impossible.*

| verb | path | aim |
|---|---|---|
| POST | `/squads` | Create: name, description, `board_visibility: shown\|hidden` (the only policy field in v1) |
| GET | `/squads` | List with member counts and health (members online/wakeable) |
| GET | `/squads/{name}` | Detail: members with per-member mute state, sources (API-set vs workspace-derived), recent broadcast stats |
| PATCH | `/squads/{name}` | Rename/redescribe (rename cascades to memberships atomically) |
| DELETE | `/squads/{name}` | Archive (members released, name reserved); `?purge=true` = structural purge — memberships, seat links, squad record; **never message history** |
| GET | `/squads/{name}/members` | Membership list: seat, muted, source, since |
| PUT | `/squads/{name}/members/{seat}` | Add member (idempotent). Body: `{muted: bool}` |
| PATCH | `/squads/{name}/members/{seat}` | Mute/unmute — API form of `mute_squad` |
| DELETE | `/squads/{name}/members/{seat}` | Remove member — API form of the `set_squads` leave |
| GET | `/squads/{name}/broadcasts` | The squad-scoped feed (read; unfiltered reads stay on the MCP surface too — scoping is delivery, not confidentiality) |

**Membership authority — the one reconciliation rule:** the hub store is
authoritative (it already is — `set_squads` replaces, register-empty
preserves). Workspace derivation stays a *seeding input* at register time,
now recorded with `source: workspace:<file>` vs `source: api`, so the board
can attribute each membership to its real origin (fixes today's
source-attribution defect by design).

### 4. Workspaces — the listings

*Functional aim: the workspace as a hub-side definition that any machine can
materialize, instead of a hand-edited file that exists only where it was
written.*

| verb | path | aim |
|---|---|---|
| POST | `/workspaces` | Define: name, target machine (optional — templates are machine-less), listings (seat refs or folder paths), squad-typing (or untyped = faculty) |
| GET | `/workspaces` | List, with squad-typing and listing counts |
| GET | `/workspaces/{id}` | Detail: listings with per-seat resolution state (folder exists? seat enrolled?) |
| PATCH | `/workspaces/{id}` | Add/remove listings, retype (typing changes membership seeding — the API makes this explicit where config editing was silent) |
| DELETE | `/workspaces/{id}` | Remove definition (files already materialized on machines are the edge's to reconcile/remove) |
| GET | `/workspaces/{id}/file` | **The download**: rendered `.code-workspace` JSON, exactly what VSCode opens — the "workspace as artifact" ask |

### 5. Capsules — squad-in-a-box

*Functional aim: compose everything a squad needs into one artifact that any
docker-ready (or bare) machine can start, with zero hub-side knowledge of the
destination.*

| verb | path | aim |
|---|---|---|
| POST | `/capsules` | Compose from a squad (or explicit seat list): for each seat — repo@commit, assigned identity, launch args, memory seed (from the staging store), history spec; plus workspace file, `.mcp.json` template (hub URL parameterized), machine config fragment, and the edge bootstrap script |
| GET | `/capsules` | List capsules with source squad and freshness (staleness vs current squad state is *shown*, never hidden) |
| GET | `/capsules/{id}` | Manifest: every artifact, hashed — the convergence witness for what a placement should contain |
| GET | `/capsules/{id}/download` | The tarball: `docker-compose.yml` + per-seat build context for docker targets; plain tree for bare targets |
| POST | `/capsules/{id}/place` | Create placements from this capsule onto a machine (`{machine}` or `{selector: "any-docker"}`) |
| DELETE | `/capsules/{id}` | Remove the artifact (placements made from it are independent records) |

**Docker note:** a capsule seat's container carries claude-code + mcp-hub cli
+ the repo + imported memory, with identity **assigned in the capsule** —
never derived inside the container. The capsule is inert; starting it is a
placement.

### 6. Placements — desired state at the edge boundary

*Functional aim: the reconciliation contract. Everything above composes; this
is the only resource that asks a machine to DO something — and in v1 it is a
record, not an action.*

| verb | path | aim |
|---|---|---|
| POST | `/placements` | Desire: `{seat or capsule, machine, substrate: worktree\|docker, desired: running\|stopped}` |
| GET | `/placements` | Fleet-wide desired-vs-observed table — the truthful machine list the cockpit lacks |
| GET | `/placements/{id}` | One placement: desired, observed (timestamped enumeration), status, history of transitions |
| PATCH | `/placements/{id}` | Change desired state (`running` → `stopped`) |
| DELETE | `/placements/{id}` | **Reclaim request** = harvest + verify + destroy, in that order: memory delta exported to staging, substrate enumerated empty (with positive control), then destroyed; failure → `orphaned` + loud |
| DELETE | `/placements/{id}?purge=true` | **Unplace**: drop the row, ask the edge for nothing, leave the substrate exactly as it is |
| POST | `/placements/{id}/observed` | Edge reports enumerated reality (v1 stub: stored and surfaced, drives `status`) |
| GET | `/machines/{name}/watch` | **The doorbell** — SSE, wake-only: *"something changed for you, pull now"* |

### The doorbell

The edge pulls on a timer, which is what makes it NAT-safe and outage-proof —
but the interval is then the latency a UI feels. Measured 2026-08-09: a wake
took 95s and a sleep 96s, almost entirely spent waiting for the next tick.
`watch` lets a machine be told *now*.

- **The machine opens it**, outbound, with the bearer it already uses to pull.
  The hub never reaches a machine — the property the whole design rests on.
- **Wake-only.** The event carries no state (`{"machine": …, "reason": …}` and
  nothing else), so a lost event costs **latency and never work**. That is
  cheaper here than in the designs this borrows from, because this reconciler
  is **level-triggered**: `pull_placements` returns every row for the machine
  and the planner diffs it against a fresh enumeration, so every pass is
  already a full resync. No cursor, no `Last-Event-ID`, nothing to replay.
- **Heartbeats** (`: heartbeat` comment lines, 20s) are as important as events:
  a dead stream returns silence and so does a quiet one, and if those are the
  same bytes a client cannot tell whether to reconnect.
- **Rung by operator writes only** — create, PATCH desired, reclaim, unplace.
  Deliberately **not** by `/observed`: that is the machine reporting, and
  ringing on a report is a feedback loop with no brake.
- **`mcp-hub edge watch`** is the client (`mcp-hub-edge-watch.service`):
  reconnect with backoff 1s→30s, a **full pass on every (re)connect** to cover
  the disconnected window, a read timeout equal to the silence budget, and a
  coalescing guard so a burst of writes never starts overlapping passes while
  a bell arriving mid-pass still earns exactly one more.

⚠️ **The timer stays underneath and the doorbell must never become
load-bearing.** `mcp-hub-edge.timer` remains at 30s; a stopped, crashlooping or
quietly-disconnected watcher costs latency and nothing else. vps-hetzner's
`egress-sync` daemon on this estate died with its event stream and the system
then looked exactly like *"nothing has changed lately"* — its failure mode was
indistinguishable from its success mode.

⚠️ **In-process only.** If the hub is ever run multi-worker, a write served by
one worker will not ring watchers held by another, and the miss is silent. The
floor covers it; a doorbell that is ever made load-bearing must move to a
shared bus first.

**Reclaim and unplace are different intents, so they are different calls.**
They shared one verb until 2026-08-09, which meant the only way to stop the
hub scheduling a seat was to *demolish the agent behind it* — for a worktree
seat, `squad rm`, unenrolling it and opting its repo out of the hub. A
placement written against a real roster agent could therefore never be tidied
away.

- `reclaim` — "this seat is finished" → substrate destroyed.
- `?purge=true` — "the hub should stop caring" → substrate untouched.

Purge asks the edge for nothing because nothing needs asking: the row is the
whole of the hub's contribution, and `plan()` only ever acts on placements it
is served. A deleted row is served to nobody. It is **opt-in** (`purge=true`
exactly — `purge=1` still reclaims), so every existing DELETE in the fleet
keeps destroying, and it is **operator-only**, so an edge cannot quietly drop
its own policy. CLI: `mcp-hub placements unplace <id>`, which refuses without
`--yes` when the seat was last observed `running` — unplacing removes the
policy, not the process.

**v1 stub semantics:** POST/PATCH/DELETE write desired state and return
`status: pending-edge`. The interim realizer is a human (or cron) running
`squad`/`transport-recv` and posting `/observed`. The edge daemon, when it
arrives, changes throughput — not the API.

### 7. Reserved, deliberately not in v1

- `/units` — units of work (brief-as-the-gate). Belongs to the runtime design;
  reserving the name so nothing else squats on it.
- `/twins`, `/memory` — the staging store and twin model already work via
  MCP/CLI; they join the REST surface when something needs them there.

---

## Squad lifecycle, end to end (the CRUD story in one run)

```
1  POST /squads                         create the circle
2  POST /seats  (or /seats/{n}/clone)   define/fork the minds
3  PUT  /squads/{s}/members/{seat}      wire membership (or let workspace
                                        seeding do it at register)
4  POST /workspaces                     define the operator's view
5  POST /capsules                        freeze it into an artifact   ┐ docker
6  POST /capsules/{id}/place             desire it onto a machine     ┘ path
7  GET  /machines/{m}/placements        edge pulls, reconciles, starts
8  POST /machines/{m}/status            edge reports by enumeration
9  PATCH /placements/{id} desired:stopped   wind down
10 DELETE /placements/{id}              reclaim = harvest + verify + destroy
11 DELETE /squads/{s}                   archive; ?purge forces retention call
```

Today's slice-0 squad maps onto steps 1–4 + 7 done by hand — which is the
proof the API describes reality rather than aspiration.

## Decisions (operator, 2026-07-29)

1. **Retention — DECIDED: structural purge only.** `?purge=true` removes
   memberships, seat links and the squad record; **message/broadcast history
   is immortal** — the fleet treats history as the record. Old-message
   cleanup is a separate, later, age-based GC design; no endpoint in v1
   deletes message content.
2. **Squad policy — DECIDED: one field.** Squads carry
   `board_visibility: shown|hidden` (+ description). Hiding an ephemeral
   squad hides its seats from the operator's board as a group — answers the
   board-visibility question at squad granularity. No other policy in v1.
3. **Auth — DECIDED: bearer tokens from day one.** Operator token (full) +
   per-machine tokens (own placements/status only), issued at machine
   enrolment. Tailnet remains the network wall; the token is the lock on the
   door.
4. **Naming — DECIDED: `capsule`.** Chosen against its definition — frozen,
   inert, self-contained, verifiable; "a capsule is to a squad what an image
   is to a container." `squad-container` rejected for the Docker-word
   collision (the artifact builds containers, it isn't one).

## Feasibility note

The hub is FastMCP on uvicorn/Starlette; mounting `/api/v1` routes on the
same app beside `/mcp` is standard Starlette routing — one process, one
store, no second service. The expensive parts (edge daemon, docker realizer,
auth hardening) are exactly the parts stubbed behind explicit `pending-edge`
status, so v1 is implementable against the existing SQLite store as
CRUD + composition + the artifact renderers (workspace file, capsule tarball),
all of which reuse shipped machinery (transport's artifact logic, memory
staging, the settings model's value+source discipline).
