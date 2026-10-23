"""F4 (j6-honest-counts): the fleet leaves its own events out of its own counts.

`typed_turns` has always been an UPPER BOUND, and its own source string said
so: a `squad send-keys` injection lands in a lane's transcript in exactly the
shape of a line Tim typed — same `type: "user"`, same `origin.kind: "human"`.
So every count of "what Tim typed" silently included the fleet's own nudges
and asks.

That is not a known margin. It is wrong in the one direction nobody checks,
because the fleet's own traffic rises exactly when the fleet is busiest — the
instrument reads high precisely when the number matters.

The ledger (`mcp_hub/injections.py`) is the missing fact: one row per
typed-in line, written by the door BEFORE its first keystroke, carrying the
length and sha256 and never the text. The load meter matches rows to turns
and counts them apart.

🔴 The two rules that make it honest rather than merely different:
  no ledger   -> `injected_turns` is null WITH a reason, never 0. Zero is a
                 measurement; an absent instrument is not one.
  a stray row -> counts NOTHING and is FLAGGED. A ledger read against the
                 wrong transcripts would otherwise leave `typed_turns`
                 carrying injections while the reading looked clean.
"""
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest


class _NotBuilt:
    """Collection on r10's base (19 Oct): what this file reads here is the build's to add. Until it
    exists, the name is a stand-in that raises the original error the moment a test USES it (call,
    iteration, truth, maths, comparison, item or key access), so the file collects and each test
    fails at run time instead of the whole file failing to collect. Attribute access gives a
    stand-in for the attribute, so a module-level alias of a missing name collects too. Once the
    build adds it, the real import or read runs and nothing here is used."""

    def __init__(self, name, err):
        object.__setattr__(self, "_name", name)
        object.__setattr__(self, "_err", err)

    def _raise(self, *a, **k):
        err = self._err
        raise type(err)(f"{self._name} is not built yet: {err}")

    def __getattr__(self, attr):
        if attr.startswith("__") and attr.endswith("__"):
            raise AttributeError(attr)
        return _NotBuilt(f"{self._name}.{attr}", self._err)

    def __repr__(self):
        return f"<not built yet: {self._name}>"

    __call__ = __iter__ = __bool__ = __len__ = __getitem__ = __contains__ = _raise
    __add__ = __radd__ = __sub__ = __rsub__ = __mul__ = __rmul__ = \
        __truediv__ = __rtruediv__ = _raise
    __floordiv__ = __rfloordiv__ = __mod__ = __neg__ = __index__ = __int__ = __float__ = _raise
    __lt__ = __le__ = __gt__ = __ge__ = __eq__ = __ne__ = _raise
    __hash__ = object.__hash__


try:
    from mcp_hub import injections
except ImportError as _e:  # not built yet on r10's base
    injections = _NotBuilt('injections', _e)
try:
    from mcp_hub import load_meter
except ImportError as _e:  # not built yet on r10's base
    load_meter = _NotBuilt('load_meter', _e)

ROOT = Path(__file__).resolve().parents[1]


def _iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z")


def _user(epoch, text, kind="human"):
    return json.dumps({"type": "user", "timestamp": _iso(epoch),
                       "message": {"role": "user", "content": text},
                       "origin": {"kind": kind}})


def _assistant(epoch):
    return json.dumps({"type": "assistant", "timestamp": _iso(epoch),
                       "message": {"role": "assistant",
                                   "content": [{"type": "text", "text": "ok"}]}})


# --- the ledger row --------------------------------------------------------

def test_a_row_carries_the_shape_the_meter_matches_on(tmp_path):
    led = tmp_path / "injections.jsonl"
    assert injections.record("lane-a", "nudge", "re-register please",
                             by="squad", path=led)
    row, = injections.read_rows(led)
    assert set(row) == {"ts", "lane", "door", "by", "chars", "sha256"}, row
    assert row["lane"] == "lane-a" and row["door"] == "nudge"
    assert row["by"] == "squad"
    assert row["chars"] == len("re-register please")
    assert row["sha256"] == injections.sha256("re-register please")


