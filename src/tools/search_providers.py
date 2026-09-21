"""Search providers: Tavily (live) and Fake (offline tests)."""

from __future__ import annotations

import asyncio
import logging
import threading
import weakref
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from core.config import Settings
from core.exceptions import (
    ProviderNotConfiguredError,
    ToolAuthenticationError,
    ToolError,
    ToolQuotaExceededError,
    TransientToolError,
)
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


@dataclass
class _TavilyPoolState:
    """Process-wide rotation and circuit-breaker state for one key pool."""

    keys: tuple[str, ...]
    next_index: int = 0
    disabled: set[int] = field(default_factory=set)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def acquire(self, excluded: set[int]) -> tuple[int, str] | None:
        with self.lock:
            for offset in range(len(self.keys)):
                index = (self.next_index + offset) % len(self.keys)
                if index in self.disabled or index in excluded:
                    continue
                self.next_index = (index + 1) % len(self.keys)
                return index, self.keys[index]
        return None

    def disable(self, index: int) -> None:
        with self.lock:
            self.disabled.add(index)

    def all_disabled(self) -> bool:
        with self.lock:
            return len(self.disabled) == len(self.keys)


_POOL_STATES: dict[tuple[str, ...], _TavilyPoolState] = {}
_POOL_STATES_LOCK = threading.Lock()


def _pool_state(keys: tuple[str, ...]) -> _TavilyPoolState:
    with _POOL_STATES_LOCK:
        state = _POOL_STATES.get(keys)
        if state is None:
            state = _TavilyPoolState(keys=keys)
            _POOL_STATES[keys] = state
        return state


def _tavily_limiter() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    limiter = _LOOP_LIMITERS.get(loop)
    if limiter is None:
        limiter = asyncio.Semaphore(_GLOBAL_TAVILY_INFLIGHT)
        _LOOP_LIMITERS[loop] = limiter
    return limiter


class TavilySearchProvider:
    """Async Tavily wrapper with bounded retries and shared key failover.

    We keep the import lazy so importing this module does not require the
    tavily package to be installed (tests use FakeSearchProvider).
    """

    name = "tavily"

    def __init__(self, api_key: str | Sequence[str], *, timeout: float = SEARCH_TIMEOUT_S) -> None:
        raw_keys = (api_key,) if isinstance(api_key, str) else tuple(api_key)
        keys = tuple(dict.fromkeys(key.strip() for key in raw_keys if key.strip()))
        if not keys:
            raise ProviderNotConfiguredError("TAVILY_API_KEY/TAVILY_API_KEYS is empty.")
        self._pool = _pool_state(keys)
        self._timeout = timeout

    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        try:
            import tavily
        except ImportError as e:  # pragma: no cover - environment dependent
            raise ToolError("tavily-python is not installed.") from e

        def _call(key: str) -> dict[str, Any]:
            client = tavily.TavilyClient(api_key=key)
            return client.search(
                query=query,
                max_results=max_results,
                search_depth="basic",
                include_answer=False,
            )

        tried: set[int] = set()
        saw_quota = False
        saw_auth = False
        saw_transient = False
        last_exc: Exception | None = None
        data: dict[str, Any] | None = None

        while selected := self._pool.acquire(tried):
            key_index, key = selected
            tried.add(key_index)
            rotate = False
            for attempt in range(MAX_RETRIES + 1):
                try:
                    async with _tavily_limiter():
                        data = await asyncio.wait_for(
                            asyncio.to_thread(_call, key), timeout=self._timeout
                        )
                    break
                except TimeoutError as e:
                    last_exc = e
                    saw_transient = True
                    rotate = attempt >= MAX_RETRIES
                except Exception as e:  # Tavily emits generic errors for HTTP failures.
                    last_exc = e
                    msg = str(e).lower()
                    if any(
                        marker in msg
                        for marker in ("usage limit", "quota", "credit limit", "credits exhausted")
                    ):
                        saw_quota = True
                        self._pool.disable(key_index)
                        rotate = True
                        break
                    if any(marker in msg for marker in ("401", "403", "unauthorized", "forbidden")):
                        saw_auth = True
                        self._pool.disable(key_index)
                        rotate = True
                        break
                    retryable = any(
                        marker in msg for marker in ("429", "502", "503", "504", "timeout", "rate")
                    )
                    if retryable:
                        saw_transient = True
                    rotate = not retryable or attempt >= MAX_RETRIES
                if rotate:
                    break
                await asyncio.sleep(0.5 * (attempt + 1))
            if data is not None:
                break
            if not rotate:  # pragma: no cover - defensive
                break

        if data is None:
            if self._pool.all_disabled():
                if saw_quota:
                    raise ToolQuotaExceededError(
                        "tavily search quota exhausted for all configured keys"
                    ) from last_exc
                if saw_auth:
                    raise ToolAuthenticationError(
                        "all configured Tavily credentials were rejected"
                    ) from last_exc
            if saw_transient:
                raise TransientToolError(
                    "all active Tavily credentials are temporarily unavailable"
                ) from last_exc
            raise ToolError("tavily search failed for all active credentials") from last_exc

        out: list[SearchResult] = []
        for r in data.get("results", []):
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
    return TavilySearchProvider(api_key=settings.tavily_key_pool())


__all__ = ["FakeSearchProvider", "TavilySearchProvider", "build_search_provider"]
