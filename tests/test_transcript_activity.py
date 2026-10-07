"""Transcript mtime as a heartbeat-like activity signal (Tim, 2026-09-17).

The hub knows "in a turn" and "idle" and nothing else. Claude Code appends to
its transcript on every turn, so the newest mtime across a lane's transcripts
is an activity reading the hub can get for the price of a stat() per file, on
a daemon beat it is already paying for.

Almost every test here is about the SAME hazard, because it is the one that
would make this instrument worse than no instrument: a lane that stops
reporting leaves its last mtime frozen in the DB, and `now - frozen_mtime`
keeps producing a larger, smoother, entirely plausible number forever. Read
naively that is a lane settling into a deep quiet. It is a dead daemon.

CLAUDE.md already states the rule this has to obey, learned on the fleet
snapshot: *"A stale fleet snapshot reads as `not reporting`, never as a quiet
fleet. An instrument that stopped being written must not be read as a
measurement."* So the hub stores the arrival time of every report as its own
column, and the renderer keeps four states apart where three of them would
otherwise collapse into a confident, wrong "quiet".
"""
from __future__ import annotations

import io
import os
import time
from pathlib import Path

import pytest

from mcp_hub.cli import (
    _call_heartbeat,
    _parse_fleet_rows,
    _parse_status_from_agents,
    _transcript_activity,
)
from mcp_hub.server import (
    TRANSCRIPT_STALE_SECONDS,
    _transcript_quiet,
    create_server,
)


@pytest.fixture
def server(tmp_path: Path):
    return create_server(db_path=tmp_path / "test.db")


async def _call_tool(server, name: str, args: dict) -> str:
    result = await server._tool_manager.call_tool(name, args)
    if hasattr(result, "content"):
        for block in result.content:
            if hasattr(block, "text"):
                return block.text
    if isinstance(result, list):
        for block in result:
            if hasattr(block, "text"):
                return block.text
    return str(result)


def _db(server):
    """The test server's connection, via the register tool's closure."""
    from mcp_hub.server import _get_db

    fn = server._tool_manager._tools["register"].fn
    for name, cell in zip(fn.__code__.co_freevars, fn.__closure__):
        if name == "db_path":
            return _get_db(cell.cell_contents)
    raise AssertionError("couldn't locate db_path in register closure")


def _row(server, name: str):
    return _db(server).execute(
        "SELECT transcript_mtime, transcript_count, transcript_reported_at "
        "FROM agents WHERE name = ?",
        (name,),
    ).fetchone()


# ---------------------------------------------------------------------------
# The scan itself
# ---------------------------------------------------------------------------


def _project_dir(home: Path, cwd: str) -> Path:
    from mcp_hub.cli import _claude_project_dirname

    d = home / ".claude" / "projects" / _claude_project_dirname(cwd)
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_the_scan_reports_the_NEWEST_write_and_how_many_it_saw(
    tmp_path, monkeypatch
):
    """The reading is a max, and it arrives with its own sample size. The
    count is not decoration: it is the only thing separating a real
    measurement from a scan that found nothing, and those look identical in
    the timestamp alone."""
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    d = _project_dir(tmp_path, "/w/repo")
    for name, mtime in (("a.jsonl", 1000), ("b.jsonl", 5000), ("c.jsonl", 3000)):
        f = d / name
        f.write_text("{}")
        os.utime(f, (mtime, mtime))

    activity = _transcript_activity("/w/repo")
    assert activity.newest == 5000
    assert activity.count == 3


def test_only_transcripts_count(tmp_path, monkeypatch):
    """The project dir also holds `memory/` and whatever else Claude Code
    keeps there. Stat-ing all of it would let a memory write — something this
    agent does at the END of a turn, and something a SYNC does on a lane that
    is not working at all — masquerade as conversational activity."""
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    d = _project_dir(tmp_path, "/w/repo")
    t = d / "a.jsonl"
    t.write_text("{}")
    os.utime(t, (1000, 1000))
    other = d / "MEMORY.md"
    other.write_text("x")
    os.utime(other, (9999, 9999))

    activity = _transcript_activity("/w/repo")
    assert activity.newest == 1000, "a non-transcript write was read as activity"
    assert activity.count == 1


