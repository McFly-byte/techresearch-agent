"""Phase 4 correction tests: re-verify after rewrite, long-term store,
embedding/reranker protocols, Feishu mock transport."""

from __future__ import annotations

import httpx
import pytest

from domain.models import Citation, Fact
from service.exporters import FeishuExporter
from service.long_term_store import LongTermStore
from service.retrieval import (
    Chunk,
    FakeEncoder,
    FakeReranker,
    hybrid_retrieve,
)
from service.verified_report import VerifiedReportBuilder
from service.verifier import CitationVerifier


# --- 1. Re-verify after rewrite ---------------------------------------------
def _cite(cid: str, url: str = "https://x") -> Citation:
    return Citation(citation_id=cid, kind="web", title="t", locator=url, fetched_at="now")


@pytest.mark.asyncio
async def test_revised_claim_gets_reverified() -> None:
    """A contradiction claim is revised (dropped), then re-verified.
    The re-verify runs EXACTLY ONCE (2 calls total per non-verified claim).
    No infinite loop."""

    class _RevisableVerifier(CitationVerifier):
        """First call says contradiction; second call (after revision) says entailment."""

        def __init__(self) -> None:
            super().__init__(fetcher=None)
            self.calls = 0

        async def verify(self, claim, citations, **kw):  # type: ignore[no-untyped-def]
            self.calls += 1
            from datetime import UTC, datetime

            from domain.verification import VerificationResult

            # First call says neutral (revised + re-verified); second call
            # (after revision) says entailment. Contradiction claims are
            # dropped without re-verify (stage4 hardening), so use neutral
            # to exercise the revise+reverify path.
            verdict = "entailment" if self.calls == 2 else "neutral"
            return VerificationResult(
                claim_id=claim.claim_id,
                verdict=verdict,
                evidence=[],
                reason="test",
                verifier_version="test-v1",
                verified_at=datetime.now(UTC).isoformat(),
                round=kw.get("round_label", "initial"),
            )

    verifier = _RevisableVerifier()
    builder = VerifiedReportBuilder(verifier=verifier)
    facts = [Fact(fact_id="f1", claim="The system does NOT support X", source_citation_ids=["c1"])]
    citations = [_cite("c1")]
    await builder.build(query="q", facts=facts, citations=citations)
    # The claim was verified twice (initial + re-verify).
    assert verifier.calls == 2, f"expected 2 verifier calls, got {verifier.calls}"


# --- 2. Long-term store -----------------------------------------------------
def test_long_term_store_namespace_isolation() -> None:
    from service.long_term_store import MemoryRecord

    s = LongTermStore()
    s.put_record(
        namespace="user/a",
        record=MemoryRecord(
            type="preference", key="user_theme", value={"lang": "zh"}, consent=True
        ),
    )
    s.put_record(
        namespace="user/b",
        record=MemoryRecord(
            type="preference", key="user_theme", value={"lang": "en"}, consent=True
        ),
    )
    assert s.get(namespace="user/a", key="user_theme") == {"lang": "zh"}
    assert s.get(namespace="user/b", key="user_theme") == {"lang": "en"}
    assert s.get(namespace="user/a", key="missing") is None


def test_long_term_store_delete_and_keys() -> None:
    from service.long_term_store import MemoryRecord

    s = LongTermStore()
    s.put_record(
        namespace="user/test",
        record=MemoryRecord(type="preference", key="user_theme", value={"v": 1}, consent=True),
    )
    s.put_record(
        namespace="user/test",
        record=MemoryRecord(type="preference", key="user_scope", value={"v": 2}, consent=True),
    )
    assert set(s.keys(namespace="user/test")) == {"user_theme", "user_scope"}
    s.delete(namespace="user/test", key="user_theme")
    assert s.get(namespace="user/test", key="user_theme") is None
    assert s.keys(namespace="user/test") == ["user_scope"]


# --- 3. Embedding + Reranker -------------------------------------------------
def test_embedding_protocol_and_fake() -> None:
    enc = FakeEncoder()
    assert isinstance(enc, enc.__class__)  # smoke
    vecs = enc.encode(["hello world", "hello foo"])
    assert len(vecs) == 2
    assert len(vecs[0]) == len(vecs[1])


def test_reranker_optional_does_not_break_hybrid_retrieve() -> None:
    chunks = [
        Chunk(
            chunk_id="c1", source_id="s1", position=0, text="alpha beta gamma", content_hash="h1"
        ),
        Chunk(
            chunk_id="c2", source_id="s2", position=0, text="delta epsilon zeta", content_hash="h2"
        ),
    ]
    # No reranker configured.
    out = hybrid_retrieve("alpha", chunks, top_k=2)
    assert len(out) >= 1
    # With identity reranker.
    out2 = hybrid_retrieve("alpha", chunks, top_k=2, reranker=FakeReranker())
    assert len(out2) == len(out)


def test_hybrid_retrieve_dedup() -> None:
    chunks = [
        Chunk(chunk_id="c1", source_id="s1", position=0, text="alpha", content_hash="h1"),
        Chunk(chunk_id="c2", source_id="s2", position=0, text="alpha beta", content_hash="h2"),
    ]
    out = hybrid_retrieve("alpha", chunks, top_k=5)
    ids = [c.chunk_id for c in out]
    assert len(ids) == len(set(ids))


# --- 4. Feishu adapter ------------------------------------------------------
@pytest.mark.asyncio
async def test_feishu_not_configured_does_not_block() -> None:
    exporter = FeishuExporter(app_id="", app_secret="")
    result = await exporter.export_markdown(title="t", markdown="md")
    assert result.exported is False
    assert result.reason == "not_configured"


@pytest.mark.asyncio
async def test_feishu_configured_sends_correct_request() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["path"] = request.url.path
        import json as _j

        body = _j.loads(request.content)
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "t-abc"})
        if request.url.path.endswith("/documents") and "blocks" not in request.url.path:
            return httpx.Response(
                200,
                json={"code": 0, "data": {"document": {"document_id": "doc_123"}}},
            )
        # blocks append: real Docx children structure (not {"markdown": ...}).
        seen["children_sent"] = body.get("children", [])
        return httpx.Response(200, json={"code": 0})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    exporter = FeishuExporter(
        app_id="cli_x",
        app_secret="sec_x",
        folder_token="fld",
        client=client,
    )
    result = await exporter.export_markdown(title="Hello", markdown="# body")
    assert result.exported is True
    assert "doc_123" in result.doc_url
    # The children blocks must be real Docx block objects (block_type=2 text).
    children = seen["children_sent"]
    assert isinstance(children, list) and len(children) >= 1
    assert children[0]["block_type"] == 2
    await client.aclose()


@pytest.mark.asyncio
async def test_feishu_failure_does_not_raise() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    exporter = FeishuExporter(app_id="x", app_secret="y", client=client)
    result = await exporter.export_markdown(title="t", markdown="m")
    assert result.exported is False
    assert "failed" in result.reason or "error" in result.reason
    await client.aclose()
