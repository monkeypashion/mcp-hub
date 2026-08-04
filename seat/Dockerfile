# mcp-hub-seat — one container = one claude seat. Contract: docs/seat-image.md.
#
# Build FROM THE REPO ROOT (the mcp-hub source is the context):
#   docker build -f seat/Dockerfile -t mcp-hub-seat:latest .
#
# NOT the root Dockerfile — that one is the hub's Coolify deploy. This image
# never runs the hub; it runs a claude seat that TALKS to one.
FROM python:3.12-slim

# git: SEAT_REPO clone + origin-derived project; tmux: the attachable
# session hub push-wake lands in; curl+ca-certificates: the claude
# installer; procps: `ps` for tmux and live debugging.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git tmux curl ca-certificates procps \
    && rm -rf /var/lib/apt/lists/*

# mcp-hub itself: seat-entry (PID 1), the hooks the settings file names
# (stop-hook / session-start / heartbeat-daemon), and memory-export for the
# edge's harvest (`docker exec <seat> mcp-hub memory-export`).
COPY . /opt/mcp-hub
RUN pip install --no-cache-dir /opt/mcp-hub

# Non-root. claude installs per-user; PATH gains ~/.local/bin for it.
RUN useradd -m -s /bin/bash seat
USER seat
ENV PATH="/home/seat/.local/bin:${PATH}"
RUN curl -fsSL https://claude.ai/install.sh | bash \
    && claude --version

# WORKDIR is the seat workdir — LOAD-BEARING for harvest: the edge's
# `docker exec` lands here, and memory-export resolves identity from cwd
# (the marker seat-entry writes lives at ./.claude/hub-agent.json).
WORKDIR /home/seat/work

# The contract door: env in (SEAT_IDENTITY, MCP_HUB_URL, credential...),
# marker + .mcp.json + hook settings out, claude under tmux 'seat'.
# Exit 42 = credential refused, 43 = contract refused — at the door,
# loudly, where the edge's enumeration sees an exited container.
ENTRYPOINT ["mcp-hub", "seat-entry"]
