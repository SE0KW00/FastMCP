"""Data contracts exchanged with the backend API and returned to MCP clients.

The models are deliberately tolerant on the way in and strict on the way out:
the backend is owned by another team, so field aliases and envelope variations
are absorbed here instead of leaking into the tool layer, while MCP clients
always receive the same normalised shape.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field


class RetrieveMethod(StrEnum):
    """Retrieval strategy, one per backend endpoint.

    The backend exposes each strategy as its own endpoint
    (``/retrieve-bm25``, ``/retrieve-knn``, ...); the value of this enum is the
    suffix that selects it.
    """

    BM25 = "bm25"
    KNN = "knn"
    CC = "cc"
    RRF = "rrf"


#: One-line summaries used to build the tool description the model reads.
RETRIEVE_METHOD_GUIDE: dict[RetrieveMethod, str] = {
    RetrieveMethod.RRF: (
        "Reciprocal Rank Fusion over lexical and vector results. The safe default: "
        "use it unless there is a reason to prefer one signal over the other."
    ),
    RetrieveMethod.BM25: (
        "Lexical keyword matching. Best for exact terms, product codes, error "
        "strings, names and other rare tokens that must appear verbatim."
    ),
    RetrieveMethod.KNN: (
        "Dense vector search. Best for paraphrased or conceptual questions whose "
        "wording is unlikely to appear in the documents."
    ),
    RetrieveMethod.CC: (
        "Convex combination of the lexical and vector scores. A hybrid like `rrf`, "
        "but blending scores rather than ranks; try it when `rrf` ranks poorly."
    ),
}


class IndexInfo(BaseModel):
    """One Elasticsearch index exposed by the backend API."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    name: str = Field(
        validation_alias=AliasChoices("name", "index", "index_name"),
        description="Index name to pass to the `retrieve` tool.",
    )
    description: str = Field(
        default="",
        validation_alias=AliasChoices("description", "desc", "summary"),
        description="What the index contains, in natural language.",
    )
    document_count: int | None = Field(
        default=None,
        validation_alias=AliasChoices("document_count", "doc_count", "count"),
        description="Number of documents in the index, when the backend reports it.",
    )


class IndexList(BaseModel):
    """Result of the ``get_indices`` tool."""

    indices: list[IndexInfo] = Field(default_factory=list)
    total: int = Field(default=0, description="Number of indices returned.")

    @classmethod
    def from_payload(cls, payload: Any) -> IndexList:
        """Build from either a bare list or an object wrapping one.

        Accepted shapes::

            [{"name": ..., "description": ...}, ...]
            {"indices": [...]}     {"items": [...]}     {"data": [...]}
        """
        raw = _unwrap_collection(payload, keys=("indices", "items", "data", "results"))
        indices = [IndexInfo.model_validate(entry) for entry in raw]
        return cls(indices=indices, total=len(indices))


class RetrievedDocument(BaseModel):
    """A single document returned by the retrieve endpoint."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("id", "_id", "doc_id", "document_id"),
        description="Elasticsearch document id.",
    )
    score: float | None = Field(
        default=None,
        validation_alias=AliasChoices("score", "_score", "relevance"),
        description="Relevance score assigned by the backend.",
    )
    content: str = Field(
        default="",
        validation_alias=AliasChoices("content", "text", "body", "chunk"),
        description="Text of the document, ready to be read by the model.",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        validation_alias=AliasChoices("metadata", "meta", "_source", "source", "fields"),
        description="Remaining structured fields carried by the document.",
    )


class RetrieveResult(BaseModel):
    """Result of the ``retrieve`` tool."""

    indices: list[str] = Field(description="Indices the documents were read from.")
    query: str = Field(description="Query that produced these documents.")
    method: RetrieveMethod = Field(description="Retrieval strategy that produced them.")
    total: int = Field(default=0, description="Number of documents returned.")
    documents: list[RetrievedDocument] = Field(default_factory=list)

    @classmethod
    def from_payload(
        cls, payload: Any, *, indices: list[str], query: str, method: RetrieveMethod
    ) -> RetrieveResult:
        """Build from either a bare list of hits or an object wrapping one.

        Accepted shapes::

            [{"id": ..., "content": ...}, ...]
            {"documents": [...]}   {"hits": [...]}   {"results": [...]}
        """
        raw = _unwrap_collection(payload, keys=("documents", "hits", "results", "items", "data"))
        documents = [RetrievedDocument.model_validate(entry) for entry in raw]
        return cls(
            indices=list(indices),
            query=query,
            method=method,
            total=len(documents),
            documents=documents,
        )


def _unwrap_collection(payload: Any, *, keys: tuple[str, ...]) -> list[Any]:
    """Return the list inside ``payload``, looking through common envelopes.

    Raises:
        ValueError: if no list can be found — the caller turns this into a
            :class:`~es_search_mcp.exceptions.BackendPayloadError`.
    """
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                return value
            # Elasticsearch-style nesting: {"hits": {"hits": [...]}}
            if isinstance(value, dict):
                for nested_key in keys:
                    nested = value.get(nested_key)
                    if isinstance(nested, list):
                        return nested
    raise ValueError(f"expected a list under one of {keys}, got {type(payload).__name__}")
