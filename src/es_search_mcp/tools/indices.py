"""``list_indices`` — discover which Elasticsearch indices can be searched."""

# NOTE: this module deliberately does not use `from __future__ import annotations`.
# Tool signatures are evaluated at registration time to build the MCP input schema,
# and the annotations below close over `settings`, which a deferred (string)
# annotation could not resolve.

from fastmcp import FastMCP

from es_search_mcp.backend import SearchBackendClient
from es_search_mcp.config import Settings
from es_search_mcp.credentials import resolve_credentials
from es_search_mcp.errors import tool_error_boundary
from es_search_mcp.logging import get_logger
from es_search_mcp.models import IndexList

logger = get_logger(__name__)


def register(mcp: FastMCP, client: SearchBackendClient, settings: Settings) -> None:
    """Register the index-discovery tool on ``mcp``."""

    @mcp.tool(
        name="list_indices",
        title="List searchable indices",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"search", "discovery"},
    )
    async def list_indices() -> IndexList:
        """List the Elasticsearch indices available for search.

        Call this first, before `retrieve_documents`: it returns each index name
        together with a description of what the index contains, which is how you
        choose the right `index` argument for a search.

        Returns:
            The available indices, each with `name`, `description` and, when the
            backend reports it, `document_count`.
        """
        async with tool_error_boundary(mask_unexpected=settings.mask_error_details):
            credentials = resolve_credentials(settings)
            result = await client.list_indices(credentials=credentials)
            logger.info("tool.list_indices.result", fields={"index_count": result.total})
            return result
