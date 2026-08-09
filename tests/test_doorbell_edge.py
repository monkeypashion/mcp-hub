"""The doorbell, machine side — the decisions, without a socket.

The loop's I/O is thin on purpose; what can actually be wrong is the
DECISIONS, so they live in pure pieces that a test can drive directly:
backoff, what counts as a bell, and the coalescing that stops a burst of
writes from starting overlapping reconciles.

Every rule here has a failure it prevents, named in its test. The one that
governs all of them: **a lost bell must cost latency and never work**, because
the 30s timer is still underneath and the next pass is a full resync anyway.
"""
from __future__ import annotations

import pytest

from mcp_hub.edge import (
    WATCH_BACKOFF_MAX_S,
    WATCH_BACKOFF_MIN_S,
    Coalescer,
    is_wake,
    next_backoff,
    watch_once,
)

# ------------------------------------------------------------------ backoff


def test_backoff_doubles_from_the_floor_and_stops_at_the_ceiling():
    seen, b = [], WATCH_BACKOFF_MIN_S
    for _ in range(8):
        seen.append(b)
        b = next_backoff(b)
    assert seen[0] == WATCH_BACKOFF_MIN_S
    assert seen == sorted(seen), "backoff must never go backwards"
    assert max(seen) <= WATCH_BACKOFF_MAX_S
    assert seen[-1] == WATCH_BACKOFF_MAX_S, "should reach the ceiling"


def test_backoff_from_zero_still_starts_at_the_floor():
    """A reset-to-zero must not produce an infinitely fast reconnect loop
    hammering a hub that is already struggling."""
    assert next_backoff(0.0) == WATCH_BACKOFF_MIN_S


# --------------------------------------------------------- what is a bell


@pytest.mark.parametrize("line,expected", [
    ("event: wake", True),
    ("event: wake\r", True),          # CR survives some proxies
    (": heartbeat", False),           # 🔴 proof of life, NOT a bell
    (": connected", False),
    ("", False),
    ('data: {"machine": "box-1"}', False),
    ("event: something-else", False),
])
def test_only_a_wake_event_counts_as_a_doorbell(line, expected):
    """The heartbeat is a COMMENT line precisely so it can never be mistaken
    for a bell. If it were, every quiet stream would reconcile on a timer of
    its own and the floor would be meaningless."""
    assert is_wake(line.rstrip("\r")) is expected


# ------------------------------------------------------------- coalescing


def test_a_lone_bell_runs_one_pass():
    c = Coalescer()
    assert c.request() is True
    assert c.finished() is False


def test_a_bell_DURING_a_pass_does_not_start_a_second_one():
    """🔴 Overlapping passes enumerate and act on the same substrate at once —
    two `squad start` for one agent, and a report built from a half-finished
    pass."""
    c = Coalescer()
    assert c.request() is True      # pass 1 begins
    assert c.request() is False     # bell mid-pass: queued, NOT started
    assert c.request() is False     # ...and a burst collapses to one


def test_a_bell_during_a_pass_is_NOT_SWALLOWED():
    """🔴 The other half, and the subtler one. The running pass already read
    its placements before that write existed, so without a trailing re-run the
    change waits for the timer and the doorbell silently did nothing."""
    c = Coalescer()
    c.request()
    c.request()                     # rang mid-pass
    assert c.finished() is True     # ...so one more pass must run
    assert c.finished() is False    # and exactly one


def test_the_coalescer_is_reusable_after_it_settles():
    c = Coalescer()
    c.request()
    assert c.finished() is False
    assert c.request() is True, "a settled coalescer must accept the next bell"


# ------------------------------------------------- one connection's worth


def _runner():
    calls = []
    return calls, lambda reason: calls.append(reason)


def test_each_bell_runs_a_pass_and_heartbeats_run_none():
    calls, run = _runner()
    watch_once([": connected", ": heartbeat", "event: wake", "",
                ": heartbeat", "event: wake"], Coalescer(), run)
    assert calls == ["doorbell", "doorbell"]


