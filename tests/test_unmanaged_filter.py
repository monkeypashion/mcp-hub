"""The pure half of squad's box-wide session inventory.

`all_box_sessions` enumerates every tmux socket under /tmp/tmux-$UID;
`unmanaged_filter` is the rule that decides which of those rows are OUTSIDE
the roster — extracted pure so it can be asserted without a tmux server, the
same argument test_heal_action.py makes for heal_action.

Why the inventory exists: five bypassPermissions sessions sat six days at a
register prompt with no line anywhere the operator looks, because `who`
rendered the ROSTER (what squad started), never the BOX (dt's sweep,
2026-08-12).

Separator note, load-bearing: rows are TAB-separated, not the \\037 the
caches use — tmux vis-escapes control characters in -F output, so a \\037 in
the format string arrives as the four literal bytes "\\037" and every row
fails the field split. The planted-probe smoke that caught this is exactly
the empty-when-blind reading the inventory exists to kill; these tests pin
the tab contract so a "tidy-up" back to \\037 fails loudly.
"""

from __future__ import annotations

import pathlib
import subprocess

import pytest

SQUAD = pathlib.Path(__file__).resolve().parents[1] / "squad" / "squad"

pytestmark = pytest.mark.skipif(not SQUAD.exists(),
                                reason="squad script not present")


def _filter(rows: str, known: list[str]) -> list[str]:
    res = subprocess.run(
        ["bash", "-c",
         'source "$1" help >/dev/null 2>&1; unmanaged_filter "${@:2}"',
         "_", str(SQUAD), *known],
        input=rows, capture_output=True, text=True, timeout=60,
    )
    assert res.returncode == 0, res.stderr
    return res.stdout.splitlines()


def test_roster_names_and_dash_are_filtered_out():
    rows = ("squad\talpha\t1\n"
            "squad\tdash\t2\n"
            "other\tmystery\t3\n")
    assert _filter(rows, ["alpha", "dash"]) == ["other\tmystery\t3"]


def test_a_roster_name_on_a_FOREIGN_socket_is_still_known():
    """The filter keys on the session NAME alone: a roster agent's session is
    managed wherever it lives, and a name-plus-socket key would re-report
    every agent the moment squad's socket is renamed."""
    assert _filter("weird-sock\talpha\t1\n", ["alpha"]) == []


def test_a_session_name_containing_spaces_survives_the_field_split():
    """tmux allows 'weird name'; the tab split must keep it whole rather
    than matching only its first word."""
    out = _filter("s\tweird name\t9\n", ["weird"])
    assert out == ["s\tweird name\t9"]


def test_a_blank_or_separator_less_row_is_dropped_not_passed():
    """A row that never split (the \\037-escaping failure mode) has an empty
    second field — it must be dropped, never smuggled through as unmanaged
    noise or silently counted as clean."""
    assert _filter("\n", []) == []
    assert _filter("no-separators-here\n", []) == []
