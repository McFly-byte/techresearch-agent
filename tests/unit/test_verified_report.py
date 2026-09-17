"""Phase 4 tests: claim model, verifier, metrics, compression, retrieval, HTML escape."""

from __future__ import annotations

import pytest
from pydantic import ValidationError as PydanticValidationError

from core.exceptions import ToolError
from domain.models import Citation, Fact, SourceDocument, SourceKind
from domain.verification import Claim, VerificationMetrics
from service.compression import compress_facts, estimate_tokens
from service.exporters import FeishuExporter
from service.retrieval import Chunk, chunk_document, hybrid_retrieve
from service.verified_report import VerifiedReportBuilder, facts_to_claims
from service.verifier import CitationVerifier, _heuristic_nli


# --- fixtures --------------------------------------------------------------
def _cite(cid: str, url: str = "https://x") -> Citation:
    return Citation(citation_id=cid, kind=SourceKind.WEB, locator=url, title=cid)


def _fact(fid: str, claim: str, cids: list[str]) -> Fact:
    return Fact(fact_id=fid, claim=claim, source_citation_ids=cids)


class _FakeFetcher:
    """Returns DIFFERENT text per call to prove verifier re-fetches."""

    def __init__(self, mapping: dict[str, str]) -> None:
        self._m = mapping
        self.calls: list[str] = []

    async def fetch(self, locator: str, *, citation_id: str) -> SourceDocument:
        self.calls.append(locator)
        text = self._m.get(locator, "")
        if not text:
            return SourceDocument(
                citation=_cite(citation_id, locator),
                content="",
                error="not found",
                fetched_ok=False,
            )
        return SourceDocument(
            citation=_cite(citation_id, locator),
            content=text,
            fetched_ok=True,
        )


# --- claim model -----------------------------------------------------------
def test_claim_requires_citation_id():  # type: ignore[no-untyped-def]
    with pytest.raises(PydanticValidationError):
        Claim(claim_id="c1", claim_text="x", citation_ids=[])  # type: ignore[arg-type]


def test_facts_to_claims_preserves_citations():  # type: ignore[no-untyped-def]
    facts = [_fact("f1", "hello world", ["c1", "c2"])]
    claims = facts_to_claims(facts)
    assert claims[0].citation_ids == ["c1", "c2"]
    assert claims[0].verification_status == "pending"


# --- verifier --------------------------------------------------------------
@pytest.mark.asyncio
async def test_verifier_entailment():  # type: ignore[no-untyped-def]
    fetcher = _FakeFetcher({"https://x": "LangGraph is a graph with typed state."})
    v = CitationVerifier(fetcher=fetcher)
    claim = Claim(
        claim_id="c1", claim_text="LangGraph is a graph with typed state.", citation_ids=["c1"]
    )
    r = await v.verify(claim, [_cite("c1", "https://x")])
    assert r.verdict == "entailment"
    assert r.evidence[0].refetch_ok is True
    # Prove we actually re-fetched.
    assert fetcher.calls == ["https://x"]


@pytest.mark.asyncio
async def test_verifier_contradiction():  # type: ignore[no-untyped-def]
    fetcher = _FakeFetcher({"https://x": "LangGraph does not support checkpoints."})
    v = CitationVerifier(fetcher=fetcher, nli=lambda c, e: "contradiction")
    claim = Claim(claim_id="c1", claim_text="LangGraph supports checkpoints.", citation_ids=["c1"])
    r = await v.verify(claim, [_cite("c1", "https://x")])
    assert r.verdict == "contradiction"


@pytest.mark.asyncio
async def test_verifier_neutral():  # type: ignore[no-untyped-def]
    fetcher = _FakeFetcher({"https://x": "unrelated text here."})
    v = CitationVerifier(fetcher=fetcher)
    claim = Claim(claim_id="c1", claim_text="LangGraph uses typed state.", citation_ids=["c1"])
    r = await v.verify(claim, [_cite("c1", "https://x")])
    assert r.verdict == "neutral"


@pytest.mark.asyncio
async def test_verifier_refetch_failed_is_not_neutral():  # type: ignore[no-untyped-def]
    fetcher = _FakeFetcher({})  # empty mapping -> not found
    v = CitationVerifier(fetcher=fetcher)
    claim = Claim(claim_id="c1", claim_text="anything", citation_ids=["c1"])
    r = await v.verify(claim, [_cite("c1", "https://missing")])
    assert r.verdict is None
    assert r.evidence[0].refetch_ok is False


@pytest.mark.asyncio
async def test_verifier_unknown_citation_id_is_refetch_failed():  # type: ignore[no-untyped-def]
    fetcher = _FakeFetcher({})
    v = CitationVerifier(fetcher=fetcher)
    claim = Claim(claim_id="c1", claim_text="x", citation_ids=["c_does_not_exist"])
    r = await v.verify(claim, [_cite("c1")])
    assert r.verdict is None


