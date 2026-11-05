"""Caller credentials, read per request and forwarded to the backend.

The backend authenticates the end user, not this server, so the two keys it
requires are supplied by the MCP client on every request and passed through
unchanged. Nothing here is stored, defaulted, or cached: a credential this
server cannot see is a credential it cannot leak, and a per-request lookup keeps
concurrent callers from borrowing each other's keys.

Consequence worth knowing: headers only exist on the HTTP transports. Under
``stdio`` there is no request to read, so every tool call fails with
``MISSING_CREDENTIALS``.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastmcp.server.dependencies import get_http_headers

from es_search_mcp.config import Settings
from es_search_mcp.exceptions import MissingCredentialsError


@dataclass(frozen=True)
class BackendCredentials:
    """The two keys a caller must present, ready to be forwarded.

    The values are deliberately never logged, never returned to the client, and
    never placed on a model this server serialises.
    """

    authorization: str
    api_key: str

    def as_headers(self, settings: Settings) -> dict[str, str]:
        """Render the credentials under the header names the backend expects."""
        return {
            settings.auth_header: self.authorization,
            settings.api_key_header: self.api_key,
        }


def resolve_credentials(settings: Settings) -> BackendCredentials:
    """Read both credential headers from the request currently being served.

    Raises:
        MissingCredentialsError: if either header is absent or blank. The message
            names the missing headers so the caller can fix its configuration,
            and never echoes a value.
    """
    # `authorization` is stripped by default precisely because forwarding it is
    # usually wrong; here it is the point, so ask for it explicitly.
    headers = get_http_headers(include={settings.auth_header, settings.api_key_header})

    authorization = headers.get(settings.auth_header, "").strip()
    api_key = headers.get(settings.api_key_header, "").strip()

    missing = [
        name
        for name, value in (
            (settings.auth_header, authorization),
            (settings.api_key_header, api_key),
        )
        if not value
    ]
    if missing:
        raise MissingCredentialsError(
            "This server forwards the caller's credentials to the search backend, "
            f"but the request is missing: {', '.join(missing)}. Send both headers "
            "with every request; note that the stdio transport cannot carry them.",
            details={"missing_headers": missing},
        )

    return BackendCredentials(authorization=authorization, api_key=api_key)
