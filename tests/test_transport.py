"""Transport: identity override + conversation-history re-key.

The re-key tests are written against the failure that actually happened:
`file-history-snapshot.snapshot.trackedFileBackups` was missed on the first
pass because the FIRST snapshot line in a real transcript has an empty
trackedFileBackups — sampling one line "proved" the type carried no paths, and
the resulting clone held 536 live pointers into the source agent's memory dir.
Hence a test for the completeness guard specifically: it is the only check
that can catch a coupling nobody thought of.
"""
from __future__ import annotations

import json

import pytest

from mcp_hub import cli

OLD = "/mnt/d/Projects/code/monkeypashion/mcp-hub"
NEW = "/home/monke/Projects/xport/mcp-hub"
OLD_ENC = "-mnt-d-Projects-code-monkeypashion-mcp-hub"
NEW_ENC = "-home-monke-Projects-xport-mcp-hub"

JS = {"separators": (",", ":"), "ensure_ascii": False}


def js(obj) -> str:
    """Serialize the way Claude Code's JS writer does."""
    return json.dumps(obj, **JS)


# ---------------------------------------------------------------- identity


@pytest.fixture
def hub_config(tmp_path, monkeypatch):
    """Point the cli at a throwaway ~/.mcp-hub/config.json."""
    cfg = tmp_path / "config.json"
    monkeypatch.setattr(cli, "_HUB_CONFIG_PATH", cfg)
    return cfg


def test_no_suffix_configured_leaves_derivation_untouched(hub_config, tmp_path):
    hub_config.write_text(json.dumps({"projects": ["org/repo"]}))
    assert cli._workspace_suffix(str(tmp_path)) is None


def test_suffix_matches_by_path(hub_config, tmp_path):
    work = tmp_path / "xport" / "mcp-hub"
    work.mkdir(parents=True)
    hub_config.write_text(json.dumps({"workspaces": {str(work): "xport"}}))
    assert cli._workspace_suffix(str(work)) == "xport"
    # a DIFFERENT worktree of the same repo keeps the plain derivation —
    # otherwise adding one clone would rename the whole existing fleet
    other = tmp_path / "code" / "mcp-hub"
    other.mkdir(parents=True)
    assert cli._workspace_suffix(str(other)) is None


def test_suffix_tolerates_trailing_separator_and_relative_form(hub_config, tmp_path):
    work = tmp_path / "xport" / "mcp-hub"
    work.mkdir(parents=True)
    hub_config.write_text(json.dumps({"workspaces": {str(work) + "/": "xport"}}))
    assert cli._workspace_suffix(str(work)) == "xport"


def test_malformed_workspaces_table_is_ignored(hub_config, tmp_path):
    # Fail-open: a hand-edited config must never break identity derivation,
    # because that would unbind the agent from the hub entirely.
    for bad in ('{"workspaces": []}', '{"workspaces": {"/x": 5}}', "not json"):
        hub_config.write_text(bad)
        assert cli._workspace_suffix(str(tmp_path)) is None


# ---------------------------------------------------------------- re-key


def test_rewrites_cwd_and_leaves_message_content_alone():
    line = js({
        "type": "user",
        "cwd": OLD,
        "message": {"content": [{"type": "text", "text": f"I edited {OLD}/squad/squad"}]},
    })
    out, stats = cli._rekey_transcript(line, OLD, NEW)
    rec = json.loads(out)
    assert rec["cwd"] == NEW
    assert stats["cwd"] == 1
    # The transcript is a RECORD: the operator really did say the old path.
    assert OLD in rec["message"]["content"][0]["text"]
    assert stats["content_touched"] == 0
    assert stats["completeness_violations"] == 0


def test_rewrites_file_history_delta_fields():
    line = js({
        "type": "file-history-delta",
        "trackingPath": f"/home/me/.claude/projects/{OLD_ENC}/memory/MEMORY.md",
        "backup": {"realParentDir": f"/home/me/.claude/projects/{OLD_ENC}/memory"},
    })
    out, stats = cli._rekey_transcript(line, OLD, NEW)
    rec = json.loads(out)
    assert NEW_ENC in rec["trackingPath"] and OLD_ENC not in rec["trackingPath"]
    assert NEW_ENC in rec["backup"]["realParentDir"]
    assert stats["tracking"] == 1 and stats["realparent"] == 1