def test_the_text_is_never_stored_only_its_length_and_hash(tmp_path):
    """The pane carries secrets — a token pasted by hand, a `cmd` line with a
    credential. The ledger sits in `~/.mcp-hub`, readable by anything holding
    the operator token, so it must be structurally unable to hold one."""
    led = tmp_path / "injections.jsonl"
    secret = "ghp_THIS_MUST_NOT_BE_WRITTEN_DOWN"
    injections.record("lane-a", "cmd", f"export TOKEN={secret}", path=led)
    raw = led.read_text()
    assert secret not in raw, "the ledger stored the typed text"
    assert "export TOKEN" not in raw
    assert injections.sha256(f"export TOKEN={secret}") in raw


@pytest.mark.parametrize("door,expected", [
    ("cmd", "hand"), ("key", "hand"), ("model", "hand"), ("answer", "hand"),
    ("nudge", "squad"), ("compaction_type", "squad"), ("edge_prompt", "edge"),
])
def test_by_is_forced_from_the_door_not_trusted_from_the_caller(door, expected):
    """`by` is what separates Tim pressing a cockpit button from a timer
    reaching for a pane. A caller that passed the wrong one would make the
    distinction unreadable for every later row, so the hand doors decide it."""
    by = "edge" if door == "edge_prompt" else "squad"
    assert injections._by_for(door, by) == expected


def test_rows_older_than_fourteen_days_are_pruned_on_append(tmp_path):
    led = tmp_path / "injections.jsonl"
    now = time.time()
    injections.record("lane-a", "nudge", "ancient", now=now - 15 * 86400, path=led)
    injections.record("lane-a", "nudge", "recent", now=now - 13 * 86400, path=led)
    injections.record("lane-a", "nudge", "now", now=now, path=led)
    kept = [r["sha256"] for r in injections.read_rows(led)]
    assert injections.sha256("ancient") not in kept, "a 15-day row survived"
    assert injections.sha256("recent") in kept, "a 13-day row was pruned"
    assert injections.sha256("now") in kept


def test_an_undatable_row_is_kept_rather_than_quietly_dropped(tmp_path):
    """Dropping what you cannot date is how a ledger loses its oldest
    evidence first — and the oldest evidence is the only kind that can show
    a door has been typing for weeks."""
    led = tmp_path / "injections.jsonl"
    led.write_text(json.dumps({"lane": "lane-a", "door": "nudge",
                               "sha256": "x" * 64, "chars": 1}) + "\n")
    injections.record("lane-a", "nudge", "fresh", path=led)
    assert len(injections.read_rows(led)) == 2


def test_an_unwritable_ledger_never_stops_the_send(tmp_path):
    """The ledger is an instrument, not a gate. A lane left un-nudged because
    its bookkeeping file was read-only would be an outage caused by a
    measurement."""
    blocked = tmp_path / "afile" / "injections.jsonl"
    (tmp_path / "afile").write_text("not a directory")
    assert injections.record("lane-a", "nudge", "text", path=blocked) is False


def test_an_absent_ledger_raises_rather_than_reading_as_empty(tmp_path):
    """`read_rows` must not resolve "there is no ledger" to "no door typed".
    The caller decides, and the load meter's answer is `missing`, never 0."""
    with pytest.raises(OSError):
        injections.read_rows(tmp_path / "nope.jsonl")


