# `/voice` in a containerised seat — design

Status: **DESIGN v3, NOTHING BUILT.** v1 (per-seat FIFO) rejected on the
operator's requirements; **v2 (host pushes to container) falsified by
measurement** — see below. v3 inverts the direction. Every rejected version is
kept, because each was killed by something worth not rediscovering.

## The requirements that decide everything

Operator, 2026-08-07:

> it must work on **all** container configurations … they all need to work the
> same way, any configuration, and it all needs to be **fully automated**. We
> can't be dependent on any **external systems** for the set up. It must be
> **from inside out** auto configured.

1. **Every configuration, identically** — 1:1, N:1 pods, containers this system
   did not create.
2. **Fully automated** — no per-container step anyone can forget.
3. **No external dependency** for setup.
4. **Inside-out** — the container configures itself.

## The design: the container PULLS

```
 host                                    container (ANY configuration)
 ──────────────────────────────────      ─────────────────────────────────
 claude_mic.monitor                      entrypoint, identical everywhere:
     │                                     read default gw from /proc/net/route
     │                                     connect OUT to <gw>:<port>
     │                                     send seat name  ── handshake ──▶
     ▼                                     read stream -> own pulse sink
 host verifies name, then                        │
 streams one-way, NEVER reads  ──────────────────┘
                                                 ▼
                                           arecord / `/voice`
```

**The host never addresses a container.** It does not discover, look up, or
hold an address. It answers connections and streams.

### Why this is the narrowest thing that works

- **No listener in the container**, so container-to-container injection has
  **no path** — not a blocked path, no path. That is structural, not a filter.
- **No discovery, no lookup, no address churn, and no IP-reuse misroute** — the
  whole class disappears because nothing on the host is aimed at a container.
- **Adopted containers work identically**: they self-connect at entrypoint, so
  nothing has to know they exist.
- **The gateway address is self-discoverable from inside** (`/proc/net/route`,
  measured — no configuration).
- **Literally inside-out**, which is the operator's word.

### ⭐ The argument that decides it: which way does it fail?

v3 needs **one ufw PERMIT rule** (`docker0 → host:<port>`). v2 needed **one
DOCKER-USER DENY rule**. Same cost, opposite failure mode:

| | missing / inert |
| --- | --- |
| **PERMIT** rule (v3) | **fails CLOSED** — no audio, loudly, immediately |
| **DENY** rule (v2) | **fails OPEN** — no isolation, silently, while audio works perfectly |

Tonight was a catalogue of things silently absent while looking healthy. Prefer
the failure mode *"voice doesn't work"* over *"the isolation isn't there and
nothing says so."*

### The handshake earns its place

Any container on the bridge could connect. The container states its seat name
and the host verifies before streaming — so a wrong peer is a **detectable
mismatch** rather than silent audio to the wrong agent. It is doing real work,
not decoration.

### Carried forward unchanged: TCP is lossless-with-backpressure

Same trap as the FIFO in v1. The host writes **non-blocking and DROPS on
would-block**. A stalled seat must never stall the stream. For realtime audio
dropping is CORRECT.

## The wire contract — both halves build against exactly this

> **CONTRACT LAST CHANGED AT `c6205fb`** (the handshake charset). If the commit
> you built against is older than that, **you are contract-compliant against a
> contract that no longer exists.**
>
> 🔴 This line is here because that happened, live. The host half was pinned to
> `57d1c4a` and its running listener **refused dotted container names at the
> wire, silently**, while being a faithful implementation of the contract as it
> stood when it was written. Nothing told it. Both halves share
> `src/mcp_hub/voice.py`, so a widening on one side is *invisible* to the other
> until it rebases — a shared document is not a shared build.
>
> ⇒ Update this SHA in the same commit that changes anything in the block
> below. A contract that cannot tell you it moved is not doing the job.

```
listen    172.17.0.1:6981/tcp   ← the DOCKER GATEWAY ADDRESS, never 0.0.0.0
audio     raw PCM s16le, 16000 Hz, 1 channel (no header, no framing)
handshake container sends ONE line then never writes again:
              "MCPHUBVOICE1 <seat-name>\n"   (ASCII, ≤128, docker's own rule:
                                             [a-zA-Z0-9][a-zA-Z0-9_.-]*)
host      read the line (5s timeout) -> verify -> stream one-way, NEVER read
```

