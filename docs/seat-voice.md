# `/voice` in a containerised seat — design

Status: **DESIGN, converged, NOTHING BUILT.** Agreed 2026-08-07 between
`mcp-hub-fireblade-wsl` and `mcp-hub-dev-vm-1-general` at the operator's
direction. No build starts without the operator.

## The shape: an audio ISLAND per container, fed one-way

The host already receives the operator's voice: a VBAN receptor binds
`0.0.0.0:6980`, accepts stream `claude-mic` from **any** source IP, resamples
every emitter to 16kHz mono, mixes them, and feeds one long-lived `pacat` into
a null sink `claude_mic`. That is host-side and stays exactly as it is.

For each seat:

```
host:  parec -d claude_mic.monitor  ->  /run/claude-voice/<seat>/mic   (a FIFO)
                (one emitter process per seat)          |
                                                 bind-mounted DIRECTORY
                                                        |
container: receptor reads the FIFO -> its OWN pulse server -> its OWN
           claude_mic null sink -> ALSA default -> arecord -> /voice
```

**Nothing of the host's is in the container's mount namespace** — no pulse
socket, no host audio server, no network listener in the container.

## Why not the obvious thing (mount the host pulse socket)

Proposed first, **rejected by both of us**. It grants read *and* write on the
host's audio server:

- **Capture** — continuous, unobserved access to the operator's voice. PTT
  gates which seat *transcribes*, never what a seat *could* record.
- **🔴 Injection** — the socket is bidirectional, so a seat can PLAY into the
  shared `claude_mic`, and every OTHER seat's `/voice` transcribes it as if the
  operator had spoken. One container putting words in the operator's mouth and
  peer agents acting on them. **This outranks eavesdropping**: passive capture
  leaks, injection acts. It generalises — *any shared bus mounted read-write
  has this shape.*
- **Ceiling INSIDE the container** — once the socket is in the namespace the
  host cannot scope what the client does, and revocation means destroying the
  container. Fails the mount rule's clause 3 (see `seat-image.md`).

The island's ceiling is outside, literally: **stop emitting and that seat is
deaf.** Per-seat, revocable with `kill <pid>`, no container cooperation.

## Why a FIFO rather than a per-container UDP port

An earlier version published a per-container UDP port and ran a second VBAN
receptor inside. The FIFO removes a port allocator from the container-creation
path (ports are exactly the state that drifts), and a pipe has **no protocol
surface at all** — it is bytes, not a client/server.

**Cross-host seats do not argue for UDP**, which was the gate question:
**VBAN already crosses machines; the FIFO only has to cross the container
boundary.** A container always runs ON a host, and that host can always run a
mixer fed over the tailnet — the receptor accepts `claude-mic` from any source
IP, which is what makes it host-agnostic. So the container hop is host-local
*by construction*, not by limitation, and using UDP there re-implements one
layer up what VBAN already does better.

Evidence the VBAN layer genuinely spans machines rather than that being a hope:
dev-vm-1's mixer log shows two distinct emitters — `100.103.25.103` at 16kHz/1ch
and `100.123.223.50` at 48kHz/2ch — joining repeatedly Aug 4–7, different
formats, resampled and mixed. Multi-source is in daily use, not theoretical.

⇒ A remote host with no audio rig should be **refused, not served**: build the
rig there (one systemd unit, the same receptor) rather than streaming audio
host-to-host on top of a transport that already does that.

## The three things that make a FIFO work — none optional

### 1. Bind-mount the DIRECTORY, never the FIFO node

`-v /run/claude-voice/<seat>:/run/claude-voice` — the pipe lives *inside*.

Two failure modes if you mount the node:
- If the host path does not exist at container start, **Docker creates a
  DIRECTORY there**, and the container finds a directory where it expects a
  pipe — the error surfaces far from the cause.
- 🔴 **A bind-mounted FIFO pins the INODE.** If the emitter restarts and
  recreates the pipe (any `unlink`+`mkfifo` path), the container keeps the old
  orphaned inode. Host writes to the new pipe, container reads the dead one,
  and **the seat goes silently deaf while every health check looks fine** —
  sink present, reader running, no error anywhere.

Recreation inside a mounted directory is visible to the container; the mount
survives it.

### 2. Drop frames — a FIFO has the WRONG backpressure semantics

⭐ The property being given up by leaving UDP, and it is invisible because it
is a *non-feature*: **UDP drops when the far end cannot keep up, and for
realtime audio dropping is CORRECT.** VBAN is fire-and-forget for this reason.

A FIFO is lossless-with-backpressure. The default pipe buffer is 64KB — about
**2 seconds** at 16kHz mono s16 — and once a container's reader stalls (load,
`docker pause`, a wedged receptor) the buffer fills and **the writer blocks**,
propagating the stall backwards into the audio path.

⇒ Open `O_NONBLOCK` and **drop on `EAGAIN`, explicitly.** A stalled seat must
never apply backpressure. With a shared fan-out this would stop being a latency
bug and become one seat stalling the whole fleet — which is the main reason the
emitter is per-seat.

### 3. Reader-absent and reader-death

- Opening a FIFO for write **blocks until a reader appears**, so the emitter
  must not hang waiting for a container that has not started: open `O_RDWR`
  (keeps a reader fd alive, so open never blocks and EOF never arrives), or
  `O_NONBLOCK` with retry.
- On container restart the writer takes `EPIPE`/`SIGPIPE`. **Handle and
  reopen** — otherwise a routine seat restart permanently deafens it, silently.

## Container-side requirements (measured: all four seats have ZERO audio wiring)

- **packages** (base Debian 13 trixie): `alsa-utils libasound2-plugins
  pulseaudio-utils`. `libasound2-plugins` is the ALSA→pulse pcm plugin — the
  piece everyone forgets.
- **`/etc/alsa/conf.d/99-pulseaudio-default.conf`**: `pcm.!default { type
  pulse }` / `ctl.!default { type pulse }`. Without it `arecord` fails with
  *"audio open error: No such file or directory"*.
- **`~/.config/pulse/client.conf`** for the seat user, pointing at the
  **container's own** server. The ABSOLUTE path matters: tmux-launched agents
  often lack a usable `XDG_RUNTIME_DIR` and libpulse then finds nothing. This
  trap already cost time on the fireblade rig.

## Pods

One island per **CONTAINER**, serving all N agents — not one per agent.

⚠️ So injection between a pod's own members remains possible. Accepted: a pod
already shares HOME and lifecycle, so its members are inside one trust boundary
by construction. Stated here so nobody later reads pod-internal injection as a
regression.

## Verify at the destination, never at the source

```
docker exec <seat> arecord -d 3 -f S16_LE -r 16000 -c 1 /tmp/v.wav
```
then check RMS/peak is non-zero **while someone speaks**. The two failure modes
are distinguishable and that is the point:

- **silent but successful** — the path is right, no audio is arriving
- **hard "audio open error"** — the ALSA conf is missing

## Known, accepted

`/voice hold` latches capture for a session, so a latched seat picks up words
the operator PTTs into another seat. **Pre-existing, not a cost of
containerising**: ~16 tmux sessions have shared this sink since Aug 4, and
PipeWire's pulse server arbitrates per client stream with no concept of
namespaces — a containerised seat and a tmux seat are the same object to it.

## Not built

Every line above is design. Nothing is implemented, no image is rebuilt, no
emitter exists. When it is built, `mcp-hub-seat` must be rebuilt under a **DATE
tag, never over `:latest`** — three live 1:1 seats run `:latest`.
