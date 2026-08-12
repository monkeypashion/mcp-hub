"""`squad rm` verifies its kill instead of assuming it.

The kill line is `tm kill-session ... || true` — right for the ordinary case
(no such session), but it also swallowed a kill that genuinely failed. rm is
the edge's DESTROY verb, and its exit code feeds the reclaim verdict; the
roster row is removed either way, after which nothing on the box asks about
the session again — the falls-between-instruments shape that left five
seat-named sessions running six days past `reclaimed · converged`
(2026-08-12).

Driven the test_heal_action.py way: source the real script, override `tm`
(the single tmux door) to script the session's survival, point $CONF at a
throwaway roster. The worktree path is nonexistent so every side-effecting
branch (opt-out, marker, daemon files) self-skips.
"""

from __future__ import annotations

import pathlib
import subprocess

import pytest

SQUAD = pathlib.Path(__file__).resolve().parents[1] / "squad" / "squad"

pytestmark = pytest.mark.skipif(not SQUAD.exists(),
                                reason="squad script not present")


def _rm(session_survives: bool) -> int:
    has = "return 0" if session_survives else "return 1"
    script = f"""
      source "{SQUAD}" help >/dev/null 2>&1
      CONF="$(mktemp)"
      echo 'ghost-agent|/nonexistent-dir|||' > "$CONF"
      tm() {{
        case "$1" in
          kill-session) return 0 ;;
          has-session)  {has} ;;
        esac
        return 0
      }}
      rm_agent ghost-agent >/dev/null 2>&1
    """
    return subprocess.run(["bash", "-c", script], timeout=60).returncode


def test_a_clean_kill_exits_zero():
    assert _rm(session_survives=False) == 0


def test_a_SURVIVING_session_fails_the_verb():
    """The retirement still completes — the failure names the leftover, not
    the removal — but the exit code goes nonzero so the edge's destroy
    reports rc!=0 instead of feeding a false absence downstream."""
    assert _rm(session_survives=True) != 0
