"""Making a placed capsule OPENABLE.

`compose` freezes a squad, `place` writes desired state, the edge makes
containers exist — and none of that gives the operator a way in. The capsule's
own `bootstrap.sh` was meant to close that gap and is four lines of comment
with no logic, so standing a squad up meant `squad add-container` by hand, once
per seat, plus a hand-edited workspace file.

The policy is pure, so it is tested without a machine.
"""
from __future__ import annotations

import argparse

from mcp_hub import cli
from mcp_hub.seat import (
    ATTACH_ENROL,
    ATTACH_PRESENT,
    ATTACH_REFUSE,
    ATTACH_SKIP,
    capsule_attach_plan,
)


def _seat(ident, machine="box", image="mcp-hub-seat:latest", mount="/w/" ):
    spec = {}
    if image:
        spec["image"] = image
        spec["volumes"] = [f"{mount}{ident}:/home/seat/work",
                           f"mem-{ident}:/home/seat/.claude"]
    return {"identity": ident, "machine": machine, "spec": spec}


def _actions(plan):
    return {i: (a, d) for i, a, d in plan}


# ---- the policy ------------------------------------------------------------

def test_a_container_seat_on_this_machine_is_enrolled():
    plan = _actions(capsule_attach_plan([_seat("a")], "box", set(), lambda p: True))
    assert plan["a"] == (ATTACH_ENROL, "/w/a")


def test_a_seat_already_in_the_roster_is_left_alone():
    """Attach has to be re-runnable: a capsule is placed, an edge pass adds a
    seat, and the operator attaches again."""
    plan = _actions(capsule_attach_plan([_seat("a")], "box", {"a"}, lambda p: True))
    assert plan["a"][0] == ATTACH_PRESENT


def test_a_seat_on_ANOTHER_machine_is_skipped():
    """Folders and the roster are per-machine facts. Enrolling a remote seat
    here would add a row pointing at a folder this box does not have."""
    plan = _actions(capsule_attach_plan([_seat("a", machine="elsewhere")],
                                        "box", set(), lambda p: True))
    assert plan["a"][0] == ATTACH_SKIP


def test_a_worktree_seat_is_skipped_not_refused():
    """A seat with no image is an ordinary agent — it already has a tab."""
    plan = _actions(capsule_attach_plan([_seat("a", image="")], "box", set(),
                                        lambda p: True))
    assert plan["a"][0] == ATTACH_SKIP


def test_a_MISSING_work_folder_is_refused_never_created():
    """Docker creates a missing bind-mount source as ROOT, and the seat runs as
    uid 1000 — so the container comes up unable to write its own worktree. That
    was the first of the six gates between "container running" and "agent on
    hub", and `docker ps` shows nothing wrong."""
    plan = _actions(capsule_attach_plan([_seat("a")], "box", set(),
                                        lambda p: False))
    assert plan["a"][0] == ATTACH_REFUSE
    assert "/w/a" in plan["a"][1]


def test_a_container_seat_with_no_bind_mount_is_refused():
    s = {"identity": "a", "machine": "box",
         "spec": {"image": "i", "volumes": ["mem-a:/home/seat/.claude"]}}
    plan = _actions(capsule_attach_plan([s], "box", set(), lambda p: True))
    assert plan["a"][0] == ATTACH_REFUSE


def test_a_seat_with_no_machine_is_taken_as_ours():
    """The manifest records what the seat said. A seat that names no machine
    cannot be excluded on that basis without inventing the fact."""
    s = {"identity": "a", "spec": {"image": "i", "volumes": ["/w/a:/x"]}}
    plan = _actions(capsule_attach_plan([s], "box", set(), lambda p: True))
    assert plan["a"][0] == ATTACH_ENROL


# ---- the command -----------------------------------------------------------

class _Api:
    def __init__(self, capsules):
        self._c = capsules

    def list_capsules(self):
        return self._c


