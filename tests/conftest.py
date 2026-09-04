"""Shared fixtures.

The integration fixtures serve the real ASGI app in-process: credentials now
arrive as HTTP headers, so an in-memory transport could not exercise them.
``httpx2.ASGITransport`` gives real HTTP semantics without opening a socket, and
because the MCP client speaks ``httpx2`` while the backend client speaks
``httpx``, ``respx`` still stubs the backend and nothing else.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable

import httpx
import httpx2
import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from starlette.applications import Starlette

from es_search_mcp.backend import SearchBackendClient
from es_search_mcp.config import Settings
from es_search_mcp.credentials import BackendCredentials
from es_search_mcp.server import create_server
from tests.constants import API_KEY, AUTHORIZATION, BASE_URL, CREDENTIAL_HEADERS

MCP_URL = "http://testserver/mcp/"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        backend_base_url=BASE_URL,
        max_retries=1,
        retry_backoff_base=0.001,
        default_top_k=3,
        max_top_k=10,
        log_level="DEBUG",
        log_format="json",
        transport="http",
    )


@pytest.fixture
async def client(settings: Settings) -> AsyncIterator[SearchBackendClient]:
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=settings.request_timeout) as http:
        yield SearchBackendClient(settings, client=http)


@pytest.fixture
def credentials() -> BackendCredentials:
    return BackendCredentials(authorization=AUTHORIZATION, api_key=API_KEY)


@pytest.fixture
async def mcp_app(settings: Settings, client: SearchBackendClient) -> AsyncIterator[Starlette]:
    """The server as an ASGI app, wired to the stubbed backend client.

    The lifespan is entered and exited inside one long-lived task. pytest-asyncio
    runs a fixture's setup and teardown halves in *different* tasks, and the
    session manager's cancel scope cannot be exited from a task other than the
    one that entered it; the events below keep both halves in the same task.
    """
    app = create_server(settings, client=client).http_app()
    started, stop = asyncio.Event(), asyncio.Event()

    async def run_lifespan() -> None:
        async with app.router.lifespan_context(app):
            started.set()
            await stop.wait()

    task = asyncio.create_task(run_lifespan())
    await started.wait()
    try:
        yield app
    finally:
        stop.set()
        await task


@pytest.fixture
def connect(mcp_app: Starlette) -> Callable[..., Client]:
    """Return a factory that connects an MCP client with the given headers."""

    def factory(headers: dict[str, str] | None = None) -> Client:
        def httpx_client_factory(**kwargs: object) -> httpx2.AsyncClient:
            return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=mcp_app), **kwargs)  # type: ignore[arg-type]

        return Client(
            StreamableHttpTransport(
                MCP_URL,
                headers=CREDENTIAL_HEADERS if headers is None else headers,
                httpx_client_factory=httpx_client_factory,
            )
        )

    return factory


@pytest.fixture
async def mcp_client(connect: Callable[..., Client]) -> AsyncIterator[Client]:
    """A connected MCP client that sends valid credential headers."""
    async with connect() as connected:
        yield connected
