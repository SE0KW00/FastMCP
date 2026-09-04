"""Assembly of the FastMCP server.

``create_server`` is the single composition root: it builds the settings, the
backend client, the middleware stack and the tools, and wires the client's
lifetime to the server's lifespan. Everything below this module is free of
global state, which keeps each piece testable on its own.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastmcp import FastMCP

from es_search_mcp.backend import SearchBackendClient
from es_search_mcp.config import Settings, load_settings
from es_search_mcp.logging import configure_logging, get_logger
from es_search_mcp.middleware import CallLoggingMiddleware, ErrorHandlingMiddleware
from es_search_mcp.tools import register_index_tools, register_retrieve_tools

logger = get_logger(__name__)

SERVER_NAME = "es-search-mcp"

SERVER_INSTRUCTIONS = """\
This server searches a document corpus stored in Elasticsearch.

Workflow:
1. Call `list_indices` to see which indices exist and what each one contains.
2. Call `retrieve_documents` with the index that matches the user's question.

`retrieve_documents` supports four retrieval strategies through its `method`
argument — `rrf` (default, hybrid), `bm25` (lexical), `knn` (vector) and `cc`
(hybrid by score blending). Leave it at the default unless the question clearly
favours exact terms or paraphrases.

Every tool is read-only; nothing here modifies the corpus. Errors are returned
with a stable `[CODE]` prefix — a code marked as retryable is worth one more
attempt, any other code needs a changed request. `[MISSING_CREDENTIALS]` means
the MCP client did not send the credential headers this server forwards to the
backend; that is a client configuration problem, not something to retry.
"""


def create_server(
    settings: Settings | None = None,
    *,
    client: SearchBackendClient | None = None,
) -> FastMCP:
    """Build a fully configured server.

    Args:
        settings: Configuration to use. Loaded from the environment when omitted.
        client: Pre-built backend client, mainly for tests. When supplied, its
            lifetime is owned by the caller rather than by the server lifespan.

    Returns:
        A :class:`FastMCP` instance with middleware and tools registered.
    """
    settings = settings or load_settings()
    configure_logging(level=settings.log_level, log_format=settings.log_format)

    owns_client = client is None
    backend_client = client or SearchBackendClient(settings)

    @asynccontextmanager
    async def lifespan(_: FastMCP) -> AsyncIterator[dict[str, object]]:
        logger.info(
            "server.starting",
            fields={
                "server": SERVER_NAME,
                "backend_base_url": settings.backend_base_url,
                "transport": settings.transport,
            },
        )
        try:
            yield {"settings": settings, "backend_client": backend_client}
        finally:
            if owns_client:
                await backend_client.aclose()
            logger.info("server.stopped", fields={"server": SERVER_NAME})

    mcp: FastMCP = FastMCP(
        name=SERVER_NAME,
        instructions=SERVER_INSTRUCTIONS,
        version="0.1.0",
        lifespan=lifespan,
        mask_error_details=settings.mask_error_details,
        middleware=[
            # Logging is outermost so it observes exceptions before the error
            # middleware rewrites them into a ToolError.
            CallLoggingMiddleware(),
            ErrorHandlingMiddleware(mask_unexpected=settings.mask_error_details),
        ],
    )

    register_index_tools(mcp, backend_client, settings)
    register_retrieve_tools(mcp, backend_client, settings)
    return mcp


def run() -> None:
    """Create and serve the MCP server using the configured transport."""
    settings = load_settings()
    mcp = create_server(settings)

    # The banner would interleave with the structured log stream on stderr;
    # startup is already reported by the `server.starting` event.
    if settings.transport == "stdio":
        mcp.run(transport="stdio", show_banner=False)
    else:
        mcp.run(
            transport=settings.transport,
            host=settings.host,
            port=settings.port,
            show_banner=False,
        )