def test_rewrites_snapshot_backups_including_dict_keys():
    """The coupling that was missed: trackedFileBackups is KEYED by path."""
    key = f"/home/me/.claude/projects/{OLD_ENC}/memory/MEMORY.md"
    line = js({
        "type": "file-history-snapshot",
        "snapshot": {
            "trackedFileBackups": {
                key: {"realParentDir": f"/home/me/.claude/projects/{OLD_ENC}/memory"}
            }
        },
    })
    out, stats = cli._rekey_transcript(line, OLD, NEW)
    tb = json.loads(out)["snapshot"]["trackedFileBackups"]
    assert stats["snapshot"] == 1
    assert all(OLD_ENC not in k for k in tb), "dict KEYS must be re-keyed, not just values"
    assert all(NEW_ENC in k for k in tb)
    assert all(NEW_ENC in v["realParentDir"] for v in tb.values())
    assert stats["completeness_violations"] == 0


def test_empty_snapshot_does_not_hide_later_populated_ones():
    """Guards against the exact sampling error that caused the miss."""
    key = f"/home/me/.claude/projects/{OLD_ENC}/x.md"
    text = "\n".join([
        js({"type": "file-history-snapshot", "snapshot": {"trackedFileBackups": {}}}),
        js({"type": "file-history-snapshot",
            "snapshot": {"trackedFileBackups": {key: {"realParentDir": "/tmp"}}}}),
    ])
    out, stats = cli._rekey_transcript(text, OLD, NEW)
    assert stats["snapshot"] == 1
    assert OLD_ENC not in out


def test_completeness_guard_catches_an_unknown_structural_field():
    """The only guard that can catch a coupling we never thought of.

    Faithfulness is checked against our own field list, so it is blind here by
    construction. Completeness asserts every surviving reference sits in a
    CONTENT field, so a new structural key trips it.
    """
    line = js({"type": "user", "cwd": OLD, "someFutureLiveField": f"{OLD}/x"})
    _out, stats = cli._rekey_transcript(line, OLD, NEW)
    assert stats["cwd"] == 1
    assert stats["completeness_violations"] >= 1


def test_serializer_matches_js_byte_for_byte():
    """Python's default writer would reformat every line in the file."""
    line = js({"type": "user", "cwd": OLD, "note": "emoji ⚡ and 🧠 stay literal"})
    out, stats = cli._rekey_transcript(line, OLD, NEW)
    assert stats["roundtrip_mismatch"] == 0
    assert "⚡" in out and "\\u26a1" not in out
    assert '"type":"user"' in out          # compact separators, as JS writes


def test_unparseable_line_is_preserved_not_dropped():
    text = "\n".join([js({"type": "user", "cwd": OLD}), "{ not json at all"])
    out, stats = cli._rekey_transcript(text, OLD, NEW)
    assert stats["unparseable"] == 1
    assert "{ not json at all" in out


# ------------------------------------------------- command-level behaviour


def _run_transport_history(tmp_path, monkeypatch, lines, dry_run=False):
    monkeypatch.setattr(cli.pathlib.Path, "home", classmethod(lambda cls: tmp_path))
    src_dir = tmp_path / ".claude" / "projects" / cli._claude_project_dirname(OLD)
    src_dir.mkdir(parents=True)
    (src_dir / "sess.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    args = cli.argparse.Namespace(from_cwd=OLD, to_cwd=NEW, dry_run=dry_run)
    rc = cli.transport_history_command(args)
    dst = tmp_path / ".claude" / "projects" / cli._claude_project_dirname(NEW) / "sess.jsonl"
    return rc, dst


def test_transport_history_writes_rekeyed_transcript(tmp_path, monkeypatch):
    rc, dst = _run_transport_history(
        tmp_path, monkeypatch, [js({"type": "user", "cwd": OLD})]
    )
    assert rc == 0
    assert json.loads(dst.read_text())["cwd"] == NEW


def test_transport_history_refuses_to_write_on_structural_leak(tmp_path, monkeypatch):
    """A missed coupling must NOT reach disk.

    Writing anyway would hand the clone live pointers into the source agent's
    state — on one machine those paths resolve, so a rewind in the clone could
    write into the original's memory.
    """
    rc, dst = _run_transport_history(
        tmp_path, monkeypatch,
        [js({"type": "user", "cwd": OLD, "someFutureLiveField": f"{OLD}/x"})],
    )
    assert rc == 1
    assert not dst.exists(), "refused transcripts must not be written"


def test_transport_history_dry_run_writes_nothing(tmp_path, monkeypatch):
    rc, dst = _run_transport_history(
        tmp_path, monkeypatch, [js({"type": "user", "cwd": OLD})], dry_run=True
    )
    assert rc == 0
    assert not dst.exists()