Anything malformed: **drop silently** *to the peer* — but **log it host-side**,
with the address, the claim and the reason. This listens where any container can
reach it, so explaining yourself to whoever knocked is not on; being unable to
explain it to the OPERATOR is a different thing, and every failure here presents
identically as "no audio". Fail-closed is correct; fail-closed and
undiagnosable is how a correctly-configured seat stays silent for a week.

⚠️ **The charset mirrors docker's container-name rule for a measured reason.**
It was `[A-Za-z0-9_-]`, and `docker create --name voice.dotcheck.tmp` succeeds
(measured on a live daemon, mcp-hub-dev-vm-1-general 2026-08-08) — so a
legally-named container was refused **at the wire, before authorisation ever
ran**, and silently. The first character stays alphanumeric so `..`, `.hidden`
and `-rf`-shaped strings cannot become a name that reaches a log line or an
argv. The charset is not the security boundary; the roster is.

### 🔴 BIND THE GATEWAY ADDRESS, NEVER `0.0.0.0`

Measured on dev-vm-1: `ufw status` shows `Anywhere on tailscale0 ALLOW
Anywhere`. **The tailnet is unrestricted inbound.** So a wildcard bind puts a
continuous live feed of the operator's microphone on every tailnet peer.

⚠️ **The `ufw` rule we are ADDING does not cause this — the tailscale allow
already there does.** Same class as the `DOCKER-USER` finding: *the thing that
grants access is not the thing we are changing, so reviewing only our own diff
misses it.* Scope the rule too: `from 172.17.0.0/16 to 172.17.0.1 port 6981`,
not a bare port-open.

### The ADDRESS is the identity; the CLAIM is advisory

`MCPHUBVOICE1 <seat>` proves nothing. Seat names are public and guessable, and
**the claim is asserted by the very party being checked** — it cannot
authenticate itself. The handshake solves the **misroute** problem, which was
its job; it does not stop a rogue container eavesdropping.

⇒ **Ask docker which container holds the connecting address, and treat that
answer as the identity.** Docker is a third party vouching. Authentication
without secrets. **IP reuse cannot bite here** — the connection is established,
so the address is current by definition — and it works for adopted containers
because it asks docker rather than needing something injected at create time.

⚠️ **This section used to say the address must MATCH THE CLAIM, and that was
wrong** (corrected 2026-08-08 after measurement). A container created with an
explicit `--hostname` reports a name with **no relationship to anything docker
knows it by** — measured: hostname `totally-unrelated-name`, docker name
`voice.drift.tmp`, id `ecde055f0717`. Under match-the-claim such a container is
refused permanently and silently. The claim is now logged, and refused only
when it **contradicts** the address by naming a *different* live container —
the impersonation case, which the handshake alone could never have caught.

🔴 **AUTHENTICATE ≠ AUTHORISE — do not let the second disappear with the
first.** Identity-from-the-address answers *"which container is this?"*, not
*"may it listen?"*. Dropping the roster check along with the claim makes the
gate "any live container on the bridge", which hands the operator's **live
microphone** to anything anyone ever `docker run`s on the box — a CI job, a
scratch image, a database. Nobody starting a container thinks they are starting
a microphone client. So:

```
authenticate:  peer address -> docker -> the container's REAL name + id
authorise:     is that container in the roster (property 5)?
```

Both. The first is what makes adopted seats work under any name; the second is
where *"this one may listen to the room"* is actually decided.

### Seven properties the host side must hold

1. **Non-blocking write, DROP on would-block.** Do not retry, do not enlarge
   the buffer: neither prevents a stall, both lengthen it, and what drains is
   stale audio transcribed as current.
