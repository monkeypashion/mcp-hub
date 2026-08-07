# `/voice` in a containerised seat — design

Status: **DESIGN v2, NOTHING BUILT.** v1 (per-seat FIFO) was rejected by the
operator's requirements on 2026-08-07; the reasoning is kept below because it
is why several things here look the way they do. Redesign approved same day.

## The requirements that decide everything

Operator, 2026-08-07:

> it must work on **all** container configurations … they all need to work the
> same way, any configuration, and it all needs to be **fully automated**. We
> can't be dependent on any **external systems** for the set up. It must be
> **from inside out** auto configured.

Four constraints, and they are not satisfied by "wire each seat up carefully":

1. **Every configuration, identically** — 1:1, N:1 pods, and containers this
   system did not create.
2. **Fully automated** — no per-container step anyone can forget.
3. **No external dependency** for setup.
4. **Inside-out** — the container configures itself.

⭐ Constraint 4 is the load-bearing one. It converts the question from *"how do
we wire a seat for audio?"* to *"how does a seat wire ITSELF?"*, and that
single change is what makes all the configurations collapse into one case.

## The design: uniform container, discovering host

```
 host                                           container (ANY configuration)
 ─────────────────────────────────────────      ─────────────────────────────
 claude_mic.monitor                             entrypoint (identical everywhere):
     │                                            pulse server
     │  one emitter per CONTAINER,                  └ null sink `claude_mic`
     │  created by DISCOVERY not config             └ ALSA default -> pulse
     ▼                                            VBAN receptor, 0.0.0.0:6980
 vban -> <container-ip>:6980  ──────────────▶      (its OWN netns)
                                                        │
                                                        ▼
                                                  arecord / `/voice`
```

### Why this satisfies constraint 4, concretely

**The container's audio configuration contains nothing about itself.** No
identity, no port allocation, no path, no address — the receptor binds a
**fixed** port because each container has its **own network namespace**, so
every seat in the fleet can bind the same port with no collision and no
allocator.

⇒ The image and entrypoint are **byte-identical in every configuration**. A
1:1 seat, a pod, and an adopted container run exactly the same audio setup,
because there is nothing configuration-shaped in it to differ.

### Why this satisfies constraints 1–3: discovery, not configuration

The host runs **one** service. It does not read per-container config, because
none exists. It enumerates **agent-seat containers from the roster and the
hub's seat records** — which is the same list that already knows about both
creation paths — resolves each container's current address, and maintains one
emitter per container.

- **1:1** — container is the seat. One emitter.
- **N:1 pod** — one emitter per **CONTAINER**, serving all N agents, because
  the island belongs to the container.
- **Adopted (`squad add-container`)** — works, **and this is the case v1 could
  not serve at all**. Nothing had to happen at creation time, so a container
  this system never created is not a special case.

⚠️ **Address churn is a lookup, not a problem.** Container IPs move across
restarts, which is why a static allocation would rot. Discovery asks docker
what the address is *now*, every cycle — the same reason the enumeration
contract asks `docker ps` what exists rather than trusting a side table.

## 🔴 Injection: the risk this design reopens, and how far the mitigation goes

A network path between host and container means container A can, in principle,
send UDP to container B's receptor — and B would transcribe it as the
operator's speech. **This is the risk v1's pipe eliminated structurally, and
this design does not.** Stated plainly rather than softened, because a
mitigation is not an impossibility.

Two layers, and they are different in kind:

1. **Receptor-side source filter** — accept only from the docker gateway. A
   seat's packets carry its own address and are dropped. This is *in* the
   container, so it is a filter the container itself enforces.
2. **Host-side firewall rule** — drop container→container traffic to the audio
   port in `DOCKER-USER`. **This one is enforced outside the container**, which
   is what the mount rule's clause 3 actually requires, so it is the layer that
   carries the guarantee. Layer 1 is defence in depth, not the ceiling.

⇒ Build **both**, and do not describe layer 1 as sufficient.

**Revocation** stays simple and outside: stop the emitter for a seat and it is
deaf. Per-container, no cooperation, no restart.

## Why not mount the host audio socket (v1's first rejection, still valid)

Rejected by both reviewers before the requirements even arrived. It grants read
*and write* on the host's audio server:

- **Capture** — continuous, unobserved access to the operator's voice. PTT
  gates which seat *transcribes*, never what a seat *could* record.
- **Injection** — bidirectional, so a seat can play into the shared sink and
  every other seat's `/voice` transcribes it as the operator. **Passive capture
  leaks; injection acts.** Generalises: *any shared bus mounted read-write has
  this shape.*
- **Ceiling INSIDE the container** — once the socket is in the namespace the
  host cannot scope what the client does, and revocation means destroying the
  container.

## Why not the per-seat FIFO (v1) — kept, because the analysis stays true

v1 mounted a per-seat directory containing a named pipe. It was **better than
this design on injection** — a pipe has no path from one container to another,
so seat-to-seat injection was impossible rather than filtered. It failed on the
operator's constraints instead:

- **Adopted containers could never receive it.** `docker create` happens in one
  place (`edge.py:491`, one caller); `squad add-container` *adopts* an existing
  container and never passes through it. A mount cannot be added to a running
  container without destroying it. So v1 served one creation path and silently
  withheld voice from the other.
