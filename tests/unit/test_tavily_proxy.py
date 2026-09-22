from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from cli.tavily_proxy import sync_keys
from core.config import Settings
from core.exceptions import (
    ToolAuthenticationError,
    ToolQuotaExceededError,
    TransientToolError,
)
from tools.search_providers import TavilyProxySearchProvider, build_search_provider


def test_sync_proxy_keys_deduplicates_without_printing_values(tmp_path: Path) -> None:
    settings = Settings(
        tavily_api_key=SecretStr("secret-a"),
        tavily_api_keys=SecretStr("secret-a,secret-b"),
    )
    target = tmp_path / "key.txt"

    assert sync_keys(target, settings) == 2
    assert target.read_text(encoding="utf-8") == "secret-a\nsecret-b\n"


def test_build_search_provider_prefers_proxy() -> None:
    settings = Settings(
        llm_provider="qwen",
        tavily_api_key=SecretStr("direct-key"),
        tavily_proxy_url="http://127.0.0.1:15280",
    )

    assert isinstance(build_search_provider(settings), TavilyProxySearchProvider)


@pytest.mark.asyncio
async def test_proxy_search_returns_tavily_results() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/search"
        return httpx.Response(
            200,
            json={
                "results": [
                    {"title": " result ", "url": "https://example.com", "content": "body"}
                ]
            },
        )

    provider = TavilyProxySearchProvider(
        "http://proxy.local", transport=httpx.MockTransport(handler)
    )

    results = await provider.search("query", max_results=1)

    assert len(results) == 1
    assert results[0].title == "result"
    assert results[0].source == "tavily"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("last_error", "error_type"),
    [
        ("401 account deactivated", ToolAuthenticationError),
        ("432 usage credit exhausted", ToolQuotaExceededError),
    ],
)
async def test_proxy_exhaustion_reports_diagnostic_cause(
    last_error: str, error_type: type[Exception]
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/accounts":
            return httpx.Response(200, json={"accounts": [{"last_error": last_error}]})
        return httpx.Response(
            503,
            json={
                "error": {
                    "code": "all_tavily_accounts_exhausted",
                    "message": "No Tavily account has remaining credits.",
                }
            },
        )

    provider = TavilyProxySearchProvider(
        "http://proxy.local", transport=httpx.MockTransport(handler)
    )

    with pytest.raises(error_type, match="tavily proxy"):
        await provider.search("query")


@pytest.mark.asyncio
async def test_proxy_busy_is_transient_and_actionable() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            503,
            headers={"retry-after": "1"},
            json={"error": {"code": "all_tavily_accounts_busy", "message": "busy"}},
        )

    provider = TavilyProxySearchProvider(
        "http://proxy.local", transport=httpx.MockTransport(handler)
    )

    with pytest.raises(TransientToolError, match="all keys busy; retry_after=1s"):
        await provider.search("query")


@pytest.mark.asyncio
async def test_proxy_empty_pool_uses_quarantine_reason_without_exposing_key(
    tmp_path: Path,
) -> None:
    diagnostics = tmp_path / "error_key.txt"
    diagnostics.write_text(
        '{"api_key":"must-not-leak","reason":"usage_unauthorized","failure_count":3}\n',
        encoding="utf-8",
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/accounts":
            return httpx.Response(200, json={"accounts": []})
        return httpx.Response(
            503,
            json={"error": {"code": "all_tavily_accounts_exhausted", "message": "empty"}},
        )

    provider = TavilyProxySearchProvider(
        "http://proxy.local",
        transport=httpx.MockTransport(handler),
        diagnostics_file=diagnostics,
    )

    with pytest.raises(ToolAuthenticationError) as caught:
        await provider.search("query")
    assert "must-not-leak" not in str(caught.value)
