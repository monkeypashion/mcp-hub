FROM python:3.12-slim
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir -e . \
 && mkdir -p /data
# Bake the deployed commit so /health can report it. Coolify can pass
# --build-arg GIT_SHA=<commit>; if unset, /health falls back to reading the
# .git dir shipped in the image.
ARG GIT_SHA=unknown
ENV MCP_HUB_GIT_SHA=${GIT_SHA}
EXPOSE 8080
CMD ["mcp-hub", "--transport", "streamable-http", "--host", "0.0.0.0", "--port", "8080", "--db", "/data/mcp-hub.db"]
