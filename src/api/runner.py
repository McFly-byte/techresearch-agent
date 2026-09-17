"""Background research runner: emits SSE events as the graph progresses.

Mode contract (P0 correction):
- `mode="fake"` (DEFAULT): forces FakeLLM + FakeSearchProvider + FakeFetcher.
  NEVER touches the network, even if real API keys are present in the
  environment. Fake is fake regardless of settings.
- `mode="live"`: requires TAVILY_API_KEY AND a real LLM provider (Qwen).
  Fails closed (ConfigurationError) if required keys are missing. Never
  silently degrades to fake.
- `mode` must be exactly "fake" or "live"; anything else raises ValueError
  at run() entry.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from typing import Any

from agents.budget import BudgetManager
from agents.worker import ModelRouter, WorkerNode
from api.task_store import TaskRecord, TaskStore
from core.config import Settings
from core.exceptions import ConfigurationError
from core.providers.factory import build_provider
from core.providers.fake import FakeLLM
from core.tracing import TracingContext, build_tracing, noop_tracing
from domain.models import SearchResult
from graph.builder import build_graph
from service.verified_report import VerifiedReportBuilder
from tools.fetchers import FakeFetcher
from tools.search_providers import FakeSearchProvider

SCHEMA_VERSION = "1.0"

VALID_MODES = ("fake", "live")

# Banner injected into fake-mode reports so readers know it is fixture data.
FAKE_MODE_BANNER_MD = (
    "> ⚠️ 本报告由 fake 模式生成，使用预定义测试资料（example.com），"
    "仅供演示和测试，不代表真实研究结果。"
)
FAKE_MODE_BANNER_HTML = (
    '<div style="background:#fff3cd;border:1px solid #ffeaa7;padding:12px;'
    'margin:8px 0;border-radius:4px;color:#856404;">'
    "⚠️ 本报告由 fake 模式生成，使用预定义测试资料（example.com），"
    "仅供演示和测试，不代表真实研究结果。"
    "</div>"
)


def _envelope(
    *,
    event_type: str,
    task_id: str,
    stage: str,
    data: dict[str, Any],
) -> dict[str, Any]:
    return {
        "event_id": str(uuid.uuid4()),
        "event_type": event_type,
        "task_id": task_id,
        "timestamp": time.time(),
        "stage": stage,
        "data": data,
        "schema_version": SCHEMA_VERSION,
    }


# --- Deterministic canned sources for fake mode -----------------------------
_FAKE_URLS = [
    "https://example.com/langgraph",
    "https://example.com/llamaindex",
    "https://example.com/deepresearch",
]
_FAKE_DOCS = {
    _FAKE_URLS[0]: (
        "LangGraph is a low-level orchestration framework for stateful agents. "
        "It models agents as a graph of nodes with typed state and checkpoints."
    ),
    _FAKE_URLS[1]: (
        "LlamaIndex is a data framework focused on retrieval over external data. "
        "It provides high-level agents and indexing abstractions."
    ),
    _FAKE_URLS[2]: (
        "Deep research agents combine planning, search, fetch and synthesis to "
        "produce long-form reports with citations."
    ),
}


def _fake_kit(settings: Settings) -> ProviderKit:
    """Build a fully offline kit. No network. Deterministic.

    Forces FakeLLM regardless of settings. Even if the environment has a
    real DashScope/Tavily key, fake mode must NOT read it or construct a
    Qwen provider. Fake is fake.
    """
    hits = [
        SearchResult(title="LangGraph overview", url=_FAKE_URLS[0], snippet="graph of nodes"),
        SearchResult(title="LlamaIndex overview", url=_FAKE_URLS[1], snippet="retrieval framework"),
        SearchResult(title="Deep research", url=_FAKE_URLS[2], snippet="planning + search"),
    ]

    class _CannedSearch(FakeSearchProvider):
        async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
            return hits[:max_results]

    web = _CannedSearch({})
    fetcher = FakeFetcher(mapping=_FAKE_DOCS)
    # FORCED: ignore settings.llm_provider and any API key.
    llm = FakeLLM(model_id="fake-1")
    return ProviderKit(web_search=web, fetcher=fetcher, llm=llm, mode="fake")


def _live_kit(settings: Settings) -> ProviderKit:
    """Build a live kit. Fails closed if required keys missing.

    Requires:
    - TAVILY_API_KEY for web search.
    - A real LLM provider: llm_provider must resolve to qwen (not fake).
      If llm_provider="fake" or "auto" without DASHSCOPE_API_KEY, raise
      ConfigurationError — never silently degrade to fake.
    """
    if not settings.has_tavily_key:
        raise ConfigurationError(
            "mode=live requires TAVILY_API_KEY. Put it in .env or drop mode="
            "fake to use offline deterministic sources."
        )
    # Live mode must not silently fall back to a fake LLM.
    if settings.llm_provider == "fake":
        raise ConfigurationError(
            "mode=live requires a real LLM provider, but llm_provider='fake'. "
            "Set llm_provider='auto' or 'qwen' and provide DASHSCOPE_API_KEY."
        )
    if settings.llm_provider in ("qwen", "auto") and not settings.has_dashscope_key:
        raise ConfigurationError(
            f"mode=live requires DASHSCOPE_API_KEY (llm_provider={settings.llm_provider!r} "
            "resolved without a key). Set DASHSCOPE_API_KEY or use mode='fake'."
        )

    from tools.fetchers import HttpPageFetcher
    from tools.search_providers import TavilySearchProvider

    web = TavilySearchProvider(api_key=settings.tavily_api_key.get_secret_value())
    fetcher = HttpPageFetcher()
    llm = build_provider(settings=settings)
    return ProviderKit(web_search=web, fetcher=fetcher, llm=llm, mode="live")


@dataclass
class ProviderKit:
    """Resolved tools for one run."""

    web_search: Any
    fetcher: Any
    llm: Any
    mode: str = "fake"


def _build_budget(settings: Settings) -> BudgetManager:
    """Construct the stage-3 BudgetManager FROM settings.

    Previously the runner called ``BudgetManager()`` with hard-coded defaults,
    silently ignoring ``max_total_tokens`` / ``max_search_rounds`` / the warning
    / critical ratios from configuration.
    """
    return BudgetManager(
        global_token_budget=settings.max_total_tokens,
        max_search_rounds=settings.max_search_rounds,
        warning_ratio=settings.warning_token_ratio,
        critical_ratio=settings.critical_token_ratio,
        per_task_token_budget=settings.per_task_token_budget or None,
    )


def _build_router(settings: Settings, llm: Any) -> ModelRouter:
    """Wire model downgrade routing from settings.

    The preferred model is the provider's OWN model (in live mode this IS
    ``settings.qwen_model``); the cheap downgrade model is read from
    ``settings.qwen_model_fast`` so the fast/cheap model is actually configured
    instead of always ``None``.
    """
    preferred = getattr(llm, "model_id", None) or settings.qwen_model
    return ModelRouter(preferred=preferred, cheap=settings.qwen_model_fast)


class ResearchRunner:
    def __init__(
        self,
        store: TaskStore,
        *,
        settings: Settings | None = None,
        kit: ProviderKit | None = None,
        mode: str = "fake",
        tracing: TracingContext | None = None,
        memory_store: Any = None,
    ) -> None:
        self._store = store
        self._memory_store = memory_store
        from core.config import get_settings

        self._settings = settings or get_settings()
        self._kit = kit
        self._mode = mode
        # Tracing resolution (stage-0-1 boundary):
        # - Injected tracing always wins (tests inject RecordingTracing).
        # - fake mode FORCES noop tracing, regardless of any stored key or the
        #   tracing v2 switch. Fake is fake: zero langsmith.Client, zero network.
        # - live mode: build the real LangSmithTracing ONLY when a key is present
        #   AND langchain_tracing_v2=True. Otherwise noop (no client, zero network).
        if tracing is not None:
            self._tracing = tracing
        elif self._mode == "fake":
            self._tracing = noop_tracing()
        else:
            api_key = self._settings.langchain_api_key.get_secret_value()
            if api_key.strip() and self._settings.langchain_tracing_v2:
                self._tracing = build_tracing(
                    api_key=api_key,
                    project_name=self._settings.langchain_project,
                )
            else:
                self._tracing = noop_tracing()

    def _emit(
        self, rec: TaskRecord, *, stage: str, data: dict[str, Any], event_type: str = "stage"
    ) -> None:
        self._store.append_event(
            rec.task_id,
            _envelope(event_type=event_type, task_id=rec.task_id, stage=stage, data=data),
        )

    async def run(self, rec: TaskRecord) -> None:
        # Validate mode BEFORE doing any work.
        if self._mode not in VALID_MODES:
            raise ValueError(f"invalid mode: {self._mode!r}; must be one of {VALID_MODES}")

        self._emit(rec, stage="queued", data={"query": rec.query, "user_context": rec.user_context})
        rec.status = "running"
        self._emit(rec, stage="running", data={})

        try:
            kit = self._kit or (
                _fake_kit(self._settings) if self._mode == "fake" else _live_kit(self._settings)
            )
            # Record the actual mode on the task record for the API response.
            rec.mode = kit.mode

            budget = _build_budget(self._settings)
            router = _build_router(self._settings, kit.llm)
            worker = WorkerNode(
                web_search=kit.web_search,
                fetcher=kit.fetcher,
                budget=budget,
                llm_provider=kit.llm,
                tracing=self._tracing,
                router=router,
            )
            graph = build_graph(
                worker=worker, budget=budget, max_workers=self._settings.max_workers
            )

            self._tracing.start_span("planner", task_id=rec.task_id)
            self._emit(rec, stage="planner_start", data={})
            result: dict[str, Any] = {}
            planner_exc: BaseException | None = None
            try:
                result = await asyncio.wait_for(
                    graph.ainvoke(
                        {
                            "query": rec.query,
                            "user_context": rec.user_context,
                            "research_depth": rec.research_depth,
                        }
                    ),
                    timeout=60.0,
                )
            except BaseException as exc:  # noqa: BLE001
                planner_exc = exc
                raise
            finally:
                # Always close the planner span, even on error / cancel / timeout.
                self._tracing.end_span(
                    "planner",
                    task_id=rec.task_id,
                    status="error" if planner_exc is not None else "ok",
                    error=planner_exc if isinstance(planner_exc, Exception) else None,
                )

            facts = result.get("facts", [])
            citations = result.get("citations", [])

            # P0 guard: empty facts/citations is NOT success.
            if not facts or not citations:
                rec.status = "failed"
                rec.error = "empty_result: no facts/citations produced"
                self._emit(
                    rec,
                    event_type="error",
                    stage="failed",
                    data={
                        "message": rec.error,
                        "n_facts": len(facts),
                        "n_citations": len(citations),
                    },
                )
                return

            self._emit(rec, stage="planner_done", data={"n_facts": len(facts)})

            for st in result.get("subtasks", []):
                self._emit(
                    rec,
                    stage="worker_done",
                    data={
                        "task_id": getattr(st, "task_id", ""),
                        "status": getattr(st, "status", ""),
                    },
                )

            self._tracing.start_span("write", task_id=rec.task_id)
            self._emit(rec, stage="verify_start", data={})
            # Stage4 P0-4: live mode injects an independent LLM-backed NLI
            # judge (scoped to qwen_verifier_model). Fake mode keeps the
            # deterministic heuristic so offline tests never call a network.
            from service.verifier import LLMNLI, CitationVerifier

            if kit.mode == "live":
                verifier_llm = kit.llm.with_model(self._settings.qwen_verifier_model)
                nli = LLMNLI(verifier_llm)
                verifier = CitationVerifier(fetcher=kit.fetcher, nli=nli)
            else:
                verifier = CitationVerifier(fetcher=kit.fetcher)
            builder = VerifiedReportBuilder(verifier=verifier)
            write_exc: BaseException | None = None
            try:
                report = await builder.build(
                    query=rec.query, facts=facts, citations=citations, mode=kit.mode
                )
            except BaseException as exc:  # noqa: BLE001
                write_exc = exc
                raise
            finally:
                # Always close the write span, even on error / cancel.
                self._tracing.end_span(
                    "write",
                    task_id=rec.task_id,
                    status="error" if write_exc is not None else "ok",
                    n_facts=len(facts) if write_exc is None else 0,
                    error=write_exc if isinstance(write_exc, Exception) else None,
                )

            self._emit(rec, stage="verify_done", data={})
            self._emit(rec, stage="write_done", data={"len": len(report.markdown)})

            rec.report_markdown = report.markdown
            rec.report_html = report.html
            rec.status = "completed"
            self._emit(
                rec,
                event_type="done",
                stage="done",
                data={
                    "mode": kit.mode,
                    "n_facts": len(facts),
                    "n_citations": len(citations),
                    "metrics": {
                        "verified": report.metrics.verified,
                        "contradicted": report.metrics.contradicted,
                        "neutral": report.metrics.neutral,
                    },
                },
            )
        except asyncio.CancelledError:
            rec.status = "cancelled"
            rec.error = "cancelled"
            self._emit(rec, event_type="cancelled", stage="cancelled", data={})
        except Exception as e:  # noqa: BLE001
            rec.status = "failed"
            rec.error = str(e)
            self._emit(rec, event_type="error", stage="failed", data={"message": str(e)})


__all__ = ["ProviderKit", "ResearchRunner", "SCHEMA_VERSION", "VALID_MODES"]
