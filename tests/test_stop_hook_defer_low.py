"""Bar 47: a low-only Stop drain costs no model turn — and loses nothing.

Emitting a hook block continues the session, and that continuation IS a model
turn. Measured on one mcp-hub session (2026-09-01): 18 hook fires, 13 costing
a turn, against 7 genuine operator prompts — most of them a flapping `[low]`
monitor. `low` already means "never interrupt" on the hub side (card #73 gave
it no backstop wake); the Stop hook was reinstating the interrupt one layer
down.

THE DANGEROUS PART, and what most of these tests are about: the hub marks
messages READ during the drain, so by the time we decide not to block, the
spool file is the only copy. A spool that could be forgotten would be the
PR #8 silent-loss bug rebuilt one layer out ("push success ≠ seen"). The
invariant is therefore not "low is quiet" but **every deferred item is
printed by the next block, and a block always eventually comes**:

  * any later block prepends and clears the spool, whatever triggered it;
  * DEFER_MAX_SECONDS forces a block by itself, so a silent lane cannot hold
    a spool forever;
  * every uncertain reading — an unparseable line, an unwritable spool, an
    untagged (i.e. normal) message — blocks instead. The failure direction is
    a wasted turn, never a lost message.
"""
from __future__ import annotations

import time

import pytest

from mcp_hub import cli


def line(ref: int, prio: str = "", sender: str = "alice", grade: str = " ·verified") -> str:
    tag = f" [{prio}]" if prio else ""
    return f"[15:39:03] **{sender}**{grade} ⟨hub.msg/1?id={ref}⟩{tag}: body text"


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    # _state_dir() reads the env at call time, so the spool lands here and
    # never touches the real fleet state.
    monkeypatch.setenv("MCP_HUB_STATE_DIR", str(tmp_path))


def build(**kw):
    base = dict(
        agent_name="lane-1", project="org/repo", messages_text="",
        broadcasts_text="", is_online=True, defer_low=True,
    )
    base.update(kw)
    return cli.build_hook_response(**base)


# --- the win ---------------------------------------------------------------

def test_a_low_only_drain_does_not_block():
    assert build(messages_text=line(1, "low")) is None


def test_a_low_only_broadcast_flood_does_not_block():
    flood = "\n".join(line(i, "low", sender="claude-mic-watch") for i in range(20))
    assert build(broadcasts_text=flood) is None


# --- what must still interrupt --------------------------------------------

def test_an_untagged_line_is_normal_and_blocks():
    # get_messages tags only NON-normal priorities, so "no tag" is the
    # case that must never be read as low.
    assert build(messages_text=line(1)) is not None


def test_one_normal_among_many_low_blocks_the_whole_drain():
    mixed = "\n".join([line(1, "low"), line(2), line(3, "low")])
    assert build(messages_text=mixed) is not None


def test_urgent_blocks():
    assert build(messages_text=line(1, "urgent")) is not None


def test_an_unparseable_drain_blocks():
    # Fails closed: an unrecognised render must surface, not vanish.
    assert build(messages_text="something in a shape we don't know") is not None


@pytest.mark.parametrize("extra", [
    {"card_nag": True},
    {"card_notice": "card #12 still open"},
    {"is_online": False},
])
def test_corrections_are_not_traffic_and_still_block(extra):
    assert build(messages_text=line(1, "low"), **extra) is not None


def test_defer_off_by_default_blocks_exactly_as_before():
    assert build(messages_text=line(1, "low"), defer_low=False) is not None


# --- losslessness: the part that matters ----------------------------------

def test_the_deferred_item_is_printed_by_the_next_block():
    assert build(messages_text=line(1, "low")) is None      # deferred
    resp = build(messages_text=line(2))                      # normal → block
    body = resp["reason"] if "reason" in resp else str(resp)
    assert "id=1" in body, "the deferred low message was not carried into the block"
    assert "id=2" in body


def test_the_spool_is_cleared_once_printed():
    build(messages_text=line(1, "low"))
    first = str(build(messages_text=line(2)))
    assert "id=1" in first
    second = str(build(messages_text=line(3)))
    assert "id=1" not in second, "a printed item must not be printed again forever"


def test_several_deferrals_all_survive_to_one_block():
    for i in (1, 2, 3):
        assert build(messages_text=line(i, "low")) is None
    body = str(build(messages_text=line(9)))
    for i in (1, 2, 3, 9):
        assert f"id={i}" in body


