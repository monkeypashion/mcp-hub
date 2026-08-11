# Seat repo access — the host clones, the container mounts

**Status: PROPOSED, not built (2026-08-11).** Shape approved by the operator;
this doc is the design against which it gets built and checked.

## What runs today

`spec.env.SEAT_REPO` names a repo; `seat-entry` clones it **inside the
container**, authenticating with `SEAT_GITHUB_TOKEN`, which the edge injects
via `--env-from-host` (`seat.py:683-743`). The token never touches disk — the
credential helper reads it from the environment at the moment git asks
(`credential_helper_argv`), deliberately avoiding the
`https://user:$TOKEN@github.com/...` form that would persist it in
`.git/config`.

That is a careful design, and it still puts a live credential **inside a
container running `permissions.defaultMode: bypassPermissions`**.

## The approved shape

> The host clones — where the credential already lives, in the edge's
> `~/.mcp-hub/edge-env` — and the container receives a **directory**, never a
> token.

Why it wins:

- **The credential leaves the container entirely.** Nothing to leak, nothing
  to cache, and the "helper must never cache" rule becomes moot rather than
  enforced.
- **It answers the transcript-leak finding by construction** — the value is
  never human-handled and never rendered.
- **Blast radius is one already argued and accepted**: a normal agent has full
  write access to its own worktree, so mounting one is the same blast radius
  as the thing it imitates. **NO DOCKER SOCKET stays absolute.**

## Three access patterns — do not conflate them

| need | mechanism | state |
|---|---|---|
| **assigned build repo** — any `dreamteam-ai-labs` repo assigned per interactive build (e.g. `browser-agent-test-fixture`) | host clone → bind mount | ⬜ this design |
| **own primary repo** — e.g. the mcp-hub agent joining a squad seat | host **worktree** → bind mount | ⬜ this design |
| **write back** — the seat pushes its own commits | needs a write credential; unresolved | ⛔ out of scope |

⚠️ **The repo is assigned PER BUILD, not fixed.** Access must be parameterised
by assignment, never baked to one repo.

## Mechanism

A new spec key, `repo_mount`, handled by the edge at **materialize** time:

```json
{ "repo_mount": { "repo": "dreamteam-ai-labs/browser-agent-test-fixture",
                  "ref": "main",
                  "dest": "/home/seat/work" } }
```

On materialize the edge:

1. Resolves the repo to an https URL (`https_repo_url` already does this, and
   already refuses an unparseable form rather than guessing — a wrong URL
   would clone the wrong repo under the right name).
2. Clones or fetches into a **managed root on the host**:
   `~/.mcp-hub/seat-repos/<seat-identity>/<repo>`, authenticating with the
   token from its own environment.
3. Appends `-v <that path>:<dest>` to the docker argv, beside the existing
   `memory_volume` handling (`edge.py:635-639`).
4. **Stops injecting `SEAT_GITHUB_TOKEN`** for seats using `repo_mount`.

**Per-seat directory, always.** Two seats sharing one host clone would corrupt
each other's index. For the *own primary repo* case the directory is a `git
worktree` of the existing host clone — isolation without a second copy, and it
is the shape already discussed for this case.

**Re-assignment costs a recreate**, which is now cheap: as of 2026-08-11 the
seat's `~/.claude` is a durable volume, so replacing the container preserves
memory and transcripts. That is what makes a per-build parameter practical
rather than destructive.

## 🔴 A gap this design WAKES UP — the volumes denylist does not cover `/home`

`check_volumes` (`spec_guard.py:165-206`) refuses `docker.sock` and the system
prefixes `/etc /root /boot /sys /proc /var/run /run`. **`/home` is not among
them.** A spec today may mount the operator's entire home directory into a
`bypassPermissions` container, and nothing objects.

That gap is **dormant only because no seat currently mounts a host path**.
This design's whole point is to start mounting host paths, so it must be
closed in the same change:

- **Positive constraint, not a bigger denylist**: a `repo_mount` may only
  resolve under the managed root `~/.mcp-hub/seat-repos/`. An allowlist is the
  right shape here because the set of legitimate destinations is small and
  known, whereas the set of dangerous host paths is not enumerable — the
  denylist has already been surprised once.
- Raw `spec.volumes` keeps the denylist, extended to refuse `$HOME` itself and
  `~/.claude`, `~/.mcp-hub`, `~/.ssh` — the paths that would hand a seat the
  operator's credentials and hook configuration.

⇒ Same lesson as [[reference_declared_is_not_enforced]] from a third angle: the
premise "the container is the sandbox" was enforced against the mounts anyone
had thought of. Adding a deliberate mount path is exactly when to re-ask what
the enforcement actually covers.

## Named limits — stated, not implied

- **No write-back.** The seat can build and test; it cannot push. The token
  stays on the host, so a push needs an edge-mediated act or fo's gateway verb.
  A seat that commits leaves those commits in the host clone, where they are
  recoverable but not published. This is the honest boundary of the
  read-only-first steer.
- **The fixture harness needs more than a repo.** The seat measured its own
  blockers: **no Node/npm**, no `.env`, no gh config. Repo access alone does
  not make `subphase-test.js` runnable — that is a seat-image change, built
  under a **DATE tag, never over `:latest`** (1:1 seats run that tag).
- **`SEAT_GITHUB_TOKEN` is `seat-dreamteam-readonly`** — fine-grained,
  `dreamteam-ai-labs`, **`dreamteam` repo only**, Contents read-only,
  ⏳ expires ~2026-09-08. It **cannot clone the fixture repo today.** Widening
  it is the functional-first step the operator ordered; when it is re-minted,
  **rotate in the same act** — a value already transited an agent transcript,
  and transcripts persist and transport.

## The bar (write it before building it)

- A `repo_mount` seat materializes with the directory mounted and **no
  `SEAT_GITHUB_TOKEN` in its environment** — asserted by reading the container's
  env, not the spec.
- Two seats assigned the same repo get **separate** host directories.
- A `repo_mount` resolving outside the managed root is **refused**, naming the
  root — deliberate negative, failing before the fix.
- A `spec.volumes` entry naming `$HOME`, `~/.ssh`, `~/.claude` or `~/.mcp-hub`
  is refused — each its own test, each failing pre-fix.
- Re-assignment: PATCH the repo, recreate, and the seat's `~/.claude` state
  **survives** — the property that makes this design usable.
