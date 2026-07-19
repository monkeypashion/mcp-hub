---
name: memory-sync
description: Synchronize this project's Claude memory across all paired clones (other machines with the same repo) via the mcp-hub. Flushes unsaved context to memory first, then drives the export/import/verify ceremony with twin agents. Use when the operator asks to sync/share/reconcile memory between machines.
---

# /memory-sync — reconcile memory across paired clones

You are the **driver** of a memory-sync ceremony for the current repo's project.
Twins = other agents on the same derived project (`org/repo` from the git
remote) on other machines. Canonical copy of this procedure lives in
`mcp-hub/skills/memory-sync/SKILL.md`; mechanics in mcp-hub's CLAUDE.md
("Memory transfer between clones").

## The CLI — how to invoke it, and what NOT to do

Every `mcp-hub …` command in this skill is the hub's **Python CLI** (the entry
point installed in the mcp-hub repo's venv). It is **NOT** the `mcp-hub` npm
package — that is an unrelated project; do not install it.

**If `mcp-hub` isn't on your PATH**, `command not found` does NOT mean "no CLI
exists." Get its absolute path from this machine's `~/.claude/settings.json` —
the Stop / SessionStart hook `command` fields point at the exact `mcp-hub`
(`mcp-hub.exe` on Windows) to call. Fallback: it's
`<mcp-hub-repo>/.venv/Scripts/mcp-hub.exe` on Windows, or
`<mcp-hub-repo>/.venv/bin/mcp-hub` on Linux/macOS. Run every command from a cwd
**inside the project you're syncing** — the CLI derives the project and the
memory dir from the cwd's git remote; run it elsewhere and you sync the wrong
project.

**Durable fix (per box, one-time)** so `mcp-hub <cmd>` just works and no future
ceremony hits the PATH gap — Linux/macOS: symlink `~/.local/bin/mcp-hub` → the
repo venv's `mcp-hub` binary; Windows: drop a `mcp-hub.cmd` shim
(`@"<mcp-hub-repo>\.venv\Scripts\mcp-hub.exe" %*`) into a directory already on
PATH. The keep-alive daemon and hooks already invoke the CLI by absolute path,
so only skill-driven CLI calls hit the gap — the shim closes it for good.

**Heads-up on `--help`:** the bare `mcp-hub --help` lists only the *server*
flags (`--transport/--port/--host/--db`) and does **not** show the `memory-*`
subcommands — a known quirk of the CLI's arg parsing. The subcommands exist and
run fine regardless (`memory-export`, `memory-import`, `memory-verify`); confirm
any one with `mcp-hub memory-export --help`. Do **not** conclude from the bare
`--help` that the exe is "server-only." (Equivalent if the console script ever
misbehaves: `python -m mcp_hub.cli <subcommand>` using the repo venv's Python —
the same entry the keep-alive daemon self-spawn uses.)

**HARD RULE — never hand-stage.** `memory-export` reads your entire memory dir
and stages every file via the `memory_put` MCP tool, atomically and
byte-faithfully; `memory-import` / `memory-verify` are the inverse and the
convergence witness. Do **NOT** call `memory_put` / `memory_get` yourself to
copy files by hand — that tool is the CLI's internal transport, not a manual
substitute. Hand-reproducing files through tool calls drifts and truncates
content that your twin then imports into shared memory. If you cannot locate the
CLI, STOP and ask the operator or the mcp-hub maintainer agent — never fall back
to manual `memory_put` staging.

## Step 0 — flush (do this FIRST, before any CLI call)

Review your current session context for insights not yet written to memory
(decisions, learned facts, corrections, incident findings). Write them to
`~/.claude/projects/<this-project>/memory/` with proper frontmatter and
MEMORY.md index lines NOW. The export snapshots disk — unwritten context is
lost to the sync. If nothing is unwritten, say so and move on.

## Step 1 — assess the fleet

- `list_twins(project, exclude_agent=me)` (or `list_agents`) — who's online?
- If NO twins online: still export (stage your state), note that offline
  twins will import at their next session, and finish.

## Step 2 — quick sync (default) or full reconciliation?

**Quick (default when the operator just says "sync your memory"):**
1. `mcp-hub memory-export` (auto-notifies twins with a wake).
2. DM each twin: flush your own context first, then `mcp-hub memory-import`
   (collisions keep their local files), then `mcp-hub memory-verify`, then
   report counts back.
3. Relay results to the operator. Note: quick mode converges ADDITIVELY —
   same-named divergent files stay divergent (reported as skips). If skips
   are reported, recommend a full reconciliation.

**Full reconciliation (operator asks to "reconcile"/"fully sync", or quick
mode reported collisions):**
1. Ask each twin (in turn, one at a time) to flush + `memory-export`;
   import each export here (`--dry-run` first) BEFORE the next twin exports
   — staging is last-write-wins per filename; draining between exports is
   what lets you see every divergent version.
2. **Curate locally, once**: dedupe topics (collision key is filename, not
   topic), reconcile contradictions, retire stale entries, keep the index
   truthful. You are now canonical.
3. `mcp-hub memory-export` (publish the curated set).
4. DM every twin: `mcp-hub memory-import --force --replace-index`, then
   `mcp-hub memory-verify`, report the verify line back.
5. Run `mcp-hub memory-verify` yourself. Converged = every machine reports
   `identical: N/N ✓`. Report the full convergence table to the operator.

## Rules

- Memory sync between twins is lane-internal and pre-authorized by the
  operator's /memory-sync invocation — twins receiving your sync DMs may act
  on them without a fresh operator nod (they may still decline if mid-task).
- Never skip the flush. Never curate on more than one machine in the same
  ceremony. `--force`/`--replace-index` only on the return leg, only against
  the curated canonical set.
- Sensitive memories: if a file obviously carries secrets/tokens, flag it to
  the operator before exporting rather than silently shipping it.