2. **Fail CLOSED on an unreadable roster.** "Cannot check who is asking" must
   resolve to *no audio*.

   ⚠️ **WHICH roster — see property 5, and read it before reaching for the
   obvious thing.** The host author reached for two wrong ones in a row before
   the right one was traced, and *both were the natural choice at the time*:
   `mcp-hub seats list` (which is a **network** call, so it cannot be the
   answer) and the container **image** (which is a fact about today's fleet,
   not a rule about seats). The answer is the local
   `~/.config/squad/squad.conf`. Naming it here because a reader who arrives at
   "fail closed on the roster" has already assumed they know what the roster
   is.
3. **Reap dead peers.** A SIGKILLed container can take ~15 minutes for TCP to
   fail a write, and drop-on-would-block would cheerfully drop into a corpse.
   Keepalive or reap on sustained would-block — otherwise *"the seat is deaf"*
   and *"the seat is gone"* are indistinguishable.
4. **Newest connection wins.** A restart can leave a half-open stream; without
   this we fan one microphone into two sockets and nobody knows which is live.
5. **The roster lookup is LOCAL and bounded — and read PER CONNECTION, not
   cached.** ⚠️ This clause previously said "cached", which conflated two
   different concerns and would have produced exactly the divergence it exists
   to prevent (caught 2026-08-08, after I advised the host author of the
   opposite of what this line said).
   - **LOCAL** is the real requirement: network I/O here makes audio inherit
     the hub's availability — breaking the no-external-dependency rule — and a
     hung lookup blocks the accept loop.
   - **Not cached** is a separate property, and reading a local file per
     connection costs nothing. A roster cached for the process lifetime
     **outlives a retirement**, and a container still receiving the operator's
     microphone after being retired is precisely the one nobody goes looking
     for. Same reason the docker map is fetched per connection.

   🔴 **THE ROSTER IS `~/.config/squad/squad.conf` — NOT the hub's seat list.**
   `mcp-hub seats list` goes through `OperatorApi.list_seats()`, an **HTTP call**
   (found by mcp-hub-dev-vm-1-general while implementing this). Authorising
   against it would make the operator's microphone depend on the hub being
   reachable — breaking the no-external-dependency rule outright — and let a
   slow hub stall the accept loop. So "LOCAL" does not describe *how* to read
   the seat list; it **excludes** it. Stated here as a refusal because the seat
   list is the obvious thing to reach for.

   The squad roster is the right list and needs no new machinery:
   ```
   ~/.config/squad/squad.conf     5-field pipe format, one row per agent
   name|worktree|?|@docker:<container>[:<session>]|class
                     ^^^^^^^^^^^^^^^^ field 4 — this row is a CONTAINER seat
   ```
   It is local, plain text, bounded, and — the load-bearing part — it is what
   **enrolment itself writes**, so it covers BOTH creation paths with no special
   case: the edge shells out to `squad add-container` (`cli.py`), and adoption
   *is* `squad add-container`. Membership in that file is the literal act of
   "this is one of ours". Pods put several rows on one container name; a
   membership test only needs the set.

   ⚠️ **Do NOT gate on the container IMAGE.** `squad add-container <name> <dir>
   <container>` takes the container as an argument and **never inspects its
   image** — it checks only that the folder exists and the name is not already
   enrolled. So an adopted seat may legitimately run anything at all, and an
   image gate refuses it *silently*. The image happens to be `mcp-hub-seat:*`
   on every seat alive today, which is exactly what makes it a convincing and
   wrong signal.
6. **Cap pending handshakes.** Any container can connect; N silent connections
   must not tie up the accept path.
7. **Retry the bind.** `docker0` may not exist at boot or across a docker
   restart. A service that exits once leaves audio dead until someone notices.

## 🔴 Why v2 (host pushes) was FALSIFIED — measured, not argued

v2 had the host discover containers and push to each one's listener, with a
`DOCKER-USER` rule as the ceiling and a receptor source-filter as defence in
depth. Both claims failed.

**Claim: "a DOCKER-USER rule is the ceiling, enforced outside the container."**
🔴 **FALSE on the box the seats run on.** Measured:

```
/proc/sys/net/bridge/bridge-nf-call-iptables  ->  DOES NOT EXIST
lsmod | grep br_netfilter                     ->  NOT LOADED
iptables -S DOCKER-USER                       ->  -N DOCKER-USER   (empty)
```

