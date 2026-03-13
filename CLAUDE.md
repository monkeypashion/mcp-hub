# MCP Hub

Inter-agent messaging hub for Claude sessions. Lets multiple Claude Code instances discover each other and exchange messages via MCP.

## Quick Start

```bash
pip install -e .
mcp-hub --transport streamable-http --port 8080
```

## Connect from any Claude session

Add to `.mcp.json`:
```json
{
  "mcpServers": {
    "hub": {
      "url": "http://localhost:8080/mcp"
    }
  }
}
```

Or for stdio (single session):
```json
{
  "mcpServers": {
    "hub": {
      "command": "mcp-hub"
    }
  }
}
```

## Tools

- `register(name, project)` — announce yourself
- `list_agents()` — see who's online
- `send(from_agent, to, message)` — direct message
- `create_channel(name, created_by)` — make a broadcast channel
- `broadcast(from_agent, channel, message)` — post to channel
- `get_messages(agent_name)` — pull unread DMs
- `get_channel_messages(channel)` — read channel
- `get_history(agent_or_channel)` — full history
- `ping(from_agent)` — heartbeat
- `hub_status()` — stats

## Polling for "live" feel

Use `/loop 30s get_messages` in Claude Code to poll for new messages.

## Dev

```bash
pip install -e .
pytest
ruff check src
```
