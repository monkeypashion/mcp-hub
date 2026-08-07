# N seats per container — design

Status: **DESIGN, nothing built. The three open questions are DECIDED (see foot).** Written 2026-08-06 after the 1:1 container
squad went live on dev-vm-1 (three containers, three agents, one OAuth token,
all ⚡). Operator direction: *"finish the 1:1 squad and then look at the N
seats in one container so that we fully support both options."* Both shapes
are first-class; neither replaces the other.

## The two shapes

| | **1:1** (shipped) | **N:1** (this design) |
| --- | --- | --- |
| Unit the edge places | one container = one agent | one container = one squad's worth of agents |
| Container name | IS the seat identity | is its own identity; agents are listed in the spec |
| Workspace file | on the HOST (`~/Projects/capsule.code-workspace`), folders reach in via `docker exec` | INSIDE the container, folders are the agents' workdirs |
| Operator door | Remote-SSH to the host, tabs run `docker exec … tmux attach` | same — **plus** Dev Containers: attach one VS Code window to one container and open the workspace like any local squad |
| Blast radius | one agent per container lifecycle | container restart/reclaim touches all N |

With Remote-SSH the two shapes give the **same operator experience** — that's
why 1:1 shipped first and why nothing forces a migration. N:1's real payoff is
the Dev Containers door (parked earlier in favour of SSH-first): one window,
one workspace, N agents, no host paths anywhere. Under 1:1, Dev Containers
would mean one VS Code window per agent, which is not a squad view.

## What 1:1 bakes in today — the inventory

Every coupling that assumes one agent per container, found by reading, with
the place it lives. This list is the actual work:

1. **`DockerExecutor.create_argv` (`edge.py:356-369`)** — `docker create
   --name <seat>` and `-e SEAT_IDENTITY=<seat>`: the container name IS the
   seat identity, injected after spec env so the two can't disagree. That
   name/identity agreement is the whole enumeration contract.
2. **`enumerate_docker` (`edge.py:468`)** — maps `docker ps` names straight
   back to placements. Sound for N:1 *unchanged*, provided the placement's
   unit name is the container name — which it already is; only the meaning of
   the unit moves.
3. **`api_placements.seat`** — one placement references one `api_seats` row.
   The row's `spec` is already substrate-JSON by design ("a column per
   substrate would make every new substrate a migration; this makes it a
   key") — the same philosophy carries N:1 without a schema migration.
4. **The marker (`seat.py:marker_content`)** — single-valued, but it lives in
   the **workdir**, not HOME. N workdirs → N markers. Not actually a blocker;
   the single-valued thing is `SEAT_IDENTITY`, the env var.
5. **tmux** — one session, literally named `seat` (`launch_argv`), and the
   attach affordance `docker exec -it <c> tmux attach -t seat`
   (`settings_app.py:1522`).
6. **`seat-entry` (`cli.py:4116`)** — one contract, one clone, one launch
   dance, one first turn, one supervisor loop over one status file.
7. **Harvest (`edge.py` op `harvest`)** — `docker exec <seat> mcp-hub
   memory-export` resolves identity **from cwd** (the image's WORKDIR). One
   cwd, one identity.
8. **`fleet_tree.py` container nodes** — already carry `"agents": []`,
   plural. The tree was built expecting this design; only attribution (which
   registry agents hang under which container) needs the member list.

## Target contract

### Placement unit moves seat → container

A placement names a **container**. The container's `api_seats` row (class
`pod` is the working name) carries the members in its spec:

```json
{
  "image": "mcp-hub-seat:latest",
  "memory_volume": "capsule-mem",
  "agents": [
    {"identity": "mcp-hub-cap-dev-vm-1",     "repo": "git@github-monkeypashion:monkeypashion/mcp-hub.git",     "squads": "capsule"},
    {"identity": "vps-hetzner-cap-dev-vm-1", "repo": "git@github-monkeypashion:monkeypashion/vps-hetzner.git", "squads": "capsule"}
  ]
}
```

