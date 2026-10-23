"""F5 (j6-no-pane-injection): nothing automated types into a pane Tim uses.

His complaint is the feature: an automated door typing into a pane he is
working in. A heal nudge appends to whatever is in his input box; a /compact
ask lands in the middle of a sentence he was writing; and the thing that did
it cannot see him, so nothing anywhere looks wrong afterwards.

One door answers "is a person in the way" — `pane_in_use` in squad,
`_seat_client_in_use` in the edge — and it reads an ATTACHED CLIENT WITH
RECENT ACTIVITY straight out of tmux. Attached-but-idle is deliberately not
in use: a cockpit terminal left open for a week would otherwise make a lane
permanently un-nudgeable, which is the silencer-with-no-expiry shape this
codebase has shipped bugs in before.

⚠️ THE HAND VERBS ARE NEVER REFUSED (mcp-hub's Term 1). cmd, key, model and
answer are what his cockpit buttons call, so the pane they would be refused
for is the pane he is pressing the button about. A guard that locks the
operator out of his own seat is not a guard.

A refused door is not a dropped message: the item goes to the lane inbox and
the lane's next Stop blocks with it. The r5 falsifiers drive the real tmux
end to end; this file pins the pieces and the invariants that are easy to
break without a pane.
"""
import json
import re
import time
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
    from mcp_hub import lane_inbox
except ImportError as _e:  # not built yet on r10's base
    lane_inbox = _NotBuilt('lane_inbox', _e)
try:
    from mcp_hub.edge import SEAT_IN_USE_SECONDS
except ImportError as _e:  # not built yet on r10's base
    SEAT_IN_USE_SECONDS = _NotBuilt('SEAT_IN_USE_SECONDS', _e)
try:
    from mcp_hub.edge import _seat_client_in_use
except ImportError as _e:  # not built yet on r10's base
    _seat_client_in_use = _NotBuilt('_seat_client_in_use', _e)
try:
    from mcp_hub.edge import realize_seat_action
except ImportError as _e:  # not built yet on r10's base
    realize_seat_action = _NotBuilt('realize_seat_action', _e)

ROOT = Path(__file__).resolve().parents[1]
SQUAD = ROOT / "squad" / "squad"
SEAT = {"session": "lane-a"}


# --- the lane inbox: refused is not dropped --------------------------------

def test_an_item_survives_until_the_block_that_carried_it_is_out():
    assert lane_inbox.put("lane-a", "nudge", "re-register please")
    items = lane_inbox.peek("lane-a")
    assert len(items) == 1
    path, text = items[0]
    assert "re-register please" in text
    assert lane_inbox.HEADER in text, (
        "the lane must be told this was NOT typed into its pane — otherwise "
        "it reads as a line somebody typed and answers accordingly")
    # Peeking does not consume: a read-then-crash would swallow the one copy.
    assert lane_inbox.peek("lane-a")
    lane_inbox.clear([path])
    assert lane_inbox.peek("lane-a") == []


def test_clearing_names_the_items_that_went_out_and_spares_the_rest():
    """A door can write between the hook's read and its block. Clearing the
    whole directory would lose that item, silently, in the one window where
    losing it is most likely."""
    lane_inbox.put("lane-a", "nudge", "first", now=1.0)
    delivered = [p for p, _t in lane_inbox.peek("lane-a")]
    lane_inbox.put("lane-a", "compaction_type", "arrived mid-drain", now=2.0)
    lane_inbox.clear(delivered)
    left = lane_inbox.peek("lane-a")
    assert len(left) == 1 and "arrived mid-drain" in left[0][1]


def test_items_are_delivered_oldest_first():
    """A directory listing is not a chronology, and two items out of order
    read as the ask answering the nudge."""
    lane_inbox.put("lane-a", "nudge", "earlier", now=1000.0)
    lane_inbox.put("lane-a", "compaction_type", "later", now=2000.0)
    texts = [t for _p, t in lane_inbox.peek("lane-a")]
    assert "earlier" in texts[0] and "later" in texts[1]


