"""Application metrics.

FastMCP's own instrumentation covers traces but emits no metrics, so the
instruments the dashboards need are declared here. They are created against the
OpenTelemetry **API**, which hands back no-op instruments until
:func:`es_search_mcp.telemetry.configure_telemetry` installs a provider — so
recording is safe and cheap whether or not export is switched on, and nothing
has to be threaded through the call graph to make it work.

Instruments are built on first use rather than at import, so they bind to the
provider the server installed rather than to whatever was (or was not) in place
when this module happened to be imported.

Attribute names follow OpenTelemetry semantic conventions where one exists
(``gen_ai.tool.name``, ``error.type``, ``http.request.method``,
``http.response.status_code``) and are prefixed with ``search.`` where the
concept is this service's own.
"""

from __future__ import annotations

from functools import cache

from opentelemetry import metrics
from opentelemetry.metrics import Counter, Histogram

INSTRUMENTATION_NAME = "es_search_mcp"

#: Explicit buckets for request latencies, in seconds. The default bucket
#: boundaries bunch up below 10s, which hides the difference between a fast
#: lexical search and a slow hybrid one; these spread across the range a search
#: backend actually spans.
_LATENCY_BUCKETS = [0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0]

#: Buckets for "how many documents came back", bounded by the configurable cap.
_COUNT_BUCKETS = [0, 1, 2, 5, 10, 20, 50, 100]


class _Instruments:
    """The service's instruments, built once against the active provider."""

    def __init__(self) -> None:
        meter = metrics.get_meter(INSTRUMENTATION_NAME)

        self.tool_calls: Counter = meter.create_counter(
            "mcp.server.tool.calls",
            unit="{call}",
            description="MCP tool calls handled, by tool and outcome.",
        )
        self.tool_duration: Histogram = meter.create_histogram(
            "mcp.server.tool.duration",
            unit="s",
            description="Wall time spent handling an MCP tool call.",
            explicit_bucket_boundaries_advisory=_LATENCY_BUCKETS,
        )
        self.backend_requests: Counter = meter.create_counter(
            "search.backend.requests",
            unit="{request}",
            description="HTTP requests made to the search backend, by outcome.",
        )
        self.backend_duration: Histogram = meter.create_histogram(
            "search.backend.duration",
            unit="s",
            description="Time the search backend took to answer one request.",
            explicit_bucket_boundaries_advisory=_LATENCY_BUCKETS,
        )
        self.backend_retries: Counter = meter.create_counter(
            "search.backend.retries",
            unit="{retry}",
            description="Retries of a transient backend failure, by error code.",
        )
        self.documents_returned: Histogram = meter.create_histogram(
            "search.documents.returned",
            unit="{document}",
            description="Documents returned by a retrieval, by strategy.",
            explicit_bucket_boundaries_advisory=_COUNT_BUCKETS,
        )


@cache
def instruments() -> _Instruments:
    """Return the process-wide instruments, creating them on first use."""
    return _Instruments()


def reset_instruments() -> None:
    """Drop the cached instruments so the next call rebinds to a new provider.

    Only needed by tests, which install a fresh in-memory provider per case.
    """
    instruments.cache_clear()


# --------------------------------------------------------------------------- #
# Recording helpers
#
# Each one keeps the attribute set for a given measurement in one place, so a
# dashboard cannot be broken by one call site spelling an attribute differently.
# --------------------------------------------------------------------------- #
def record_tool_call(
    *,
    tool_name: str,
    duration_seconds: float,
    outcome: str,
    error_code: str | None = None,
) -> None:
    """Record the completion of one MCP tool call.

    Args:
        tool_name: Name of the tool as the client called it.
        duration_seconds: Wall time spent handling the call.
        outcome: ``success``, ``error`` (classified) or ``unhandled_error``.
        error_code: The stable ``[CODE]`` of a classified failure, when there
            is one. Recorded as ``error.type`` to match semantic conventions.
    """
    attributes: dict[str, str] = {"gen_ai.tool.name": tool_name, "outcome": outcome}
    if error_code:
        attributes["error.type"] = error_code

    recorders = instruments()
    recorders.tool_calls.add(1, attributes)
    recorders.tool_duration.record(duration_seconds, attributes)


def record_backend_request(
    *,
    http_method: str,
    path: str,
    duration_seconds: float,
    status_code: int | None = None,
    error_code: str | None = None,
) -> None:
    """Record one HTTP call to the search backend, successful or not.

    ``path`` is the configured endpoint path, never a caller-supplied value, so
    it stays a bounded set and cannot blow up the metric's cardinality.
    """
    attributes: dict[str, str | int] = {"http.request.method": http_method, "url.path": path}
    if status_code is not None:
        attributes["http.response.status_code"] = status_code
    if error_code:
        attributes["error.type"] = error_code
    attributes["outcome"] = "error" if error_code else "success"

    recorders = instruments()
    recorders.backend_requests.add(1, attributes)
    recorders.backend_duration.record(duration_seconds, attributes)


def record_backend_retry(*, path: str, error_code: str, attempt: int) -> None:
    """Record that a transient backend failure is about to be retried."""
    instruments().backend_retries.add(
        1, {"url.path": path, "error.type": error_code, "retry.attempt": attempt}
    )


def record_documents_returned(*, method: str, count: int) -> None:
    """Record how many documents a retrieval produced, by strategy."""
    instruments().documents_returned.record(count, {"search.method": method})
