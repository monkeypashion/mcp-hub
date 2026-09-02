FROM python:3.12-slim
WORKDIR /app
COPY . .
# 🔴 THE LOCK IS INSTALLED FIRST, AND THE PACKAGE WITH --no-deps.
# `pip install -e .` alone re-resolves the whole closure from PyPI at BUILD
# time, so identical source ships different libraries. That is not a
# hypothetical: on 2026-09-01 a rebuild resolved fastmcp 4.0.0 and
# crash-looped, ~40 min down, and the commit being deployed was irrelevant
# (see pyproject's note). Measured again 2026-09-02: two builds of the SAME
# commit 90 minutes apart resolved anyio 4.14.2 then 4.15.0.
# Without --no-deps pip re-resolves and the lock becomes decoration.
# `pip check` fails the build loudly if the lock and pyproject disagree —
# the alternative is a build that succeeds while shipping versions
# pyproject would reject.
RUN pip install --no-cache-dir -r requirements.lock \
 && pip install --no-cache-dir --no-deps -e . \
 && pip check \
 && mkdir -p /data
# Bake the deployed commit so /health can report it. Coolify can pass
# --build-arg GIT_SHA=<commit>; if unset, /health falls back to reading the
# .git dir shipped in the image.
ARG GIT_SHA=unknown
ENV MCP_HUB_GIT_SHA=${GIT_SHA}
EXPOSE 8080
CMD ["mcp-hub", "--transport", "streamable-http", "--host", "0.0.0.0", "--port", "8080", "--db", "/data/mcp-hub.db"]
