FROM python:3.12-slim
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir -e . \
 && mkdir -p /data
# Bake the deployed commit so /health can report it. Coolify auto-injects
# SOURCE_COMMIT as a build-arg on every build, so declaring the ARG is all
# that's needed — no manual Coolify build-arg config. We surface it as
# MCP_HUB_GIT_SHA (what /health reads). If unset (e.g. a plain local
# `docker build`), /health falls back to the .git dir shipped in the image.
ARG SOURCE_COMMIT=unknown
ENV MCP_HUB_GIT_SHA=${SOURCE_COMMIT}
EXPOSE 8080
CMD ["mcp-hub", "--transport", "streamable-http", "--host", "0.0.0.0", "--port", "8080", "--db", "/data/mcp-hub.db"]
