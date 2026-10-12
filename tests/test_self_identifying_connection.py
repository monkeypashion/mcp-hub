"""Every lane's hub connection names itself (v2-fleet F16).

A lane whose hub URL carries `?agent=<name>` is rebound at transport connect,
within seconds of a hub restart. A lane without it stays unbound until its
next turn — and an unbound idle lane cannot be woken to take one, which is how
slipstream-dev-vm-1 sat off the hub from 05:51Z to 17:00Z on 7 Oct while
Vortex's r23 build waited eleven hours for it.

Stamping the parameter is each lane owner's act; `mcp-hub rebind-url --fleet`
is the check that says WHICH lanes are missing it. The frozen falsifiers drive
that check end to end through `cli.main`; these tests go at the parts
underneath — which scopes may carry the stamp, what counts as naming yourself,
and the two things the check must never do (write, or report a lane it cannot
actually see).

Hermetic: tmp_path and monkeypatch only. No git, no real ~/.mcp-hub, no hub.
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from mcp_hub import cli

ROOT = Path(__file__).resolve().parents[1]
HOST = "probehost"
COMMS = "--dangerously-load-development-channels server:hub"


def _roster_line(agent, worktree, args, klass=""):
    return f"{agent}|{worktree}||{args}|{klass}"


@pytest.fixture
def box(tmp_path, monkeypatch):
    """A machine: a HOME, a roster, and a derived identity per worktree.

    `lanes()` writes the roster and returns a runner for the fleet check, so a
    test says what the box looks like and reads what the check said about it.
    """
    home = tmp_path / "home"
    (home / ".mcp-hub").mkdir(parents=True)
    work = tmp_path / "w"
    work.mkdir()
    remotes: dict[str, str] = {}
    projects: list[str] = []
    claude_json: dict = {"mcpServers": {}, "projects": {}}

    cfg = home / ".mcp-hub" / "config.json"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    monkeypatch.setattr(cli, "_HUB_CONFIG_PATH", cfg)
    monkeypatch.setattr(cli, "_git_remote_url", lambda cwd: remotes.get(str(cwd)))
    monkeypatch.setattr(cli.platform, "node", lambda: HOST)

    state: dict = {"rows": []}

    def lane(agent, repo, *, args=COMMS, url=None, scope="mcp", opted_in=True,
             klass=""):
        wt = work / repo
        wt.mkdir(exist_ok=True)
        remotes[str(wt)] = f"git@github.com:org/{repo}.git"
        if opted_in:
            projects.append(f"org/{repo}")
        if url is not None:
            entry = {"mcpServers": {"hub": {"type": "http", "url": url}}}
            if scope == "mcp":
                (wt / ".mcp.json").write_text(json.dumps(entry))
            elif scope == "project":
                claude_json["projects"][str(wt)] = entry
            elif scope == "global":
                claude_json["mcpServers"]["hub"] = {"url": url}
        state["rows"].append(_roster_line(agent, wt, args, klass))
        return wt

    def check(*extra):
        cfg.write_text(json.dumps({"projects": projects}))
        (home / ".claude.json").write_text(json.dumps(claude_json))
        conf = tmp_path / "squad.conf"
        conf.write_text("\n".join(state["rows"]) + "\n")
        monkeypatch.setattr(cli, "SQUAD_CONF", conf)
        return cli.main(["rebind-url", "--fleet", *extra])

    return {"lane": lane, "check": check, "home": home, "work": work,
            "claude_json": claude_json, "tmp": tmp_path}


def line_for(capsys, agent):
    out = capsys.readouterr()
    hits = [ln for ln in (out.out + out.err).splitlines() if agent in ln]
    return hits[-1] if hits else None


# ----------------------------------------------------------------------------
# what counts as naming yourself
# ----------------------------------------------------------------------------

@pytest.mark.parametrize("url,expected", [
    ("http://hub:8090/mcp?agent=mcp-hub-dev-vm-1", "mcp-hub-dev-vm-1"),
    ("http://hub:8090/mcp?keep=1&agent=lane-a", "lane-a"),
    ("http://hub:8090/mcp?agent=lane-a&keep=1", "lane-a"),
    ("http://hub:8090/mcp", None),
    ("http://hub:8090/mcp?keep=1", None),
    ("http://hub:8090/mcp?agent=", None),      # present and empty is not a name
    ("", None),
])
def test_the_agent_parameter_is_read_from_the_url_and_nothing_else(url, expected):
    assert cli._url_agent_param(url) == expected


def test_a_lane_whose_url_names_it_is_ok(box, capsys):
    box["lane"]("alpha-probehost", "alpha", url="http://h/mcp?agent=alpha-probehost")
    assert box["check"]() == 0
    assert "ok" in line_for(capsys, "alpha-probehost")


def test_a_lane_whose_url_names_nobody_is_missing_agent_and_fails(box, capsys):
    box["lane"]("alpha-probehost", "alpha", url="http://h/mcp")
    assert box["check"]() == 1
    assert "missing-agent" in line_for(capsys, "alpha-probehost")


def test_a_lane_whose_url_names_another_seat_is_a_mismatch_naming_both(box, capsys):
    """The failure that matters: the URL works, the binding it claims is
    someone else's. 2026-07-27 — one seat's identity in a shared scope had
    every reconnecting agent on dev-vm-1 announcing itself as that seat."""
    box["lane"]("alpha-probehost", "alpha", url="http://h/mcp?agent=beta-probehost")
    assert box["check"]() == 1
    found = line_for(capsys, "alpha-probehost")
    assert "mismatch" in found and "beta-probehost" in found


# ----------------------------------------------------------------------------
# which scopes may carry the stamp
# ----------------------------------------------------------------------------

def test_the_per_project_override_counts_as_the_lanes_own_stamp(box, capsys):
    """~/.claude.json's projects[<worktree>] denotes ONE seat, so it is a
    legitimate place for that seat's identity — unlike the global entry."""
    box["lane"]("alpha-probehost", "alpha", scope="project",
                url="http://h/mcp?agent=alpha-probehost")
    assert box["check"]() == 0
    assert "ok" in line_for(capsys, "alpha-probehost")


