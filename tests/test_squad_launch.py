"""`squad launch model|effort` — value-taking launch flags, persisted.

Model and effort were session-only: `squad model` types /model into the running
pane and it dies with that session. The settings panel presented them as
settings, which made the panel lie, so 2026-07-28 gave them a persisted form —
`--model` / `--effort` in the roster's launch args, applied at next start.

The whole difficulty is that these flags take a VALUE. comms and resume are
single tokens that can be matched by pattern; "--model opus" is two words whose
second is arbitrary, so only POSITION identifies it. Getting that wrong leaves
an orphaned "opus" in the args, which is not cosmetic: it becomes an unknown
positional and the agent stops launching.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

SQUAD = Path(__file__).resolve().parents[1] / "squad" / "squad"
COMMS = "--dangerously-load-development-channels server:hub"


@pytest.fixture
def conf(tmp_path: Path) -> Path:
    p = tmp_path / "squad.conf"
    p.write_text(f"demo|/tmp/demo||--continue {COMMS}|squad\n", encoding="utf-8")
    return p


def run(conf: Path, *argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SQUAD), *argv],
        capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(conf.parent), "SQUAD_CONF": str(conf)},
    )


def args_of(conf: Path) -> str:
    return conf.read_text(encoding="utf-8").split("|")[3]


def test_setting_a_model_keeps_every_other_arg(conf):
    run(conf, "launch", "model", "demo", "opus")
    args = args_of(conf)
    assert "--model opus" in args
    assert "--continue" in args and COMMS in args, args


def test_changing_the_value_replaces_it_rather_than_appending(conf):
    run(conf, "launch", "model", "demo", "opus")
    run(conf, "launch", "model", "demo", "sonnet")
    args = args_of(conf)
    assert args.count("--model") == 1, args
    assert "opus" not in args, f"the old value survived as an orphan: {args}"
    assert "--model sonnet" in args


def test_default_removes_the_flag_AND_its_value(conf):
    """THE failure this verb exists to avoid. A stripper that matches the flag
    by pattern removes "--model" and leaves "opus" behind, where it becomes an
    unknown positional and the next launch fails — with a roster that still
    reads plausibly."""
    run(conf, "launch", "model", "demo", "opus")
    run(conf, "launch", "model", "demo", "default")
    args = args_of(conf)
    assert "--model" not in args and "opus" not in args, args
    assert "--continue" in args and COMMS in args, "took other args with it"


def test_model_and_effort_do_not_disturb_each_other(conf):
    run(conf, "launch", "model", "demo", "opus")
    run(conf, "launch", "effort", "demo", "high")
    run(conf, "launch", "model", "demo", "default")
    args = args_of(conf)
    assert "--effort high" in args, args
    assert "--model" not in args


def test_reporting_changes_nothing(conf):
    before = args_of(conf)
    out = run(conf, "launch", "model", "demo")
    assert "default" in out.stdout
    assert args_of(conf) == before


def test_an_invalid_effort_is_refused(conf):
    before = args_of(conf)
    out = run(conf, "launch", "effort", "demo", "turbo")
    assert out.returncode == 1
    assert args_of(conf) == before, "wrote an effort claude will reject at launch"


def test_a_flag_shaped_value_is_refused(conf):
    """`squad launch model --fresh` would write "--model --fresh", which breaks
    every subsequent launch while the roster still looks reasonable."""
    before = args_of(conf)
    out = run(conf, "launch", "model", "demo", "--fresh")
    assert out.returncode == 1
    assert args_of(conf) == before


def test_an_unknown_setting_is_refused(conf):
    out = run(conf, "launch", "colour", "demo", "red")
    assert out.returncode == 1
    assert "usage" in (out.stderr + out.stdout).lower()


def test_the_value_is_the_last_word_not_a_substring_match(conf):
    """A model id can contain the word that names another flag. Position, not
    pattern, is what identifies a value."""
    run(conf, "launch", "model", "demo", "claude-opus-4-8")
    assert "--model claude-opus-4-8" in args_of(conf)
    run(conf, "launch", "model", "demo", "default")
    assert "claude-opus-4-8" not in args_of(conf)