`spec.agents` **present → N:1; absent → today's 1:1 contract verbatim.** N=1
via `spec.agents` is legal and identical in behaviour to legacy, so both
shapes are one code path with the legacy env kept indefinitely — nothing
deployed changes shape.

### Env contract

New: **`SEAT_MANIFEST`** — the JSON above (agents list only; no secrets in it,
same as everything else the hub stores — names, never values). Injected by the
edge from `spec.agents` exactly as `SEAT_IDENTITY` is injected today.

Rules, enforced at the door (`seat-entry`):

- `SEAT_MANIFEST` and `SEAT_IDENTITY` both set → **refuse, exit 43.** A
  container that could be read as both shapes is a container whose identity
  is ambiguous, and ambiguity is how a message reaches the wrong lane.
- Every `identity` non-empty and unique, else 43. Identities are **assigned**,
  as always — nothing in a container derives a name.
- One credential validation for the whole container (unchanged) — N agents
  share the account. Measured 2026-08-06: three concurrent seats on one OAuth
  token, all ⚡; N-in-one is the same account concurrency, no new question.

### seat-entry, N:1 flow

Once per container: validate credential; write shared
`~/.claude/settings.json` (the fleet contract is identical for every agent,
so shared-by-construction is correct); onboarding keys in `~/.claude.json`.

Then **per agent** `i` with workdir `~/work/<identity>`:

1. clone `repo` if the workdir is empty (missing repo → plain-folder seat,
   legitimate as ever);
2. write the marker `{name, project}` — per-workdir, so the "single-valued
   marker" problem dissolves;
3. write `.mcp.json` with `?agent=<identity>` — **project scope, never user
   scope.** ⚠️ This rule is now load-bearing again: 1:1 got to say "safe
   because the container's `~/.claude.json` is per-seat, not shared"
   (`seat.py:mcp_json_content`). In N:1 HOME **is** shared, so the 2026-07-27
   DM-misroute class (an `?agent=` stamp in a shared user-scope file pushing
   one agent's DMs into another's session) is live again. The stamp lives in
   the workdir's `.mcp.json` and nowhere else.
4. seed folder trust for the workdir (`seed_first_launch`, per path);
5. launch tmux session named `<identity>` (already dot-free — the sanitize
   rule exists because tmux reads `.` as its pane separator);
6. run the launch dance against that session; refuse loudly on any unknown
   dialog, per session;
7. send the first turn (`first_turn_prompt`, which now carries squads).

Once more per container: write **`~/work/<squad>.code-workspace`** listing
every agent workdir, with the standard settings block — the file that makes
the Dev Containers door a squad view. Then PID 1 supervises **all N**: one
status file per agent (`status-<identity>.json`), `needs_reregister` evidence
rule per agent, nudge typed into that agent's session only.

Gate economics, from the six-gates list: theme, onboarding, and the bypass
acceptance are **HOME-level** — they settle once per container, not once per
agent. Trust and the launch dance are per-workdir/per-session. N:1 does not
multiply the worst gates.

### Edge

- `create_argv`: when `spec.agents` present, inject `SEAT_MANIFEST` instead
  of `SEAT_IDENTITY` (still after spec env, last-wins, so a stale spec can't
  smuggle a divergent manifest).
- Enumeration: **unchanged.** The container name is the placement's unit name.
- Harvest: `docker exec -w /home/seat/work/<identity> <container> mcp-hub
  memory-export`, once per agent — cwd selects identity, as it already does.
  `memory_volume` presence stays the agent-vs-service line; reclaim harvests
  **all N** before destroying, and the report names each.

### Hub + board

- No schema migration. `spec.agents` is a key in the JSON column that exists
  for exactly this reason.
- `fleet_tree.build_tree`: attribute registry agents to a container when their
  name appears in that container's `spec.agents` (today: when it equals the
  container identity — which becomes the N=1 special case of the same rule).
  The node's `agents: []` finally earns its plural.
- Attach affordance: one entry **per agent** — `docker exec -it <container>
  tmux attach -t <identity>`.
