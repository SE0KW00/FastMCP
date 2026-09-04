"""Payload normalisation for backend responses."""

from __future__ import annotations

import pytest

from es_search_mcp.models import IndexList, RetrieveMethod, RetrieveResult


def test_index_aliases_are_accepted():
    result = IndexList.from_payload([{"index_name": "faq", "summary": "FAQ", "count": 3}])
    assert result.indices[0].name == "faq"
    assert result.indices[0].description == "FAQ"
    assert result.indices[0].document_count == 3


@pytest.mark.parametrize("key", ["indices", "items", "data", "results"])
def test_common_envelopes_are_unwrapped(key):
    assert IndexList.from_payload({key: [{"name": "faq"}]}).total == 1


def test_unknown_shapes_are_rejected():
    with pytest.raises(ValueError, match="expected a list"):
        IndexList.from_payload({"nope": 1})


def test_document_aliases_are_accepted():
    result = RetrieveResult.from_payload(
        {"hits": [{"_id": "1", "_score": 0.5, "body": "text", "meta": {"lang": "ko"}}]},
        index="faq",
        query="q",
        method=RetrieveMethod.RRF,
    )
    document = result.documents[0]
    assert (document.id, document.score, document.content) == ("1", 0.5, "text")
    assert document.metadata == {"lang": "ko"}


def test_the_method_is_carried_back_to_the_caller():
    result = RetrieveResult.from_payload([], index="faq", query="q", method=RetrieveMethod.BM25)
    assert result.method is RetrieveMethod.BM25


def test_missing_optional_fields_get_defaults():
    document = RetrieveResult.from_payload(
        [{}], index="faq", query="q", method=RetrieveMethod.RRF
    ).documents[0]
    assert document.id is None
    assert document.score is None
    assert document.content == ""
    assert document.metadata == {}
