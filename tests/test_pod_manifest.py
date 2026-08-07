"""N seats in one container — the pure layer. Contract:
docs/n-seats-per-container.md.

The design constraint these tests exist to hold: a pod is N ORDINARY seats
sharing a HOME and a lifecycle, not a new kind of thing. `agent_contract()`
returns the same `SeatContract` the 1:1 path builds, so every helper
downstream is reused rather than reimplemented — and `SEAT_MANIFEST` absent
must leave the 1:1 contract byte-identical, because that is the entire
compatibility story for a fleet that is already running.
"""
from __future__ import annotations

import argparse
import json
import pathlib

import pytest

from mcp_hub import cli
from mcp_hub.seat import (
    SeatContract,
    SeatContractError,
    agent_contract,
    launch_argv,
    marker_content,
    mcp_json_content,
    parse_pod_manifest,
    parse_seat_contract,
    pod_workspace,
)

HUB = "http://hub.example/mcp"


def _env(agents, **kw):
    e = {"SEAT_MANIFEST": json.dumps({"agents": agents}), "MCP_HUB_URL": HUB}
    e.update(kw)
    return e


# ---- the 1:1 path is untouched ---------------------------------------------

def test_no_manifest_means_no_pod():
    """The cheap signal: an image on the old contract never enters any of
    this."""
    assert parse_pod_manifest({"SEAT_IDENTITY": "s", "MCP_HUB_URL": HUB}) is None


def test_an_empty_manifest_is_not_a_pod():
    assert parse_pod_manifest({"SEAT_MANIFEST": "  ", "MCP_HUB_URL": HUB}) is None


def test_the_single_seat_launch_line_is_unchanged():
    """`seat` is the session name the attach affordance and the launch dance
    both hard-code. A default that drifted would break every 1:1 container."""
    c = parse_seat_contract({"SEAT_IDENTITY": "s", "MCP_HUB_URL": HUB})
    assert launch_argv(c, "/w")[:5] == ["tmux", "new-session", "-d", "-s", "seat"]


# ---- refusals --------------------------------------------------------------

def test_both_shapes_at_once_is_refused():
    """A container readable as both has an ambiguous identity, and ambiguity
    is how a message reaches the wrong lane."""
    with pytest.raises(SeatContractError, match="both set"):
        parse_pod_manifest(_env([{"identity": "a"}], SEAT_IDENTITY="a"))


def test_a_dotted_identity_is_refused_because_tmux_cannot_address_it():
    """A dotted name RUNS and cannot be reached — tmux reads `.` as its pane
    separator. Measured on the fleet when one shipped."""
    with pytest.raises(SeatContractError, match=r"tmux"):
        parse_pod_manifest(_env([{"identity": "a.b"}]))


def test_a_colon_is_refused_for_the_same_reason():
    with pytest.raises(SeatContractError, match=r"tmux"):
        parse_pod_manifest(_env([{"identity": "a:b"}]))


def test_a_duplicate_identity_is_refused():
    """Two agents of one name share a workdir, a marker and a session, and the
    last to register silently owns it — the collapse derived identity exists
    to prevent."""
    with pytest.raises(SeatContractError, match="twice"):
        parse_pod_manifest(_env([{"identity": "a"}, {"identity": "a"}]))


def test_an_agent_with_no_identity_is_refused():
    with pytest.raises(SeatContractError, match="no identity"):
        parse_pod_manifest(_env([{"repo": "git@x:o/r.git"}]))


def test_an_empty_pod_is_refused():
    """It would start a container that runs nothing and reports healthy."""
    with pytest.raises(SeatContractError, match="no agents"):
        parse_pod_manifest(_env([]))


def test_bad_json_names_itself():
    with pytest.raises(SeatContractError, match="not valid JSON"):
        parse_pod_manifest({"SEAT_MANIFEST": "{oops", "MCP_HUB_URL": HUB})


def test_a_pod_still_needs_a_hub_url():
    with pytest.raises(SeatContractError, match="MCP_HUB_URL"):
        parse_pod_manifest({"SEAT_MANIFEST": json.dumps([{"identity": "a"}])})


def test_headless_is_refused_for_a_pod():
    """SEAT_PROMPT is single-valued and a pod has several agents; guessing
    which one it addressed is how work lands in the wrong lane."""
    with pytest.raises(SeatContractError, match="headless"):
        parse_pod_manifest(_env([{"identity": "a"}], SEAT_MODE="headless",
                                SEAT_PROMPT="go"))


