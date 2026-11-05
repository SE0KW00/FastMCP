"""Reading the caller's credential headers."""

from __future__ import annotations

import pytest
from starlette.requests import Request

from es_search_mcp.config import Settings
from es_search_mcp.credentials import BackendCredentials, resolve_credentials
from es_search_mcp.exceptions import MissingCredentialsError
from tests.constants import API_KEY, AUTHORIZATION


def _request_scope(headers: dict[str, str]) -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/mcp/",
            "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
        }
    )


@pytest.fixture
def serving(monkeypatch):
    """Pretend a request with the given headers is being served."""

    def install(headers: dict[str, str]) -> None:
        request = _request_scope(headers)
        monkeypatch.setattr(
            "es_search_mcp.credentials.get_http_headers",
            lambda include=None, include_all=False: dict(request.headers),
        )

    return install


def test_both_headers_are_read(settings, serving):
    serving({"authorization": AUTHORIZATION, "x-api-key": API_KEY})
    assert resolve_credentials(settings) == BackendCredentials(
        authorization=AUTHORIZATION, api_key=API_KEY
    )


def test_surrounding_whitespace_is_trimmed(settings, serving):
    serving({"authorization": f"  {AUTHORIZATION}  ", "x-api-key": API_KEY})
    assert resolve_credentials(settings).authorization == AUTHORIZATION


@pytest.mark.parametrize(
    ("headers", "expected_missing"),
    [
        ({}, ["authorization", "x-api-key"]),
        ({"authorization": AUTHORIZATION}, ["x-api-key"]),
        ({"x-api-key": API_KEY}, ["authorization"]),
        ({"authorization": " ", "x-api-key": API_KEY}, ["authorization"]),
    ],
)
def test_missing_headers_are_named(settings, serving, headers, expected_missing):
    serving(headers)
    with pytest.raises(MissingCredentialsError) as excinfo:
        resolve_credentials(settings)

    assert excinfo.value.details["missing_headers"] == expected_missing
    assert excinfo.value.retryable is False


def test_the_error_never_echoes_a_supplied_value(settings, serving):
    serving({"authorization": AUTHORIZATION})
    with pytest.raises(MissingCredentialsError) as excinfo:
        resolve_credentials(settings)
    assert AUTHORIZATION not in excinfo.value.client_message()


def test_configured_header_names_are_used(settings, serving):
    renamed = settings.model_copy(
        update={"auth_header": "x-tenant-token", "api_key_header": "x-client-key"}
    )
    serving({"x-tenant-token": AUTHORIZATION, "x-client-key": API_KEY})

    resolved = resolve_credentials(renamed)
    assert resolved.as_headers(renamed) == {
        "x-tenant-token": AUTHORIZATION,
        "x-client-key": API_KEY,
    }


def test_stdio_has_no_request_so_credentials_are_missing(settings):
    """There is no live HTTP request here — the same situation as stdio."""
    with pytest.raises(MissingCredentialsError):
        resolve_credentials(settings)


def test_header_names_are_normalised_to_lowercase():
    configured = Settings(_env_file=None, auth_header="X-Tenant-Token")
    assert configured.auth_header == "x-tenant-token"