def test_a_stream_with_no_bells_runs_nothing():
    """The control: if heartbeats triggered passes, a quiet edge would
    reconcile every 20s forever and the timer's cadence would be a fiction."""
    calls, run = _runner()
    watch_once([": connected"] + [": heartbeat"] * 10, Coalescer(), run)
    assert calls == []


def test_a_FAILING_pass_never_kills_the_stream():
    """🔴 Dropping the connection because one reconcile threw turns a
    transient error into a silently deaf edge — the exact failure the timer
    floor exists to make survivable, so the stream must survive it too."""
    calls = []

    def boom(reason):
        calls.append(reason)
        raise RuntimeError("enumeration failed")

    logged = []
    watch_once(["event: wake", "event: wake"], Coalescer(), boom, logged.append)
    assert calls == ["doorbell", "doorbell"], "stream stopped after a failure"
    assert any("non-fatal" in m for m in logged)


def test_a_bell_arriving_while_a_pass_runs_produces_exactly_one_more():
    """End to end through watch_once: the pass itself rings the bell again,
    which is what a real mid-pass write looks like from in here."""
    c = Coalescer()
    calls = []

    def run(reason):
        calls.append(reason)
        if len(calls) == 1:
            c.request()          # a write landed while pass 1 was running
    watch_once(["event: wake"], c, run)
    assert calls == ["doorbell", "doorbell"], "the trailing pass did not run"


# ------------------------------------------------ the reconnect loop


class _Stream:
    """A fake SSE response: some lines, then the stream ends."""

    def __init__(self, lines, status=200):
        self._lines = lines
        self.status_code = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def iter_lines(self):
        return iter(self._lines)


def _connector(streams):
    seen = []

    def connect(url, headers, timeout):
        seen.append((url, headers, timeout))
        if not streams:
            raise RuntimeError("no more streams")
        return streams.pop(0)

    return seen, connect


def test_every_reconnect_runs_a_FULL_RESYNC_before_reading_events():
    """🔴 The disconnected window. Whatever changed while the stream was down
    produced no event this client will ever see, so the first act of a new
    connection must be a pass — not the first event on it."""
    from mcp_hub.edge import watch_forever

    calls = []
    streams = [_Stream([": connected"]), _Stream([": connected"])]
    _seen, connect = _connector(streams)
    watch_forever("http://h", "tok", "box-1", lambda r: calls.append(r),
                  log=lambda _m: None, sleeper=lambda _s: None,
                  connect=connect, max_connects=2)
    assert calls == ["reconnect-resync", "reconnect-resync"]


def test_the_loop_SURVIVES_a_refused_connection_and_retries():
    """A hub restart (they happen — every deploy) must not end the watch."""
    from mcp_hub.edge import watch_forever

    calls, logged = [], []
    streams = [_Stream([], status=503), _Stream([": connected", "event: wake"])]
    _seen, connect = _connector(streams)
    watch_forever("http://h", "tok", "box-1", lambda r: calls.append(r),
                  log=logged.append, sleeper=lambda _s: None,
                  connect=connect, max_connects=2)
    assert any("503" in m for m in logged)
    assert calls == ["reconnect-resync", "doorbell"], calls


def test_a_healthy_connect_RESETS_the_backoff():
    """Otherwise a long-lived edge that reconnects occasionally creeps to the
    30s ceiling and stays there, quietly slow forever."""
    from mcp_hub.edge import watch_forever

    slept = []
    streams = [_Stream([], status=500), _Stream([], status=500),
               _Stream([": connected"]), _Stream([], status=500)]
    _seen, connect = _connector(streams)
    watch_forever("http://h", "tok", "box-1", lambda _r: None,
                  log=lambda _m: None, sleeper=slept.append,
                  connect=connect, max_connects=4)
    # two failures climb, the healthy third resets, so the fourth wait is the
    # floor again rather than a continued climb.
    assert slept[0] == WATCH_BACKOFF_MIN_S
    assert slept[1] > slept[0]
    assert slept[2] == WATCH_BACKOFF_MIN_S, f"backoff not reset: {slept}"