# ---- accepted shapes -------------------------------------------------------

def test_a_bare_list_is_accepted():
    """Unambiguous — refusing it would be pedantry, not a guard."""
    pod = parse_pod_manifest({"SEAT_MANIFEST": json.dumps([{"identity": "a"}]),
                              "MCP_HUB_URL": HUB})
    assert [a.identity for a in pod.agents] == ["a"]


def test_an_object_carries_the_squad_name():
    pod = parse_pod_manifest({
        "SEAT_MANIFEST": json.dumps({"squad": "capsule",
                                     "agents": [{"identity": "a"}]}),
        "MCP_HUB_URL": HUB})
    assert pod.squad == "capsule"


def test_order_is_preserved():
    """The workspace file lists folders in this order, and an operator reading
    a squad expects the order they declared it in."""
    pod = parse_pod_manifest(_env([{"identity": "c"}, {"identity": "a"},
                                   {"identity": "b"}]))
    assert [a.identity for a in pod.agents] == ["c", "a", "b"]


# ---- one inhabitant is an ORDINARY seat ------------------------------------

def test_an_agent_contract_is_the_same_type_the_1to1_path_builds():
    """The whole design: nothing downstream learns a second shape."""
    pod = parse_pod_manifest(_env([{"identity": "a", "squads": "capsule"}]))
    c = agent_contract(pod, pod.agents[0])
    assert isinstance(c, SeatContract)
    assert marker_content(c) == {"name": "a", "project": "a"}
    assert mcp_json_content(c)["mcpServers"]["hub"]["url"].endswith("?agent=a")


def test_the_session_is_the_identity_not_seat():
    """N sessions cannot share one name."""
    pod = parse_pod_manifest(_env([{"identity": "a"}, {"identity": "b"}]))
    argv = launch_argv(agent_contract(pod, pod.agents[1]), "/w/b", session="b")
    assert argv[:5] == ["tmux", "new-session", "-d", "-s", "b"]


def test_project_precedence_matches_the_single_seat_rule():
    """explicit > origin-derived > the identity itself — the same order
    parse_seat_contract uses, because a pod agent is not a different kind of
    agent."""
    pod = parse_pod_manifest(_env([
        {"identity": "a", "project": "org/explicit"},
        {"identity": "b"},
        {"identity": "c"},
    ]))
    by = {a.identity: a for a in pod.agents}
    assert agent_contract(pod, by["a"], "git@h:org/other.git").project == "org/explicit"
    assert agent_contract(pod, by["b"], "git@h:org/derived.git").project == "org/derived"
    assert agent_contract(pod, by["c"]).project == "c"


def test_squads_reach_register_through_the_shared_prompt():
    """The SEAT_SQUADS fix applies per agent; a pod placed as a squad whose
    members came up squadless is the same defect one layer along."""
    from mcp_hub.seat import first_turn_prompt

    pod = parse_pod_manifest(_env([{"identity": "a", "squads": "capsule"}]))
    assert 'squads="capsule"' in first_turn_prompt(agent_contract(pod, pod.agents[0]))


# ---- the workspace that makes a pod a SQUAD VIEW ---------------------------

def test_the_workspace_lists_container_paths():
    """Host paths cannot appear here — nothing on the host can see inside."""
    pod = parse_pod_manifest(_env([{"identity": "a"}, {"identity": "b"}]))
    ws = pod_workspace(pod, {"a": "/home/seat/work/a", "b": "/home/seat/work/b"})
    assert [f["path"] for f in ws["folders"]] == ["/home/seat/work/a",
                                                  "/home/seat/work/b"]
    assert [f["name"] for f in ws["folders"]] == ["a", "b"]


def test_an_agent_with_no_workdir_is_omitted_not_pointed_at_nothing():
    """A folder entry for a path that does not exist makes VSCode show a
    broken root, which reads as a fault in the agent rather than in the
    file."""
    pod = parse_pod_manifest(_env([{"identity": "a"}, {"identity": "b"}]))
    ws = pod_workspace(pod, {"a": "/home/seat/work/a"})
    assert [f["name"] for f in ws["folders"]] == ["a"]


# ---- the entrypoint itself, --prepare-only ---------------------------------
#
# The pure layer above proves the POLICY. This proves the command actually
# lays a pod down on disk — N workdirs, N markers, N stamped .mcp.json, one
# shared settings.json, one workspace file — without launching claude.


TOKEN = "x" * 60


