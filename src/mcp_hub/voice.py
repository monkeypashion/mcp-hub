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

# Bytes per FRAME — the indivisible unit of the format (s16 = 2 bytes/sample).
# Dropping any smaller unit than this corrupts the stream permanently; see
# send_or_drop. Derived rather than hardcoded so a move to stereo does not
# require anyone to remember this file exists.
VOICE_FRAME_BYTES = VOICE_CHANNELS * 2

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
    # A seat name is an agent identity: letters, digits, dash, underscore —
    # matching the wire contract's [A-Za-z0-9_-]. Anything else is not a name
    # we could match against the roster anyway.
    #
    # ⚠️ `isalnum()` is Unicode-aware ('٣'.isalnum() is True), so this is only
    # ASCII-safe because of the strict ASCII decode two lines up. That is
    # correctness by a neighbouring line rather than by this one — do not
    # "simplify" the decode without replacing this check.
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


def send_or_drop(sock: Any, chunk: bytes, carry: bytes = b"",
                 frame_size: int = VOICE_FRAME_BYTES) -> tuple[int, bytes]:
    """Write what fits, DROP whole FRAMES, never block.

    ⚠️ THE DROP IS THE FEATURE. A TCP stream is lossless-with-backpressure and
    realtime audio wants the opposite: when a seat cannot keep up, the correct
    behaviour is to lose audio, not to queue it and deliver it late. Queued
    audio arrives stale and is transcribed as though it were current. **Do not
    add a retry loop and do not enlarge the socket buffer: neither prevents the
    stall, both lengthen it.**

    🔴 BUT THE DROP MUST OPERATE ON WHOLE FRAMES. `send()` returns however many
    bytes fitted, and that count is usually ODD — measured over real TCP,
    **10 of 12 partial sends returned an odd count** (mcp-hub-dev-vm-1-general,
    2026-08-08). Dropping the remainder then leaves the peer holding half a
    sample, and since TCP is a byte stream it pairs that orphan with the first
    byte of the NEXT chunk. Every sample after that point is built from two
    halves of different samples: not a glitch, **permanent noise**, because
    nothing in the stream ever tells the reader it is off by one.

    It only triggers when the socket buffer fills — i.e. under load — so it is
    intermittent, absent on a quiet box, and presents as a TRANSCRIPTION
    QUALITY problem, which sends the first suspicion to the mic or the model
    rather than the socket.

    ⇒ So: carry the orphaned partial frame and prepend it to the next chunk.
    **The carry is at most one frame minus one byte, and can never grow — it is
    not a retry and not a queue.** Everything beyond it is still dropped, so
    this is still lossy and still non-blocking.

    Returns `(bytes_written, carry)`. The caller MUST thread `carry` into the
    next call; dropping it on the floor reintroduces the bug.
    """
    data = carry + chunk
    try:
        sent = sock.send(data)
    except (BlockingIOError, InterruptedError):
        return 0, carry
    except OSError as exc:
        if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
            return 0, carry
        raise
    # ⚠️ ALIGNMENT IS CUMULATIVE, NOT PER-CALL. `sent % frame_size` is the
    # obvious expression and it is WRONG: this call may send an odd count while
    # the peer's TOTAL is even, i.e. perfectly aligned, and carrying a byte
    # then CREATES the misalignment it was meant to prevent. (Caught by the
    # stream test, which failed against this exact mistake.)
    #
    # The peer's phase before this call is implied by the carry we were holding:
    # a carry of k bytes means it was waiting for k more to finish a frame.
    phase_before = (frame_size - len(carry)) % frame_size
    phase_after = (phase_before + sent) % frame_size
    if not phase_after:
        return sent, b""
    # Exactly the bytes that finish the frame the peer is now half-holding.
    return sent, data[sent:sent + (frame_size - phase_after)]


class FrameSender:
    """The API the host side should USE. Holds the carry so it cannot be lost.

    `send_or_drop` stays public because it is the pure, directly-assertable
    primitive — but it returns a carry the caller MUST thread into the next
    call, and a caller that drops it silently reintroduces the byte-shift bug
    **while every test still passes**. "Explicit" and "easy to misuse" are the
    same property here.

    So: the carry lives in the object, one per connection, and there is no way
    to hold it wrong.

    ⚠️ This is NOT a buffer and NOT a queue. It holds at most `frame_size - 1`
    bytes — the tail of a single frame the peer is mid-way through — and that
    bound is asserted. Everything else is still dropped, so the stream stays
    lossy and non-blocking, which is what realtime audio requires.
    """

    __slots__ = ("_sock", "_carry", "_frame")

    def __init__(self, sock: Any, frame_size: int = VOICE_FRAME_BYTES) -> None:
        self._sock = sock
        self._frame = frame_size
        self._carry = b""

    def send(self, chunk: bytes) -> int:
        """Write what fits, drop whole frames, never block. Returns bytes sent."""
        sent, self._carry = send_or_drop(
            self._sock, chunk, self._carry, self._frame,
        )
        return sent

    @property
    def carry(self) -> bytes:
        """Exposed for assertions only — never for the caller to manage."""
        return self._carry


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
