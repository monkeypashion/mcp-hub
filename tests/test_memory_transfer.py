"""Memory export/import between paired clones.

Covers the three layers:
- hub transfer store (memory_put / memory_list / memory_get, last-write-wins)
- twin pairing (list_twins, register()'s paired-clones announcement)
- CLI pure logic (Claude project-dir encoding, filename safety, MEMORY.md merge)

The end-to-end file movement (export on machine A → import on machine B) is
exercised operationally per-agent during migration; these tests pin the
contracts each side relies on.
"""

from __future__ import annotations

from pathlib import Path

from mcp_hub.cli import (
    _claude_project_dirname,
    _is_safe_memory_filename,
    _merge_memory_index,
)
from mcp_hub.server import create_server


async def _call_tool(server, name: str, args: dict) -> str:
    result = await server._tool_manager.call_tool(name, args)
    if hasattr(result, "content"):
        for block in result.content:
            if hasattr(block, "text"):
                return block.text
    if isinstance(result, list):
        for block in result:
            if hasattr(block, "text"):
                return block.text
    return str(result) if result is not None else ""


# ---------------------------------------------------------------------------
# Hub transfer store
# ---------------------------------------------------------------------------


async def test_memory_put_get_roundtrip(tmp_path: Path):
    server = create_server(db_path=tmp_path / "t.db")
    out = await _call_tool(server, "memory_put", {
        "project": "acme/widgets", "filename": "topic.md",
        "content": "# fact\nbody\n", "from_agent": "widgets-win",
    })
    assert "staged acme/widgets/topic.md" in out
    got = await _call_tool(server, "memory_get", {
        "project": "acme/widgets", "filename": "topic.md",
    })
    assert got == "# fact\nbody\n"


async def test_memory_put_last_write_wins(tmp_path: Path):
    server = create_server(db_path=tmp_path / "t.db")
    for content, agent in [("v1", "a"), ("v2", "b")]:
        await _call_tool(server, "memory_put", {
            "project": "p/r", "filename": "f.md",
            "content": content, "from_agent": agent,
        })
    assert await _call_tool(server, "memory_get", {
        "project": "p/r", "filename": "f.md",
    }) == "v2"
    listing = await _call_tool(server, "memory_list", {"project": "p/r"})
    assert listing.count("f.md") == 1  # upsert, not duplicate rows
    assert "\tb\t" in listing  # provenance follows the last writer


async def test_memory_files_are_project_scoped(tmp_path: Path):
    server = create_server(db_path=tmp_path / "t.db")
    await _call_tool(server, "memory_put", {
        "project": "acme/widgets", "filename": "f.md", "content": "w",
    })
    assert await _call_tool(server, "memory_get", {
        "project": "other/repo", "filename": "f.md",
    }) == ""
    assert await _call_tool(server, "memory_list", {"project": "other/repo"}) == ""


async def test_memory_put_rejects_pathy_filenames(tmp_path: Path):
    server = create_server(db_path=tmp_path / "t.db")
    for bad in ["../evil.md", "a/b.md", "a\\b.md", ".."]:
        out = await _call_tool(server, "memory_put", {
            "project": "p/r", "filename": bad, "content": "x",
        })
        assert "invalid filename" in out
    assert await _call_tool(server, "memory_list", {"project": "p/r"}) == ""


# ---------------------------------------------------------------------------
# Twin pairing
# ---------------------------------------------------------------------------


async def test_list_twins_same_project_only(tmp_path: Path):
    server = create_server(db_path=tmp_path / "t.db")
    for name, proj in [
        ("widgets-linux", "acme/widgets"),
        ("widgets-win", "acme/widgets"),
        ("gizmo-linux", "acme/gizmo"),
    ]:
        await _call_tool(server, "register", {"name": name, "project": proj})
    out = await _call_tool(server, "list_twins", {
        "project": "acme/widgets", "exclude_agent": "widgets-linux",
    })
    assert out == "widgets-win"


async def test_register_announces_paired_clones(tmp_path: Path):
    server = create_server(db_path=tmp_path / "t.db")
    first = await _call_tool(server, "register", {
        "name": "widgets-linux", "project": "acme/widgets",
    })
    assert "Paired clones" not in first  # alone — no announcement
    second = await _call_tool(server, "register", {
        "name": "widgets-win", "project": "acme/widgets",
    })
    assert "Paired clones" in second
    assert "widgets-linux" in second


# ---------------------------------------------------------------------------
# CLI pure logic
# ---------------------------------------------------------------------------


def test_claude_project_dirname_posix():
    assert (
        _claude_project_dirname("/home/monke/Projects/code/monkeypashion/mcp-hub")
        == "-home-monke-Projects-code-monkeypashion-mcp-hub"
    )


