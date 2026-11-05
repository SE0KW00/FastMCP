"""FastMCP server exposing a backend Elasticsearch search API as MCP tools."""

from es_search_mcp.config import Settings, load_settings
from es_search_mcp.server import create_server, run

__version__ = "0.1.0"

__all__ = ["Settings", "__version__", "create_server", "load_settings", "run"]
