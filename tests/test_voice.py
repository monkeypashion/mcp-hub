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


def test_a_DOTTED_container_name_is_accepted_because_docker_allows_one():
    """The narrow charset refused a correctly-configured container.

    `docker create --name voice.dotcheck.tmp` succeeds — measured on a live
    daemon, not inferred from docker's docs. Refusing the dot here rejected
    such a container at the WIRE, before any authorisation ran, and said
    nothing about why. The charset is not a security boundary; the roster is.
    """
    for good in ("voice.dotcheck.tmp", "my.seat_2", "a.b.c-d_e"):
        assert voice.parse_handshake(voice.handshake_line(good)) == good, good


def test_a_name_may_not_START_with_a_dot_or_dash():
    """Docker's own rule is `[a-zA-Z0-9][a-zA-Z0-9_.-]*`, and mirroring it is
    what keeps `..`, `.hidden` and `-flag`-shaped strings out of a name that
    later reaches a log line and a roster lookup."""
    for bad in (b"MCPHUBVOICE1 .hidden\n", b"MCPHUBVOICE1 ..\n",
                b"MCPHUBVOICE1 -rf\n", b"MCPHUBVOICE1 _leading\n"):
        assert voice.parse_handshake(bad) == "", bad


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


def test_a_dropped_PARTIAL_frame_would_shift_every_sample_after_it():
    """🔴 The bug my other tests could not see (found 2026-08-08 by
    mcp-hub-dev-vm-1-general, measured: 10 of 12 real partial TCP sends
    returned an ODD count).

    `send_or_drop` was correct as specified — write what fits, drop the rest.
    The defect was in the SPECIFICATION: "drop the rest" is unsafe when the
    unit dropped is smaller than the unit the FORMAT is made of. A lone
    orphaned byte at the peer pairs with the first byte of the next chunk and
    every sample afterwards is two halves of different samples — permanent
    noise, not a glitch, because nothing in the stream says it is off by one.

    ⭐ This asserts the STREAM, not the call. A per-call test cannot catch it:
    each individual send is correct, and the damage is only visible in what the
    peer has accumulated one call later.
    """
    received = bytearray()

    class _OddSock:
        """Sends an odd number of bytes, which is the common real case."""

        def send(self, data):
            n = min(len(data), 5)      # odd on purpose
            received.extend(data[:n])
            return n

    sock = _OddSock()
    carry = b""
    # Every frame is (0x01, 0x02) — so at the peer, ALIGNED means every even
    # offset holds 0x01 and every odd offset holds 0x02. That is the property;
    # total length being even is NOT (20 odd sends of 5 bytes sum to 100, which
    # is even while the stream is thoroughly shifted — this test asserted that
    # weaker thing first and passed against the bug).
    for _ in range(20):
        _sent, carry = voice.send_or_drop(sock, b"\x01\x02" * 8, carry)
        assert len(carry) < voice.VOICE_FRAME_BYTES, "the carry grew — that is a queue"

    assert len(received) % voice.VOICE_FRAME_BYTES == 0, "peer holds a part-frame"
    firsts = set(received[0::2])
    seconds = set(received[1::2])
    assert firsts == {0x01} and seconds == {0x02}, (
        f"peer's stream is BYTE-SHIFTED: even offsets={sorted(firsts)}, "
        f"odd offsets={sorted(seconds)} — every sample from the shift onward is "
        "two halves of different samples"
    )


def test_the_carry_is_bounded_and_never_becomes_a_queue():
    """The line between this fix and the retry loop we banned. If the carry can
    grow, we have reintroduced buffering — and buffered realtime audio arrives
    stale and is transcribed as current."""
    class _Trickle:
        def send(self, data):
            return 1               # worst case: one byte at a time

    carry = b""
    for _ in range(50):
        _sent, carry = voice.send_or_drop(_Trickle(), b"\x01\x02" * 64, carry)
        assert len(carry) <= voice.VOICE_FRAME_BYTES - 1


def test_a_whole_frame_write_carries_NOTHING():
    """The ordinary case must not accumulate state."""
    class _Even:
        def send(self, data):
            return 8
    sent, carry = voice.send_or_drop(_Even(), b"\x01\x02" * 8)
    assert (sent, carry) == (8, b"")


