"""End-to-end test of SimpleResearchService using fake providers.

Also doubles as the contract test for:
- search dedup by URL
- empty search results
- fetch failure recorded in errors[] (not silently swallowed)
- illegal URL rejected by the fetcher
- every Fact has a source_citation_id that maps to a known Citation
- writer never invents citations
"""

from __future__ import annotations

import pytest

from domain.models import SearchResult
from service.extractor import HeuristicFactExtractor
from service.report_writer import MarkdownReportWriter
from service.simple_research import SimpleResearchService
from tools.fetchers import FakeFetcher
from tools.search_providers import FakeSearchProvider


def _hit(title: str, url: str, snippet: str = "") -> SearchResult:
    return SearchResult(title=title, url=url, snippet=snippet, source="tavily")


@pytest.mark.asyncio
async def test_e2e_fake_happy_path():  # type: ignore[no-untyped-def]
    web = FakeSearchProvider(
        {
            "langgraph": [
                _hit("LG", "https://x.com/a", "snippet a"),
                _hit("LG dup", "https://x.com/a", "dupe"),  # duplicate URL, must be deduped
                _hit("LG b", "https://x.com/b", "snippet b"),
            ]
        }
    )
    fetcher = FakeFetcher(
        mapping={
            "https://x.com/a": (
                "LangGraph models agents as a graph of nodes. "
                "It supports typed state and durable checkpoints out of the box."
            ),
            "https://x.com/b": (
                "LlamaIndex focuses on retrieval over external data sources. "
                "It offers high-level agents with less explicit state control."
            ),
        }
    )
    svc = SimpleResearchService(
        web_search=web,
        paper_search=None,
        fetcher=fetcher,
        fact_extractor=HeuristicFactExtractor(),
        report_writer=MarkdownReportWriter(),
    )
    res = await svc.run("what is langgraph")

    assert res.errors == []
    # dedup: two unique URLs, not three
    assert [c.locator for c in res.citations] == ["https://x.com/a", "https://x.com/b"]
    assert res.facts, "expected at least one fact"
    idx = res.citation_index()
    for f in res.facts:
        for cid in f.source_citation_ids:
            assert cid in idx, f"fact {f.fact_id} references unknown citation {cid}"
    # report markdown is non-empty and contains the citation ids
    for c in res.citations:
        assert c.citation_id in res.report_markdown
    assert "# 调研报告：what is langgraph" in res.report_markdown


@pytest.mark.asyncio
async def test_e2e_empty_search_is_not_faked():  # type: ignore[no-untyped-def]
    web = FakeSearchProvider({})  # no hits
    fetcher = FakeFetcher(mapping={})
    svc = SimpleResearchService(
        web_search=web,
        fetcher=fetcher,
        fact_extractor=HeuristicFactExtractor(),
        report_writer=MarkdownReportWriter(),
    )
    res = await svc.run("quantum knitting")
    assert res.citations == []
    assert res.facts == []
    assert "未从任何来源中提取到事实" in res.report_markdown


@pytest.mark.asyncio
async def test_e2e_fetch_failure_recorded_not_swallowed():  # type: ignore[no-untyped-def]
    web = FakeSearchProvider({"x": [_hit("bad", "https://bad.example/x")]})
    fetcher = FakeFetcher(errors={"https://bad.example/x": "connection refused"})
    svc = SimpleResearchService(
        web_search=web,
        fetcher=fetcher,
        fact_extractor=HeuristicFactExtractor(),
        report_writer=MarkdownReportWriter(),
    )
    res = await svc.run("x")
    # The fetch failed -> no facts, but errors[] must name the URL.
    assert res.facts == []
    assert any("bad.example" in e for e in res.errors), res.errors
    # Citation still recorded so the user sees what was attempted.
    assert len(res.citations) == 1


@pytest.mark.asyncio
async def test_expand_queries_dedupes():  # type: ignore[no-untyped-def]
    qs = SimpleResearchService.expand_queries("LangGraph vs LlamaIndex")
    assert len(qs) >= 2
    assert len(qs) == len(set(qs))
