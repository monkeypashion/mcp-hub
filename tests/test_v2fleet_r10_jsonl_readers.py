"""v2-fleet r10 base tests: three JSONL readers the r10 port adds (mcp-hub's r9 review, 18 Oct, T47/F33 widened). FROZEN.

r9 (part C) settled one rule for JSONL: a record is one "\n"-terminated line, so a reader splits on "\n" alone, never
with `str.splitlines()`, which also breaks at U+2028, U+2029 and U+0085. JSON allows those three RAW inside a string,
and the writers below leave them raw. The r10 port adds three more readers that use `.splitlines()`:

- `conversation.tail_lines`: the tail of a Claude Code transcript (JS writer, raw). `last_typed_turn` walks it, so a
  typed turn carrying a raw separator is cut in two and skipped: the lane's last typed turn reads as an EARLIER one.
- `injections.read_rows`: `~/.mcp-hub/injections.jsonl`, written by `injections.record` with `ensure_ascii=False`
  (lane, door and by are caller strings). A cut row is skipped, and the prune rewrite (`record` on a stale ledger)
  re-reads with `read_rows` and rewrites the file without it, for good.
- `load_meter.Transcripts._events`: every transcript file in the snapshot (JS writer, raw). A typed turn carrying a
  raw separator is lost: the meter undercounts Tim's turns and loses its sha (no ledger match).

One group (`-k r10_jsonl`): each reader keeps a raw-separator record whole; a torn line is still skipped.
Red at the base BY ASSERTION (the readers do not exist at the base until the port; their tests then fail by import,
see the base report). Tests named *_guard_* are green wherever the module exists. Hermetic: tmp files only.
"""
# ruff: noqa: E501 — the build's acceptance tests, kept as written
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

RAW_SEPARATORS = {"u2028": "\u2028", "u2029": "\u2029", "nel": "\x85"}
SEP_IDS = list(RAW_SEPARATORS)
WORKTREE = "/home/me/Projects/lane-a"


def _js(rec: dict) -> str:
    """Claude Code's JS writer: compact, non-ASCII (U+2028/U+2029/U+0085 included) left raw."""
    return json.dumps(rec, separators=(",", ":"), ensure_ascii=False)


def _typed(text: str, ts: str) -> dict:
    return {"type": "user", "timestamp": ts, "origin": {"kind": "human"}, "message": {"role": "user", "content": text}}


def _write(path: Path, lines: list[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))
    return path


def _transcript(home: Path, lines: list[str]) -> Path:
    from mcp_hub import conversation

    return _write(conversation.transcript_dir(WORKTREE, home) / "s1.jsonl", lines)


# ---- conversation.tail_lines / last_typed_turn ----

@pytest.mark.parametrize("sep", SEP_IDS)
def test_r10_jsonl_conversation_tail_keeps_a_raw_break_record_whole(tmp_path, sep):
    from mcp_hub import conversation

    s = RAW_SEPARATORS[sep]
    recs = [_typed("first", "2026-10-18T10:00:00Z"), _typed(f"typed{s}line", "2026-10-18T10:05:00Z")]
    path = _transcript(tmp_path, [_js(r) for r in recs])
    lines = [ln for ln in conversation.tail_lines(path) if ln.strip()]
    try:
        got = [json.loads(ln) for ln in lines]
    except ValueError as e:
        raise AssertionError(f"tail_lines returned a line that is not a record ({sep}): {lines!r}") from e
    assert got == recs, f"tail_lines did not return each record whole ({sep}): {got!r}"
    when = conversation.last_typed_turn("lane-a", WORKTREE, conversation.Ledger([]), home=tmp_path)
    assert when == conversation._instant(recs[1]), f"the last typed turn read as {when!r}, not the newest ({sep})"


def test_r10_jsonl_guard_conversation_torn_line_skipped(tmp_path):
    from mcp_hub import conversation

    recs = [_typed("first", "2026-10-18T10:00:00Z"), _typed("second", "2026-10-18T10:05:00Z")]
    torn = '{"type":"user","timestamp":"2026-10-18T10:09:00Z","origin":{"kind":"human"},"message":{"content":"cut'
    _transcript(tmp_path, [_js(r) for r in recs] + [torn])
    when = conversation.last_typed_turn("lane-a", WORKTREE, conversation.Ledger([]), home=tmp_path)
    assert when == conversation._instant(recs[1]), when


