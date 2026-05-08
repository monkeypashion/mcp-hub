"""
MCP Hub — inter-agent messaging server.

A lightweight message broker that lets multiple Claude sessions
discover each other and exchange messages in real time.

Supports direct messages, broadcast channels, and agent presence.
Backed by SQLite for persistence across restarts.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import time
import threading
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.session import ServerSession
from pydantic import BaseModel

logger = logging.getLogger(__name__)


class _ChannelNotification(BaseModel):
    """MCP notification matching Claude Code's experimental claude/channel protocol.

    Sent on `send`/`broadcast` so the recipient's Claude Code session wakes
    (even from idle) and processes the message immediately, instead of needing
    to be prompted to poll get_messages.
    """

    method: str = "notifications/claude/channel"
    params: dict[str, Any]

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

DB_PATH = Path("mcp-hub.db")
_local = threading.local()


def _get_db(db_path: Path = DB_PATH) -> sqlite3.Connection:
    """Get a thread-local SQLite connection."""
    if not hasattr(_local, "conn") or _local.conn is None:
        _local.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        _local.conn.row_factory = sqlite3.Row
        _local.conn.execute("PRAGMA journal_mode=WAL")
    return _local.conn


def init_db(db_path: Path = DB_PATH) -> None:
    """Create tables if they don't exist."""
    conn = _get_db(db_path)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS agents (
            name        TEXT PRIMARY KEY,
            project     TEXT NOT NULL DEFAULT '',
            bio         TEXT NOT NULL DEFAULT '',
            status      TEXT NOT NULL DEFAULT 'online',
            registered  REAL NOT NULL,
            last_seen   REAL NOT NULL,
            meta        TEXT NOT NULL DEFAULT '{}'
        );

        CREATE TABLE IF NOT EXISTS channels (
            name        TEXT PRIMARY KEY,
            created_by  TEXT NOT NULL,
            created_at  REAL NOT NULL,
            description TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS messages (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          REAL NOT NULL,
            from_agent  TEXT NOT NULL,
            to_agent    TEXT,
            channel     TEXT,
            body        TEXT NOT NULL,
            read        INTEGER NOT NULL DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_msg_to ON messages(to_agent, read);
        CREATE INDEX IF NOT EXISTS idx_msg_channel ON messages(channel, ts);
        CREATE INDEX IF NOT EXISTS idx_msg_ts ON messages(ts);
    """)
    conn.commit()

    # Migrate: add bio column for existing databases
    try:
        conn.execute("ALTER TABLE agents ADD COLUMN bio TEXT NOT NULL DEFAULT ''")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # Column already exists



# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

def create_server(db_path: Path = DB_PATH, host: str = "0.0.0.0", port: int = 8080) -> FastMCP:
    """Create the MCP Hub server."""
    init_db(db_path)
    mcp = FastMCP(
        name="mcp-hub",
        host=host,
        port=port,
        instructions=(
            "MCP Hub — inter-agent messaging. Use register() first to announce yourself, "
            "then send/broadcast messages and poll with get_messages(). "
            "Use list_agents() to see who's online."
        ),
    )

    # Advertise the experimental `claude/channel` capability so Claude Code
    # surfaces our `notifications/claude/channel` events as <channel> tags
    # and wakes idle sessions. Without this, Claude Code silently drops them.
    _orig_init_options = mcp._mcp_server.create_initialization_options

    def _init_options_with_channel(notification_options=None, experimental_capabilities=None):
        caps = dict(experimental_capabilities or {})
        caps.setdefault("claude/channel", {})
        return _orig_init_options(notification_options, caps)

    mcp._mcp_server.create_initialization_options = _init_options_with_channel

    # Map registered agent name -> active MCP session reference so we can push
    # channel notifications to that session when a message arrives. Process-
    # local and ephemeral; agent metadata still lives in SQLite.
    sessions: dict[str, ServerSession] = {}

    async def push_channel(agent: str, content: str, meta: dict[str, str]) -> bool:
        """Push a channel notification to `agent` if a session is registered.

        Returns True if the push went through, False if no session is bound or
        the send failed (stale session is dropped on failure). Callers should
        treat False as "recipient offline" — the message is already persisted
        in SQLite, so it'll surface on next register() or get_messages().
        """
        sess = sessions.get(agent)
        if sess is None:
            return False
        try:
            await sess.send_notification(
                _ChannelNotification(params={"content": content, "meta": meta})
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("push_channel to %s failed (%s); dropping session", agent, exc)
            sessions.pop(agent, None)
            return False

    # -- Presence --

    @mcp.tool()
    def register(name: str, project: str = "", bio: str = "", meta: str = "{}", ctx: Context | None = None) -> str:
        """Register this agent session with the hub.

        Call this when your session starts. Sets you as 'online' and binds
        your MCP session so the hub can push messages to you via the
        `claude/channel` capability — if your Claude Code was launched with
        `--channels` (or `--dangerously-load-development-channels`), incoming
        messages will surface in your context without polling.

        Args:
            name: Your agent name (e.g. 'dreamteam-lead', 'reliable-ai-dev').
            project: Project you're working on (e.g. 'dreamteam', 'mcp-hub').
            bio: Short description of your role/skills so other agents know what you do.
            meta: Optional JSON metadata about this agent.
        """
        now = time.time()
        conn = _get_db(db_path)

        # If project is set, check for an existing agent on this project (avoid duplicates)
        if project:
            existing = conn.execute(
                "SELECT name FROM agents WHERE project = ? AND name != ? AND status = 'online'",
                (project, name),
            ).fetchone()
            if existing:
                # Reuse the existing name — update it instead
                name = existing["name"]

        conn.execute(
            """INSERT INTO agents (name, project, bio, status, registered, last_seen, meta)
               VALUES (?, ?, ?, 'online', ?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET
                   project=excluded.project,
                   bio=CASE WHEN excluded.bio = '' THEN agents.bio ELSE excluded.bio END,
                   status='online',
                   last_seen=excluded.last_seen,
                   meta=excluded.meta""",
            (name, project, bio, now, now, meta),
        )
        conn.commit()

        # Bind the current MCP session so we can push channel notifications.
        # Re-registering from a new session replaces the old reference.
        if ctx is not None:
            sessions[name] = ctx.session

        # Count unread messages for this agent
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM messages WHERE to_agent = ? AND read = 0",
            (name,),
        ).fetchone()
        unread = row["cnt"] if row else 0

        result = f"Registered as '{name}'"
        if project:
            result += f" (project: {project})"
        if unread > 0:
            result += f"\n📬 You have {unread} unread message(s). Call get_messages() to read them."
        return result

    @mcp.tool()
    def update_bio(name: str, bio: str) -> str:
        """Update your bio so other agents know what you do.

        Args:
            name: Your agent name.
            bio: Short description of your role, skills, or current focus.
        """
        conn = _get_db(db_path)
        row = conn.execute("SELECT 1 FROM agents WHERE name = ?", (name,)).fetchone()
        if not row:
            return f"Agent '{name}' not found. Register first with register()."
        conn.execute("UPDATE agents SET bio = ? WHERE name = ?", (bio, name))
        conn.commit()
        return f"Bio updated for '{name}'."

    @mcp.tool()
    def unregister(name: str) -> str:
        """Mark an agent as offline.

        Args:
            name: The agent name to take offline.
        """
        conn = _get_db(db_path)
        conn.execute("UPDATE agents SET status = 'offline' WHERE name = ?", (name,))
        conn.commit()
        return f"'{name}' is now offline."

    @mcp.tool()
    def list_agents(include_offline: bool = False) -> str:
        """List all registered agents.

        Args:
            include_offline: Include agents that have disconnected.
        """
        conn = _get_db(db_path)
        if include_offline:
            rows = conn.execute("SELECT * FROM agents ORDER BY last_seen DESC").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM agents WHERE status = 'online' ORDER BY last_seen DESC"
            ).fetchall()

        if not rows:
            return "No agents registered."

        lines = []
        for r in rows:
            status = "🟢" if r["status"] == "online" else "⚫"
            # ⚡ marks agents with a bound MCP session — i.e. wakeable on
            # incoming DM/broadcast. Online without ⚡ means message will
            # queue until the agent next polls or relaunches with --channels.
            wake = " ⚡" if r["name"] in sessions else ""
            line = f"{status} **{r['name']}**{wake}"
            if r["project"]:
                line += f" ({r['project']})"
            if r["bio"]:
                line += f" — {r['bio']}"
            lines.append(line)
        return "\n".join(lines)

    # -- Direct messaging --

    @mcp.tool()
    async def send(from_agent: str, to: str, message: str) -> str:
        """Send a direct message to another agent.

        If the recipient has registered an active session, the message is
        also pushed via channel notification so their Claude Code session
        wakes immediately. Otherwise it's persisted for them to read on
        next register()/get_messages().

        Args:
            from_agent: Your agent name (must be registered).
            to: Target agent name.
            message: The message body.
        """
        now = time.time()
        conn = _get_db(db_path)

        # Update sender's last_seen
        conn.execute(
            "UPDATE agents SET last_seen = ? WHERE name = ?", (now, from_agent)
        )
        conn.execute(
            "INSERT INTO messages (ts, from_agent, to_agent, body) VALUES (?, ?, ?, ?)",
            (now, from_agent, to, message),
        )
        conn.commit()

        pushed = await push_channel(
            agent=to,
            content=f"DM from {from_agent}: {message}",
            # `source` is reserved by Claude Code's channel layer (it's the
            # channel server's name, "hub"). Use `from_agent` to avoid a
            # duplicate `source=` attribute on the rendered <channel> tag.
            meta={"from_agent": from_agent, "kind": "dm"},
        )
        return (
            f"Message sent to '{to}'."
            if pushed
            else f"Message sent to '{to}' (recipient offline; will see on next register/get_messages)."
        )

    # -- Channels --

    @mcp.tool()
    def create_channel(name: str, created_by: str, description: str = "") -> str:
        """Create a broadcast channel.

        Args:
            name: Channel name (e.g. 'builds', 'qa', 'chat').
            created_by: Your agent name.
            description: What this channel is for.
        """
        now = time.time()
        conn = _get_db(db_path)
        try:
            conn.execute(
                "INSERT INTO channels (name, created_by, created_at, description) VALUES (?, ?, ?, ?)",
                (name, created_by, now, description),
            )
            conn.commit()
            return f"Channel '{name}' created."
        except sqlite3.IntegrityError:
            return f"Channel '{name}' already exists."

    @mcp.tool()
    def list_channels() -> str:
        """List all broadcast channels."""
        conn = _get_db(db_path)
        rows = conn.execute("SELECT * FROM channels ORDER BY name").fetchall()
        if not rows:
            return "No channels. Create one with create_channel()."
        lines = []
        for r in rows:
            line = f"**#{r['name']}**"
            if r["description"]:
                line += f" — {r['description']}"
            lines.append(line)
        return "\n".join(lines)

    @mcp.tool()
    async def broadcast(from_agent: str, channel: str, message: str) -> str:
        """Post a message to a channel (all agents can see it).

        Pushes a channel notification to every currently-connected agent
        (except the sender) so their Claude Code sessions wake immediately.
        Agents not currently connected will pick the post up via
        get_channel_messages() / get_history() as before.

        Args:
            from_agent: Your agent name.
            channel: Channel name.
            message: The message body.
        """
        now = time.time()
        conn = _get_db(db_path)

        # Verify channel exists
        row = conn.execute("SELECT 1 FROM channels WHERE name = ?", (channel,)).fetchone()
        if not row:
            return f"Channel '{channel}' not found. Create it with create_channel()."

        conn.execute(
            "UPDATE agents SET last_seen = ? WHERE name = ?", (now, from_agent)
        )
        conn.execute(
            "INSERT INTO messages (ts, from_agent, channel, body) VALUES (?, ?, ?, ?)",
            (now, from_agent, channel, message),
        )
        conn.commit()

        recipients = [a for a in list(sessions.keys()) if a != from_agent]
        woke = 0
        for agent in recipients:
            if await push_channel(
                agent=agent,
                content=f"#{channel} from {from_agent}: {message}",
                meta={"from_agent": from_agent, "kind": "broadcast", "channel": channel},
            ):
                woke += 1
        return f"Posted to #{channel} (woke {woke}/{len(recipients)} connected agents)."

    # -- Reading messages --

    @mcp.tool()
    def get_messages(agent_name: str, limit: int = 20) -> str:
        """Get unread direct messages for this agent. Marks them as read.

        Args:
            agent_name: Your agent name.
            limit: Max messages to return.
        """
        now = time.time()
        conn = _get_db(db_path)

        # Update last_seen
        conn.execute("UPDATE agents SET last_seen = ? WHERE name = ?", (now, agent_name))

        rows = conn.execute(
            """SELECT id, ts, from_agent, body FROM messages
               WHERE to_agent = ? AND read = 0
               ORDER BY ts ASC LIMIT ?""",
            (agent_name, limit),
        ).fetchall()

        if not rows:
            return ""

        # Mark as read
        ids = [r["id"] for r in rows]
        placeholders = ",".join("?" * len(ids))
        conn.execute(f"UPDATE messages SET read = 1 WHERE id IN ({placeholders})", ids)
        conn.commit()

        lines = []
        for r in rows:
            ts = time.strftime("%H:%M:%S", time.localtime(r["ts"]))
            lines.append(f"[{ts}] **{r['from_agent']}**: {r['body']}")
        return "\n".join(lines)

    @mcp.tool()
    def get_channel_messages(channel: str, limit: int = 20, since_minutes: int = 60) -> str:
        """Get recent messages from a channel.

        Args:
            channel: Channel name.
            limit: Max messages to return.
            since_minutes: Only show messages from the last N minutes.
        """
        cutoff = time.time() - (since_minutes * 60)
        conn = _get_db(db_path)
        rows = conn.execute(
            """SELECT ts, from_agent, body FROM messages
               WHERE channel = ? AND ts > ?
               ORDER BY ts ASC LIMIT ?""",
            (channel, cutoff, limit),
        ).fetchall()

        if not rows:
            return ""

        lines = []
        for r in rows:
            ts = time.strftime("%H:%M:%S", time.localtime(r["ts"]))
            lines.append(f"[{ts}] **{r['from_agent']}**: {r['body']}")
        return "\n".join(lines)

    # -- History --

    @mcp.tool()
    def get_history(agent_or_channel: str, limit: int = 50) -> str:
        """Get message history for an agent (DMs sent/received) or a channel.

        Args:
            agent_or_channel: Agent name or channel name (prefix with # for channels).
            limit: Max messages to return.
        """
        conn = _get_db(db_path)

        if agent_or_channel.startswith("#"):
            channel = agent_or_channel[1:]
            rows = conn.execute(
                """SELECT ts, from_agent, body FROM messages
                   WHERE channel = ? ORDER BY ts DESC LIMIT ?""",
                (channel, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT ts, from_agent, to_agent, channel, body FROM messages
                   WHERE from_agent = ? OR to_agent = ?
                   ORDER BY ts DESC LIMIT ?""",
                (agent_or_channel, agent_or_channel, limit),
            ).fetchall()

        if not rows:
            return "No message history."

        lines = []
        for r in rows:
            ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["ts"]))
            if "to_agent" in r.keys() and r["to_agent"]:
                lines.append(f"[{ts}] {r['from_agent']} → {r['to_agent']}: {r['body']}")
            elif "channel" in r.keys() and r["channel"]:
                lines.append(f"[{ts}] {r['from_agent']} → #{r['channel']}: {r['body']}")
            else:
                lines.append(f"[{ts}] {r['from_agent']}: {r['body']}")
        lines.reverse()
        return "\n".join(lines)

    # -- Utility --

    @mcp.tool()
    def ping(from_agent: str) -> str:
        """Heartbeat — updates your last_seen timestamp.

        Args:
            from_agent: Your agent name.
        """
        now = time.time()
        conn = _get_db(db_path)
        conn.execute("UPDATE agents SET last_seen = ? WHERE name = ?", (now, from_agent))
        conn.commit()
        return f"pong ({time.strftime('%H:%M:%S')})"

    @mcp.tool()
    def hub_status() -> str:
        """Get hub statistics — agents online, channels, message counts."""
        conn = _get_db(db_path)
        agents = conn.execute("SELECT COUNT(*) as c FROM agents WHERE status='online'").fetchone()
        channels = conn.execute("SELECT COUNT(*) as c FROM channels").fetchone()
        messages = conn.execute("SELECT COUNT(*) as c FROM messages").fetchone()
        unread = conn.execute("SELECT COUNT(*) as c FROM messages WHERE read=0").fetchone()
        return (
            f"Agents online: {agents['c']}\n"
            f"Channels: {channels['c']}\n"
            f"Total messages: {messages['c']}\n"
            f"Unread: {unread['c']}"
        )

    return mcp


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        prog="mcp-hub",
        description="Inter-agent messaging hub for Claude sessions",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "streamable-http"],
        default=os.environ.get("MCP_TRANSPORT", "stdio"),
        help="MCP transport (default: stdio, or $MCP_TRANSPORT)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("PORT", "8080")),
        help="Port for SSE/HTTP transport (default: 8080, or $PORT)",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("HOST", "0.0.0.0"),
        help="Host to bind (default: 0.0.0.0, or $HOST)",
    )
    parser.add_argument(
        "--db",
        default="mcp-hub.db",
        help="SQLite database path (default: mcp-hub.db)",
    )
    args = parser.parse_args()

    global DB_PATH
    DB_PATH = Path(args.db)

    server = create_server(DB_PATH, host=args.host, port=args.port)
    server.run(transport=args.transport)


if __name__ == "__main__":
    main()
