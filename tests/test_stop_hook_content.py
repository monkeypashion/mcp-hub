"""F3 (j6-turn-only-with-content): a forced turn always carries something new.

A Stop-hook block costs the lane a model turn. Two corrections used to buy
one on EVERY turn boundary for as long as their condition held, so a lane
could be stopped once per turn to be told a sentence it had already read:

  the hold notice   "THIS LANE IS HELD" on every Stop until the hold lapsed.
  the offline nag   "call register()" on every Stop until the binding came back.

Both are DERIVED — re-read from the hold mirror and from list_agents on every
Stop — so delivering one consumes nothing and withholding one loses nothing.
That is the same argument that retired the card owner-notice's block in
2026-09, and it is why neither needs a spool.

r5 closes the two gaps and makes the result COUNTABLE: every drain record
carries a `cause`, written on the no-block branch and the hub-error branch
too. "No forced turn is content-free" is not a claim anybody can check from
a transcript — a drain that surfaces nothing leaves no trace anywhere else —
so the record is the only place the bar can be read.

Term 3 is pinned by the r5 falsifiers' golden strings: nothing here asserts
rendered text, because `cause` lives in the record and nowhere else.
"""
import argparse
import contextlib
import io
import json
import time

import pytest

from mcp_hub import cli, hold

UNTIL_A = 4102444800.0          # 2100-01-01T00:00Z — far past any test clock
UNTIL_B = UNTIL_A + 3600
DM = "[10:00:00] **bob** ·verified ⟨hub.msg/1?id=7⟩: look at F3"
LOW = "[10:00:00] **mon** ·verified ⟨hub.msg/1?id=9⟩ [low]: whenever you like"


class Lane:
    """One lane's Stop hook, driven in-process with the hub round-trip stubbed."""

    def __init__(self, tmp, monkeypatch, name="lane-1"):
        self.name = name
        self.mp = monkeypatch
        self.held = tmp / "held-lanes.json"
        monkeypatch.setenv("MCP_HUB_HELD_FILE", str(self.held))
        monkeypatch.setenv("MCP_HUB_HOLD_BOUNDARY_DIR", str(tmp / "boundary"))
        monkeypatch.delenv("MCP_HUB_DEFER_LOW", raising=False)
        self.fail_hub = False

    def hold(self, until=UNTIL_A, owner="", held_ago=30.0):
        entry = {"until": until, "held_at": time.time() - held_ago,
                 "reason": "over fair share",
                 "release_condition": "the window resets"}
        if owner:
            entry["owner"] = owner
            entry["kind"] = "brake"
        self.held.write_text(json.dumps({"generated": time.time(),
                                         "held": {self.name: entry}}))

    def release(self):
        self.held.write_text(json.dumps({"generated": time.time(), "held": {}}))

    def stop(self, *, active=False, session="s1", dms="", online=True):
        self.mp.setattr(cli, "_read_hook_stdin",
                        lambda: {"session_id": session,
                                 "stop_hook_active": active})

        async def _q(*_a, **_k):
            if self.fail_hub:
                raise OSError("the hub is unreachable in this test")
            return (dms, "", online, "")

        self.mp.setattr(cli, "_query_hub", _q)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.stop_hook_command(argparse.Namespace(
                name=self.name, project="org/repo",
                hub_url="http://hub.invalid/mcp"))
        assert rc == 0, "the stop hook must always fail open"
        out = buf.getvalue().strip()
        text = None
        if out:
            doc = json.loads(out)
            text = ((doc.get("hookSpecificOutput") or {}).get("additionalContext")
                    or doc.get("reason"))
        return text, self.drains()[-1]

    def drains(self):
        path = cli._state_dir() / "activity-log.jsonl"
        rows = [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]
        return [r for r in rows if r.get("kind") == "drain"]


@pytest.fixture
def lane(tmp_path, monkeypatch):
    return Lane(tmp_path, monkeypatch)


# --- the hold notice: once per hold, keyed to its `until` -------------------

def test_the_hold_notice_buys_one_block_and_then_rides_along(lane):
    first, rec = lane.stop()
    assert first is None and rec["cause"] == "none", "the control is not quiet"
    lane.hold()
    told, rec = lane.stop()
    assert told is not None and "HELD" in told
    assert rec["cause"] == "held_first", rec
    again, rec = lane.stop()
    assert again is None, f"the same hold's notice blocked twice: {again!r}"
    assert rec["cause"] == "none", rec
    # ...but it is not SUPPRESSED, only unpaid-for: a block with real content
    # still carries it, and still carries it first. Being stopped is the one
    # thing an agent must not learn by having its pane disappear.
    riding, rec = lane.stop(dms=DM)
    assert riding.startswith("⏸️"), f"the delivered notice stopped riding along: {riding!r}"
    assert DM in riding
    assert rec["cause"] == "content", rec


def test_a_new_hold_is_announced_because_the_marker_is_keyed_to_until(lane):
    """A bare 'already told them' flag would silence every LATER hold on this
    lane too — the silencer-with-no-expiry shape. The marker carries the
    hold's `until`, so a different hold does not match it."""
    lane.hold(until=UNTIL_A)
    assert lane.stop()[0] is not None
    assert lane.stop()[0] is None
    lane.hold(until=UNTIL_B)
    told, rec = lane.stop()
    assert told is not None, "a NEW hold's notice was swallowed by the old one's marker"
    assert rec["cause"] == "held_first", rec
    assert lane.stop()[0] is None, "the new hold's notice blocked twice"


