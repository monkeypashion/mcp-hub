"""`mcp-hub identity` — the ONE place anything asks an agent's name.

CLAUDE.md tells every tool to ask here rather than re-derive, so the two
failures below are worse than they look: a container is identified ONLY by
its marker, and a command that fails silently is indistinguishable from a
command that does not exist.

Both were found by the fleet's first containerized seat, which ran
`mcp-hub identity --cwd .`, got exit 1 and no output, and reported back
that the CLI "has no identity subcommand at all". It does — this is what
it actually did.
"""

from __future__ import annotations

import argparse
import json

from mcp_hub import cli


def _args(**kw):
    base = {"cwd": None, "json": False, "any": False}
    base.update(kw)
    return argparse.Namespace(**base)


def _marker(tmp_path, name="seat-1", project="org/repo"):
    d = tmp_path / ".claude"
    d.mkdir(parents=True, exist_ok=True)
    (d / "hub-agent.json").write_text(
        json.dumps({"name": name, "project": project}), encoding="utf-8"
    )


def test_the_marker_identifies_a_worktree_derivation_cannot(
    tmp_path, monkeypatch, capsys
):
    """A container has no git remote and is deliberately NOT opted in, so
    derivation returns nothing and the ASSIGNED marker is the only truth.
    The hooks have always honoured it (_resolve_agent_identity); identity
    did not, so the one command everything is told to ask gave the wrong
    answer in every container."""
    monkeypatch.setattr(cli, "_derive_agent_identity", lambda cwd: (None, None))
    _marker(tmp_path, name="claude-seat-dev-vm-1", project="proj")

    rc = cli.identity_command(_args(cwd=str(tmp_path)))
    assert rc == 0
    assert capsys.readouterr().out.strip() == "claude-seat-dev-vm-1"


def test_derived_identity_still_outranks_the_marker(tmp_path, monkeypatch, capsys):
    """Unchanged precedence: a stale committed marker must never drag a
    migrated machine back to a shared identity."""
    monkeypatch.setattr(
        cli, "_derive_agent_identity", lambda cwd: ("derived-name", "org/x")
    )
    _marker(tmp_path, name="stale-marker-name")

    assert cli.identity_command(_args(cwd=str(tmp_path))) == 0
    assert capsys.readouterr().out.strip() == "derived-name"


def test_the_marker_project_reaches_json_output(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_derive_agent_identity", lambda cwd: (None, None))
    _marker(tmp_path, name="seat-9", project="org/thing")

    assert cli.identity_command(_args(cwd=str(tmp_path), json=True)) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["name"] == "seat-9"
    assert out["project"] == "org/thing"


def test_no_identity_says_so_instead_of_failing_mutely(
    tmp_path, monkeypatch, capsys
):
    """The silent exit 1 is what made a working command look missing. Exit
    code stays 1 (callers branch on it) and stdout stays EMPTY (callers
    capture it as the name) — the explanation goes to stderr, where the one
    consumer in squad already discards it."""
    monkeypatch.setattr(cli, "_derive_agent_identity", lambda cwd: (None, None))

    rc = cli.identity_command(_args(cwd=str(tmp_path)))
    assert rc == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "no identity" in captured.err.lower()
    # Names what to DO about it, not merely that it failed.
    assert "hub-agent.json" in captured.err or "config.json" in captured.err