def test_the_cli_takes_the_text_on_stdin_and_never_in_argv(tmp_path):
    """argv is readable in `ps` by anything on the box. A door that passed
    the line as an argument would publish, to every process on the machine,
    exactly what the file refuses to store."""
    led = tmp_path / "injections.jsonl"
    secret = "ZZ-ARGV-MUST-NOT-CARRY-THIS"
    p = subprocess.run(
        [sys.executable, "-m", "mcp_hub.injections", "--lane", "lane-a",
         "--door", "nudge", "--path", str(led)],
        input=secret + "\n", capture_output=True, text=True,
        env={"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin"})
    assert p.returncode == 0, p.stderr
    row, = injections.read_rows(led)
    # The heredoc's trailing newline is the shell's, not the lane's: counting
    # it would make every `chars` one too many and every sha unmatchable.
    assert row["chars"] == len(secret)
    assert row["sha256"] == injections.sha256(secret)


def test_the_cli_refuses_a_door_it_does_not_recognise(tmp_path):
    p = subprocess.run(
        [sys.executable, "-m", "mcp_hub.injections", "--lane", "a",
         "--door", "sneaky", "--path", str(tmp_path / "l.jsonl")],
        input="", capture_output=True, text=True,
        env={"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin"})
    assert p.returncode != 0 and "sneaky" in p.stderr


# --- matching rows to turns ------------------------------------------------

@pytest.fixture
def transcripts(tmp_path):
    """One lane, five user turns ten minutes apart, all origin.kind=human."""
    day = datetime(2026, 10, 3, 10, 0, tzinfo=timezone.utc).timestamp()
    lines, stamps = [], []
    for i in range(5):
        t = day + i * 600
        stamps.append(t)
        lines += [_user(t, f"turn {i}"), _assistant(t + 20)]
    root = tmp_path / "tr"
    (root / "lane-a").mkdir(parents=True)
    (root / "lane-a" / "s.jsonl").write_text("\n".join(lines) + "\n")
    return load_meter.Transcripts(root), stamps


def _row(ts, text, lane="lane-a"):
    return {"ts": ts, "lane": lane, "door": "nudge", "by": "squad",
            "chars": len(text), "sha256": injections.sha256(text)}


def _kinds(tr):
    return [e[1] for _lane, events in tr.sessions for e in events]


def test_a_matching_row_moves_its_turn_out_of_typed(transcripts):
    tr, stamps = transcripts
    left = tr.apply_injections([_row(stamps[1] - 2, "turn 1")])
    assert left == [], f"a row that matched was still reported unmatched: {left}"
    assert _kinds(tr).count("injected") == 1
    assert _kinds(tr).count("typed") == 4


def test_one_row_accounts_for_one_turn(tmp_path):
    """Two identical nudges account for two turns, and a single nudge cannot
    discount a lane's whole day. The ledger is consumed as it matches."""
    day = datetime(2026, 10, 3, 10, 0, tzinfo=timezone.utc).timestamp()
    lines = []
    for i in range(3):
        lines += [_user(day + i * 600, "same text"), _assistant(day + i * 600 + 20)]
    root = tmp_path / "same"
    (root / "lane-a").mkdir(parents=True)
    (root / "lane-a" / "s.jsonl").write_text("\n".join(lines) + "\n")
    tr = load_meter.Transcripts(root)
    left = tr.apply_injections([_row(day - 2, "same text"),
                                _row(day + 600 - 2, "same text")])
    assert left == []
    assert _kinds(tr).count("injected") == 2 and _kinds(tr).count("typed") == 1


def test_a_row_only_matches_a_turn_that_follows_it_inside_the_window(transcripts):
    """A row is written BEFORE its keystroke, so a turn that PRECEDES it is a
    different event however well the text agrees — and a row minutes early is
    not the cause of a turn either."""
    tr, stamps = transcripts
    early = _row(stamps[1] - 60, "turn 1")     # right text, too early
    after = _row(stamps[2] + 2, "turn 2")      # right text, but AFTER the turn
    left = tr.apply_injections([early, after])
    assert _kinds(tr).count("injected") == 0
    assert len(left) == 2, f"both strays should be reported: {left}"


