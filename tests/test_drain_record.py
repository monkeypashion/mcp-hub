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
