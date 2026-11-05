"""Tool registration for the search MCP server.

Each module exposes a ``register(mcp, client, settings)`` function. Dependencies
are passed in explicitly rather than reached for through globals, so a tool can
be exercised in tests against a stub client.
"""

from es_search_mcp.tools.indices import register as register_index_tools
from es_search_mcp.tools.retrieve import register as register_retrieve_tools

__all__ = ["register_index_tools", "register_retrieve_tools"]
