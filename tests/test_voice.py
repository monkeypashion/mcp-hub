"""`/voice` for containerised seats — the rules, asserted without a container.

The properties here are the ones two rejected designs died to establish, so
each test names the death it guards against:

  * no listener in the container (v2 died to a demonstrated injection between
    two seats, past a firewall rule that was present and inert)
  * the host never addresses a container (docker REUSES addresses — one was
    held by two containers minutes apart — so a stored target can misroute the
    operator's voice to the wrong agent with no attacker involved)
  * fail CLOSED
  * drop, never block
"""

from __future__ import annotations

import errno

import pytest

from mcp_hub import voice

# A real /proc/net/route from a container, header included.
ROUTE = (
    "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\n"
    "eth0\t00000000\t010011AC\t0003\t0\t0\t0\t00000000\t0\t0\t0\n"
    "eth0\t000011AC\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0\n"
)


# ---- the container discovers where to dial, and configures nothing ---------

def test_the_gateway_is_read_from_the_kernel():
    """The container's whole configuration. It carries no address, no
    identity and no per-container anything — which is what makes the image
    byte-identical in every configuration."""
    assert voice.default_gateway(ROUTE) == "172.17.0.1"


def test_no_default_route_yields_NOTHING_rather_than_a_guess():
    """A guessed gateway would dial something, and never dialling the wrong
    thing is the entire point of this design."""
    only_local = (
        "Iface\tDestination\tGateway\n"
        "eth0\t000011AC\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0\n"
    )
    assert voice.default_gateway(only_local) == ""
    assert voice.default_gateway("") == ""
    assert voice.default_gateway("Iface\tDestination\tGateway\n") == ""


def test_a_malformed_route_table_does_not_crash_the_seat():
    """/proc parsing that raises takes the whole seat down at entrypoint."""
    assert voice.default_gateway("garbage\nnot\ta\troute\n") == ""
    assert voice.default_gateway("x\t00000000\tZZZZZZZZ\n") == ""
    assert voice.default_gateway("x\t00000000\tABC\n") == ""


# ---- the handshake is what makes a wrong peer DETECTABLE -------------------

def test_the_seat_names_itself_on_connect():
    assert voice.handshake_line("mcp-hub-seat-dev-vm-1") == \
        b"MCPHUBVOICE1 mcp-hub-seat-dev-vm-1\n"


def test_a_handshake_round_trips():
    assert voice.parse_handshake(voice.handshake_line("duo-pod-box")) == "duo-pod-box"


def test_anything_that_is_not_our_greeting_is_dropped():
    """This listens where any container on the bridge can reach it."""
    for junk in (b"", b"\n", b"GET / HTTP/1.1\n", b"MCPHUBVOICE0 x\n",
                 b"hello\n", b"\xff\xfe\n", b"MCPHUBVOICE1\n"):
        assert voice.parse_handshake(junk) == "", junk


def test_a_name_that_could_not_be_a_seat_is_refused():
    """The name is matched against the roster, so anything that could not be
    an agent identity is refused before it gets there — and the refusal keeps
    path-ish and shell-ish characters out of whatever consumes it."""
    for bad in (b"MCPHUBVOICE1 ../../etc\n", b"MCPHUBVOICE1 a b\n",
                b"MCPHUBVOICE1 seat;rm\n", b"MCPHUBVOICE1 " + b"x" * 200 + b"\n"):
        assert voice.parse_handshake(bad) == "", bad


# ---- authorisation fails CLOSED -------------------------------------------

def test_a_roster_member_is_authorised():
    assert voice.authorise("alpha", lambda: {"alpha", "beta"}) is True


def test_a_stranger_is_refused():
    assert voice.authorise("intruder", lambda: {"alpha"}) is False


def test_an_UNREADABLE_roster_refuses_rather_than_allows():
    """The failure direction that matters. 'We could not check' must never
    resolve to 'go ahead' — the operator's microphone is on the other side."""
    def boom():
        raise OSError("roster unreadable")
    assert voice.authorise("alpha", boom) is False


def test_an_EMPTY_roster_authorises_nobody():
    assert voice.authorise("alpha", lambda: set()) is False
    assert voice.authorise("alpha", lambda: None) is False


def test_an_empty_seat_name_is_refused_without_consulting_the_roster():
    """A blank name reaching a membership test is how an empty string ends up
    matching something it shouldn't."""
    def explode():
        raise AssertionError("roster consulted for an empty name")
    assert voice.authorise("", explode) is False


# ---- drop, never block ----------------------------------------------------

class _Sock:
    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.sent = []

    def send(self, chunk):
        result = self.behaviour(chunk)
        if isinstance(result, Exception):
            raise result
        self.sent.append(chunk[:result])
        return result


