"""v2-fleet revision 9 falsifiers, part C: transcript JSONL readers (Vortex project v2-fleet, line hub-master, mcp-hub). FROZEN.

A Claude Code transcript is JSONL: one JSON record per "\n"-terminated line. JSON allows U+2028, U+2029 and U+0085 RAW
inside a string (JavaScript's JSON.stringify and Python's json.dumps(ensure_ascii=False) write them unescaped), but
`str.splitlines()` breaks a line at each of them. `receipts.rendered_message_ids` and `cli._read_last_assistant_text`
read the transcript with `.splitlines()`, so a record carrying one of them is cut in two, neither half parses, and
the record is skipped: no receipt (the message is re-printed at the next drain), or the wrong last assistant text
(r9 finding, with mcp-hub's message lane, hub 37091). \v, \f and \x1c-\x1e cannot appear raw in valid JSON (control
characters are escaped), so they are not tested here. Base: r8's built head d0f5887 (provisional).

One group (`-k r9_receipts`): both readers split JSONL on "\n" only; a record with a raw U+2028/U+2029/U+0085 is read;
normal records still count and a truly malformed line is still skipped.

Red at the base BY ASSERTION. Tests named *_guard_* are green on the base and after. Hermetic: tmp files only.
"""
# ruff: noqa: E501 — the build's acceptance tests, kept as written
from __future__ import annotations

import json
from pathlib import Path

import pytest

from mcp_hub import receipts
from mcp_hub.cli import _read_last_assistant_text

RAW_SEPARATORS = {"u2028": "\u2028", "u2029": "\u2029", "nel": "\x85"}
SEP_IDS = list(RAW_SEPARATORS)


def _transcript(tmp_path: Path, lines: list[str]) -> str:
    p = tmp_path / "transcript.jsonl"
    p.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))
    return str(p)


def _raw(rec: dict) -> str:
    """One JSONL record as a JS writer emits it: non-ASCII, U+2028/U+2029/U+0085 included, left raw."""
    return json.dumps(rec, ensure_ascii=False)


def _user(text: str) -> dict:
    return {"type": "user", "message": {"content": text}}


def _asst(text: str) -> dict:
    return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def _dm(msg_id: int, tail: str = "hello") -> str:
    return f'<channel source="hub">DM from alice ⟨hub.msg/1?id={msg_id}⟩: {tail}</channel>'


@pytest.mark.parametrize("sep", SEP_IDS)
def test_r9_receipts_record_with_raw_break_is_counted(tmp_path, sep):
    s = RAW_SEPARATORS[sep]
    lines = [_raw(_user(_dm(7))), _raw(_user(_dm(9, f"two{s}lines{s}three"))), _raw(_user(_dm(11)))]
    path = _transcript(tmp_path, lines)
    assert s in Path(path).read_text(encoding="utf-8"), "the harness escaped the separator"
    got = receipts.rendered_message_ids(path)
    assert got == [7, 9, 11], f"a record with a raw {sep} lost its receipt: {got}"


@pytest.mark.parametrize("sep", SEP_IDS)
def test_r9_receipts_last_assistant_text_with_raw_break(tmp_path, sep):
    s = RAW_SEPARATORS[sep]
    want = f"the last word{s}and the rest"
    path = _transcript(tmp_path, [_raw(_asst("first turn")), _raw(_user("operator says hi")), _raw(_asst(want))])
    got = _read_last_assistant_text(path)
    assert got == want, f"the last assistant text with a raw {sep} read as {got!r}"


def test_r9_receipts_guard_normal_and_malformed(tmp_path):
    lines = [
        _raw(_user(_dm(7))),
        # Truly malformed (cut mid-record), with a ref a regex could lift: never a receipt.
        '{"type": "user", "message": {"content": "' + _dm(8).replace('"', '\\"')[:60],
        json.dumps(_user(_dm(9, "escaped\u2028break"))),  # ensure_ascii: the separator escaped, \u2028
        _raw({**_user(_dm(10)), "isSidechain": True}),
        _raw(_user(_dm(11))),
    ]
    assert receipts.rendered_message_ids(_transcript(tmp_path, lines)) == [7, 9, 11]


def test_r9_receipts_guard_cli_normal_and_malformed(tmp_path):
    lines = [_raw(_asst("first turn")), json.dumps(_asst("escaped\u2028last")), '{"type": "assistant", "message": {"content": [{"type": "text", "text": "cut']
    assert _read_last_assistant_text(_transcript(tmp_path, lines)) == "escaped\u2028last"
    assert _read_last_assistant_text(_transcript(tmp_path, [_raw(_asst("one")), _raw(_asst("two"))])) == "two"
