"""v2-fleet r11 F44 / T58 v7 (j6-compaction-ask-says-one-thing): the compaction ask makes ONE promise
about typing, and its steps are ones the lane can do.

Frozen falsifiers, cut on r10's built head b-132 5d5f9b51 (= merge-prep 7d7346bc plus stages/).
ALREADY MET there and kept as GUARDS: one statement about typing per render, chosen by the exec leg
alone (armed by exactly "1"); COMPACT and CLEAR named once each, and the ask is never its own answer.
STILL OPEN, the FALSIFIER (1): the fixed text says "... then park and run /compact or /clear yourself.
Then reply with ..." and the off sentence says "run the command yourself, then reply": the lane is told
to run the command BEFORE it replies, and after /clear it has lost the ask and cannot reply.

Driven through the SAME harness as tests/test_compaction_detect_and_ask.py (its `run` and `_ask_text`),
so the ask under test is the one typed into the stub pane, keystroke for keystroke.

  (1a) the first 'reply' comes before the first 'run ', '/compact' and '/clear';
  (1b) after the first 'run ' the text never asks for the reply as a later step: no 'then reply' and
       no 'reply with' follows it (the armed 'reply anyway' is neither);
  (2)  GUARD: each render holds exactly one statement about typing, true for that box;
  (3)  GUARD: COMPACT and CLEAR occur once each, in the one instruction, and the ask reads as no answer;
  CONTROL: a compact_boundary after the ask still counts as done without a reply (F37 unchanged).
"""
# ruff: noqa: E501 — the build's acceptance tests, kept as written
from __future__ import annotations

import re

import pytest

from tests.test_compaction_detect_and_ask import (
    OVER,
    SAFE_PANE,
    SHRUNK,
    _ask_text,
    commands_typed,
    rows,
    run,
)

# The four renders T58 names: the leg unset, "0", "true" (a truthy word that is NOT "1") and "1".
RENDERS = [None, "0", "true", "1"]
RENDER_IDS = ["unset", "'0'", "'true'", "'1'"]

ARMED_PROMISE = "the command you name is typed for you"
OFF_PROMISE = "nothing will be typed"
RUN_IT = "run the command yourself"


def _env(value):
    return {} if value is None else {"MCP_HUB_COMPACTION_EXEC": value}


def _render(tmp_path, value):
    run(tmp_path, "compaction_one lane-a", env=_env(value), pane=SAFE_PANE)
    return _ask_text(tmp_path)


# --- (1) FALSIFIER: the reply before any step that ends the session ------------------------------

@pytest.mark.parametrize("value", RENDERS, ids=RENDER_IDS)
def test_r11_t58_1a_the_first_reply_comes_before_run_compact_and_clear(tmp_path, value):
    ask = _render(tmp_path, value)
    low = ask.lower()
    assert "reply" in low, f"the ask never asks for a reply:\n{ask}"
    first_reply = low.index("reply")
    for word in ("run ", "/compact", "/clear"):
        at = low.find(word)
        if at < 0:
            continue
        assert first_reply < at, (
            f"{word!r} (at {at}) comes before the first 'reply' (at {first_reply}) with the exec leg "
            f"{value!r}:\n{ask}")


@pytest.mark.parametrize("value", RENDERS, ids=RENDER_IDS)
def test_r11_t58_1b_after_the_first_run_the_reply_is_never_asked_for_as_a_later_step(tmp_path, value):
    ask = _render(tmp_path, value)
    low = ask.lower()
    at = low.find("run ")
    if at < 0:
        return
    after = low[at:]
    for phrase in ("then reply", "reply with"):
        assert phrase not in after, (
            f"{phrase!r} follows the first 'run ' (at {at}) with the exec leg {value!r}: the lane is told "
            f"to run the command before it replies:\n{ask}")


# --- (2) GUARD: one promise about typing --------------------------------------------------------

@pytest.mark.parametrize("value", RENDERS, ids=RENDER_IDS)
def test_r11_t58_2_the_ask_makes_exactly_one_statement_about_typing(tmp_path, value):
    """Every statement about typing uses the word 'typed'; there is exactly one, true for this box."""
    ask = _render(tmp_path, value)
    low = ask.lower()
    statements = re.findall(r"\btyped\b", low)
    assert len(statements) == 1, (
        f"the ask (exec leg {value!r}) makes {len(statements)} statements about typing, not one:\n{ask}")
    if value == "1":
        assert ARMED_PROMISE in low, f"the armed ask does not say the named command is typed for you:\n{ask}"
        assert OFF_PROMISE not in low, f"the armed ask also says nothing will be typed:\n{ask}"
    else:
        assert OFF_PROMISE in low, f"the ask with the leg {value!r} does not say nothing is typed:\n{ask}"
        assert RUN_IT in low, f"the ask with the leg {value!r} does not say to run the command yourself:\n{ask}"
        assert "typed for you" not in low, f"the ask with the leg {value!r} promises typing:\n{ask}"


# --- (3) GUARD: the verdict words are named once, and the ask is never its own answer -------------

@pytest.mark.parametrize("value", RENDERS, ids=RENDER_IDS)
def test_r11_t58_3_the_verdict_words_occur_only_in_the_instruction_that_names_them(tmp_path, value):
    ask = _render(tmp_path, value)
    compact = [m.start() for m in re.finditer(r"\bCOMPACT\b", ask)]
    clear = [m.start() for m in re.finditer(r"\bCLEAR\b", ask)]
    assert len(compact) == 1 and len(clear) == 1, (
        f"COMPACT x{len(compact)}, CLEAR x{len(clear)} in the ask; each must occur once:\n{ask}")
    assert abs(compact[0] - clear[0]) < 40, f"COMPACT and CLEAR are not in one instruction:\n{ask}"
    assert "/COMPACT" not in ask and "/CLEAR" not in ask, f"a slash command is uppercase:\n{ask}"


@pytest.mark.parametrize("value", RENDERS, ids=RENDER_IDS)
def test_r11_t58_3_the_ask_itself_is_read_as_no_answer(tmp_path, value):
    """Three passes with no reply: the answer leg reads nothing, and nothing is typed after the ask."""
    run(tmp_path, "compaction_one lane-a\n" * 3, env=_env(value), pane=SAFE_PANE)
    body = rows(tmp_path)
    assert "AMBIGUOUS" not in body, body
    assert " answer " not in body and "lane answered" not in body, body
    assert commands_typed(tmp_path) == []


# --- CONTROL: F37 unchanged ---------------------------------------------------------------------

@pytest.mark.parametrize("value", [None, "1"], ids=["off", "armed"])
def test_r11_t58_control_a_compact_boundary_after_the_ask_is_done_without_a_reply(tmp_path, value):
    run(tmp_path, "compaction_one lane-a", env=_env(value), pane=SAFE_PANE)
    p = run(tmp_path, "compaction_one lane-a", env=_env(value), pane=SAFE_PANE,
            tokens=SHRUNK, compacted=(OVER, SHRUNK))
    assert "compacted itself on the ask" in p.stdout, p.stdout + p.stderr
    assert "THE LANE DID IT ITSELF" in rows(tmp_path)
    assert commands_typed(tmp_path) == []
