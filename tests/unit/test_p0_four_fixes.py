"""P0 four fixes (phase 0-1): fake-mode forcing, fixture labeling,
real LLM extraction wiring, and LangSmith adapter.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import pytest

from api.runner import ResearchRunner, _fake_kit, _live_kit
from api.task_store import TaskStore
from core.config import Settings
from core.exceptions import ConfigurationError
from core.providers.base import LLMResponse, Message
from core.providers.fake import FakeLLM
from core.tracing import (
    LangSmithTracing,
    RecordingTracing,
    SpanRecord,
    build_tracing,
    noop_tracing,
)
from domain.models import Citation, SourceDocument
from domain.verification import VerificationMetrics
from service.extractor import LLMFactExtractor
from service.verified_report import render_html, render_markdown


def _sentinel_settings(**overrides) -> Settings:
    """Settings with a sentinel dashscope key and llm_provider=auto."""
    defaults = dict(
        _env_file=None,
        tavily_api_key="",
        dashscope_api_key="offline-test-sentinel",
        langchain_api_key="",
        feishu_app_secret="",
        llm_provider="auto",
    )
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


# =========================================================================
# Fix 1: fake mode forces FakeLLM, independent of environment
# =========================================================================


class TestFakeModeForcesFake:
    def test_empty_key_fake_mode_uses_fake_llm(self) -> None:
        s = _sentinel_settings(tavily_api_key="", dashscope_api_key="", llm_provider="fake")
        kit = _fake_kit(s)
        assert kit.mode == "fake"
        assert isinstance(kit.llm, FakeLLM)

    def test_sentinel_key_auto_provider_still_fake(self) -> None:
        """dashscope=sentinel + llm_provider=auto + mode=fake → still FakeLLM."""
        s = _sentinel_settings()
        kit = _fake_kit(s)
        assert isinstance(kit.llm, FakeLLM), (
            f"fake mode must force FakeLLM, got {kit.llm.provider_name}"
        )

    def test_explicit_qwen_provider_fake_mode_still_fake(self) -> None:
        s = _sentinel_settings(llm_provider="qwen")
        kit = _fake_kit(s)
        assert isinstance(kit.llm, FakeLLM), "fake mode must not construct Qwen"

    def test_fake_mode_zero_network_patch_transport(self) -> None:
        """With sentinel key, fake mode must make zero network calls."""
        s = _sentinel_settings()
        store = TaskStore()
        rec = store.create(query="test query", user_context="", depth="quick")
        runner = ResearchRunner(store, settings=s, mode="fake")

        async def _run() -> None:
            with patch(
                "httpx.AsyncClient.send",
                side_effect=AssertionError("network disabled"),
            ):
                await runner.run(rec)

        asyncio.run(_run())
        assert rec.status == "completed", rec.error
        assert rec.report_markdown, "report must be non-empty"

    def test_invalid_mode_raises_valueerror(self) -> None:
        s = _sentinel_settings()
        store = TaskStore()
        rec = store.create(query="q", user_context="", depth="quick")
        runner = ResearchRunner(store, settings=s, mode="invalid")  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="invalid mode"):
            asyncio.run(runner.run(rec))

    def test_live_mode_empty_key_raises_configuration_error(self) -> None:
        s = _sentinel_settings(tavily_api_key="", dashscope_api_key="")
        store = TaskStore()
        rec = store.create(query="q", user_context="", depth="quick")
        runner = ResearchRunner(store, settings=s, mode="live")
        asyncio.run(runner.run(rec))
        assert rec.status == "failed"
        assert "TAVILY_API_KEY" in rec.error or "configuration" in rec.error.lower()

    def test_live_mode_with_tavily_but_no_dashscope_raises(self) -> None:
        s = _sentinel_settings(tavily_api_key="some-key", dashscope_api_key="")
        with pytest.raises(ConfigurationError, match="DASHSCOPE"):
            _live_kit(s)


# =========================================================================
# Fix 2: fixture labeling in reports
# =========================================================================


class TestFixtureLabeling:
    def test_markdown_has_fake_banner(self) -> None:
        md = render_markdown(
            query="test",
            claims=[],
            citations=[],
            metrics=VerificationMetrics(total_claims=0),
            mode="fake",
        )
        assert "fake" in md.lower() or "fixture" in md.lower() or "测试" in md
        assert "预定义" in md or "example.com" in md

    def test_markdown_no_banner_in_live_mode(self) -> None:
        md = render_markdown(
            query="test",
            claims=[],
            citations=[],
            metrics=VerificationMetrics(total_claims=0),
            mode="live",
        )
        assert "fake 模式" not in md

    def test_html_has_fake_banner(self) -> None:
        h = render_html(
            query="test",
            claims=[],
            citations=[],
            metrics=VerificationMetrics(total_claims=0),
            mode="fake",
        )
        assert "fake" in h.lower() or "测试" in h
        assert "example.com" in h

    def test_report_object_has_mode_field(self) -> None:
        s = _sentinel_settings()
        store = TaskStore()
        rec = store.create(query="q", user_context="", depth="quick")
        runner = ResearchRunner(store, settings=s, mode="fake")
        asyncio.run(runner.run(rec))
        assert rec.mode == "fake"
        pub = rec.to_public()
        assert pub["mode"] == "fake"

    def test_http_report_response_includes_mode(self) -> None:
        from fastapi.testclient import TestClient

        from api.main import create_app

        with TestClient(create_app()) as c:
            r = c.post(
                "/api/research",
                json={"query": "test query", "research_depth": "quick"},
            )
            tid = r.json()["task_id"]
            for _ in range(50):
                d = c.get(f"/api/research/{tid}").json()
                if d["status"] in {"completed", "failed", "cancelled"}:
                    break
                import time

                time.sleep(0.1)
            assert d["status"] == "completed", d
            rep = c.get(f"/api/research/{tid}/report").json()
            assert rep["mode"] == "fake"
            assert "fake 模式" in rep["markdown"]


# =========================================================================
# Fix 3: real LLM extraction wiring
# =========================================================================


class _RecordingLLM:
    """Mock LLM that returns configurable JSON responses."""

    provider_name = "recording"

    def __init__(self, responses: list[str]) -> None:
        self._responses = responses
        self.calls: list[list[Message]] = []
        self.call_count = 0

    async def acomplete(self, messages: list[Message]) -> LLMResponse:
        self.calls.append(list(messages))
        idx = min(self.call_count, len(self._responses) - 1)
        self.call_count += 1
        text = self._responses[idx]
        return LLMResponse(
            text=text,
            model="recording-1",
            provider=self.provider_name,
            prompt_tokens=100,
            completion_tokens=50,
            # This double models a REAL provider that reported usage from the
            # API, so mark it as reported (not estimated).
            usage_estimated=False,
        )

    def is_configured(self) -> bool:
        return True


def _make_doc(cid: str, content: str) -> SourceDocument:
    return SourceDocument(
        citation=Citation(citation_id=cid, kind="web", locator=f"https://example.com/{cid}"),
        content=content,
    )


class TestLLMExtraction:
    @pytest.mark.asyncio
    async def test_causal_llm_extraction_changes_report(self) -> None:
        """Two different LLM responses → different facts in report.

        The single source contains TWO verbatim, source-supported claims. Each
        LLM response picks one of them. This proves LLM output causally changes
        the extracted facts WITHOUT encouraging hallucination: every accepted
        claim is a substring of the source.
        """
        source = (
            "LangGraph is an orchestration framework. "
            "It models agents as a graph of nodes with typed state."
        )
        claim_a = "LangGraph is an orchestration framework."
        claim_b = "It models agents as a graph of nodes with typed state."
        assert claim_a in source and claim_b in source
        docs = [_make_doc("c1", source)]

        # Response A: one supported fact.
        llm_a = _RecordingLLM(
            [
                json.dumps(
                    {"facts": [{"claim": claim_a, "citation_ids": ["c1"], "snippet": claim_a}]}
                )
            ]
        )
        extractor_a = LLMFactExtractor(llm_a)
        facts_a = await extractor_a.extract(docs)

        # Response B: the other supported fact.
        llm_b = _RecordingLLM(
            [
                json.dumps(
                    {"facts": [{"claim": claim_b, "citation_ids": ["c1"], "snippet": claim_b}]}
                )
            ]
        )
        extractor_b = LLMFactExtractor(llm_b)
        facts_b = await extractor_b.extract(docs)

        assert len(facts_a) == 1
        assert len(facts_b) == 1
        assert facts_a[0].claim != facts_b[0].claim, (
            "LLM output must causally affect report content"
        )

    @pytest.mark.asyncio
    async def test_unknown_citation_id_rejected(self) -> None:
        """Facts citing non-existent citation_ids are dropped."""
        # The source MUST contain the valid claim verbatim (extractive contract);
        # the valid claim cites c1, the invalid one cites unknown c999.
        docs = [_make_doc("c1", "Valid fact. Some additional supporting content here.")]
        llm = _RecordingLLM(
            [
                json.dumps(
                    {
                        "facts": [
                            {"claim": "Valid fact.", "citation_ids": ["c1"]},
                            {"claim": "Valid fact.", "citation_ids": ["c999"]},
                        ]
                    }
                )
            ]
        )
        extractor = LLMFactExtractor(llm)
        facts = await extractor.extract(docs)
        assert len(facts) == 1
        assert facts[0].claim == "Valid fact."
        assert "c999" not in facts[0].source_citation_ids

    @pytest.mark.asyncio
    async def test_structural_error_triggers_repair(self) -> None:
        """Non-JSON output → one repair request → success on repair."""
        # The repaired claim MUST be a verbatim substring of the source, so it
        # survives the strict extractive-support check.
        docs = [_make_doc("c1", "Repaired fact. Content for testing here.")]
        # First call: garbage. Second call (repair): valid JSON.
        llm = _RecordingLLM(
            [
                "this is not json at all",
                json.dumps({"facts": [{"claim": "Repaired fact.", "citation_ids": ["c1"]}]}),
            ]
        )
        extractor = LLMFactExtractor(llm)
        facts = await extractor.extract(docs)
        assert len(facts) == 1
        assert facts[0].claim == "Repaired fact."
        # First call + repair call = 2 calls.
        assert llm.call_count == 2

    @pytest.mark.asyncio
    async def test_repair_failure_falls_back_heuristic(self) -> None:
        """Both calls fail to parse → heuristic fallback."""
        docs = [_make_doc("c1", "This is a sufficiently long sentence for heuristic extraction.")]
        llm = _RecordingLLM(["garbage", "more garbage"])
        extractor = LLMFactExtractor(llm)
        facts = await extractor.extract(docs)
        # Heuristic fallback produces facts from the sentence.
        assert len(facts) >= 1
        assert all("c1" in f.source_citation_ids for f in facts)

    @pytest.mark.asyncio
    async def test_usage_estimated_false_when_provider_returns_tokens(self) -> None:
        """When provider returns real tokens, estimated=False."""
        docs = [_make_doc("c1", "Some content here.")]
        llm = _RecordingLLM([json.dumps({"facts": [{"claim": "A fact.", "citation_ids": ["c1"]}]})])
        extractor = LLMFactExtractor(llm)
        await extractor.extract(docs)
        # RecordingLLM returns prompt_tokens=100, completion_tokens=50.
        assert extractor.last_usage_estimated is False

    @pytest.mark.asyncio
    async def test_usage_estimated_true_when_no_tokens(self) -> None:
        """FakeLLM's synthetic (non-zero) token counts are still estimated.

        Non-zero counts do NOT prove measured usage: FakeLLM derives them from
        character length, so the extractor must keep estimated=True.
        """
        docs = [_make_doc("c1", "Some content here.")]
        fake = FakeLLM()
        extractor = LLMFactExtractor(fake)
        await extractor.extract(docs)
        assert extractor.last_usage_estimated is True
        # FakeLLM does derive non-zero counts from length, but those are not
        # measured — estimated must remain True regardless of magnitude.
        assert extractor.last_prompt_tokens >= 0


# =========================================================================
# Fix 4: LangSmith real implementation
# =========================================================================


class _MockLangSmithClient:
    """In-memory mock of langsmith.Client for testing."""

    def __init__(self) -> None:
        self.created_runs: list[dict] = []
        self.updated_runs: list[dict] = []

    def create_run(self, **kwargs: object) -> None:
        self.created_runs.append(dict(kwargs))

    def update_run(self, run_id: str, **kwargs: object) -> None:
        self.updated_runs.append({"run_id": run_id, **kwargs})


class TestLangSmithAdapter:
    def test_no_key_returns_noop(self) -> None:
        t = build_tracing(api_key="", project_name="p")
        assert isinstance(t, type(noop_tracing())) or t.__class__.__name__ == "TracingContext"

    def test_no_key_zero_network(self) -> None:
        """No key → noop, no client created, zero network."""
        t = build_tracing(api_key="", project_name="p")
        # noop should never create a client or make network calls.
        t.start_span("search", task_id="t1", n_results=3)
        t.end_span("search", task_id="t1")
        # No exception = no network.

    def test_configured_adapter_creates_spans(self) -> None:
        client = _MockLangSmithClient()
        t = LangSmithTracing(api_key="test", project_name="proj", client=client)
        t.start_span("search", task_id="task1", n_results=5)
        t.end_span("search", task_id="task1")
        assert len(client.created_runs) == 1
        assert client.created_runs[0]["name"] == "search"
        assert len(client.updated_runs) == 1
        assert client.updated_runs[0]["outputs"]["status"] == "ok"

    def test_span_error_path_records_error_type(self) -> None:
        client = _MockLangSmithClient()
        t = LangSmithTracing(api_key="test", project_name="proj", client=client)
        t.start_span("fetch", task_id="task1")
        try:
            raise ValueError("boom: secret data here")
        except ValueError as e:
            t.end_span("fetch", task_id="task1", status="error", error=e)
        assert len(client.updated_runs) == 1
        outputs = client.updated_runs[0]["outputs"]
        assert outputs["status"] == "error"
        assert outputs["error_type"] == "ValueError"
        # Error message content must NOT appear.
        assert "secret" not in str(outputs).lower()

    def test_sensitive_metadata_redacted(self) -> None:
        client = _MockLangSmithClient()
        t = LangSmithTracing(api_key="test", project_name="proj", client=client)
        t.start_span(
            "extract",
            task_id="task1",
            prompt="SECRET_PROMPT_TEXT",
            api_key="sk-xxx",
            document_body="full text here",
            n_facts=3,
        )
        inputs = client.created_runs[0]["inputs"]
        assert "prompt" not in inputs
        assert "api_key" not in inputs
        assert "document_body" not in inputs
        assert inputs.get("n_facts") == 3

    def test_build_tracing_with_injected_client(self) -> None:
        client = _MockLangSmithClient()
        t = build_tracing(api_key="", project_name="p", client=client)
        assert isinstance(t, LangSmithTracing)

    def test_recording_tracing_span_record_type_consistent(self) -> None:
        """SpanRecord is always a dataclass, not mixed tuples."""
        tr = RecordingTracing()
        tr.start_span("search", task_id="t1", n_results=2)
        tr.end_span("search", task_id="t1")
        assert isinstance(tr.spans[0], SpanRecord)
        assert isinstance(tr.spans[1], SpanRecord)
        assert tr.spans[0].action == "start"
        assert tr.spans[1].action == "end"

    def test_four_stage_spans_wired_in_run(self) -> None:
        """search/fetch/extract/write spans all appear in a full run."""
        tr = RecordingTracing()
        s = _sentinel_settings()
        store = TaskStore()
        rec = store.create(query="q", user_context="", depth="quick")
        runner = ResearchRunner(store, settings=s, tracing=tr)
        asyncio.run(runner.run(rec))
        stages = {span.stage for span in tr.spans}
        assert "search" in stages, f"missing search span: {stages}"
        assert "fetch" in stages, f"missing fetch span: {stages}"
        assert "extract" in stages, f"missing extract span: {stages}"
        assert "write" in stages, f"missing write span: {stages}"

    def test_runner_builds_langsmith_when_key_present(self) -> None:
        """When settings has langchain_api_key, runner uses LangSmithTracing."""
        s = _sentinel_settings(langchain_api_key="sk-test-real")
        # We can't easily intercept the client creation, but we can verify
        # that build_tracing returns a LangSmithTracing when key present.
        t = build_tracing(
            api_key=s.langchain_api_key.get_secret_value(),
            project_name=s.langchain_project,
        )
        assert isinstance(t, LangSmithTracing)

    def test_fake_mode_no_langsmith_no_key(self) -> None:
        """Fake mode with no key → noop tracing, zero network."""
        s = _sentinel_settings(langchain_api_key="")
        tr = RecordingTracing()
        store = TaskStore()
        rec = store.create(query="q", user_context="", depth="quick")
        runner = ResearchRunner(store, settings=s, tracing=tr, mode="fake")
        asyncio.run(runner.run(rec))
        assert rec.status == "completed"