def test_a_blocked_write_PRESERVES_the_carry():
    """Losing the carry on a would-block is the same bug by another route: the
    peer keeps its half-sample and the completing byte is gone forever."""
    class _Blocked:
        def send(self, data):
            raise BlockingIOError()
    sent, carry = voice.send_or_drop(_Blocked(), b"\x01\x02", carry=b"\x09")
    assert sent == 0
    assert carry == b"\x09", "the completing byte was dropped — stream stays shifted"


def test_a_stalled_seat_drops_audio_instead_of_blocking_the_host():
    """⚠️ THE DROP IS THE FEATURE. A stream is lossless-with-backpressure and
    realtime audio wants the opposite: queued audio arrives stale and is
    transcribed as if it were current. One stalled seat must never stall the
    host."""
    for stall in (BlockingIOError(), InterruptedError(),
                  OSError(errno.EAGAIN, "would block"),
                  OSError(errno.EWOULDBLOCK, "would block")):
        assert voice.send_or_drop(_Sock(lambda c, e=stall: e), b"pcm") == (0, b"")


def test_a_partial_write_reports_what_actually_went():
    """Not what we hoped went. A caller that assumes the whole chunk landed
    silently corrupts the stream."""
    sock = _Sock(lambda c: 2)
    sent, carry = voice.send_or_drop(sock, b"pcmpcm")
    assert sent == 2
    assert carry == b""          # 2 bytes IS a whole frame — nothing orphaned
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


def test_seat_entry_starts_voice_WITHOUT_letting_it_fail_the_seat(monkeypatch):
    """Audio is a convenience. A seat with no microphone is a working seat; a
    seat that will not start because the audio host is down is not.

    ⚠️ `_seat_pulse` is stubbed rather than left real. Unstubbed, this test
    passes only on a machine that happens to have a working pulse server (this
    dev box does), and silently stops reaching Popen anywhere else — the
    `assert boom` guard would then fail for an environmental reason that has
    nothing to do with what the test is named for.
    """
    import mcp_hub.cli as cli

    boom = []

    def explode(*a, **k):
        boom.append(a)
        raise OSError("no such binary")

    monkeypatch.setattr(cli, "_seat_pulse", lambda: True)
    monkeypatch.setattr(cli.subprocess, "Popen", explode)
    cli._seat_voice()              # must not raise
    assert boom, "the fixture never reached Popen — this test proves nothing"


def test_a_seat_with_no_pulse_server_says_so_and_does_not_spin_a_client(monkeypatch):
    """🔴 The 2026-08-08 defect, as a test. The image installed `pulseaudio`
    and nothing ever ran it, so `voice-client` connected to the host, received
    audio and had nowhere to put it — `pacat` and `arecord` both died with
    "Connection refused". It was fail-open AND silent, so a seat with entirely
    broken audio started, registered and looked healthy.

    Two properties, and the second is the one that cost the time: don't start a
    client that can only fail, and SAY the audio stack is unavailable.
    """
    import mcp_hub.cli as cli

    started = []
    monkeypatch.setattr(cli, "_seat_pulse", lambda: False)
    monkeypatch.setattr(cli.subprocess, "Popen",
                        lambda *a, **k: started.append(a))
    cli._seat_voice()              # must not raise
    assert not started, "spun a voice client with no server for it to play into"


def test_the_pulse_server_start_can_never_fail_the_seat(monkeypatch):
    """Fail-open, but not fail-silent: returns False, does not raise."""
    import mcp_hub.cli as cli

    def explode(*a, **k):
        raise OSError("pulseaudio: not found")

    monkeypatch.setattr(cli.subprocess, "run", explode)
    assert cli._seat_pulse() is False


def test_a_second_seat_entry_does_not_start_a_SECOND_pulse_server(monkeypatch):
    """A recreated container re-runs seat-entry. Starting a second server would
    fail to bind rather than replace the first, so the check comes first."""
    import mcp_hub.cli as cli

    calls = []

    class _Ok:
        returncode = 0
        stderr = b""

    def fake_run(argv, **k):
        calls.append(argv[0])
        return _Ok()

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    assert cli._seat_pulse() is True
    assert calls == ["pactl"], f"started a server despite one answering: {calls}"


