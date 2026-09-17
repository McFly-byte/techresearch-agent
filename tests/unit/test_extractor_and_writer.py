"""Tests for fact extractor and report writer."""

from __future__ import annotations

import pytest

from core.exceptions import ToolError
from domain.models import Citation, Fact, SourceDocument, SourceKind
from service.extractor import HeuristicFactExtractor
from service.report_writer import MarkdownReportWriter


def _doc(cid: str, text: str) -> SourceDocument:
    return SourceDocument(
        citation=Citation(citation_id=cid, kind=SourceKind.WEB, locator=f"https://{cid}"),
        content=text,
    )


@pytest.mark.asyncio
async def test_extractor_emits_verbatim_sentences():  # type: ignore[no-untyped-def]
    text = (
        "LangGraph models agents as a graph of nodes. "
        "It provides typed state and checkpoints for long-running work. "
        "Short. "
        "It is maintained by the LangChain team under an MIT-style license."
    )
    docs = [_doc("c1", text)]
    facts = await HeuristicFactExtractor().extract(docs)
    assert len(facts) >= 2
    for f in facts:
        # claim must be a verbatim substring of the source
        assert f.claim in text
        assert f.source_citation_ids == ["c1"]


@pytest.mark.asyncio
async def test_extractor_skips_failed_docs():  # type: ignore[no-untyped-def]
    bad = SourceDocument(
        citation=Citation(citation_id="c1", kind=SourceKind.WEB, locator="x"),
        error="boom",
        fetched_ok=False,
    )
    facts = await HeuristicFactExtractor().extract([bad])
    assert facts == []


def test_writer_renders_citations():  # type: ignore[no-untyped-def]
    cites = [Citation(citation_id="c1", kind=SourceKind.WEB, locator="https://a", title="A")]
    facts = [Fact(fact_id="f1", claim="some claim", source_citation_ids=["c1"])]
    md = MarkdownReportWriter().render(query="q", facts=facts, citations=cites)
    assert "# 调研报告：q" in md
    assert "[c1]" in md
    assert "some claim" in md
    assert "c1" in md  # citation section


def test_writer_refuses_unknown_citation():  # type: ignore[no-untyped-def]
    cites = [Citation(citation_id="c1", kind=SourceKind.WEB, locator="https://a")]
    facts = [Fact(fact_id="f1", claim="x", source_citation_ids=["c99"])]
    with pytest.raises(ToolError):
        MarkdownReportWriter().render(query="q", facts=facts, citations=cites)


def test_writer_empty_facts_is_explicit():  # type: ignore[no-untyped-def]
    md = MarkdownReportWriter().render(query="q", facts=[], citations=[])
    assert "未从任何来源中提取到事实" in md