def test_a_missing_project_dir_is_BLIND_not_quiet(tmp_path, monkeypatch):
    """The discriminator, stated as its own test because everything
    downstream leans on it. A lane whose transcripts cannot be found returns
    the same 0.0 timestamp as a lane that has never written one — and the
    count, not the timestamp, is what says which. Returning 0.0 alone would
    hand every reader a number that formats beautifully as an epoch and means
    nothing at all."""
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    activity = _transcript_activity("/w/never-opened-here")
    assert activity.count == 0
    assert activity.newest == 0.0


def test_an_empty_project_dir_is_also_blind(tmp_path, monkeypatch):
    """The dir existing is not the dir having anything to say — `enabled` and
    `firing` are not `working`."""
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    _project_dir(tmp_path, "/w/repo")
    assert _transcript_activity("/w/repo").count == 0


def test_the_scan_never_opens_a_transcript(tmp_path, monkeypatch):
    """Transcripts run to tens of MB (4.5MB and 1.4MB sat in this repo's own
    dir when this was written, 70 files deep). This rides a 60s beat on every
    lane in the fleet, so it has to stay a stat(). _read_last_assistant_text
    tails rather than reads for the same reason.

    ⚠️ BOTH sinks are patched, and the second one is the point. `io.open IS
    builtins.open` — the same function object — but `pathlib` resolves it as
    an ATTRIBUTE of the `io` module, so patching `builtins.open` alone leaves
    every `Path.read_text()` invisible. The first version of this test did
    exactly that and passed against a scan mutated to read each file whole:
    right event, wrong sink of the same call. Verified on 3.12.3.
    """
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    d = _project_dir(tmp_path, "/w/repo")
    (d / "a.jsonl").write_text("{}")

    opened: list[str] = []
    real_open = io.open

    def spy(path, *a, **k):
        opened.append(str(path))
        return real_open(path, *a, **k)

    monkeypatch.setattr("io.open", spy)
    monkeypatch.setattr("builtins.open", spy)
    _transcript_activity("/w/repo")
    assert not [p for p in opened if p.endswith(".jsonl")], (
        f"the scan opened a transcript: {opened}"
    )


def test_the_open_spy_can_actually_see_a_read(tmp_path, monkeypatch):
    """The instrument's own control, because the test above is only worth its
    assertion if the spy fires at all — and the first version of it did not.
    A counter that cannot register the failure cannot report its absence."""
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    d = _project_dir(tmp_path, "/w/repo")
    (d / "a.jsonl").write_text("{}")

    opened: list[str] = []
    real_open = io.open

    def spy(path, *a, **k):
        opened.append(str(path))
        return real_open(path, *a, **k)

    monkeypatch.setattr("io.open", spy)
    monkeypatch.setattr("builtins.open", spy)
    (d / "a.jsonl").read_text()
    assert [p for p in opened if p.endswith(".jsonl")], (
        "the spy is blind to pathlib reads — this is the sink mismatch the "
        "test above exists to avoid"
    )


# ---------------------------------------------------------------------------
# The renderer — four states, and why none of them may collapse into "quiet"
# ---------------------------------------------------------------------------


def test_never_reported_renders_NOTHING(tmp_path):
    """No daemon, or a daemon older than this feature. The absence of a
    measurement is not a measurement, and a blank says so where a rendered
    "0m" would claim a reading nobody took."""
    now = time.time()
    assert _transcript_quiet(0.0, 0, 0.0, now) == ""


def test_a_stale_report_says_NOT_REPORTING_and_never_a_quiet_time():
    """🔴 The load-bearing test.

    The daemon died an hour ago holding a perfectly good reading. The stored
    mtime is still there, still divisible, and `now - mtime` says 60 minutes
    of silence — which is TRUE about the file and a lie about the lane. This
    is the fleet-snapshot rule applied to a second instrument, and the whole
    reason transcript_reported_at is a column rather than a derived value."""
    now = time.time()
    out = _transcript_quiet(
        mtime=now - 3600,
        count=12,
        reported_at=now - TRANSCRIPT_STALE_SECONDS - 1,
        now=now,
    )
    assert "not reporting" in out
    assert "60m" not in out and "1h" not in out, (
        "a stale instrument rendered a quiet DURATION — the exact reading "
        "that makes a dead daemon look like a resting lane"
    )