def test_a_row_naming_another_lane_matches_nothing(transcripts):
    tr, stamps = transcripts
    left = tr.apply_injections([_row(stamps[1] - 2, "turn 1", lane="lane-b")])
    assert _kinds(tr).count("injected") == 0
    assert len(left) == 1 and left[0]["lane"] == "lane-b"


def test_an_injected_turn_is_counted_apart_and_ends_no_wait(transcripts):
    tr, stamps = transcripts
    tr.apply_injections([_row(stamps[1] - 2, "turn 1")])
    window = (datetime(2026, 10, 3, tzinfo=timezone.utc),
              datetime(2026, 10, 4, tzinfo=timezone.utc))
    read = tr.read(window)
    assert read["typed"] == {"lane-a": 4}
    assert read["injected"] == {"lane-a": 1}
    # Four typed turns, the first of which had no preceding assistant turn,
    # so three waits — and none of them belongs to the squad-typed turn.
    assert len(read["waits"]) == 3, read["waits"]


# --- the live layout: a row's lane is NOT the meter's lane ------------------
# features-json's independent check (T17, hub 35649) read the first cut of
# this feature on the live fleet and found `injected_turns` at 0 with every
# row flagged unmatched. The reason was a naming mismatch, not a counting
# bug: a ledger row carries the name its DOOR knows — squad writes the roster
# agent (`ledger_note "$a" ...`), the edge writes the tmux session squad
# named for that same agent — while the meter keys a lane by its TRANSCRIPT
# FOLDER, which the client derives from the worktree. Live, the two strings
# are never equal, so the first cut matched only in fixtures that used one
# name on both sides.
#
# The squad roster is the join: field 2 of the agent's row is its worktree.

AGENT = "vortex-dev-vm-1"
WORKTREE = "/home/monke/Projects/code/dreamteam-ai-labs/vortex-driver"
FOLDER = "-home-monke-Projects-code-dreamteam-ai-labs-vortex-driver"


def _roster(tmp_path, *rows):
    conf = tmp_path / "squad.conf"
    conf.write_text("# agent_name | worktree | gh_config_dir | claude_args\n\n"
                    + "".join(f"{a}|{w}||--continue\n" for a, w in rows))
    return conf


def test_the_roster_maps_an_agent_name_to_the_folder_the_client_names(tmp_path):
    conf = _roster(tmp_path, (AGENT, WORKTREE))
    assert injections.lane_folders(conf) == {AGENT: FOLDER}


def test_a_worktree_holding_a_dot_maps_to_the_doubled_dash_the_client_writes(tmp_path):
    """`tr '/' '-'` is not the client's rule. Every character that is not
    `[A-Za-z0-9]` becomes a `-`, so a dot shows as a second dash — and a lane
    whose worktree holds one (`~/.mcp-hub/hooks-clone`) would otherwise map
    to a folder that does not exist and read as never injected."""
    conf = _roster(tmp_path, ("mcp-hub-dev-vm-1", "/home/monke/.mcp-hub/hooks-clone"))
    assert injections.lane_folders(conf) == {
        "mcp-hub-dev-vm-1": "-home-monke--mcp-hub-hooks-clone"}


