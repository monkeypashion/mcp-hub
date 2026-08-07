# mcp-hub-seat — the seat container contract

The image that makes "start a squad on docker" true. One container = one
claude seat: a repo, an **assigned** identity, a hub connection, and a
credential. The edge places it (`substrate='docker'`), observes it, and
reports it — this document is the contract between the image and everything
that launches it.

**The image is deliberately generic.** Nothing inside it knows whether the
seat is a hub-maintainer, a scratch agent, or (later) a factory build seat
driven by reliable-ai + a features.json. A factory interactive seat is the
same shape with a different repo — keep it that way. Env names and exit
codes below are chosen to match the factory's codespace mechanism
(`dreamteam/scripts/codespace-runner.js`) so one seat dialect serves both
estates.

## Environment contract

Injected by the launcher (edge `DockerExecutor` via the seat's `spec.env` /
`spec.env_from_host`; capsule compose stamps the same names).

| Var | Required | Meaning |
| --- | --- | --- |
| `SEAT_IDENTITY` | yes | Hub agent name, **ASSIGNED by the placement** — never derived inside the container (a container hostname is noise; `bootstrap.sh` already states this rule). |
| `SEAT_PROJECT` | no | Hub project string. Default: derived from the repo's `origin` URL when present, else `SEAT_IDENTITY`. |
| `SEAT_REPO` | no | Git URL cloned into the workdir at first start when the workdir is empty. Absent → the seat runs on whatever the workdir holds (plain-folder seats are legitimate, same as `squad add-folder`). |
| `MCP_HUB_URL` | yes | Hub endpoint. `.mcp.json` is **generated from this at start, never baked into the image** — a baked URL is the transport `.mcp.json` mistake in a new costume. |
| `SEAT_MODE` | no | `interactive` (default) or `headless`. See modes. |
| `SEAT_PROMPT` | headless only | The prompt for `claude -p`. |
| `SEAT_SQUADS` | no | Comma-separated squads passed to `register()`. Empty preserves, as always. |
| `SEAT_GITHUB_TOKEN` | to clone | **How a container fetches its own code.** Same channel as the Anthropic credential — hub stores the NAME, the edge host supplies the VALUE via `--env-from-host`. Installed as a git credential HELPER, so the token is never written to disk; a token embedded in the clone URL would persist in `.git/config` as `remote.origin.url`, survive the container, and show up in `git remote -v`. Not needed when the workdir is bind-mounted from the host, which is every seat on the fleet today. |
| `CLAUDE_CODE_OAUTH_TOKEN` | one of | **The default lane** (operator decision 2026-08-04, card #353): a long-lived Claude Code OAuth token minted by `claude setup-token` on the edge host, injected via `--env-from-host` — the hub stores the NAME only, the value never enters the control plane. |
| `ANTHROPIC_API_KEY` | one of | **The override lane.** API billing. Same `--env-from-host` channel. |

### Why `SEAT_GITHUB_TOKEN` and not `GITHUB_TOKEN`

**One token per role, each with its own name** — dt's rule, 2026-08-07, paid
for with a real incident (`codespace-runner.js:1138-1151`): the factory
injected a deps-READ token as `GITHUB_TOKEN`, it clobbered the environment's
native WRITE token, and every agent push failed with "Repository not found".
Nothing collides in a docker seat today; the name is prefixed anyway because
this image is aimed at the factory estate too, where the collision would
surface only on a push and long after anyone would connect it to this choice.

### Where this credential is WEAK, and the shape of the fix

dt reviewed our approach and was explicit about both halves:

- **Storage: ours is the better one, keep it.** The factory does
  `git remote set-url origin https://x-access-token:${t}@github.com/...`
  (`codespace-runner.js:842`), which writes the token into `.git/config`. They
  get away with it because codespaces are torn down; **our containers live for
  days, so that pattern would be strictly worse here than there.** Match the
  factory's env names and exit codes — do NOT converge on this.
- **Scope: ours is the weaker one.** A PAT is broad and long-lived where their
  App installation token is narrow (`contents:read`, ~1h) and, critically,
  **the caller cannot name the repos** — their gateway derives the repo set
  from its own code, so a compromised caller cannot widen its own scope.

The eventual fix is a gateway verb minting an installation token for a SEAT
identity, with the helper calling it **per git operation** rather than caching
it. That keeps the property we already have and adds the one we lack.

⚠️ It cannot simply reuse the existing endpoint: `POST
/provision/github-build-token` is gated behind `USE_GATEWAY_GITHUB` /
`USE_GATEWAY_CODESPACE_CREDS` and scoped to product builds keyed on a factory
project uuid. Seats are not product builds and have no such uuid — that is a
new verb, not a new caller.

⚠️ And a short-TTL token must **never be resolved at container start**. dt has
a live defect of exactly this shape: a 1-hour `ghs_` persisted into a durable
field, three deployed apps silently un-rebuildable, nothing alarming because
the running containers kept serving. Fetch at point of use, or store an
indirection that resolves at point of use.

## Auth: validate, never arbitrate

The entrypoint enforces **presence and plausibility**, and nothing else.
Which credential wins when both are set is Claude Code's own auth
hierarchy — the factory's comments say API key outranks OAuth token, but
**nobody has ever measured it** (dt confirmed the comment is hypothetical,
2026-08-04). We measure it once on the first live seat and record the result
here; the entrypoint never reimplements it either way.

Validation rules (dt's hard-fail shape, `codespace-runner.js:2288-2302`,
built from two real incidents):

- `CLAUDE_CODE_OAUTH_TOKEN` set → length **≥ 50** or fatal.
- else `ANTHROPIC_API_KEY` set → length **≥ 20** or fatal.
- else → fatal.
- **Presence is not validity**: an empty `export X=` passes `-z`-style
  set-ness checks and lets claude start unauthenticated — the factory lost
  five silent weeks to the un-length-checked variant. Check length, always.
- Fatal = **exit 42**, stderr names the plausible causes (secret deleted?
  empty export clobbered a good one? launcher env not reaching the
  container?). 42 is the factory's auth-death code; keeping it means an auth
  failure is never misread as a build failure in either estate.

A seat that would die authenticating **dies at the door, loudly**, where the
edge observes the container exited — never three prompts into a turn.

## Identity: the marker, not derivation

Derived identity (git remote + hostname) is wrong in a container by
construction. The entrypoint therefore:

1. Writes `<workdir>/.claude/hub-agent.json` =
   `{"name": $SEAT_IDENTITY, "project": $SEAT_PROJECT}` (the legacy-marker
   shape, `cli.py:_discover_agent_from_marker`).
2. Does **NOT** opt the repo into `~/.mcp-hub/config.json` `projects` —
   derivation only outranks the marker for opted-in repos, so staying out is
   what makes the assigned identity win. This is load-bearing; an "opt in for
   completeness" edit would silently rename every containerized seat.

Zero cli changes needed: hooks and statusline already resolve the marker.

## What the container promises once running

- Hooks installed (container-local `~/.claude/settings.json`): SessionStart
  register + heartbeat-daemon, Stop hook — the standard fleet contract, so a
  containerized seat is ⚡ from session start like any other.
- claude runs under **tmux** (session `seat`) with the channels flag, so hub
  push wake works and an operator can attach:
  `docker exec -it <name> tmux attach -t seat`.
- The hub sees a normal agent. Nothing on the hub knows or cares that the
  seat is containerized — placement state lives in the edge, presence lives
  in the registry, same as every other seat.

## Permissions — the container is the sandbox

Operator decision 2026-08-05 (card #360): a seat runs with
`permissions.defaultMode: bypassPermissions`, so it can actually work.
The first live seat proved the alternative is useless — it woke, went to
work, and stopped on `git status` waiting for an approval nobody inside a
container can give. Docker already bounds the blast radius; a second
bound that no one can clear just means nothing happens.

🔴 **THE RULE THAT KEEPS THIS SOUND — a seat must stay genuinely
contained:**

- **NO DOCKER SOCKET, ever.** `-v /var/run/docker.sock:...` in a seat spec
  turns bypass mode from "sandboxed" into "root on the host". If a seat
  ever needs to manage containers, that is the EDGE's job, from outside.
- **Non-root user** (`seat`), as the image already does.
- **No host mounts beyond its own `memory_volume`.** A seat with the
  host's home mounted is not sandboxed by anything.
- The comms allowlist stays alongside the mode, so a seat can still
  register and report if the mode is ever disabled by policy.

A seat that needs to violate any of these is not a seat — it is a
privileged tool, and it should be an explicit operator act rather than a
placement.

### The ONE mount exception — a credential socket, and why it is not a precedent

⚠️ Read this before citing it for anything else. **Someone will eventually
point at this exception to justify mounting the docker socket. This paragraph
is what stops them.**

A seat that must fetch code needs a GitHub credential, and the naive design —
a token inside the container, or worse a credential that lets the container
MINT tokens — puts capability where the untrusted output runs. The codespace
path never did this, and nobody had written down why: **the runner calls the
token service from the HOST and injects only the results. A codespace holds
tokens; it cannot mint** (dt, 2026-08-07). A container living for days with
mint capability is strictly worse than one that dies in an hour holding a
token: an adversary with execution inside does not steal a credential, they
hold the ability to make new ones.

So the seat is permitted **one** additional mount: a **unix socket to a
mint-only agent**, one per agent, run by the estate that owns the credential —
never by this one. The in-container helper is a thin forwarder speaking git's
own credential-helper protocol.

**Why this is allowed when the docker socket never is** — and the contrast IS
the rule:

| | credential socket | docker socket |
| --- | --- | --- |
| What it exposes | one operation: mint a token already scoped by the far side | the full Docker API |
| Ceiling | the far side's scope; the container cannot widen it | root on the host |
| Effect on the seat | **strictly LESS** capability than the credential it replaces | unbounded escalation |
| Revocation | kill the agent or unmount — capability dies, container lives | none |

The test to apply to any future mount request: **does it give the container
strictly less capability than the alternative it replaces, and is its ceiling
enforced by something outside the container?** A mint-only socket passes both.
`/var/run/docker.sock` fails both, and no amount of "but we already allow a
socket" changes that.

Design record: `memory/project_interactive_factory_build_2026_08_07.md`.

## Modes

- **`interactive`** (v1): long-running claude session under tmux, registered
  on the hub, driven by hub messages and/or attach. The container's lifetime
  is the session's.
- **`headless`** (structure reserved, not shipped in v1): `claude -p
  "$SEAT_PROMPT"`, one shot, exit code is the verdict — the shape
  codespace-runner runs today. Kept as a mode of the SAME image so a future
  factory build on the edge is a flag, not a fork.

## Volumes

- `spec.memory_volume` mounts at **`/home/seat/.claude`** — the whole claude
  state dir (memory, transcripts, credentials cache). Its presence is the
  agent-vs-service line the edge already enforces: present → reclaim
  harvests before destroying; absent → the seat is stateless and reclaim
  says so.
- The workdir (`/home/seat/work`) is container-local unless the spec mounts
  it. A `SEAT_REPO` seat treats the clone as disposable — what's pushed is
  what's real (the transport gate's rule, applied to containers).

## Exit codes

| Code | Meaning |
| --- | --- |
| 42 | Credential missing/implausible (pre-claude, at the door) |
| 43 | Contract violation other than auth (no `SEAT_IDENTITY`, no `MCP_HUB_URL`, clone failed) |
| else | claude's own exit status, passed through |

## Non-goals (v1)

- **No model-gateway support.** The factory's gateway is launcher-side
  per-machine config, not seat-inheritable (dt, 2026-08-04). If the fleet
  adopts a gateway later, it arrives as three env vars in `spec.env` — the
  contract already carries arbitrary env, so nothing here changes shape.
- **No credential storage.** Not in the image, not in the hub. The token
  lives in the edge host's environment; `--env-from-host` is the only door.
- ~~**No subscription-concurrency claims.**~~ **MEASURED 2026-08-06:** three
  seats (`mcp-hub-seat-dev-vm-1`, `mcp-hub-cap-dev-vm-1`,
  `vps-hetzner-cap-dev-vm-1`) ran concurrently on ONE `CLAUDE_CODE_OAUTH_TOKEN`
  on dev-vm-1, all registered and ⚡ at once. The "start at one" caution is
  discharged for three; nothing above three has been tried, and the number
  that matters is per-account concurrency, not per-container — so N seats in
  ONE container (docs/n-seats-per-container.md) asks the same question, not a
  new one.
