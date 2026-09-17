"""Page / document fetchers.

- HttpPageFetcher: downloads with httpx (timeout, size cap) and extracts main
  text via trafilatura.
- LocalMarkdownFetcher: reads a local .md file.
- LocalPdfFetcher: reads a local .pdf file with pypdf (text-only, no OCR).
- FakeFetcher: canned responses for tests.

All fetchers return a SourceDocument with `fetched_ok=False` and a populated
`error` field on failure — they do NOT raise for expected parse failures, so
the orchestrator can record them in `errors[]` and continue. They DO raise
ToolError for programmer-error style issues (bad URL scheme, path traversal
outside an allowed root).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from core.exceptions import ToolError
from domain.models import Citation, SourceDocument, SourceKind

from .netutil import is_http_url, truncate

log = logging.getLogger(__name__)

FETCH_TIMEOUT_S = 15
MAX_BODY_BYTES = 2_000_000  # 2 MB cap on raw download
MAX_BODY_CHARS = 20_000


class HttpPageFetcher:
    name = "http"

    def __init__(self, *, timeout: float = FETCH_TIMEOUT_S) -> None:
        self._timeout = timeout

    async def fetch(self, locator: str, *, citation_id: str) -> SourceDocument:
        if not is_http_url(locator):
            raise ToolError(f"http fetcher got non-http locator: {locator!r}")

        try:
            import httpx  # local import so tests can use FakeFetcher without network deps
        except ImportError as e:  # pragma: no cover
            raise ToolError("httpx is not installed.") from e

        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                follow_redirects=True,
                headers={"User-Agent": "TechResearchAgent/0.1 (+research)"},
            ) as client:
                resp = await client.get(locator)
                resp.raise_for_status()
                raw = resp.text
        except Exception as e:
            return SourceDocument(
                citation=Citation(
                    citation_id=citation_id,
                    kind=SourceKind.WEB,
                    locator=locator,
                ),
                error=f"fetch_failed: {type(e).__name__}: {e}",
                fetched_ok=False,
            )

        if len(raw) > MAX_BODY_BYTES:
            raw = raw[:MAX_BODY_BYTES]
            log.warning("truncated %s to %d bytes", locator, MAX_BODY_BYTES)

        text = await asyncio.to_thread(_trafilatura_extract, raw, locator)
        if not text:
            return SourceDocument(
                citation=Citation(citation_id=citation_id, kind=SourceKind.WEB, locator=locator),
                error="parse_failed: trafilatura returned no main text",
                fetched_ok=False,
            )
        text = truncate(text, MAX_BODY_CHARS)
        return SourceDocument(
            citation=Citation(
                citation_id=citation_id,
                kind=SourceKind.WEB,
                locator=locator,
                snippet=text[:600],
            ),
            content=text,
        )


def _trafilatura_extract(raw: str, url: str) -> str:
    try:
        import trafilatura
    except ImportError:  # pragma: no cover
        return ""
    downloaded = raw
    extracted = trafilatura.extract(
        downloaded,
        url=url,
        no_fallback=False,
        favor_recall=True,
    )
    return extracted or ""


class LocalMarkdownFetcher:
    """Read a local .md file. Paths must stay under an allowed root."""

    name = "local_md"

    def __init__(self, *, root: Path | None = None) -> None:
        self._root = (root or Path.cwd()).resolve()

    async def fetch(self, locator: str, *, citation_id: str) -> SourceDocument:
        path = (
            (self._root / locator).resolve() if not Path(locator).is_absolute() else Path(locator)
        )
        try:
            path.relative_to(self._root)
        except ValueError:
            raise ToolError(f"path escapes root: {locator!r}") from None
        if not path.is_file():
            return SourceDocument(
                citation=Citation(
                    citation_id=citation_id, kind=SourceKind.LOCAL_MD, locator=str(path)
                ),
                error=f"file_not_found: {path}",
                fetched_ok=False,
            )
        try:
            text = await asyncio.to_thread(path.read_text, encoding="utf-8")
        except Exception as e:
            return SourceDocument(
                citation=Citation(
                    citation_id=citation_id, kind=SourceKind.LOCAL_MD, locator=str(path)
                ),
                error=f"read_failed: {e}",
                fetched_ok=False,
            )
        text = truncate(text, MAX_BODY_CHARS)
        return SourceDocument(
            citation=Citation(
                citation_id=citation_id,
                kind=SourceKind.LOCAL_MD,
                locator=str(path),
                title=path.name,
                snippet=text[:600],
            ),
            content=text,
        )


class LocalPdfFetcher:
    name = "local_pdf"

    def __init__(self, *, root: Path | None = None) -> None:
        self._root = (root or Path.cwd()).resolve()

    async def fetch(self, locator: str, *, citation_id: str) -> SourceDocument:
        path = (
            (self._root / locator).resolve() if not Path(locator).is_absolute() else Path(locator)
        )
        try:
            path.relative_to(self._root)
        except ValueError:
            raise ToolError(f"path escapes root: {locator!r}") from None
        if not path.is_file():
            return SourceDocument(
                citation=Citation(
                    citation_id=citation_id, kind=SourceKind.LOCAL_PDF, locator=str(path)
                ),
                error=f"file_not_found: {path}",
                fetched_ok=False,
            )
        try:
            text = await asyncio.to_thread(_read_pdf_text, str(path))
        except Exception as e:
            return SourceDocument(
                citation=Citation(
                    citation_id=citation_id, kind=SourceKind.LOCAL_PDF, locator=str(path)
                ),
                error=f"pdf_parse_failed: {e}",
                fetched_ok=False,
            )
        if not text.strip():
            return SourceDocument(
                citation=Citation(
                    citation_id=citation_id, kind=SourceKind.LOCAL_PDF, locator=str(path)
                ),
                error="pdf_no_text: scanned/image-only PDF, OCR not in phase 1",
                fetched_ok=False,
            )
        text = truncate(text, MAX_BODY_CHARS)
        return SourceDocument(
            citation=Citation(
                citation_id=citation_id,
                kind=SourceKind.LOCAL_PDF,
                locator=str(path),
                title=path.name,
                snippet=text[:600],
            ),
            content=text,
        )


def _read_pdf_text(path: str) -> str:
    from pypdf import PdfReader

    reader = PdfReader(path)
    chunks: list[str] = []
    for page in reader.pages:
        chunks.append(page.extract_text() or "")
    return "\n".join(chunks)


class FakeFetcher:
    """Canned fetcher for tests: maps locator -> (content, error)."""

    name = "fake"

    def __init__(
        self, mapping: dict[str, str] | None = None, errors: dict[str, str] | None = None
    ) -> None:
        self._mapping = mapping or {}
        self._errors = errors or {}
        self.calls: list[str] = []

    async def fetch(self, locator: str, *, citation_id: str) -> SourceDocument:
        self.calls.append(locator)
        if locator in self._errors:
            return SourceDocument(
                citation=Citation(citation_id=citation_id, kind=SourceKind.WEB, locator=locator),
                error=self._errors[locator],
                fetched_ok=False,
            )
        content = self._mapping.get(locator, "")
        return SourceDocument(
            citation=Citation(
                citation_id=citation_id,
                kind=SourceKind.WEB,
                locator=locator,
                title=locator,
                snippet=content[:600],
            ),
            content=content,
        )


__all__ = [
    "FakeFetcher",
    "HttpPageFetcher",
    "LocalMarkdownFetcher",
    "LocalPdfFetcher",
]
