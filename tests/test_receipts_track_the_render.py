"""Receipt extraction must follow the RENDER — proved against the render itself.

`receipts.py` scans a transcript for the shapes a render puts a ⟨ref⟩ in. Those
shapes live in `server.py`, so the two files are coupled by string layout and
nothing enforced it. On 2026-08-29 the attribution grade (e630fa3 + 9537ba2)
inserted ` ·verified` / ` ·asserted` between the sender's name and the ref —
one space and a word, in exactly the gap both anchors matched literally.

From 07:06Z that day `rendered_message_ids()` returned `[]` for every agent on
every transcript, no matter how many messages had rendered. Nothing raised and
nothing logged: the extractor's entire output is an optimisation, so a total
miss is indistinguishable from a quiet inbox. Every drain fell back to the
legacy `pushed_gen` inference the receipts table was built to replace — the one
the module docstring records as measured wrong in both directions (113 double
surfaces, 76 false compactions). Measured on one live mcp-hub session,
2026-09-01: 6 distinct messages re-read, 18 stop-hook reprints of text already
in context — i.e. goal #25, "the stop hook is re-reading text".

So these tests build their input with the SERVER'S OWN helpers rather than with
hand-written strings. A hand-written fixture would have been updated alongside
the grade and would have kept passing while delivery-receipts stayed dead; a
fixture built from `_grade_tag_str` cannot drift from what the server emits.
The next suffix added between name and ref fails HERE, loudly, instead of
silently switching every agent back to inference.
"""
from __future__ import annotations

import pytest

from mcp_hub import receipts
from mcp_hub.server import _grade_tag_str, _msg_ref

# Every grade the server can render, by the value stored in the column.
GRADES = [
    "session-verified",   # -> ·verified
    "operator-verified",  # -> ·verified
    "asserted",           # -> ·asserted
    "hub-authored",       # -> ·hub
    "",                   # -> ·ungraded (pre-grading rows)
]


def _live_dm(mid: int, grade: str) -> str:
    """The live push tag, built exactly as server.py builds it (:3146)."""
    return (
        f'<channel source="hub" from_agent="alice" kind="dm">\n'
        f"DM from alice{_grade_tag_str(grade)} ⟨{_msg_ref(mid)}⟩: body text\n"
        f"</channel>"
    )


def _live_broadcast(mid: int, grade: str) -> str:
    """The live broadcast tag, as server.py builds it (:3367, :3424)."""
    return (
        f"BROADCAST from alice{_grade_tag_str(grade)} ⟨{_msg_ref(mid)}⟩: body text"
    )


def _drain_line(mid: int, grade: str) -> str:
    """The drain / drain-batch line, as get_messages builds it (:4180)."""
    return f"[15:39:03] **alice**{_grade_tag_str(grade)} ⟨{_msg_ref(mid)}⟩: body text"


def _batched_wake(mid: int, grade: str) -> str:
    """The batched wake line, as _wake_with_queue builds it (:2250)."""
    return f"[15:39:03] DM from alice{_grade_tag_str(grade)} ⟨{_msg_ref(mid)}⟩: body"


RENDERS = {
    "live dm tag": _live_dm,
    "live broadcast tag": _live_broadcast,
    "drain line": _drain_line,
    "batched wake line": _batched_wake,
}


@pytest.mark.parametrize("grade", GRADES)
@pytest.mark.parametrize("shape", sorted(RENDERS))
def test_every_render_shape_yields_a_receipt_at_every_grade(shape, grade):
    text = RENDERS[shape](7, grade)
    assert receipts._refs_in(text) == {7}, (
        f"{shape} at grade {grade!r} rendered as {text!r} but minted no receipt — "
        "the render moved and the extractor did not follow it"
    )


def test_the_grade_really_is_present_in_the_rendered_text():
    # Guards the guard: if _grade_tag_str ever returned "" the parametrised
    # test above would pass while proving nothing about grades at all.
    assert " ·verified" in _drain_line(7, "session-verified")
    assert " ·asserted" in _drain_line(7, "asserted")
    assert " ·ungraded" in _drain_line(7, "")


def test_a_ref_quoted_in_prose_still_mints_nothing():
    # The property the anchors exist for, unchanged by tolerating a grade:
    # a false receipt truncates a message its recipient never saw.
    assert receipts._refs_in(f"re your ⟨{_msg_ref(7)}⟩ from earlier — see above") == set()
    assert receipts._refs_in(f"I mentioned ⟨{_msg_ref(7)}⟩ in passing") == set()


def test_the_grade_cannot_swallow_a_newline():
    # `·` tolerance must not let the anchor cross lines and pair a head with
    # some LATER line's ref, which would receipt a message never rendered.
    text = f"DM from alice ·asserted\nsomething else ⟨{_msg_ref(7)}⟩"
    assert receipts._refs_in(text) == set()