def _run(tmp_path, monkeypatch, manifest, workdir=None):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", TOKEN)
    monkeypatch.setenv("MCP_HUB_URL", HUB)
    monkeypatch.setenv("SEAT_MANIFEST", json.dumps(manifest))
    monkeypatch.delenv("SEAT_IDENTITY", raising=False)
    work = workdir or (home / "work")
    rc = cli.seat_entry_command(argparse.Namespace(
        workdir=str(work), prepare_only=True))
    return rc, pathlib.Path(work), home


def test_a_pod_lays_down_one_workdir_per_agent(tmp_path, monkeypatch):
    rc, work, home = _run(tmp_path, monkeypatch,
                          {"squad": "capsule",
                           "agents": [{"identity": "a"}, {"identity": "b"}]})
    assert rc == 0
    for ident in ("a", "b"):
        assert (work / ident / ".claude" / "hub-agent.json").exists()
        marker = json.loads((work / ident / ".claude" / "hub-agent.json").read_text())
        assert marker["name"] == ident


def test_each_agent_gets_its_OWN_stamped_mcp_json(tmp_path, monkeypatch):
    """PROJECT scope, never user scope. In a pod the HOME is shared, so an
    `?agent=` stamp in a user-scope file would push one agent's DMs into
    another's session — the 2026-07-27 misroute needed exactly a shared
    file."""
    rc, work, home = _run(tmp_path, monkeypatch,
                          {"agents": [{"identity": "a"}, {"identity": "b"}]})
    assert rc == 0
    for ident in ("a", "b"):
        url = json.loads((work / ident / ".mcp.json").read_text())
        assert url["mcpServers"]["hub"]["url"].endswith(f"?agent={ident}")
    assert not (home / ".mcp.json").exists(), "a shared stamp is the misroute"


def test_the_pod_writes_its_workspace_file(tmp_path, monkeypatch):
    """The point of the shape: one Dev Containers window as a squad view."""
    rc, work, home = _run(tmp_path, monkeypatch,
                          {"squad": "capsule",
                           "agents": [{"identity": "a"}, {"identity": "b"}]})
    assert rc == 0
    ws = json.loads((work / "capsule.code-workspace").read_text())
    assert [f["name"] for f in ws["folders"]] == ["a", "b"]
    for f in ws["folders"]:
        assert f["path"].startswith(str(work)), "container paths, not host"


def test_the_home_level_settings_are_written_ONCE_and_shared(tmp_path, monkeypatch):
    """Theme, onboarding and the bypass acceptance are HOME-level — three of
    the six gates, and a pod pays them once rather than N times."""
    rc, work, home = _run(tmp_path, monkeypatch,
                          {"agents": [{"identity": "a"}, {"identity": "b"}]})
    assert rc == 0
    settings = json.loads((home / ".claude" / "settings.json").read_text())
    assert settings["theme"] == "dark"
    assert settings["skipDangerousModePermissionPrompt"] is True
    state = json.loads((home / ".claude.json").read_text())
    assert state["hasCompletedOnboarding"] is True
    # Trust seeded per WORKDIR, not once for the container.
    assert set(state["projects"]) == {str(work / "a"), str(work / "b")}


def test_a_refused_manifest_never_touches_the_disk(tmp_path, monkeypatch):
    rc, work, home = _run(tmp_path, monkeypatch,
                          {"agents": [{"identity": "a"}, {"identity": "a"}]})
    from mcp_hub.seat import EXIT_CONTRACT
    assert rc == EXIT_CONTRACT
    assert not work.exists() or not any(work.iterdir())
    # The load-bearing half: the refusal happens BEFORE any container-level
    # write, so a bad manifest leaves no half-prepared HOME behind. Checking
    # only the workdir would pass even if home setup had run.
    assert not (home / ".claude" / "settings.json").exists()


