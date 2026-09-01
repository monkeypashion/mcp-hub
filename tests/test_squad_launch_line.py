"""`launch_line` — an EMPTY override is "explicitly no args", not "no override".

`restart --fresh` strips `--continue` from the roster's args and passes the
result down as a one-off override. On a seat whose ONLY roster arg is
`--continue`, that result is the empty string — and `${override:-$(field …)}`
cannot tell "" from "absent", so it restored the very flag the caller had just
removed. The pane typed `claude --continue` into a brand-new worktree and
claude exited with "no previous conversation" (operator hit this adding
`operator-cockpit-ui-agent-dev-vm-1`, 2026-09-01).

`launch_agent_cmd` already makes the `[ $# -ge 2 ]` distinction one layer up;
`launch_line` making it differently is what let the two disagree. Only a seat
with NO comms armed could reach it — `has_comms` appends the channels flag,
which makes the override non-empty and hides the defect. That is why no
established seat ever saw it, and why the test below pins the no-comms case.

The script has no source guard (running it runs `main`), so the function is
extracted from the real file rather than copied — a copy would drift silently,
and this test exists precisely to catch a one-character regression.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

SQUAD = Path(__file__).resolve().parents[1] / "squad" / "squad"

# Stubs stand in for the two roster/docker lookups launch_line makes:
# not-a-container, and a roster whose args field is exactly `--continue`.
PRELUDE = """
container_of() { return 1; }
field() { printf -- '--continue\\n'; }
"""


def _launch_line_source() -> str:
    src = SQUAD.read_text(encoding="utf-8")
    m = re.search(r"^launch_line\(\) \{.*?^\}", src, re.S | re.M)
    assert m, "launch_line not found in squad/squad — did it get renamed?"
    return m.group(0)


def run_launch_line(*argv: str) -> str:
    script = PRELUDE + _launch_line_source() + '\nlaunch_line "$@"\n'
    p = subprocess.run(
        ["bash", "-c", script, "_", *argv],
        capture_output=True, text=True,
    )
    assert p.returncode == 0, p.stderr
    return p.stdout.strip("\n")


def test_no_override_uses_the_roster_args():
    assert run_launch_line("demo") == "claude --continue"


def test_an_empty_override_means_no_args_not_the_roster_args():
    # THE REGRESSION. With `${override:-$(field …)}` this returns
    # "claude --continue" — the exact failure the operator hit.
    assert run_launch_line("demo", "") == "claude"


def test_an_explicit_override_is_used_verbatim():
    assert run_launch_line("demo", "--continue --model opus") == (
        "claude --continue --model opus"
    )


def test_no_args_prints_bare_claude_with_no_trailing_space():
    # The pane types this line verbatim, so a trailing space is a different
    # command line than the one intended.
    assert run_launch_line("demo", "") == "claude"
    assert not run_launch_line("demo", "").endswith(" ")


@pytest.mark.parametrize("bad", ["${override:-", 'override="${2:-}"'])
def test_the_colon_dash_default_is_gone(bad):
    # Belt and braces: the defect is a shape, not just an outcome. If someone
    # reintroduces the `:-` default the outcome tests above catch it, but this
    # names the cause in the failure message.
    #
    # COMMENTS ARE STRIPPED FIRST — the fix documents the old form in prose to
    # explain itself, and a shape test that reads its own explanation as the
    # defect fails on the correct code (it did, first run).
    code = "\n".join(
        line for line in _launch_line_source().splitlines()
        if not line.lstrip().startswith("#")
    )
    assert bad not in code
