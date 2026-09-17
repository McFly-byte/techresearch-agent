"""Unit tests for domain models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from domain.models import Citation, Fact, SearchResult, SourceDocument, SourceKind


def test_citation_requires_valid_id():  # type: ignore[no-untyped-def]
    with pytest.raises(ValidationError):
        Citation(citation_id="not-a-cite", kind=SourceKind.WEB, locator="https://x")


def test_fact_requires_at_least_one_source():  # type: ignore[no-untyped-def]
    with pytest.raises(ValidationError):
        Fact(fact_id="f1", claim="hello", source_citation_ids=[])


def test_search_result_defaults():  # type: ignore[no-untyped-def]
    r = SearchResult(title="t", url="https://x")
    assert r.source == "tavily"
    assert r.authors == []


def test_source_document_error_shape():  # type: ignore[no-untyped-def]
    doc = SourceDocument(
        citation=Citation(citation_id="c1", kind=SourceKind.WEB, locator="https://x"),
        error="boom",
        fetched_ok=False,
    )
    assert doc.fetched_ok is False
