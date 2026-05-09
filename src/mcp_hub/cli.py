"""Hub CLI — utility commands for agents.

Currently provides one subcommand:

    mcp-hub stop-hook --name=<agent> [--project=<proj>] [--hub-url=<url>]

Designed to be wired into an agent's `~/.claude/settings.json` Stop hook so
queued hub messages surface automatically at every turn boundary, plus a
re-register call if the agent has drifted off the wake path (e.g., after a
hub redeploy wiped the in-memory session registry).

The hook protocol contract:
    - Hook process exits 0
    - If we want Claude to take an extra turn to process content, write JSON
      to stdout: `{"decision": "block", "reason": "<text>"}`. Claude treats
      `reason` as a new prompt and continues.
    - If we want Stop to proceed normally, write nothing.

Fail-open philosophy: any hub error (unreachable, timeout, malformed
response) MUST result in writing nothing and exiting 0. The hook should
NEVER block an agent's Stop because of hub flakiness.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import sys
from typing import Any

DEFAULT_HUB_URL = os.environ.get("MCP_HUB_URL", "https://mcp.monkeypashion.co.uk/mcp")

# Marker file each project uses to declare its agent identity to the hub. Lets
# a single global Stop hook (in ~/.claude/settings.json) work across the whole
# fleet — the cli reads cwd from the hook's stdin payload, looks here, and
# uses the values it finds. Projects without this file silently no-op.
AGENT_MARKER_PATH = pathlib.Path(".claude") / "hub-agent.json"


# ---------------------------------------------------------------------------
# Hub interaction (thin wrapper over the MCP client)
# ---------------------------------------------------------------------------


async def _query_hub(hub_url: str, agent_name: str) -> tuple[str, bool]:
    """Connect to the hub, return (unread_messages_text, is_currently_bound).

    `unread_messages_text` is the rendered output of `get_messages` (empty
    string if no unread). `is_currently_bound` is True when the agent's name
    has ⚡ in `list_agents` (i.e. a live MCP session is bound on the hub
    side). On any error, raises — the caller is responsible for fail-open
    handling.
    """
    # Lazy import so missing-deps doesn't break --help / arg parsing
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async with streamablehttp_client(hub_url, timeout=10) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()

            messages_result = await session.call_tool(
                "get_messages", {"agent_name": agent_name}
            )
            agents_result = await session.call_tool("list_agents", {})

    messages_text = _extract_text(messages_result)
    agents_text = _extract_text(agents_result)

    # ⚡ next to the agent's name means they're bound for wake. Substring match
    # is good enough — list_agents output is one line per agent and the marker
    # appears immediately after the name in `**name** ⚡` form.
    is_bound = f"**{agent_name}** ⚡" in agents_text

    return messages_text, is_bound


def _extract_text(call_tool_result: Any) -> str:
    """Pull the text payload out of an MCP call_tool result."""
    if call_tool_result is None:
        return ""
    content = getattr(call_tool_result, "content", None)
    if content is None and isinstance(call_tool_result, list):
        content = call_tool_result
    if content is None:
        return ""
    for block in content:
        text = getattr(block, "text", None)
        if text is not None:
            return text
    return ""


# ---------------------------------------------------------------------------
# Hook output building
# ---------------------------------------------------------------------------


def build_hook_response(
    *,
    agent_name: str,
    project: str | None,
    messages_text: str,
    is_bound: bool,
) -> dict[str, Any] | None:
    """Decide whether to emit a hook block and what the reason should be.

    Returns the JSON payload Claude Code expects, or None to mean "no block,
    let Stop proceed normally."

    The block is emitted in two cases:
      1. There are queued messages (the primary purpose).
      2. The agent has drifted off the wake path AND there's actionable
         content — re-register reminder rides along.

    A drifted agent with no queued content is left alone — re-registering
    proactively on every Stop would be noisy. They'll get nudged the next
    time something interesting lands for them.
    """
    has_messages = bool(messages_text.strip())

    if not has_messages:
        return None

    # Build the reason text. The discipline part comes from the hub
    # instructions agents already see at session register, but a brief
    # one-line nudge here keeps it salient.
    parts = ["📬 Auto-checked at Stop boundary — queued items below:", "", messages_text.strip()]

    if not is_bound:
        rebind_args = [f'name="{agent_name}"']
        if project:
            rebind_args.append(f'project="{project}"')
        parts.extend(
            [
                "",
                (
                    f"⚠️ Your hub session is currently NOT bound for wake "
                    f"(no ⚡ in list_agents — likely after a hub redeploy). "
                    f"Call `register({', '.join(rebind_args)})` to re-establish "
                    f"the wake path before processing the queue."
                ),
            ]
        )

    parts.extend(
        [
            "",
            (
                "Discipline reminder: process if related/important to current "
                "work; otherwise note (one-line ack) and continue. Don't deeply "
                "context-switch on FYI/low-priority items. Urgent always "
                "responds."
            ),
        ]
    )

    return {"decision": "block", "reason": "\n".join(parts)}


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------


def _read_hook_stdin() -> dict[str, Any]:
    """Read the JSON payload Claude Code sends to hooks on stdin.

    Returns {} on any error (no input, malformed JSON, no stdin attached).
    Callers should treat absent fields as "unknown" — the CLI is designed
    to no-op rather than fail when context is missing.
    """
    try:
        if sys.stdin.isatty():
            return {}
        raw = sys.stdin.read()
        if not raw.strip():
            return {}
        return json.loads(raw)
    except Exception:  # noqa: BLE001
        return {}


def _discover_agent_from_marker(cwd: str | None) -> tuple[str | None, str | None]:
    """Look for `<cwd>/.claude/hub-agent.json` and read agent identity.

    The marker file shape:
        {"name": "dreamteam-lead", "project": "dreamteam"}

    Returns (name, project) — either or both may be None if the marker
    doesn't exist or is malformed. The cli silently no-ops in that case;
    not every project on the system is a hub agent, and most aren't.
    """
    if not cwd:
        return None, None
    marker = pathlib.Path(cwd) / AGENT_MARKER_PATH
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None, None
    name = data.get("name")
    project = data.get("project")
    return (
        name if isinstance(name, str) and name else None,
        project if isinstance(project, str) and project else None,
    )


def _resolve_agent_identity(
    args: argparse.Namespace,
) -> tuple[str | None, str | None]:
    """Resolve which agent this hook invocation is for.

    Resolution order:
      1. Explicit --name (and --project) on the CLI — overrides everything.
         Useful for tests, manual checks, non-standard setups.
      2. Project marker file at <cwd>/.claude/hub-agent.json — discovered
         from the cwd Claude Code passes to hooks via stdin. Lets a single
         global hook config cover the whole fleet.
      3. Nothing — return (None, None) and the cli will silently no-op.

    The marker file path is fixed (`.claude/hub-agent.json`) so each
    project self-declares with no central registry to maintain.
    """
    if args.name:
        return args.name, args.project

    payload = _read_hook_stdin()
    cwd = payload.get("cwd")
    return _discover_agent_from_marker(cwd)


def stop_hook_command(args: argparse.Namespace) -> int:
    """Run the stop-hook subcommand. Always returns 0 (fail-open)."""
    name, project = _resolve_agent_identity(args)
    if name is None:
        # No identity resolved — this project isn't onboarded as a hub agent.
        # Silent no-op: most projects on the box aren't hub agents and the
        # global Stop hook fires in all of them. We don't want noise.
        return 0

    try:
        messages_text, is_bound = asyncio.run(
            _query_hub(args.hub_url, name)
        )
    except Exception as exc:  # noqa: BLE001
        # Fail open — never block the agent on hub flakiness.
        print(f"[mcp-hub stop-hook] hub query failed: {exc!r}", file=sys.stderr)
        return 0

    response = build_hook_response(
        agent_name=name,
        project=project,
        messages_text=messages_text,
        is_bound=is_bound,
    )

    if response is None:
        return 0  # No block — Stop proceeds normally

    print(json.dumps(response))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcp-hub",
        description="MCP Hub — server + utility CLI",
    )
    sub = parser.add_subparsers(dest="subcommand")

    stop_hook = sub.add_parser(
        "stop-hook",
        help="Auto-check hub messages at Stop boundaries (for settings.json hooks)",
        description=(
            "Queries the hub for queued DMs to the active agent and emits "
            "Claude Code Stop hook JSON if any are pending. Designed to be "
            "wired into a global ~/.claude/settings.json Stop hook with no "
            "args — the cli auto-discovers agent identity from the project's "
            ".claude/hub-agent.json marker. Use explicit --name to override "
            "auto-discovery (e.g. for testing). Fail-open — never blocks Stop "
            "on hub errors or missing markers."
        ),
    )
    stop_hook.add_argument(
        "--name",
        default=None,
        help=(
            "Agent name on the hub. If omitted, auto-discovers from "
            "<cwd>/.claude/hub-agent.json via the cwd Claude Code passes to "
            "hooks on stdin."
        ),
    )
    stop_hook.add_argument(
        "--project",
        default=None,
        help="Project name (used in re-register hint when drifted)",
    )
    stop_hook.add_argument(
        "--hub-url",
        default=DEFAULT_HUB_URL,
        help=f"Hub MCP endpoint (default: {DEFAULT_HUB_URL}, or $MCP_HUB_URL)",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.subcommand == "stop-hook":
        return stop_hook_command(args)

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
