"""End-to-end tool behaviour through an in-memory MCP client.

These exercise the full stack — client transport, middleware, tool, backend
client — with only the HTTP layer stubbed out.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx
from fastmcp import Client
from fastmcp.exceptions import ToolError

from es_search_mcp.errors import GENERIC_ERROR_MESSAGE
from es_search_mcp.server import create_server

BASE_URL = "http://backend.test"


@pytest.fixture
async def mcp_client(settings, client):
    async with Client(create_server(settings, client=client)) as connected:
        yield connected


def _payload(result):
    """Read the structured content a tool returned."""
    return result.structured_content or json.loads(result.content[0].text)


async def test_tools_are_advertised(mcp_client):
    names = {tool.name for tool in await mcp_client.list_tools()}
    assert names == {"list_indices", "retrieve_documents"}


async def test_tool_schemas_document_their_arguments(mcp_client):
    tools = {tool.name: tool for tool in await mcp_client.list_tools()}
    schema = tools["retrieve_documents"].input_schema
    assert set(schema["required"]) == {"index", "query"}
    assert "list_indices" in tools["retrieve_documents"].description


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


@respx.mock
async def test_retrieve_documents_returns_hits(mcp_client):
    respx.post(f"{BASE_URL}/retrieve").mock(
        return_value=httpx.Response(
            200, json={"documents": [{"id": "1", "content": "refund policy"}]}
        )
    )
    payload = _payload(
        await mcp_client.call_tool(
            "retrieve_documents", {"index": "faq", "query": "refund", "top_k": 2}
        )
    )
    assert payload["index"] == "faq"
    assert payload["documents"][0]["content"] == "refund policy"


@respx.mock
async def test_top_k_defaults_to_the_configured_value(mcp_client, settings):
    route = respx.post(f"{BASE_URL}/retrieve").mock(return_value=httpx.Response(200, json=[]))
    await mcp_client.call_tool("retrieve_documents", {"index": "faq", "query": "q"})
    assert json.loads(route.calls.last.request.read())["top_k"] == settings.default_top_k


async def test_blank_query_is_rejected_before_any_backend_call(mcp_client):
    with respx.mock:
        route = respx.post(f"{BASE_URL}/retrieve")
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


async def test_unexpected_errors_are_masked(settings, client, monkeypatch):
    async def boom() -> None:
        raise RuntimeError("secret internal detail")

    monkeypatch.setattr(client, "list_indices", boom)
    async with Client(create_server(settings, client=client)) as connected:
        with pytest.raises(ToolError) as excinfo:
            await connected.call_tool("list_indices", {})

    message = str(excinfo.value)
    assert message == GENERIC_ERROR_MESSAGE
    assert "secret internal detail" not in message


@respx.mock
async def test_each_call_is_logged_with_a_correlation_id(capsys, mcp_client):
    respx.get(f"{BASE_URL}/indices").mock(return_value=httpx.Response(200, json=[]))
    await mcp_client.call_tool("list_indices", {})

    events = [
        json.loads(line) for line in capsys.readouterr().err.splitlines() if line.startswith("{")
    ]
    by_event = {entry["event"]: entry for entry in events}
    assert by_event["tool.call.start"]["tool_name"] == "list_indices"
    assert by_event["tool.call.finished"]["outcome"] == "success"
    assert by_event["tool.call.finished"]["duration_ms"] >= 0
    assert by_event["tool.call.start"]["request_id"] == by_event["tool.call.finished"]["request_id"]


@respx.mock
async def test_failures_are_logged_with_their_error_code(capsys, mcp_client):
    respx.get(f"{BASE_URL}/indices").mock(return_value=httpx.Response(404))
    with pytest.raises(ToolError):
        await mcp_client.call_tool("list_indices", {})

    failures = [
        json.loads(line)
        for line in capsys.readouterr().err.splitlines()
        if line.startswith("{") and json.loads(line)["event"] == "tool.call.failed"
    ]
    assert failures and failures[0]["code"] == "BACKEND_NOT_FOUND"