def test_heuristic_nli_does_not_claim_absolute():  # type: ignore[no-untyped-def]
    # The heuristic is a weak signal; just verify it returns one of 3.
    assert _heuristic_nli("hello world", "hello world") in {
        "entailment",
        "neutral",
        "contradiction",
    }


# --- metrics zero denominator ----------------------------------------------
def test_metrics_zero_denominator():  # type: ignore[no-untyped-def]
    m = VerificationMetrics()
    assert m.citation_precision == 0.0
    assert m.claim_coverage == 0.0
    assert m.contradiction_rate == 0.0


def test_metrics_ratios():  # type: ignore[no-untyped-def]
    m = VerificationMetrics(total_claims=4, verified=2, contradicted=1, neutral=1)
    assert m.citation_precision == 0.5
    assert m.claim_coverage == 0.75  # (2+1)/4
    assert m.contradiction_rate == 0.25


# --- compression preserves provenance ---------------------------------------
def test_compression_preserves_ids():  # type: ignore[no-untyped-def]
    facts = [_fact(f"f{i}", "word " * 50, ["c1"]) for i in range(10)]
    res = compress_facts(facts, max_estimated_tokens=10)
    assert res.truncated is True
    # Kept facts still have citation_ids.
    for cf in res.facts:
        assert cf.citation_ids == ["c1"]
        assert cf.fact_id.startswith("f")


def test_estimate_tokens_is_estimated():  # type: ignore[no-untyped-def]
    # Just a smoke test: non-zero and scales.
    assert estimate_tokens("hello world") > 0
    assert estimate_tokens("a" * 100) > estimate_tokens("a" * 10)


# --- retrieval -------------------------------------------------------------
def test_chunk_document_keeps_provenance():  # type: ignore[no-untyped-def]
    doc = SourceDocument(
        citation=_cite("c1", "https://x"),
        content="word " * 200,
    )
    chunks = chunk_document(doc, chunk_chars=100)
    assert len(chunks) > 1
    assert all(c.source_id == "c1" for c in chunks)
    assert all(c.locator == "https://x" for c in chunks)
    assert all(c.content_hash for c in chunks)


def test_hybrid_retrieve_dedup_and_topk():  # type: ignore[no-untyped-def]
    chunks = [
        Chunk(
            chunk_id="c1_0",
            source_id="c1",
            position=0,
            text="langgraph state graph",
            content_hash="a",
        ),
        Chunk(
            chunk_id="c1_1",
            source_id="c1",
            position=1,
            text="llamaindex retrieval",
            content_hash="b",
        ),
        Chunk(
            chunk_id="c2_0",
            source_id="c2",
            position=0,
            text="langgraph checkpoints",
            content_hash="c",
        ),
    ]
    out = hybrid_retrieve("langgraph", chunks, top_k=2)
    assert len(out) == 2
    # Both top hits must be the langgraph chunks (not llamaindex).
    assert {c.source_id for c in out} == {"c1", "c2"}
    # No duplicates.
    assert len({c.chunk_id for c in out}) == len(out)


# --- HTML escape -----------------------------------------------------------
@pytest.mark.asyncio
async def test_html_escapes_unsafe_content():  # type: ignore[no-untyped-def]
    fetcher = _FakeFetcher({"https://x": "safe text about LangGraph."})
    builder = VerifiedReportBuilder(verifier=CitationVerifier(fetcher=fetcher))
    facts = [_fact("f1", "<script>alert(1)</script> is a claim", ["c1"])]
    out = await builder.build(
        query="<img src=x onerror=alert(1)>",
        facts=facts,
        citations=[_cite("c1", "https://x")],
    )
    assert "<script>alert(1)</script>" not in out.html
    assert "&lt;script&gt;" in out.html
    assert "onerror" in out.html  # escaped


@pytest.mark.asyncio
async def test_writer_rejects_unknown_citation():  # type: ignore[no-untyped-def]
    builder = VerifiedReportBuilder(verifier=CitationVerifier(fetcher=_FakeFetcher({})))
    facts = [_fact("f1", "claim", ["c_unknown"])]
    with pytest.raises(ToolError):
        await builder.build(query="q", facts=facts, citations=[_cite("c1")])


# --- Feishu unconfigured ---------------------------------------------------
@pytest.mark.asyncio
async def test_feishu_unconfigured_does_not_raise():  # type: ignore[no-untyped-def]
    ex = FeishuExporter()
    assert ex.configured is False
    r = await ex.export_markdown(title="t", markdown="x")
    assert r.exported is False
    assert r.reason == "not_configured"
