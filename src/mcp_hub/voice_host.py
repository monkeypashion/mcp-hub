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

from typing import NamedTuple


class Container(NamedTuple):
    """What docker vouches for about the thing at an address."""

    id: str
    name: str
    image: str


# The roster: this machine's squad file, one row per agent. LOCAL plain text —
# no network, no hub, no daemon.
#
# ⚠️ NOT the hub's seat list: `mcp-hub seats list` is an HTTP call to the
# operator API, so authorising against it would make the operator's microphone
# depend on the hub being reachable and would let a slow hub stall the accept
# loop. The obvious roster is the wrong one.
#
# 🔴 And NOT the container's image, which was my first answer and was wrong
# (mcp-hub-fireblade-wsl traced `squad add-container`: it validates the folder
# and the name and NEVER reads the image, so "adopted ⇒ seat image" is false by
# construction). Every seat alive today happens to run `mcp-hub-seat:*`, which
# made the signal 100% accurate on the current fleet and wrong about the rule —
# **a measurement of the population mistaken for a constraint on it.** A gate
# built on it would have silently refused the first adopted container built
# from anything else.
#
# Membership in this file IS the act of "this is one of ours": the edge shells
# out to `squad add-container`, and adoption IS `squad add-container`, so both
# creation paths land here with no special case. We read the record of the
# decision rather than inferring it from a side effect.
DEFAULT_SQUAD_CONF = "~/.config/squad/squad.conf"

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


def parse_container_map(text: str) -> dict[str, Container]:
    """`docker inspect` output -> {ip: Container}.

    Input lines are `<id>|/<name>|<image>|<ip> <ip> ...` (docker renders names
    with a leading slash). A line we cannot parse is SKIPPED rather than
    guessed at: a half-understood mapping is how a stream reaches the wrong
    container.
    """
    out: dict[str, Container] = {}
    for line in text.splitlines():
        parts = line.split("|")
        if len(parts) < 4:
            continue
        cid = parts[0].strip()
        name = parts[1].strip().lstrip("/")
        image = parts[2].strip()
        if not cid or not name:
            continue
        for ip in parts[3].split():
            ip = ip.strip()
            if ip:
                out[ip] = Container(cid, name, image)
    return out


def parse_squad_roster(text: str) -> set[str]:
    """Squad conf -> the set of container names enrolled on this machine.

    Row format: `name|worktree|gh_config_dir|@docker:<container>[:<session>]|class`
    Field 4 beginning `@docker:` is what makes a row a CONTAINER seat. Docker
    forbids `:` in a name, so splitting on it is unambiguous.

    A POD puts several rows on ONE container name (measured here: two `duo-*`
    rows both naming `duo-pod-dev-vm-1`), which a membership set handles without
    caring — one island per container serving N agents, as designed.
    """
    names: set[str] = set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split("|")
        if len(fields) < 4:
            continue
        spec = fields[3].strip()
        if not spec.startswith("@docker:"):
            continue
        container = spec[len("@docker:"):].split(":")[0].strip()
        if container:
            names.add(container)
    return names


def is_seat(container: Container, roster: set[str]) -> bool:
    """Is this container one of OURS, or merely something running on the box?

    ⭐ The AUTHORISATION half, separate from authentication on purpose
    (mcp-hub-fireblade-wsl's push-back, and they were right). Deriving identity
    from the address proves *which* container is calling; it does not decide
    that the container may listen to the room. Without this, the operator's
    live microphone is available to anything anyone ever `docker run`s on this
    host — a CI container, a scratch image, a database. Nobody starting a
    container thinks they are starting a microphone client, and a capability
    that exists because nothing decided against it is the shape of failure this
    design keeps meeting.
    """
    return bool(container.name) and container.name in roster