def test_releasing_the_hold_drops_the_marker(lane):
    lane.hold()
    lane.stop()
    marker = hold.notice_marker_path(lane.name)
    assert marker.exists()
    lane.release()
    lane.stop()
    assert not marker.exists(), "a marker outlived the hold it was written for"


def test_a_deferred_hold_is_not_announced_and_leaves_no_stamp(lane):
    """F6's other half, read from the hook's side: squad has deferred this
    hold because the lane is in conversation with Tim, so the lane is NOT
    being stopped at this boundary and must not be told it is."""
    lane.hold(owner="brake")
    hold.record_deferral(lane.name, until=UNTIL_A, owner="brake")
    text, rec = lane.stop()
    assert text is None, f"a deferred lane was told it is being stopped: {text!r}"
    assert rec["cause"] == "none", rec
    assert hold.boundary_reached_at(lane.name) is None, (
        "the hook stamped a boundary while the hold was deferred — the next "
        "pass would read it and stop the lane mid-sentence")
    # The deferral EXPIRES. A squad that stops running must not silence the
    # notice forever, which is why the record carries a deadline and not a flag.
    hold.record_deferral(lane.name, until=UNTIL_A, owner="brake",
                         now=time.time() - hold.DEFERRAL_TTL_SECONDS - 1)
    text, rec = lane.stop()
    assert text is not None and "HELD" in text, "a lapsed deferral stayed silent"


# --- the offline nag: once per offline episode, per session -----------------

def test_the_offline_nag_fires_once_per_episode(lane):
    first, rec = lane.stop(online=False)
    assert first is not None and "register(" in first
    assert rec["cause"] == "offline_first", rec
    again, rec = lane.stop(online=False)
    assert again is None, f"the offline nag blocked twice in one episode: {again!r}"
    assert rec["cause"] == "none", rec


def test_reading_online_ends_the_episode_so_a_new_drop_nags_again(lane):
    assert lane.stop(online=False)[0] is not None
    assert lane.stop(online=False)[0] is None
    assert lane.stop(online=True)[0] is None          # the episode ends here
    back, rec = lane.stop(online=False)
    assert back is not None and "register(" in back, (
        "a lane that recovered and dropped again was not told — a NEW episode "
        "is exactly the case the nag exists for")
    assert rec["cause"] == "offline_first", rec


def test_a_new_session_has_a_context_that_has_never_read_the_line(lane):
    assert lane.stop(online=False, session="s1")[0] is not None
    assert lane.stop(online=False, session="s1")[0] is None
    fresh, _ = lane.stop(online=False, session="s2")
    assert fresh is not None, "a new session inherited the old one's marker"


def test_offline_with_a_queued_dm_always_blocks(lane):
    """The warning is not what buys the block here — the DM is. Nothing about
    the nag's rate limit may touch a drain that has real traffic in it."""
    first, rec = lane.stop(online=False, dms=DM)
    assert first is not None and DM in first and "register(" in first
    assert rec["cause"] == "mixed", f"a DM plus a first-of-episode nag is both: {rec}"
    for _ in range(3):
        text, rec = lane.stop(online=False, dms=DM)
        assert text is not None and DM in text and "register(" in text, text
        # The nag has had its one block; it still RENDERS on every offline
        # drain, it just no longer buys one. The cause names what paid.
        assert rec["cause"] == "content", rec


# --- the cause, on every branch -------------------------------------------

def test_every_drain_records_a_cause_including_the_hub_error_branch(lane):
    lane.fail_hub = True
    text, rec = lane.stop()
    assert text is None
    assert rec.get("error") is True and rec["cause"] == "none", rec


def test_a_released_low_spool_is_its_own_cause(lane, monkeypatch):
    monkeypatch.setenv("MCP_HUB_DEFER_LOW", "1")
    spooled, rec = lane.stop(dms=LOW)
    assert spooled is None and rec["defer_effect"] == "spooled", rec
    assert rec["cause"] == "none", rec
    released, rec = lane.stop(dms=DM)
    assert released is not None and "id=9" in released, (
        "the spool was not carried into the block that released it")
    assert rec["cause"] == "mixed", f"content plus a released spool is both: {rec}"


def test_a_surfaced_drain_never_records_cause_none(lane):
    """The bar, stated as the record has to be able to falsify it: surfaced
    implies a cause, and `none` implies nothing went out."""
    seen = set()
    for kw, holding in (({}, None), ({"dms": DM}, None), ({}, UNTIL_A),
                        ({}, UNTIL_A), ({"online": False}, None),
                        ({"online": False}, None), ({"dms": DM}, UNTIL_B)):
        if holding:
            lane.hold(until=holding)
        else:
            lane.release()
        text, rec = lane.stop(**kw)
        assert rec["cause"] in {"none", "content", "spool", "held_first",
                                "offline_first", "mixed"}, rec
        assert rec["surfaced"] is (text is not None)
        if text is not None:
            assert rec["cause"] != "none", rec
        else:
            assert rec["cause"] == "none", rec
        seen.add(rec["cause"])
    assert {"none", "content", "held_first", "offline_first"} <= seen, seen
