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


def test_suffix_covers_subdirectories_of_the_worktree(hub_config, tmp_path):
    """Card #432. The entry covers the whole worktree, not just its top folder.

    Exact equality lost the suffix one level down while the repo half of the
    derivation kept working (git walks UP from cwd), so a session in a
    subdirectory — the normal case, not the exception — fell back to the bare
    name.
    """
    work = tmp_path / "xport" / "mcp-hub"
    (work / "src" / "deep").mkdir(parents=True)
    hub_config.write_text(json.dumps({"workspaces": {str(work): "xport"}}))
    assert cli._workspace_suffix(str(work)) == "xport"
    assert cli._workspace_suffix(str(work / "src")) == "xport"
    assert cli._workspace_suffix(str(work / "src" / "deep")) == "xport"


def test_two_clones_do_not_collapse_below_their_top_folders(hub_config, tmp_path):
    """The regression the card exists for: ONE name for TWO clones.

    Both clones of a repo derive the same `<repo>-<host>`, so with the suffix
    dropped in a subdirectory they became indistinguishable — the collision
    the suffixes were added to prevent, one directory below them.
    """
    a = tmp_path / "one" / "templates"
    b = tmp_path / "two" / "templates"
    (a / "templates").mkdir(parents=True)
    (b / "templates").mkdir(parents=True)
    hub_config.write_text(json.dumps({"workspaces": {str(a): "one", str(b): "two"}}))
    assert cli._workspace_suffix(str(a / "templates")) == "one"
    assert cli._workspace_suffix(str(b / "templates")) == "two"


def test_suffix_matches_path_components_not_string_prefixes(hub_config, tmp_path):
    """`/a/b` must not claim `/a/bc` — a sibling is not a descendant."""
    work = tmp_path / "xport" / "mcp-hub"
    sibling = tmp_path / "xport" / "mcp-hub-2"
    work.mkdir(parents=True)
    sibling.mkdir(parents=True)
    hub_config.write_text(json.dumps({"workspaces": {str(work): "xport"}}))
    assert cli._workspace_suffix(str(sibling)) is None


def test_nested_entries_resolve_to_the_most_specific(hub_config, tmp_path):
    """A submodule with its own entry keeps ITS suffix, not its parent's."""
    outer = tmp_path / "clone"
    inner = outer / "vendor" / "submodule"
    inner.mkdir(parents=True)
    hub_config.write_text(
        json.dumps({"workspaces": {str(outer): "outer", str(inner): "inner"}})
    )
    assert cli._workspace_suffix(str(outer)) == "outer"
    assert cli._workspace_suffix(str(outer / "vendor")) == "outer"
    assert cli._workspace_suffix(str(inner)) == "inner"
    assert cli._workspace_suffix(str(inner / "src")) == "inner"


def test_root_entry_does_not_rename_the_whole_machine(hub_config, tmp_path):
    """`"/"` normalises to the empty string — a prefix of every path.

    Skipped deliberately: one stray character in a hand-edited config would
    otherwise re-suffix every lane on the box at once.
    """
    hub_config.write_text(json.dumps({"workspaces": {"/": "everything"}}))
    assert cli._workspace_suffix(str(tmp_path)) is None


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


