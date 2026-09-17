"""Phase 3 graph-level tests: replan cap and checkpoint resume."""

from __future__ import annotations

import pytest
from langgraph.checkpoint.memory import MemorySaver

from agents.budget import BudgetManager
from agents.worker import WorkerNode
from domain.models import SearchResult
from graph.builder import build_graph
from tools.fetchers import FakeFetcher


def _web_returning_none_then_url():  # type: ignore[no-untyped-def]
    """First call returns 0 results, subsequent calls return a URL."""
    calls = {"n": 0}
    url = "https://example.com/good"

    class W:
        async def search(self, query, max_results=5):  # type: ignore[no-untyped-def]
            calls["n"] += 1
            if calls["n"] == 1:
                return []
            return [SearchResult(title="good", url=url, snippet="")]

    return W(), calls


@pytest.mark.asyncio
async def test_first_search_fails_then_rewrite_succeeds():  # type: ignore[no-untyped-def]
    web, calls = _web_returning_none_then_url()
    fetch = FakeFetcher(mapping={"https://example.com/good": "LangGraph uses typed state."})
    worker = WorkerNode(web_search=web, fetcher=fetch)
    g = build_graph(worker=worker)
    out = await g.ainvoke({"query": "LangGraph", "research_depth": "quick"})
    # Worker should have searched at least twice (empty then real hit).
    assert calls["n"] >= 2
    assert len(out["facts"]) > 0
    assert (
        "completed" in out["subtasks"][0].result_summary or out["subtasks"][0].status == "completed"
    )


@pytest.mark.asyncio
async def test_replan_adds_followup_until_cap():  # type: ignore[no-untyped-def]
    # A web provider that always returns 0 facts -> replan triggers.
    class EmptyWeb:
        async def search(self, query, max_results=5):  # type: ignore[no-untyped-def]
            return []

    fetch = FakeFetcher(mapping={})
    budget = BudgetManager(max_replans=1)
    worker = WorkerNode(web_search=EmptyWeb(), fetcher=fetch, budget=budget)
    g = build_graph(worker=worker, budget=budget, min_facts_to_stop_replan=2)
    out = await g.ainvoke({"query": "anything", "research_depth": "quick"})
    # Initial planner for "anything" (no "vs") produces 2 quick tasks.
    # Each produces 0 facts. After first wave, replan adds 1 task.
    # After second wave, replan cap hit -> writer.
    assert out.get("replan_count", 0) == 1
    # All subtasks terminal.
    for t in out["subtasks"]:
        assert t.status in {"completed", "failed"}


@pytest.mark.asyncio
async def test_checkpoint_resume_does_not_re_execute_completed_tasks():  # type: ignore[no-untyped-def]
    """Run the graph with a MemorySaver; stop mid-way; resume; completed tasks
    must not be re-executed (their facts appear once).
    """
    url = "https://example.com/resume"
    calls = {"n": 0}

    class CountingWeb:
        async def search(self, query, max_results=5):  # type: ignore[no-untyped-def]
            calls["n"] += 1
            return [SearchResult(title="x", url=url, snippet="")]

    fetch = FakeFetcher(mapping={url: "Resumable source content here."})
    worker = WorkerNode(web_search=CountingWeb(), fetcher=fetch)
    checkpointer = MemorySaver()
    g = build_graph(worker=worker, checkpointer=checkpointer)

    config = {"configurable": {"thread_id": "test-thread-1"}}
    out1 = await g.ainvoke({"query": "LangGraph", "research_depth": "quick"}, config=config)
    facts1 = len(out1["facts"])
    searches_after_first = calls["n"]

    # Resume on the same thread: should not re-run workers (no new facts).
    out2 = await g.ainvoke(None, config=config)  # type: ignore[arg-type]
    facts2 = len(out2["facts"])
    assert facts1 == facts2, f"resume added facts: {facts1} -> {facts2}"
    # No new searches on resume.
    assert calls["n"] == searches_after_first


@pytest.mark.asyncio
async def test_auth_error_not_retried():  # type: ignore[no-untyped-def]
    class AuthFail:
        async def search(self, query, max_results=5):  # type: ignore[no-untyped-def]
            raise RuntimeError("401 unauthorized")

    worker = WorkerNode(web_search=AuthFail(), fetcher=FakeFetcher(mapping={}))
    g = build_graph(worker=worker)
    out = await g.ainvoke({"query": "x", "research_depth": "quick"})
    # All subtasks should be failed quickly (no retries).
    for t in out["subtasks"]:
        assert t.status == "failed"
    # The directly-failed task_1 should carry the auth error.
    t1 = next(t for t in out["subtasks"] if t.task_id == "task_1")
    assert "401" in t1.error or "auth" in t1.error
