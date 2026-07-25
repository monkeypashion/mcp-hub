"""Comms capability is read from the ARGS field, not the roster CLASS.

Background (2026-07-25): roster class doubled as the comms proxy — "faculty =
no hub by design". That broke the day a faculty agent became a real hub member:
mcp-hub-fireblade-wsl registered, held a binding, ran a heartbeat daemon and was
counted in the hub's fleet_total, yet launched as plain `claude --continue` with
no channels flag. It could never hear a wake, and the hub still claimed
"already delivered live" to it.

Two consequences pinned here:
  1. Comms capability now comes from the launch args (the flag IS the ground
     truth), so a comms-enabled faculty agent is healed and shows real hub state.
  2. An agent whose repo is in the hub opt-in list is ARMED automatically at
     launch — pre-launch, never mid-session, because comms is a launch flag and
     "enabling it live" would mean killing a running conversation to apply it.

Also guards a set -e footgun: `comms off` strips the flag with grep -v, which
exits 1 when it filters out EVERY line — exactly the case where the flag was the
only arg. With pipefail set, that silently killed the command and never wrote
the roster.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

SQUAD = Path(__file__).resolve().parents[1] / "squad" / "squad"
FLAG = "--dangerously-load-development-channels server:hub"


def _git(repo: Path, *argv: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *argv],
        check=True,
        capture_output=True,
    )


@pytest.fixture
def box(tmp_path: Path):
    """An isolated 'machine': its own HOME (so the hub opt-in file is ours), its
    own roster, and a real git worktree with an origin remote so project
    derivation works the same way it does live."""
    home = tmp_path / "home"
    (home / ".mcp-hub").mkdir(parents=True)
    worktree = tmp_path / "repo"
    worktree.mkdir()
    _git(worktree, "init", "-q")
    _git(worktree, "remote", "add", "origin", "git@github.com:org/repo.git")
    return home, worktree, tmp_path / "roster.conf"


def _roster(path: Path, rows: list[str]) -> None:
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


def _optin(home: Path, projects: list[str]) -> None:
    import json

    (home / ".mcp-hub" / "config.json").write_text(
        json.dumps({"projects": projects}), encoding="utf-8"
    )


def _squad(box, *argv: str) -> subprocess.CompletedProcess:
    home, _, conf = box
    return subprocess.run(
        ["bash", str(SQUAD), *argv],
        capture_output=True,
        text=True,
        env={
            "HOME": str(home),
            "SQUAD_CONF": str(conf),
            "PATH": "/usr/bin:/bin:/usr/local/bin",
        },
    )


def _helpers(box, snippet: str) -> subprocess.CompletedProcess:
    """Run `snippet` with squad's helper functions in scope.

    Sources everything ABOVE the dispatch `case` so no verb can execute — the
    only way to exercise arm_comms/comms_agents, which have no CLI verb of their
    own (arming is a launch-path side effect).
    """
    home, _, conf = box
    src = SQUAD.read_text(encoding="utf-8")
    head = src.split('\ncase "${1:-help}" in', 1)[0]
    assert "arm_comms()" in head, "extraction boundary moved — fix this helper"
    return subprocess.run(
        ["bash", "-c", head + "\n" + snippet],
        capture_output=True,
        text=True,
        env={
            "HOME": str(home),
            "SQUAD_CONF": str(conf),
            "PATH": "/usr/bin:/bin:/usr/local/bin",
        },
    )


def _args_of(conf: Path, agent: str) -> str:
    for line in conf.read_text(encoding="utf-8").splitlines():
        parts = line.split("|")
        if parts and parts[0] == agent:
            return parts[3]
    raise AssertionError(f"{agent} not in roster")


# ---------------------------------------------------------------------------
# has_comms — the capability predicate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args,expected",
    [
        ("", False),
        ("--continue", False),
        ("--verbose --continue", False),
        (FLAG, True),
        (f"--continue {FLAG}", True),
        ("--channels plugin:hub@1.2.0", True),  # eventual marketplace form
        ("--channels server:other", False),  # channels, but not the hub
    ],
)
def test_has_comms_reads_args_not_class(box, args, expected):
    home, worktree, conf = box
    _roster(conf, [f"a|{worktree}||{args}|faculty"])
    out = _squad(box, "comms", "a").stdout
    assert ("comms ON" in out) is expected, out


def test_comms_is_class_blind(box):
    """Same args => same verdict, whether faculty or squad. Class is lifecycle."""
    home, worktree, conf = box
    _roster(conf, [f"fac|{worktree}||{FLAG}|faculty", f"sq|{worktree}||{FLAG}|"])
    assert "comms ON" in _squad(box, "comms", "fac").stdout
    assert "comms ON" in _squad(box, "comms", "sq").stdout


# ---------------------------------------------------------------------------
# comms on / off
# ---------------------------------------------------------------------------


def test_comms_on_preserves_other_args(box):
    home, worktree, conf = box
    _roster(conf, [f"a|{worktree}||--continue --verbose|faculty"])
    assert _squad(box, "comms", "on", "a").returncode == 0
    got = _args_of(conf, "a")
    assert "--continue" in got and "--verbose" in got
    assert FLAG in got


def test_comms_off_when_flag_is_the_only_arg(box):
    """The set -e regression: grep -v exits 1 when it strips every line, which
    with pipefail silently aborted the command and left the roster unwritten."""
    home, worktree, conf = box
    _roster(conf, [f"a|{worktree}||{FLAG}|faculty"])
    res = _squad(box, "comms", "off", "a")
    assert res.returncode == 0, res.stderr
    assert "comms off" in res.stdout, res.stdout
    assert _args_of(conf, "a") == "", "roster was not written"


def test_comms_off_keeps_other_args_and_orphans_nothing(box):
    home, worktree, conf = box
    _roster(conf, [f"a|{worktree}||--continue {FLAG} --verbose|faculty"])
    assert _squad(box, "comms", "off", "a").returncode == 0
    got = _args_of(conf, "a")
    assert "--continue" in got and "--verbose" in got
    assert "channels" not in got
    # The flag is TWO words — an orphaned target would break the next launch.
    assert "server:hub" not in got


def test_comms_off_plugin_form_drops_its_target_too(box):
    home, worktree, conf = box
    _roster(conf, [f"a|{worktree}||--channels plugin:hub@1.2.0|faculty"])
    assert _squad(box, "comms", "off", "a").returncode == 0
    got = _args_of(conf, "a")
    assert "channels" not in got and "plugin:hub" not in got


def test_comms_toggles_are_idempotent(box):
    home, worktree, conf = box
    _roster(conf, [f"a|{worktree}||{FLAG}|faculty"])
    assert "already on" in _squad(box, "comms", "on", "a").stdout
    _squad(box, "comms", "off", "a")
    assert "already off" in _squad(box, "comms", "off", "a").stdout


# ---------------------------------------------------------------------------
# arm-at-launch — derived from the hub opt-in, not from class or twins
# ---------------------------------------------------------------------------


def test_arm_comms_arms_an_opted_in_agent(box):
    home, worktree, conf = box
    _roster(conf, [f"a|{worktree}|||faculty"])
    _optin(home, ["org/repo"])
    res = _helpers(box, "arm_comms a")
    assert res.returncode == 0, res.stderr
    assert FLAG in _args_of(conf, "a")


def test_arm_comms_is_idempotent_and_quiet(box):
    home, worktree, conf = box
    _roster(conf, [f"a|{worktree}||{FLAG}|faculty"])
    _optin(home, ["org/repo"])
    res = _helpers(box, "arm_comms a")
    assert res.returncode == 0
    assert res.stdout.strip() == "", f"should say nothing when armed: {res.stdout!r}"


def test_arm_comms_leaves_a_non_opted_in_agent_alone(box):
    """Opt-in is the operator's statement of intent; without it, hands off."""
    home, worktree, conf = box
    _roster(conf, [f"a|{worktree}|||faculty"])
    _optin(home, ["someone/else"])
    assert _helpers(box, "arm_comms a").returncode == 0
    assert _args_of(conf, "a") == ""