def test_a_tilde_worktree_resolves_against_home_as_squad_resolves_it(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", "/home/monke")
    conf = _roster(tmp_path, (AGENT, "~/Projects/code/dreamteam-ai-labs/vortex-driver"))
    assert injections.lane_folders(conf) == {AGENT: FOLDER}


def test_the_roster_is_read_with_squads_own_rules(tmp_path):
    """Comments and blank lines are not rows (`rows`), and the FIRST row whose
    first field is the name answers for it (`field "$a" 2`). A second rule
    here would make the meter disagree with the script it is reading."""
    conf = tmp_path / "squad.conf"
    conf.write_text(f"# a comment|{WORKTREE}||x\n"
                    "\n"
                    "   \n"
                    f"{AGENT}|{WORKTREE}||--continue\n"
                    f"{AGENT}|/home/monke/elsewhere||--continue\n"
                    "no-pipes-here\n")
    assert injections.lane_folders(conf) == {AGENT: FOLDER}


def test_no_roster_is_an_empty_mapping_not_an_error(tmp_path):
    """A box with no squad still takes a reading: the rows then match only by
    the name they carry, and a refusal would make the meter unobtainable."""
    assert injections.lane_folders(tmp_path / "absent.conf") == {}


def test_the_default_roster_is_squads_own_path(tmp_path, monkeypatch):
    monkeypatch.delenv("SQUAD_CONF", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert injections.roster_path() == tmp_path / ".config" / "squad" / "squad.conf"
    monkeypatch.setenv("SQUAD_CONF", str(tmp_path / "other.conf"))
    assert injections.roster_path() == tmp_path / "other.conf"


@pytest.fixture
def live_lane(tmp_path):
    """One lane under its CLIENT-NAMED folder, three typed turns in it."""
    day = datetime(2026, 10, 3, 10, 0, tzinfo=timezone.utc).timestamp()
    lines, stamps = [], []
    for i in range(3):
        t = day + i * 600
        stamps.append(t)
        lines += [_user(t, f"turn {i}"), _assistant(t + 20)]
    root = tmp_path / "projects"
    (root / FOLDER).mkdir(parents=True)
    (root / FOLDER / "s.jsonl").write_text("\n".join(lines) + "\n")
    return load_meter.Transcripts(root), stamps


def test_a_row_naming_the_agent_matches_the_turn_in_that_agents_folder(live_lane):
    """The live shape: squad's row says `vortex-dev-vm-1`, the turn sits in
    `-home-monke-...-vortex-driver`, and the roster is what joins them."""
    tr, stamps = live_lane
    left = tr.apply_injections([_row(stamps[1] - 2, "turn 1", lane=AGENT)],
                               {AGENT: FOLDER})
    assert left == [], f"a mapped row was still reported unmatched: {left}"
    assert _kinds(tr).count("injected") == 1 and _kinds(tr).count("typed") == 2


def test_without_the_mapping_the_same_row_matches_nothing(live_lane):
    """The bug, kept as a test: with no roster the two names are never equal,
    so `injected_turns` reads 0 and the row reads unmatched."""
    tr, stamps = live_lane
    left = tr.apply_injections([_row(stamps[1] - 2, "turn 1", lane=AGENT)])
    assert _kinds(tr).count("injected") == 0 and len(left) == 1


def test_a_mapping_never_lets_a_row_match_another_lanes_folder(live_lane):
    """The mapping widens WHICH folder a row can reach, never how many. A row
    for one lane must not discount a same-sha turn in another lane's."""
    tr, stamps = live_lane
    left = tr.apply_injections(
        [_row(stamps[1] - 2, "turn 1", lane="features-json-dev-vm-1")],
        {"features-json-dev-vm-1": "-home-monke-Projects-code-features-json",
         AGENT: FOLDER})
    assert _kinds(tr).count("injected") == 0
    assert len(left) == 1 and left[0]["lane"] == "features-json-dev-vm-1"


def test_a_row_still_accounts_for_one_turn_with_two_candidate_names(tmp_path):
    """A mapped row is looked up under both its own name and its folder. Both
    candidates share ONE consumed flag, or a single nudge would discount two
    turns on a box whose transcripts carry both namings."""
    day = datetime(2026, 10, 3, 10, 0, tzinfo=timezone.utc).timestamp()
    root = tmp_path / "projects"
    for folder in (AGENT, FOLDER):
        (root / folder).mkdir(parents=True)
        (root / folder / "s.jsonl").write_text(
            "\n".join([_user(day, "same text"), _assistant(day + 20)]) + "\n")
    tr = load_meter.Transcripts(root)
    left = tr.apply_injections([_row(day - 2, "same text", lane=AGENT)],
                               {AGENT: FOLDER})
    assert left == []
    assert _kinds(tr).count("injected") == 1, (
        "one row discounted a turn under both of its candidate names")
    assert _kinds(tr).count("typed") == 1


def test_the_rows_that_type_no_line_are_their_own_kind_not_unmatched(live_lane):
    """`key`, `answer` and `edge_interrupt` send a keystroke, not a line: a
    key name, a menu digit, an Escape. The client records no user turn for
    them, so flagging them would fill `injection_unmatched` with rows that
    can never match and hide the one case the flag is FOR."""
    tr, stamps = live_lane
    rows = []
    for door, text in (("key", "Escape"), ("answer", "2"),
                       ("edge_interrupt", "Escape")):
        row = _row(stamps[1] - 2, text, lane=AGENT)
        row["door"] = door
        rows.append(row)
    stray = _row(stamps[1] - 2, "never landed", lane=AGENT)
    left = tr.apply_injections(rows + [stray], {AGENT: FOLDER})
    assert left == [stray], f"a textless door was flagged unmatched: {left}"
    assert _kinds(tr).count("injected") == 0
    assert _kinds(tr).count("typed") == 3, "a keystroke row changed a count"


def test_the_reading_names_the_roster_and_the_textless_doors_in_its_source():
    """The source has to account for every row the reading did not count, or
    the doors whose rows are skipped become a number nobody can reconstruct."""
    ledger = {"read": True, "path": "/state/injections.jsonl", "missing": "",
              "roster": "/home/monke/.config/squad/squad.conf",
              "textless": {"key": 2, "edge_interrupt": 1}}
    note = load_meter._textless_note(ledger)
    assert "key x2" in note and "edge_interrupt x1" in note
    assert "answer" in note, "a door with no rows is still named as exempt"
    assert load_meter._textless_note({"textless": {}}).endswith("none.")


# --- the ledger appends ----------------------------------------------------

def test_record_appends_to_the_same_file_rather_than_replacing_it(tmp_path):
    """squad nudging one lane while the edge prompts another writes twice at
    once. A read-rewrite-replace per append loses whichever row loses the
    race — and a ledger that drops rows under load reports the fleet's own
    traffic as Tim's, which is the thing this feature removes."""
    led = tmp_path / "injections.jsonl"
    for i in range(3):
        assert injections.record("lane-a", "nudge", f"row {i}", path=led)
    before, inode = led.read_bytes(), led.stat().st_ino
    assert injections.record("lane-a", "edge_prompt", "one more",
                             by="edge", path=led)
    assert led.stat().st_ino == inode, "record() replaced the ledger file"
    assert led.read_bytes().startswith(before), "earlier rows were rewritten"
    assert len(injections.read_rows(led)) == 4


def test_only_a_prune_rewrites_the_file(tmp_path):
    """The one case that may replace the file, and only when a row is
    genuinely past the retention window."""
    led = tmp_path / "injections.jsonl"
    now = time.time()
    injections.record("lane-a", "nudge", "ancient", now=now - 15 * 86400, path=led)
    inode = led.stat().st_ino
    injections.record("lane-a", "nudge", "now", now=now, path=led)
    assert led.stat().st_ino != inode, "the stale row was never pruned"
    assert [r["sha256"] for r in injections.read_rows(led)] == [
        injections.sha256("now")]


def test_concurrent_appends_both_keep_their_row(tmp_path):
    """Two doors, interleaved: both rows are in the ledger afterwards."""
    led = tmp_path / "injections.jsonl"
    done = [
        subprocess.run(
            [sys.executable, "-m", "mcp_hub.injections", "--lane", lane,
             "--door", "nudge", "--path", str(led)],
            input=f"line for {lane}\n", capture_output=True, text=True,
            env={"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin"})
        for lane in ("lane-a", "lane-b")
    ]
    assert all(p.returncode == 0 for p in done), [p.stderr for p in done]
    assert sorted(r["lane"] for r in injections.read_rows(led)) == ["lane-a", "lane-b"]
