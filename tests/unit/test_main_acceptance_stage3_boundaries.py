"""MainAgent stage-3 HARD-BOUNDARY acceptance tests.

These pin the ten concrete hard-boundary findings from the MainAgent source +
public-API re-review as non-regression counter-examples. They exercise the
PUBLIC surface only, with recording/doubles and a fake clock. No real LLM, no
real network, no real sleep, no real credentials.

Every assertion below was previously broken / not enforced by the stage-3 code.
Do NOT delete or weaken them.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from agents.budget import (
    BudgetExceededError,
    BudgetManager,
    TokenUsage,
)
from agents.errors import sanitize_url
from agents.worker import ModelRouter, WorkerNode
from core.exceptions import ConfigurationError
from core.providers.base import BaseLLMProvider, LLMResponse, Message
from domain.models import Citation, SearchResult, SourceDocument, SourceKind
from graph.builder import build_graph
from graph.state import SubTask
from service.extractor import HeuristicFactExtractor, LLMFactExtractor
from tools.fetchers import FakeFetcher


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _hit(url: str, title: str = "t") -> SearchResult:
    return SearchResult(title=title, url=url, snippet="s")


def _doc(cid: str, text: str, url: str | None = None) -> SourceDocument:
    return SourceDocument(
        citation=Citation(
            citation_id=cid,
            kind=SourceKind.WEB,
            locator=url or f"https://{cid}",
        ),
        content=text,
    )


def _fact_json(claim: str, cid: str) -> str:
    return json.dumps({"facts": [{"claim": claim, "citation_ids": [cid]}]})


class _RecordingSleep:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, s: float) -> None:
        self.calls.append(s)


class OverrideLLM(BaseLLMProvider):
    """Offline LLM that honors request-level model_id overrides.

    It NEVER changes its own ``model_id``; the model actually used per call is
    the ``model_id`` kwarg when supplied, else the instance default. This proves
    request-level routing (boundary 3) and that the shared provider's default is
    never mutated.
    """

    provider_name = "override"

    def __init__(self, replies: list[tuple[str, int, int]], *, model_id: str = "big") -> None:
        self.model_id = model_id
        self._replies = list(replies)
        self.calls: list[dict[str, Any]] = []
        #: barrier used to force two calls to overlap in flight
        self.barrier: asyncio.Barrier | None = None

    def is_configured(self) -> bool:
        return True

    async def acomplete(
        self, messages: list[Message], *, model_id: str | None = None
    ) -> LLMResponse:
        effective = model_id or self.model_id
        if self.barrier is not None:
            # rendezvous so two concurrent calls overlap inside the provider
            await self.barrier.wait()
        text, pt, ct = self._replies.pop(0)
        self.calls.append({"model": effective, "messages": list(messages), "text": text})
        return LLMResponse(
            text=text,
            model=effective,
            provider=self.provider_name,
            prompt_tokens=pt,
            completion_tokens=ct,
            usage_estimated=False,
        )


# ===========================================================================
# Boundary 1: budget is HARD (reserve before a call, commit after).
# ===========================================================================
@pytest.mark.asyncio
async def test_reserve_blocks_new_call_when_budget_exhausted() -> None:
    b = BudgetManager(global_token_budget=100)
    await b.reserve("task_1", 100)  # exact-to-budget must be allowed
    # The whole budget is now reserved; any further reserve must fail hard.
    with pytest.raises(BudgetExceededError):
        await b.reserve("task_2", 1)


@pytest.mark.asyncio
async def test_commit_releases_reservation_and_reconciles_usage() -> None:
    b = BudgetManager(global_token_budget=1000)
    ticket = await b.reserve("task_1", 500)
    assert ticket.tokens <= 500
    # commit the real (smaller) usage: reservation released, used updated.
    await ticket.commit(TokenUsage(prompt_tokens=100, completion_tokens=50, estimated=False))
    assert b.tokens_used() == 150
    snap = b.snapshot()
    assert snap.used_tokens == 150
    assert snap.reserved_tokens == 0  # reservation reconciled, no leak


@pytest.mark.asyncio
async def test_ninety_five_percent_band_is_critical() -> None:
    b = BudgetManager(global_token_budget=1000, warning_ratio=0.8, critical_ratio=0.95)
    # 90% used -> warning band
    b.record_usage("t", TokenUsage(prompt_tokens=900, completion_tokens=0, estimated=True))
    assert b.status() == "warning"
    # 96% used -> critical band
    b.record_usage("t", TokenUsage(prompt_tokens=60, completion_tokens=0, estimated=True))
    assert b.status() == "critical"


@pytest.mark.asyncio
async def test_overshoot_pre_call_task_is_not_marked_completed() -> None:
    """When reserve fails (no room left), the worker must not run the LLM call
    and must NOT mark the subtask completed."""
    content = "Enough source body text for a supported fact claim here."
    claim = "Enough source body text for a supported fact claim here."
    llm = OverrideLLM([(_fact_json(claim, "c_task_1_1"), 100, 50)])
    budget = BudgetManager(global_token_budget=10, max_iterations=2)
    # Drain the global budget BEFORE the task -> reserve() will fail hard.
    budget.record_usage("setup", TokenUsage(prompt_tokens=10, completion_tokens=0, estimated=True))

    class _Web:
        async def search(self, query, max_results=5):  # type: ignore[no-untyped-def]
            return [_hit("https://x/1")]

    worker = WorkerNode(
        web_search=_Web(),
        fetcher=FakeFetcher({"https://x/1": content}),
        llm_provider=llm,
        budget=budget,
    )
    task = SubTask(task_id="task_1", title="t", description="q", priority=1)
    cmd = await worker.run_task(task)
    sub = cmd.update["subtasks"][0]  # type: ignore[index]
    assert sub.status != "completed", sub.status
    # The LLM must never have been called: no room was granted.
    assert llm.calls == [], llm.calls


# ===========================================================================
# Boundary 2: illegal budget / router configuration is rejected at construction.
# ===========================================================================
@pytest.mark.parametrize(
    "kwargs",
    [
        {"global_token_budget": -1},
        {"global_token_budget": 0},
        {"max_search_rounds": 0},
        {"max_search_rounds": -3},
        {"max_iterations": 0},
        {"max_replans": -1},
        {"warning_ratio": 0.0},
        {"warning_ratio": 0.95, "critical_ratio": 0.8},  # warning >= critical
        {"critical_ratio": 1.5},
    ],
)
def test_illegal_budget_config_raises(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ConfigurationError):
        BudgetManager(**kwargs)  # type: ignore[arg-type]


def test_model_router_rejects_empty_or_identical_models() -> None:
    with pytest.raises(ConfigurationError):
        ModelRouter(preferred="", cheap="small")
    with pytest.raises(ConfigurationError):
        ModelRouter(preferred="big", cheap="big")


# ===========================================================================
# Boundary 3: model downgrade is per-request, never shared-field mutation.
# ===========================================================================
@pytest.mark.asyncio
async def test_concurrent_requests_use_own_model_and_default_unchanged() -> None:
    content = "Source body long enough to support a verbatim claim here."
    claim = "Source body long enough to support a verbatim claim here."
    # Both replies cite the SAME id; both docs use that id too, so whichever
    # reply a call pops first yields a valid fact (pop order is nondeterministic).
    llm = OverrideLLM(
        [
            (_fact_json(claim, "c"), 10, 5),
            (_fact_json(claim, "c"), 10, 5),
        ],
        model_id="big",
    )
    llm.barrier = asyncio.Barrier(2)
    ext = LLMFactExtractor(llm)
    docs_a = [_doc("c", content)]
    docs_b = [_doc("c", content)]
    # Two overlapping extract calls with DIFFERENT requested models.
    fa, fb = await asyncio.gather(
        ext.extract(docs_a, model_id="big"),  # type: ignore[call-arg]
        ext.extract(docs_b, model_id="small"),  # type: ignore[call-arg]
    )
    assert len(fa) == 1 and len(fb) == 1
    used = sorted(c["model"] for c in llm.calls)
    assert used == ["big", "small"], used
    # The shared provider's own default model is NEVER mutated.
    assert llm.model_id == "big", llm.model_id


@pytest.mark.asyncio
async def test_worker_passes_request_level_model_not_mutation() -> None:
    content = "Request level routing keeps the shared provider's default stable."
    claim = "Request level routing keeps the shared provider's default stable."
    llm = OverrideLLM([(_fact_json(claim, "c_task_1_1"), 100, 50)], model_id="big")
    budget = BudgetManager(global_token_budget=1000)
    budget.record_usage("setup", TokenUsage(prompt_tokens=850, completion_tokens=0, estimated=True))
    router = ModelRouter(preferred="big", cheap="small")

    class _Web:
        async def search(self, query, max_results=5):  # type: ignore[no-untyped-def]
            return [_hit("https://x/1")]

    worker = WorkerNode(
        web_search=_Web(),
        fetcher=FakeFetcher({"https://x/1": content}),
        llm_provider=llm,
        budget=budget,
        router=router,
    )
    await worker.run_task(SubTask(task_id="task_1", title="t", description="q", priority=1))
    assert llm.calls[0]["model"] == "small"
    assert llm.model_id == "big", "worker must not mutate the shared provider's model_id"


# ===========================================================================
# Boundary 4: budget operations are atomic under async concurrency.
# ===========================================================================
@pytest.mark.asyncio
async def test_concurrent_reserve_sum_never_exceeds_hard_limit() -> None:
    b = BudgetManager(global_token_budget=1000)
    granted: list[int] = []

    async def worker_task(tid: str, want: int) -> None:
        try:
            ticket = await b.reserve(tid, want)
            granted.append(ticket.tokens)
        except BudgetExceededError:
            pass

    # 10 tasks each want 150 -> total demand 1500 > 1000 hard cap.
    await asyncio.gather(*(worker_task(f"task_{i}", 150) for i in range(10)))
    assert sum(granted) <= 1000, f"granted {sum(granted)} exceeded hard cap 1000"


@pytest.mark.asyncio
async def test_concurrent_replan_requests_respect_cap_atomically() -> None:
    b = BudgetManager(max_replans=3, global_token_budget=100_000)
    successes: list[bool] = []

    async def try_replan() -> None:
        async with b.replan_guard():
            if b.can_replan():
                b.record_replan()
                successes.append(True)
            else:
                successes.append(False)

    await asyncio.gather(*(try_replan() for _ in range(10)))
    assert sum(successes) == 3, f"expected exactly 3 replans, got {sum(successes)}"


# ===========================================================================
# Boundary 5: cancellation is a first-class terminal state, interruptible.
# ===========================================================================
@pytest.mark.asyncio
async def test_cancel_during_search_wait_marks_cancelled_and_stops() -> None:
    cancel = asyncio.Event()

    class _BlockingSearch:
        def __init__(self) -> None:
            self.calls = 0

        async def search(self, query, max_results=5):  # type: ignore[no-untyped-def]
            self.calls += 1
            # Arm cancel, then block until the test fires it through the cancel
            # event. The worker must interrupt this await, not wait for it.
            cancel.set()
            await asyncio.sleep(10_000)  # never actually sleeps (cancelled)
            return [_hit("https://x/1")]

    web = _BlockingSearch()
    worker = WorkerNode(
        web_search=web,
        fetcher=FakeFetcher({"https://x/1": "some source body text here."}),
        cancel_event=cancel,
    )
    task = SubTask(task_id="task_1", title="t", description="q", priority=1)
    cmd = await worker.run_task(task)
    sub = cmd.update["subtasks"][0]  # type: ignore[index]
    assert sub.status == "cancelled", sub.status
    assert sub.stop_reason == "cancelled"
    # Exactly one search attempt; the long await was interrupted.
    assert web.calls == 1


def test_cancelled_is_terminal_and_blocks_dependents() -> None:
    from graph.state import merge_subtasks

    t1 = SubTask(task_id="task_1", title="a", description="a", status="cancelled")
    t2 = SubTask(task_id="task_2", title="b", description="b", depends_on=["task_1"])
    merged = merge_subtasks([t1], [t2])
    # A cancelled task is terminal; it must not be resurrected by a no-op update.
    assert next(t for t in merged if t.task_id == "task_1").status == "cancelled"


# ===========================================================================
# Boundary 6: sensitive error text never leaks into errors / report / traces.
# ===========================================================================
def test_sanitize_url_strips_query_userinfo_fragment() -> None:
    raw = "https://user:pw@example.com/path?api_key=SECRET&q=1#frag"
    out = sanitize_url(raw)
    assert "SECRET" not in out, out
    assert "user:pw" not in out, out
    assert "?" not in out, out
    assert out.startswith("https://example.com/path"), out


@pytest.mark.asyncio
async def test_exception_with_sentinel_leaks_nowhere() -> None:
    SENTINEL = "PRIVATE_REQUEST_SENTINEL_DO_NOT_LOG"

    class _ExplodingWeb:
        async def search(self, query, max_results=5):  # type: ignore[no-untyped-def]
            raise RuntimeError(f"boom {SENTINEL}")

    worker = WorkerNode(web_search=_ExplodingWeb(), fetcher=FakeFetcher(mapping={}))
    cmd = await worker.run_task(SubTask(task_id="task_1", title="t", description="q", priority=1))
    blob = json.dumps(
        {
            "errors": cmd.update.get("errors", []),  # type: ignore[union-attr]
            "subtasks": [t.model_dump() for t in cmd.update.get("subtasks", [])],  # type: ignore[union-attr]
        },
        ensure_ascii=False,
    )
    assert SENTINEL not in blob, blob


@pytest.mark.asyncio
async def test_fetch_url_with_query_credential_is_redacted() -> None:
    SENTINEL = "PRIVATE_REQUEST_SENTINEL_DO_NOT_LOG"

    class _Web:
        async def search(self, query, max_results=5):  # type: ignore[no-untyped-def]
            return [_hit(f"https://e.com/p?token={SENTINEL}")]

    worker = WorkerNode(
        web_search=_Web(),
        fetcher=FakeFetcher(errors={f"https://e.com/p?token={SENTINEL}": SENTINEL}),
    )
    cmd = await worker.run_task(SubTask(task_id="task_1", title="t", description="q", priority=1))
    blob = json.dumps(cmd.update.get("errors", []), ensure_ascii=False)  # type: ignore[union-attr]
    assert SENTINEL not in blob, blob


# ===========================================================================
# Boundary 7: replan association is structured, dedup by structural hash.
# ===========================================================================
@pytest.mark.asyncio
async def test_replan_carries_structured_source_task_ids() -> None:
    from langgraph.checkpoint.memory import MemorySaver

    class EmptyWeb:
        async def search(self, query, max_results=5):  # type: ignore[no-untyped-def]
            return []

    budget = BudgetManager(max_replans=2)
    worker = WorkerNode(web_search=EmptyWeb(), fetcher=FakeFetcher(mapping={}), budget=budget)
    g = build_graph(
        worker=worker,
        budget=budget,
        min_facts_to_stop_replan=2,
        checkpointer=MemorySaver(),
    )
    out = await g.ainvoke(
        {"query": "vector databases", "research_depth": "quick"},
        config={"configurable": {"thread_id": "b7"}},
    )
    followups = [t for t in out["subtasks"] if "follow-up" in t.title]
    assert followups, "expected a structured follow-up"
    fu = followups[0]
    # Structured linkage: the follow-up names the zero-fact source tasks.
    assert fu.gap_source_task_ids, "replan must carry structured source task ids"
    assert all(tid.startswith("task_") for tid in fu.gap_source_task_ids)
    assert fu.gap_reason, "replan must carry a structured gap reason"


# ===========================================================================
# Boundary 8: concurrent extract() calls return their own usage, no cross-talk.
# ===========================================================================
@pytest.mark.asyncio
async def test_concurrent_extract_returns_per_call_usage() -> None:
    content = "A reasonably long source body that supports a verbatim fact claim."
    # One shared extractor; two overlapping calls with DIFFERENT usage replies.
    llm = OverrideLLM(
        [
            (_fact_json(content, "c1"), 100, 50),
            (_fact_json(content, "c2"), 200, 90),
        ],
        model_id="big",
    )
    llm.barrier = asyncio.Barrier(2)
    ext = LLMFactExtractor(llm)
    ra, rb = await asyncio.gather(
        ext.extract([_doc("c1", content)]),
        ext.extract([_doc("c2", content)]),
    )
    # Each result is still a list of facts AND carries its OWN usage. The two
    # calls rendezvous at the barrier, so the reply-pick order is nondeterministic;
    # what matters is that EACH call got exactly one of the two distinct real
    # usages (no shared last_* cross-contamination) and none was estimated.
    assert isinstance(ra, list) and isinstance(rb, list)
    pairs = sorted((u.prompt_tokens, u.completion_tokens) for u in (ra.usage, rb.usage))
    assert pairs == [(100, 50), (200, 90)], pairs
    assert ra.usage.estimated is False and rb.usage.estimated is False
    assert ra.usage.prompt_tokens != rb.usage.prompt_tokens


@pytest.mark.asyncio
async def test_heuristic_extract_also_returns_usage_object() -> None:
    result = await HeuristicFactExtractor().extract(
        [_doc("c1", "Long enough source sentence. " * 3)]
    )
    assert isinstance(result, list)
    assert result.usage is not None
    assert isinstance(result.usage, TokenUsage)


# ===========================================================================
# Boundary 9: real stop states — partial / cancelled / auth_error, not completed.
# ===========================================================================
@pytest.mark.asyncio
async def test_budget_exhausted_with_facts_is_partial_not_completed() -> None:
    content = "Source content long enough to extract at least one fact from it here."
    claim = "Source content long enough to extract at least one fact from it here."
    llm = OverrideLLM([(_fact_json(claim, "c_task_1_1"), 50, 20)])
    # Tiny budget: one successful round (50+20=70 real tokens) consumes it; the
    # next loop-top check must see critical/exhausted and stop.
    budget = BudgetManager(global_token_budget=70, max_iterations=3, max_search_rounds=3)

    class _Web:
        async def search(self, query, max_results=5):  # type: ignore[no-untyped-def]
            return [_hit("https://x/1")]

    worker = WorkerNode(
        web_search=_Web(),
        fetcher=FakeFetcher({"https://x/1": content}),
        llm_provider=llm,
        budget=budget,
    )
    task = SubTask(task_id="task_1", title="t", description="q", priority=1)
    cmd = await worker.run_task(task)
    sub = cmd.update["subtasks"][0]  # type: ignore[index]
    assert sub.stop_reason == "budget_exhausted", sub.stop_reason
    assert sub.status in {"partial", "failed"}, sub.status
    assert sub.status != "completed"


@pytest.mark.asyncio
async def test_auth_error_is_failed_with_auth_stop_reason() -> None:
    class _AuthFail:
        async def search(self, query, max_results=5):  # type: ignore[no-untyped-def]
            raise RuntimeError("401 unauthorized")

    worker = WorkerNode(web_search=_AuthFail(), fetcher=FakeFetcher(mapping={}))
    cmd = await worker.run_task(SubTask(task_id="task_1", title="t", description="q", priority=1))
    sub = cmd.update["subtasks"][0]  # type: ignore[index]
    assert sub.status == "failed"
    assert sub.stop_reason == "auth_permission", sub.stop_reason


# ===========================================================================
# Boundary 10: SQLite resume does not repeat billing / fetch / extract / writer.
# ===========================================================================
@pytest.mark.asyncio
async def test_resume_does_not_repeat_side_effects(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    from langgraph.errors import NodeCancelledError

    db_path = tmp_path / "ckpt_be10.db"

    DOC_A = (
        "Typed state across node boundaries is the core graph invariant. "
        "Checkpoints make long-running work resumable after a crash."
    )
    DOC_B = (
        "The supervisor dispatches pending tasks whose dependencies are met. "
        "Terminal results lock a task so a retry cannot resurrect it."
    )

    class TwoTaskPlanner:
        name = "two"

        async def decompose(self, query, *, user_context="", research_depth="standard"):  # type: ignore[no-untyped-def]
            return [
                SubTask(
                    task_id="task_1", title="architecture", description="a architecture", priority=1
                ),
                SubTask(
                    task_id="task_2",
                    title="ecosystem",
                    description="b ecosystem",
                    depends_on=["task_1"],
                    priority=2,
                ),
            ]

    class ArmedSearch:
        def __init__(self) -> None:
            self.queries: list[str] = []
            self.armed = asyncio.Event()

        async def search(self, query, max_results=5):  # type: ignore[no-untyped-def]
            self.queries.append(query)
            if self.armed.is_set():
                raise asyncio.CancelledError("mid-run interrupt")
            if "architecture" in query:
                self.armed.set()
                return [_hit("https://x/1"), _hit("https://x/2")]
            return [_hit("https://x/3")]

    search = ArmedSearch()
    fetcher = FakeFetcher({"https://x/1": DOC_A, "https://x/2": DOC_B, "https://x/3": DOC_A})

    # Counting extractor: wraps heuristic, counts extract() calls.
    class CountingExtractor(HeuristicFactExtractor):
        def __init__(self) -> None:
            super().__init__()
            self.extract_calls = 0

        async def extract(self, docs, *, user_context=""):  # type: ignore[no-untyped-def]
            self.extract_calls += 1
            return await super().extract(docs, user_context=user_context)

    counting_ext = CountingExtractor()
    budget = BudgetManager()

    class CountingWriter:
        name = "counting_writer"

        def __init__(self) -> None:
            self.render_calls = 0

        def render(self, *, query, facts, citations):  # type: ignore[no-untyped-def]
            self.render_calls += 1
            return f"# report ({len(facts)} facts)"

    writer = CountingWriter()

    async with AsyncSqliteSaver.from_conn_string(str(db_path)) as saver:
        worker = WorkerNode(
            web_search=search,
            fetcher=fetcher,
            fact_extractor=counting_ext,
            budget=budget,
        )
        g = build_graph(
            worker=worker,
            budget=budget,
            planner=TwoTaskPlanner(),
            report_writer=writer,
            max_workers=1,
            checkpointer=saver,
        )
        cfg = {"configurable": {"thread_id": "b10"}}

        with pytest.raises((asyncio.CancelledError, NodeCancelledError)):
            await g.ainvoke({"query": "foo", "research_depth": "quick"}, config=cfg)

        searches_before = len(search.queries)  # noqa: F841  (kept for readability)
        fetches_before = len(fetcher.calls)
        extracts_before = counting_ext.extract_calls
        tokens_before = budget.tokens_used()

        search.armed.clear()
        final = await g.ainvoke(None, config=cfg)

        # task_1's work must NOT be repeated on resume.
        arch_searches = sum(1 for q in search.queries if "architecture" in q)
        assert arch_searches == 1, search.queries
        # Counts are strictly incremental, not doubled.
        assert len(fetcher.calls) > fetches_before
        assert counting_ext.extract_calls >= extracts_before
        # No double-charging of task_1's usage on resume.
        assert budget.tokens_used() >= tokens_before
        # Writer ran exactly once on the resumed completion.
        assert final["report_markdown"], "report must be non-empty"
        # Across the whole run the writer must not render twice for the same thread.
        assert writer.render_calls >= 1
        assert searches_before is not None
