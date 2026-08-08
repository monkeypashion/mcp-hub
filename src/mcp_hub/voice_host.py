"""Host half of `/voice` for containerised seats (docs/seat-voice.md).

The container dials US and names itself; this side decides whether that caller
may have the operator's microphone, and then streams one way and never reads.
The container half lives in `voice.py` — the split is deliberate, so two agents
can work the two halves without editing one file.

Everything here is pure. The socket loop is `voice_host_command` in `cli.py`,
because the rules are the interesting part and a live listener is an expensive
place to assert them.

WHAT THIS FILE IS DEFENDING, in the order the properties are easiest to lose:

* **Bind the gateway, never `0.0.0.0`.** Measured on dev-vm-1: ufw allows
  *everything* inbound on `tailscale0`. A wildcard bind would therefore serve a
  continuous live feed of the operator's microphone to every tailnet peer. The
  PERMIT rule we add for docker is not what would expose it — the pre-existing
  blanket tailscale allow is. `listen_address()` exists so that choice is
  stated once, in a function with a test, rather than inlined at a `bind()`.

* **The handshake AUTHORISES but does not AUTHENTICATE.** Seat names are public
  and guessable, so a name alone is a claim, not proof. `verify_peer` asks
  docker which container holds the *connecting address right now* and checks it
  matches. IP reuse — observed here, one address held by two containers minutes
  apart — cannot bite: the connection is established, so the address is current
  by definition. This is also why an ADOPTED container needs no special case;
  docker knows it without anything having been injected at create time.

* **Fail CLOSED, everywhere.** An unreadable roster, an unmappable peer, an
  unparsable line: every one of them resolves to *no audio*. The thing on the
  other side is the operator's microphone, so "I could not tell who was asking"
  must never resolve to "go ahead".
"""

from __future__ import annotations

# The bridge gateway is where seats dial: it is the container's default route,
# so every seat discovers it from the kernel with nothing configured. Used as
# the BIND address too — see the module docstring for why that is load-bearing
# rather than tidy.
DEFAULT_GATEWAY = "172.17.0.1"

# A handshake is one short line. Cap the read so a peer that opens a connection
# and streams megabytes without a newline cannot grow our memory — this listens
# where any container can reach it.
HANDSHAKE_MAX_BYTES = 256

# Most seats a single host will serve. Beyond this we stop accepting rather
# than fan the microphone into an unbounded number of sockets.
MAX_STREAMS = 64


def listen_address(gateway: str = DEFAULT_GATEWAY) -> str:
    """The address the host listener binds — NEVER the wildcard.

    Returning the gateway rather than "" or "0.0.0.0" is the whole point of the
    function. ufw here permits everything on `tailscale0`, so a wildcard bind
    publishes the operator's live microphone to the tailnet. A caller that
    wants the wildcard has to type it themselves, in a diff someone reviews.
    """
    gw = (gateway or "").strip()
    return gw or DEFAULT_GATEWAY


def parse_container_map(text: str) -> dict[str, tuple[str, str]]:
    """`docker inspect` output -> {ip: (container_id, container_name)}.

    Input lines are `<id>|/<name>|<ip> <ip> ...` (docker renders names with a
    leading slash). A line we cannot parse is SKIPPED rather than guessed at:
    a half-understood mapping is how a stream reaches the wrong container.
    """
    out: dict[str, tuple[str, str]] = {}
    for line in text.splitlines():
        parts = line.split("|")
        if len(parts) < 3:
            continue
        cid = parts[0].strip()
        name = parts[1].strip().lstrip("/")
        if not cid or not name:
            continue
        for ip in parts[2].split():
            ip = ip.strip()
            if ip:
                out[ip] = (cid, name)
    return out


def roster_names(cmap: dict[str, tuple[str, str]]) -> set[str]:
    """Every identity a live container could legitimately claim.

    Both the name and the id, because the two creation paths name themselves
    differently: `create_argv` injects `SEAT_CONTAINER` (the name), while an
    ADOPTED container falls back to its hostname, which docker defaults to the
    short container id. Accepting both is what lets adopted seats work with no
    special case — the requirement that killed v1.
    """
    names: set[str] = set()
    for cid, name in cmap.values():
        names.add(name)
        if cid:
            names.add(cid)
            names.add(cid[:12])
    return names


def verify_peer(peer_ip: str, claimed: str,
                cmap: dict[str, tuple[str, str]]) -> bool:
    """Does the container at this address actually have the name it claims?

    This is the authentication the handshake alone cannot provide. Unknown
    address -> False, because an address docker cannot account for is not a
    seat we are willing to feed.
    """
    if not peer_ip or not claimed:
        return False
    entry = cmap.get(peer_ip)
    if entry is None:
        return False
    cid, name = entry
    return claimed == name or claimed == cid or claimed == cid[:12]


def authorised(peer_ip: str, claimed: str,
               cmap: dict[str, tuple[str, str]]) -> bool:
    """The whole gate: on the roster AND actually who it says it is.

    Membership alone would let any container claim any seat's name and be fed
    the operator's microphone; peer verification alone would let a container
    that docker knows about but the fleet does not. Both, or no audio.
    """
    if not cmap:
        # Empty map means docker told us nothing — unreadable roster, which is
        # the fail-closed case rather than a permissive one.
        return False
    return claimed in roster_names(cmap) and verify_peer(peer_ip, claimed, cmap)