def test_the_IMAGE_STARTS_the_pulse_server_it_installs():
    """🔴 THE TEST THAT WOULD HAVE CAUGHT IT. An assertion that the image
    INSTALLS `pulseaudio` passed throughout the outage — the package was there,
    the comment described its purpose, and nothing invoked it. Installing and
    running are different claims and only one of them was true.

    So this asserts the whole chain exists as *behaviour*: a pa script with a
    NULL SINK (a container has no hardware, so without one `pacat` has nowhere
    to play even with a server up), its monitor as the default source (what
    `arecord`, and therefore /voice, reads back), and code that actually
    launches the daemon.
    """
    import pathlib

    import mcp_hub.cli as cli

    root = pathlib.Path(__file__).resolve().parents[1]
    dockerfile = (root / "seat" / "Dockerfile").read_text(encoding="utf-8")
    for needed in ("module-null-sink", "sink_name=claude_mic",
                   "set-default-source claude_mic.monitor",
                   "seat-voice.pa", "XDG_RUNTIME_DIR"):
        assert needed in dockerfile, f"seat image lacks {needed!r}"

    src = pathlib.Path(cli.__file__).read_text(encoding="utf-8")
    assert '"pulseaudio", "--daemonize=yes"' in src, \
        "nothing in the cli STARTS the pulse server the image installs"
    assert "--file=/etc/pulse/seat-voice.pa" in src, \
        "the server must load the null-sink script, not the hardware default"


def test_no_ROOT_OWNED_path_is_written_after_the_image_drops_privilege():
    """🔴 The second defect in the same fix, and it never reached a container.

    The pulse-server steps above were written BELOW `USER seat`, so they ran
    unprivileged and the build died at step 12/16 with "cannot create
    /etc/pulse/seat-voice.pa: Permission denied". A Dockerfile can only be
    proven by building it — but *this particular* class is decidable by reading,
    and there is no docker on the machine the image is authored on, which is
    exactly why it needs to be caught here.

    The property is general to the seat image, not to /voice: after the USER
    switch, nothing may write outside the seat's own home.
    """
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[1]
    lines = (root / "seat" / "Dockerfile").read_text(encoding="utf-8").splitlines()

    # Join continuations so a multi-line RUN is inspected as one command.
    steps, buf, dropped_at = [], "", None
    for i, raw in enumerate(lines):
        ln = raw.split("#")[0].rstrip() if not raw.lstrip().startswith("#") else ""
        if re.match(r"^\s*USER\s+seat\s*$", raw) and dropped_at is None:
            dropped_at = i
        if buf or re.match(r"^\s*RUN\b", ln):
            buf += " " + ln.rstrip("\\")
            if not ln.endswith("\\"):
                steps.append((i, buf))
                buf = ""
    assert dropped_at is not None, "the image no longer drops privilege at all"

    bad = re.compile(r"(>\s*|mkdir\s+-?\w*\s*|chown\s+\S+\s+|chmod\s+\S+\s+|tee\s+)"
                     r"(/etc/|/run/|/usr/|/opt/|/var/)")
    offenders = [(n + 1, s.strip()[:90]) for n, s in steps
                 if n > dropped_at and bad.search(s)]
    assert not offenders, (
        "RUN step(s) after `USER seat` write to a root-owned path and will fail "
        f"the build with Permission denied: {offenders}")


def test_the_voice_client_never_fails_when_there_is_no_identity_or_route(tmp_path):
    """Every 'no audio' path returns 0. A non-zero here would propagate into
    the seat's own exit status."""
    import argparse

    import mcp_hub.cli as cli

    args = argparse.Namespace(seat="", port=6981)
    rc = cli.voice_client_command(args)
    assert rc == 0


# ---- the API the host side actually uses -----------------------------------

def test_the_frame_sender_keeps_alignment_across_odd_writes():
    """Same stream property as the raw primitive, but the caller cannot lose
    the carry — because the caller never holds it. `send_or_drop` returning a
    tuple is correct and easy to misuse: drop the second element and the
    byte-shift bug is back with every test still green."""
    received = bytearray()

    class _OddSock:
        def send(self, data):
            n = min(len(data), 5)
            received.extend(data[:n])
            return n

    sender = voice.FrameSender(_OddSock())
    for _ in range(20):
        sender.send(b"\x01\x02" * 8)
        assert len(sender.carry) < voice.VOICE_FRAME_BYTES

    assert set(received[0::2]) == {0x01} and set(received[1::2]) == {0x02}, \
        "the stream is byte-shifted through the object API"


def test_the_frame_senders_carry_is_bounded():
    """It must never become the buffer we spent this design avoiding."""
    class _Trickle:
        def send(self, data):
            return 1

    sender = voice.FrameSender(_Trickle())
    for _ in range(50):
        sender.send(b"\x01\x02" * 64)
        assert len(sender.carry) <= voice.VOICE_FRAME_BYTES - 1
