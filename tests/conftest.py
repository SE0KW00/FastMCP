"""Shared fixtures."""

from __future__ import annotations

import httpx
import pytest

from es_search_mcp.backend import SearchBackendClient
from es_search_mcp.config import Settings

BASE_URL = "http://backend.test"


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
    )


@pytest.fixture
async def client(settings: Settings):
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=settings.request_timeout) as http:
        yield SearchBackendClient(settings, client=http)
