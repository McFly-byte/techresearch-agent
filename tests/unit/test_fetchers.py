"""Tests for fetchers: fake, local markdown, local PDF error paths, URL guards."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.exceptions import ToolError
from tools.fetchers import (
    FakeFetcher,
    LocalMarkdownFetcher,
    LocalPdfFetcher,
)


@pytest.mark.asyncio
async def test_fake_fetcher_happy_and_error():  # type: ignore[no-untyped-def]
    f = FakeFetcher(mapping={"https://x": "hello world"}, errors={"https://y": "boom"})
    ok = await f.fetch("https://x", citation_id="c1")
    assert ok.fetched_ok and ok.content == "hello world"
    bad = await f.fetch("https://y", citation_id="c2")
    assert not bad.fetched_ok and bad.error == "boom"


@pytest.mark.asyncio
async def test_local_markdown_fetcher(tmp_path: Path):  # type: ignore[no-untyped-def]
    (tmp_path / "note.md").write_text("# Title\nSome real content here.\n", encoding="utf-8")
    fetcher = LocalMarkdownFetcher(root=tmp_path)
    doc = await fetcher.fetch("note.md", citation_id="c1")
    assert doc.fetched_ok
    assert "Some real content" in doc.content
    assert doc.citation.title == "note.md"


@pytest.mark.asyncio
async def test_local_markdown_missing_file(tmp_path: Path):  # type: ignore[no-untyped-def]
    fetcher = LocalMarkdownFetcher(root=tmp_path)
    doc = await fetcher.fetch("nope.md", citation_id="c1")
    assert not doc.fetched_ok
    assert "file_not_found" in doc.error


@pytest.mark.asyncio
async def test_local_markdown_rejects_path_escape(tmp_path: Path):  # type: ignore[no-untyped-def]
    fetcher = LocalMarkdownFetcher(root=tmp_path / "work")
    (tmp_path / "work").mkdir()
    with pytest.raises(ToolError):
        await fetcher.fetch("../secret.md", citation_id="c1")


@pytest.mark.asyncio
async def test_local_pdf_missing_file(tmp_path: Path):  # type: ignore[no-untyped-def]
    fetcher = LocalPdfFetcher(root=tmp_path)
    doc = await fetcher.fetch("missing.pdf", citation_id="c1")
    assert not doc.fetched_ok
    assert "file_not_found" in doc.error


@pytest.mark.asyncio
async def test_local_pdf_non_pdf_returns_parse_error(tmp_path: Path):  # type: ignore[no-untyped-def]
    fake = tmp_path / "fake.pdf"
    fake.write_text("this is not a pdf", encoding="utf-8")
    fetcher = LocalPdfFetcher(root=tmp_path)
    doc = await fetcher.fetch("fake.pdf", citation_id="c1")
    assert not doc.fetched_ok
    assert "pdf" in doc.error.lower()
