"""Search providers: Tavily (live) and Fake (offline tests)."""

from __future__ import annotations

import asyncio
import logging
import threading
import weakref
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

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
_LOOP_KEY_LOCKS: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop,
    dict[tuple[str, ...], tuple[asyncio.Lock, ...]],
] = weakref.WeakKeyDictionary()


@dataclass
class _TavilyPoolState:
    """Process-wide rotation and circuit-breaker state for one key pool."""

    keys: tuple[str, ...]
    next_index: int = 0
    disabled: set[int] = field(default_factory=set)
    disable_reasons: dict[int, str] = field(default_factory=dict)
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

    def disable(self, index: int, reason: str) -> None:
        with self.lock:
            self.disabled.add(index)
            self.disable_reasons[index] = reason

    def disabled_reason(self, index: int) -> str:
        with self.lock:
            return self.disable_reasons.get(index, "")

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


def _tavily_key_lock(keys: tuple[str, ...], index: int) -> asyncio.Lock:
    """Return the loop-local mutex for one credential in a shared key pool.

    Provider instances share the same locks, so one key can never serve two
    in-flight requests in the evaluation event loop.  Locks are loop-local to
    avoid binding asyncio primitives to a different loop in tests or workers.
    """
    loop = asyncio.get_running_loop()
    locks_by_pool = _LOOP_KEY_LOCKS.get(loop)
    if locks_by_pool is None:
        locks_by_pool = {}
        _LOOP_KEY_LOCKS[loop] = locks_by_pool
    locks = locks_by_pool.get(keys)
    if locks is None:
        locks = tuple(asyncio.Lock() for _ in keys)
        locks_by_pool[keys] = locks
    return locks[index]


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
            # Keep retries for a logical request under the same per-key lock.
            # Waiting for the key happens before taking the global slot, so a
            # busy credential cannot starve requests assigned to other keys.
            async with _tavily_key_lock(self._pool.keys, key_index):
                # Another request may have disabled this key while we waited.
                disabled_reason = self._pool.disabled_reason(key_index)
                if disabled_reason:
                    rotate = True
                    saw_auth = saw_auth or disabled_reason == "auth"
                    saw_quota = saw_quota or disabled_reason == "quota"
                attempts = range(0) if rotate else range(MAX_RETRIES + 1)
                for attempt in attempts:
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
                        error_type = type(e).__name__.lower()
                        if any(
                            marker in msg
                            for marker in (
                                "usage limit",
                                "quota",
                                "credit limit",
                                "credits exhausted",
                            )
                        ):
                            saw_quota = True
                            self._pool.disable(key_index, "quota")
                            rotate = True
                            break
                        if (
                            "invalidapikey" in error_type
                            or "authentication" in error_type
                            or any(
                                marker in msg
                                for marker in (
                                    "401",
                                    "403",
                                    "unauthorized",
                                    "forbidden",
                                    "invalid api key",
                                    "invalid_api_key",
                                )
                            )
                        ):
                            saw_auth = True
                            self._pool.disable(key_index, "auth")
                            rotate = True
                            break
                        retryable = any(
                            marker in msg
                            for marker in ("429", "502", "503", "504", "timeout", "rate")
                        ) or any(
                            marker in error_type
                            for marker in (
                                "timeout",
                                "connectionerror",
                                "sslerror",
                                "proxyerror",
                            )
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


class TavilyProxySearchProvider:
    """Tavily-compatible HTTP client backed by the local key-pool proxy.

    Key rotation, per-key concurrency and upstream retrying belong to the
    proxy. This adapter deliberately does not keep a second credential pool;
    it translates the proxy's structured failures into the project's stable
    tool-error taxonomy without exposing credentials.
    """

    name = "tavily"

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = SEARCH_TIMEOUT_S,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        normalized = base_url.strip().rstrip("/")
        if not normalized.startswith(("http://", "https://")):
            raise ProviderNotConfiguredError(
                "TAVILY_PROXY_URL must be an http(s) URL."
            )
        self._base_url = normalized
        self._timeout = timeout
        self._transport = transport

    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout,
                transport=self._transport,
            ) as client:
                response = await client.post(
                    "/search",
                    headers={"Authorization": "Bearer local-tavily-proxy-client"},
                    json={
                        "query": query,
                        "max_results": max_results,
                        "search_depth": "basic",
                        "include_answer": False,
                    },
                )
                if response.is_success:
                    return _search_results(response.json())
                await self._raise_proxy_error(client, response)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise TransientToolError(
                "tavily proxy is temporarily unreachable"
            ) from exc
        except ValueError as exc:
            raise ToolError("tavily proxy returned invalid JSON") from exc
        raise ToolError("tavily proxy search failed")  # pragma: no cover

    async def _raise_proxy_error(
        self, client: httpx.AsyncClient, response: httpx.Response
    ) -> None:
        code, message = _proxy_error_details(response)
        if code == "all_tavily_accounts_cooling_down":
            retry_after = response.headers.get("retry-after", "unknown")
            raise TransientToolError(
                f"tavily proxy reports all keys cooling down; retry_after={retry_after}s"
            )
        if code == "all_tavily_accounts_busy":
            retry_after = response.headers.get("retry-after", "unknown")
            raise TransientToolError(
                f"tavily proxy reports all keys busy; retry_after={retry_after}s"
            )
        if code == "tavily_upstream_pool_busy":
            raise TransientToolError("tavily proxy upstream connection pool is busy")
        if code == "tavily_upstream_unavailable" or (
            response.status_code >= 500 and code != "all_tavily_accounts_exhausted"
        ):
            raise TransientToolError(
                f"tavily proxy upstream unavailable; code={code or response.status_code}"
            )
        if code == "all_tavily_accounts_exhausted":
            await _raise_exhausted_proxy_pool(client, code)
        if response.status_code in {401, 403}:
            raise ToolAuthenticationError(
                f"tavily proxy rejected the request; code={code or response.status_code}"
            )
        if response.status_code == 429:
            raise TransientToolError(
                f"tavily proxy rate limited the request; code={code or response.status_code}"
            )
        safe_message = message[:160] if message else "request failed"
        raise ToolError(
            f"tavily proxy request failed; status={response.status_code}; "
            f"code={code or 'unknown'}; message={safe_message}"
        )


