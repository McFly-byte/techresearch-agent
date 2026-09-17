"""Paper search providers: arXiv (live) and Fake (offline)."""

from __future__ import annotations

import asyncio
import logging

from core.exceptions import ToolError, ToolTimeoutError, TransientToolError
from domain.models import SearchResult

log = logging.getLogger(__name__)

ARXIV_TIMEOUT_S = 15
MAX_RETRIES = 1


class ArxivSearchProvider:
    """Async wrapper around the `arxiv` package with timeout + one retry."""

    name = "arxiv"

    def __init__(self, *, timeout: float = ARXIV_TIMEOUT_S) -> None:
        self._timeout = timeout

    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        try:
            import arxiv
        except ImportError as e:  # pragma: no cover
            raise ToolError("arxiv package is not installed.") from e

        def _call() -> list[SearchResult]:
            client = arxiv.Search(query=query, max_results=max_results)
            out: list[SearchResult] = []
            for r in client.results():
                out.append(
                    SearchResult(
                        title=(r.title or "").replace("\n", " ").strip(),
                        url=r.entry_id,
                        snippet=(r.summary or "")[:2000].strip(),
                        source="arxiv",
                        authors=[str(a) for a in getattr(r, "authors", [])][:6],
                        published=str(getattr(r, "published", "") or "")[:10],
                    )
                )
            return out

        last_exc: Exception | None = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                return await asyncio.wait_for(asyncio.to_thread(_call), timeout=self._timeout)
            except TimeoutError as e:
                last_exc = e
                if attempt >= MAX_RETRIES:
                    raise ToolTimeoutError(f"arxiv search timed out after {self._timeout}s") from e
            except Exception as e:
                last_exc = e
                msg = str(e).lower()
                retryable = any(t in msg for t in ("503", "502", "timeout", "reset", "temporarily"))
                if not retryable or attempt >= MAX_RETRIES:
                    raise ToolError(f"arxiv search failed: {e}") from e
                await asyncio.sleep(1.0 * (attempt + 1))
        raise TransientToolError(f"arxiv exhausted retries: {last_exc}")


class FakePaperSearchProvider:
    name = "fake_arxiv"

    def __init__(self, results: list[SearchResult] | None = None) -> None:
        self._results = results or []
        self.calls: list[str] = []

    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        self.calls.append(query)
        return self._results[:max_results]


__all__ = ["ArxivSearchProvider", "FakePaperSearchProvider"]