def roster_names(cmap: dict[str, Container]) -> set[str]:
    """Every identity a live container could legitimately claim.

    Both the name and the id, because the two creation paths name themselves
    differently: `create_argv` injects `SEAT_CONTAINER` (the name), while an
    ADOPTED container falls back to its hostname, which docker defaults to the
    short container id. Accepting both is what lets adopted seats work with no
    special case — the requirement that killed v1.
    """
    names: set[str] = set()
    for c in cmap.values():
        names.add(c.name)
        if c.id:
            names.add(c.id)
            names.add(c.id[:12])
    return names


def verify_peer(peer_ip: str, claimed: str,
                cmap: dict[str, Container]) -> bool:
    """Does the container at this address actually have the name it claims?

    This is the authentication the handshake alone cannot provide. Unknown
    address -> False, because an address docker cannot account for is not a
    seat we are willing to feed.
    """
    if not peer_ip or not claimed:
        return False
    c = cmap.get(peer_ip)
    if c is None:
        return False
    return claimed in (c.name, c.id, c.id[:12])


def decide(peer_ip: str, claimed: str, cmap: dict[str, Container],
           roster: set[str]) -> tuple[bool, str, str]:
    """May this caller have the microphone, who is it, and why — `(ok, seat, why)`.

    ⭐ **Identity comes from the ADDRESS, not from the claim.** Docker's answer
    for the connecting address is authoritative; the handshake name is a claim
    by the party being checked, and cannot authenticate itself. So the claim is
    advisory here: it is logged, and it is refused only when it CONTRADICTS the
    address by naming a different live container.

    That inversion is what closes the adopted-container hole. Measured
    2026-08-08: a container created with an explicit `--hostname` reports
    `totally-unrelated-name` — neither its docker name nor its id — and
    `gethostname()` is the only identity an adopted container has. Matching the
    claim against a roster would refuse it, silently, forever. Taking identity
    from the address means an adopted container works no matter what it is
    called, which is the universality requirement that killed v1.

    It costs nothing in security, and this is the part worth checking rather
    than believing: the name never carried any. Any live container could always
    have obtained audio by claiming its OWN name correctly, so refusing an
    unrecognised claim from a container docker vouches for excludes nobody. The
    real gate is, and always was, "is the peer a live container".

    `why` is non-empty on refusal AND on an accepted-but-odd claim, because a
    control that fails closed and silently is undiagnosable — which is the
    failure this whole design has met at every layer.
    """
    if not cmap:
        # Docker told us nothing: unreadable roster, which is the fail-closed
        # case rather than a permissive one.
        return False, "", "roster unavailable"
    c = cmap.get(peer_ip)
    if c is None:
        return False, "", f"address {peer_ip} is not a live container"
    if not roster:
        # No enrolled containers, or an unreadable squad conf. Fail closed:
        # "I could not tell who is ours" must never resolve to "go ahead".
        return False, c.name, "squad roster unavailable or empty"
    if not is_seat(c, roster):
        # AUTHORISATION, separate from authentication: we know exactly who this
        # is, and it is not one of ours.
        return False, c.name, (
            f"{c.name!r} is not enrolled in the squad roster — refused"
        )
    name, cid = c.name, c.id
    if claimed and claimed not in (name, cid, cid[:12]):
        if claimed in roster_names(cmap):
            # Naming a DIFFERENT live container is the impersonation attempt
            # the handshake alone could never have caught.
            return False, name, (
                f"claimed {claimed!r} but {peer_ip} is {name!r} — refused"
            )
        # Neither this container nor any other: hostname drift, not an attack.
        return True, name, (
            f"claim {claimed!r} matches no container; identity taken from "
            f"{peer_ip} = {name!r}"
        )
    return True, name, ""


def authorised(peer_ip: str, claimed: str, cmap: dict[str, Container],
               roster: set[str]) -> bool:
    """`decide` reduced to a boolean, for callers that only need the gate."""
    ok, _seat, _why = decide(peer_ip, claimed, cmap, roster)
    return ok