def test_a_single_seat_still_lands_in_the_bare_workdir(tmp_path, monkeypatch):
    """1:1 is unchanged: no per-identity subfolder, no workspace file. A pod
    of one must not quietly restructure a container the fleet is running."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", TOKEN)
    monkeypatch.setenv("MCP_HUB_URL", HUB)
    monkeypatch.delenv("SEAT_MANIFEST", raising=False)
    monkeypatch.setenv("SEAT_IDENTITY", "solo")
    work = home / "work"
    rc = cli.seat_entry_command(argparse.Namespace(
        workdir=str(work), prepare_only=True))
    assert rc == 0
    assert (work / ".claude" / "hub-agent.json").exists()
    assert not (work / "solo").exists()
    assert not list(work.glob("*.code-workspace"))


# ---- phase 3: the edge places a pod ----------------------------------------

class _R:
    def __init__(self, rcs=None):
        self.calls = []
        self._rcs = list(rcs or [])

    def __call__(self, cmd, cwd=None):
        self.calls.append(cmd)
        rc = self._rcs.pop(0) if self._rcs else 0
        return rc, "out"


POD_SPEC = {"spec": {
    "image": "mcp-hub-seat:latest",
    "memory_volume": "pod-mem:/home/seat/.claude",
    "squad": "capsule",
    "agents": [{"identity": "a", "repo": "git@h:o/a.git"},
               {"identity": "b", "repo": "git@h:o/b.git"}],
}}


def _envs(argv):
    return [argv[i + 1] for i, x in enumerate(argv) if x == "-e"]


def test_a_pod_is_created_with_a_MANIFEST_and_no_identity():
    """A container carrying both is refused at its own door, so the launcher
    must send exactly one shape."""
    from mcp_hub.edge import DockerExecutor

    argv = DockerExecutor.create_argv("capsule-pod-box", POD_SPEC["spec"])
    envs = _envs(argv)
    assert not any(e.startswith("SEAT_IDENTITY=") for e in envs)
    manifest = [e for e in envs if e.startswith("SEAT_MANIFEST=")]
    assert len(manifest) == 1
    doc = json.loads(manifest[0].split("=", 1)[1])
    assert [a["identity"] for a in doc["agents"]] == ["a", "b"]
    assert doc["squad"] == "capsule"


def test_a_1to1_seat_still_gets_SEAT_IDENTITY_and_no_manifest():
    """Every container on the fleet today takes this branch."""
    from mcp_hub.edge import DockerExecutor

    envs = _envs(DockerExecutor.create_argv("solo-box", {"image": "img"}))
    assert "SEAT_IDENTITY=solo-box" in envs
    assert not any(e.startswith("SEAT_MANIFEST=") for e in envs)


def test_the_manifest_is_injected_AFTER_spec_env_so_it_wins():
    """Same rule as SEAT_IDENTITY: a stale spec must not be able to make a
    container report one membership to docker and another to the hub."""
    from mcp_hub.edge import DockerExecutor

    spec = dict(POD_SPEC["spec"], env={"SEAT_MANIFEST": "stale"})
    envs = [e for e in _envs(DockerExecutor.create_argv("p", spec))
            if e.startswith("SEAT_MANIFEST=")]
    assert len(envs) == 2 and envs[-1] != "SEAT_MANIFEST=stale"


def test_a_pod_harvests_once_per_agent_in_ITS_OWN_workdir():
    """`memory-export` resolves identity from cwd, so `-w` IS the choice of
    which agent is harvested."""
    from mcp_hub.edge import DockerExecutor

    r = _R()
    out = DockerExecutor(r, {}).execute({"op": "harvest", "seat": "p"}, POD_SPEC)
    assert out["rc"] == 0
    assert [c[3] for c in r.calls] == ["/home/seat/work/a", "/home/seat/work/b"]
    assert [a["identity"] for a in out["agents"]] == ["a", "b"]


def test_ONE_failed_export_fails_the_whole_harvest():
    """A pod where one agent's export failed has NOT been harvested. Reporting
    0 because the others succeeded would let reclaim destroy the container on
    the strength of a partial save — the exact loss this step prevents."""
    from mcp_hub.edge import DockerExecutor

    out = DockerExecutor(_R([0, 3]), {}).execute(
        {"op": "harvest", "seat": "p"}, POD_SPEC)
    assert out["rc"] == 3


def test_a_1to1_harvest_is_unchanged():
    from mcp_hub.edge import DockerExecutor

    r = _R()
    DockerExecutor(r, {}).execute(
        {"op": "harvest", "seat": "s"},
        {"spec": {"image": "i", "memory_volume": "m:/home/seat/.claude"}})
    assert r.calls[-1] == ["docker", "exec", "s", "mcp-hub", "memory-export"]


def test_a_pod_with_no_memory_volume_still_says_so_rather_than_looping_agents():
    from mcp_hub.edge import DockerExecutor

    r = _R()
    out = DockerExecutor(r, {}).execute(
        {"op": "harvest", "seat": "p"},
        {"spec": dict(POD_SPEC["spec"], memory_volume="")})
    assert out["skipped"] is True and r.calls == []


# ---- phase 4: the board shows a pod ----------------------------------------

def test_a_1to1_container_holds_exactly_itself():
    """The single-seat case is the N=1 case of one rule, not a branch."""
    from mcp_hub.fleet_tree import container_members, container_session

    s = {"identity": "solo-box", "spec": {"image": "i"}}
    assert container_members(s) == ["solo-box"]
    assert container_session(s, "solo-box") == "seat"


def test_a_pod_holds_its_manifest_in_order():
    from mcp_hub.fleet_tree import container_members, container_session

    s = {"identity": "capsule-pod-box", "spec": {
        "image": "i", "agents": [{"identity": "b"}, {"identity": "a"}]}}
    assert container_members(s) == ["b", "a"]
    assert container_session(s, "a") == "a"


def test_pod_inhabitants_hang_under_their_container():
    """Keyed on the MEMBERS, not the container name — for a pod those differ,
    and keying on the container would leave its agents homeless, matched by
    repo basename, which is the defect container nodes exist to end."""
    from mcp_hub.fleet_tree import build_tree, walk_agents

    t = build_tree(
        roster=[], board={"agents": {}},
        workspaces={"rows": [], "machines": ["box"], "this_machine": "box"},
        fleet={"ts": 1000.0, "agents": [{"name": "a"}, {"name": "b"}]},
        this_machine="box",
        seats=[{"identity": "capsule-pod-box", "machine": "box", "spec": {
            "image": "i", "agents": [{"identity": "a"}, {"identity": "b"}]}}],
        now=1000.0,
    )
    box = t["machines"][0]
    assert [c["identity"] for c in box["containers"]] == ["capsule-pod-box"]
    assert [a["agent"] for a in box["containers"][0]["agents"]] == ["a", "b"]
    assert not box["loose"], "an inhabitant must not fall out to the machine"
    assert len(list(walk_agents(t))) == 2


def test_a_pod_node_carries_no_agent_NAME():
    """A pod is not an agent — no marker, no registration, nothing on the hub
    by that name. A container's name masquerading as an agent's is how a row
    comes to claim a presence nobody has."""
    from mcp_hub.fleet_tree import build_tree

    t = build_tree(
        roster=[], board={"agents": {}},
        workspaces={"rows": [], "machines": ["box"], "this_machine": "box"},
        fleet={"ts": 1000.0, "agents": []}, this_machine="box",
        seats=[{"identity": "capsule-pod-box", "machine": "box", "spec": {
            "image": "i", "agents": [{"identity": "a"}]}}],
        now=1000.0,
    )
    assert t["machines"][0]["containers"][0]["agent"] == ""


def test_a_1to1_container_keeps_its_agent_name():
    from mcp_hub.fleet_tree import build_tree

    t = build_tree(
        roster=[], board={"agents": {}},
        workspaces={"rows": [], "machines": ["box"], "this_machine": "box"},
        fleet={"ts": 1000.0, "agents": []}, this_machine="box",
        seats=[{"identity": "solo-box", "machine": "box",
                "spec": {"image": "i"}}],
        now=1000.0,
    )
    assert t["machines"][0]["containers"][0]["agent"] == "solo-box"


def test_a_1to1_container_attaches_to_the_session_named_seat():
    """`seat` is what the image creates and what the launch dance answers
    into. Every container on the fleet uses this line."""
    from mcp_hub.fleet_tree import container_attach

    assert container_attach({"identity": "solo-box", "members": ["solo-box"]}) == [
        ("Attach", "docker exec -it solo-box tmux attach -t seat")]


def test_a_pod_attaches_PER_AGENT_by_session_name():
    """A single `-t seat` line would fail against a healthy pod, and an
    operator reading a failing attach concludes the pod is broken."""
    from mcp_hub.fleet_tree import container_attach

    assert container_attach(
        {"identity": "capsule-pod-box", "members": ["a", "b"]}) == [
        ("Attach a", "docker exec -it capsule-pod-box tmux attach -t a"),
        ("Attach b", "docker exec -it capsule-pod-box tmux attach -t b"),
    ]


def test_a_pod_of_ONE_still_names_its_session_for_the_agent():
    """N=1 is a pod, not a 1:1 seat — its entrypoint took the manifest branch
    and named the session for the agent, so the attach must match what was
    actually created rather than what the container count suggests."""
    from mcp_hub.fleet_tree import container_attach

    assert container_attach({"identity": "pod-box", "members": ["only"]}) == [
        ("Attach", "docker exec -it pod-box tmux attach -t only")]
