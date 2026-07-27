"""The display rule, which lives in TWO implementations that must agree.

`squad` paints tab titles; the extension reads them back to map a right-click to
an agent. Both strip this machine's hostname from the derived identity, and both
had the same end-anchored assumption — correct for `<repo>-<host>`, wrong for the
`<repo>-<host>-<suffix>` that every transported or duplicated agent carries. The
original rendered as "mcp-hub" while its own copy rendered as
"mcp-hub-fireblade-wsl-windows", side by side in one panel.

"Mirrored — change both or neither" is a comment, and a comment cannot fail.
This runs a table through BOTH implementations and fails if they disagree.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SQUAD = ROOT / "squad" / "squad"
HARNESS = ROOT / "tests" / "cockpit_harness.js"

pytestmark = pytest.mark.skipif(
    not SQUAD.exists() or not HARNESS.exists()
    or subprocess.run(["sh", "-c", "command -v node"], capture_output=True).returncode != 0,
    reason="squad or node not present",
)

HOST = subprocess.run(["hostname"], capture_output=True, text=True).stdout.strip()
SAFE = "".join(c if (c.isalnum() or c in "-_") else "-" for c in HOST.lower()).strip("-")

# (agent, expected display label)
CASES = [
    (f"mcp-hub-{SAFE}", "mcp-hub"),                              # the plain derived form
    (f"mcp-hub-{SAFE}-windows", "mcp-hub-windows"),              # transported / duplicated
    (f"mcp-hub-{SAFE}-showcase-2", "mcp-hub-showcase-2"),        # a numbered copy
    ("unrelated-agent", "unrelated-agent"),                      # another machine's agent
    (f"{SAFE}-thing-{SAFE}", f"{SAFE}-thing"),                   # host as a repo name too
    (SAFE, SAFE),                                                # nothing to strip
    (f"a-{SAFE}b-{SAFE}", f"a-{SAFE}b"),                         # near-miss, not on a boundary
]


def _shell(agent: str) -> str:
    """Run squad's short_label directly. Extracted rather than invoked through a
    verb, so the test pins the FUNCTION and not some caller's use of it."""
    fn = []
    keep = False
    for line in SQUAD.read_text(encoding="utf-8").splitlines():
        if line.startswith("short_label()"):
            keep = True
        if keep:
            fn.append(line)
            if line == "}":
                break
    assert fn, "short_label() not found in squad"
    script = "\n".join(fn) + '\nshort_label "$1" "$2"\n'
    return subprocess.run(["bash", "-c", script, "_", agent, SAFE],
                          capture_output=True, text=True, check=True).stdout


def _js(agent: str) -> str:
    node = subprocess.run(["sh", "-c", "command -v node"],
                          capture_output=True, text=True).stdout.strip()
    res = subprocess.run([node, str(HARNESS), "shortlabel", agent],
                         capture_output=True, text=True, timeout=60,
                         env=dict(os.environ, HARNESS_ANSWERS="[]"))
    assert res.stdout.strip(), f"harness produced nothing: {res.stderr[-500:]}"
    return json.loads(res.stdout.strip().splitlines()[-1])["label"]


@pytest.mark.parametrize("agent,expected", CASES)
def test_both_implementations_agree_and_are_right(agent, expected):
    sh, js = _shell(agent), _js(agent)
    assert sh == js, f"squad says {sh!r}, the cockpit says {js!r} — they must not drift"
    assert sh == expected, f"expected {expected!r}, got {sh!r}"


def test_a_suffixed_copy_is_shortened_at_all():
    """The regression itself, stated plainly: a copy must not render its raw name.

    Guards the specific thing the operator saw — `mcp-hub-fireblade-wsl-windows`
    displayed in full next to a `mcp-hub` that had been shortened.
    """
    raw = f"mcp-hub-{SAFE}-windows"
    assert _shell(raw) != raw and _js(raw) != raw
    assert SAFE not in _shell(raw), "the hostname is still in the display label"
