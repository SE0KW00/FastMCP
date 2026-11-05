"""Console entry point: ``python -m es_search_mcp`` / ``es-search-mcp``."""

from __future__ import annotations

import sys

from es_search_mcp.exceptions import SearchMCPError
from es_search_mcp.logging import configure_logging, get_logger
from es_search_mcp.server import run


def main() -> int:
    """Start the server, turning startup failures into a clean exit code."""
    try:
        run()
    except KeyboardInterrupt:
        return 0
    except SearchMCPError as exc:
        configure_logging()
        get_logger(__name__).error("server.startup_failed", fields=exc.to_dict())
        return 1
    except Exception:
        configure_logging()
        get_logger(__name__).exception("server.crashed")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