- `capsules attach`: enrols one roster row per member agent, folder = the
  host-visible path only if the workdir is bind-mounted; a container-local
  workdir means the host roster has nothing truthful to point at, and the tab
  is the `docker exec` attach instead. (Same refusal-over-guess rule as
  `capsule_attach_plan` today.)

## What N:1 costs — stated, not hidden

- **Blast radius**: container restart drops N agents at once; they all
  re-register (supervisor per session). Reclaim destroys N seats in one act —
  the placement is the unit, so that is by design, but per-agent retirement
  inside a running container becomes a *new* operation (kill one tmux
  session + unregister) that 1:1 got for free.
- **Shared uid**: all N agents run as `seat` and can write each other's
  worktrees. Same trust domain as a host squad sharing a machine — acceptable
  for squad-mates, and exactly why the container spec must hold ONE squad,
  not a faculty of strangers. The sandbox rules are unchanged and
  non-negotiable: no docker socket, non-root, no host mounts beyond the
  memory volume.
- **Shared HOME**: one `~/.claude` means one memory volume for N agents.
  Claude keys project state by encoded workdir path, so transcripts and
  memory separate naturally per agent — but the volume is one blob to the
  edge, and harvest granularity is per-agent only because cwd selects it.

## Build order (when approved)

1. `seat.py`: `parse_seat_manifest` + per-agent contract derivation — pure,
   testable without a container, mutation-checked like the rest.
2. `cli.py seat-entry`: the N-loop around the existing steps; `--prepare-only`
   exercises it in tests.
3. `edge.py`: manifest injection + per-agent harvest.
4. `fleet_tree.py` + `settings_app.py`: membership attribution + per-agent
   attach entries.
5. Image rebuild, then a live N=2 pod on dev-vm-1 beside the 1:1 squad —
   both shapes running at once is the acceptance test.
6. Dev Containers door measured last, as its own step, against the workspace
   file the pod wrote.

## Decisions (2026-08-07)

The three open questions were delegated to me ("pick some sensible behaviour").
Recorded here with the reasoning, so the next person reads a decision rather
than re-opens a question.

**1. Pod naming: `<squad>-pod-<machine>`, assigned by the placement.**

Sanitized by the same rule as agent identity (lowercase, non `[a-z0-9_-]` → `-`)
— dots especially, because tmux reads `.` as its pane separator and a dotted
name produces an agent that runs and cannot be addressed.

Machine-scoped because two boxes may each host a pod of the same squad, and the
suffix keeps `machine_of()` — which resolves a name by its hostname suffix —
working unchanged. Assigned, never derived inside the container: the rule that a
container's hostname must not be able to name anything is not relaxed for pods.

**2. The topology flag belongs on `place`, not `compose`: `capsules place <id>
--machine <m> --pod`.**

This is a change of mind from the question as posed, and the better answer.
`compose` FREEZES membership — a squad's seats as they are at that moment. How
those frozen seats are then REALIZED, as N containers or as one, is a property
of the placement, not of the freeze. Putting `--pod` on `compose` would bake a
substrate decision into an artifact whose whole job is to be inert and
re-placeable, and changing your mind about topology would mean re-freezing —
which also re-reads the squad and so is no longer the same capsule.

On `place` it costs nothing and buys something real: ONE capsule can be placed
1:1 on one machine and as a pod on another, which is exactly how the two shapes
get compared on equal footing.

**3. The pod is the unit of placement AND of reclaim. Per-agent retirement is
deferred, and PID 1 must not fight it.**

Reclaiming a pod harvests all N, then destroys once. Retiring ONE agent inside a
running pod stays an operator act (`docker exec <pod> tmux kill-session -t
<identity>`) until there is a reason for an edge op — a verb nothing has needed
yet is a verb written blind.

The load-bearing consequence: **PID 1 supervises REGISTRATION, not session
existence.** It nudges an agent that has fallen off the hub, as it does today,
but it never recreates a session that has gone. Otherwise the supervisor would
resurrect a deliberately killed agent and the operator's only per-agent control
would be in a fight with the container's own init. When the LAST session ends,
PID 1 exits and the container stops — which mirrors 1:1, where the container's
lifetime is its session's.