- **It needed a per-seat host-side emitter created at seat-creation time** —
  precisely the external, forgettable step constraint 3 excludes.

⭐ **The trade, stated honestly:** v1 was stronger on isolation, v2 is stronger
on universality and automation. The operator's requirements chose universality,
and the injection guarantee is what was spent to buy it. That is a real cost,
not a wash.

Two findings from the v1 review that outlived the design:

- **A bind-mounted FIFO pins the inode**, so an emitter restart that recreates
  the pipe leaves the container reading a dead one — **silently deaf while
  every health check passes**. Not applicable to v2 (no mount), but the *shape*
  is: see the health-check note below.
- **A pipe is lossless-with-backpressure; UDP drops.** For realtime audio
  dropping is CORRECT, and v1 had to re-add it (`O_NONBLOCK`, drop on `EAGAIN`)
  because replacing UDP silently removed it. **v2 gets this back for free** —
  it never leaves UDP. ⇒ *When replacing a mechanism, enumerate what the old
  one did badly; that is where its useful properties hide.*

## 🔴 v1 also had an arbitrary-write hole, found after it was rejected

Recorded because it nearly shipped, and because the reason it cannot bite v2 is
not that we were careful.

v1 argued its per-seat directory was **inert** — "an empty directory is not a
capability" — and used that to license mounting it into every container.
`mcp-hub-dev-vm-1-general` falsified it with a working exploit: the container
replaces the FIFO with a **symlink**, the host emitter opens its own path
`O_RDWR` and writes, follows the link, and **clobbers an arbitrary host file as
`monke`** — a user in `sudo` and `docker`. The attacker picks the target, not
the bytes, which is destructive on its own.

⭐ **Our own hardening was the enabler.** `O_RDWR` was chosen so the emitter's
open would never block waiting for a reader; `O_RDWR` follows symlinks. *A
mitigation adopted for one property was the precondition for a failure in
another.*

The fix would have been `:ro` (measured: blocks `rm`/`touch`/`ln -s`, and a
FIFO still reads fine because opening a pipe `O_RDONLY` is not a filesystem
write) plus `O_NOFOLLOW` + `fstat`/`S_ISFIFO` on the writer. The general rule
now lives in `seat-image.md`: **a bind mount is inert only if READ-ONLY.**

⭐ **v2 needs none of it, because v2 has no mount at all.** The operator's
requirements removed the mount for universality and automation — and
incidentally removed a live arbitrary-write primitive nobody had found yet.
*A constraint imposed for one reason eliminated a hazard imposed by another.*

## Container-side requirements (measured: all four seats have ZERO audio wiring)

- **packages** (base Debian 13 trixie): `alsa-utils libasound2-plugins
  pulseaudio-utils` + the receptor's runtime. `libasound2-plugins` is the
  ALSA→pulse plugin — the piece everyone forgets.
- **`/etc/alsa/conf.d/99-pulseaudio-default.conf`**: `pcm.!default { type
  pulse }` / `ctl.!default { type pulse }`. Without it `arecord` fails with
  *"audio open error: No such file or directory"*.
- **`~/.config/pulse/client.conf`** pointing at the **container's own** server,
  by ABSOLUTE path: tmux-launched agents often lack a usable
  `XDG_RUNTIME_DIR` and libpulse then finds nothing. This trap already cost
  time on the fireblade rig.
- **entrypoint** starts the pulse server, the sink, and the receptor before any
  agent session. Same in every configuration.

## Pods

One island per **CONTAINER**, serving all N agents — not one per agent.

⚠️ Injection between a pod's own members remains possible. Accepted: a pod
already shares HOME and lifecycle, so its members are inside one trust boundary
by construction. Stated so nobody later reads pod-internal injection as a
regression.

## Verify at the DESTINATION, never at the source

```
docker exec <seat> arecord -d 3 -f S16_LE -r 16000 -c 1 /tmp/v.wav
```
then check RMS/peak is non-zero **while someone speaks**.

⭐ **Health checks that assert PRESENCE will pass through every failure in this
system.** Sink exists, receptor alive, emitter running, no error returned — all
true, all perfectly compatible with zero bytes moving. The check above is the
right shape *because it measures FLOW*. Any monitoring added later must do the
same or it is decoration.

## Known, accepted

`/voice hold` latches capture for a session, so a latched seat picks up words
the operator PTTs into another seat. **Pre-existing, not a cost of
containerising**: ~16 tmux sessions have shared this sink since Aug 4, and
PipeWire's pulse server arbitrates per client stream with no concept of
namespaces — a containerised seat and a tmux seat are the same object to it.

## Not built

Every line above is design. No image rebuilt, no emitter, no firewall rule.
When built, `mcp-hub-seat` must be rebuilt under a **DATE tag, never over
`:latest`** — three live 1:1 seats run `:latest`.

Split, if approved: container side (image, entrypoint, receptor) here; host
side (discovery service, emitters, `DOCKER-USER` rule) with
`mcp-hub-dev-vm-1-general`, each owner able to verify their own half.
