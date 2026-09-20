"""Search providers: Tavily (live) and Fake (offline tests)."""

from __future__ import annotations

import asyncio
import logging
import weakref
from typing import Any

from core.config import Settings
from core.exceptions import ProviderNotConfiguredError, ToolError, TransientToolError
from domain.models import SearchResult

log = logging.getLogger(__name__)

# Hard caps (seconds / count) enforced by every adapter.
SEARCH_TIMEOUT_S = 10
MAX_RETRIES = 1
MAX_RESULT_CHARS = 4_000
_GLOBAL_TAVILY_INFLIGHT = 4
_LOOP_LIMITERS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = (
    weakref.WeakKeyDictionary()
)


def _tavily_limiter() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    limiter = _LOOP_LIMITERS.get(loop)
    if limiter is None:
        limiter = asyncio.Semaphore(_GLOBAL_TAVILY_INFLIGHT)
        _LOOP_LIMITERS[loop] = limiter
    return limiter


class TavilySearchProvider:
    """Async wrapper around tavily-python with timeout + one retry.

    We keep the import lazy so importing this module does not require the
    tavily package to be installed (tests use FakeSearchProvider).
    """

    name = "tavily"

    def __init__(self, api_key: str, *, timeout: float = SEARCH_TIMEOUT_S) -> None:
        if not api_key:
            raise ProviderNotConfiguredError("TAVILY_API_KEY is empty.")
        self._api_key = api_key
        self._timeout = timeout

    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        try:
            import tavily
        except ImportError as e:  # pragma: no cover - environment dependent
            raise ToolError("tavily-python is not installed.") from e

        def _call() -> dict[str, Any]:
            client = tavily.TavilyClient(api_key=self._api_key)
            return client.search(
                query=query,
                max_results=max_results,
                search_depth="basic",
                include_answer=False,
            )

        last_exc: Exception | None = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                async with _tavily_limiter():
                    data = await asyncio.wait_for(asyncio.to_thread(_call), timeout=self._timeout)
                break
            except TimeoutError as e:
                last_exc = e
                if attempt >= MAX_RETRIES:
                    raise ToolError(f"tavily search timed out after {self._timeout}s") from e
            except Exception as e:  # tavily raises generic RuntimeError on 4xx/5xx
                last_exc = e
                msg = str(e).lower()
                retryable = any(t in msg for t in ("429", "502", "503", "504", "timeout", "rate"))
                if not retryable or attempt >= MAX_RETRIES:
                    raise ToolError(f"tavily search failed: {e}") from e
                await asyncio.sleep(0.5 * (attempt + 1))
        else:  # pragma: no cover
            raise TransientToolError(f"tavily exhausted retries: {last_exc}")

        out: list[SearchResult] = []
        for r in (data or {}).get("results", []):
            content = (r.get("content") or "")[:MAX_RESULT_CHARS]
            out.append(
                SearchResult(
                    title=(r.get("title") or "").strip(),
                    url=r.get("url", ""),
                    snippet=content,
                    source="tavily",
                )
            )
        return out


class FakeSearchProvider:
    """Deterministic offline search. Returns canned results keyed by query substring."""

    name = "fake_search"

    def __init__(self, results: dict[str, list[SearchResult]] | None = None) -> None:
        self._results = results or {}
        self.calls: list[tuple[str, int]] = []

    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        self.calls.append((query, max_results))
        for key, hits in self._results.items():
            if key.lower() in query.lower():
                return hits[:max_results]
        return []


def build_search_provider(settings: Settings) -> Any:
    """Pick live Tavily iff key is set and provider is not forced to fake."""
    if settings.llm_provider == "fake" or not settings.has_tavily_key:
        return FakeSearchProvider()
    return TavilySearchProvider(api_key=settings.tavily_api_key.get_secret_value())


__all__ = ["FakeSearchProvider", "TavilySearchProvider", "build_search_provider"]
