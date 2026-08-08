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
# alsa-utils: arecord, which /voice records with. libasound2-plugins: the
# ALSA->pulse pcm plugin — the piece everyone forgets, and without it arecord
# fails with "audio open error: No such file or directory". pulseaudio-utils:
# pacat, to feed the container's OWN sink. pulseaudio: that sink's server,
# which is per-container and reaches nothing outside it.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git tmux curl ca-certificates procps \
        alsa-utils libasound2-plugins pulseaudio-utils pulseaudio \
    && rm -rf /var/lib/apt/lists/*

# ALSA's default device IS the container's own pulse server. Baked into the
# IMAGE, not injected per container: this is the half of /voice that is
# automatic by construction, so a seat cannot exist without it
# (docs/seat-voice.md, "how this gets into EVERY container").
RUN mkdir -p /etc/alsa/conf.d && printf '%s\n' \
        'pcm.!default { type pulse }' \
        'ctl.!default { type pulse }' \
        > /etc/alsa/conf.d/99-pulseaudio-default.conf

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
# mkdir'd as the seat user FIRST: the WORKDIR directive creates missing
# dirs as ROOT even after USER, leaving PID 1 unable to write its own
# workdir (found by the first container smoke test, not by unit tests —
# they run as one user on the host and cannot see ownership).
RUN mkdir -p /home/seat/work

# Point libpulse at the container's OWN server by ABSOLUTE path. Load-bearing:
# tmux-launched agents often lack a usable XDG_RUNTIME_DIR, and libpulse then
# finds nothing — a trap that already cost time on the fireblade rig.
RUN mkdir -p /home/seat/.config/pulse && printf '%s\n' \
        'default-server = unix:/run/user/1000/pulse/native' \
        'autospawn = no' \
        > /home/seat/.config/pulse/client.conf

# 🔴 THE SERVER THIS IMAGE INSTALLS MUST ALSO BE STARTED, AND WITH A SINK.
# Until 2026-08-08 the package was installed, the comment above said "that
# sink's server" — and NOTHING EVER RAN IT. Measured in a throwaway container
# from this Dockerfile (mcp-hub-dev-vm-1-general): `pactl info`, `pacat` and
# `arecord` all failed with "Connection refused". Every piece present, nothing
# running: a comment asserting a component is not a component.
#
# `autospawn = no` above stays deliberately. Autospawn would start a server
# from the DEFAULT script, which hunts for hardware a container does not have
# and creates NO null sink — so `pacat` would still have nowhere to play, and
# would fail more confusingly than it does now. The server is started
# explicitly by seat-entry instead, in a known order, with this script.
#
# `-n --file=` means ONLY these modules load: no udev, no hardware detection,
# no bluetooth. A container has no devices, so the default script is all
# failure modes and no benefit.
RUN mkdir -p /etc/pulse && printf '%s\n' \
        '# The container half of the /voice chain, mirroring the host rig:' \
        '# a null sink to play the host stream INTO, and its monitor as the' \
        '# default source, so arecord (and therefore /voice) reads it back.' \
        'load-module module-native-protocol-unix' \
        'load-module module-null-sink sink_name=claude_mic rate=16000 channels=1 sink_properties=device.description=claude_mic' \
        'set-default-sink claude_mic' \
        'set-default-source claude_mic.monitor' \
        > /etc/pulse/seat-voice.pa

# XDG_RUNTIME_DIR must EXIST and be owned by the seat, because client.conf
# above names `/run/user/1000/pulse/native` by absolute path and the server
# creates its socket under $XDG_RUNTIME_DIR. Created at build time: /run is a
# plain image directory here, so this persists into every container.
ENV XDG_RUNTIME_DIR=/run/user/1000
RUN mkdir -p /run/user/1000 && chown seat:seat /run/user/1000 \
    && chmod 700 /run/user/1000

WORKDIR /home/seat/work

# The contract door: env in (SEAT_IDENTITY, MCP_HUB_URL, credential...),
# marker + .mcp.json + hook settings out, claude under tmux 'seat'.
# Exit 42 = credential refused, 43 = contract refused — at the door,
# loudly, where the edge's enumeration sees an exited container.
ENTRYPOINT ["mcp-hub", "seat-entry"]
