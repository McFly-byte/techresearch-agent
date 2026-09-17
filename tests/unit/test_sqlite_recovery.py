"""True mid-run interrupt -> resume test with AsyncSqliteSaver.

DETERMINISTIC by construction (the previous flaky version assumed quick-depth
emits 2 sequential tasks; it actually emits 3 PARALLEL tasks and task_1 runs
multiple reflection rounds, so the interrupt point raced):

1. A custom planner emits exactly TWO tasks: task_1 (no deps) then task_2
   (depends on task_1). With ``max_workers=1`` they run STRICTLY SEQUENTIALLY.
2. task_1's search returns 2 independent URLs and fetches content that yields
   >=3 facts, so the pure reflection stops after round 1 -> task_1 issues
   EXACTLY ONE search call.
3. That single search call ARMS an asyncio.Event. The very next search call
   (task_2's first) raises asyncio.CancelledError at a known point.
4. The first ainvoke raises CancelledError. The SQLite checkpoint already has
   task_1 completed.
5. Resume: disarm the event. task_1 must NOT be searched again; task_2
   completes; the final report is non-empty.

No timing sleeps are involved; the interrupt is triggered by an Event armed
deterministically after task_1's provable single search round.
"""

from __future__ import annotations

import asyncio
import os

import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.errors import NodeCancelledError

from agents.budget import BudgetManager
from agents.worker import WorkerNode
from domain.models import SearchResult
from graph.builder import build_graph
from graph.state import ResearchDepth, SubTask
from service.extractor import HeuristicFactExtractor
from tools.fetchers import FakeFetcher


def _hit(url: str) -> SearchResult:
    return SearchResult(title="t", url=url, snippet="s")


# Substantive content so the heuristic extractor emits >=3 facts per doc.
_DOC_A = (
    "Typed state across node boundaries is the core graph invariant. "
    "Checkpoints make long-running work resumable after an unexpected crash."
)
_DOC_B = (
    "The supervisor dispatches pending tasks whose dependencies are met. "
    "Terminal results lock a task so a later retry cannot resurrect it."
)


class _TwoTaskPlanner:
    """Emits task_1 then task_2 (task_2 depends on task_1) -> sequential."""

    name = "two_task_planner"

    async def decompose(
        self,
        query: str,
        *,
        user_context: str = "",
        research_depth: ResearchDepth = "standard",
    ) -> list[SubTask]:
        return [
            SubTask(
                task_id="task_1",
                title="architecture",
                description="foo architecture",
                depends_on=[],
                priority=1,
            ),
            SubTask(
                task_id="task_2",
                title="ecosystem",
                description="foo ecosystem",
                depends_on=["task_1"],
                priority=2,
            ),
        ]


class _ArmedInterruptSearch:
    """Serves task_1's query once, arms an Event, then raises on the next call.

    Because task_1 provably issues exactly one search (reflection reaches
    evidence_sufficient after round 1), the armed interrupt deterministically
    lands on task_2's first search -- no wall-clock timing involved.
    """

    def __init__(self) -> None:
        self.queries_seen: list[str] = []
        self.armed = asyncio.Event()

    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        self.queries_seen.append(query)
        if self.armed.is_set():
            raise asyncio.CancelledError("deterministic mid-run interrupt")
        if "architecture" in query:
            # Serve task_1, then arm the interrupt for the next (task_2) call.
            self.armed.set()
            return [_hit("https://x/1"), _hit("https://x/2")]
        # task_2 query (only reached after disarming on resume).
        return [_hit("https://x/3"), _hit("https://x/4")]


class _CountingExtractor(HeuristicFactExtractor):
    """Wraps the heuristic extractor and records extract() calls PER TASK.

    Used to prove task_1's extraction is NOT repeated on resume (task_2's own
    extract on resume is legitimate and is counted under its own task_id).
    """

    def __init__(self) -> None:
        super().__init__()
        self.extract_calls = 0
        self.calls_by_task: dict[str, int] = {}

    async def extract(self, docs, *, user_context=""):  # type: ignore[no-untyped-def]
        self.extract_calls += 1
        # The worker stamps each fetched doc's citation.source_task_id before
        # calling extract, so we can attribute this call to its owning task.
        for tid in {d.citation.source_task_id for d in docs if d.citation.source_task_id}:
            self.calls_by_task[tid] = self.calls_by_task.get(tid, 0) + 1
        return await super().extract(docs, user_context=user_context)


class _CountingWriter:
    """Report writer that counts render() invocations (must be exactly 1)."""

    name = "counting_writer"

    def __init__(self) -> None:
        self.render_calls = 0

    def render(self, *, query, facts, citations):  # type: ignore[no-untyped-def]
        self.render_calls += 1
        return f"# report ({len(facts)} facts)"