def test_one_lanes_inbox_is_not_anothers():
    lane_inbox.put("lane-a", "nudge", "for a")
    assert lane_inbox.peek("lane-b") == []


def test_an_unwritable_inbox_says_so_rather_than_raising(tmp_path, monkeypatch):
    """The caller has still REFUSED to type, which is the half of the rule
    that protects the pane. It reports the failure instead of pretending the
    item landed."""
    blocker = tmp_path / "state"
    blocker.write_text("not a directory")
    monkeypatch.setenv("MCP_HUB_STATE_DIR", str(blocker))
    assert lane_inbox.put("lane-a", "nudge", "text") is False
    assert lane_inbox.peek("lane-a") == []


# --- the edge's door ------------------------------------------------------

class Runner:
    """The edge's injected runner: canned clients, everything else empty."""

    def __init__(self, clients=(), rc=0, out=None):
        self.calls, self.clients, self.rc, self.out = [], list(clients), rc, out

    def __call__(self, cmd, cwd=None):  # noqa: ARG002
        self.calls.append(list(cmd))
        if "list-clients" in cmd:
            if self.out is not None:
                return self.rc, self.out
            return self.rc, "".join(
                f"{int(a)} {tty}\n" for tty, a in self.clients)
        if "capture-pane" in cmd:
            return 0, "● ready\n❯ \n"
        return 0, ""

    def sent(self):
        return [c for c in self.calls if "send-keys" in c]


@pytest.mark.parametrize("kind", ["prompt", "interrupt"])
def test_an_active_client_refuses_the_edges_keystroke_and_names_it(kind):
    r = Runner(clients=[("/dev/pts/9", time.time() - 5)])
    out = realize_seat_action({"kind": kind, "args": {"text": "hello"}},
                              SEAT, r, pause=lambda _s: None)
    assert out["status"] == "refused", out
    assert r.sent() == [], "keys reached a pane in use"
    # Named, not merely refused: "pane in use" sends the operator looking,
    # "/dev/pts/9 was active 5s ago" tells them which of their own windows.
    assert "/dev/pts/9" in out["observed"]["why"]
    assert out["observed"]["client_tty"] == "/dev/pts/9"


def test_an_idle_client_is_not_in_use():
    r = Runner(clients=[("/dev/pts/9", time.time() - SEAT_IN_USE_SECONDS - 60)])
    out = realize_seat_action({"kind": "interrupt", "args": {}}, SEAT, r,
                              pause=lambda _s: None)
    assert out["status"] == "done", out
    assert r.sent(), "an attached-but-idle client blocked the edge forever"


def test_the_in_use_probe_asks_before_it_types():
    r = Runner()
    realize_seat_action({"kind": "prompt", "args": {"text": "hi"}}, SEAT, r,
                        pause=lambda _s: None)
    kinds = [next((x for x in c if x in ("list-clients", "capture-pane",
                                         "send-keys")), "?") for c in r.calls]
    assert kinds.index("list-clients") < kinds.index("send-keys"), kinds


@pytest.mark.parametrize("rc,out", [
    (1, ""),                                  # the call failed
    (0, ""),                                  # nobody attached
    (0, "● ready\n> a pane capture\n"),       # not a client listing at all
    (0, "notanumber /dev/pts/1\n"),           # unparseable activity
])
def test_an_unreadable_probe_fails_open(rc, out):
    """🔴 Fails OPEN, like the hold mirror: a guard nobody can read must not
    become a second way for every seat action to fail. The pane capture below
    it is still the fail-closed gate on typing blind."""
    assert _seat_client_in_use(SEAT, Runner(rc=rc, out=out)) == ""


def test_a_probe_that_raises_fails_open():
    def boom(_cmd, cwd=None):  # noqa: ARG001
        raise OSError("no runner here")

    assert _seat_client_in_use(SEAT, boom) == ""


