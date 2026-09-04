"""Backend client: payload normalisation, error mapping and retries."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from es_search_mcp.backend import SearchBackendClient
from es_search_mcp.exceptions import (
    BackendAuthError,
    BackendNotFoundError,
    BackendPayloadError,
    BackendRateLimitError,
    BackendServerError,
    BackendTimeoutError,
)
from es_search_mcp.logging import bind_call_context
from es_search_mcp.models import RetrieveMethod
from tests.constants import API_KEY, AUTHORIZATION, BASE_URL


@respx.mock
async def test_list_indices_parses_a_bare_list(client, credentials):
    respx.get(f"{BASE_URL}/indices").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"index": "faq", "desc": "Customer FAQ", "doc_count": 12},
                {"name": "manuals", "description": "Product manuals"},
            ],
        )
    )
    result = await client.list_indices(credentials=credentials)
    assert result.total == 2
    assert result.indices[0].name == "faq"
    assert result.indices[0].description == "Customer FAQ"
    assert result.indices[0].document_count == 12
    assert result.indices[1].document_count is None


@respx.mock
async def test_list_indices_parses_an_envelope(client, credentials):
    respx.get(f"{BASE_URL}/indices").mock(
        return_value=httpx.Response(200, json={"indices": [{"name": "faq"}]})
    )
    assert (await client.list_indices(credentials=credentials)).indices[0].description == ""


@respx.mock
async def test_list_indices_rejects_an_unparseable_body(client, credentials):
    respx.get(f"{BASE_URL}/indices").mock(
        return_value=httpx.Response(200, json={"unexpected": "shape"})
    )
    with pytest.raises(BackendPayloadError):
        await client.list_indices(credentials=credentials)


@respx.mock
async def test_retrieve_sends_the_expected_body(client, credentials):
    route = respx.post(f"{BASE_URL}/retrieve-rrf").mock(
        return_value=httpx.Response(
            200,
            json={"documents": [{"_id": "1", "_score": 1.5, "text": "hello"}]},
        )
    )
    result = await client.retrieve(
        credentials=credentials,
        index="faq",
        query="refund",
        top_k=3,
        method=RetrieveMethod.RRF,
        filters={"lang": "ko"},
    )

    sent = json.loads(route.calls.last.request.read())
    assert sent == {"index": "faq", "query": "refund", "top_k": 3, "filters": {"lang": "ko"}}
    assert result.index == "faq"
    assert result.query == "refund"
    assert result.method is RetrieveMethod.RRF
    assert result.total == 1
    assert result.documents[0].id == "1"
    assert result.documents[0].score == 1.5
    assert result.documents[0].content == "hello"


@pytest.mark.parametrize("method", list(RetrieveMethod))
@respx.mock
async def test_each_method_uses_its_own_endpoint(client, credentials, method):
    """The strategy selects the path; it does not travel in the request body."""
    route = respx.post(f"{BASE_URL}/retrieve-{method.value}").mock(
        return_value=httpx.Response(200, json=[])
    )
    result = await client.retrieve(
        credentials=credentials, index="faq", query="q", top_k=1, method=method
    )

    assert route.call_count == 1
    assert "method" not in json.loads(route.calls.last.request.read())
    assert result.method is method


@respx.mock
async def test_retrieve_omits_absent_filters(client, credentials):
    route = respx.post(f"{BASE_URL}/retrieve-rrf").mock(return_value=httpx.Response(200, json=[]))
    await client.retrieve(
        credentials=credentials, index="faq", query="refund", top_k=1, method=RetrieveMethod.RRF
    )
    assert b"filters" not in route.calls.last.request.read()


@respx.mock
async def test_retrieve_reads_nested_elasticsearch_hits(client, credentials):
    respx.post(f"{BASE_URL}/retrieve-knn").mock(
        return_value=httpx.Response(
            200, json={"hits": {"hits": [{"_id": "a", "_source": {"title": "t"}}]}}
        )
    )
    result = await client.retrieve(
        credentials=credentials, index="faq", query="q", top_k=1, method=RetrieveMethod.KNN
    )
    assert result.documents[0].metadata == {"title": "t"}


@respx.mock
async def test_invalid_json_becomes_a_payload_error(client, credentials):
    respx.get(f"{BASE_URL}/indices").mock(return_value=httpx.Response(200, content=b"not json"))
    with pytest.raises(BackendPayloadError):
        await client.list_indices(credentials=credentials)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, BackendAuthError),
        (403, BackendAuthError),
        (404, BackendNotFoundError),
        (429, BackendRateLimitError),
        (500, BackendServerError),
    ],
)
@respx.mock
async def test_status_codes_map_to_domain_errors(client, credentials, status, expected):
    respx.get(f"{BASE_URL}/indices").mock(return_value=httpx.Response(status))
    with pytest.raises(expected):
        await client.list_indices(credentials=credentials)


@respx.mock
async def test_timeouts_map_to_a_timeout_error(client, credentials):
    respx.get(f"{BASE_URL}/indices").mock(side_effect=httpx.ReadTimeout("too slow"))
    with pytest.raises(BackendTimeoutError):
        await client.list_indices(credentials=credentials)


@respx.mock
async def test_retryable_failure_is_retried_then_succeeds(client, credentials):
    route = respx.get(f"{BASE_URL}/indices").mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(200, json=[{"name": "faq"}]),
        ]
    )
    result = await client.list_indices(credentials=credentials)
    assert route.call_count == 2
    assert result.total == 1


@respx.mock
async def test_non_retryable_failure_is_not_retried(client, credentials):
    route = respx.get(f"{BASE_URL}/indices").mock(return_value=httpx.Response(404))
    with pytest.raises(BackendNotFoundError):
        await client.list_indices(credentials=credentials)
    assert route.call_count == 1


@respx.mock
async def test_retries_are_bounded_by_max_retries(client, credentials):
    route = respx.get(f"{BASE_URL}/indices").mock(return_value=httpx.Response(500))
    with pytest.raises(BackendServerError):
        await client.list_indices(credentials=credentials)
    assert route.call_count == 2  # 1 attempt + max_retries=1


@respx.mock
async def test_caller_credentials_are_forwarded_unchanged(client, credentials):
    respx.get(f"{BASE_URL}/indices").mock(return_value=httpx.Response(200, json=[]))
    await client.list_indices(credentials=credentials)

    sent = respx.calls.last.request.headers
    assert sent["authorization"] == AUTHORIZATION
    assert sent["x-api-key"] == API_KEY


@respx.mock
async def test_credential_header_names_are_configurable(settings, credentials):
    renamed = settings.model_copy(
        update={"auth_header": "x-tenant-token", "api_key_header": "x-client-key"}
    )
    respx.get(f"{BASE_URL}/indices").mock(return_value=httpx.Response(200, json=[]))

    async with SearchBackendClient(renamed) as renamed_client:
        await renamed_client.list_indices(credentials=credentials)

    sent = respx.calls.last.request.headers
    assert sent["x-tenant-token"] == AUTHORIZATION
    assert sent["x-client-key"] == API_KEY
    assert "x-api-key" not in sent


@respx.mock
async def test_the_shared_client_carries_no_credentials_of_its_own(settings):
    """Credentials belong to the caller, so nothing leaks between requests."""
    async with SearchBackendClient(settings) as bare:
        assert "authorization" not in bare._client.headers
        assert "x-api-key" not in bare._client.headers


@respx.mock
async def test_request_id_is_propagated_to_the_backend(client, credentials):
    respx.get(f"{BASE_URL}/indices").mock(return_value=httpx.Response(200, json=[]))
    with bind_call_context(request_id="abc123", tool_name="list_indices"):
        await client.list_indices(credentials=credentials)
    assert respx.calls.last.request.headers["x-request-id"] == "abc123"
