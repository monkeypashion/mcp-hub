"""v2-fleet revision 9 falsifiers, part C: transcript JSONL readers (Vortex project v2-fleet, line hub-master, mcp-hub). FROZEN.

A Claude Code transcript is JSONL: one JSON record per "\n"-terminated line. JSON allows U+2028, U+2029 and U+0085 RAW
inside a string (JavaScript's JSON.stringify and Python's json.dumps(ensure_ascii=False) write them unescaped), but
`str.splitlines()` breaks a line at each of them. `receipts.rendered_message_ids` and `cli._read_last_assistant_text`
read the transcript with `.splitlines()`, so a record carrying one of them is cut in two, neither half parses, and
the record is skipped: no receipt (the message is re-printed at the next drain), or the wrong last assistant text
(r9 finding, with mcp-hub's message lane, hub 37091). \v, \f and \x1c-\x1e cannot appear raw in valid JSON (control
characters are escaped), so they are not tested here. Base: r8's built head d0f5887 (provisional).

Widened with hub 37103 to three more JSONL readers with the same fault: `shadow.observed_renders` (a render
undercounted), `evidence.read_turn` (the turn lost) and the trim in `shadow.record` (read_text().splitlines() then
"\n".join rewrites a raw-separator record as two broken lines, for good).

One group (`-k r9_receipts`): all five readers split JSONL on "\n" only; a record with a raw U+2028/U+2029/U+0085 is read;
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


# -- added with hub 37103: three more JSONL readers (shadow.observed_renders, evidence.read_turn, shadow.record's trim)


def _render_rec(agent: str, text: str) -> dict:
    return {"type": "user", "message": {"content": f'<channel source="hub" from_agent="{agent}">{text}</channel>'}}


@pytest.mark.parametrize("sep", SEP_IDS)
def test_r9_receipts_shadow_render_with_raw_break_is_counted(tmp_path, sep):
    from mcp_hub import shadow

    s = RAW_SEPARATORS[sep]
    lines = [_raw(_render_rec("alice", "one")), _raw(_render_rec("bob", f"two{s}lines")), _raw(_render_rec("carol", "three"))]
    got = shadow.observed_renders(_transcript(tmp_path, lines))
    assert [r["agent"] for r in got] == ["alice", "bob", "carol"], f"a render with a raw {sep} was not counted: {got!r}"


@pytest.mark.parametrize("sep", SEP_IDS)
def test_r9_receipts_evidence_turn_with_raw_break_is_read(tmp_path, sep):
    from mcp_hub import evidence

    s = RAW_SEPARATORS[sep]
    want = f"the reply{s}and the rest"
    path = _transcript(tmp_path, [_raw(_user("please do it")), _raw(_asst(want))])
    turn = evidence.read_turn(path)
    assert turn is not None and turn["text"] == want, f"the turn with a raw {sep} read as {turn!r}"
    assert turn["trigger"] == "operator"


@pytest.mark.parametrize("sep", SEP_IDS)
def test_r9_receipts_shadow_trim_keeps_records_whole(tmp_path, sep):
    from mcp_hub import shadow

    s = RAW_SEPARATORS[sep]
    log = tmp_path / "shadow.jsonl"
    # A log as the r8 writer leaves it (ensure_ascii=False: the separator raw), then appends that force trims.
    seeded = [{"kind": "double_surface", "handle": f"seed{s}{i}"} for i in range(3)]
    log.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in seeded), encoding="utf-8")
    new = [{"kind": "false_compaction", "handle": f"new{s}{i}", "n": i} for i in range(4)]
    for e in new:
        shadow.record([e], log, cap=5)
    lines = log.read_text(encoding="utf-8").split("\n")
    assert lines[-1] == "", "the log does not end with a newline"
    got = []
    for ln in lines[:-1]:
        try:
            got.append(json.loads(ln))
        except ValueError as e:
            raise AssertionError(f"the trim left a line that is not a record ({sep}): {ln!r}") from e
    assert got == (seeded + new)[-5:], f"the trim did not keep the last 5 records whole: {got!r}"


def test_r9_receipts_guard_shadow_and_evidence_normal_and_malformed(tmp_path):
    from mcp_hub import evidence, shadow

    lines = [_raw(_render_rec("alice", "one")), '{"type": "user", "message": {"content": "<channel from_agent=\\"x\\">cut',
             json.dumps(_render_rec("bob", "escaped\u2028two")), _raw(_render_rec("carol", "three"))]
    assert [r["agent"] for r in shadow.observed_renders(_transcript(tmp_path, lines))] == ["alice", "bob", "carol"]
    turn = evidence.read_turn(_transcript(tmp_path, [_raw(_user("go")), _raw(_asst("done it")),
                                                     '{"type": "assistant", "message": {"content": [{"type": "text", "text": "cut']))
    assert turn is not None and turn["text"] == "done it" and turn["trigger"] == "operator", turn


def test_r9_receipts_guard_shadow_trim_plain(tmp_path):
    from mcp_hub import shadow

    log = tmp_path / "shadow.jsonl"
    entries = [{"kind": "false_compaction", "n": i, "handle": "café ⟨x⟩"} for i in range(8)]
    for e in entries:
        shadow.record([e], log, cap=5)
    text = log.read_text(encoding="utf-8")
    assert [json.loads(ln) for ln in text.splitlines()] == entries[-5:]
    assert text.endswith("\n")


# Added with mcp-hub's r9 review (18 Oct, T47/F33 widened): `cli._rekey_transcript`, the history re-key of a
# transported transcript. It reads with `.splitlines()` and writes back with "\n".join, so a record carrying a raw
# U+2028/U+2029/U+0085 is cut in two, both halves are kept as "unparseable" lines, and the separator itself is gone:
# the transported transcript holds two broken lines where one record was.
REKEY_OLD = "/mnt/d/Projects/code/monkeypashion/mcp-hub"
REKEY_NEW = "/home/monke/Projects/xport/mcp-hub"


def _js(rec: dict) -> str:
    """Claude Code's JS writer: compact separators, non-ASCII left raw."""
    return json.dumps(rec, separators=(",", ":"), ensure_ascii=False)