def test_a_docker_seat_is_probed_inside_its_container():
    """A docker seat's pane lives INSIDE the container, so host tmux would
    either miss it or hit a same-named host session."""
    r = Runner()
    _seat_client_in_use({"identity": "dt-poc", "substrate": "docker",
                         "session": "seat"}, r)
    assert r.calls[0][:3] == ["docker", "exec", "dt-poc"], r.calls


def test_the_edge_records_its_keystroke_before_sending_it():
    r = Runner()
    realize_seat_action({"kind": "prompt", "args": {"text": "edge text"}},
                        SEAT, r, pause=lambda _s: None)
    row, = injections.read_rows()
    assert row["door"] == "edge_prompt" and row["by"] == "edge"
    assert row["lane"] == "lane-a"
    assert row["sha256"] == injections.sha256("edge text")


def test_a_refused_edge_action_records_nothing():
    """Only a keystroke that is about to happen gets a row. A refusal that
    ledgered would make the load meter discount a turn nobody caused."""
    r = Runner(clients=[("/dev/pts/9", time.time() - 5)])
    realize_seat_action({"kind": "prompt", "args": {"text": "edge text"}},
                        SEAT, r, pause=lambda _s: None)
    with pytest.raises(OSError):
        injections.read_rows()


# --- squad: the rules that live in the script ------------------------------

@pytest.fixture(scope="module")
def squad_src():
    return SQUAD.read_text(encoding="utf-8")


def test_the_compaction_ask_is_off_in_code_not_in_a_drop_in(squad_src):
    """Term 2. Until r5 the only thing between an idle lane over the cap and
    a typed ask was a drop-in on ONE box — and the complaint this area comes
    from is about a lane typed into on a box nobody had configured."""
    assert 'COMPACT_ASK="${MCP_HUB_COMPACT_ASK:-0}"' in squad_src, (
        "the ask must default OFF in the script itself")
    assert '[ "$COMPACT_ASK" != 1 ]' in squad_src, "nothing reads the switch"


def test_arming_the_ask_does_not_move_the_measured_cap(squad_src):
    """The 138111 is the deputy's measured number (180000 - 41889 over 65
    post-fix episodes). Arming the ask must not require re-deriving it."""
    assert 'COMPACT_ASK_TOKENS="${MCP_HUB_COMPACT_ASK_TOKENS:-138111}"' in squad_src


def test_the_hand_verbs_never_consult_pane_in_use(squad_src):
    """Term 1, as a property of the source: his cockpit buttons call these,
    and there is no --by-hand flag to remember either."""
    code = "\n".join(ln for ln in squad_src.splitlines()
                     if not ln.lstrip().startswith("#"))
    assert "--by-hand" not in code, "Term 1 forbids an override flag"
    body = squad_src.split('\ncase "${1:-help}" in', 1)[1]
    for verb in ("cmd", "key", "answer", "model"):
        arm = re.search(rf"\n  {verb}\)(.*?)\n  [a-z][a-z0-9_|-]*\)",
                        body, re.S)
        assert arm, f"no {verb} arm found"
        assert "pane_in_use" not in arm.group(1), (
            f"the {verb} arm refuses a pane in use — Term 1 says it must not")
        assert "ledger_note" in arm.group(1), (
            f"the {verb} arm types without recording a ledger row")
        assert re.search(r"ledger_note\s+\"\$\w+\"\s+\w+\s+hand", arm.group(1)), (
            f"the {verb} arm must record `by: hand`")


def test_the_in_use_window_has_a_default_and_an_override(squad_src):
    assert 'SQUAD_IN_USE_SECONDS="${SQUAD_IN_USE_SECONDS:-600}"' in squad_src


def test_the_probe_asks_tmux_rather_than_inferring(squad_src):
    """`list-clients` is the only thing that knows. Inferring from a pane
    capture, a status cache or a roster field would be a guess about a
    person."""
    body = re.search(r"\npane_in_use\(\) \{(.*?)\n\}", squad_src, re.S)
    assert body, "squad has no pane_in_use door"
    assert "list-clients" in body.group(1)
    assert "client_activity" in body.group(1), (
        "attached-but-idle must not count as in use")


