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


def test_neutralises_file_history_delta_rather_than_repointing_it():
    """A delta names a backup of a file on the SOURCE machine.

    Those backups do not travel, so re-keying them to the destination produced
    dangling pointers — and, when the old path happened to exist on the
    destination, pointers into a tree we do not own. Measured on the first real
    cross-machine transport: ~1,600 such pointers aimed at the RECEIVING agent's
    live worktree, where a rewind would have written. Blank them instead.
    """
    line = js({
        "type": "file-history-delta",
        "trackingPath": f"/home/me/.claude/projects/{OLD_ENC}/memory/MEMORY.md",
        "backup": {"realParentDir": f"/home/me/.claude/projects/{OLD_ENC}/memory"},
    })
    out, stats = cli._rekey_transcript(line, OLD, NEW)
    rec = json.loads(out)
    assert rec["trackingPath"] == "", "must not point anywhere, not even at the new path"
    assert rec["backup"]["realParentDir"] == ""
    assert stats["tracking"] == 1 and stats["realparent"] == 1


def test_drops_snapshot_backups_entirely():
    """trackedFileBackups is keyed by absolute path — and must not survive.

    Originally this asserted the keys were RE-KEYED. That was wrong for the same
    reason as the delta above: the backups do not travel. Note an emptied dict
    would satisfy an `all(...)` assertion vacuously, which is how the weaker
    version of this test kept passing after the behaviour changed — assert the
    dict is EMPTY, not that every remaining key looks right.
    """
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
    assert tb == {}, f"the journal must be dropped, not repointed; got {tb}"
    assert stats["snapshot"] == 1
    assert stats["completeness_violations"] == 0


def test_empty_snapshot_does_not_hide_later_populated_ones():
    """Guards against the sampling error that hid the coupling originally."""
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


THIRD = "/home/monke/Projects/code/monkeypashion/mcp-hub"   # neither source nor dest


def test_a_third_path_in_cwd_is_repointed_at_the_destination(tmp_path, monkeypatch):
    """The gap the old guard could not see, measured on a real transcript.

    Rewriting only the SOURCE path left any OTHER path untouched. This seat's
    recorded cwd is one tree while it works in another, so 109 records in a real
    transported transcript kept naming a different real clone of the same repo.
    A transcript is per-project, so every cwd in it named that project somewhere;
    the clone's copy should say the clone.
    """
    rc, dst = _run_transport_history(
        tmp_path, monkeypatch,
        [js({"type": "user", "cwd": OLD}), js({"type": "user", "cwd": THIRD})],
    )
    assert rc == 0
    cwds = [json.loads(ln)["cwd"] for ln in dst.read_text().splitlines()]
    assert cwds == [NEW, NEW], cwds


def test_an_unclassified_path_field_is_refused_by_class_not_by_string(tmp_path, monkeypatch):
    """The point of classifying FIELDS: a path that is neither source nor
    destination still trips the guard, so the next coupling nobody predicted does
    not need a fourth special case."""
    rc, dst = _run_transport_history(
        tmp_path, monkeypatch,
        [js({"type": "user", "cwd": OLD, "somethingNobodyClassified": f"{THIRD}/x"})],
    )
    assert rc == 1, "an unclassified structural path must refuse"
    assert not dst.exists()


def test_environment_paths_do_not_block_a_transport(tmp_path, monkeypatch):
    """hookInfos names binaries on whichever box ran them — configuration, not
    location. One is always present, so refusing on it would block everything."""
    rc, dst = _run_transport_history(
        tmp_path, monkeypatch,
        [js({"type": "user", "cwd": OLD,
             "hookInfos": [{"command": "/home/monke/.venv/bin/mcp-hub stop-hook"}]})],
    )
    assert rc == 0, "an environment path must not refuse"
    assert json.loads(dst.read_text())["hookInfos"][0]["command"].endswith("stop-hook"), \
        "and it must be left byte-exact, not re-keyed"


def test_a_slash_command_in_prose_is_not_mistaken_for_a_path(tmp_path, monkeypatch):
    """"/compact" is indistinguishable from an absolute path by SHAPE.

    Found by running the new guard against a real transcript: it refused on nine
    `lastPrompt` values, all of them slash commands. Only classifying the field
    can tell text that starts with a slash from a pointer at a directory.
    """
    rc, dst = _run_transport_history(
        tmp_path, monkeypatch,
        [js({"type": "user", "cwd": OLD, "lastPrompt": "/compact"}),
         js({"type": "summary", "cwd": OLD, "content": "/status then /cost"})],
    )
    assert rc == 0, "prose beginning with a slash must not be read as a path"
    assert json.loads(dst.read_text().splitlines()[0])["lastPrompt"] == "/compact"


def test_transport_history_dry_run_writes_nothing(tmp_path, monkeypatch):
    rc, dst = _run_transport_history(
        tmp_path, monkeypatch, [js({"type": "user", "cwd": OLD})], dry_run=True
    )
    assert rc == 0
    assert not dst.exists()
