"""Bar 47's second instrument (g#24): the drain record.

⭐⭐ WHY THIS FILE EXISTS. Bar 47 asks whether stop-hook drains and heartbeats
cost ZERO model turns — "a lane with nothing to do shows zero turns for that
hour". Measured 2026-09-04 across 139 transcripts: **the transcript cannot
answer it.** A drain that surfaces nothing prints nothing and blocks nothing,
so it leaves no transcript entry at all; all 323 zero-turn hours found were
VACUOUS, satisfying the bar's wording while distinguishing nothing, because
"drains are free" and "no drain happened" look identical from there. A schema
check confirmed no hub table records a drain either.

So the record is written by the process that performs the drain, whether or
not anything surfaces. The tests below exist to stop that guarantee rotting:
the one that matters most is the SILENT drain, because that is the case every
other instrument is blind to.

⚠️ SCOPE, STATED SO NOBODY OVERREADS A GREEN FILE: this records the ACT, never
the COST. It proves a drain happened at a time and whether it surfaced
anything. The turn count still comes from the transcript, and bar 47 closes on
the CROSS-REFERENCE of the two — hours with activity here AND zero turns there.
Nothing in this file is evidence that drains are free.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
from unittest.mock import patch

from mcp_hub import cli


def _log_path() -> pathlib.Path:
    return pathlib.Path(os.environ["MCP_HUB_STATE_DIR"]) / "activity-log.jsonl"


def _records() -> list[dict]:
    p = _log_path()
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text().splitlines() if ln.strip()]


def _args(**kw):
    base = dict(name="alice", project="org/repo", hub_url="http://x/mcp")
    base.update(kw)
    return argparse.Namespace(**base)


def _quiet_hub(*_a, **_k):
    async def _q(*_aa, **_kk):
        return ("", "", True, "")  # nothing to surface
    return _q


# --- the case no other instrument can see ----------------------------------

def test_a_drain_that_surfaces_nothing_is_still_recorded(capsys):
    """THE POINT OF THE WHOLE FILE. This drain writes no stdout and no
    transcript entry — before this record existed it was indistinguishable
    from no drain at all, which is exactly why 323 zero-turn hours proved
    nothing."""
    with patch("mcp_hub.cli._query_hub", side_effect=_quiet_hub()):
        rc = cli.stop_hook_command(_args())

    assert rc == 0
    assert capsys.readouterr().out == ""  # invisible to the transcript

    recs = _records()
    assert len(recs) == 1, "a silent drain must still leave a record"
    assert recs[0]["kind"] == "drain"
    assert recs[0]["agent"] == "alice"
    assert recs[0]["surfaced"] is False
    assert recs[0]["ts"] > 0


def test_a_drain_that_surfaces_something_is_recorded_as_surfaced(capsys):
    async def _loud(*_a, **_k):
        return ("[10:00] **bob**: hello", "", True, "")

    with patch("mcp_hub.cli._query_hub", side_effect=_loud):
        rc = cli.stop_hook_command(_args())

    assert rc == 0
    assert capsys.readouterr().out != ""  # this one DID cost a turn
    recs = _records()
    assert len(recs) == 1
    assert recs[0]["surfaced"] is True


def test_a_hub_failure_is_recorded_and_marked_as_one(capsys):
    """A drain that could not reach the hub still happened and still cost no
    turn. It is recorded with `error` so an analysis can exclude it
    deliberately, rather than by never having heard of it."""
    with patch("mcp_hub.cli._query_hub", side_effect=ConnectionError("boom")):
        rc = cli.stop_hook_command(_args())

    assert rc == 0
    recs = _records()
    assert len(recs) == 1
    assert recs[0]["surfaced"] is False
    assert recs[0]["error"] is True


def test_records_are_one_json_object_per_line():
    """The cross-reference is a query over this file; a half-written line
    would poison it silently."""
    for _ in range(3):
        with patch("mcp_hub.cli._query_hub", side_effect=_quiet_hub()):
            cli.stop_hook_command(_args())

    lines = _log_path().read_text().splitlines()
    assert len(lines) == 3
    for ln in lines:
        assert json.loads(ln)["kind"] == "drain"


# --- fail-open: the record must never cost what it is measuring ------------

def test_an_unwritable_record_does_not_break_the_stop(capsys):
    """Fail-open is the contract on this whole path. An instrument that can
    break a turn boundary is worse than no instrument."""
    async def _loud(*_a, **_k):
        return ("[10:00] **bob**: hello", "", True, "")

    with patch("mcp_hub.cli._query_hub", side_effect=_loud), \
         patch("mcp_hub.cli.open", side_effect=OSError("read-only fs"),
               create=True):
        rc = cli.stop_hook_command(_args())

    assert rc == 0
    assert capsys.readouterr().out != ""  # the drain still delivered


# --- the heartbeat half -----------------------------------------------------

def test_beat_is_recorded_once_per_hour_not_once_per_beat():
    """A per-beat line would be 1440/day/lane and need rotation the nag-log
    pattern deliberately does without. The measurement is hourly, so the
    record is a PRESENCE marker for the hour."""
    hour = cli._log_beat_if_new_hour("alice", "")
    assert len(_records()) == 1

    again = cli._log_beat_if_new_hour("alice", hour)
    assert again == hour
    assert len(_records()) == 1, "same hour must not write a second line"

    cli._log_beat_if_new_hour("alice", "1999-01-01T00")
    recs = _records()
    assert len(recs) == 2
    assert recs[-1]["kind"] == "beat"
    assert recs[-1]["hour"] == hour


def test_beats_and_drains_share_one_file_and_are_told_apart_by_kind():
    """The cross-reference asks 'was this lane doing hook work in that hour',
    and both halves answer it. They must be distinguishable, not merged."""
    with patch("mcp_hub.cli._query_hub", side_effect=_quiet_hub()):
        cli.stop_hook_command(_args())
    cli._log_beat_if_new_hour("alice", "")

    kinds = [r["kind"] for r in _records()]
    assert kinds == ["drain", "beat"]


# --- the caveat, pinned so a future reader cannot miss it ------------------

def test_the_record_carries_no_turn_count():
    """⚠️ Deliberate: this file cannot close bar 47 alone. If a `turns` field
    ever appears here it will have been INFERRED, not measured — the hook
    cannot see how many turns the lane spent. Fail loudly if someone adds one.
    """
    with patch("mcp_hub.cli._query_hub", side_effect=_quiet_hub()):
        cli.stop_hook_command(_args())

    rec = _records()[0]
    assert "turns" not in rec
    assert "cost" not in rec


# --- bar 145: the per-call duration field ----------------------------------
#
# ⭐ WHY THESE EXIST. Bar 145 was first drafted as "median and p95 duration per
# call" and was measured UNPERFORMABLE: the corpus carried no per-call duration
# and no start/end pair, and no amount of effort makes a corpus gain a field
# retroactively. The bar was re-drafted into the INSTRUMENT (this field) and the
# ANALYSIS (bar 401, which reads it). These tests guard the instrument, and the
# one that matters most is the SILENT drain — same reason as the top of the
# file: it is the case every other instrument is blind to, so it is the one
# where a missing duration would go unnoticed.

def test_every_drain_record_carries_an_elapsed_ms(capsys):
    """"Per-call duration on EVERY call" — including the silent drain, which
    is the population bar 401 has to read."""
    with patch("mcp_hub.cli._query_hub", side_effect=_quiet_hub()):
        cli.stop_hook_command(_args())

    rec = _records()[0]
    assert "elapsed_ms" in rec, "a drain with no duration is the old corpus"
    assert isinstance(rec["elapsed_ms"], (int, float))
    assert rec["elapsed_ms"] >= 0


def test_a_hub_failure_still_carries_a_duration(capsys):
    """The error branch is a real call that really took time. Dropping the
    field there would bias the population toward the fast path — a failed
    round-trip is usually the SLOW one, so its absence would pull p95 down
    precisely where bar 401 is looking."""
    with patch("mcp_hub.cli._query_hub", side_effect=ConnectionError("boom")):
        cli.stop_hook_command(_args())

    rec = _records()[0]
    assert rec["error"] is True
    assert rec["elapsed_ms"] >= 0


def test_the_duration_is_monotonic_not_wall_clock():
    """⭐⭐ THE GUARANTEE THAT MATTERS. A duration differenced from wall-clock
    readings goes NEGATIVE across an NTP step or a resume, and a negative
    outlier does not announce itself in a median — it silently drags one. This
    pins the base to `time.monotonic`, so the p95 bar 401 reports cannot be a
    clock artifact. Asserted by making wall-clock jump BACKWARDS a full hour
    while monotonic advances normally."""
    with patch("mcp_hub.cli.time.time", side_effect=[1000.0, 1000.0 - 3600.0,
                                                     1000.0 - 3600.0]):
        with patch("mcp_hub.cli.time.monotonic", side_effect=[5.0, 5.25]):
            ms = cli._elapsed_ms(cli.time.monotonic())

    assert ms == 250.0, "duration must come from monotonic, not the wall clock"
    assert ms >= 0


def test_a_beat_carries_the_duration_of_the_call_that_opened_the_hour():
    """The beat line is one per UTC hour; the duration on it is the ONE
    heartbeat call that opened that hour — a sample of size 1, not a summary
    of the hour. Pinned here so the caveat is a test, not a comment."""
    hour = cli._log_beat_if_new_hour("alice", "", 12.5)

    rec = _records()[0]
    assert rec["kind"] == "beat"
    assert rec["hour"] == hour
    assert rec["elapsed_ms"] == 12.5


def test_a_beat_without_a_measured_call_omits_the_field_rather_than_faking_one():
    """ABSENT is not ZERO. A caller that did not time its call must leave the
    field off; writing 0.0 would put a fabricated fast sample into the very
    population bar 401 takes a median of."""
    cli._log_beat_if_new_hour("alice", "", None)

    rec = _records()[0]
    assert rec["kind"] == "beat"
    assert "elapsed_ms" not in rec


def test_the_same_hour_does_not_log_a_second_beat():
    """The once-per-hour rule still holds with the new argument — a duration
    must not become a reason to write more lines."""
    hour = cli._log_beat_if_new_hour("alice", "", 1.0)
    cli._log_beat_if_new_hour("alice", hour, 2.0)

    assert len(_records()) == 1
