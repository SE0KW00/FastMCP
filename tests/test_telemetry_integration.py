"""Telemetry end to end: real MCP calls, real spans, real measurements.

The point of these is the claim that makes the whole telemetry module worth
having: FastMCP describes its own spans against the OpenTelemetry *API*, and
installing an SDK is all it takes for them to be recorded. Nothing here fakes a
span — a tool is called over the ASGI app and the exported spans are read back.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx
from opentelemetry import trace
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from es_search_mcp import metrics as metrics_module
from es_search_mcp.logging import configure_logging, get_logger
from es_search_mcp.metrics import reset_instruments
from es_search_mcp.telemetry import use_stable_http_semconv
from tests.constants import BASE_URL
from tests.otel_globals import reset_otel_globals


# --------------------------------------------------------------------------- #
# Providers
#
# The global tracer provider is set-once, so another module that installs one
# would otherwise decide what these tests see. Resetting and reinstalling per
# test keeps them independent of suite order.
# --------------------------------------------------------------------------- #
@pytest.fixture
def spans() -> InMemorySpanExporter:
    reset_otel_globals()
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    try:
        yield exporter
    finally:
        provider.shutdown()
        reset_otel_globals()


@pytest.fixture
def metric_reader(monkeypatch) -> InMemoryMetricReader:
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    monkeypatch.setattr(metrics_module.metrics, "get_meter", provider.get_meter)
    reset_instruments()
    yield reader
    reset_instruments()
    provider.shutdown()


@pytest.fixture
def instrumented_client(client):
    """Instrument just this backend client, as the server does when enabled.

    The semconv opt-in has to happen before the instrumentation is applied,
    exactly as `configure_telemetry` arranges it in the real server.
    """
    use_stable_http_semconv()
    HTTPXClientInstrumentor.instrument_client(client.http_client)
    yield client
    HTTPXClientInstrumentor.uninstrument_client(client.http_client)


def names(exporter: InMemorySpanExporter) -> list[str]:
    return [span.name for span in exporter.get_finished_spans()]


def span_named(exporter: InMemorySpanExporter, needle: str):
    for span in exporter.get_finished_spans():
        if needle in span.name:
            return span
    raise AssertionError(f"no span matching {needle!r} in {names(exporter)}")


# --------------------------------------------------------------------------- #
# Traces
# --------------------------------------------------------------------------- #
@respx.mock
async def test_a_tool_call_produces_a_recorded_mcp_span(spans, mcp_client):
    respx.get(f"{BASE_URL}/indices").mock(return_value=httpx.Response(200, json=[]))
    await mcp_client.call_tool("get_indices", {})

    span = span_named(spans, "get_indices")
    assert span.attributes["mcp.method.name"] == "tools/call"
    assert span.attributes["gen_ai.tool.name"] == "get_indices"
    assert span.attributes["fastmcp.server.name"] == "es-search-mcp"
    assert span.status.is_ok or span.status.status_code.name == "UNSET"


@respx.mock
async def test_a_failing_tool_call_marks_the_span_as_an_error(spans, mcp_client):
    from fastmcp.exceptions import ToolError

    respx.get(f"{BASE_URL}/indices").mock(return_value=httpx.Response(404))
    with pytest.raises(ToolError):
        await mcp_client.call_tool("get_indices", {})

    span = span_named(spans, "get_indices")
    assert span.status.status_code.name == "ERROR"
    assert "error.type" in span.attributes


@respx.mock
async def test_the_retrieval_strategy_is_visible_on_the_span(spans, mcp_client):
    respx.post(f"{BASE_URL}/retrieve-knn").mock(return_value=httpx.Response(200, json=[]))
    await mcp_client.call_tool("retrieve", {"indices": ["faq"], "query": "q", "method": "knn"})

    assert span_named(spans, "retrieve").attributes["gen_ai.tool.name"] == "retrieve"


@respx.mock
async def test_backend_calls_are_child_spans_of_the_tool_call(
    spans, instrumented_client, mcp_client
):
    """One trace has to span the whole path, MCP call down to the HTTP request.

    `instrumented_client` and `mcp_client` resolve to the same backend client,
    so the server under test is the instrumented one.
    """
    respx.get(f"{BASE_URL}/indices").mock(return_value=httpx.Response(200, json=[]))
    await mcp_client.call_tool("get_indices", {})

    tool_span = span_named(spans, "get_indices")
    http_span = span_named(spans, "GET")

    assert http_span.parent is not None
    assert http_span.context.trace_id == tool_span.context.trace_id
    # Current semantic conventions, because `configure_telemetry` opts in. The
    # pre-1.0 default (`http.method`) would not join against the attributes this
    # service puts on its own metrics.
    assert http_span.attributes["http.request.method"] == "GET"
    assert http_span.attributes["http.response.status_code"] == 200


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #
def _points(reader: InMemoryMetricReader) -> dict[str, list]:
    data = reader.get_metrics_data()
    points: dict[str, list] = {}
    for resource_metric in data.resource_metrics if data else []:
        for scope_metric in resource_metric.scope_metrics:
            for metric in scope_metric.metrics:
                points.setdefault(metric.name, []).extend(metric.data.data_points)
    return points


@respx.mock
async def test_a_real_tool_call_is_measured(metric_reader, mcp_client):
    respx.post(f"{BASE_URL}/retrieve-bm25").mock(
        return_value=httpx.Response(200, json={"documents": [{"id": "1"}, {"id": "2"}]})
    )
    await mcp_client.call_tool("retrieve", {"indices": ["faq"], "query": "q", "method": "bm25"})

    points = _points(metric_reader)

    (call,) = points["mcp.server.tool.calls"]
    assert call.attributes["gen_ai.tool.name"] == "retrieve"
    assert call.attributes["outcome"] == "success"

    (duration,) = points["mcp.server.tool.duration"]
    assert duration.count == 1 and duration.sum > 0

    (request,) = points["search.backend.requests"]
    assert request.attributes["url.path"] == "/retrieve-bm25"
    assert request.attributes["http.response.status_code"] == 200

    (documents,) = points["search.documents.returned"]
    assert documents.attributes["search.method"] == "bm25"
    assert documents.sum == 2


@respx.mock
async def test_a_backend_failure_is_measured_with_its_code(metric_reader, mcp_client):
    from fastmcp.exceptions import ToolError

    respx.get(f"{BASE_URL}/indices").mock(return_value=httpx.Response(404))
    with pytest.raises(ToolError):
        await mcp_client.call_tool("get_indices", {})

    points = _points(metric_reader)
    (call,) = points["mcp.server.tool.calls"]
    assert call.attributes["outcome"] == "error"
    assert call.attributes["error.type"] == "BACKEND_NOT_FOUND"

    (request,) = points["search.backend.requests"]
    assert request.attributes["error.type"] == "BACKEND_NOT_FOUND"


@respx.mock
async def test_retries_are_measured(metric_reader, mcp_client):
    respx.get(f"{BASE_URL}/indices").mock(
        side_effect=[httpx.Response(503), httpx.Response(200, json=[])]
    )
    await mcp_client.call_tool("get_indices", {})

    (retry,) = _points(metric_reader)["search.backend.retries"]
    assert retry.value == 1
    assert retry.attributes["error.type"] == "BACKEND_SERVER_ERROR"


# --------------------------------------------------------------------------- #
# Log / trace correlation
# --------------------------------------------------------------------------- #
@respx.mock
async def test_logs_emitted_during_a_call_carry_the_trace_context(capsys, spans, mcp_client):
    """This is what links a log line in Elastic back to its APM trace."""
    respx.get(f"{BASE_URL}/indices").mock(return_value=httpx.Response(200, json=[]))
    configure_logging(level="INFO", log_format="json")

    await mcp_client.call_tool("get_indices", {})

    entries = [
        json.loads(line) for line in capsys.readouterr().err.splitlines() if line.startswith("{")
    ]
    correlated = [entry for entry in entries if "trace.id" in entry]
    assert correlated, f"no correlated log lines in {[e['event'] for e in entries]}"

    trace_ids = {entry["trace.id"] for entry in correlated}
    assert trace_ids == {format(span_named(spans, "get_indices").context.trace_id, "032x")}
    assert all(entry["span.id"] for entry in correlated)


def test_logs_outside_a_span_carry_no_trace_fields(capsys):
    """An all-zero id would be worse than no field at all."""
    configure_logging(level="INFO", log_format="json")
    get_logger(__name__).info("standalone.event")

    entry = json.loads(capsys.readouterr().err.strip())
    assert "trace.id" not in entry
    assert "span.id" not in entry
