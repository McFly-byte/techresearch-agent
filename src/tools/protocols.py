"""Protocols (abstract interfaces) for the tools used by SimpleResearchService.

Every adapter implements one of these. The orchestrator depends only on the
Protocol, so tests can inject fakes without touching live network code.

Conventions shared by all adapters:
- Async methods only (the FastAPI layer is async).
- Network calls have a hard timeout (default 15s) and at most 1 retry on
  transient errors.
- URLs are validated with `url_validation.is_http_url()` before any fetch.
- Large responses are truncated to `MAX_BODY_CHARS` inside the adapter.
- Failures raise `ToolError` subclasses defined in `core.exceptions`; the
  orchestrator never swallows them into an "empty success".
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from core.exceptions import ToolError  # re-exported for adapters
from domain.models import Fact, SearchResult, SourceDocument


@runtime_checkable
class SearchProvider(Protocol):
    """Web search (Tavily in production, Fake in tests)."""

    name: str

    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]: ...


@runtime_checkable
class PaperSearchProvider(Protocol):
    """Academic paper search (arXiv in production)."""

    name: str

    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]: ...


@runtime_checkable
class PageFetcher(Protocol):
    """Fetch / read a single locator into a SourceDocument."""

    name: str

    async def fetch(self, locator: str, *, citation_id: str) -> SourceDocument: ...


@runtime_checkable
class FactExtractor(Protocol):
    """Pull structured facts out of fetched SourceDocuments."""

    name: str

    async def extract(self, docs: list[SourceDocument]) -> list[Fact]: ...


@runtime_checkable
class ReportWriter(Protocol):
    """Render facts + citations into Markdown.

    MUST only cite citation ids that appear in the input.
    """

    name: str

    def render(self, *, query: str, facts: list[Fact], citations: list) -> str:  # noqa: ANN001
        ...


__all__ = [
    "FactExtractor",
    "PageFetcher",
    "PaperSearchProvider",
    "ReportWriter",
    "SearchProvider",
    "ToolError",
]