def test_arm_comms_preserves_existing_args(box):
    home, worktree, conf = box
    _roster(conf, [f"a|{worktree}||--continue|faculty"])
    _optin(home, ["org/repo"])
    _helpers(box, "arm_comms a")
    got = _args_of(conf, "a")
    assert got.startswith("--continue") and FLAG in got


@pytest.mark.parametrize("worktree_path", ["/nonexistent/path", "/tmp"])
def test_arm_comms_survives_underivable_worktrees(box, worktree_path):
    """A missing dir or a non-git dir yields no project — decline, never error
    (this runs on the launch path, where a failure would block the launch)."""
    home, _, conf = box
    _roster(conf, [f"a|{worktree_path}|||faculty"])
    _optin(home, ["org/repo"])
    res = _helpers(box, "arm_comms a")
    assert res.returncode == 0, res.stderr
    assert _args_of(conf, "a") == ""


# ---------------------------------------------------------------------------
# comms_agents — heal's iteration set
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# resume — conversation continuity, a SEPARATE concern from comms
#
# heal's deaf-sweep refuses to relaunch an agent whose roster lacks --continue,
# because that would start a blank conversation and destroy the running one. So
# an agent with comms ON and resume OFF is detected-deaf and still
# unrecoverable — heal nudges it forever with advice the hub says cannot work.
# Deliberately not folded into arm_comms: capability != lifecycle policy.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args,expected",
    [
        ("", False),
        ("--continue", True),
        (f"--continue {FLAG}", True),
        (FLAG, False),
        ("--continue-on-error", False),  # must match the whole token, not a prefix
    ],
)
def test_has_resume_matches_whole_token_only(box, args, expected):
    home, worktree, conf = box
    _roster(conf, [f"a|{worktree}||{args}|faculty"])
    out = _squad(box, "resume", "a").stdout
    assert ("resume ON" in out) is expected, out


