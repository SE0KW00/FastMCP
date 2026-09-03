"""Settings normalisation and validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from es_search_mcp.config import Settings


def test_base_url_trailing_slash_is_stripped():
    assert Settings(backend_base_url="http://api.test/").backend_base_url == "http://api.test"


def test_endpoint_paths_get_a_leading_slash():
    assert Settings(backend_retrieve_path="search").backend_retrieve_path == "/search"


def test_default_top_k_may_not_exceed_max_top_k():
    with pytest.raises(ValidationError):
        Settings(default_top_k=20, max_top_k=10)


def test_env_prefix_is_honoured(monkeypatch):
    monkeypatch.setenv("ES_MCP_BACKEND_BASE_URL", "http://from-env.test")
    monkeypatch.setenv("ES_MCP_MAX_RETRIES", "4")
    loaded = Settings(_env_file=None)
    assert loaded.backend_base_url == "http://from-env.test"
    assert loaded.max_retries == 4
