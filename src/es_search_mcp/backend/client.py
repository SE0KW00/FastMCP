"""HTTP client for the backend API that fronts Elasticsearch.

Every network concern lives here: authentication headers, timeouts, retries with
exponential backoff, request correlation, and — most importantly — translating
transport-level failures into the domain errors declared in
:mod:`es_search_mcp.exceptions`. Callers above this layer never see an
``httpx`` exception.
"""

from __future__ import annotations

import asyncio
import random
from types import TracebackType
from typing import Any

import httpx

from es_search_mcp.config import Settings
from es_search_mcp.exceptions import (
    BackendConnectionError,
    BackendError,
    BackendPayloadError,
    BackendTimeoutError,
    error_for_status,
)
from es_search_mcp.logging import current_request_id, get_logger
from es_search_mcp.models import IndexList, RetrieveResult

logger = get_logger(__name__)

#: How much of an error body is worth keeping for diagnostics.
_MAX_ERROR_BODY = 500


class SearchBackendClient:
    """Async client for the two endpoints this server exposes as tools."""

    def __init__(self, settings: Settings, *, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=settings.backend_base_url,
            timeout=settings.request_timeout,
            headers=self._default_headers(settings),
        )

    # -- lifecycle --------------------------------------------------------
    async def aclose(self) -> None:
        """Close the underlying connection pool if this client owns it."""
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> SearchBackendClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    # -- public API -------------------------------------------------------
    async def list_indices(self) -> IndexList:
        """Fetch the indices the backend exposes, with their descriptions."""
        payload = await self._request("GET", self._settings.backend_indices_path)
        try:
            return IndexList.from_payload(payload)
        except ValueError as exc:
            raise BackendPayloadError(
                "The backend returned an index list this server cannot parse.",
                details={"path": self._settings.backend_indices_path, "reason": str(exc)},
            ) from exc

    async def retrieve(
        self,
        *,
        index: str,
        query: str,
        top_k: int,
        filters: dict[str, Any] | None = None,
    ) -> RetrieveResult:
        """Search ``index`` for ``query`` and return at most ``top_k`` documents."""
        body: dict[str, Any] = {"index": index, "query": query, "top_k": top_k}
        if filters:
            body["filters"] = filters

        payload = await self._request("POST", self._settings.backend_retrieve_path, json=body)
        try:
            return RetrieveResult.from_payload(payload, index=index, query=query)
        except ValueError as exc:
            raise BackendPayloadError(
                "The backend returned a search result this server cannot parse.",
                details={"path": self._settings.backend_retrieve_path, "reason": str(exc)},
            ) from exc

    # -- internals --------------------------------------------------------
    @staticmethod
    def _default_headers(settings: Settings) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if settings.backend_api_key is not None:
            headers["Authorization"] = f"Bearer {settings.backend_api_key.get_secret_value()}"
        return headers

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        """Perform one backend call, retrying transient failures.

        Returns:
            The decoded JSON body.

        Raises:
            BackendError: for every failure mode, already classified.
        """
        attempts = self._settings.max_retries + 1
        last_error: BackendError | None = None

        for attempt in range(1, attempts + 1):
            try:
                return await self._attempt(method, path, attempt=attempt, **kwargs)
            except BackendError as exc:
                last_error = exc
                if not exc.retryable or attempt == attempts:
                    raise
                delay = self._backoff_delay(attempt)
                logger.warning(
                    "backend.retry",
                    fields={
                        "method": method,
                        "path": path,
                        "attempt": attempt,
                        "max_attempts": attempts,
                        "delay_seconds": round(delay, 3),
                        "error_code": exc.code,
                    },
                )
                await asyncio.sleep(delay)

        # Defensive: the loop either returns or raises, but keep the contract explicit.
        raise last_error or BackendError("The backend call failed for an unknown reason.")

    async def _attempt(self, method: str, path: str, *, attempt: int, **kwargs: Any) -> Any:
        headers = dict(kwargs.pop("headers", {}) or {})
        request_id = current_request_id()
        if request_id:
            headers["X-Request-ID"] = request_id

        logger.debug(
            "backend.request",
            fields={"method": method, "path": path, "attempt": attempt},
        )
        try:
            response = await self._client.request(method, path, headers=headers, **kwargs)
        except httpx.TimeoutException as exc:
            raise BackendTimeoutError(
                f"The backend API did not respond within {self._settings.request_timeout:g}s.",
                details={"method": method, "path": path},
            ) from exc
        except httpx.HTTPError as exc:
            raise BackendConnectionError(
                "The backend API could not be reached.",
                details={"method": method, "path": path, "reason": type(exc).__name__},
            ) from exc

        logger.info(
            "backend.response",
            fields={
                "method": method,
                "path": path,
                "status_code": response.status_code,
                "elapsed_ms": round(response.elapsed.total_seconds() * 1000, 2),
            },
        )

        if response.is_error:
            raise self._error_from_response(response, method=method, path=path)

        try:
            return response.json()
        except ValueError as exc:
            raise BackendPayloadError(
                "The backend API returned a body that is not valid JSON.",
                details={"method": method, "path": path, "status_code": response.status_code},
            ) from exc

    @staticmethod
    def _error_from_response(response: httpx.Response, *, method: str, path: str) -> BackendError:
        error_cls = error_for_status(response.status_code)
        return error_cls(
            f"The backend API responded with HTTP {response.status_code}.",
            details={
                "method": method,
                "path": path,
                "status_code": response.status_code,
                "body": response.text[:_MAX_ERROR_BODY],
            },
        )

    def _backoff_delay(self, attempt: int) -> float:
        """Exponential backoff with full jitter, to avoid retry storms."""
        ceiling = self._settings.retry_backoff_base * (2 ** (attempt - 1))
        return random.uniform(0, ceiling)
