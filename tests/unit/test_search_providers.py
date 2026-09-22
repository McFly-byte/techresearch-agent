"""Tests for search providers (fake) and URL validation."""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from core.exceptions import (
    ProviderNotConfiguredError,
    ToolAuthenticationError,
    ToolQuotaExceededError,
    TransientToolError,
)
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
async def test_tavily_pool_round_robins_across_provider_instances(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class Client:
        def __init__(self, *, api_key: str):
            self.api_key = api_key

        def search(self, **_kwargs):  # type: ignore[no-untyped-def]
            calls.append(self.api_key)
            return {"results": []}

    monkeypatch.setitem(sys.modules, "tavily", SimpleNamespace(TavilyClient=Client))
    keys = ("round-robin-a", "round-robin-b")
    first = TavilySearchProvider(api_key=keys)
    second = TavilySearchProvider(api_key=keys)

    await first.search("one")
    await second.search("two")

    assert calls == ["round-robin-a", "round-robin-b"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["account usage limit reached", "403 forbidden"])
async def test_tavily_pool_disables_bad_key_and_fails_over(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    calls: list[str] = []

    class Client:
        def __init__(self, *, api_key: str):
            self.api_key = api_key

        def search(self, **_kwargs):  # type: ignore[no-untyped-def]
            calls.append(self.api_key)
            if self.api_key == f"bad-{failure}":
                raise RuntimeError(failure)
            return {"results": []}

    monkeypatch.setitem(sys.modules, "tavily", SimpleNamespace(TavilyClient=Client))
    provider = TavilySearchProvider(api_key=(f"bad-{failure}", f"good-{failure}"))

    assert await provider.search("query") == []
    assert calls == [f"bad-{failure}", f"good-{failure}"]


@pytest.mark.asyncio
async def test_tavily_pool_reports_auth_when_every_key_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Client:
        def __init__(self, **_kwargs):  # type: ignore[no-untyped-def]
            pass

        def search(self, **_kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("401 unauthorized")

    monkeypatch.setitem(sys.modules, "tavily", SimpleNamespace(TavilyClient=Client))
    provider = TavilySearchProvider(api_key=("auth-a", "auth-b"))

    with pytest.raises(ToolAuthenticationError, match="credentials were rejected"):
        await provider.search("query")


@pytest.mark.asyncio
async def test_tavily_pool_recognizes_sdk_invalid_api_key_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    class InvalidAPIKeyError(Exception):
        pass

    class Client:
        def __init__(self, **_kwargs):  # type: ignore[no-untyped-def]
            pass

        def search(self, **_kwargs):  # type: ignore[no-untyped-def]
            nonlocal calls
            calls += 1
            raise InvalidAPIKeyError()

    monkeypatch.setitem(sys.modules, "tavily", SimpleNamespace(TavilyClient=Client))
    provider = TavilySearchProvider(api_key=("invalid-sdk-a", "invalid-sdk-b"))

    with pytest.raises(ToolAuthenticationError, match="credentials were rejected"):
        await provider.search("query")
    assert calls == 2


@pytest.mark.asyncio
async def test_tavily_pool_classifies_sdk_ssl_error_as_transient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    class SSLError(Exception):
        pass

    class Client:
        def __init__(self, **_kwargs):  # type: ignore[no-untyped-def]
            pass

        def search(self, **_kwargs):  # type: ignore[no-untyped-def]
            nonlocal calls
            calls += 1
            raise SSLError()

    monkeypatch.setitem(sys.modules, "tavily", SimpleNamespace(TavilyClient=Client))
    provider = TavilySearchProvider(api_key=("ssl-a", "ssl-b"))

    with pytest.raises(TransientToolError, match="temporarily unavailable"):
        await provider.search("query")
    assert calls == 4


@pytest.mark.asyncio
async def test_fake_paper_search():  # type: ignore[no-untyped-def]
    hits = [SearchResult(title="p", url="https://arxiv.org/abs/1", snippet="s", source="arxiv")]
    pp = FakePaperSearchProvider(hits)
    out = await pp.search("transformers", max_results=1)
    assert out == hits
