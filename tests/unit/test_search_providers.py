"""Tests for search providers (fake) and URL validation."""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from core.exceptions import ProviderNotConfiguredError, ToolQuotaExceededError
from domain.models import SearchResult
from tools.netutil import is_http_url, truncate
from tools.paper_providers import FakePaperSearchProvider
from tools.search_providers import FakeSearchProvider, TavilySearchProvider


def test_is_http_url():  # type: ignore[no-untyped-def]
    assert is_http_url("https://example.com/x")
    assert is_http_url("http://example.com")
    assert not is_http_url("ftp://example.com")
    assert not is_http_url("file:///etc/passwd")
    assert not is_http_url("not a url")


def test_truncate():  # type: ignore[no-untyped-def]
    assert truncate("abc", 10) == "abc"
    out = truncate("a" * 100, 10)
    assert out.startswith("a" * 10)
    assert "truncated" in out


@pytest.mark.asyncio
async def test_fake_search_returns_matching():  # type: ignore[no-untyped-def]
    hits = [SearchResult(title="t", url="https://x", snippet="s")]
    sp = FakeSearchProvider({"langgraph": hits})
    out = await sp.search("what is langgraph", max_results=3)
    assert out == hits
    assert sp.calls == [("what is langgraph", 3)]


@pytest.mark.asyncio
async def test_fake_search_no_match():  # type: ignore[no-untyped-def]
    sp = FakeSearchProvider({"langgraph": []})
    out = await sp.search("quantum knitting", max_results=3)
    assert out == []


def test_tavily_provider_requires_key():  # type: ignore[no-untyped-def]
    with pytest.raises(ProviderNotConfiguredError):
        TavilySearchProvider(api_key="")


@pytest.mark.asyncio
async def test_tavily_usage_limit_has_stable_quota_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Client:
        def __init__(self, **_kwargs):  # type: ignore[no-untyped-def]
            pass

        def search(self, **_kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("account usage limit reached; private provider text")

    monkeypatch.setitem(sys.modules, "tavily", SimpleNamespace(TavilyClient=Client))
    provider = TavilySearchProvider(api_key="not-a-real-key")

    with pytest.raises(ToolQuotaExceededError, match="tavily search quota exhausted"):
        await provider.search("query", max_results=1)


@pytest.mark.asyncio
async def test_fake_paper_search():  # type: ignore[no-untyped-def]
    hits = [SearchResult(title="p", url="https://arxiv.org/abs/1", snippet="s", source="arxiv")]
    pp = FakePaperSearchProvider(hits)
    out = await pp.search("transformers", max_results=1)
    assert out == hits
