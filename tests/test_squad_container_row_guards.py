"""`comms` / `resume` must never write to a CONTAINER seat's roster row.

🔴 Found 2026-08-13 by RUNNING the cockpit's verbs against a scratch roster,
during the functional half of the cockpit capability audit. All 111 static
cockpit tests were green and could not see it: they prove each menu item is
wired to a verb that exists, never that clicking it does the right thing.

A container seat's field 4 is the **marker** (`@docker:<name>[:<session>]`),
not launch args. `has_comms` knows that — it has a container branch, added
because answering "no comms" for a seat that is demonstrably ⚡ makes the
instrument wrong about its subject. `has_resume` did **not**, so:

    squad resume on seat-x     ->  field 4 becomes "--continue @docker:seat"

after which the row no longer matches `container_of`'s `@docker:*` glob and
squad silently stops knowing it is a container at all. Then it CASCADES: with
`has_comms` now false, `comms on` appends the channels flag to the marker too —
the exact corruption `arm_comms` and the launch path both guard against by
name. Two clicks in the cockpit's Launch settings submenu, two success toasts,
marker destroyed.

The verbs are reachable for every agent tab: the toggles are gated on
`squad.isAgent` alone, deliberately (a context key cannot be refreshed for a
multi-selection — see test_cockpit_menu.py), so the guard has to live here.
"""
from __future__ import annotations

import os
import pathlib
import subprocess

import pytest

SQUAD = pathlib.Path(__file__).resolve().parents[1] / "squad" / "squad"

pytestmark = pytest.mark.skipif(not SQUAD.exists(), reason="squad script not present")

MARKER = "@docker:my-container"


@pytest.fixture
def env(tmp_path):
    """Own HOME, own roster and own tmux SOCKET, so a test can never touch the
    real fleet."""
    home = tmp_path / "home"
    (home / ".config" / "squad").mkdir(parents=True)
    (home / ".mcp-hub").mkdir()
    conf = home / ".config" / "squad" / "squad.conf"
    work = tmp_path / "seat"
    work.mkdir()
    conf.write_text(
        f"seat-x|{work}||{MARKER}|faculty\n"
        f"plain-x|{work}||--continue|faculty\n",
        encoding="utf-8",
    )
    return dict(
        os.environ, HOME=str(home), SQUAD_CONF=str(conf), SQUAD_SOCKET="capaudit-test"
    ), conf


def _run(env_conf, *args) -> subprocess.CompletedProcess:
    env, _ = env_conf
    return subprocess.run(
        ["bash", str(SQUAD), *args],
        capture_output=True, text=True, timeout=60, env=env,
    )


def _args_of(env_conf, agent: str) -> str:
    _, conf = env_conf
    for line in conf.read_text().splitlines():
        parts = line.split("|")
        if parts and parts[0] == agent:
            return parts[3]
    raise AssertionError(f"{agent} not in roster")


# ------------------------------------------------ the marker survives the verb


@pytest.mark.parametrize("verb,mode", [
    ("resume", "on"), ("resume", "off"), ("comms", "on"), ("comms", "off"),
])
def test_the_marker_is_never_rewritten(env, verb, mode):
    res = _run(env, verb, mode, "seat-x")
    assert res.returncode != 0, (
        f"`squad {verb} {mode}` accepted a container seat: {res.stdout}")
    assert "container seat" in res.stderr, res.stderr
    assert _args_of(env, "seat-x") == MARKER, (
        "the marker was rewritten — container_of stops matching and squad "
        "silently forgets this row is a container")


def test_resume_on_is_the_one_that_actually_bit(env):
    """The specific mutation: prepending --continue to the marker. Kept as its
    own test because the parametrized one would still pass if only `comms`
    were guarded."""
    _run(env, "resume", "on", "seat-x")
    assert not _args_of(env, "seat-x").startswith("--continue"), (
        "--continue was prepended to the marker")


def test_the_CASCADE_cannot_start(env):
    """`resume on` was the door: it made has_comms answer false, which
    unblocked `comms on` writing the channels flag into the marker as well.
    Both writes are refused, so the second one has no door to come through."""
    _run(env, "resume", "on", "seat-x")
    _run(env, "comms", "on", "seat-x")
    assert _args_of(env, "seat-x") == MARKER
    assert "channels" not in _args_of(env, "seat-x")


# ------------------------------------------------------- and it still REPORTS


def test_show_answers_instead_of_refusing(env):
    """A read is answerable even where a write is not, and refusing it would
    make the container seat look broken rather than differently-managed."""
    for verb in ("comms", "resume"):
        res = _run(env, verb, "seat-x")
        assert res.returncode == 0, res.stderr
        assert "container seat" in res.stdout, res.stdout


def test_comms_show_still_says_ON_for_a_container(env):
    """has_comms's existing verdict must survive the guard: a container seat's
    comms ARE armed, inside the container, so answering OFF would be the
    instrument-lies failure the container branch was written to prevent."""
    res = _run(env, "comms", "seat-x")
    assert "comms ON" in res.stdout, res.stdout


# --------------------------------------------------------------- the CONTROL
#
# Without this the guard could be a blanket refusal and every test above would
# still pass — the shape of vacuous test #19: probe that the control EXISTS
# before probing its strength.


@pytest.mark.parametrize("verb,mode,expect", [
    ("resume", "off", ""),
    ("comms", "on", "channels"),
])
def test_an_ORDINARY_row_still_toggles(env, verb, mode, expect):
    res = _run(env, verb, mode, "plain-x")
    assert res.returncode == 0, res.stderr
    args = _args_of(env, "plain-x")
    if expect:
        assert expect in args, args
    else:
        assert "--continue" not in args, args


def test_heal_no_longer_prescribes_the_corrupting_command():
    """heal's deaf-sweep iterates comms_agents, which INCLUDES container seats
    (has_comms answers ON for them), and their marker can never carry
    --continue — so they always fell to the branch whose cure string is
    "Fix: squad resume on <a>". The tool recommended the one command that
    corrupted the marker. Behaviour is unchanged (a container was never
    relaunched there either way); only the cure it names."""
    body = SQUAD.read_text()
    sweep = body[body.index("DEAF (predates hub disruption)") - 2000:]
    sweep = sweep[: sweep.index("COOLDOWN")]
    assert "container_of" in sweep, (
        "the deaf-sweep does not distinguish a container seat, so it still "
        "tells the operator to run `resume on` on one")