def _args(**kw):
    base = dict(action="attach", target="cap-1", workspace=None,
                dry_run=False, squad=None, machine=None, json=False,
                register=False, hub_url="http://x/mcp")
    base.update(kw)
    return argparse.Namespace(**base)


def _capsule(seats):
    return [{"id": "cap-1", "squad": "s", "manifest": {"seats": seats}}]


def test_attach_refuses_the_WHOLE_capsule_when_a_folder_is_missing(
        tmp_path, monkeypatch, capsys):
    """All-or-nothing: half a squad wired up is worse than none, because the
    half that is missing looks like a container fault."""
    ran = []
    monkeypatch.setattr(cli.subprocess, "run",
                        lambda *a, **k: ran.append(a) or (_ for _ in ()).throw(
                            AssertionError("wrote something")))
    monkeypatch.setattr(cli, "_roster_all", lambda: [])
    monkeypatch.setattr(cli.platform, "node", lambda: "box")
    monkeypatch.setattr(cli.os.path, "isdir", lambda p: False)
    rc = cli.capsules_command(_args(), api=_Api(_capsule([_seat("a")])))
    assert rc == 1
    assert ran == []
    assert "REFUSE" in capsys.readouterr().out


def test_attach_dry_run_writes_nothing(tmp_path, monkeypatch, capsys):
    ran = []
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: ran.append(a))
    monkeypatch.setattr(cli, "_roster_all", lambda: [])
    monkeypatch.setattr(cli.platform, "node", lambda: "box")
    monkeypatch.setattr(cli.os.path, "isdir", lambda p: True)
    rc = cli.capsules_command(_args(dry_run=True),
                              api=_Api(_capsule([_seat("a")])))
    assert rc == 0 and ran == []
    assert "dry run" in capsys.readouterr().out


def test_attach_calls_squad_rather_than_writing_the_roster_itself(
        tmp_path, monkeypatch, capsys):
    """`squad` is the single writer of squad.conf and of workspace files. A
    second writer of a hand-formatted JSONC file destroys its comments, and a
    second writer of the roster is how two views of the fleet disagree."""
    calls = []

    class R:
        stdout, stderr = "enrolled", ""

    monkeypatch.setattr(cli.subprocess, "run",
                        lambda argv, **k: calls.append(argv) or R())
    monkeypatch.setattr(cli, "_roster_all", lambda: [])
    monkeypatch.setattr(cli.platform, "node", lambda: "box")
    monkeypatch.setattr(cli.os.path, "isdir", lambda p: True)
    ws = tmp_path / "cap.code-workspace"
    rc = cli.capsules_command(_args(workspace=str(ws)),
                              api=_Api(_capsule([_seat("a"), _seat("b")])))
    assert rc == 0
    verbs = [c[1] for c in calls]
    assert "ws-new" in verbs, "an absent workspace must be created, not assumed"
    assert verbs.count("add-container") == 2
    for c in calls:
        if c[1] == "add-container":
            # name, folder, CONTAINER — the container name IS the identity,
            # which is the edge's own naming rule (`docker create --name seat`).
            assert c[2] == c[4], c


def test_attach_is_idempotent_over_an_already_enrolled_squad(
        tmp_path, monkeypatch, capsys):
    ran = []
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: ran.append(a))
    monkeypatch.setattr(cli, "_roster_all",
                        lambda: [{"agent": "a"}, {"agent": "b"}])
    monkeypatch.setattr(cli.platform, "node", lambda: "box")
    monkeypatch.setattr(cli.os.path, "isdir", lambda p: True)
    rc = cli.capsules_command(_args(), api=_Api(_capsule([_seat("a"), _seat("b")])))
    assert rc == 0 and ran == []
    assert "nothing to do" in capsys.readouterr().out