def test_a_stalled_seat_drops_audio_instead_of_blocking_the_host():
    """⚠️ THE DROP IS THE FEATURE. A stream is lossless-with-backpressure and
    realtime audio wants the opposite: queued audio arrives stale and is
    transcribed as if it were current. One stalled seat must never stall the
    host."""
    for stall in (BlockingIOError(), InterruptedError(),
                  OSError(errno.EAGAIN, "would block"),
                  OSError(errno.EWOULDBLOCK, "would block")):
        assert voice.send_or_drop(_Sock(lambda c, e=stall: e), b"pcm") == 0


def test_a_partial_write_reports_what_actually_went():
    """Not what we hoped went. A caller that assumes the whole chunk landed
    silently corrupts the stream."""
    sock = _Sock(lambda c: 2)
    assert voice.send_or_drop(sock, b"pcmpcm") == 2
    assert sock.sent == [b"pc"]


def test_a_REAL_error_is_raised_rather_than_swallowed_as_a_drop():
    """A closed peer is not congestion. Treating every OSError as a drop turns
    a dead seat into one that looks like it is merely busy, forever."""
    with pytest.raises(OSError):
        voice.send_or_drop(_Sock(lambda c: OSError(errno.EPIPE, "gone")), b"pcm")


# ---- the structural properties, asserted so a refactor cannot lose them ----

def test_the_container_side_NEVER_LISTENS():
    """v2 died here. A listener in the container is a path from any other
    container on the bridge; without one, injection is not blocked — it is
    impossible. The proposed firewall ceiling was demonstrated to be present
    and INERT (br_netfilter absent, so bridged traffic never reaches
    iptables), so 'we filter it' is not an acceptable substitute.
    """
    src = (voice.__file__).replace(".pyc", ".py")
    body = open(src, encoding="utf-8").read()
    for forbidden in (".bind(", ".listen(", ".accept("):
        assert forbidden not in body, \
            f"voice.py calls {forbidden} — the container must be a CLIENT only"


def test_every_seat_uses_the_SAME_port():
    """No allocator, no per-container config. Safe only because each container
    has its own network namespace — and it is what makes the image identical
    in every configuration."""
    assert voice.connect_argv("172.17.0.1") == ("172.17.0.1", 6981)
    assert voice.connect_argv("10.0.0.1") == ("10.0.0.1", 6981)


def test_opening_a_stream_dials_the_gateway_and_names_the_seat(monkeypatch):
    """The container's entire startup, in one assertion: dial the address the
    kernel gave us, say who we are, and never bind anything."""
    calls = {}

    class _Conn:
        def sendall(self, data):
            calls["sent"] = data

    def fake_connect(addr, timeout):
        calls["addr"] = addr
        calls["timeout"] = timeout
        return _Conn()

    voice.open_stream("172.17.0.1", "seat-box", connector=fake_connect)
    assert calls["addr"] == ("172.17.0.1", 6981)
    assert calls["sent"] == b"MCPHUBVOICE1 seat-box\n"
    assert calls["timeout"] == voice.HANDSHAKE_TIMEOUT_SECONDS


# ---- the wiring: automatic in EVERY configuration --------------------------

def test_the_container_name_is_injected_for_BOTH_shapes():
    """/voice is per CONTAINER, and a POD has no SEAT_IDENTITY — so the name
    the client presents must exist in both shapes. Derived at create time like
    SEAT_IDENTITY, never declared in a spec, because a spec field is something
    a seat can be created without (see spec.memory_volume, which was declared
    on every seat and mounted nowhere)."""
    from mcp_hub.edge import DockerExecutor

    solo = DockerExecutor.create_argv("solo-box", {"image": "img"})
    assert "SEAT_CONTAINER=solo-box" in solo

    pod = DockerExecutor.create_argv(
        "duo-pod-box", {"image": "img", "squad": "duo",
                        "agents": [{"identity": "a-box"}, {"identity": "b-box"}]})
    assert "SEAT_CONTAINER=duo-pod-box" in pod
    # A pod deliberately carries no SEAT_IDENTITY — that is the contract the
    # entrypoint refuses on — so SEAT_CONTAINER is the ONLY name it has.
    assert not any(e.startswith("SEAT_IDENTITY=") for e in pod)


def test_seat_entry_starts_voice_WITHOUT_letting_it_fail_the_seat():
    """Audio is a convenience. A seat with no microphone is a working seat; a
    seat that will not start because the audio host is down is not."""
    import mcp_hub.cli as cli

    boom = []

    def explode(*a, **k):
        boom.append(a)
        raise OSError("no such binary")

    orig = cli.subprocess.Popen
    cli.subprocess.Popen = explode
    try:
        cli._seat_voice()          # must not raise
    finally:
        cli.subprocess.Popen = orig
    assert boom, "the fixture never reached Popen — this test proves nothing"


def test_the_voice_client_never_fails_when_there_is_no_identity_or_route(tmp_path):
    """Every 'no audio' path returns 0. A non-zero here would propagate into
    the seat's own exit status."""
    import argparse

    import mcp_hub.cli as cli

    args = argparse.Namespace(seat="", port=6981)
    rc = cli.voice_client_command(args)
    assert rc == 0