@pytest.mark.asyncio
async def test_true_mid_run_interrupt_then_resume(tmp_path) -> None:  # type: ignore[no-untyped-def]
    db_path = tmp_path / "ckpt.db"
    search = _ArmedInterruptSearch()
    fetcher = FakeFetcher(
        {
            "https://x/1": _DOC_A,
            "https://x/2": _DOC_B,
            "https://x/3": _DOC_A,
            "https://x/4": _DOC_B,
        }
    )
    counting_ext = _CountingExtractor()
    writer = _CountingWriter()

    async with AsyncSqliteSaver.from_conn_string(str(db_path)) as saver:
        budget = BudgetManager()
        worker = WorkerNode(
            web_search=search,
            fetcher=fetcher,
            fact_extractor=counting_ext,
            budget=budget,
        )
        graph = build_graph(
            worker=worker,
            budget=budget,
            planner=_TwoTaskPlanner(),
            report_writer=writer,
            max_workers=1,
            checkpointer=saver,
        )
        cfg = {"configurable": {"thread_id": "t_interrupt_det"}}

        # First run: task_1 completes in one round, task_2's first search raises.
        with pytest.raises((asyncio.CancelledError, NodeCancelledError)):
            await graph.ainvoke(
                {"query": "foo", "research_depth": "quick"},
                config=cfg,
            )

        # SQLite file must exist (checkpoint written despite the interrupt).
        assert os.path.exists(db_path)

        # --- Precise snapshot of task_1's side effects BEFORE resume ----------
        task1_query = search.queries_seen[0]
        assert "architecture" in task1_query
        task1_searches_before = sum(1 for q in search.queries_seen if q == task1_query)
        assert task1_searches_before == 1, search.queries_seen
        # task_1 fetched exactly x/1 and x/2 (2 URLs from its single search).
        task1_fetch_urls_before = [u for u in fetcher.calls if u in {"https://x/1", "https://x/2"}]
        task1_extracts_before = counting_ext.calls_by_task.get("task_1", 0)
        task1_tokens_before = budget.task_tokens_used("task_1")
        # task_1 actually did an extract + was charged for it (non-trivial proof).
        assert task1_extracts_before >= 1, "task_1 should have extracted before interrupt"
        assert task1_tokens_before > 0, "task_1 should have been billed before interrupt"
        # The writer must NOT have run yet (run was interrupted mid-task_2).
        assert writer.render_calls == 0, writer.render_calls

        # Resume: disarm the interrupt so task_2 can now complete.
        search.armed.clear()
        final = await graph.ainvoke(None, config=cfg)

        # --- task_1's work must NOT be repeated on resume --------------------
        task1_searches_after = sum(1 for q in search.queries_seen if q == task1_query)
        assert task1_searches_after == task1_searches_before == 1, (
            f"resume re-executed task_1 search: {search.queries_seen}"
        )
        task1_fetch_urls_after = [u for u in fetcher.calls if u in {"https://x/1", "https://x/2"}]
        assert task1_fetch_urls_after == task1_fetch_urls_before, (
            f"task_1 fetches repeated on resume: {task1_fetch_urls_after} vs {task1_fetch_urls_before}"
        )
        # Per-URL fetch counts for task_1 are exactly preserved.
        for url in ("https://x/1", "https://x/2"):
            assert task1_fetch_urls_after.count(url) == task1_fetch_urls_before.count(url)
        # task_1's extract call count is UNCHANGED (not doubled). task_2 may
        # legitimately extract on resume, counted under its own task_id.
        task1_extracts_after = counting_ext.calls_by_task.get("task_1", 0)
        assert task1_extracts_after == task1_extracts_before, (
            f"task_1 extract repeated on resume: {task1_extracts_after} vs {task1_extracts_before}"
        )
        # task_1's budget used is EXACTLY unchanged (no double-billing).
        assert budget.task_tokens_used("task_1") == task1_tokens_before, (
            f"task_1 billing changed on resume: "
            f"{budget.task_tokens_used('task_1')} vs {task1_tokens_before}"
        )
        # The writer renders EXACTLY once across the whole (interrupted+resumed) run.
        assert writer.render_calls == 1, f"writer ran {writer.render_calls}x, expected exactly 1"
        # The resumed run must have produced a non-empty report.
        assert "report_markdown" in final
        assert final["report_markdown"], "report should be non-empty after resume"


def test_db_written_after_interrupt(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Sanity: checkpoint file exists even after interrupt."""
    db_path = tmp_path / "x.db"

    async def run() -> None:
        async with AsyncSqliteSaver.from_conn_string(str(db_path)):
            pass

    asyncio.run(run())
    assert os.path.exists(db_path)
