"""End-to-end tool behaviour over the real ASGI app.

Only the backend's HTTP layer is stubbed; the MCP request, the middleware, the
credential headers and the tools all run for real.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx
from fastmcp.exceptions import ToolError

from es_search_mcp.errors import GENERIC_ERROR_MESSAGE
from es_search_mcp.models import RetrieveMethod
from tests.constants import API_KEY, AUTHORIZATION, BASE_URL


def _payload(result):
    """Read the structured content a tool returned."""
    return result.structured_content or json.loads(result.content[0].text)


async def test_tools_are_advertised(mcp_client):
    names = {tool.name for tool in await mcp_client.list_tools()}
    assert names == {"list_indices", "retrieve_documents"}


async def test_tool_schema_documents_the_retrieval_methods(mcp_client):
    tools = {tool.name: tool for tool in await mcp_client.list_tools()}
    retrieve = tools["retrieve_documents"]
    schema = retrieve.input_schema

    assert set(schema["required"]) == {"index", "query"}
    assert schema["properties"]["method"]["default"] == "rrf"
    assert "list_indices" in retrieve.description
    # Each strategy is explained in the description the model reads.
    for method in RetrieveMethod:
        assert f"`{method.value}`" in retrieve.description


@respx.mock
async def test_list_indices_returns_names_and_descriptions(mcp_client):
    respx.get(f"{BASE_URL}/indices").mock(
        return_value=httpx.Response(200, json=[{"name": "faq", "description": "Customer FAQ"}])
    )
    payload = _payload(await mcp_client.call_tool("list_indices", {}))
    assert payload["total"] == 1
    assert payload["indices"][0] == {
        "name": "faq",
        "description": "Customer FAQ",
        "document_count": None,
    }


# --------------------------------------------------------------------------- #
# Retrieval methods
# --------------------------------------------------------------------------- #
@respx.mock
async def test_retrieve_defaults_to_rrf(mcp_client):
    rrf = respx.post(f"{BASE_URL}/retrieve-rrf").mock(
        return_value=httpx.Response(200, json={"documents": [{"id": "1", "content": "refund"}]})
    )
    payload = _payload(
        await mcp_client.call_tool("retrieve_documents", {"index": "faq", "query": "refund"})
    )

    assert rrf.call_count == 1
    assert payload["method"] == "rrf"
    assert payload["documents"][0]["content"] == "refund"


@pytest.mark.parametrize("method", [m.value for m in RetrieveMethod])
@respx.mock
async def test_each_method_reaches_its_own_endpoint(mcp_client, method):
    routes = {
        candidate.value: respx.post(f"{BASE_URL}/retrieve-{candidate.value}").mock(
            return_value=httpx.Response(200, json=[])
        )
        for candidate in RetrieveMethod
    }
    payload = _payload(
        await mcp_client.call_tool(
            "retrieve_documents", {"index": "faq", "query": "q", "method": method}
        )
    )

    assert payload["method"] == method
    assert routes[method].call_count == 1
    assert all(route.call_count == 0 for name, route in routes.items() if name != method)


async def test_an_unknown_method_is_rejected_with_the_valid_options(mcp_client):
    """A schema violation must tell the caller what to send instead of being masked."""
    with pytest.raises(ToolError) as excinfo:
        await mcp_client.call_tool(
            "retrieve_documents", {"index": "faq", "query": "q", "method": "bm42"}
        )

    message = str(excinfo.value)
    assert "[INVALID_INPUT]" in message
    assert "`method`" in message
    for method in RetrieveMethod:
        assert method.value in message


@respx.mock
async def test_top_k_defaults_to_the_configured_value(mcp_client, settings):
    route = respx.post(f"{BASE_URL}/retrieve-rrf").mock(return_value=httpx.Response(200, json=[]))
    await mcp_client.call_tool("retrieve_documents", {"index": "faq", "query": "q"})
    assert json.loads(route.calls.last.request.read())["top_k"] == settings.default_top_k


# --------------------------------------------------------------------------- #
# Credentials
# --------------------------------------------------------------------------- #
@respx.mock
async def test_caller_credentials_are_forwarded_to_the_backend(mcp_client):
    respx.get(f"{BASE_URL}/indices").mock(return_value=httpx.Response(200, json=[]))
    await mcp_client.call_tool("list_indices", {})

    forwarded = respx.calls.last.request.headers
    assert forwarded["authorization"] == AUTHORIZATION
    assert forwarded["x-api-key"] == API_KEY


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({}, id="neither"),
        pytest.param({"authorization": AUTHORIZATION}, id="api-key-missing"),
        pytest.param({"x-api-key": API_KEY}, id="authorization-missing"),
        pytest.param({"authorization": "   ", "x-api-key": API_KEY}, id="authorization-blank"),
    ],
)
@respx.mock
async def test_missing_credentials_are_reported_without_calling_the_backend(connect, headers):
    route = respx.get(f"{BASE_URL}/indices")
    async with connect(headers) as connected:
        with pytest.raises(ToolError) as excinfo:
            await connected.call_tool("list_indices", {})

    assert "[MISSING_CREDENTIALS]" in str(excinfo.value)
    assert route.call_count == 0


@respx.mock
async def test_credential_values_are_never_echoed_back_to_the_caller(connect):
    async with connect({"authorization": AUTHORIZATION}) as connected:
        with pytest.raises(ToolError) as excinfo:
            await connected.call_tool("list_indices", {})
    assert AUTHORIZATION not in str(excinfo.value)


@respx.mock
async def test_concurrent_callers_do_not_share_credentials(connect):
    """Credentials are resolved per request, never cached on the shared client."""
    respx.get(f"{BASE_URL}/indices").mock(return_value=httpx.Response(200, json=[]))
    other = {"authorization": "Bearer other-token", "x-api-key": "other-key"}

    async with connect() as first:
        await first.call_tool("list_indices", {})
        assert respx.calls.last.request.headers["authorization"] == AUTHORIZATION

    async with connect(other) as second:
        await second.call_tool("list_indices", {})
        assert respx.calls.last.request.headers["authorization"] == "Bearer other-token"


# --------------------------------------------------------------------------- #
# Input validation and errors
# --------------------------------------------------------------------------- #
@respx.mock
async def test_blank_query_is_rejected_before_any_backend_call(mcp_client):
    route = respx.post(f"{BASE_URL}/retrieve-rrf")
    with pytest.raises(ToolError) as excinfo:
        await mcp_client.call_tool("retrieve_documents", {"index": "faq", "query": "   "})

    assert "[INVALID_INPUT]" in str(excinfo.value)
    assert route.call_count == 0


async def test_top_k_above_the_cap_is_rejected(mcp_client, settings):
    with pytest.raises(ToolError) as excinfo:
        await mcp_client.call_tool(
            "retrieve_documents",
            {"index": "faq", "query": "q", "top_k": settings.max_top_k + 1},
        )
    assert "[INVALID_INPUT]" in str(excinfo.value)


@respx.mock
async def test_backend_error_reaches_the_client_with_its_code(mcp_client):
    respx.get(f"{BASE_URL}/indices").mock(return_value=httpx.Response(404))
    with pytest.raises(ToolError) as excinfo:
        await mcp_client.call_tool("list_indices", {})
    assert "[BACKEND_NOT_FOUND]" in str(excinfo.value)


@respx.mock
async def test_retryable_error_tells_the_client_to_retry(mcp_client):
    respx.get(f"{BASE_URL}/indices").mock(return_value=httpx.Response(503))
    with pytest.raises(ToolError) as excinfo:
        await mcp_client.call_tool("list_indices", {})

    message = str(excinfo.value)
    assert "[BACKEND_SERVER_ERROR]" in message
    assert "retried" in message


@respx.mock
async def test_backend_error_body_is_not_leaked_to_the_client(mcp_client):
    respx.get(f"{BASE_URL}/indices").mock(
        return_value=httpx.Response(500, text="Traceback: /srv/app/internal.py line 42")
    )
    with pytest.raises(ToolError) as excinfo:
        await mcp_client.call_tool("list_indices", {})
    assert "internal.py" not in str(excinfo.value)


async def test_unexpected_errors_are_masked(mcp_client, client, monkeypatch):
    async def boom(**_: object) -> None:
        raise RuntimeError("secret internal detail")

    monkeypatch.setattr(client, "list_indices", boom)
    with pytest.raises(ToolError) as excinfo:
        await mcp_client.call_tool("list_indices", {})

    message = str(excinfo.value)
    assert message == GENERIC_ERROR_MESSAGE
    assert "secret internal detail" not in message


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
def _json_logs(capsys):
    lines = capsys.readouterr().err.splitlines()
    return [json.loads(line) for line in lines if line.startswith("{")]


@respx.mock
async def test_each_call_is_logged_with_a_correlation_id(capsys, mcp_client):
    respx.get(f"{BASE_URL}/indices").mock(return_value=httpx.Response(200, json=[]))
    await mcp_client.call_tool("list_indices", {})

    by_event = {entry["event"]: entry for entry in _json_logs(capsys)}
    assert by_event["tool.call.start"]["tool_name"] == "list_indices"
    assert by_event["tool.call.finished"]["outcome"] == "success"
    assert by_event["tool.call.finished"]["duration_ms"] >= 0
    assert by_event["tool.call.start"]["request_id"] == by_event["tool.call.finished"]["request_id"]


@respx.mock
async def test_the_retrieval_method_is_logged(capsys, mcp_client):
    respx.post(f"{BASE_URL}/retrieve-knn").mock(return_value=httpx.Response(200, json=[]))
    await mcp_client.call_tool(
        "retrieve_documents", {"index": "faq", "query": "q", "method": "knn"}
    )

    results = [e for e in _json_logs(capsys) if e["event"] == "tool.retrieve_documents.result"]
    assert results and results[0]["method"] == "knn"


@respx.mock
async def test_failures_are_logged_with_their_error_code(capsys, mcp_client):
    respx.get(f"{BASE_URL}/indices").mock(return_value=httpx.Response(404))
    with pytest.raises(ToolError):
        await mcp_client.call_tool("list_indices", {})

    failures = [e for e in _json_logs(capsys) if e["event"] == "tool.call.failed"]
    assert failures and failures[0]["code"] == "BACKEND_NOT_FOUND"


@respx.mock
async def test_credentials_never_appear_in_the_log_stream(capsys, mcp_client):
    respx.get(f"{BASE_URL}/indices").mock(return_value=httpx.Response(200, json=[]))
    await mcp_client.call_tool("list_indices", {})

    captured = capsys.readouterr().err
    assert AUTHORIZATION not in captured
    assert API_KEY not in captured