def test_the_ledger_text_never_reaches_argv_from_squad(squad_src):
    """A `cmd` line can carry a credential and argv is readable in `ps`."""
    note = re.search(r"\nledger_note\(\) \{(.*?)\n\}", squad_src, re.S)
    assert note, "squad has no ledger_note"
    assert "printf '%s' \"$4\" |" in note.group(1), (
        "the typed text must be piped in, never passed as an argument")
    assert "|| true" in note.group(1), (
        "a failed ledger write must never stop the send")


def test_a_refused_nudge_is_left_for_the_lane_rather_than_dropped(squad_src):
    arm = re.search(r"\n              nudge\)(.*?)\n                ;;",
                    squad_src, re.S)
    assert arm, "no heal nudge arm found"
    text = arm.group(1)
    assert "pane_in_use" in text, "the nudge does not reach the door"
    assert "lane_inbox_put" in text, "a refused nudge is dropped"
    # NOT charged and NO strike: the lane was never asked anything, so it
    # cannot have failed to answer — and a strike escalates the next pass to
    # a relaunch of a lane somebody is working in.
    charged = text.split("if pane_in_use", 1)[1].split("else", 1)[0]
    assert "$ncnt" not in charged, "a refused nudge was charged"
    assert 'touch "$flag"' not in charged, "a refused nudge armed the strike"


def test_the_compaction_door_is_gated_once_for_every_caller(squad_src):
    """The ask and the exec leg both type through `compaction_type`. Gating
    the callers instead would leave whichever path is added next silently
    unguarded — the four-of-five-routes mistake in a new costume."""
    body = re.search(r"\ncompaction_type\(\) \{(.*?)\n\}", squad_src, re.S)
    assert body and "pane_in_use" in body.group(1)
    assert body.group(1).index("pane_in_use") < body.group(1).index("send-keys"), (
        "the door types before it asks whether anybody is there")


def test_the_lane_inbox_header_has_exactly_one_definition(squad_src):
    """squad reaches `python -m mcp_hub.lane_inbox` rather than writing the
    file itself, so the sentence the lane reads is defined once. Two copies
    is two things to keep in step, and the one that drifts is the copy nobody
    is looking at."""
    assert lane_inbox.HEADER not in squad_src
    put = re.search(r"\nlane_inbox_put\(\) \{(.*?)\n\}", squad_src, re.S)
    assert put and "mcp_hub.lane_inbox put" in put.group(1)


def test_the_hook_blocks_with_the_inbox_and_clears_it(tmp_path, monkeypatch):
    """The other end of the refusal: the lane hears it at its next Stop."""
    import argparse
    import contextlib
    import io

    from mcp_hub import cli

    monkeypatch.setenv("MCP_HUB_HELD_FILE", str(tmp_path / "held.json"))
    monkeypatch.setenv("MCP_HUB_HOLD_BOUNDARY_DIR", str(tmp_path / "b"))
    monkeypatch.setattr(cli, "_read_hook_stdin",
                        lambda: {"session_id": "s1", "stop_hook_active": False})

    async def _q(*_a, **_k):
        return ("", "", True, "")

    monkeypatch.setattr(cli, "_query_hub", _q)
    lane_inbox.put("lane-1", "nudge", "re-register please")

    def one_stop():
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.stop_hook_command(argparse.Namespace(
                name="lane-1", project="org/repo",
                hub_url="http://hub.invalid/mcp"))
        out = buf.getvalue().strip()
        if not out:
            return None
        doc = json.loads(out)
        return (doc.get("hookSpecificOutput") or {}).get("additionalContext")

    text = one_stop()
    assert text is not None and "re-register please" in text, text
    assert lane_inbox.peek("lane-1") == [], "the inbox was not cleared"
    assert one_stop() is None, "the inbox was delivered twice"
