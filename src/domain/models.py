"""Phase 1 domain models.

These are the minimal structured objects that flow through the simple research
pipeline. They are deliberately narrower than the full LangGraph ResearchState
from the design doc (ch.3) — that state comes in phase 2 when we add
Supervisor-Worker. Everything here is plain Pydantic, no LangGraph imports.

Traceability contract (enforced by ReportWriter + tests):
- Every Fact MUST point to at least one Citation id.
- Every Citation MUST have a stable source locator (URL or local path) and a
  non-empty snippet.
- The ReportWriter MUST NOT invent citations that do not exist in the input.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class SourceKind(StrEnum):
    WEB = "web"
    ARXIV = "arxiv"
    LOCAL_MD = "local_md"
    LOCAL_PDF = "local_pdf"


class Citation(BaseModel):
    """A single traceable source. One SourceDocument yields one Citation."""

    citation_id: str = Field(pattern=r"^[A-Za-z0-9_]+$")
    kind: SourceKind
    title: str = ""
    locator: str = Field(description="URL for web/arxiv, absolute path for local files")
    snippet: str = Field(default="", description="verbatim excerpt, never paraphrased")
    fetched_at: str = Field(default_factory=_now_iso)
    # Phase 2: which sub-task fetched this. Preserves provenance across parallel merge.
    source_task_id: str = ""

    def short_label(self) -> str:
        return self.title or self.locator


class Fact(BaseModel):
    """A structured claim extracted from one or more sources.

    The `claim` MUST be supported by the snippet(s) of every cited source.
    Phase 1 uses a deterministic heuristic extractor, so this is enforced by
    construction: each Fact is a sentence copied verbatim from a source.
    """

    fact_id: str = Field(pattern=r"^[A-Za-z0-9_]+$")
    claim: str
    source_citation_ids: list[str] = Field(min_length=1)
    extracted_at: str = Field(default_factory=_now_iso)
    # Phase 2: which sub-task produced this fact.
    source_task_id: str = ""


class SearchResult(BaseModel):
    """One hit from a search provider (before fetching the page)."""

    title: str
    url: str
    snippet: str = ""
    source: Literal["tavily", "arxiv", "local"] = "tavily"
    # arXiv-only metadata; empty for web.
    authors: list[str] = Field(default_factory=list)
    published: str = ""


class SourceDocument(BaseModel):
    """A fetched / loaded source, ready for fact extraction."""

    citation: Citation
    content: str = Field(default="", description="extracted main text, truncated upstream")
    error: str = ""
    fetched_ok: bool = True


class SimpleResearchResult(BaseModel):
    """Output of SimpleResearchService.run()."""

    query: str
    queries_used: list[str] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    facts: list[Fact] = Field(default_factory=list)
    report_markdown: str = ""
    errors: list[str] = Field(default_factory=list)
    started_at: str = Field(default_factory=_now_iso)
    finished_at: str = ""

    def citation_index(self) -> dict[str, Citation]:
        return {c.citation_id: c for c in self.citations}


__all__ = [
    "Citation",
    "Fact",
    "SearchResult",
    "SimpleResearchResult",
    "SourceDocument",
    "SourceKind",
]