def test_the_shared_global_entry_is_not_a_stamp_and_reads_no_url(box, capsys):
    box["lane"]("alpha-probehost", "alpha", scope="global",
                url="http://h/mcp?agent=alpha-probehost")
    assert box["check"]() == 1
    found = line_for(capsys, "alpha-probehost")
    assert "no-url" in found and "ok" not in found


def test_the_repo_scope_wins_over_the_per_project_override(box, capsys):
    """`_hub_config_candidates`' precedence, read the same way the stamping
    verb reads it — two readers of one order, or they disagree about which
    file the lane is actually connecting with."""
    wt = box["lane"]("alpha-probehost", "alpha", scope="project",
                     url="http://h/mcp?agent=stale-name")
    (wt / ".mcp.json").write_text(json.dumps(
        {"mcpServers": {"hub": {"url": "http://h/mcp?agent=alpha-probehost"}}}))
    assert box["check"]() == 0
    assert "ok" in line_for(capsys, "alpha-probehost")


def test_the_candidate_list_itself_never_includes_the_global_scope(tmp_path):
    """Both readers — the stamping verb and the fleet check — take their
    scopes from this one list, so this is where "never the global entry"
    actually holds. A bare `mcpServers.hub` under HOME would be it."""
    candidates = cli._hub_config_candidates(str(tmp_path))
    assert [(c, tuple(p)) for c, p in candidates] == [
        (Path(tmp_path) / ".mcp.json", ("mcpServers", "hub")),
        (Path.home() / ".claude.json", ("projects", str(tmp_path), "mcpServers", "hub")),
    ], candidates
    assert not [c for c, p in candidates
                if c.name == ".claude.json" and p[:1] != ["projects"]]


# ----------------------------------------------------------------------------
# lanes the check cannot see, and lanes it must not judge
# ----------------------------------------------------------------------------

def test_a_container_lane_is_reported_unchecked_and_decides_nothing(box, capsys):
    """The config that matters lives inside the container. Reporting it `ok`
    from out here would be the 2026-07-25 mistake — claiming a capability
    nothing on this side of the wall measured."""
    box["lane"]("boxed", "boxed", args="@docker:ctr-boxed", klass="faculty")
    assert box["check"]() == 0, "an unchecked lane is not a failure"
    found = line_for(capsys, "boxed")
    assert "container: not checkable from host" in found
    assert "ok" not in found.replace("container: not checkable from host", "")


def test_a_lane_with_no_comms_flag_is_not_listed_at_all(box, capsys):
    box["lane"]("plain-probehost", "plain", args="--continue", url="http://h/mcp")
    box["lane"]("alpha-probehost", "alpha", url="http://h/mcp?agent=alpha-probehost")
    assert box["check"]() == 0
    assert line_for(capsys, "plain-probehost") is None