def test_the_read_timeout_is_the_SILENCE_budget():
    """A dead-but-open socket looks exactly like a quiet one. Without a read
    timeout the edge waits forever, deaf, with nothing to log."""
    from mcp_hub.edge import WATCH_SILENCE_S, watch_forever

    streams = [_Stream([": connected"])]
    seen, connect = _connector(streams)
    watch_forever("http://h", "tok", "box-1", lambda _r: None,
                  log=lambda _m: None, sleeper=lambda _s: None,
                  connect=connect, max_connects=1)
    _url, headers, timeout = seen[0]
    assert timeout == WATCH_SILENCE_S
    assert headers["Authorization"] == "Bearer tok"
    assert headers["Accept"] == "text/event-stream"


def test_it_watches_its_OWN_machine_only():
    from mcp_hub.edge import watch_forever

    seen, connect = _connector([_Stream([": connected"])])
    watch_forever("http://h", "tok", "box-7", lambda _r: None,
                  log=lambda _m: None, sleeper=lambda _s: None,
                  connect=connect, max_connects=1)
    assert seen[0][0] == "/api/v1/machines/box-7/watch"


# ---------------------------------------------------------- the unit file


def _unit(name: str) -> dict[str, dict[str, str]]:
    """Parse a unit file into {section: {key: value}}.

    ⚠️ Parsed, not substring-matched, and that is the whole point. The first
    version of this test asserted `"StartLimitIntervalSec=0" in text` — which
    PASSED while systemd logged `Unknown key name 'StartLimitIntervalSec' in
    section 'Service', ignoring` and the crashloop protection did nothing
    (2026-08-09). A key in the wrong section is present in the file and absent
    from the running system. **Presence is not effect.**
    """
    import configparser
    import pathlib
    # interpolation=None: systemd specifiers like %h are not configparser
    # interpolation, and the default parser raises on them.
    cp = configparser.ConfigParser(
        strict=False, allow_no_value=True, interpolation=None)
    cp.optionxform = str
    cp.read(pathlib.Path(__file__).resolve().parents[1] / "squad" / "systemd" / name)
    return {s: dict(cp[s]) for s in cp.sections()}


def test_the_watch_unit_never_becomes_load_bearing():
    """🔴 The whole safety argument in one file. If this unit ever replaced
    the timer, a quietly-dead doorbell would look exactly like a quiet fleet —
    the failure vps-hetzner hit with egress-sync, where the failure mode was
    indistinguishable from the success mode."""
    watch = _unit("mcp-hub-edge-watch.service")
    timer = _unit("mcp-hub-edge.timer")

    assert "edge watch" in watch["Service"]["ExecStart"]
    assert watch["Service"]["Restart"] == "always"
    # Same credentials as the timer's pass, or the two triggers stop being
    # the same pass.
    assert watch["Service"]["EnvironmentFile"] == "-%h/.mcp-hub/edge-env"
    assert ".venv/bin/mcp-hub" in watch["Service"]["ExecStart"], \
        "bare PATH has no ~/.local/bin"
    # ...and the floor is still a floor.
    assert timer["Timer"]["OnUnitActiveSec"] == "30s"


def test_the_crashloop_guard_is_in_the_SECTION_SYSTEMD_READS():
    """🔴 systemd moved StartLimitIntervalSec to [Unit] in 229 and SILENTLY
    IGNORES it in [Service]. Retrying forever is correct here BECAUSE the timer
    covers correctness — giving up would lose the doorbell with nobody
    noticing, which is the exact failure this unit is shaped to avoid."""
    watch = _unit("mcp-hub-edge-watch.service")
    assert watch["Unit"].get("StartLimitIntervalSec") == "0", \
        "not in [Unit] — systemd ignores it and the guard does nothing"
    assert "StartLimitIntervalSec" not in watch["Service"]


def test_edge_watch_is_reachable_through_the_parser():
    """A verb the entry point cannot dispatch is a verb that does not exist."""
    from mcp_hub import cli
    from mcp_hub.server import _CLI_SUBCOMMANDS

    assert "edge" in _CLI_SUBCOMMANDS
    ns = cli.build_parser().parse_args(["edge", "watch"])
    assert ns.subcommand == "edge" and ns.action == "watch"
