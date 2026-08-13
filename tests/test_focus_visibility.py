"""Focus is visible on the surfaces the OPERATOR looks at, not only in a tool.

🔴 Operator, 2026-08-13: *"I set you do not disturb for 30 minutes — I don't
see any 🔕 icon anywhere."* They were right. `🔕` had exactly one renderer,
`server.py`'s `list_agents()`, which is an MCP tool response — something an
agent sees when it asks, and the operator never sees at all. The statusline,
the board tree and the cockpit tab were all blind to it.

Worse than absent: the BOARD PALETTE can switch focus on for a seat (`Focus
30m / 1h / 2h / off`) and then rendered nothing to say it was on. A surface
that can perform an act and cannot show its result is the same defect class as
the cockpit/board divergence found earlier the same day.

CLAUDE.md states the rule this broke outright: *"A silencer nobody can see
turns a delayed message into an apparently-ignored one, and sends the sender
hunting for a relaunch."* The design named the failure; only one surface was
ever wired to prevent it.

⚠️ **BESIDE ⚡, never instead of it** (operator's choice, after the trade was
put to them). `⚡` is "bound and push-deliverable"; `🔕` is "wakes suppressed
for N minutes"; urgent pierces focus, so a focused agent IS still wakeable.
Replacing ⚡ would make focused-and-healthy render identically to
focused-and-dead, and leave a lapsed focus indistinguishable from a lost
binding.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path

import pytest

from mcp_hub.cli import _parse_focus_remaining, _parse_status_from_agents

ROOT = Path(__file__).resolve().parents[1]
STATUSLINE = ROOT / "statusline" / "statusline-command.js"
SQUAD = ROOT / "squad" / "squad"


# ------------------------------------------------ the daemon carries it at all


def test_the_snapshot_carries_focus_as_an_EXPIRY_not_a_countdown():
    """A snapshot is up to a heartbeat old. A stored "28m" would be rendered
    stale, and would FREEZE at 28m if the daemon died holding it — a silencer
    that outlives its own expiry on screen. An expiry lets every reader compute
    the truth and lapses on its own, which is why the hub stores one too."""
    txt = "🟢 **me** ⚡ 💤 🔕 28m (p/q) — bio"
    snap = _parse_status_from_agents(txt, "me")
    left = snap["focus_until"] - time.time()
    assert 27 * 60 < left <= 28 * 60, f"focus_until is not an expiry: {left}"


def test_an_unfocused_agent_gets_no_expiry():
    """The control. Without it every assertion here would pass against a
    parser that stamped an expiry on everyone."""
    txt = "🟢 **me** ⚡ 💤 (p/q) — bio"
    assert _parse_status_from_agents(txt, "me")["focus_until"] == 0.0


def test_focus_is_read_from_MY_row_not_the_fleet():
    """`list_agents` renders every agent. Reading 🔕 off the wrong line would
    put someone else's silence on my statusline."""
    txt = ("🟢 **other** ⚡ 🔕 45m (a/b) — bio\n"
           "🟢 **me** ⚡ 💤 (p/q) — bio")
    assert _parse_status_from_agents(txt, "me")["focus_until"] == 0.0


@pytest.mark.parametrize("head,secs", [
    ("x 🔕 45m", 45 * 60),
    ("x 🔕 2h10m", 130 * 60),
    ("x 🔕 0m", 0),
    ("🟢 **a** ⚡ 💤", 0),
    ("nonsense", 0),
])
def test_the_duration_parse_mirrors_the_hub_formatter(head, secs):
    """server._fmt_minutes is the only writer of this text ('45m' / '2h10m').
    An unrecognised shape yields 0 — inventing a duration would put a silencer
    on screen the hub never reported."""
    assert _parse_focus_remaining(head) == secs


def test_a_bio_containing_the_glyph_cannot_forge_focus():
    """Same reasoning the wakeable parse already applies: only the head, before
    the ' — ' bio separator, is read for markers."""
    txt = "🟢 **me** ⚡ (p/q) — I once wrote 🔕 99m in my bio"
    assert _parse_status_from_agents(txt, "me")["focus_until"] == 0.0


# ------------------------------------------------------------- the statusline


def test_the_statusline_renders_focus_BESIDE_the_wake_glyph():
    src = STATUSLINE.read_text()
    m = re.search(r"hubSeg = paint\(`(⚡[^`]*)`", src)
    assert m, "the statusline's wakeable segment no longer renders ⚡"
    seg = m.group(1)
    assert "fmtFocus" in seg, (
        "the statusline shows ⚡ with no focus marker — the operator cannot "
        "see a silencer they switched on")
    assert seg.startswith("⚡"), (
        "🔕 must sit BESIDE ⚡, not replace it: a focused-and-dead agent would "
        "otherwise render identically to a focused-and-healthy one")