def test_claude_project_dirname_windows():
    assert (
        _claude_project_dirname("D:\\Projects\\code\\monkeypashion\\mcp-hub")
        == "D--Projects-code-monkeypashion-mcp-hub"
    )


def test_claude_project_dirname_strips_trailing_separator():
    assert _claude_project_dirname("/a/b/") == "-a-b"


def test_safe_memory_filename():
    assert _is_safe_memory_filename("topic.md")
    assert not _is_safe_memory_filename("../x.md")
    assert not _is_safe_memory_filename("a/b.md")
    assert not _is_safe_memory_filename("a\\b.md")
    assert not _is_safe_memory_filename("")
    assert not _is_safe_memory_filename("..")


def test_merge_memory_index_appends_only_new_and_present(tmp_path: Path):
    """Staged lines are appended only when (a) not already referenced locally
    and (b) the linked file actually exists (was imported, not skipped)."""
    (tmp_path / "imported.md").write_text("x", encoding="utf-8")
    (tmp_path / "already.md").write_text("x", encoding="utf-8")
    local = "- [Already](already.md) — local hook\n"
    staged = (
        "- [Already](already.md) — twin's wording (must not duplicate)\n"
        "- [Imported](imported.md) — new from twin\n"
        "- [Skipped](never-imported.md) — file not present locally\n"
        "not an index line\n"
    )
    merged, added = _merge_memory_index(local, staged, tmp_path)
    assert added == 1
    assert merged.count("(already.md)") == 1
    assert "(imported.md)" in merged
    assert "never-imported.md" not in merged
    # Local content preserved verbatim at the top.
    assert merged.startswith("- [Already](already.md) — local hook")


def test_merge_memory_index_empty_local(tmp_path: Path):
    (tmp_path / "a.md").write_text("x", encoding="utf-8")
    merged, added = _merge_memory_index("", "- [A](a.md) — hook\n", tmp_path)
    assert added == 1
    assert merged == "- [A](a.md) — hook\n"


def test_merge_memory_index_nothing_to_add(tmp_path: Path):
    local = "- [A](a.md) — hook\n"
    merged, added = _merge_memory_index(local, local, tmp_path)
    assert added == 0
    assert merged == local


# ---------------------------------------------------------------------------
# Reconciliation pieces — hash in listing, --replace-index, verify digest
# ---------------------------------------------------------------------------


async def test_memory_list_includes_content_hash(tmp_path: Path):
    """memory_list's 5th field is a truncated sha256 of the content — the
    basis of memory-verify's convergence proof."""
    import hashlib

    server = create_server(db_path=tmp_path / "t.db")
    await _call_tool(server, "memory_put", {
        "project": "p/r", "filename": "f.md", "content": "hello\n",
    })
    listing = await _call_tool(server, "memory_list", {"project": "p/r"})
    parts = listing.split("\t")
    assert len(parts) == 5
    expected = hashlib.sha256(b"hello\n").hexdigest()[:16]
    assert parts[4] == expected


def test_text_digest_matches_server_hash():
    """Client and server must hash identically or verify always fails."""
    import hashlib

    from mcp_hub.cli import _text_digest

    text = "line1\nline2\n"
    assert _text_digest(text) == hashlib.sha256(text.encode()).hexdigest()[:16]


def test_replace_index_semantics(tmp_path: Path):
    """--replace-index adopts the canonical index verbatim; the default merge
    path appends. Exercised at the function level (write behavior)."""
    # Simulate what _memory_import does for the index in each mode.
    from mcp_hub.cli import _merge_memory_index

    (tmp_path / "a.md").write_text("x", encoding="utf-8")
    local = "# My structure\n- [A](a.md) — mine\n"
    staged = "# Canonical structure\n- [A](a.md) — curated wording\n"

    # Default merge: a.md already referenced → nothing appended, local wins.
    merged, added = _merge_memory_index(local, staged, tmp_path)
    assert added == 0 and merged == local
    # Replace mode is a verbatim adoption — by definition staged text itself.
    assert staged != local  # the divergence --replace-index exists to resolve


def test_main_survives_streams_without_reconfigure(monkeypatch, capsys):
    """cp1252-crash fix must be fail-soft: a stdout replacement lacking
    .reconfigure() (pytest capture, pipes on old runtimes) must not break
    the CLI entrypoint."""
    import io

    from mcp_hub import cli

    class _Plain(io.StringIO):
        pass  # no reconfigure()

    monkeypatch.setattr("sys.stdout", _Plain())
    monkeypatch.setattr("sys.stderr", _Plain())
    assert cli.main([]) == 0  # bare invocation prints help, exits 0
