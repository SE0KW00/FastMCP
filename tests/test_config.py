"""Settings normalisation and validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from es_search_mcp.config import Settings
from es_search_mcp.models import RetrieveMethod


def test_base_url_trailing_slash_is_stripped():
    assert Settings(backend_base_url="http://api.test/").backend_base_url == "http://api.test"


def test_endpoint_paths_get_a_leading_slash():
    configured = Settings(backend_retrieve_path_template="search-{method}")
    assert configured.backend_retrieve_path_template == "/search-{method}"


@pytest.mark.parametrize("method", list(RetrieveMethod))
def test_each_method_maps_to_its_own_path(method):
    assert Settings(_env_file=None).retrieve_path(method) == f"/retrieve-{method.value}"


def test_a_custom_path_template_is_honoured():
    configured = Settings(backend_retrieve_path_template="/v2/search/{method}")
    assert configured.retrieve_path(RetrieveMethod.KNN) == "/v2/search/knn"


def test_a_path_template_without_the_placeholder_is_rejected():
    with pytest.raises(ValidationError, match="method"):
        Settings(backend_retrieve_path_template="/retrieve")


def test_rrf_is_the_default_method():
    assert Settings(_env_file=None).default_retrieve_method is RetrieveMethod.RRF


def test_credential_header_names_are_lowercased():
    configured = Settings(_env_file=None, auth_header=" Authorization ", api_key_header="X-API-Key")
    assert (configured.auth_header, configured.api_key_header) == ("authorization", "x-api-key")


def test_a_blank_credential_header_name_is_rejected():
    with pytest.raises(ValidationError):
        Settings(auth_header="   ")


def test_default_top_k_may_not_exceed_max_top_k():
    with pytest.raises(ValidationError):
        Settings(default_top_k=20, max_top_k=10)


def test_env_prefix_is_honoured(monkeypatch):
    monkeypatch.setenv("ES_MCP_BACKEND_BASE_URL", "http://from-env.test")
    monkeypatch.setenv("ES_MCP_MAX_RETRIES", "4")
    loaded = Settings(_env_file=None)
    assert loaded.backend_base_url == "http://from-env.test"
    assert loaded.max_retries == 4
