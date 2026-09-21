"""Report synthesis + quality gate tests (offline, no paid calls).

Covers:
- report_synthesis_system / _user prompt templates render with placeholders.
- check_report_quality: every dimension and pass/fail scenarios.
- VerifiedReportBuilder with an injected LLM: renumbered citations, synthesis
  output replaces the template, quality gate stored, usage surfaced.
- VerifiedReportBuilder WITHOUT llm_provider: deterministic template fallback
  (fake mode / offline hermetic contract), no quality gate.
- EvalRunner: dict answerer fills EvalResult.citations / tokens_estimated;
  quality_passed=False marks the question failed WITHOUT calling the judge.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from evals.adapter import FixtureDataset
from evals.configs import CONFIGS
from evals.runner import EvalRunner

from core.prompts import get_default_registry
from core.providers.base import LLMResponse
from core.tracing import noop_tracing
from domain.models import Citation, Fact, SourceDocument, SourceKind
from domain.verification import VerificationResult
from service.verified_report import (
    VerifiedReportBuilder,
    check_report_quality,
)
from service.verifier import CitationVerifier


# --- fixtures ---------------------------------------------------------------
def _cite(cid: str, url: str = "https://x.test/page") -> Citation:
    return Citation(citation_id=cid, kind=SourceKind.WEB, locator=url, title=cid)


class _FakeFetcher:
    def __init__(self, mapping: dict[str, str]) -> None:
        self._m = mapping

    async def fetch(self, locator: str, *, citation_id: str) -> SourceDocument:
        text = self._m.get(locator, "")
        if not text:
            return SourceDocument(
                citation=_cite(citation_id, locator),
                content="",
                error="not found",
                fetched_ok=False,
            )
        return SourceDocument(
            citation=_cite(citation_id, locator), content=text, fetched_ok=True
        )


class _ScriptedLLM:
    """Returns a fixed report; records every request; reports REAL usage."""

    model_id = "synth-mock"

    def __init__(self, text: str) -> None:
        self._text = text
        self.calls: list[list] = []

    async def acomplete(self, messages, *, model_id=None, max_tokens=None):  # type: ignore[no-untyped-def]
        self.calls.append(list(messages))
        return LLMResponse(
            text=self._text,
            model=self.model_id,
            provider="mock",
            prompt_tokens=111,
            completion_tokens=222,
            usage_estimated=False,
        )


_GOOD_REPORT = (
    "# 调研报告：LangGraph 是什么\n\n"
    "LangGraph 是一个面向有状态 agent 的编排框架 [c1]。"
    "它把 agent 建模为带类型状态的节点图 [c1]。\n\n"
    "## 分析\nLangGraph 主要用于需要检查点和状态机语义的多步 agent 场景，"
    "这一点与检索导向的框架定位不同。报告内容足够长以通过非空检查。"
    "这是补充文本，确保报告长度远超 100 字符的下限，"
    "同时不回显用户原始问题中的任务指令。"
)


# --- prompt rendering --------------------------------------------------------
def test_synthesis_prompts_render_with_placeholders():
    reg = get_default_registry()
    sys_p = reg.render("report_synthesis_system")
    assert "research-report writer" in sys_p.system_text()
    assert "[cN]" in sys_p.system_text()
    u = reg.render(
        "report_synthesis_user",
        query="用户原始问题 Q?",
        verified_facts="FACT BLOCK",
        citations="CIT BLOCK",
        constraints="CONSTRAINT BLOCK",
    )
    body = u.messages[0][1]
    assert "用户原始问题 Q?" in body
    assert "FACT BLOCK" in body
    assert "CIT BLOCK" in body
    assert "CONSTRAINT BLOCK" in body


# --- quality gate: unit dimensions ------------------------------------------
def _cites(*ids: str) -> list[Citation]:
    return [_cite(i) for i in ids]


def test_quality_gate_passes_on_valid_report():
    q = check_report_quality(_GOOD_REPORT, query="LangGraph 是什么？", citations=_cites("c1"))
    assert q["passed"] is True
    assert q["failure_reason"] == ""


def test_quality_gate_non_empty():
    q = check_report_quality("", query="q", citations=_cites("c1"))
    assert not q["non_empty"] and not q["passed"]
    assert "non_empty" in q["failure_reason"]


def test_quality_gate_detects_prompt_echo():
    query = "请详细分析 LangGraph 与 LlamaIndex 的架构差异并给出选型建议和适用场景。"
    q = check_report_quality(query, query=query, citations=_cites("c1"))
    assert not q["not_prompt_echo"] and not q["passed"]
    assert "not_prompt_echo" in q["failure_reason"]


def test_quality_gate_requires_citation_tags():
    md = "这是一份完全没有引用标签的长报告内容。" * 20
    q = check_report_quality(md, query="用户问题", citations=_cites("c1"))
    assert not q["has_citation_tags"] and not q["passed"]
    assert "has_citation_tags" in q["failure_reason"]


def test_quality_gate_catch_untraceable_citation():
    md = "足够长的报告正文内容 " * 20 + "结论见 [c99]。"
    q = check_report_quality(md, query="用户问题", citations=_cites("c1"))
    assert not q["citations_traceable"] and not q["passed"]
    assert "citations_traceable" in q["failure_reason"]


def test_quality_gate_requires_input_citations():
    md = "足够长的报告正文内容 " * 20 + "结论见 [c1]。"
    q = check_report_quality(md, query="用户问题", citations=[])
    assert not q["citations_nonempty"] and not q["passed"]
    assert "citations_nonempty" in q["failure_reason"]


# --- builder: LLM synthesis path --------------------------------------------
def _facts_and_citations() -> tuple[list[Fact], list[Citation]]:
    facts = [
        Fact(
            fact_id="f1",
            claim="LangGraph is a graph of nodes with typed state.",
            source_citation_ids=["c_worker1_1"],
        )
    ]
    citations = [
        Citation(
            citation_id="c_worker1_1",
            kind=SourceKind.WEB,
            locator="https://x.test/page",
            title="LangGraph overview",
        )
    ]
    return facts, citations


@pytest.mark.asyncio
async def test_builder_synthesizes_with_llm_provider():
    fetcher = _FakeFetcher(
        {"https://x.test/page": "LangGraph is a graph of nodes with typed state."}
    )
    facts, citations = _facts_and_citations()
    llm = _ScriptedLLM(_GOOD_REPORT)
    builder = VerifiedReportBuilder(
        verifier=CitationVerifier(fetcher=fetcher),
        llm_provider=llm,
        tracing=noop_tracing(),
    )
    report = await builder.build(
        query="LangGraph 是什么？", facts=facts, citations=citations, mode="live"
    )
    # LLM output replaced the template.
    assert report.markdown == _GOOD_REPORT
    assert report.markdown.startswith("# 调研报告")
    # Quality gate ran and passed.
    assert report.synthesis_quality["passed"] is True
    # Real usage surfaced.
    assert builder.last_synthesis_usage is not None
    assert builder.last_synthesis_usage.prompt_tokens == 111
    assert builder.last_synthesis_usage.usage_estimated is False
    # Post-source-policy citations are exposed on the report (original ids kept).
    assert report.citations[0].citation_id == "c_worker1_1"
    # The prompt the LLM saw: query + renumbered citation [c1].
    user_msg = llm.calls[0][-1].content
    assert "LangGraph 是什么？" in user_msg
    assert "[c1] LangGraph overview — https://x.test/page" in user_msg


@pytest.mark.asyncio
async def test_builder_repairs_untraceable_synthesis_with_verified_template():
    fetcher = _FakeFetcher(
        {"https://x.test/page": "LangGraph is a graph of nodes with typed state."}
    )
    facts, citations = _facts_and_citations()
    invalid_report = (
        "# Research report\n\n"
        + "A sufficiently detailed report grounded in the supplied evidence. " * 8
        + "Unsupported citation marker [c99]."
    )
    builder = VerifiedReportBuilder(
        verifier=CitationVerifier(fetcher=fetcher),
        llm_provider=_ScriptedLLM(invalid_report),
        tracing=noop_tracing(),
    )

    report = await builder.build(
        query="What is LangGraph?", facts=facts, citations=citations, mode="live"
    )

    assert report.synthesis_quality["passed"] is True
    assert report.synthesis_quality["repaired_with_traceable_template"] is True
    assert "[c1]" in report.markdown
    assert "[c99]" not in report.markdown


@pytest.mark.asyncio
async def test_builder_synthesis_failure_records_failed_quality():
    fetcher = _FakeFetcher(
        {"https://x.test/page": "LangGraph is a graph of nodes with typed state."}
    )
    facts, citations = _facts_and_citations()

    class _BoomLLM:
        model_id = "boom"

        async def acomplete(self, messages, *, model_id=None, max_tokens=None):  # type: ignore[no-untyped-def]
            raise RuntimeError("provider down")

    builder = VerifiedReportBuilder(
        verifier=CitationVerifier(fetcher=fetcher),
        llm_provider=_BoomLLM(),
        tracing=noop_tracing(),
    )
    report = await builder.build(
        query="LangGraph 是什么？", facts=facts, citations=citations, mode="live"
    )
    # Fell back to the template, but quality gate is recorded as failed.
    assert "已验证声明" in report.markdown
    assert report.synthesis_quality["passed"] is False
    assert "synthesis" in report.synthesis_quality["failure_reason"]


# --- builder: fake / template fallback --------------------------------------
@pytest.mark.asyncio
async def test_builder_without_llm_uses_template_and_no_gate():
    fetcher = _FakeFetcher(
        {"https://x.test/page": "LangGraph is a graph of nodes with typed state."}
    )
    facts, citations = _facts_and_citations()
    builder = VerifiedReportBuilder(
        verifier=CitationVerifier(fetcher=fetcher),
    )
    assert builder._llm_provider is None
    report = await builder.build(
        query="LangGraph 是什么？", facts=facts, citations=citations, mode="fake"
    )
    # Deterministic template (contains the known section headers).
    assert "已验证声明" in report.markdown
    # No synthesis -> no quality gate record; no usage.
    assert report.synthesis_quality == {}
    assert builder.last_synthesis_usage is None


@pytest.mark.asyncio
async def test_builder_caps_and_parallelizes_claim_verification() -> None:
    class TrackingVerifier:
        def __init__(self) -> None:
            self.active = 0
            self.max_active = 0
            self.calls: list[str] = []

        async def verify(self, claim, citations, *, round_label="initial"):  # type: ignore[no-untyped-def]
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.calls.append(claim.claim_id)
            await asyncio.sleep(0.01)
            self.active -= 1
            return VerificationResult(
                claim_id=claim.claim_id,
                verdict="entailment",
                reason="ok",
                round=round_label,
            )

    facts = [
        Fact(
            fact_id=f"f{i}",
            claim=f"supported fact number {i}",
            source_citation_ids=[f"src{i}"],
        )
        for i in range(12)
    ]
    citations = [_cite(f"src{i}", f"https://x.test/{i}") for i in range(12)]
    verifier = TrackingVerifier()
    builder = VerifiedReportBuilder(verifier=verifier)  # type: ignore[arg-type]

    report = await builder.build(query="q", facts=facts, citations=citations, mode="fake")

    assert len(report.claims) == 8
    assert len(verifier.calls) == 8
    assert verifier.max_active == 2


# --- EvalRunner integration ---------------------------------------------------
class _RecordingJudge:
    """Keyword-style judge that records every call (must stay 0 on gate failure)."""

    kind = "keyword"

    def __init__(self) -> None:
        self.calls = 0

    def score(self, answer: str, keywords: list[str]):  # type: ignore[no-untyped-def]
        self.calls += 1
        return 1.0, "ok"


def test_eval_runner_dict_answerer_fills_citations_and_tokens(tmp_path: Path):
    ds = FixtureDataset()

    def answerer(prompt):  # type: ignore[no-untyped-def]
        return {
            "answer": "a" * 500,
            "rounds": 2,
            "tokens": 1234,
            "citations": [
                {
                    "citation_id": "c1",
                    "title": "t",
                    "url": "https://example.com/a",
                    "source_date": "2026-01-01",
                }
            ],
            "quality_passed": True,
            "usage_estimated": False,
        }

    out = tmp_path / "run"
    runner = EvalRunner(
        out_dir=out, config=CONFIGS["full"], questions=ds.questions(), answerer=answerer
    )
    results = runner.run_sync(max_questions=1)
    res = results[0]
    assert res["status"] == "completed"
    assert res["citations"] == ["https://example.com/a"]
    assert res["tokens_estimated"] == 1234
    assert res["usage_estimated"] is False


def test_eval_runner_quality_gate_failed_skips_judge(tmp_path: Path):
    ds = FixtureDataset()
    judge = _RecordingJudge()

    def answerer(prompt):  # type: ignore[no-untyped-def]
        return {
            "answer": "",
            "rounds": 0,
            "tokens": 0,
            "citations": [],
            "quality_passed": False,
            "usage_estimated": True,
        }

    runner = EvalRunner(
        out_dir=tmp_path / "run",
        config=CONFIGS["full"],
        questions=ds.questions(),
        answerer=answerer,
        judge=judge,
    )
    results = runner.run_sync(max_questions=1)
    assert results[0]["status"] == "failed"
    assert "quality gate" in results[0]["error"]
    # Judge was NEVER consulted for a gate-failed answer.
    assert judge.calls == 0