def test_a_live_daemon_that_sees_nothing_says_so(tmp_path):
    """Reporting-and-blind is its own state, distinct from both a dead daemon
    and a resting lane. A scan pointed at the wrong directory would sit here
    forever, and if this rendered as quiet it would read as a lane that never
    works — a check written against the wrong sink, reporting clean.

    ⚠️ The mtime here is deliberately NON-ZERO, and that is the whole test.
    The hub does not compute these two numbers, it receives them from a client
    it does not control, so `count == 0` with a live-looking timestamp is a
    shape it has to handle — and it is the only shape that proves the COUNT is
    what decides. The first version passed `(0.0, 0, ...)`, where the separate
    `mtime <= 0` guard returns the same string, so the assertion held with the
    count branch deleted outright."""
    now = time.time()
    out = _transcript_quiet(now - 60, 0, now - 10, now)
    assert "no transcripts" in out, (
        "a zero count was rendered from its timestamp — the count is the "
        "discriminator, and nothing else can tell blind from quiet"
    )
    assert "not reporting" not in out
    assert "1m" not in out


def test_a_fresh_reading_renders_the_time_since_the_last_write():
    now = time.time()
    assert _transcript_quiet(now - 180, 4, now - 5, now) == " ✍ 3m"


def test_a_count_without_a_timestamp_is_not_rendered_as_a_fresh_write():
    """Nonsense in, named nonsense out. Falling through to the duration branch
    would compute now-0 and print the age of the epoch; clamping it to 0 would
    print `0m`, which reads as "wrote just now" — the most wrong answer
    available."""
    now = time.time()
    out = _transcript_quiet(0.0, 3, now - 5, now)
    assert "no transcripts" in out
    assert "0m" not in out


def test_the_stale_boundary_is_three_beats():
    """One missed beat is a blip; three is a lane that stopped talking. Same
    tolerance heartbeat_touch already applies before it stops believing a
    binding, so the two instruments give up on a lane together rather than
    one contradicting the other for two minutes."""
    assert TRANSCRIPT_STALE_SECONDS == 180.0
    now = time.time()
    just_inside = _transcript_quiet(now - 60, 2, now - 179, now)
    assert "not reporting" not in just_inside


# ---------------------------------------------------------------------------
# The hub records it
# ---------------------------------------------------------------------------


async def test_heartbeat_records_the_reading(server):
    await _call_tool(server, "register", {"name": "alice", "project": "p"})
    await _call_tool(
        server, "heartbeat",
        {"agent_name": "alice", "transcript_mtime": 1700.0, "transcript_count": 7},
    )
    row = _row(server, "alice")
    assert row["transcript_mtime"] == 1700.0
    assert row["transcript_count"] == 7
    assert row["transcript_reported_at"] > 0


async def test_the_reading_is_recorded_even_when_the_binding_is_gone(server):
    """🔴 The case this signal exists for.

    wake_architecture §6: a seat with no bound MCP session reads OFFLINE
    FOREVER while working perfectly well. The binding is about the socket,
    the transcript is about the process, and they fail independently —
    recording only on the "refreshed" path would blind the instrument in
    precisely the situation where it is the only thing still telling the
    truth about the lane."""
    await _call_tool(server, "register", {"name": "alice", "project": "p"})
    # Registered through the tool manager, so there is no bound session at
    # all: heartbeat_touch returns "unbound".
    out = await _call_tool(
        server, "heartbeat",
        {"agent_name": "alice", "transcript_mtime": 1700.0, "transcript_count": 7},
    )
    assert "no binding" in out, "expected the unbound path for this test"
    row = _row(server, "alice")
    assert row["transcript_count"] == 7, (
        "the activity reading was dropped on the unbound path — the one path "
        "where it is the only remaining evidence the lane is alive"
    )
    assert row["transcript_reported_at"] > 0


async def test_a_blind_scan_still_stamps_that_it_REPORTED(server):
    """Skipping the write on count 0 would leave reported_at stale, and a
    live-but-blind daemon would then be indistinguishable from no daemon.
    Storing the zero keeps "reporting, and seeing nothing" a state a reader
    can actually name — and a blind daemon is a defect worth seeing, not one
    worth hiding behind a blank."""
    await _call_tool(server, "register", {"name": "alice", "project": "p"})
    await _call_tool(
        server, "heartbeat",
        {"agent_name": "alice", "transcript_mtime": 0.0, "transcript_count": 0},
    )
    row = _row(server, "alice")
    assert row["transcript_reported_at"] > 0
    assert row["transcript_count"] == 0


