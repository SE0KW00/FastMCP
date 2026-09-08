# syntax=docker/dockerfile:1
#
# es-search-mcp — MCP server fronting the Elasticsearch search API.
#
#   docker build -t es-search-mcp:0.1.0 .
#   docker run --rm -p 8080:8080 \
#     -e ES_MCP_BACKEND_BASE_URL=https://search-api.internal \
#     es-search-mcp:0.1.0
#
# The server speaks MCP over Streamable HTTP at http://<host>:8080/mcp/.
# Callers send their own credentials as request headers (Authorization and
# X-API-Key); this image holds no secrets and needs none at build time.

ARG PYTHON_VERSION=3.12


# --------------------------------------------------------------------------- #
# Stage 1 — build the virtualenv
# --------------------------------------------------------------------------- #
FROM python:${PYTHON_VERSION}-slim AS builder

# `uv` comes from PyPI rather than a second registry, so the whole build needs
# only the Python base image and a package index — one less thing to mirror or
# allow through a corporate proxy.
RUN pip install --no-cache-dir uv==0.8.17

ENV UV_PROJECT_ENVIRONMENT=/app/.venv \
    # Pin the interpreter to the one this image ships. Left to itself uv may
    # fetch a standalone build, and the venv would then point at an interpreter
    # that does not exist in the runtime stage.
    UV_PYTHON=/usr/local/bin/python \
    UV_PYTHON_DOWNLOADS=never \
    # Ship .pyc files so the first request does not pay for compilation.
    UV_COMPILE_BYTECODE=1 \
    UV_NO_CACHE=1

WORKDIR /app

# Dependencies first, without the project itself: this layer is rebuilt only
# when the lockfile changes, so ordinary source edits reuse it.
# (README.md is listed as the project readme, so the metadata read needs it.)
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

# Then the source. `--no-editable` installs a real copy into the venv, so the
# runtime stage needs nothing but the venv itself.
COPY src/ ./src/
RUN uv sync --frozen --no-dev --no-editable


# --------------------------------------------------------------------------- #
# Stage 2 — runtime
# --------------------------------------------------------------------------- #
FROM python:${PYTHON_VERSION}-slim AS runtime

LABEL org.opencontainers.image.title="es-search-mcp" \
      org.opencontainers.image.description="MCP server wrapping an Elasticsearch-backed search API" \
      org.opencontainers.image.source="https://github.com/SE0KW00/FastMCP"

# Unprivileged, no shell, no home: the process only needs to read its own code
# and open a socket.
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin app

# The venv must land on the same path it was built at — console scripts inside
# it carry an absolute shebang.
COPY --from=builder --chown=root:root /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # Container defaults. Every one of these is overridable with `-e`.
    ES_MCP_TRANSPORT=http \
    # 127.0.0.1 (the app default) would only be reachable from inside the
    # container; published ports need a bind on all interfaces.
    ES_MCP_HOST=0.0.0.0 \
    ES_MCP_PORT=8080 \
    ES_MCP_LOG_FORMAT=json \
    ES_MCP_LOG_LEVEL=INFO

WORKDIR /app
USER 10001

EXPOSE 8080

# There is no unauthenticated HTTP endpoint to probe — an MCP call needs a
# session and the caller's credentials — so this checks that the server is
# listening, which is what a container-level liveness check can honestly assert.
# For a readiness probe that also proves the backend is reachable, add a
# `/health` route to the app and curl it here instead.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import os,socket; socket.create_connection(('127.0.0.1', int(os.environ.get('ES_MCP_PORT', '8080'))), timeout=3).close()"]

# Exec form, so the server is PID 1 and receives SIGTERM directly on `docker
# stop` — uvicorn then drains connections instead of being killed after the
# grace period.
ENTRYPOINT ["python", "-m", "es_search_mcp"]
