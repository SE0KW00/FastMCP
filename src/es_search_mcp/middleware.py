"""Cross-cutting middleware: call logging and centralised error handling.

FastMCP middleware wraps every tool call, which makes it the right place for
concerns that must not be repeated in each tool:

``CallLoggingMiddleware``
    Opens a correlation scope, logs a start/finish pair with a duration, and
    keeps failures visible in the log stream even when their details are masked
    on the wire.

``ErrorHandlingMiddleware``
    A backstop that classifies anything raised *around* a tool body — by another
    middleware, or by output validation — into a coded ``ToolError``. Errors
    raised *inside* a tool are handled one layer down, by
    :func:`es_search_mcp.errors.tool_error_boundary`, because FastMCP masks them
    before middleware runs.

Order matters: logging is registered first so that it wraps the error
middleware and observes the outcome of every call.
"""

from __future__ import annotations

import time
from typing import Any

import mcp_types
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext

from es_search_mcp.errors import to_tool_error
from es_search_mcp.exceptions import SearchMCPError
from es_search_mcp.logging import bind_call_context, get_logger

logger = get_logger(__name__)


def _tool_name(context: MiddlewareContext[Any]) -> str:
    message = context.message
    return getattr(message, "name", None) or context.method or "unknown"


def _code_from_message(message: str) -> str | None:
    """Recover the ``[CODE]`` prefix that the error boundary writes."""
    if message.startswith("[") and "]" in message:
        return message[1 : message.index("]")]
    return None


def _client_id(context: MiddlewareContext[Any]) -> str | None:
    fastmcp_context = context.fastmcp_context
    if fastmcp_context is None:
        return None
    try:
        return fastmcp_context.client_id
    except Exception:  # pragma: no cover - context not always available
        return None


class CallLoggingMiddleware(Middleware):
    """Log every tool call with a correlation id and a duration."""

    async def on_call_tool(
        self,
        context: MiddlewareContext[mcp_types.CallToolRequestParams],
        call_next: CallNext[mcp_types.CallToolRequestParams, Any],
    ) -> Any:
        tool = _tool_name(context)
        with bind_call_context(tool_name=tool, client_id=_client_id(context)):
            started = time.perf_counter()
            logger.info(
                "tool.call.start",
                fields={"arguments": getattr(context.message, "arguments", None) or {}},
            )
            try:
                result = await call_next(context)
            except SearchMCPError as exc:
                # Raised around the tool body; the boundary inside it has not run.
                logger.warning(
                    "tool.call.failed",
                    fields={
                        "duration_ms": self._elapsed_ms(started),
                        "outcome": "error",
                        **exc.to_dict(),
                    },
                )
                raise
            except ToolError as exc:
                # Already classified — record the outcome, the detail was logged
                # where the failure happened.
                logger.warning(
                    "tool.call.failed",
                    fields={
                        "duration_ms": self._elapsed_ms(started),
                        "outcome": "error",
                        "code": _code_from_message(str(exc)),
                        "message": str(exc),
                    },
                )
                raise
            except Exception:
                logger.exception(
                    "tool.call.crashed",
                    fields={
                        "duration_ms": self._elapsed_ms(started),
                        "outcome": "unhandled_error",
                    },
                )
                raise
            logger.info(
                "tool.call.finished",
                fields={"duration_ms": self._elapsed_ms(started), "outcome": "success"},
            )
            return result

    @staticmethod
    def _elapsed_ms(started: float) -> float:
        return round((time.perf_counter() - started) * 1000, 2)


class ErrorHandlingMiddleware(Middleware):
    """Classify errors raised around a tool body into a client-facing ``ToolError``."""

    def __init__(self, *, mask_unexpected: bool = True) -> None:
        self._mask_unexpected = mask_unexpected

    async def on_call_tool(
        self,
        context: MiddlewareContext[mcp_types.CallToolRequestParams],
        call_next: CallNext[mcp_types.CallToolRequestParams, Any],
    ) -> Any:
        try:
            return await call_next(context)
        except ToolError:
            # Already shaped for the client — pass it through untouched.
            raise
        except Exception as exc:
            raise to_tool_error(exc, mask_unexpected=self._mask_unexpected) from exc