async def test_a_heartbeat_never_conjures_a_row(server):
    """The UPDATE is a no-op for an unknown name. An INSERT here would let any
    daemon mint an agent the hub never registered."""
    await _call_tool(
        server, "heartbeat",
        {"agent_name": "ghost", "transcript_mtime": 1700.0, "transcript_count": 3},
    )
    assert _row(server, "ghost") is None


async def test_the_reading_is_optional(server):
    """An older daemon calls heartbeat with one argument and must keep
    working — the beat holds the binding alive, and that outranks any
    reading."""
    await _call_tool(server, "register", {"name": "alice", "project": "p"})
    out = await _call_tool(server, "heartbeat", {"agent_name": "alice"})
    assert "hub_boot=" in out


async def test_list_agents_shows_the_reading(server):
    await _call_tool(server, "register", {"name": "alice", "project": "p"})
    await _call_tool(
        server, "heartbeat",
        {
            "agent_name": "alice",
            "transcript_mtime": time.time() - 120,
            "transcript_count": 4,
        },
    )
    out = await _call_tool(server, "list_agents", {})
    assert "✍ 2m" in out


async def test_list_agents_stays_silent_for_a_lane_that_never_reported(server):
    """The control. Without it, every assertion above would pass against a
    renderer that stamped a confident "0m" on every lane in the fleet."""
    await _call_tool(server, "register", {"name": "alice", "project": "p"})
    out = await _call_tool(server, "list_agents", {})
    assert "✍" not in out


async def test_an_old_daemons_beat_is_not_a_blind_reading(server):
    """An older daemon beats with agent_name alone. That is NO reading, and
    it must render as nothing. Before the fix it was stored as count 0 and
    every such lane read "✍ no transcripts", a claim that a daemon looked
    and saw nothing (prod, 2026-10-07 03:06Z, every lane). The control
    above never beats, so it could not see this."""
    await _call_tool(server, "register", {"name": "alice", "project": "p"})
    await _call_tool(server, "heartbeat", {"agent_name": "alice"})
    out = await _call_tool(server, "list_agents", {})
    assert "✍" not in out
    # A real blind scan (the daemon sent count 0) still says so.
    await _call_tool(server, "heartbeat",
                     {"agent_name": "alice", "transcript_mtime": 0.0,
                      "transcript_count": 0})
    assert "✍ no transcripts" in await _call_tool(server, "list_agents", {})


# ---------------------------------------------------------------------------
# The daemon — and the rule that it must keep beating no matter what
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, text: str, is_error: bool = False):
        self.content = [type("B", (), {"text": text})()]
        self.isError = is_error


class _FakeSession:
    """Records every call; fails the extended form if `reject` is set."""

    def __init__(self, reject: bool = False):
        self.reject = reject
        self.calls: list[dict] = []

    async def call_tool(self, name, args):
        self.calls.append(dict(args))
        if self.reject and "transcript_mtime" in args:
            return _FakeResult("unexpected keyword argument", is_error=True)
        return _FakeResult("heartbeat ok [hub_boot=abc]")


async def test_the_daemon_carries_the_reading_on_the_beat_it_already_pays_for(
    tmp_path, monkeypatch
):
    """No new call, no new connection, no new cadence — the argument rides
    the heartbeat that was going to happen anyway. That is the whole latency
    and traffic budget for this feature."""
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    d = _project_dir(tmp_path, os.getcwd())
    f = d / "a.jsonl"
    f.write_text("{}")
    os.utime(f, (4242, 4242))

    session = _FakeSession()
    text, send_transcript = await _call_heartbeat(session, "alice", True)

    assert len(session.calls) == 1, "the reading cost an extra hub call"
    assert session.calls[0]["transcript_mtime"] == 4242
    assert session.calls[0]["transcript_count"] == 1
    assert send_transcript is True
    assert "hub_boot=abc" in text