def test_an_empty_roster_is_reported_not_a_failure(box, capsys):
    assert box["check"]() == 0
    assert "no roster rows" in capsys.readouterr().out


# ----------------------------------------------------------------------------
# the name split: information, not a verdict
# ----------------------------------------------------------------------------

def test_a_roster_name_that_differs_from_the_derived_one_is_still_ok(box, capsys):
    """Two names for one lane is a real fault — and a DIFFERENT one. The URL
    naming either of them is enough to be rebound, so the split is reported
    beside `ok` rather than failing a check that is about something else."""
    box["lane"]("roster-alias", "alpha", url="http://h/mcp?agent=alpha-probehost")
    assert box["check"]() == 0
    found = line_for(capsys, "roster-alias")
    assert "ok" in found and "name-split" in found and "alpha-probehost" in found


def test_a_lane_with_no_derived_identity_is_judged_on_its_roster_name(box, capsys):
    """Not opted in (or no git remote): the derivation answers None, and the
    check still has the roster name to judge by — refusing to look would hide
    exactly the lanes most likely to be misconfigured."""
    box["lane"]("alpha-probehost", "alpha", opted_in=False,
                url="http://h/mcp?agent=alpha-probehost")
    assert box["check"]() == 0
    assert "ok" in line_for(capsys, "alpha-probehost")


# ----------------------------------------------------------------------------
# read-only, and the flag's wiring
# ----------------------------------------------------------------------------

def test_the_check_writes_nothing_even_without_dry_run(box):
    box["lane"]("alpha-probehost", "alpha", url="http://h/mcp")
    box["lane"]("beta-probehost", "beta", scope="project", url="http://h/mcp")
    box["lane"]("boxed", "boxed", args="@docker:ctr-boxed")
    assert box["check"]() == 1           # also what writes the fixture's files

    def tree():
        return ({p: (p.read_bytes(), p.stat().st_mtime_ns)
                 for p in sorted(box["tmp"].rglob("*")) if p.is_file()},
                sorted(str(p) for p in box["tmp"].rglob("*")))

    before = tree()
    # the check itself, with nothing of the fixture's own writing in the way
    assert cli.main(["rebind-url", "--fleet"]) == 1
    assert tree() == before, "the fleet check wrote something"


def test_the_exit_code_is_the_whole_fleets_verdict(box, capsys):
    box["lane"]("alpha-probehost", "alpha", url="http://h/mcp?agent=alpha-probehost")
    assert box["check"]() == 0
    capsys.readouterr()
    box["lane"]("beta-probehost", "beta", url="http://h/mcp")
    assert box["check"]() == 1, "one unstamped lane fails the fleet check"
    out = capsys.readouterr().out
    assert "1/2" in out, out


def test_the_stamping_path_survives_a_namespace_without_fleet(tmp_path, monkeypatch):
    """`rebind_url_command` is called with a hand-built Namespace (tests, the
    settings app). The new flag must be read with getattr or every one of
    those callers becomes an AttributeError in the WRITE path."""
    monkeypatch.setattr(cli, "_derive_agent_identity",
                        lambda cwd: ("lane-probehost", "org/lane"))
    (tmp_path / ".mcp.json").write_text(json.dumps(
        {"mcpServers": {"hub": {"url": "http://h/mcp"}}}))
    args = argparse.Namespace(cwd=str(tmp_path), dry_run=False)
    assert cli.rebind_url_command(args) == 0
    url = json.loads((tmp_path / ".mcp.json").read_text())["mcpServers"]["hub"]["url"]
    assert url.endswith("?agent=lane-probehost")


def test_the_flag_rides_the_existing_verb_because_server_py_is_frozen():
    """A new verb would have to be listed in server.py's _CLI_SUBCOMMANDS —
    which is frozen — and a verb the hub cannot name is a verb nobody is told
    about. So the fleet check is a flag on `rebind-url`."""
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    p = subprocess.run([sys.executable, "-m", "mcp_hub.cli", "rebind-url", "--help"],
                       capture_output=True, text=True, env=env, timeout=120)
    assert p.returncode == 0, p.stderr
    assert "--fleet" in p.stdout and "--dry-run" in p.stdout
    server = (ROOT / "src" / "mcp_hub" / "server.py").read_text()
    assert "rebind-url" in server, "the verb itself is the one the hub names"
