"""Domain exception hierarchy for the search MCP server.

Every failure the server can produce is expressed as an :class:`SearchMCPError`
so that a single place — :mod:`es_search_mcp.middleware` — decides how it is
logged and how it is reported back to the MCP client.

Each error carries:

``code``
    A stable, machine-readable identifier (``BACKEND_TIMEOUT``, ...). Clients and
    log pipelines can branch on it without parsing prose.
``message``
    A short sentence written for the model calling the tool.
``details``
    Structured, non-sensitive context (status code, index name, ...).
``retryable``
    Whether calling the same tool again could plausibly succeed.
"""

from __future__ import annotations

from typing import Any


class SearchMCPError(Exception):
    """Base class for every error raised by this server."""

    code: str = "INTERNAL_ERROR"
    retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        details: dict[str, Any] | None = None,
        retryable: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if retryable is not None:
            self.retryable = retryable
        self.details: dict[str, Any] = details or {}

    def to_dict(self) -> dict[str, Any]:
        """Return a log/serialisation friendly representation."""
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "details": self.details,
        }

    def client_message(self) -> str:
        """Render the message surfaced to the MCP client."""
        suffix = " The request may succeed if retried." if self.retryable else ""
        return f"[{self.code}] {self.message}{suffix}"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.client_message()


class ConfigurationError(SearchMCPError):
    """The server is misconfigured and cannot serve requests."""

    code = "CONFIGURATION_ERROR"


class InvalidInputError(SearchMCPError):
    """Tool arguments failed validation before any backend call was made."""

    code = "INVALID_INPUT"


class BackendError(SearchMCPError):
    """Base class for failures that originate in the backend API."""

    code = "BACKEND_ERROR"


class BackendConnectionError(BackendError):
    """The backend API could not be reached."""

    code = "BACKEND_UNAVAILABLE"
    retryable = True


class BackendTimeoutError(BackendError):
    """The backend API did not answer within the configured timeout."""

    code = "BACKEND_TIMEOUT"
    retryable = True


class BackendAuthError(BackendError):
    """The backend API rejected our credentials (401/403)."""

    code = "BACKEND_UNAUTHORIZED"


class BackendNotFoundError(BackendError):
    """The requested resource — usually an index — does not exist (404)."""

    code = "BACKEND_NOT_FOUND"


class BackendRateLimitError(BackendError):
    """The backend API is throttling us (429)."""

    code = "BACKEND_RATE_LIMITED"
    retryable = True


class BackendBadRequestError(BackendError):
    """The backend API rejected the request payload (4xx other than the above)."""

    code = "BACKEND_BAD_REQUEST"


class BackendServerError(BackendError):
    """The backend API failed internally (5xx)."""

    code = "BACKEND_SERVER_ERROR"
    retryable = True


class BackendPayloadError(BackendError):
    """The backend API answered with a body this server cannot understand."""

    code = "BACKEND_INVALID_PAYLOAD"


def error_for_status(status_code: int) -> type[BackendError]:
    """Map an HTTP status code onto the matching :class:`BackendError` subclass."""
    if status_code in (401, 403):
        return BackendAuthError
    if status_code == 404:
        return BackendNotFoundError
    if status_code == 429:
        return BackendRateLimitError
    if 400 <= status_code < 500:
        return BackendBadRequestError
    return BackendServerError
