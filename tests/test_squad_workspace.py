"""Workspace-scoped squad operations.

The workspace — a `.code-workspace` file — is the unit the operator works in, and
folder membership is already how the cockpit decides which tabs to show. These
verbs reuse that one rule, which is what makes "squad workspace" vs "general
workspace" irrelevant: you act on a workspace, whatever it contains.

`transport all` was MACHINE-scoped and wrong for both real cases (standing up a
second squad for a side project; retiring a box by migrating one workspace) —
it would drag in every unrelated faculty agent on the machine.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess

import pytest

SQUAD = pathlib.Path(__file__).resolve().parents[1] / "squad" / "squad"
pytestmark = pytest.mark.skipif(not SQUAD.exists(), reason="squad script not present")


def _run(home, conf, *args, cwd=None):
    env = dict(os.environ, HOME=str(home), SQUAD_CONF=str(conf))
    return subprocess.run(["bash", str(SQUAD), *args], capture_output=True,
                          text=True, timeout=90, env=env, cwd=cwd)


@pytest.fixture
def rig(tmp_path):
    home = tmp_path / "home"
    (home / ".config" / "squad").mkdir(parents=True)
    conf = home / ".config" / "squad" / "squad.conf"
    # two agents in the workspace, one deliberately outside it
    for name in ("in-a", "in-b", "outside"):
        (tmp_path / name).mkdir()
    conf.write_text(
        f"in-a-h|{tmp_path/'in-a'}|||faculty\n"
        f"in-b-h|{tmp_path/'in-b'}|||faculty\n"
        f"outside-h|{tmp_path/'outside'}|||faculty\n", encoding="utf-8")
    ws = tmp_path / "t.code-workspace"
    # JSONC comment + RELATIVE paths: dev-vm-1's general workspace does both
    # deliberately, so the file moves with the tree.
    ws.write_text(
        '{\n  // General workspace — comms off for these.\n'
        '  "folders": [\n'
        '    { "name": "in-a", "path": "in-a" },\n'
        '    { "name": "in-b", "path": "in-b" }\n'
        '  ],\n  "settings": { "squad.comms": false }\n}\n', encoding="utf-8")
    return home, conf, ws, tmp_path


def test_workspace_scope_excludes_agents_outside_it(rig):
    home, conf, ws, tmp = rig
    res = _run(home, conf, "transport", "workspace", str(ws),
               "--to", str(tmp / "dest.code-workspace"), "--dry-run")
    assert "2 agent(s)" in res.stdout, res.stdout + res.stderr
    assert "in-a-h" in res.stdout and "in-b-h" in res.stdout
    assert "outside-h" not in res.stdout, "machine scope leaked into workspace scope"


def test_machine_scope_still_includes_everything(rig):
    home, conf, ws, tmp = rig
    res = _run(home, conf, "transport", "all",
               "--to", str(tmp / "dest.code-workspace"), "--dry-run")
    assert "outside-h" in res.stdout, "`all` must remain machine-scoped"


def test_ws_remove_drops_the_folder_and_keeps_enrolment(rig):
    home, conf, ws, tmp = rig
    res = _run(home, conf, "ws-remove", "in-a-h", "--from", str(ws))
    assert res.returncode == 0, res.stdout + res.stderr
    text = ws.read_text()
    assert '"in-a"' not in text, "folder entry should be gone"
    assert '"in-b"' in text, "the other folder must survive"
    assert "General workspace" in text, "comments must survive a surgical edit"
    json.loads("\n".join(
        line for line in text.splitlines() if not line.strip().startswith("//")))
    # enrolment untouched — that's `rm`'s job, not this verb's
    assert "in-a-h|" in conf.read_text()


def test_rm_retires_the_agent_everywhere(rig):
    home, conf, ws, tmp = rig
    assert _run(home, conf, "rm", "in-a-h").returncode == 0
    assert "in-a-h|" not in conf.read_text()


def test_removing_a_folder_the_workspace_never_had_is_a_noop(rig):
    home, conf, ws, tmp = rig
    before = ws.read_text()
    res = _run(home, conf, "ws-remove", "outside-h", "--from", str(ws))
    assert res.returncode == 0
    assert "did not list that folder" in res.stdout
    assert ws.read_text() == before