def test_an_aged_spool_blocks_on_its_own_with_no_new_traffic():
    # The silent-lane case: nothing else ever arrives. Without this the
    # spool would be held indefinitely, which is a silent drop with extra
    # steps.
    assert build(messages_text=line(1, "low")) is None
    spool = cli._defer_spool("lane-1")
    spool.write_text(
        f"#spooled-at {time.time() - cli.DEFER_MAX_SECONDS - 60}\n" + line(1, "low"),
        encoding="utf-8",
    )
    resp = build(messages_text=line(2, "low"))   # still low-only
    assert resp is not None, "an aged spool must force a block by itself"
    assert "id=1" in str(resp)


def test_the_age_bound_measures_the_oldest_item_not_the_newest():
    # A steady trickle of low traffic must not keep resetting its own
    # deadline — otherwise a busy-but-quiet lane never flushes.
    spool = cli._defer_spool("lane-1")
    old = time.time() - cli.DEFER_MAX_SECONDS + 30
    spool.write_text(f"#spooled-at {old}\n" + line(1, "low"), encoding="utf-8")
    build(messages_text=line(2, "low"))          # appends, does not reset
    _, age = cli._spool_read("lane-1")
    assert age > cli.DEFER_MAX_SECONDS - 60


def test_an_unwritable_spool_blocks_rather_than_dropping(monkeypatch):
    monkeypatch.setattr(cli, "_spool_append", lambda *a, **k: False)
    assert build(messages_text=line(1, "low")) is not None


# --- bar 113: the drain record must carry the deferral decision ------------
#
# Bar 47 is blocked as not closable from existing data: the saving IS the
# suppressed drain, and a suppressed drain prints nothing, blocks nothing and
# leaves no transcript entry. These pin the field that makes the data exist.

def test_the_setting_is_recorded_even_when_deferral_is_OFF():
    """The control, and the reason it is written unconditionally: a field
    present only on deferred drains gives bar 47 a numerator with no
    denominator — every row would show an effect and nothing would say how
    many drains ran without one."""
    t: dict[str, object] = {}
    build(messages_text=line(1), defer_low=False, trace=t)
    assert t["defer_low"] is False
    assert t["defer_effect"] == "off"


def test_a_suppressed_drain_records_that_it_was_spooled():
    """The saving itself. This is the branch that returns None, so it is
    invisible to every other instrument — which is precisely why bar 47
    could not be measured from existing data."""
    t: dict[str, object] = {}
    assert build(messages_text=line(1, "low"), trace=t) is None
    assert t["defer_low"] is True
    assert t["defer_effect"] == "spooled"


def test_a_drain_that_defers_nothing_records_none_not_spooled():
    """The negative control against a constant-true field: deferral ON and a
    normal message must NOT read as a saving. A field that says "spooled"
    whenever the setting is on measures the setting, not the effect."""
    t: dict[str, object] = {}
    assert build(messages_text=line(1), trace=t) is not None
    assert t["defer_effect"] == "none"


def test_releasing_the_spool_records_the_effect_and_how_long_it_was_held():
    t: dict[str, object] = {}
    assert build(messages_text=line(1, "low")) is None      # spool it
    assert build(messages_text=line(2), trace=t) is not None  # normal releases it
    assert t["defer_effect"] == "released"
    assert isinstance(t["defer_held_s"], float)


def test_a_spool_DESTROYED_by_the_already_delivered_guard_is_recorded():
    """`_spool_take` has already unlinked the file, so this drain throws held
    text away. Deliberate — every line read as already-delivered — but a
    silent destroy is the one thing the record must not omit, or the spool
    becomes another place messages can vanish without a trace."""
    def delivered(ref: int, prio: str = "") -> str:
        # the marker opens the BODY, not a suffix on it
        return line(ref, prio).replace(
            ": body text", ": (already delivered live — full text above)")

    t: dict[str, object] = {}
    assert build(messages_text=delivered(1, "low")) is None   # spool it
    build(messages_text=delivered(2), trace=t)                # takes, then discards
    assert t.get("defer_discarded") is True


def test_no_trace_is_a_no_op():
    """Every existing caller passes no trace; the decision must be unchanged
    by whether anyone is listening."""
    assert build(messages_text=line(1, "low")) is None
    assert build(messages_text=line(2)) is not None