Two containers on the same `docker0` bridge talk over **L2 bridging**. Without
`br_netfilter` that traffic **never enters iptables**, so the rule filters
nothing. Injection was demonstrated live between two seat containers
(`172.17.0.7` → `172.17.0.6:6980`, victim logged the payload).

⭐ **The rule would have been PRESENT AND INERT.** I would have written it,
`iptables -S` would have shown it, and I would have reasonably called clause 3
satisfied — while injection still worked. That is **exactly** the failure class
this whole design process has been about: *verified by PRESENCE, not by FLOW.*
**A rule that exists is not a rule that runs.**

⇒ If any future design leans on a firewall rule, the acceptance test cannot be
"the rule is installed". It must be "**the attack, executed, fails**".

⚠️ And loading `br_netfilter` is not a free flag — it changes how ALL bridged
traffic on the host is handled, and becomes a **hidden prerequisite** a rebuilt
or different host silently lacks. That is requirement 3 broken invisibly.

**Claim: "address churn is just a lookup."** 🔴 **Answered the wrong problem.**
The objection was not staleness, it was **IP REUSE**: `172.17.0.6` was held by
two different containers minutes apart. A stale address does not point at
nothing — **it points at a different live seat**. Between discovery cycles, the
operator's voice is delivered to the wrong agent, with **no attacker, no
misconfiguration, just timing**, and every health check green on both sides.
Discovery shortens that window; it cannot close it, because the emitter's
target is only ever as fresh as its last lookup.

⚠️ On the source-filter defence in depth: seats run uid 1000 with
`CapEff: 0000000000000000` (no raw sockets, so no spoofing) — but `CapBnd`
still carries `NET_RAW` and the image ships **8 setuid-root binaries**. The
floor under that mitigation is lower than it looks.

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
⚠️ **Do NOT check "non-zero" — see acceptance criterion 1.** A live noise floor
measures ~1800 RMS with peaks into five figures, so non-zero is satisfied by an
empty room. Capture a QUIET floor and a SPEAKING sample in the same run on the
same box, and require the second to be at least 3× the first. Record both
numbers.

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

Split, if approved: container side (image, entrypoint, the outbound connect +
handshake) here; host side (the listener, the ufw PERMIT rule, non-blocking
writes) with `mcp-hub-dev-vm-1-general`, each owner able to verify their own
half.

⚠️ **The acceptance test for this design is not "it works".** Three things must
be demonstrated by execution, because every one of them has already been
believed on the strength of a component being present:

1. **Audio flows** — and 🔴 **NOT "RMS non-zero", which this criterion said
   until 2026-08-08 and which the noise floor satisfies on its own.** Measured
   on dev-vm-1 with **nobody speaking**: RMS 1765 / 2101 / 1781 across three
   captures, peaks to 12304. So `RMS > 0` is met continuously, by silence, and
   the test would pass on a rig where the operator's voice never arrives.

   It still distinguishes **dead** from **connected** — that is what caught the
   2026-08-08 outage, where the wire carried exactly-zero samples — but that is
   not what this criterion claims.

   ⇒ Make it a **comparison against a floor measured in the same run on the same
   box**, never an absolute threshold:
   ```
   floor  = RMS of N seconds of QUIET
   signal = RMS of N seconds while the operator speaks
   require signal >= 3 x floor        # and record both numbers, not a boolean
   ```
   ⚠️ **The margin is thinner than 3× sounds.** Speech measured 6638 RMS against
   a 1800 floor — **3.7×**. So the pass is real but not comfortable, and a
   quietly-spoken phrase could fail honestly. Record the numbers; a boolean
   discards the only evidence that would explain a marginal result.

   ⇒ Per-box, per-run: floors differ between machines and move with input gain,
   so a floor measured yesterday or elsewhere proves nothing today.
2. **Injection has no path** — run the attack from a second container and show
   it fails. Not "the rule is installed".
3. **Removing the PERMIT rule kills audio** — proving the control is the thing
   actually carrying the guarantee, rather than something else happening to
   work.