def test_an_unknown_capsule_is_named_not_guessed(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_roster_all", lambda: [])
    rc = cli.capsules_command(_args(target="cap-nope"), api=_Api(_capsule([])))
    assert rc == 1
    assert "no capsule cap-nope" in capsys.readouterr().err


# ---- the structured plan the cockpit reads ---------------------------------
#
# The extension must never parse the rendered lines: a UI reading a human
# format is how the board came to attribute agents by repo basename, and every
# later wording change would silently become a behaviour change.

class _Done:
    """What subprocess.run returns for a successful `squad add-container`."""
    stdout = "enrolled"
    stderr = ""
    returncode = 0


def _plan_json(capsys):
    import json as _json
    return _json.loads(capsys.readouterr().out)


def test_json_plan_names_each_seat_and_its_folder(monkeypatch, capsys):
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_roster_all", lambda: [])
    monkeypatch.setattr(cli.platform, "node", lambda: "box")
    monkeypatch.setattr(cli.os.path, "isdir", lambda p: True)
    rc = cli.capsules_command(_args(dry_run=True, json=True),
                              api=_Api(_capsule([_seat("a"), _seat("b")])))
    out = _plan_json(capsys)
    assert rc == 0
    assert [e["identity"] for e in out["enrol"]] == ["a", "b"]
    assert out["enrol"][0]["folder"] == "/w/a"      # the cockpit adds THIS
    assert out["dry_run"] is True and out["machine"] == "box"


def test_json_dry_run_still_writes_nothing(monkeypatch, capsys):
    """The RENDERING must not decide whether the guard runs."""
    ran = []
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: ran.append(a))
    monkeypatch.setattr(cli, "_roster_all", lambda: [])
    monkeypatch.setattr(cli.platform, "node", lambda: "box")
    monkeypatch.setattr(cli.os.path, "isdir", lambda p: True)
    rc = cli.capsules_command(_args(dry_run=True, json=True),
                              api=_Api(_capsule([_seat("a")])))
    assert rc == 0 and ran == []


def test_json_refusal_is_a_NAMED_list_not_an_empty_enrol(monkeypatch, capsys):
    """`nothing to enrol` and `refused, so nothing was written` are opposite
    outcomes that both leave `enrol` empty — the cockpit must tell them apart
    or it reports a blocked capsule as an already-done one."""
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_roster_all", lambda: [])
    monkeypatch.setattr(cli.platform, "node", lambda: "box")
    monkeypatch.setattr(cli.os.path, "isdir", lambda p: False)
    rc = cli.capsules_command(_args(json=True),
                              api=_Api(_capsule([_seat("a")])))
    out = _plan_json(capsys)
    assert rc == 1
    assert out["enrol"] == []
    assert [r["identity"] for r in out["refused"]] == ["a"]


def test_json_run_still_enrols_through_squad(monkeypatch, capsys):
    """One execution path, two renderings — the json branch must not be a
    second implementation free to drift from the guarded one."""
    calls = []
    monkeypatch.setattr(
        cli.subprocess, "run",
        lambda argv, *a, **k: calls.append(argv) or _Done())
    monkeypatch.setattr(cli, "_roster_all", lambda: [])
    monkeypatch.setattr(cli.platform, "node", lambda: "box")
    monkeypatch.setattr(cli.os.path, "isdir", lambda p: True)
    rc = cli.capsules_command(_args(json=True),
                              api=_Api(_capsule([_seat("a")])))
    assert rc == 0
    assert any("add-container" in c for c in calls)


def test_json_output_is_ONLY_json(monkeypatch, capsys):
    """A stray human line makes JSON.parse throw in the cockpit."""
    import json as _json
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: _Done())
    monkeypatch.setattr(cli, "_roster_all", lambda: [])
    monkeypatch.setattr(cli.platform, "node", lambda: "box")
    monkeypatch.setattr(cli.os.path, "isdir", lambda p: True)
    cli.capsules_command(_args(json=True), api=_Api(_capsule([_seat("a")])))
    _json.loads(capsys.readouterr().out)     # throws if anything else printed

