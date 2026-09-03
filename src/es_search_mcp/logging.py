"""Structured logging for the search MCP server.

Two things make MCP logging different from ordinary service logging:

1. On the ``stdio`` transport, **stdout belongs to the MCP protocol**. Anything
   written there corrupts the JSON-RPC stream, so every handler installed here
   writes to ``stderr``.
2. A tool call is the unit of work worth correlating. ``request_id``,
   ``tool_name`` and ``client_id`` are kept in context variables and attached to
   every record emitted while a call is in flight, so a single call can be
   reconstructed from the log stream without threading a logger through the code.

Usage::

    log = get_logger(__name__)
    log.info("backend.request", fields={"method": "GET", "path": "/indices"})
"""

from __future__ import annotations

import json
import logging
import sys
import uuid
from collections.abc import Iterator, MutableMapping
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

LOGGER_NAMESPACE = "es_search_mcp"

#: Field names whose values are never written to the log stream.
SENSITIVE_KEYS = frozenset({"api_key", "apikey", "authorization", "password", "secret", "token"})

_REDACTED = "***"

#: Attributes present on every ``LogRecord``; anything else was added by us.
_RESERVED_RECORD_ATTRS = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__)

_request_id: ContextVar[str | None] = ContextVar("es_mcp_request_id", default=None)
_tool_name: ContextVar[str | None] = ContextVar("es_mcp_tool_name", default=None)
_client_id: ContextVar[str | None] = ContextVar("es_mcp_client_id", default=None)


# --------------------------------------------------------------------------- #
# Call context
# --------------------------------------------------------------------------- #
def new_request_id() -> str:
    """Return a short, unique identifier for one tool call."""
    return uuid.uuid4().hex[:16]


def current_request_id() -> str | None:
    """Return the request id bound to the current context, if any."""
    return _request_id.get()


def current_call_context() -> dict[str, str]:
    """Return the currently bound call context as a plain dict."""
    context = {
        "request_id": _request_id.get(),
        "tool_name": _tool_name.get(),
        "client_id": _client_id.get(),
    }
    return {key: value for key, value in context.items() if value is not None}


@contextmanager
def bind_call_context(
    *,
    request_id: str | None = None,
    tool_name: str | None = None,
    client_id: str | None = None,
) -> Iterator[str]:
    """Bind call metadata for the duration of the block.

    Yields the effective request id so callers can propagate it downstream (for
    example as an ``X-Request-ID`` header).
    """
    resolved_id = request_id or new_request_id()
    tokens = [
        (_request_id, _request_id.set(resolved_id)),
        (_tool_name, _tool_name.set(tool_name)),
        (_client_id, _client_id.set(client_id)),
    ]
    try:
        yield resolved_id
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


# --------------------------------------------------------------------------- #
# Redaction
# --------------------------------------------------------------------------- #
def redact(value: Any, *, _depth: int = 0) -> Any:
    """Recursively replace values of sensitive keys with a placeholder."""
    if _depth > 6:
        return value
    if isinstance(value, dict):
        return {
            key: _REDACTED
            if str(key).lower() in SENSITIVE_KEYS
            else redact(item, _depth=_depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item, _depth=_depth + 1) for item in value]
    return value


# --------------------------------------------------------------------------- #
# Formatters and filters
# --------------------------------------------------------------------------- #
class CallContextFilter(logging.Filter):
    """Attach the bound call context to every record passing through."""

    def filter(self, record: logging.LogRecord) -> bool:
        for key, value in current_call_context().items():
            if not hasattr(record, key):
                setattr(record, key, value)
        return True


def _record_fields(record: logging.LogRecord) -> dict[str, Any]:
    """Collect the structured fields carried by a record."""
    fields: dict[str, Any] = {}
    for key, value in record.__dict__.items():
        if key in _RESERVED_RECORD_ATTRS or key.startswith("_") or key == "taskName":
            continue
        if key == "fields" and isinstance(value, dict):
            fields.update(value)
        else:
            fields[key] = value
    return fields


class JsonFormatter(logging.Formatter):
    """Render records as one JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        payload.update(redact(_record_fields(record)))
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):
    """Render records as a compact, human-readable line."""

    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)-8s %(name)s | %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        fields = redact(_record_fields(record))
        if not fields:
            return base
        rendered = " ".join(f"{key}={value}" for key, value in sorted(fields.items()))
        return f"{base} | {rendered}"


# --------------------------------------------------------------------------- #
# Setup
# --------------------------------------------------------------------------- #
class StderrHandler(logging.StreamHandler[Any]):
    """Stream handler that resolves ``sys.stderr`` at emit time.

    Binding the stream once would pin whatever ``sys.stderr`` happened to be
    installed when logging was configured — a problem whenever the process
    replaces it later (a supervisor reopening the log file, a test harness
    capturing output). Resolving on each write keeps records on the real
    ``stderr`` and, just as importantly, off ``stdout``.
    """

    def __init__(self) -> None:
        super().__init__(sys.stderr)

    @property
    def stream(self) -> Any:
        return sys.stderr

    @stream.setter
    def stream(self, value: Any) -> None:
        """Ignore assignment; the stream is always the current ``sys.stderr``."""


def configure_logging(level: str = "INFO", log_format: str = "json") -> None:
    """Install the server's logging handler.

    Safe to call more than once: existing handlers on the namespace logger are
    replaced rather than stacked. Records are not propagated to the root logger,
    so an embedding application's configuration cannot duplicate or reroute them
    onto stdout.
    """
    handler = StderrHandler()
    handler.setFormatter(JsonFormatter() if log_format == "json" else TextFormatter())
    handler.addFilter(CallContextFilter())

    logger = logging.getLogger(LOGGER_NAMESPACE)
    for existing in list(logger.handlers):
        logger.removeHandler(existing)
        existing.close()
    logger.addHandler(handler)
    logger.setLevel(level.upper())
    logger.propagate = False

    # Third-party loggers are noisy at DEBUG and would otherwise leak onto the
    # root handler; route them through the same handler at a calmer level.
    for name in ("fastmcp", "httpx", "httpcore", "mcp"):
        third_party = logging.getLogger(name)
        for existing in list(third_party.handlers):
            third_party.removeHandler(existing)
        third_party.addHandler(handler)
        third_party.setLevel(max(logging.getLevelName(level.upper()), logging.WARNING))
        third_party.propagate = False


class StructuredLogger(logging.LoggerAdapter[logging.Logger]):
    """Logger adapter that accepts a ``fields=`` mapping on every call."""

    def process(
        self, msg: Any, kwargs: MutableMapping[str, Any]
    ) -> tuple[Any, MutableMapping[str, Any]]:
        fields: dict[str, Any] = dict(self.extra or {})
        supplied = kwargs.pop("fields", None)
        if isinstance(supplied, dict):
            fields.update(supplied)
        extra = dict(kwargs.get("extra") or {})
        extra["fields"] = fields
        kwargs["extra"] = extra
        return msg, kwargs


def get_logger(name: str | None = None) -> StructuredLogger:
    """Return a structured logger inside the server's logging namespace."""
    if name and not name.startswith(LOGGER_NAMESPACE):
        name = f"{LOGGER_NAMESPACE}.{name.rsplit('.', 1)[-1]}"
    return StructuredLogger(logging.getLogger(name or LOGGER_NAMESPACE), {})
