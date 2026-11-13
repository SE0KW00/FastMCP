"""Metrics instruments and the attributes each measurement carries."""

from __future__ import annotations

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from es_search_mcp import metrics as metrics_module
from es_search_mcp.metrics import (
    record_backend_request,
    record_backend_retry,
    record_documents_returned,
    record_tool_call,
    reset_instruments,
)


@pytest.fixture
def reader(monkeypatch) -> InMemoryMetricReader:
    """Point the instruments at an in-memory reader for the duration of a test."""
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    monkeypatch.setattr(metrics_module.metrics, "get_meter", provider.get_meter)
    reset_instruments()
    yield reader
    reset_instruments()
    provider.shutdown()


def collect(reader: InMemoryMetricReader) -> dict[str, list]:
    """Return {metric name: [data points]} from one collection cycle."""
    data = reader.get_metrics_data()
    points: dict[str, list] = {}
    for resource_metric in data.resource_metrics if data else []:
        for scope_metric in resource_metric.scope_metrics:
            for metric in scope_metric.metrics:
                points.setdefault(metric.name, []).extend(metric.data.data_points)
    return points


# --------------------------------------------------------------------------- #
# Tool calls
# --------------------------------------------------------------------------- #
def test_a_successful_call_counts_once_and_records_its_duration(reader):
    record_tool_call(tool_name="retrieve", duration_seconds=0.25, outcome="success")

    points = collect(reader)
    (call,) = points["mcp.server.tool.calls"]
    (duration,) = points["mcp.server.tool.duration"]

    assert call.value == 1
    assert call.attributes["gen_ai.tool.name"] == "retrieve"
    assert call.attributes["outcome"] == "success"
    # No error on a success, so dashboards can filter on its absence.
    assert "error.type" not in call.attributes
    assert duration.sum == pytest.approx(0.25)


def test_a_failed_call_carries_its_error_code(reader):
    record_tool_call(
        tool_name="get_indices",
        duration_seconds=0.01,
        outcome="error",
        error_code="BACKEND_TIMEOUT",
    )

    (call,) = collect(reader)["mcp.server.tool.calls"]
    assert call.attributes["outcome"] == "error"
    assert call.attributes["error.type"] == "BACKEND_TIMEOUT"


def test_calls_are_split_by_tool_and_outcome(reader):
    record_tool_call(tool_name="retrieve", duration_seconds=0.1, outcome="success")
    record_tool_call(tool_name="retrieve", duration_seconds=0.1, outcome="success")
    record_tool_call(
        tool_name="retrieve", duration_seconds=0.1, outcome="error", error_code="INVALID_INPUT"
    )

    by_outcome = {
        point.attributes["outcome"]: point.value
        for point in collect(reader)["mcp.server.tool.calls"]
    }
    assert by_outcome == {"success": 2, "error": 1}


# --------------------------------------------------------------------------- #
# Backend requests
# --------------------------------------------------------------------------- #
def test_a_successful_backend_call_records_its_status(reader):
    record_backend_request(
        http_method="POST", path="/retrieve-rrf", duration_seconds=0.4, status_code=200
    )

    (request,) = collect(reader)["search.backend.requests"]
    assert request.attributes["http.request.method"] == "POST"
    assert request.attributes["url.path"] == "/retrieve-rrf"
    assert request.attributes["http.response.status_code"] == 200
    assert request.attributes["outcome"] == "success"


def test_a_failed_backend_call_is_marked_and_coded(reader):
    record_backend_request(
        http_method="GET",
        path="/indices",
        duration_seconds=0.1,
        status_code=503,
        error_code="BACKEND_SERVER_ERROR",
    )

    (request,) = collect(reader)["search.backend.requests"]
    assert request.attributes["outcome"] == "error"
    assert request.attributes["error.type"] == "BACKEND_SERVER_ERROR"


def test_a_call_that_never_got_a_response_is_still_timed(reader):
    """A timeout has no status code, and is exactly what needs to be visible."""
    record_backend_request(
        http_method="GET", path="/indices", duration_seconds=30.0, error_code="BACKEND_TIMEOUT"
    )

    points = collect(reader)
    (request,) = points["search.backend.requests"]
    (duration,) = points["search.backend.duration"]

    assert "http.response.status_code" not in request.attributes
    assert request.attributes["error.type"] == "BACKEND_TIMEOUT"
    assert duration.sum == pytest.approx(30.0)


def test_retries_are_counted_by_code_and_attempt(reader):
    record_backend_retry(path="/indices", error_code="BACKEND_TIMEOUT", attempt=1)

    (retry,) = collect(reader)["search.backend.retries"]
    assert retry.value == 1
    assert retry.attributes["error.type"] == "BACKEND_TIMEOUT"
    assert retry.attributes["retry.attempt"] == 1


# --------------------------------------------------------------------------- #
# Retrieval results
# --------------------------------------------------------------------------- #
def test_documents_returned_is_split_by_strategy(reader):
    record_documents_returned(method="bm25", count=5)
    record_documents_returned(method="rrf", count=3)

    by_method = {
        point.attributes["search.method"]: point.sum
        for point in collect(reader)["search.documents.returned"]
    }
    assert by_method == {"bm25": 5, "rrf": 3}


def test_an_empty_result_is_recorded_rather_than_skipped(reader):
    """Searches that find nothing are the interesting ones."""
    record_documents_returned(method="knn", count=0)

    (point,) = collect(reader)["search.documents.returned"]
    assert point.count == 1
    assert point.sum == 0


# --------------------------------------------------------------------------- #
# Behaviour without a provider
# --------------------------------------------------------------------------- #
def test_recording_without_a_configured_provider_is_a_no_op():
    """Export is off in most deployments; recording must stay free and silent."""
    reset_instruments()
    try:
        record_tool_call(tool_name="retrieve", duration_seconds=0.1, outcome="success")
        record_backend_request(http_method="GET", path="/indices", duration_seconds=0.1)
        record_backend_retry(path="/indices", error_code="X", attempt=1)
        record_documents_returned(method="rrf", count=1)
    finally:
        reset_instruments()
