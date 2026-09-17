"""SimpleResearchService — the phase 1 vertical slice.

Pipeline (linear, no LangGraph):
  query
    -> expand to 2-3 search queries (heuristic)
    -> web search (Tavily or fake)
    -> optional arxiv search
    -> dedupe by URL
    -> fetch each page / doc
    -> extract facts (heuristic)
    -> render markdown (writer)

Failures are recorded in `result.errors` and the pipeline continues. A fetch
that yields no content does NOT become a "successful empty fact" — it just
produces no facts, and the empty case is handled by the writer explicitly.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, Protocol

from core.exceptions import ToolError
from domain.models import (
    Fact,
    SearchResult,
    SimpleResearchResult,
    SourceDocument,
    SourceKind,
)

log = logging.getLogger(__name__)

MAX_SEARCH_QUERIES = 3
MAX_RESULTS_PER_QUERY = 4


class _HasName(Protocol):
    name: str


class SimpleResearchService:
    """Wires together the phase 1 tools. Construct with explicit fakes in tests."""

    def __init__(
        self,
        *,
        web_search: Any,  # SearchProvider
        paper_search: Any | None = None,  # PaperSearchProvider | None
        fetcher: Any,  # PageFetcher
        fact_extractor: Any,  # FactExtractor
        report_writer: Any,  # ReportWriter
        max_results_per_query: int = MAX_RESULTS_PER_QUERY,
    ) -> None:
        self._web = web_search
        self._paper = paper_search
        self._fetcher = fetcher
        self._extractor = fact_extractor
        self._writer = report_writer
        self._max_results = max_results_per_query

    @staticmethod
    def expand_queries(query: str) -> list[str]:
        """Phase 1 heuristic: user query + one paraphrase. No LLM."""
        q = query.strip()
        out = [q]
        if " vs " in q.lower():
            out.append(q.replace(" vs ", " versus "))
        else:
            out.append(f"{q} comparison")
        # de-dup, preserve order
        seen: set[str] = set()
        unique: list[str] = []
        for qq in out:
            if qq not in seen:
                seen.add(qq)
                unique.append(qq)
        return unique[:MAX_SEARCH_QUERIES]

    async def run(self, query: str, *, include_papers: bool = False) -> SimpleResearchResult:
        errors: list[str] = []
        queries = self.expand_queries(query)

        # 1) Search
        hits: list[SearchResult] = []
        for q in queries:
            try:
                hits.extend(await self._web.search(q, max_results=self._max_results))
            except ToolError as e:
                errors.append(f"web_search[{q}]: {e.message}")
            if include_papers and self._paper is not None:
                try:
                    hits.extend(await self._paper.search(q, max_results=self._max_results))
                except ToolError as e:
                    errors.append(f"paper_search[{q}]: {e.message}")

        # 2) Dedupe by URL (keep first). arXiv entries keep their entry_id as URL.
        seen_urls: set[str] = set()
        unique_hits: list[SearchResult] = []
        for h in hits:
            key = h.url.strip()
            if not key or key in seen_urls:
                continue
            seen_urls.add(key)
            unique_hits.append(h)

        # 3) Fetch
        docs: list[SourceDocument] = []
        for i, h in enumerate(unique_hits, start=1):
            cid = f"c{i}"
            try:
                doc = await self._fetcher.fetch(h.url, citation_id=cid)
            except ToolError as e:
                errors.append(f"fetch[{h.url}]: {e.message}")
                continue
            if not doc.fetched_ok:
                errors.append(f"fetch[{h.url}]: {doc.error}")
            # Use search metadata for title if fetch produced none.
            if not doc.citation.title and h.title:
                doc.citation = doc.citation.model_copy(update={"title": h.title})
            if h.source == "arxiv":
                doc.citation = doc.citation.model_copy(update={"kind": SourceKind.ARXIV})
            docs.append(doc)

        citations = [d.citation for d in docs]

        # 4) Extract
        facts: list[Fact] = []
        try:
            facts = await self._extractor.extract(docs)
        except ToolError as e:
            errors.append(f"extract: {e.message}")

        # 5) Write
        try:
            md = self._writer.render(query=query, facts=facts, citations=citations)
        except ToolError as e:
            errors.append(f"writer: {e.message}")
            md = ""

        return SimpleResearchResult(
            query=query,
            queries_used=queries,
            citations=citations,
            facts=facts,
            report_markdown=md,
            errors=errors,
            finished_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )


__all__ = ["SimpleResearchService"]
