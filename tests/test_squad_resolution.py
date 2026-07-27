"""Client-side squad resolution — where an agent's membership comes from.

The model (operator, 2026-07-27): a `.code-workspace` file is typed. A SQUAD
workspace names a squad and its agents are members; a FACULTY workspace is an
assembly of unrelated agents gathered for convenience and confers nothing.

Faculty is therefore the ABSENCE of a squad, not a kind of one — which is why
only squad workspaces appear in the config and there is nothing to keep in
sync for faculty. An agent in three squad workspaces is in three squads: that
is where multi-membership comes from, with no per-agent list to maintain.
"""
from __future__ import annotations

import json
from pathlib import Path

from mcp_hub.cli import _resolve_squads, _workspace_folders


def _ws(path: Path, folders: list[str], raw: str | None = None) -> None:
    path.write_text(
        raw if raw is not None
        else json.dumps({"folders": [{"path": f} for f in folders]}),
        encoding="utf-8",
    )


def _config(tmp_path: Path, monkeypatch, table: dict) -> None:
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"squad_workspaces": table}), encoding="utf-8")
    monkeypatch.setattr("mcp_hub.cli._HUB_CONFIG_PATH", cfg)


def test_an_agent_in_a_squad_workspace_is_in_that_squad(tmp_path, monkeypatch):
    repo = tmp_path / "pm"
    repo.mkdir()
    ws = tmp_path / "dreamteam.code-workspace"
    _ws(ws, [str(repo)])
    _config(tmp_path, monkeypatch, {str(ws): "dreamteam"})
    assert _resolve_squads(str(repo)) == ["dreamteam"]


def test_an_agent_in_three_squad_workspaces_is_in_three_squads(tmp_path, monkeypatch):
    """Multi-membership with no per-agent bookkeeping — put the folder in the
    workspace and the membership follows."""
    repo = tmp_path / "shared"
    repo.mkdir()
    table = {}
    for squad in ("alpha", "beta", "gamma"):
        ws = tmp_path / f"{squad}.code-workspace"
        _ws(ws, [str(repo)])
        table[str(ws)] = squad
    _config(tmp_path, monkeypatch, table)
    assert _resolve_squads(str(repo)) == ["alpha", "beta", "gamma"]


def test_a_faculty_workspace_confers_nothing(tmp_path, monkeypatch):
    """A faculty workspace simply isn't listed. The agent sits in it and gets
    no squad from it — and, per the hub, therefore cannot broadcast at all."""
    repo = tmp_path / "scratch"
    repo.mkdir()
    faculty = tmp_path / "general.code-workspace"
    _ws(faculty, [str(repo)])
    _config(tmp_path, monkeypatch, {})          # nothing declared
    assert _resolve_squads(str(repo)) == []


def test_an_agent_in_both_a_squad_and_a_faculty_workspace_gets_only_the_squad(
    tmp_path, monkeypatch
):
    """The operator's case: "members of the squad might also be members of the
    isolated agent workspace"."""
    repo = tmp_path / "pm"
    repo.mkdir()
    squad_ws = tmp_path / "dreamteam.code-workspace"
    faculty_ws = tmp_path / "general.code-workspace"
    _ws(squad_ws, [str(repo)])
    _ws(faculty_ws, [str(repo)])
    _config(tmp_path, monkeypatch, {str(squad_ws): "dreamteam"})
    assert _resolve_squads(str(repo)) == ["dreamteam"]


def test_an_agent_not_in_the_workspace_gets_nothing(tmp_path, monkeypatch):
    repo = tmp_path / "pm"
    other = tmp_path / "unrelated"
    repo.mkdir()
    other.mkdir()
    ws = tmp_path / "dreamteam.code-workspace"
    _ws(ws, [str(repo)])
    _config(tmp_path, monkeypatch, {str(ws): "dreamteam"})
    assert _resolve_squads(str(other)) == []


def test_relative_folder_paths_resolve_against_the_workspace_file(tmp_path, monkeypatch):
    """VSCode resolves relative folder paths against the workspace file's own
    directory, and real workspace files use them constantly."""
    proj = tmp_path / "Projects"
    repo = proj / "code" / "mcp-hub"
    repo.mkdir(parents=True)
    ws = proj / "hub.code-workspace"
    _ws(ws, ["code/mcp-hub"])
    _config(tmp_path, monkeypatch, {str(ws): "hublane"})
    assert _resolve_squads(str(repo)) == ["hublane"]


def test_a_hand_formatted_jsonc_workspace_still_parses(tmp_path, monkeypatch):
    """Real .code-workspace files carry comments and trailing commas — strict
    json.loads fails on them. Transport learned the neighbouring lesson (never
    load-and-dump one, it destroys the formatting); here we only need to READ,
    so comments are stripped in memory and the file is never rewritten."""
    repo = tmp_path / "pm"
    repo.mkdir()
    ws = tmp_path / "dreamteam.code-workspace"
    _ws(ws, [], raw=(
        "{\n"
        "  // the squad's own workspace — do not add scratch repos here\n"
        '  "folders": [\n'
        f'    {{ "path": "{repo}" }},   /* pm lives here */\n'
        "  ],\n"
        '  "settings": { "terminal.integrated.tabs.title": "${sequence}" },\n'
        "}\n"
    ))
    _config(tmp_path, monkeypatch, {str(ws): "dreamteam"})
    assert _resolve_squads(str(repo)) == ["dreamteam"]


def test_a_url_in_a_comment_does_not_break_the_stripper(tmp_path, monkeypatch):
    """`//` inside a STRING is not a comment. A naive stripper eats the rest of
    the line and the folder list vanishes — silently, since the failure mode is
    an empty list, i.e. "no squads" rather than an error."""
    repo = tmp_path / "pm"
    repo.mkdir()
    ws = tmp_path / "dreamteam.code-workspace"
    _ws(ws, [], raw=json.dumps({
        "folders": [{"path": str(repo)}],
        "settings": {"docs": "https://example.com/guide"},
    }))
    _config(tmp_path, monkeypatch, {str(ws): "dreamteam"})
    assert _resolve_squads(str(repo)) == ["dreamteam"]


def test_a_missing_or_broken_workspace_file_is_skipped_not_fatal(tmp_path, monkeypatch):
    """Losing one squad is a smaller failure than refusing to register at all,
    so a deleted or corrupt workspace is skipped and the others still resolve."""
    repo = tmp_path / "pm"
    repo.mkdir()
    good = tmp_path / "good.code-workspace"
    broken = tmp_path / "broken.code-workspace"
    _ws(good, [str(repo)])
    broken.write_text("{ this is not json at all", encoding="utf-8")
    _config(tmp_path, monkeypatch, {
        str(good): "alpha",
        str(broken): "beta",
        str(tmp_path / "gone.code-workspace"): "gamma",
    })
    assert _resolve_squads(str(repo)) == ["alpha"]


def test_no_config_at_all_means_no_squads(tmp_path, monkeypatch):
    monkeypatch.setattr("mcp_hub.cli._HUB_CONFIG_PATH", tmp_path / "absent.json")
    assert _resolve_squads(str(tmp_path)) == []


def test_workspace_folders_reads_the_folder_list(tmp_path):
    ws = tmp_path / "x.code-workspace"
    _ws(ws, ["/a", "/b"])
    assert _workspace_folders(str(ws)) == ["/a", "/b"]
