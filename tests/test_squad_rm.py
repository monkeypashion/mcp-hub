"""`squad rm` — retiring ONE agent must not disconnect its siblings.

🔴 THE INCIDENT (2026-08-08). The hub opt-in in `~/.mcp-hub/config.json` is
keyed by PROJECT (`org/repo`); `squad rm` acts on an AGENT. Two clones of one
repo therefore share a single opt-in entry, and `rm_agent` removed it
unconditionally — so retiring `mcp-hub-fireblade-wsl-xport` set
`"projects": []` and took the hub identity away from the operator's live
hub-maintainer agent and from `-windows` at the same time.

Nothing failed loudly. `mcp-hub identity` simply stopped resolving, which makes
the Stop hook, SessionStart and the heartbeat daemon silent no-ops — the fleet
would have shown an agent quietly going dark with no error anywhere. It was
caught only because the command happens to print its opt-out line.

⇒ The property under test is a NEGATIVE with a positive control beside it: the
opt-in must survive while a sibling remains, and must be removed once the last
agent in the project goes. Asserting only the first would pass on a version
that never opts out at all.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess

import pytest

SQUAD = pathlib.Path(__file__).resolve().parents[1] / "squad" / "squad"

pytestmark = pytest.mark.skipif(not SQUAD.exists(), reason="squad script not present")


@pytest.fixture
def rig(tmp_path):
    """Own HOME and roster, plus two real git repos sharing ONE origin.

    The repos must be real: `worktree_project` derives the project from
    `git remote get-url origin`, so a fake directory would resolve to no
    project and the code path under test would never run — the test would pass
    by not reaching the bug.
    """
    home = tmp_path / "home"
    (home / ".config" / "squad").mkdir(parents=True)
    (home / ".mcp-hub").mkdir()
    conf = home / ".config" / "squad" / "squad.conf"
    env = dict(os.environ, HOME=str(home), SQUAD_CONF=str(conf))

    rows = []
    for name in ("alpha", "beta"):
        d = tmp_path / name
        d.mkdir()
        for argv in (["init", "-q"],
                     ["remote", "add", "origin",
                      "git@github.com:acme/shared-repo.git"]):
            subprocess.run(["git", *argv], cwd=d, check=True,
                           capture_output=True)
        rows.append(f"{name}-box|{d}||--continue|faculty")
    conf.write_text("\n".join(rows) + "\n", encoding="utf-8")

    cfg = home / ".mcp-hub" / "config.json"
    cfg.write_text(json.dumps({"projects": ["acme/shared-repo"]}), encoding="utf-8")
    return env, conf, cfg


def _rm(env, agent):
    return subprocess.run(["bash", str(SQUAD), "rm", agent],
                          capture_output=True, text=True, timeout=60, env=env)


def _projects(cfg) -> list[str]:
    return json.loads(cfg.read_text()).get("projects", [])


def test_retiring_one_clone_leaves_its_SIBLING_still_opted_in(rig):
    """The bug, exactly. Two agents, one project, remove one."""
    env, conf, cfg = rig
    r = _rm(env, "alpha-box")
    assert r.returncode == 0, r.stderr

    assert _projects(cfg) == ["acme/shared-repo"], (
        "retiring one clone opted the whole project out — every sibling agent "
        f"just lost its hub identity. stdout: {r.stdout}")
    # and the row it was asked to remove is genuinely gone, so the test is not
    # passing because nothing happened at all
    assert "alpha-box" not in conf.read_text()
    assert "beta-box" in conf.read_text()


def test_it_SAYS_the_opt_in_was_kept_rather_than_claiming_removal(rig):
    """A summary line that overclaims is how the opposite error gets believed."""
    env, _conf, _cfg = rig
    out = _rm(env, "alpha-box").stdout
    assert "opt-in KEPT" in out or "opt-in kept" in out, out


def test_the_LAST_agent_in_a_project_DOES_opt_it_out(rig):
    """The positive control. Without this, a version that never opts out —
    which is also wrong — would pass the test above."""
    env, _conf, cfg = rig
    assert _rm(env, "alpha-box").returncode == 0
    assert _projects(cfg) == ["acme/shared-repo"]      # sibling still there
    assert _rm(env, "beta-box").returncode == 0
    assert _projects(cfg) == [], (
        "the last agent in the project left, but the hub opt-in survived — "
        "hooks will keep firing for a repo with no agents")
