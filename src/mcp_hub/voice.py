"""`/voice` for containerised seats — the container PULLS (docs/seat-voice.md).

The container connects OUT to the host and asks for audio; the host verifies
who is asking and then streams one way, never reading. Two earlier designs died
to get here and both deaths are worth keeping in mind while editing this file:

  v1  host wrote into a per-seat FIFO bind-mounted into the container. Died on
      the operator's requirements (an ADOPTED container never passes through
      `docker create`, so it could never receive the mount) — and separately
      had an arbitrary-write hole: the container could replace the pipe with a
      symlink and make the host writer clobber a host file.
  v2  host discovered containers and pushed to a listener in each. Died to
      MEASUREMENT: the `DOCKER-USER` rule proposed as its ceiling would have
      been PRESENT AND INERT, because `br_netfilter` is not loaded and two
      containers on one bridge never traverse iptables at all. Injection was
      demonstrated between two live seats.

So the properties this file exists to hold, in order of how easily they are
lost:

* **No listener in the container.** Container-to-container injection has no
  path, rather than a blocked one. Nothing here may ever `bind()`.
* **The host never addresses a container.** No discovery, no lookup, no stored
  address — which is what makes docker's IP REUSE (observed: one address held
  by two containers minutes apart) unable to misroute one seat's audio into
  another.
* **Fail CLOSED.** The one firewall rule this needs is a PERMIT. If it is
  missing the audio stops, loudly and immediately. A DENY rule that is missing
  — or present and inert — fails open and silent, which is how v2 died.
* **Drop, never block.** The stream is lossless-with-backpressure and realtime
  audio wants the opposite. A stalled seat must never stall the host.

The pure functions are separated from the I/O deliberately: the rules are the
interesting part and a container is an expensive place to assert them.
"""

from __future__ import annotations

import errno
import socket
from typing import Any, Callable

# One fixed port, every container. This is only safe — and only possible —
# because each container has its OWN network namespace, so every seat in the
# fleet binds nothing and dials the same number. No allocator, no per-container
# config, and therefore nothing configuration-shaped to drift.
VOICE_PORT = 6981

# The audio the seats actually consume. 16kHz mono s16 is what the host mixer
# resamples every emitter to, so the container never has to negotiate a format.
VOICE_RATE = 16000
VOICE_CHANNELS = 1

# Wire protocol, deliberately one line of ASCII: the container's first act is to
# say who it is, and the host's first act is to check. Anything else is a
# protocol nobody can debug with `nc`.
VOICE_MAGIC = "MCPHUBVOICE1"

# Handshake must arrive promptly — a connection that opens and says nothing is
# either broken or probing, and either way it must not hold a slot.
HANDSHAKE_TIMEOUT_SECONDS = 5.0


def default_gateway(route_text: str) -> str:
    """The container's default gateway, parsed from /proc/net/route.

    This is the whole of the container's "configuration": it discovers where to
    dial from the kernel, so the image carries no address, no identity and no
    per-container anything. Measured on a live seat — the gateway reads
    `172.17.0.1` with nothing configured.

    Returns "" when there is no default route, which the caller must treat as
    "no audio" rather than guessing an address. A guessed gateway would dial
    something, and the whole point is that we never dial the wrong thing.
    """
    for line in route_text.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 3:
            continue
        # Destination 00000000 is the default route; gateway is little-endian
        # hex of the v4 address, which is why the octets are reversed.
        if parts[1] != "00000000":
            continue
        raw = parts[2]
        if len(raw) != 8:
            continue
        try:
            octets = [int(raw[i:i + 2], 16) for i in (6, 4, 2, 0)]
        except ValueError:
            continue
        return ".".join(str(o) for o in octets)
    return ""


def handshake_line(seat: str) -> bytes:
    """What the container sends the instant it connects.

    The seat NAMES ITSELF and the host verifies. That is what turns a wrong
    peer into a detectable mismatch instead of silent audio delivered to the
    wrong agent — the misroute class that killed v2, which had no way to tell
    which container it had reached.
    """
    return f"{VOICE_MAGIC} {seat}\n".encode()


def parse_handshake(line: bytes) -> str:
    """The seat name from a handshake, or "" if this is not one of ours.

    Deliberately strict and deliberately silent about WHY: this listens on a
    bridge any container can reach, so a malformed greeting is something to
    drop, not something to explain to whoever sent it.
    """
    try:
        text = line.decode("ascii", "strict").strip()
    except UnicodeDecodeError:
        return ""
    magic, _, name = text.partition(" ")
    if magic != VOICE_MAGIC:
        return ""
    name = name.strip()
    # A seat name is an agent identity: lowercase, digits, dash, underscore
    # (the same sanitize rule the rest of the fleet derives). Anything else is
    # not a name we could match against the roster anyway.
    if not name or len(name) > 128:
        return ""
    if not all(c.isalnum() or c in "-_" for c in name):
        return ""
    return name


def authorise(seat: str, known: Callable[[], set[str]]) -> bool:
    """May this seat have the operator's microphone?

    The check is membership of the roster — the same list that already knows
    about BOTH creation paths, which is why an adopted container needs no
    special case. `known` is injected so this is assertable without a machine.

    Fails CLOSED on an empty or unavailable roster: no audio is the correct
    answer when we cannot tell who is asking.
    """
    if not seat:
        return False
    try:
        names = known()
    except Exception:  # noqa: BLE001 — an unreadable roster is not an allow
        return False
    return seat in (names or set())


def send_or_drop(sock: Any, chunk: bytes) -> int:
    """Write what fits, DROP the rest, never block.

    ⚠️ THE DROP IS THE FEATURE. A TCP stream is lossless-with-backpressure and
    realtime audio wants the opposite: when a seat cannot keep up, the correct
    behaviour is to lose audio, not to queue it and deliver it late. Queued
    audio arrives stale and is transcribed as though it were current.

    This is the third time this project has had to re-learn it — VBAN is
    fire-and-forget by design, v1 lost the property by moving to a pipe and had
    to re-add it explicitly, and it would be lost again by "fixing" this to
    retry. **Do not add a retry loop, and do not enlarge the socket buffer:
    neither prevents the stall, both lengthen it.**

    Returns bytes actually written (0 when the peer is not draining).
    """
    try:
        return sock.send(chunk)
    except (BlockingIOError, InterruptedError):
        return 0
    except OSError as exc:
        if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
            return 0
        raise


def connect_argv(gateway: str, port: int = VOICE_PORT) -> tuple[str, int]:
    """Where the container dials. Separated so the caller can be asserted."""
    return (gateway, port)


def open_stream(gateway: str, seat: str, port: int = VOICE_PORT,
                timeout: float = HANDSHAKE_TIMEOUT_SECONDS,
                connector: Callable[..., Any] | None = None) -> Any:
    """Container side: dial the host, name ourselves, return the socket.

    NOTE what is absent: no bind, no listen, no accept. A seat is a client
    only, which is what makes injection between containers impossible rather
    than filtered.
    """
    make = connector or socket.create_connection
    sock = make((gateway, port), timeout)
    sock.sendall(handshake_line(seat))
    return sock
