"""The boundary between domain errors and what an MCP client is told.

FastMCP catches whatever escapes a tool function and, when
``mask_error_details`` is on, replaces it with a generic message *before* any
middleware sees it. A useful error message therefore has to be produced inside
the tool body, which is what :func:`tool_error_boundary` is for::

    async def my_tool() -> Result:
        async with tool_error_boundary():
            return await client.do_something()

Inside the boundary:

* a :class:`~es_search_mcp.exceptions.SearchMCPError` keeps its stable code and
  its actionable message — the model can tell "retry this" apart from "change
  your arguments";
* anything else is logged with a full traceback and reported as a single
  generic message, so stack traces, URLs and internal identifiers never reach
  the client.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastmcp.exceptions import ToolError

from es_search_mcp.exceptions import SearchMCPError
from es_search_mcp.logging import get_logger

logger = get_logger(__name__)

#: What a client is told when an unexpected error is masked.
GENERIC_ERROR_MESSAGE = (
    "[INTERNAL_ERROR] The search server failed to handle this request. "
    "The failure has been logged; please retry or try a different query."
)


def to_tool_error(exc: Exception, *, mask_unexpected: bool = True) -> ToolError:
    """Translate an exception into the ``ToolError`` a client should receive."""
    if isinstance(exc, ToolError):
        return exc
    if isinstance(exc, SearchMCPError):
        return ToolError(exc.client_message())
    if mask_unexpected:
        return ToolError(GENERIC_ERROR_MESSAGE)
    return ToolError(f"[INTERNAL_ERROR] {exc}")


@asynccontextmanager
async def tool_error_boundary(*, mask_unexpected: bool = True) -> AsyncIterator[None]:
    """Wrap a tool body so every failure leaves it as a classified ``ToolError``.

    Args:
        mask_unexpected: When true, unexpected exceptions are reported to the
            client as :data:`GENERIC_ERROR_MESSAGE`. Turn it off in development
            to see the original message on the wire; the log always has it.
    """
    try:
        yield
    except ToolError:
        # Already shaped for the client — pass it through untouched.
        raise
    except SearchMCPError as exc:
        logger.warning("tool.error", fields=exc.to_dict())
        raise to_tool_error(exc, mask_unexpected=mask_unexpected) from exc
    except Exception as exc:
        logger.exception("tool.unhandled_error", fields={"error_type": type(exc).__name__})
        raise to_tool_error(exc, mask_unexpected=mask_unexpected) from exc