def test_the_statusline_formatter_lapses_on_its_own():
    """Driven through the real function, not a reimplementation of it."""
    src = STATUSLINE.read_text()
    m = re.search(r"const fmtFocus = \(until\) => \{.*?\n  \};", src, re.S)
    assert m, "fmtFocus not found in the statusline"
    probe = m.group(0) + """
const now = Date.now() / 1000;
console.log(JSON.stringify([
  fmtFocus(now + 28 * 60), fmtFocus(now + 130 * 60),
  fmtFocus(now - 60), fmtFocus(undefined), fmtFocus(0),
]));
"""
    out = subprocess.run(["node", "-e", probe], capture_output=True,
                         text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    live, longer, past, absent, zero = json.loads(out.stdout)
    assert live.startswith("🔕") and live.endswith("m")
    assert longer.startswith("🔕2h")
    assert past == "", "an elapsed focus still renders"
    assert absent == "", "a snapshot with no focus field renders a silencer"
    assert zero == ""


# ------------------------------------------------------------------ the board


def _focus_glyph(body: str) -> str:
    """Run squad's own focus_glyph, extracted with its real bounds."""
    text = SQUAD.read_text().splitlines()
    start = next(i for i, ln in enumerate(text) if ln.startswith("focus_glyph()"))
    end = next(i for i in range(start + 1, len(text)) if text[i] == "}")
    fn = "\n".join(text[start:end + 1])
    out = subprocess.run(
        ["bash", "-c", fn + f"\nfocus_glyph '{body}'"],
        capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return out.stdout


@pytest.mark.skipif(not SQUAD.exists(), reason="squad script not present")
def test_squad_hub_glyph_shows_focus_and_only_while_it_lasts():
    now = int(time.time())
    assert _focus_glyph('{"online":true,"focus_until":%d.5}' % (now + 1800)) == "🔕"
    assert _focus_glyph('{"online":true,"focus_until":%d.0}' % (now - 60)) == ""
    assert _focus_glyph('{"online":true,"focus_until":0}') == ""
    # An older daemon writes no such field. Absence of the instrument must not
    # render a silencer.
    assert _focus_glyph('{"online":true,"wakeable":true}') == ""
    assert _focus_glyph("not json at all") == ""


@pytest.mark.skipif(not SQUAD.exists(), reason="squad script not present")
def test_focus_rides_BESIDE_the_wake_glyph_in_squads_hub_column():
    """hub_glyph is the single producer for `who`, the cockpit tab title and
    the board JSON — so appending there covers all three at once."""
    body = SQUAD.read_text()
    fn = body[body.index("hub_glyph() {"):]
    fn = fn[: fn.index("\n}\n")]
    assert "focus_glyph" in fn, "hub_glyph does not show focus"
    assert "printf '⚡'" in fn, (
        "⚡ must still be printed — focus is additive, not a replacement")


# ------------------------------- 🔴 the defect found while wiring the above


def test_a_LOCAL_seat_can_render_the_wake_glyph_at_all():
    """🔴 Pre-existing, measured 2026-08-13: `squad board --json` emits
    `{"agent":…,"hub":"⚡",…}` and has NEVER emitted a `wakeable` key, but the
    tree row read `rec.get("wakeable")`. So the condition was always falsy and
    a LOCAL seat could not show ⚡ — while CLAUDE.md's own example row is
    `🔴 ⚡ 🙋 mcp-hub-fireblade-wsl`. Remote seats were fine (the fleet
    snapshot does carry `wakeable`), which is exactly the shape that hides it:
    the panel looked instrumented because half of it was.
    """
    src = (ROOT / "src" / "mcp_hub" / "settings_app.py").read_text()
    body = src[src.index("def _agent_label"):]
    body = body[: body.index("\n    def ")]
    # The REMOTE branch starts at the 8-space `else:` matching `if a["local"]`.
    # Slicing on a bare "else:" catches the waiting/else one nested above it
    # and cuts the local branch short — which is how this test first "failed"
    # against code that was correct.
    local = body[: body.index('\n        else:')]
    assert 'rec.get("hub")' in local, (
        "the local branch does not read the field board --json actually "
        "emits, so ⚡ can never appear on a local seat")
