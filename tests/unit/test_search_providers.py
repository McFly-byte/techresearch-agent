"""Tests for search providers (fake) and URL validation."""

from __future__ import annotations

import asyncio
import sys
import threading
import time
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
async def test_tavily_pool_serializes_each_key_but_keeps_keys_parallel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stats_lock = threading.Lock()
    active_by_key: dict[str, int] = {}
    max_by_key: dict[str, int] = {}
    active_total = 0
    max_total = 0

    class Client:
        def __init__(self, *, api_key: str):
            self.api_key = api_key

        def search(self, **_kwargs):  # type: ignore[no-untyped-def]
            nonlocal active_total, max_total
            with stats_lock:
                active_by_key[self.api_key] = active_by_key.get(self.api_key, 0) + 1
                max_by_key[self.api_key] = max(
                    max_by_key.get(self.api_key, 0), active_by_key[self.api_key]
                )
                active_total += 1
                max_total = max(max_total, active_total)
            time.sleep(0.05)
            with stats_lock:
                active_by_key[self.api_key] -= 1
                active_total -= 1
            return {"results": []}

    monkeypatch.setitem(sys.modules, "tavily", SimpleNamespace(TavilyClient=Client))
    keys = ("serialized-a", "serialized-b")
    first = TavilySearchProvider(api_key=keys)
    second = TavilySearchProvider(api_key=keys)

    await asyncio.gather(
        first.search("one"),
        second.search("two"),
        first.search("three"),
        second.search("four"),
    )

    assert max_by_key == {"serialized-a": 1, "serialized-b": 1}
    assert max_total == 2


@pytest.mark.asyncio
async def test_tavily_waiter_rechecks_key_after_circuit_break(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    calls_lock = threading.Lock()

    class Client:
        def __init__(self, *, api_key: str):
            self.api_key = api_key

        def search(self, **_kwargs):  # type: ignore[no-untyped-def]
            with calls_lock:
                calls.append(self.api_key)
            if self.api_key == "queued-bad":
                time.sleep(0.03)
                raise RuntimeError("401 unauthorized")
            time.sleep(0.06)
            return {"results": []}

    monkeypatch.setitem(sys.modules, "tavily", SimpleNamespace(TavilyClient=Client))
    provider = TavilySearchProvider(api_key=("queued-bad", "queued-good"))

    results = await asyncio.gather(
        provider.search("one"),
        provider.search("two"),
        provider.search("three"),
    )

    assert results == [[], [], []]
    assert calls.count("queued-bad") == 1
    assert calls.count("queued-good") == 3


@pytest.mark.asyncio
async def test_tavily_auth_breaker_reason_reaches_waiters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    calls_lock = threading.Lock()

    class Client:
        def __init__(self, **_kwargs):  # type: ignore[no-untyped-def]
            pass

        def search(self, **_kwargs):  # type: ignore[no-untyped-def]
            nonlocal calls
            with calls_lock:
                calls += 1
            time.sleep(0.03)
            raise RuntimeError("401 unauthorized")

    monkeypatch.setitem(sys.modules, "tavily", SimpleNamespace(TavilyClient=Client))
    provider = TavilySearchProvider(api_key="one-bad-key")

    results = await asyncio.gather(
        provider.search("one"),
        provider.search("two"),
        provider.search("three"),
        return_exceptions=True,
    )

    assert calls == 1
    assert all(isinstance(result, ToolAuthenticationError) for result in results)


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