@pytest.mark.parametrize("sep", SEP_IDS)
def test_r9_receipts_rekey_keeps_a_raw_break_record_whole(sep):
    from mcp_hub import cli

    s = RAW_SEPARATORS[sep]
    recs = [
        {"type": "user", "cwd": REKEY_OLD, "message": {"content": "first"}},
        {"type": "assistant", "cwd": REKEY_OLD, "message": {"role": "assistant",
                                                            "content": [{"type": "text", "text": f"before{s}after"}]}},
        {"type": "user", "cwd": REKEY_OLD, "message": {"content": "last"}},
    ]
    out, stats = cli._rekey_transcript("\n".join(_js(r) for r in recs) + "\n", REKEY_OLD, REKEY_NEW)
    assert out.endswith("\n"), "the re-keyed transcript does not end with a newline"
    lines = out[:-1].split("\n")
    got = []
    for ln in lines:
        try:
            got.append(json.loads(ln))
        except ValueError as e:
            raise AssertionError(f"the re-key wrote a line that is not a record ({sep}): {ln!r}") from e
    want = [dict(r, cwd=REKEY_NEW) for r in recs]
    assert got == want, f"the re-key did not keep each record whole apart from cwd ({sep}): {got!r}"
    assert stats["unparseable"] == 0, f"the re-key counted {stats['unparseable']} unparseable lines ({sep})"


def test_r9_receipts_guard_rekey_cut_line_kept_verbatim():
    from mcp_hub import cli

    cut = '{"type":"user","cwd":"' + REKEY_OLD + '","message":{"content":"cut'
    good = {"type": "user", "cwd": REKEY_OLD, "message": {"content": "café \u2028 escaped in the input"}}
    text = _js({"type": "user", "cwd": REKEY_OLD, "message": {"content": "one"}}) + "\n" + cut + "\n" + json.dumps(good) + "\n"
    out, stats = cli._rekey_transcript(text, REKEY_OLD, REKEY_NEW)
    lines = out[:-1].split("\n")
    assert len(lines) == 3 and lines[1] == cut, f"a cut line was not kept verbatim: {lines!r}"
    assert stats["unparseable"] == 1, stats
    assert json.loads(lines[0])["cwd"] == REKEY_NEW and json.loads(lines[2]) == dict(good, cwd=REKEY_NEW)