async def test_a_REJECTING_hub_gets_a_plain_heartbeat_and_the_beat_SURVIVES(
    tmp_path, monkeypatch
):
    """The fallback path, driven by a session that rejects the arguments.

    ⚠️ Read the name literally: a REJECTING hub, which is not the same thing
    as an OLD one. Measured 2026-09-17 against a hub built from the
    pre-change tree, FastMCP ACCEPTED the two unknown arguments, answered
    `heartbeat ok` with isError False, and silently discarded them — so on
    the skew that actually ships this branch never runs, and the lane just
    renders no ✍ marker (see the never-reported test). An earlier version of
    this test claimed to prove "an older hub keeps beating" while stubbing a
    rejection no real hub produced: it was asserting its own fixture.

    What it does prove is the fallback itself, kept as insurance against a
    stricter validator (a different SDK version, or extra-fields-forbidden).
    heartbeat_touch drops a binding after three consecutive misses, so a beat
    that turned into an error would cost the lane its wake target in about
    three minutes — trading the thing that matters for a nice-to-have."""
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    session = _FakeSession(reject=True)

    text, send_transcript = await _call_heartbeat(session, "alice", True)

    assert send_transcript is False, "the daemon did not latch off"
    assert "transcript_mtime" not in session.calls[-1], (
        "the fallback still carried the argument the hub had just rejected"
    )
    assert "hub_boot=abc" in text, (
        "the heartbeat did not survive the rejection — the binding this beat "
        "exists to hold alive would be dropped three beats from here"
    )


async def test_an_unknown_argument_does_not_break_the_beat_in_EITHER_shape():
    """The measured skew, pinned so the claim above has an anchor in the
    suite rather than only in a docstring: a tool that ignores the extra
    arguments still answers normally, and the daemon keeps its latch on."""
    session = _FakeSession()  # accepts everything, like real FastMCP
    text, still_on = await _call_heartbeat(session, "alice", True)
    assert still_on is True
    assert "hub_boot=" in text


async def test_the_latch_stops_the_daemon_retrying_every_beat(
    tmp_path, monkeypatch
):
    """Once off, stays off for this daemon's lifetime. Re-probing would pay
    the rejected call 1440 times a day per lane; a respawn re-enables it,
    which is the right granularity because a respawn is also how new code
    arrives."""
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    session = _FakeSession(reject=True)

    _, flag = await _call_heartbeat(session, "alice", True)
    session.calls.clear()
    _, flag = await _call_heartbeat(session, "alice", flag)

    assert flag is False
    assert len(session.calls) == 1, "the daemon re-probed a hub that said no"
    assert "transcript_mtime" not in session.calls[0]


# ---------------------------------------------------------------------------
# The row is parsed by two readers that are not this one
# ---------------------------------------------------------------------------


def test_the_new_marker_does_not_disturb_the_statusline_parser():
    """_parse_fleet_rows' docstring: "The rendered format is ours (server.py
    list_agents) — if that ever changes, change this with it." This is that
    check, kept as a test rather than a promise."""
    txt = "🟢 **me** ⚡ 💤 🔕 45m ✍ 3m (proj) — next: ship it"
    snap = _parse_status_from_agents(txt, "me")
    assert snap["online"] and snap["wakeable"]
    left = snap["focus_until"] - time.time()
    assert 44 * 60 < left <= 45 * 60, (
        "✍ sitting after 🔕 was absorbed into the focus duration"
    )


def test_the_new_marker_does_not_disturb_the_fleet_board_parser():
    """The project regex is `\\(([^)]*)\\)` against everything after the name,
    so a marker carrying parentheses would be read as the project. ✍ carries
    none — asserted here so it stays that way."""
    txt = "🟢 **me** ⚡ ✍ not reporting (org/repo) — next: ship it"
    rows = _parse_fleet_rows(txt)
    assert len(rows) == 1
    assert rows[0]["project"] == "org/repo"
    assert rows[0]["name"] == "me"
    assert rows[0]["wakeable"] is True


# ---------------------------------------------------------------------------
# One beat, one write — and last_seen still means what it meant
# ---------------------------------------------------------------------------


def _last_seen(server, name: str) -> float:
    return _db(server).execute(
        "SELECT last_seen FROM agents WHERE name = ?", (name,)
    ).fetchone()["last_seen"]


async def test_the_refreshed_path_still_advances_last_seen(server, monkeypatch):
    """last_seen moved INTO the transcript UPDATE so the beat costs one write
    instead of two. It is what list_agents orders by and what the reaper reads,
    so "I merged the writes" has to be shown, not asserted."""
    await _call_tool(server, "register", {"name": "alice", "project": "p"})
    _db(server).execute(
        "UPDATE agents SET last_seen = 1000 WHERE name = 'alice'"
    )
    _db(server).commit()
    monkeypatch.setattr(
        server._hub_registry, "heartbeat_touch", lambda _n: "refreshed"
    )

    await _call_tool(
        server, "heartbeat",
        {"agent_name": "alice", "transcript_mtime": 1700.0, "transcript_count": 2},
    )
    assert _last_seen(server, "alice") > 1000