def _proxy_error_details(response: httpx.Response) -> tuple[str, str]:
    try:
        payload = response.json()
    except ValueError:
        return "", ""
    error = payload.get("error", {}) if isinstance(payload, dict) else {}
    if not isinstance(error, dict):
        return "", ""
    return str(error.get("code", "")), str(error.get("message", ""))


async def _raise_exhausted_proxy_pool(client: httpx.AsyncClient, code: str) -> None:
    """Resolve the generic proxy exhaustion response to an actionable cause."""
    try:
        response = await client.get("/accounts")
        response.raise_for_status()
        payload = response.json()
        accounts = payload.get("accounts", []) if isinstance(payload, dict) else []
    except (httpx.HTTPError, ValueError):
        raise ToolError(
            f"tavily proxy has no available upstream key; code={code}; diagnostics=unavailable"
        ) from None

    errors = " ".join(
        str(account.get("last_error", "")).lower()
        for account in accounts
        if isinstance(account, dict)
    )
    has_auth = any(
        marker in errors
        for marker in ("401", "403", "unauthorized", "forbidden", "deactivated", "invalid")
    )
    has_quota = any(
        marker in errors
        for marker in ("432", "433", "quota", "usage limit", "credit", "exhausted")
    )
    if has_auth and not has_quota:
        raise ToolAuthenticationError(
            f"tavily proxy rejected all upstream credentials; code={code}"
        )
    if has_quota and not has_auth:
        raise ToolQuotaExceededError(
            f"tavily proxy exhausted all upstream quotas; code={code}"
        )
    if has_auth and has_quota:
        raise ToolError(
            f"tavily proxy pool exhausted by mixed authentication and quota failures; code={code}"
        )
    raise ToolError(
        f"tavily proxy has no available upstream key; code={code}; inspect=/accounts"
    )


def _search_results(data: dict[str, Any]) -> list[SearchResult]:
    out: list[SearchResult] = []
    for result in data.get("results", []):
        content = (result.get("content") or "")[:MAX_RESULT_CHARS]
        out.append(
            SearchResult(
                title=(result.get("title") or "").strip(),
                url=result.get("url", ""),
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
    if settings.llm_provider == "fake" or not settings.has_tavily_search:
        return FakeSearchProvider()
    if settings.tavily_proxy_url.strip():
        return TavilyProxySearchProvider(settings.tavily_proxy_url)
    return TavilySearchProvider(api_key=settings.tavily_key_pool())


__all__ = [
    "FakeSearchProvider",
    "TavilyProxySearchProvider",
    "TavilySearchProvider",
    "build_search_provider",
]
