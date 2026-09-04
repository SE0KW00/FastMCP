"""``retrieve_documents`` — search one index with a chosen retrieval strategy.

The backend exposes each strategy as its own endpoint (``/retrieve-bm25``,
``/retrieve-knn``, ``/retrieve-cc``, ``/retrieve-rrf``). They are presented here
as a single tool with a ``method`` argument, so the model picks a strategy rather
than picking between four near-identical tools — and so a caller that does not
care gets the sensible default (`rrf`) without having to choose at all.
"""

# NOTE: this module deliberately does not use `from __future__ import annotations`.
# Tool signatures are evaluated at registration time to build the MCP input schema,
# and the annotations below close over `settings`, which a deferred (string)
# annotation could not resolve.

from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import Field

from es_search_mcp.backend import SearchBackendClient
from es_search_mcp.config import Settings
from es_search_mcp.credentials import resolve_credentials
from es_search_mcp.errors import tool_error_boundary
from es_search_mcp.exceptions import InvalidInputError
from es_search_mcp.logging import get_logger
from es_search_mcp.models import RETRIEVE_METHOD_GUIDE, RetrieveMethod, RetrieveResult

logger = get_logger(__name__)

#: Queries longer than this are almost certainly a mistake by the caller.
MAX_QUERY_LENGTH = 1_000


def _method_documentation(settings: Settings) -> str:
    """Render the per-strategy guidance that goes into the tool's docstring."""
    lines = [
        f"- `{method.value}`{' (default)' if method == settings.default_retrieve_method else ''}: "
        f"{summary}"
        for method, summary in RETRIEVE_METHOD_GUIDE.items()
    ]
    return "\n".join(lines)


def register(mcp: FastMCP, client: SearchBackendClient, settings: Settings) -> None:
    """Register the document-retrieval tool on ``mcp``."""

    async def retrieve_documents(
        index: Annotated[
            str,
            Field(description="Index to search. Must be a name returned by `list_indices`."),
        ],
        query: Annotated[
            str,
            Field(description="Natural-language or keyword query to search for."),
        ],
        method: Annotated[
            RetrieveMethod,
            Field(
                description=(
                    "Retrieval strategy. Defaults to "
                    f"`{settings.default_retrieve_method.value}`. "
                    "Each strategy is served by its own backend endpoint."
                ),
            ),
        ] = settings.default_retrieve_method,
        top_k: Annotated[
            int | None,
            Field(
                default=None,
                ge=1,
                description=(
                    "How many documents to return. Defaults to "
                    f"{settings.default_top_k}, capped at {settings.max_top_k}."
                ),
            ),
        ] = None,
        filters: Annotated[
            dict[str, Any] | None,
            Field(
                default=None,
                description=(
                    "Optional field/value pairs used to narrow the search, "
                    'e.g. {"category": "billing"}.'
                ),
            ),
        ] = None,
    ) -> RetrieveResult:
        """Search an Elasticsearch index and return the most relevant documents.

        Use `list_indices` first to find out which index fits the question, then
        call this tool with that index name. Prefer a specific query over a broad
        one, and raise `top_k` only when more evidence is genuinely needed.

        Choosing `method`:

        {method_documentation}

        Leave `method` alone unless the question clearly favours one signal. If a
        search returns nothing useful, retrying with `bm25` (for exact terms) or
        `knn` (for paraphrases) is usually more effective than raising `top_k`.

        Returns:
            The matching documents, each with its id, relevance score, text
            content and remaining metadata fields, plus the `method` used.
        """
        async with tool_error_boundary(mask_unexpected=settings.mask_error_details):
            credentials = resolve_credentials(settings)
            normalised_index = _validate_index(index)
            normalised_query = _validate_query(query)
            effective_top_k = _resolve_top_k(top_k, settings)

            result = await client.retrieve(
                credentials=credentials,
                index=normalised_index,
                query=normalised_query,
                top_k=effective_top_k,
                method=method,
                filters=filters,
            )
            logger.info(
                "tool.retrieve_documents.result",
                fields={
                    "index": normalised_index,
                    "method": method.value,
                    "top_k": effective_top_k,
                    "document_count": result.total,
                    "has_filters": bool(filters),
                },
            )
            return result

    # The strategy guidance is data (`RETRIEVE_METHOD_GUIDE`), so it is substituted
    # into the docstring the model reads rather than duplicated as a literal. A
    # docstring cannot be an f-string, hence the fill-in before registration.
    assert retrieve_documents.__doc__ is not None
    retrieve_documents.__doc__ = retrieve_documents.__doc__.format(
        method_documentation=_method_documentation(settings)
    )

    mcp.tool(
        name="retrieve_documents",
        title="Retrieve documents from an index",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"search", "retrieval"},
    )(retrieve_documents)


def _validate_index(index: str) -> str:
    """Reject an empty index name before spending a network round-trip on it."""
    normalised = index.strip()
    if not normalised:
        raise InvalidInputError(
            "`index` must not be empty. Call `list_indices` to see the available indices.",
            details={"argument": "index"},
        )
    return normalised


def _validate_query(query: str) -> str:
    """Reject empty or absurdly long queries."""
    normalised = query.strip()
    if not normalised:
        raise InvalidInputError(
            "`query` must not be empty. Provide the text to search for.",
            details={"argument": "query"},
        )
    if len(normalised) > MAX_QUERY_LENGTH:
        raise InvalidInputError(
            f"`query` must be at most {MAX_QUERY_LENGTH} characters.",
            details={"argument": "query", "length": len(normalised)},
        )
    return normalised


def _resolve_top_k(top_k: int | None, settings: Settings) -> int:
    """Apply the configured default and cap to the requested result count."""
    if top_k is None:
        return settings.default_top_k
    if top_k < 1:
        raise InvalidInputError(
            "`top_k` must be at least 1.",
            details={"argument": "top_k", "value": top_k},
        )
    if top_k > settings.max_top_k:
        raise InvalidInputError(
            f"`top_k` must be at most {settings.max_top_k}.",
            details={"argument": "top_k", "value": top_k, "max": settings.max_top_k},
        )
    return top_k
