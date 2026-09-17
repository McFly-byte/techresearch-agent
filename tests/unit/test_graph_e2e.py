"""Graph-level tests: real parallelism, dependency ordering, failure isolation,
deterministic aggregation, and a fake E2E that produces a multi-section draft.

Concurrency contract for these tests (no ``asyncio.sleep`` to "wait for
parallelism"):

- Real parallelism is proven with ``asyncio.Barrier``. N workers must rendezvous
  on the barrier inside their first fetch; the barrier only releases once all N
  have arrived, which deterministically proves they were concurrently in-flight.
- A dependent task starting *after* its upstream *completed* is proven with
  ``time.monotonic`` start/complete timestamps recorded by a thin WorkerNode
  wrapper, not by comparing start-order strings.
- The graph-level ``max_workers`` cap is proven by measuring ``in_flight`` peak
  under a real compiled graph with a planner that emits N independent tasks.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time

import pytest

from agents.worker import WorkerNode
from core.providers.base import LLMResponse, Message
from domain.models import Citation, SearchResult, SourceDocument
from graph.builder import build_graph
from graph.state import SubTask
from service.extractor import LLMFactExtractor
from tools.fetchers import FakeFetcher
from tools.search_providers import FakeSearchProvider


# ---------------------------------------------------------------------------
# Concurrency-recording fakes
# ---------------------------------------------------------------------------
def _tid_from_citation_id(cid: str) -> str:
    """Reconstruct ``task_1`` from a worker-emitted citation id ``c_task_1_1``."""
    parts = cid.split("_")
    return f"{parts[1]}_{parts[2]}"


class BarrierProbeFetcher(FakeFetcher):
    """Tracks in-flight fetches and rendezvous the FIRST fetch of each task on a
    one-shot ``asyncio.Barrier``.

    The barrier trips exactly once: the first ``parties`` tasks to reach their
    first fetch block until all of them have arrived, then all release. Every
    later fetch (subsequent iterations, or tasks that start after the rendezvous)
    proceeds immediately. This proves real concurrency deterministically without
    any ``asyncio.sleep``: a worker cannot pass through its first fetch until the
    other expected workers are actually inside ``fetch()``.
    """

    def __init__(self, mapping: dict[str, str], *, parties: int) -> None:
        super().__init__(mapping=mapping)
        self._barrier = asyncio.Barrier(parties)
        self._synced: set[str] = set()
        self._synced_done = asyncio.Event()
        self.in_flight = 0
        self.max_in_flight = 0
        self._lock = threading.Lock()

    async def fetch(self, locator: str, *, citation_id: str):  # type: ignore[no-untyped-def]
        tid = _tid_from_citation_id(citation_id)
        # Only the first fetch of a task can join the rendezvous, and only
        # before the one-shot rendezvous has already fired.
        do_sync = tid not in self._synced and not self._synced_done.is_set()
        with self._lock:
            self.in_flight += 1
            if self.in_flight > self.max_in_flight:
                self.max_in_flight = self.in_flight
        try:
            if do_sync:
                await self._barrier.wait()
                with self._lock:
                    self._synced.add(tid)
                self._synced_done.set()
            return await super().fetch(locator, citation_id=citation_id)
        finally:
            with self._lock:
                self.in_flight -= 1


class TimingWorker(WorkerNode):
    """Records monotonic start/complete timestamps per task_id.

    ``started_at[tid]`` is the instant the worker begins ``run_task``;
    ``completed_at[tid]`` is the instant ``run_task`` returns. Comparing them
    across tasks proves a dependent task started strictly after its upstream
    *completed* (not merely started).
    """

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.started_at: dict[str, float] = {}
        self.completed_at: dict[str, float] = {}

    async def run_task(self, task: SubTask, *, user_context: str = ""):  # type: ignore[override]
        self.started_at[task.task_id] = time.monotonic()
        try:
            return await super().run_task(task, user_context=user_context)
        finally:
            self.completed_at[task.task_id] = time.monotonic()


def _make_web(*urls: str) -> FakeSearchProvider:
    # A search provider that returns ALL urls for ANY query (test convenience).
    hits = [SearchResult(title=u, url=u, snippet="") for u in urls]

    class AlwaysHits(FakeSearchProvider):
        async def search(self, query: str, max_results: int = 5):  # type: ignore[override]
            self.calls.append((query, max_results))
            return hits[:max_results]

    return AlwaysHits({})


def _make_mapping(*contents: str) -> dict[str, str]:
    return {
        c: f"Content about {c}. It has multiple sentences here so the heuristic "
        f"extractor can emit at least one fact per document. " * 3
        for c in contents
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_workers_actually_run_in_parallel():  # type: ignore[no-untyped-def]
    urls = [f"https://example.com/{i}" for i in range(6)]
    web = _make_web(*urls)
    # parties=2: the two independent tasks (task_1, task_2) must rendezvous.
    fetch = BarrierProbeFetcher(_make_mapping(*urls), parties=2)
    worker = WorkerNode(web_search=web, fetcher=fetch, max_results=6)
    g = build_graph(worker=worker, max_workers=4)
    async with asyncio.timeout(30):  # never hang on a future barrier regression
        out = await g.ainvoke({"query": "LangGraph vs LlamaIndex", "research_depth": "standard"})
    # The barrier can only release when both independent workers are inside
    # fetch() simultaneously, so max_in_flight must be >= 2 deterministically.
    assert fetch.max_in_flight >= 2, f"expected parallel fetches, got {fetch.max_in_flight}"
    # All three subtasks completed.
    statuses = {t.task_id: t.status for t in out["subtasks"]}
    assert statuses["task_1"] == "completed"
    assert statuses["task_2"] == "completed"
    assert statuses["task_3"] == "completed"


@pytest.mark.asyncio
async def test_dependent_task_starts_only_after_upstream_completes():  # type: ignore[no-untyped-def]
    urls = [f"https://example.com/{i}" for i in range(6)]
    web = _make_web(*urls)
    fetch = BarrierProbeFetcher(_make_mapping(*urls), parties=2)
    worker = TimingWorker(web_search=web, fetcher=fetch, max_results=6)
    g = build_graph(worker=worker, max_workers=3)
    async with asyncio.timeout(30):
        await g.ainvoke({"query": "LangGraph vs LlamaIndex", "research_depth": "standard"})

    # task_3 depends on task_1 and task_2. Prove task_3 STARTED only AFTER both
    # upstream tasks COMPLETED (monotonic timestamps), not just that it started
    # later in some start-order list.
    s1, c1 = worker.started_at["task_1"], worker.completed_at["task_1"]
    s2, c2 = worker.started_at["task_2"], worker.completed_at["task_2"]
    s3 = worker.started_at["task_3"]
    assert s3 > c1, f"task_3 started before task_1 completed: {s3} <= {c1}"
    assert s3 > c2, f"task_3 started before task_2 completed: {s3} <= {c2}"
    # Sanity: upstream tasks actually started and completed in order.
    assert c1 > s1
    assert c2 > s2


@pytest.mark.asyncio
async def test_graph_level_max_workers_hard_cap():  # type: ignore[no-untyped-def]
    """Real compiled graph: max_workers=2, 4 independent tasks.

    The supervisor must never dispatch a 3rd worker while 2 are running. The
    barrier rendezvous in wave 1 proves 2 workers overlapped (parallelism
    actually reaches the cap), and ``max_in_flight <= 2`` proves the hard cap
    held for the entire run.
    """
    from agents.planner import HeuristicPlanner

    class IndependentPlanner(HeuristicPlanner):
        """Emit N fully-independent tasks (no dependencies)."""

        def __init__(self, n: int) -> None:
            self._n = n

        async def decompose(self, query, *, user_context="", research_depth="standard"):  # type: ignore[override]
            return [
                SubTask(
                    task_id=f"task_{i}",
                    title=f"task {i}",
                    description=f"independent task {i}",
                    depends_on=[],
                    priority=i,
                )
                for i in range(1, self._n + 1)
            ]

    urls = [f"https://example.com/cap{i}" for i in range(8)]
    web = _make_web(*urls)
    fetch = BarrierProbeFetcher(_make_mapping(*urls), parties=2)
    worker = WorkerNode(web_search=web, fetcher=fetch, max_results=4)
    g = build_graph(
        worker=worker,
        max_workers=2,
        planner=IndependentPlanner(4),
        min_facts_to_stop_replan=1,
    )
    async with asyncio.timeout(30):
        out = await g.ainvoke({"query": "independent batch", "research_depth": "standard"})

    # Hard cap: at no point did more than 2 workers fetch concurrently.
    assert fetch.max_in_flight <= 2, (
        f"max_workers=2 violated: {fetch.max_in_flight} workers were in-flight"
    )
    # Parallelism actually reached the cap (wave-1 barrier rendezvous).
    assert fetch.max_in_flight >= 2, "expected to observe 2 concurrent workers"
    # All 4 tasks completed.
    statuses = {t.task_id: t.status for t in out["subtasks"]}
    assert len(statuses) >= 4
    for i in range(1, 5):
        assert statuses[f"task_{i}"] == "completed"


@pytest.mark.asyncio
async def test_one_failed_worker_does_not_pollute_others():  # type: ignore[no-untyped-def]
    urls = ["https://example.com/1", "https://example.com/2"]
    hits = [SearchResult(title=u, url=u, snippet="") for u in urls]

    # Make task_2's search always fail.
    class FailingSearch(FakeSearchProvider):
        async def search(self, query: str, max_results: int = 5):  # type: ignore[override]
            self.calls.append((query, max_results))
            if "LlamaIndex" in query:
                raise RuntimeError("boom")
            return hits[:max_results]

    web = FailingSearch({})
    # parties=1: this test is about failure isolation, not parallelism. task_2
    # fails at the search step and never reaches fetch, so a 2-party barrier
    # would deadlock waiting for it. parties=1 means the rendezvous trips
    # immediately and measures in_flight only.
    fetch = BarrierProbeFetcher(_make_mapping(*urls), parties=1)
    worker = WorkerNode(web_search=web, fetcher=fetch, max_results=2)
    g = build_graph(worker=worker, max_workers=3)
    async with asyncio.timeout(30):
        out = await g.ainvoke({"query": "LangGraph vs LlamaIndex", "research_depth": "standard"})
    statuses = {t.task_id: t.status for t in out["subtasks"]}
    # task_2 failed, task_1 succeeded. task_3 depends on task_1+task_2, so it
    # should be marked failed (blocked), not silently skipped.
    assert statuses["task_1"] == "completed"
    assert statuses["task_2"] == "failed"
    assert statuses["task_3"] == "failed"
    # Facts from task_1 still present; no facts from task_2.
    task1_facts = [f for f in out["facts"] if f.source_task_id == "task_1"]
    task2_facts = [f for f in out["facts"] if f.source_task_id == "task_2"]
    assert len(task1_facts) > 0
    assert task2_facts == []


@pytest.mark.asyncio
async def test_empty_plan_finishes_without_workers():  # type: ignore[no-untyped-def]
    # A planner that returns [] must still produce a valid (empty) report.
    from agents.planner import HeuristicPlanner

    class EmptyPlanner(HeuristicPlanner):
        async def decompose(self, query, *, user_context="", research_depth="standard"):  # type: ignore[override]
            return []

    web = _make_web()
    fetch = BarrierProbeFetcher(_make_mapping(), parties=1)
    worker = WorkerNode(web_search=web, fetcher=fetch)
    g = build_graph(worker=worker, planner=EmptyPlanner())
    async with asyncio.timeout(30):
        out = await g.ainvoke({"query": "anything", "research_depth": "quick"})
    # Empty plan + empty web -> replan adds follow-ups but they produce 0 facts.
    # The graph must still terminate (replan cap hit) and produce a valid report.
    assert "未从任何来源" in out["report_markdown"]


@pytest.mark.asyncio
async def test_fake_e2e_produces_multi_section_draft():  # type: ignore[no-untyped-def]
    urls = {
        "https://example.com/lg-arch": "LangGraph models agents as a graph of nodes. It supports typed state and durable checkpoints.",
        "https://example.com/li-arch": "LlamaIndex focuses on retrieval over external data. It offers high-level agents.",
        "https://example.com/eco": "LangGraph is maintained by LangChain. LlamaIndex is backed by a commercial company.",
    }
    web = _make_web(*urls.keys())
    fetch = FakeFetcher(mapping=urls)
    worker = WorkerNode(web_search=web, fetcher=fetch, max_results=5)
    g = build_graph(worker=worker, max_workers=3)
    async with asyncio.timeout(30):
        out = await g.ainvoke({"query": "LangGraph vs LlamaIndex", "research_depth": "standard"})

    # Provenance: every fact carries source_task_id.
    assert out["facts"], "expected facts"
    for f in out["facts"]:
        assert f.source_task_id in {"task_1", "task_2", "task_3", "task_4"}
    # Every citation is tagged.
    for c in out["citations"]:
        assert c.source_task_id

    # Report markdown references citations.
    assert "# 调研报告：LangGraph vs LlamaIndex" in out["report_markdown"]
    for c in out["citations"]:
        assert c.citation_id in out["report_markdown"]


# ---------------------------------------------------------------------------
# Gap 4: extractor call-scoped usage under concurrent shared use
# ---------------------------------------------------------------------------
_SUPPORTED_CLAIM = (
    "The service supports caching and retains data for up to ten days without "
    "manual intervention for most tenants."
)


class _PerCallUsageProvider:
    """Offline provider whose reported usage encodes the call number.

    Call n returns prompt_tokens = 1000 + 100*n and completion_tokens = n.
    A correct call-scoped extractor therefore yields, for each call, a pair that
    satisfies ``prompt_tokens == 1000 + 100*completion_tokens``. If usage were
    leaked across concurrent calls (e.g. incrementally accumulated onto shared
    instance attributes), one call would observe a mixed pair like
    (2300, 3) and fail that identity.
    """

    provider_name = "usage_probe"
    model_id = "usage_probe"

    def __init__(self) -> None:
        self._calls = 0

    async def acomplete(self, messages: list[Message]) -> LLMResponse:
        self._calls += 1
        n = self._calls
        return LLMResponse(
            text=json.dumps({"facts": [{"claim": _SUPPORTED_CLAIM, "citation_ids": ["c1"]}]}),
            model=self.model_id,
            provider=self.provider_name,
            prompt_tokens=1000 + 100 * n,
            completion_tokens=n,
            usage_estimated=False,
        )

    def is_configured(self) -> bool:
        return True


def _doc_for_probe() -> SourceDocument:
    return SourceDocument(
        citation=Citation(
            citation_id="c1",
            kind="web",
            locator="https://example.com/probe",
        ),
        content=_SUPPORTED_CLAIM,
    )


@pytest.mark.asyncio
async def test_shared_extractor_usage_is_call_scoped_under_concurrency():  # type: ignore[no-untyped-def]
    extractor = LLMFactExtractor(_PerCallUsageProvider())
    docs = [_doc_for_probe()]

    async def extract_and_snapshot() -> tuple[int, int, bool]:
        await extractor.extract(docs)
        # Read last_* synchronously immediately after extract() returns. The
        # extractor accumulates usage in per-call locals and commits atomically
        # before returning, so this read observes THIS call's own usage even when
        # another concurrent extract() is in flight.
        return (
            extractor.last_prompt_tokens,
            extractor.last_completion_tokens,
            extractor.last_usage_estimated,
        )

    r1, r2 = await asyncio.gather(extract_and_snapshot(), extract_and_snapshot())

    for prompt_tokens, completion_tokens, estimated in (r1, r2):
        assert not estimated, "provider reported real usage; must not be estimated"
        # Each call's usage must be self-consistent: prompt_tokens uniquely
        # identifies which call produced it, and must not be mixed with the
        # other call's numbers.
        assert prompt_tokens == 1000 + 100 * completion_tokens, (
            f"cross-call usage pollution: prompt={prompt_tokens} completion={completion_tokens}"
        )
    # The two calls must have observed DIFFERENT (self-consistent) usage pairs,
    # proving neither call overwrote the other's snapshot before it was read.
    assert r1 != r2, "concurrent calls should observe distinct per-call usage"
