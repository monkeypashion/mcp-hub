"""The voice client must not report success it has not observed.

🔴 THE INCIDENT (2026-08-11, dev-vm-1). Every containerised seat had been
refused audio for days while its own log read `voice: streaming from
172.17.0.1:6981 as <seat>`. The line printed on CONNECT. The host's identity
gate refuses AFTER the accept, by closing the connection — so the client's loop
exited on the first empty `recv` and said nothing further.

Both live seats recorded RMS exactly 0 (a quiet room still has a noise floor
~1700, so zero is "no stream", not "silence"), and only the HOST journal
disagreed with the container's own evidence.

⭐ The family: a sender reporting its own send is not a receipt. Same shape as
`project_message_loss_mark_read_on_push` and the broadcast receipt fix.
"""

from __future__ import annotations

import argparse

import pytest

from mcp_hub import cli


class FakeSock:
    """Yields the scripted chunks, then EOF — a refusal is simply no chunks."""

    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.closed = False

    def recv(self, _n):
        return self._chunks.pop(0) if self._chunks else b""

    def close(self):
        self.closed = True


class FakePacat:
    def __init__(self, *_a, **_k):
        class _In:
            def write(self, _b):
                return None
        self.stdin = _In()


@pytest.fixture()
def rig(monkeypatch, tmp_path):
    """Drive voice_client_command with no network, no /proc, no pacat."""
    monkeypatch.setenv("SEAT_CONTAINER", "seat-x")
    monkeypatch.setattr(cli.pathlib.Path, "read_text",
                        lambda *_a, **_k: "Iface\tDestination\tGateway\n"
                                          "eth0\t00000000\t0100110A\n")
    monkeypatch.setattr("mcp_hub.voice.default_gateway", lambda _t: "10.17.0.1")
    monkeypatch.setattr("subprocess.Popen", FakePacat)

    def _run(chunks):
        monkeypatch.setattr("mcp_hub.voice.open_stream",
                            lambda *_a, **_k: FakeSock(chunks))
        return cli.voice_client_command(
            argparse.Namespace(seat="", port=6981))

    return _run


def test_a_refused_connection_reports_NO_AUDIO(rig, capsys):
    """Mutation: print "streaming" on connect again → this fails.

    The host accepts, refuses at the gate, closes. Zero frames.
    """
    rig([])
    err = capsys.readouterr().err
    assert "NO AUDIO" in err
    assert "streaming from" not in err


def test_the_refusal_names_the_likely_cause_and_where_to_confirm(rig, capsys):
    """A diagnostic that says only 'failed' sends the reader nowhere. It must
    name the roster (the measured cause) AND the host-side command that
    settles it — while saying LIKELY, because a host restart mid-handshake is
    indistinguishable from inside."""
    rig([])
    err = capsys.readouterr().err
    assert "squad roster" in err
    assert "journalctl" in err
    assert "likely" in err.lower()


def test_streaming_is_claimed_ONLY_once_a_frame_arrives(rig, capsys):
    """Positive control — the fix must not make a WORKING client silent, or
    the operator loses the signal that voice is up."""
    rig([b"\x01\x02" * 64])
    err = capsys.readouterr().err
    assert "streaming from" in err
    assert "NO AUDIO" not in err


def test_a_stream_that_dies_mid_session_says_so(rig, capsys):
    """Audio that stops after working is a third state, distinct from both
    'never started' and 'fine' — and it was previously silent too."""
    rig([b"\x00" * 32])
    err = capsys.readouterr().err
    assert "stream ended after" in err
    assert "NO AUDIO" not in err


def test_the_socket_is_closed_on_every_path(rig):
    """The finally block does the reporting now; it must still do the closing."""
    sock = FakeSock([])
    import mcp_hub.voice as voice
    orig = voice.open_stream
    try:
        voice.open_stream = lambda *_a, **_k: sock
        cli.voice_client_command(argparse.Namespace(seat="", port=6981))
    finally:
        voice.open_stream = orig
    assert sock.closed is True
