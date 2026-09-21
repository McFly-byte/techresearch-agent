"""Baseline answerers that actually call service code.

Three reference baselines, all offline (fake provider):
- llm_only: no retrieval; just asks the fake LLM to answer.
- naive_rag: one round of fake search + direct summary, no reflection/verifier.
- full: the real graph + verifier.

Each returns ``(answer, n_search_rounds, tokens_estimated)``.

P0-1 / P0-4 additions:
- ``make_research_runner_answerer(mode=...)`` returns the production answerer.
  It goes THROUGH ``api.runner.ResearchRunner`` (the same object that serves the
  live API) instead of hand-rolling a graph, so evals measure the real shipped
  system. ``mode="fake"`` is offline; ``mode="live"`` uses real Qwen + Tavily.
- Every answerer receives ONLY an ``EvalPrompt`` (qid + question). The gold
  ``reference_answer`` / ``rubrics`` never reach the research system.
"""

from __future__ import annotations

from typing import Any

from agents.budget import BudgetManager
from agents.worker import WorkerNode
from api.runner import ResearchRunner
from api.task_store import TaskStore
from core.tracing import LangSmithTracing
from domain.models import SearchResult
from evals.adapter import EvalPrompt
from graph.builder import build_graph
from tools.fetchers import FakeFetcher
from tools.search_providers import FakeSearchProvider


def _hit(url: str) -> SearchResult:
    return SearchResult(title="doc", url=url, snippet="")


async def answer_llm_only(prompt: EvalPrompt) -> tuple[str, int, int]:
    """No retrieval: just a canned LLM answer."""
    return f"Answer (llm_only): {prompt.question}", 0, 20


async def answer_naive_rag(prompt: EvalPrompt) -> tuple[str, int, int]:
    """One round of fake search + direct summary, no reflection/verifier."""
    url = f"https://example.com/{prompt.question[:20]}"
    search = FakeSearchProvider({prompt.question: [_hit(url)]})
    fetcher = FakeFetcher({url: f"about {prompt.question}"})
    hits = await search.search(prompt.question, max_results=1)
    if not hits:
        return "no results", 1, 10
    doc = await fetcher.fetch(hits[0].url, citation_id="c_eval")
    return f"Answer (naive_rag): {doc.content}", 1, 30


async def answer_full(prompt: EvalPrompt) -> tuple[str, int, int]:
    """Run the real graph (fake provider) and return its markdown report."""
    search = FakeSearchProvider({prompt.question: [_hit("https://example.com/x")]})
    fetcher = FakeFetcher({"https://example.com/x": f"evidence for {prompt.question}"})
    budget = BudgetManager()
    worker = WorkerNode(web_search=search, fetcher=fetcher, budget=budget)
    graph = build_graph(worker=worker, budget=budget)
    result = await graph.ainvoke(
        {"query": prompt.question, "research_depth": "quick"},
    )
    md = result.get("report_markdown", "")
    return md, budget.rounds_used("task_1"), budget.tokens_used()


# --- Production answerer: through ResearchRunner -----------------------------


def make_research_runner_answerer(
    mode: str = "fake",
    *,
    research_timeout: float = 420.0,
    write_timeout: float = 180.0,
    verifier_call_timeout: float = 60.0,
    synthesis_call_timeout: float = 120.0,
    research_workers: int = 2,
):
    """Build an async answerer that runs the REAL shipped research system.

    Returns ``async (EvalPrompt) -> dict`` with keys:
      answer (str), rounds (int), tokens (int), citations (list[dict]),
      quality_passed (bool), usage_estimated (bool).

    - ``mode="fake"``: fully offline (FakeLLM + canned search/fetch). No keys.
    - ``mode="live"``: real Qwen + Tavily. ResearchRunner fails closed if the
      required keys are missing (it never silently degrades to fake).

    ResearchRunner.run() mutates a TaskRecord in place (report_markdown,
    status, error). A non-completed status — including a failed report quality
    gate — is surfaced as an exception so the runner marks the question failed.
    """

    async def _answer(prompt: EvalPrompt) -> dict[str, Any]:
        store = TaskStore(max_tasks=1)
        rec = store.create(query=prompt.question, user_context="", depth="standard")
        # Thread the DRB2 source constraints (blocked urls/domains/titles +
        # as-of cutoff) onto the task record so ResearchRunner passes them to
        # the worker and the report builder. These are safety rules already in
        # the prompt, not answer leakage.
        rec.blocked_urls = list(prompt.blocked_urls)
        rec.blocked_domains = list(prompt.blocked_domains)
        rec.blocked_titles = list(prompt.blocked_titles)
        rec.as_of_date = prompt.as_of_date
        tracing = None
        if mode == "live":
            try:
                from langsmith.run_helpers import get_current_run_tree

                root = get_current_run_tree()
                if root is not None:
                    tracing = LangSmithTracing(
                        client=root.ls_client,
                        project_name=root.session_name or "deepresearch-eval",
                    )
            except Exception:  # noqa: BLE001 - tracing must not block an eval
                tracing = None
        runner = ResearchRunner(
            store,
            mode=mode,
            tracing=tracing,
            research_timeout=research_timeout,
            write_timeout=write_timeout,
            verifier_call_timeout=verifier_call_timeout,
            synthesis_call_timeout=synthesis_call_timeout,
            max_workers=research_workers,
        )
        await runner.run(rec)
        if rec.status != "completed":
            raise RuntimeError(rec.error or "research runner did not complete")
        # ResearchRunner does not expose the budget; derive rounds from events.
        rounds = sum(1 for e in rec.events if e.get("stage") == "worker_done")
        return {
            "answer": rec.report_markdown,
            "rounds": rounds,
            "tokens": rec.usage_tokens,
            "citations": list(rec.citations),
            "quality_passed": bool(rec.report_quality.get("passed", True)),
            "usage_estimated": rec.usage_estimated,
        }

    _answer.__name__ = f"answer_research_runner_{mode}"
    return _answer


async def answer_research_runner(prompt: EvalPrompt) -> tuple[str, int, int]:
    """Default production answerer (fake mode). Prefer the factory for live."""
    return await make_research_runner_answerer(mode="fake")(prompt)


BASELINES: dict[str, Any] = {
    "llm_only": answer_llm_only,
    "naive_rag": answer_naive_rag,
    "full": answer_full,
}


__all__ = [
    "BASELINES",
    "answer_full",
    "answer_llm_only",
    "answer_naive_rag",
    "answer_research_runner",
    "make_research_runner_answerer",
]
