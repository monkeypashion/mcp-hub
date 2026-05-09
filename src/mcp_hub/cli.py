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
import sys
from typing import Any

DEFAULT_HUB_URL = os.environ.get("MCP_HUB_URL", "https://mcp.monkeypashion.co.uk/mcp")


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


def stop_hook_command(args: argparse.Namespace) -> int:
    """Run the stop-hook subcommand. Always returns 0 (fail-open)."""
    try:
        messages_text, is_bound = asyncio.run(
            _query_hub(args.hub_url, args.name)
        )
    except Exception as exc:  # noqa: BLE001
        # Fail open — never block the agent on hub flakiness.
        print(f"[mcp-hub stop-hook] hub query failed: {exc!r}", file=sys.stderr)
        return 0

    response = build_hook_response(
        agent_name=args.name,
        project=args.project,
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
        help="Auto-check hub messages at Stop boundaries (for ~/.claude/settings.json hooks)",
        description=(
            "Queries the hub for queued DMs to <name> and emits Claude Code "
            "Stop hook JSON if any are pending. Designed to be wired into "
            "settings.json Stop hooks. Fail-open — never blocks Stop on hub errors."
        ),
    )
    stop_hook.add_argument("--name", required=True, help="Your agent name on the hub")
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