async def test_an_unbound_beat_does_NOT_advance_last_seen(server):
    """The control on the merge, and the behaviour that was there before it.
    last_seen means "the hub heard from a LIVE BINDING"; the transcript
    columns mean "the daemon reported". Letting the merged UPDATE carry
    last_seen on every outcome would make an unbound lane look freshly seen
    and hold it at the top of list_agents — the binding lie that
    heartbeat_touch's deliverability check exists to stop."""
    await _call_tool(server, "register", {"name": "alice", "project": "p"})
    _db(server).execute(
        "UPDATE agents SET last_seen = 1000 WHERE name = 'alice'"
    )
    _db(server).commit()

    out = await _call_tool(
        server, "heartbeat",
        {"agent_name": "alice", "transcript_mtime": 1700.0, "transcript_count": 2},
    )
    assert "no binding" in out
    assert _last_seen(server, "alice") == 1000, (
        "an unbound beat refreshed last_seen — the merged UPDATE widened what "
        "the heartbeat asserts about the binding"
    )
    # ...while the reading it DID establish still landed.
    assert _row(server, "alice")["transcript_count"] == 2


async def test_a_beat_costs_exactly_one_commit(server, monkeypatch):
    """This is the hottest write path the hub has: every lane, every 60s,
    forever. The loan purge already cost this codebase a `database is locked`
    for adding one unconditional write to a hot path, so the beat carrying a
    second fsync is a regression worth a test rather than a comment."""
    await _call_tool(server, "register", {"name": "alice", "project": "p"})
    monkeypatch.setattr(
        server._hub_registry, "heartbeat_touch", lambda _n: "refreshed"
    )
    # sqlite3.Connection is a C type and refuses attribute patching, so the
    # count is taken on a delegating proxy installed at _get_db — the one
    # place the tool reaches for a connection.
    conn = _db(server)
    commits: list[int] = []

    class _CountingConn:
        def __getattr__(self, name):
            return getattr(conn, name)

        def commit(self):
            commits.append(1)
            return conn.commit()

    monkeypatch.setattr("mcp_hub.server._get_db", lambda *_a, **_k: _CountingConn())

    await _call_tool(
        server, "heartbeat",
        {"agent_name": "alice", "transcript_mtime": 1700.0, "transcript_count": 2},
    )
    assert len(commits) == 1, f"the beat committed {len(commits)} times"


# ---------------------------------------------------------------------------
# The board carries the VERDICT, never the raw number
# ---------------------------------------------------------------------------


def test_the_board_row_carries_the_rendered_reading():
    rows = _parse_fleet_rows("🟢 **me** ⚡ ✍ 12m (org/repo) — next: ship it")
    assert rows[0]["activity"] == "12m"
    assert rows[0]["project"] == "org/repo", "the ✍ marker swallowed the project"


def test_the_board_row_carries_NOT_REPORTING_as_itself():
    """🔴 The reason `activity` is a rendered string and not an mtime.

    Hand a downstream the raw number and the obvious `now - mtime` gives a
    quiet time that grows forever on a lane whose daemon died — plausible,
    smooth and wrong. The verdict travels already made, so the staleness
    judgement stays in the one place holding the arrival time it needs, and
    no board, panel or statusline gets the chance to re-derive it and
    re-acquire the bug. Word-for-word what the focus expiry does, for the same
    reason."""
    rows = _parse_fleet_rows("🟢 **me** ⚡ ✍ not reporting (org/repo) — bio")
    assert rows[0]["activity"] == "not reporting"


def test_a_lane_with_no_reading_carries_an_empty_string():
    """The control: no reading is not a reading. An absent ✍ must not become
    a confident "0m" on the board."""
    rows = _parse_fleet_rows("🟢 **me** ⚡ 💤 (org/repo) — bio")
    assert rows[0]["activity"] == ""


def test_a_bio_mentioning_the_marker_cannot_forge_a_reading():
    """Same head/bio discipline the other markers already keep: the row is
    read before the em-dash so a bio can't write the fleet's instruments."""
    rows = _parse_fleet_rows("🟢 **me** ⚡ (org/repo) — next: ✍ 99m of work")
    assert rows[0]["activity"] == ""