# ---- injections.read_rows (and the prune rewrite that reads with it) ----

@pytest.mark.parametrize("sep", SEP_IDS)
def test_r10_jsonl_injections_rows_with_a_raw_break_are_read(tmp_path, sep):
    from mcp_hub import injections

    s = RAW_SEPARATORS[sep]
    ledger = tmp_path / "injections.jsonl"
    now = time.time()
    for i, lane in enumerate(["lane-a", f"lane{s}b", "lane-c"]):
        assert injections.record(lane, "nudge", f"text {i}", now=now + i, path=ledger)
    assert s in ledger.read_text(encoding="utf-8"), "the writer escaped the separator; the test needs it raw"
    rows = injections.read_rows(ledger)
    assert [r["lane"] for r in rows] == ["lane-a", f"lane{s}b", "lane-c"], f"read_rows lost a row ({sep}): {rows!r}"


@pytest.mark.parametrize("sep", SEP_IDS)
def test_r10_jsonl_injections_prune_keeps_a_raw_break_row(tmp_path, sep):
    from mcp_hub import injections

    s = RAW_SEPARATORS[sep]
    ledger = tmp_path / "injections.jsonl"
    day = 86400
    t0 = time.time() - (injections.RETENTION_DAYS + 1) * day
    assert injections.record("lane-old", "nudge", "old", now=t0, path=ledger)
    # Written while the first row is still inside the window, so this append does not prune.
    assert injections.record(f"lane{s}b", "nudge", "kept", now=t0 + (injections.RETENTION_DAYS - 1) * day, path=ledger)
    assert s in ledger.read_text(encoding="utf-8") and ledger.read_text(encoding="utf-8").count("\n") == 2
    # Now the first row is past the window: this record runs the prune rewrite, which re-reads with read_rows.
    assert injections.record("lane-c", "nudge", "new", now=t0 + (injections.RETENTION_DAYS + 1) * day, path=ledger)
    text = ledger.read_text(encoding="utf-8")
    assert text.endswith("\n")
    try:
        got = [json.loads(ln) for ln in text[:-1].split("\n")]
    except ValueError as e:
        raise AssertionError(f"the prune left a line that is not a row ({sep}): {text!r}") from e
    assert [r["lane"] for r in got] == [f"lane{s}b", "lane-c"], f"the prune did not keep the row whole ({sep}): {got!r}"


def test_r10_jsonl_guard_injections_torn_line_skipped(tmp_path):
    from mcp_hub import injections

    ledger = tmp_path / "injections.jsonl"
    now = time.time()
    injections.record("lane-a", "nudge", "one", now=now, path=ledger)
    with ledger.open("a", encoding="utf-8") as fh:
        fh.write('{"ts": 1, "lane": "cut\n')
    injections.record("lane-c", "nudge", "two", now=now + 1, path=ledger)
    assert [r["lane"] for r in injections.read_rows(ledger)] == ["lane-a", "lane-c"]


# ---- load_meter.Transcripts._events ----

@pytest.mark.parametrize("sep", SEP_IDS)
def test_r10_jsonl_load_meter_counts_a_raw_break_turn(tmp_path, sep):
    from mcp_hub import injections, load_meter

    s = RAW_SEPARATORS[sep]
    texts = ["one", f"two{s}lines", "three"]
    recs = [_typed(t, f"2026-10-18T10:0{i}:00Z") for i, t in enumerate(texts)]
    path = _write(tmp_path / "lane-a" / "s1.jsonl", [_js(r) for r in recs])
    events = load_meter.Transcripts._events(path)
    assert [e[1] for e in events] == ["typed"] * 3, f"the meter read {len(events)} turns, not 3 ({sep}): {events!r}"
    assert [e[2] for e in events] == [injections.sha256(t) for t in texts], f"a typed turn's sha is wrong ({sep})"


def test_r10_jsonl_guard_load_meter_torn_line_skipped(tmp_path):
    from mcp_hub import load_meter

    recs = [_typed("one", "2026-10-18T10:00:00Z"), _typed("two", "2026-10-18T10:01:00Z")]
    torn = '{"type":"user","timestamp":"2026-10-18T10:02:00Z","origin":{"kind":"human"},"message":{"content":"cut'
    path = _write(tmp_path / "lane-a" / "s1.jsonl", [_js(recs[0]), torn, _js(recs[1])])
    assert [e[1] for e in load_meter.Transcripts._events(path)] == ["typed", "typed"]