def test_resume_on_preserves_comms_flag(box):
    home, worktree, conf = box
    _roster(conf, [f"a|{worktree}||{FLAG}|faculty"])
    assert _squad(box, "resume", "on", "a").returncode == 0
    got = _args_of(conf, "a")
    assert "--continue" in got and FLAG in got


def test_resume_off_when_continue_is_the_only_arg(box):
    """Same set -e/pipefail trap as `comms off`: grep -v exits 1 when it strips
    every line, which silently aborted the write before the `|| true` guard."""
    home, worktree, conf = box
    _roster(conf, [f"a|{worktree}||--continue|faculty"])
    res = _squad(box, "resume", "off", "a")
    assert res.returncode == 0, res.stderr
    assert _args_of(conf, "a") == "", "roster was not written"


def test_resume_off_keeps_comms_flag(box):
    home, worktree, conf = box
    _roster(conf, [f"a|{worktree}||--continue {FLAG}|faculty"])
    assert _squad(box, "resume", "off", "a").returncode == 0
    got = _args_of(conf, "a")
    assert "--continue" not in got
    assert FLAG in got, "turning resume off must not disturb comms"


def test_resume_toggles_are_idempotent(box):
    home, worktree, conf = box
    _roster(conf, [f"a|{worktree}||--continue|faculty"])
    assert "already on" in _squad(box, "resume", "on", "a").stdout
    _squad(box, "resume", "off", "a")
    assert "already off" in _squad(box, "resume", "off", "a").stdout


def test_resume_off_warns_that_heal_cannot_restart(box):
    """The report must name the consequence, not just the state — this is the
    condition that left a live agent detected-deaf and unrecoverable."""
    home, worktree, conf = box
    _roster(conf, [f"a|{worktree}||{FLAG}|faculty"])
    out = _squad(box, "resume", "a").stdout
    assert "REFUSE" in out and "heal" in out, out


def test_comms_and_resume_are_independent(box):
    """The two flags must not interfere — the whole point of separating them."""
    home, worktree, conf = box
    _roster(conf, [f"a|{worktree}|||faculty"])
    _squad(box, "comms", "on", "a")
    _squad(box, "resume", "on", "a")
    got = _args_of(conf, "a")
    assert FLAG in got and "--continue" in got
    _squad(box, "comms", "off", "a")
    got = _args_of(conf, "a")
    assert "channels" not in got and "--continue" in got


def test_comms_agents_is_the_set_that_can_go_deaf(box):
    """Heal must iterate agents that CAN receive wakes, regardless of class.

    A no-comms agent's status cache reads unwakeable forever, so including it
    would make heal nudge and restart it on a loop — while no restart can add a
    launch flag. And a comms-enabled faculty agent genuinely needs healing.
    """
    home, worktree, conf = box
    _roster(
        conf,
        [
            f"fac-comms|{worktree}||{FLAG}|faculty",
            f"fac-plain|{worktree}|||faculty",
            f"sq-comms|{worktree}||{FLAG}|",
            f"sq-plain|{worktree}|||",
        ],
    )
    out = _helpers(box, "comms_agents").stdout.split()
    assert out == ["fac-comms", "sq-comms"], out