def _run_transport_history(tmp_path, monkeypatch, lines, dry_run=False,
                           old=OLD, new=NEW):
    monkeypatch.setattr(cli.pathlib.Path, "home", classmethod(lambda cls: tmp_path))
    src_dir = tmp_path / ".claude" / "projects" / cli._claude_project_dirname(old)
    src_dir.mkdir(parents=True)
    (src_dir / "sess.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    args = cli.argparse.Namespace(from_cwd=old, to_cwd=new, dry_run=dry_run)
    rc = cli.transport_history_command(args)
    dst = tmp_path / ".claude" / "projects" / cli._claude_project_dirname(new) / "sess.jsonl"
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


def test_a_hook_inside_the_transported_worktree_does_not_refuse(tmp_path, monkeypatch):
    """Found by dev-vm-1 verifying my guard, and it bit THEIR box, not mine.

    ENVIRONMENT was exempt in the new guard but not in the older completeness
    loop, so "never re-keyed, never refused" was only half true. A hook command
    living INSIDE the transported worktree contains the source path by
    construction — which is the mcp-hub agent itself, the one most likely to be
    migrated when a machine is retired.

    My own live tests could not catch it: this seat's source is the /mnt/d tree
    while its hooks name /home/monke, so the two differ here and coincide there.
    A works-on-my-machine that only a second machine could find.
    """
    rc, dst = _run_transport_history(
        tmp_path, monkeypatch,
        [js({"type": "user", "cwd": OLD,
             "hookInfos": [{"command": f"{OLD}/.venv/bin/mcp-hub stop-hook"}]})],
    )
    assert rc == 0, "a hook path inside the worktree must not refuse the transport"
    rec = json.loads(dst.read_text())
    assert rec["hookInfos"][0]["command"] == f"{OLD}/.venv/bin/mcp-hub stop-hook", \
        "and it must stay byte-exact — it records what ran, on the box that ran it"


def test_a_session_launched_below_the_worktree_root_keeps_its_subdirectory(tmp_path, monkeypatch):
    """LOCATED means "inside the destination", not "equal to it".

    Demanding equality contradicted the re-key branch, which preserves subdirs:
    one rewrote `<old>/skills/x` to `<dest>/skills/x` and the other then refused
    it. Both cannot be right.
    """
    rc, dst = _run_transport_history(
        tmp_path, monkeypatch, [js({"type": "user", "cwd": f"{OLD}/skills/memory-sync"})]
    )
    assert rc == 0
    assert json.loads(dst.read_text())["cwd"] == f"{NEW}/skills/memory-sync"


def test_a_sibling_worktree_is_not_mistaken_for_the_source(tmp_path, monkeypatch):
    """`<old>-2` is a SIBLING, not a child — and the fan-out genuinely creates
    those when two clones of one repo land together. A substring test read it as
    the source and produced `<dest>-2`."""
    rc, dst = _run_transport_history(
        tmp_path, monkeypatch, [js({"type": "user", "cwd": f"{OLD}-2"})]
    )
    assert rc == 0
    assert json.loads(dst.read_text())["cwd"] == NEW, "a sibling is a third path"


# The destination of a SAME-MACHINE transport is a sibling of the source by
# construction: the original owns the canonical path, so the clone lands at
# `<repo>-<label>`. That makes the source a PREFIX of the destination — the one
# arrangement the substring-based leak check could not read.
SIBLING = f"{OLD}-sidecar"


def test_a_destination_that_extends_the_source_is_not_read_as_a_leak(tmp_path, monkeypatch):
    """The default same-machine destination, refused by its own guard.

    `<old>` is a prefix of `<old>-sidecar`, so a correctly-rewritten `cwd` still
    CONTAINS the source path and the substring test called it a structural leak.
    Every same-machine transport therefore landed with no conversation history —
    and not visibly, because squad treats a re-key refusal as "transported
    WITHOUT history" while reporting the transport itself as done. Found by
    running the showcase, not by a fixture: no fixture here had ever used a
    destination derived the way the real one is.
    """
    rc, dst = _run_transport_history(
        tmp_path, monkeypatch, [js({"type": "user", "cwd": OLD})], new=SIBLING
    )
    assert rc == 0, "the ordinary same-machine destination must not be refused"
    assert json.loads(dst.read_text())["cwd"] == SIBLING


def test_stale_fields_excuses_the_destination_but_not_the_source():
    """The positive control for the test above, asked of the guard directly.

    Excusing destination-shaped text is only safe if the SAME prefix arrangement
    still catches a genuine reference to the source. Written at this level
    deliberately: the first draft of this control went through the command and
    passed against a completely blinded guard, because the refusal it observed
    came from the unclassified-path check instead — green for the wrong reason.
    """
    old = (cli._claude_project_dirname(OLD), OLD)
    new = (cli._claude_project_dirname(SIBLING), SIBLING)

    # the destination contains the source as a prefix, and is not a leak
    assert cli._history_stale_fields({"cwd": SIBLING}, old, new) == []
    assert cli._history_stale_fields({"cwd": f"{SIBLING}/src"}, old, new) == []
    # the source itself still is, in that same arrangement
    assert cli._history_stale_fields({"cwd": OLD}, old, new) == ["cwd"]
    assert cli._history_stale_fields({"x": f"{OLD}/memory"}, old, new) == ["x"]
    # and in ENCODED form, where '-' is itself the separator — which is why this
    # masks the destination rather than checking for a path boundary
    assert cli._history_stale_fields({"p": f"/h/.claude/projects/{new[0]}/m"}, old, new) == []
    assert cli._history_stale_fields({"p": f"/h/.claude/projects/{old[0]}/m"}, old, new) == ["p"]


def test_an_unclassified_path_is_caught_as_a_dict_KEY_too(tmp_path, monkeypatch):
    """The guard walked values only, so half the structure was blind to it.

    trackedFileBackups is a dict keyed BY absolute path — the very shape this
    exists to catch. It is emptied before the check today, so nothing known hits
    it, but a guard whose whole purpose is the coupling nobody predicted cannot
    inspect only half of what it walks.
    """
    rc, dst = _run_transport_history(
        tmp_path, monkeypatch,
        [js({"type": "user", "cwd": OLD, "mystery": {"/some/third/path": 1}})],
    )
    assert rc == 1, "a third path as a KEY must refuse exactly as a value does"
    assert not dst.exists()


def test_transport_history_dry_run_writes_nothing(tmp_path, monkeypatch):
    rc, dst = _run_transport_history(
        tmp_path, monkeypatch, [js({"type": "user", "cwd": OLD})], dry_run=True
    )
    assert rc == 0
    assert not dst.exists()
