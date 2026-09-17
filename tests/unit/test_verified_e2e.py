"""Phase 4 E2E: a fake report with verified / contradicted / neutral claims."""

from __future__ import annotations

import pytest

from domain.models import Citation, Fact, SourceDocument, SourceKind
from service.verified_report import VerifiedReportBuilder
from service.verifier import CitationVerifier


class _Fetcher:
    """Returns content tailored to each URL so NLI lands on the desired verdict."""

    async def fetch(self, locator: str, *, citation_id: str) -> SourceDocument:
        c = Citation(
            citation_id=citation_id, kind=SourceKind.WEB, locator=locator, title=citation_id
        )
        table = {
            "https://example.com/good": "LangGraph supports typed state and checkpoints natively.",
            "https://example.com/bad": "LangGraph does not support checkpoints; it is stateless.",
            "https://example.com/vague": "Some framework has state features. Details unclear.",
        }
        text = table.get(locator)
        if text is None:
            return SourceDocument(citation=c, content="", error="missing", fetched_ok=False)
        return SourceDocument(citation=c, content=text, fetched_ok=True)


@pytest.mark.asyncio
async def test_e2e_three_verdict_states(tmp_path):  # type: ignore[no-untyped-def]
    fetcher = _Fetcher()

    # Deterministic NLI: verdict depends on which URL we're verifying against.
    # c1 (good) -> entailment, c2 (bad) -> contradiction, c3 (vague) -> neutral.
    def nli(claim: str, excerpt: str):  # type: ignore[no-untyped-def]
        if "does not support" in excerpt:
            return "contradiction"
        if "Details unclear" in excerpt:
            return "neutral"
        return "entailment"

    verifier = CitationVerifier(fetcher=fetcher, nli=nli)
    builder = VerifiedReportBuilder(verifier=verifier)

    citations = [
        Citation(
            citation_id="c1", kind=SourceKind.WEB, locator="https://example.com/good", title="good"
        ),
        Citation(
            citation_id="c2", kind=SourceKind.WEB, locator="https://example.com/bad", title="bad"
        ),
        Citation(
            citation_id="c3",
            kind=SourceKind.WEB,
            locator="https://example.com/vague",
            title="vague",
        ),
    ]
    facts = [
        Fact(
            fact_id="f1",
            claim="LangGraph supports typed state and checkpoints.",
            source_citation_ids=["c1"],
        ),
        Fact(fact_id="f2", claim="LangGraph supports checkpoints.", source_citation_ids=["c2"]),
        Fact(fact_id="f3", claim="LangGraph has state features.", source_citation_ids=["c3"]),
    ]

    report = await builder.build(
        query="LangGraph state model",
        facts=facts,
        citations=citations,
    )

    # One claim verified (c1), one contradicted→dropped (c2), one neutral (c3).
    statuses = {c.citation_ids[0]: c.verification_status for c in report.claims}
    assert statuses["c1"] == "verified"
    assert statuses["c2"] == "dropped"
    assert statuses["c3"] == "neutral"

    # Metrics: precision 1/3, coverage 2/3.
    assert report.metrics.verified == 1
    assert report.metrics.dropped == 1
    assert report.metrics.neutral == 1
    assert abs(report.metrics.citation_precision - 1 / 3) < 0.01

    # Markdown contains the three sections.
    assert "已验证声明" in report.markdown
    assert "存疑 / 矛盾声明" in report.markdown

    # Save the fixture.
    out = tmp_path / "phase4_e2e.md"
    out.write_text(report.markdown, encoding="utf-8")
    html_out = tmp_path / "phase4_e2e.html"
    html_out.write_text(report.html, encoding="utf-8")
    assert out.exists()
    assert html_out.exists()
