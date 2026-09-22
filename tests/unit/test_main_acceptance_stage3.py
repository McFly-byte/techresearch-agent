"""MainAgent stage-3 acceptance tests.

These pin the ten concrete gap findings from the MainAgent audit as
non-regression counter-examples. They exercise the PUBLIC surface (WorkerNode,
BudgetManager, ModelRouter, decide_reflection, build_graph, the LLMFactExtractor
boundary) with recording/mock providers and a fake clock. No real LLM, no real
network, no real sleep.

Each assertion below was previously broken / not enforced; do not weaken them.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from agents.budget import BudgetManager, TokenUsage
from agents.errors import ClassifiedError, backoff_seconds, classify_tool_error
from agents.reflection import decide_reflection
from agents.worker import ModelRouter, WorkerNode
from core.providers.base import BaseLLMProvider, LLMResponse, Message
from domain.models import Citation, SearchResult, SourceDocument, SourceKind
from graph.builder import build_graph
from graph.state import SubTask
from service.extractor import MAX_TOTAL_INPUT_CHARS, LLMFactExtractor
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


class _RecordingSleep:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, s: float) -> None:
        self.calls.append(s)


class RecordingLLM(BaseLLMProvider):
    """Offline LLM that snapshots model_id per call and returns queued replies.

    Supports ``with_model(model_id)``: the worker obtains a per-request clone that
    records under the router-selected model WITHOUT mutating this shared base
    instance. The base ``model_id`` field stays put; the clone pins its own.
    """

    provider_name = "recording"

    def __init__(self, replies: list[tuple[str, int, int]], *, model_id: str = "big") -> None:
        self.model_id = model_id
        self._replies = list(replies)
        #: one entry per acomplete() call: (model_at_call_time, [messages])
        self.calls: list[dict[str, Any]] = []

    def is_configured(self) -> bool:
        return True

    def with_model(self, model_id: str) -> _ScopedRecordingLLM:  # type: ignore[override]
        return _ScopedRecordingLLM(self, model_id)

    async def _record(self, *, model_id: str, messages: list[Message]) -> LLMResponse:
        text, pt, ct = self._replies.pop(0)
        self.calls.append({"model": model_id, "messages": list(messages), "text": text})
        return LLMResponse(
            text=text,
            model=model_id,
            provider=self.provider_name,
            prompt_tokens=pt,
            completion_tokens=ct,
            usage_estimated=False,  # we report "real" usage
        )

    async def acomplete(
        self, messages: list[Message], *, model_id: str | None = None
    ) -> LLMResponse:
        # Request-level override (stage-3 boundary): honor the per-request model
        # without mutating this shared base instance's model_id.
        return await self._record(model_id=model_id or self.model_id, messages=messages)


class _ScopedRecordingLLM(BaseLLMProvider):
    """Per-request clone pinned to its own model_id; shares the base's call log."""

    provider_name = "recording"

    def __init__(self, base: RecordingLLM, model_id: str) -> None:
        self._base = base
        self.model_id = model_id

    def is_configured(self) -> bool:
        return True

    async def acomplete(
        self, messages: list[Message], *, model_id: str | None = None
    ) -> LLMResponse:
        return await self._base._record(model_id=model_id or self.model_id, messages=messages)


def _fact_json(claim: str, cid: str) -> str:
    return json.dumps({"facts": [{"claim": claim, "citation_ids": [cid]}]})


# ---------------------------------------------------------------------------
# Gap 1: ModelRouter is actually wired into the worker / provider routing.
# ---------------------------------------------------------------------------
def test_router_normal_preferred_warning_critical_downgrade() -> None:
    b = BudgetManager(global_token_budget=1000)
    router = ModelRouter(preferred="big", cheap="small")
    # healthy
    assert router.pick(b.snapshot()) == "big"
    # warning: 85% used -> 15% remaining
    b.record_usage("t", TokenUsage(prompt_tokens=850, completion_tokens=0, estimated=True))
    assert b.status() == "warning"
    assert router.pick(b.snapshot()) == "small"
    # exhausted
    b.record_usage("t", TokenUsage(prompt_tokens=160, completion_tokens=0, estimated=True))
    assert b.status() == "exhausted"
    assert router.pick(b.snapshot()) == "small"


@pytest.mark.asyncio
async def test_worker_actually_uses_downgraded_model_at_warning() -> None:
    """At warning budget the worker must ask the router and feed the cheap model
    to the provider (proven by the recording provider's per-call model id)."""
    content = "LangGraph provides typed state and checkpointing for long-running work."
    claim = "LangGraph provides typed state and checkpointing"
    llm = RecordingLLM([(_fact_json(claim, "c_task_1_1"), 100, 50)], model_id="big")
    budget = BudgetManager(global_token_budget=1000, max_iterations=2)
    # Push to warning WITHOUT tripping the worker's critical/exhausted stop.
    budget.record_usage("setup", TokenUsage(prompt_tokens=850, completion_tokens=0, estimated=True))
    router = ModelRouter(preferred="big", cheap="small")

    class _Web:
        async def search(self, query, max_results=5):  # type: ignore[no-untyped-def]
            return [_hit("https://x/1")]

    fetch = FakeFetcher({"https://x/1": content})
    worker = WorkerNode(
        web_search=_Web(),
        fetcher=fetch,
        llm_provider=llm,
        budget=budget,
        router=router,
    )
    task = SubTask(task_id="task_1", title="t", description="q", priority=1)
    await worker.run_task(task)
    # The single extract call must have gone out under the downgraded model.
    assert llm.calls, "extractor should have called the provider"
    assert llm.calls[0]["model"] == "small", llm.calls[0]["model"]


@pytest.mark.asyncio
async def test_worker_uses_preferred_model_when_healthy() -> None:
    content = "LangGraph provides typed state and checkpointing for long-running work."
    claim = "LangGraph provides typed state and checkpointing"
    llm = RecordingLLM([(_fact_json(claim, "c_task_1_1"), 100, 50)] * 4, model_id="big")
    router = ModelRouter(preferred="big", cheap="small")

    class _Web:
        async def search(self, query, max_results=5):  # type: ignore[no-untyped-def]
            return [_hit("https://x/1")]

    fetch = FakeFetcher({"https://x/1": content})
    worker = WorkerNode(web_search=_Web(), fetcher=fetch, llm_provider=llm, router=router)
    task = SubTask(task_id="task_1", title="t", description="q", priority=1)
    await worker.run_task(task)
    assert llm.calls
    assert all(c["model"] == "big" for c in llm.calls), [c["model"] for c in llm.calls]


# ---------------------------------------------------------------------------
# Gap 2: global/per-task budget hard execution; usage not double-counted.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_usage_not_double_counted_across_rounds() -> None:
    """Two extract rounds report (100,50) then (200,100) -> budget total is
    450, not 900 and not re-adding the first round."""
    content = "Deterministic orchestration across rounds is the core guarantee here."
    claim = "Deterministic orchestration across rounds is the core guarantee here."
    llm = RecordingLLM(
        [
            (_fact_json(claim, "c_task_1_1"), 100, 50),
            (_fact_json(claim, "c_task_1_2"), 200, 100),
        ],
    )
    budget = BudgetManager(global_token_budget=100000, max_iterations=2, max_search_rounds=2)

    class _CountingWeb:
        def __init__(self) -> None:
            self.n = 0

        async def search(self, query, max_results=5):  # type: ignore[no-untyped-def]
            self.n += 1
            # one distinct URL per round so they are not deduped
            return [_hit(f"https://x/{self.n}")]

    web = _CountingWeb()
    fetch = FakeFetcher({"https://x/1": content, "https://x/2": content})
    worker = WorkerNode(web_search=web, fetcher=fetch, llm_provider=llm, budget=budget)
    task = SubTask(task_id="task_1", title="t", description="q", priority=1)
    await worker.run_task(task)
    assert len(llm.calls) == 2, llm.calls
    assert budget.tokens_used() == 100 + 50 + 200 + 100, budget.tokens_used()


@pytest.mark.asyncio
async def test_empty_docs_record_no_spurious_usage() -> None:
    """When every fetch fails (no docs fed to extract), the worker must NOT
    estimate/record tokens from the bare query string."""
    budget = BudgetManager(global_token_budget=100000, max_iterations=2, max_search_rounds=2)

    class _Web:
        async def search(self, query, max_results=5):  # type: ignore[no-untyped-def]
            return [_hit("https://bad/1"), _hit("https://bad/2")]

    fetch = FakeFetcher(
        errors={"https://bad/1": "boom1", "https://bad/2": "boom2"},
    )
    worker = WorkerNode(web_search=_Web(), fetcher=fetch, budget=budget)
    task = SubTask(task_id="task_1", title="t", description="q", priority=1)
    await worker.run_task(task)
    assert budget.tokens_used() == 0, (
        f"no LLM call happened, but tokens recorded: {budget.tokens_used()}"
    )


@pytest.mark.asyncio
async def test_budget_critical_worker_stops_and_records_gap() -> None:
    budget = BudgetManager(global_token_budget=10, max_iterations=4)
    budget.record_usage("task_1", TokenUsage(prompt_tokens=10, completion_tokens=0, estimated=True))
    assert budget.status() in {"critical", "exhausted"}

    class _Web:
        async def search(self, query, max_results=5):  # type: ignore[no-untyped-def]
            return [_hit("https://x/1")]

    worker = WorkerNode(web_search=_Web(), fetcher=FakeFetcher(), budget=budget)
    task = SubTask(task_id="task_1", title="t", description="q", priority=1)
    cmd = await worker.run_task(task)
    summary = cmd.update["subtasks"][0].result_summary  # type: ignore[index]
    assert "budget" in summary.lower()
    assert "INFO_GAP" in summary


# ---------------------------------------------------------------------------
# Gap 3: pre-call boundary applies to the ACTUAL sent messages (system+user
# [+assistant+repair]), enforced by the extractor's bounded send entry.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_extractor_actual_outgoing_messages_within_120k() -> None:
    big = "长" * 6000
    docs = [_doc(f"c{i}", big) for i in range(1, 26)]  # 25 docs x 6000 chars
    llm = RecordingLLM([("not-json-trigger-repair", 1, 1)])
    ext = LLMFactExtractor(llm)
    # First call returns garbage -> triggers ONE repair; repair must also fit.
    await ext.extract(docs)
    for call in llm.calls:
        total = sum(len(m.content) for m in call["messages"])
        assert total <= MAX_TOTAL_INPUT_CHARS, f"sent {total} chars > bound {MAX_TOTAL_INPUT_CHARS}"
    # At least the first call was made; repair path exercised by garbage reply.
    assert len(llm.calls) >= 1


@pytest.mark.asyncio
async def test_repair_echo_messages_stay_within_bound() -> None:
    big = "长" * 6000
    docs = [_doc(f"c{i}", big) for i in range(1, 26)]
    # First invalid, repair also invalid -> bounded repair messages observed.
    llm = RecordingLLM([("garbage-one", 1, 1), ("garbage-two", 1, 1)])
    ext = LLMFactExtractor(llm)
    await ext.extract(docs)
    assert len(llm.calls) == 2, "expected first call + one repair call"
    for call in llm.calls:
        total = sum(len(m.content) for m in call["messages"])
        assert total <= MAX_TOTAL_INPUT_CHARS


# ---------------------------------------------------------------------------
# Gap 4: retry categories, Retry-After, injected sleep, max_retries cap.
# ---------------------------------------------------------------------------
class _AlwaysRaise:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc
        self.calls = 0

    async def search(self, query, max_results=5):  # type: ignore[no-untyped-def]
        self.calls += 1
        raise self._exc


@pytest.mark.asyncio
async def test_rate_limit_reads_retry_after_from_exception() -> None:
    exc = ClassifiedError("rate_limit", RuntimeError("429"), retry_after_s=5.0)
    sleep = _RecordingSleep()
    web = _AlwaysRaise(exc)
    budget = BudgetManager(max_iterations=5, max_search_rounds=5)
    worker = WorkerNode(web_search=web, fetcher=FakeFetcher(), sleep_fn=sleep, budget=budget)
    task = SubTask(task_id="task_1", title="t", description="q", priority=1)
    await worker.run_task(task)
    assert 5.0 in sleep.calls, f"Retry-After 5.0s not honored: {sleep.calls}"


@pytest.mark.asyncio
async def test_auth_error_not_retried_no_sleep() -> None:
    web = _AlwaysRaise(RuntimeError("401 unauthorized"))
    sleep = _RecordingSleep()
    worker = WorkerNode(web_search=web, fetcher=FakeFetcher(), sleep_fn=sleep)
    task = SubTask(task_id="task_1", title="t", description="q", priority=1)
    cmd = await worker.run_task(task)
    assert web.calls == 1, f"auth must not retry, but search called {web.calls}x"
    assert sleep.calls == [], f"auth must not sleep, but slept {sleep.calls}"
    assert cmd.update["subtasks"][0].status == "failed"  # type: ignore[index]


@pytest.mark.asyncio
async def test_transient_retry_capped_at_max_retries() -> None:
    from core.exceptions import TransientToolError

    web = _AlwaysRaise(TransientToolError("blip"))
    sleep = _RecordingSleep()
    budget = BudgetManager(max_iterations=5, max_search_rounds=5)
    # max_search_retries=2 -> 1 initial + 2 retries = 3 attempts, then stop.
    worker = WorkerNode(
        web_search=web,
        fetcher=FakeFetcher(),
        sleep_fn=sleep,
        budget=budget,
        max_search_retries=2,
    )
    task = SubTask(task_id="task_1", title="t", description="q", priority=1)
    await worker.run_task(task)
    assert web.calls == 3, f"expected 3 attempts (1+2 retries), got {web.calls}"
    # backoff_seconds is deterministic (no real time); fake clock recorded it.
    assert sleep.calls, "should have slept on the retryable failures"


def test_classify_categories_complete() -> None:
    from core.exceptions import ToolTimeoutError, TransientToolError

    assert classify_tool_error(ToolTimeoutError("x")).category == "timeout"
    assert classify_tool_error(TransientToolError("5xx")).category == "transient_network"
    assert classify_tool_error(RuntimeError("429 slow")).category == "rate_limit"
    assert classify_tool_error(RuntimeError("403 forbidden")).category == "auth_permission"
    assert classify_tool_error(RuntimeError("operation cancelled")).category == "cancelled"
    assert classify_tool_error(RuntimeError("json decode error")).category == "parse_error"
    assert backoff_seconds(0) == 0.5


# ---------------------------------------------------------------------------
# Gap 5: fetch failure records structured provenance and keeps the citation.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_fetch_failure_records_structured_provenance_and_keeps_citation() -> None:
    class _Web:
        async def search(self, query, max_results=5):  # type: ignore[no-untyped-def]
            return [_hit("https://dead/url")]

    fetch = FakeFetcher(errors={"https://dead/url": "boom"})
    budget = BudgetManager(max_iterations=1, max_search_rounds=1)
    worker = WorkerNode(web_search=_Web(), fetcher=fetch, budget=budget)
    task = SubTask(task_id="task_1", title="t", description="q", priority=1)
    cmd = await worker.run_task(task)
    updates = cmd.update  # type: ignore[assignment]
    errs = updates["errors"]
    assert any("fetch_failed" in e and "https://dead/url" in e for e in errs), errs
    assert any("error_type" in e and "attempt=" in e for e in errs), errs
    # The attempted URL is kept as a citation (fetched_ok provenance).
    cites = updates["citations"]
    assert any(c.locator == "https://dead/url" for c in cites), cites


# ---------------------------------------------------------------------------
# Gap 6: cancel propagates into the worker loop; state stays consistent.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_cancel_mid_loop_stops_and_is_consistent() -> None:
    cancel = asyncio.Event()

    class _Web:
        def __init__(self) -> None:
            self.calls = 0

        async def search(self, query, max_results=5):  # type: ignore[no-untyped-def]
            self.calls += 1
            # Arm cancel right after the first search returns; the loop must
            # break at the top of the next iteration instead of searching again.
            cancel.set()
            return [_hit("https://x/1")]

    web = _Web()
    worker = WorkerNode(
        web_search=web,
        fetcher=FakeFetcher({"https://x/1": "some reasonable source body text here."}),
        cancel_event=cancel,
    )
    task = SubTask(task_id="task_1", title="t", description="q", priority=1)
    cmd = await worker.run_task(task)
    sub = cmd.update["subtasks"][0]  # type: ignore[index]
    assert web.calls == 1, f"cancelled loop must not search twice, did {web.calls}"
    # Stage-3 hard boundary 5: cancellation is its own terminal status, not failed.
    assert sub.status == "cancelled", sub.status
    assert "cancel" in (sub.result_summary or "").lower() or "cancel" in (sub.error or "").lower()


# ---------------------------------------------------------------------------
# Gap 7: replan gap structured / dedup / budget / max_replans.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_replan_followup_names_specific_gap() -> None:
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
        config={"configurable": {"thread_id": "gap1"}},
    )
    followups = [
        t for t in out["subtasks"] if t.task_id.startswith("task_") and "follow-up" in t.title
    ]
    assert followups, "expected at least one follow-up after a zero-fact wave"
    desc = followups[0].description
    assert "evidence gap" in desc, desc
    # It must reference a concrete zero-fact perspective, not be generic.
    assert "[" in desc and "]" in desc, desc


@pytest.mark.asyncio
async def test_replan_respects_max_replans_hard_cap() -> None:
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
        config={"configurable": {"thread_id": "gap2"}},
    )
    assert out.get("replan_count", 0) <= 2, out.get("replan_count")
    # No duplicate follow-up descriptions.
    follow_descs = [t.description for t in out["subtasks"] if "follow-up" in t.title]
    assert len(follow_descs) == len(set(follow_descs)), follow_descs


@pytest.mark.asyncio
async def test_replan_skipped_when_budget_exhausted() -> None:
    from langgraph.checkpoint.memory import MemorySaver

    class EmptyWeb:
        async def search(self, query, max_results=5):  # type: ignore[no-untyped-def]
            return []

    budget = BudgetManager(max_replans=2, global_token_budget=100)
    # Drain the whole token budget before the run -> can_replan() is False.
    budget.record_usage("setup", TokenUsage(prompt_tokens=100, completion_tokens=0, estimated=True))
    worker = WorkerNode(web_search=EmptyWeb(), fetcher=FakeFetcher(mapping={}), budget=budget)
    g = build_graph(
        worker=worker,
        budget=budget,
        min_facts_to_stop_replan=2,
        checkpointer=MemorySaver(),
    )
    out = await g.ainvoke(
        {"query": "vector databases", "research_depth": "quick"},
        config={"configurable": {"thread_id": "gap3"}},
    )
    assert out.get("replan_count", 0) == 0, out.get("replan_count")


# ---------------------------------------------------------------------------
# Gap 9: user_context threads planner -> worker search query -> extractor prompt.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_worker_search_query_includes_user_context() -> None:
    seen: list[str] = []

    class _RecordingWeb:
        async def search(self, query, max_results=5):  # type: ignore[no-untyped-def]
            seen.append(query)
            return []

    worker = WorkerNode(web_search=_RecordingWeb(), fetcher=FakeFetcher())
    # planner folded "focus on cost" into the description scope.
    task = SubTask(
        task_id="task_1",
        title="architecture",
        description="vector databases: architecture overview (scope: focus on cost)",
        priority=1,
    )
    await worker.run_task(task)
    assert any("focus on cost" in q for q in seen), seen


@pytest.mark.asyncio
async def test_extractor_prompt_includes_user_context() -> None:
    content = "User context must appear in the outgoing extraction prompt payload."
    llm = RecordingLLM([(_fact_json(content, "c1"), 1, 1)])
    ext = LLMFactExtractor(llm)
    await ext.extract([_doc("c1", content)], user_context="offline-scope-XYZ")
    joined = "\n".join(m.content for call in llm.calls for m in call["messages"])
    assert "offline-scope-XYZ" in joined, joined


@pytest.mark.asyncio
async def test_worker_threads_coverage_scope_into_search_and_extraction() -> None:
    content = "The 2024 market size was 42 billion dollars."
    llm = RecordingLLM([(_fact_json(content, "c_task_1_1"), 10, 5)])
    queries: list[str] = []

    class _RecordingWeb:
        async def search(self, query, max_results=5):  # type: ignore[no-untyped-def]
            queries.append(query)
            return [_hit("https://x/coverage")]

    worker = WorkerNode(
        web_search=_RecordingWeb(),
        fetcher=FakeFetcher({"https://x/coverage": content}),
        llm_provider=llm,
        budget=BudgetManager(max_iterations=1),
    )
    task = SubTask(
        task_id="task_1",
        title="market size",
        description="Coverage requirements:\n- R1: quantify the 2024 market size",
        search_query="2024 market size evidence",
        requirement_ids=["R1"],
        coverage_requirements=["quantify the 2024 market size"],
    )

    await worker.run_task(task)

    assert queries == ["2024 market size evidence"]
    joined = "\n".join(m.content for call in llm.calls for m in call["messages"])
    assert "R1: quantify the 2024 market size" in joined


# ---------------------------------------------------------------------------
# Gap 10: reflection pure function covers all six categories.
# ---------------------------------------------------------------------------
def _base_reflect(**kw: Any) -> dict[str, Any]:
    defaults: dict[str, Any] = dict(
        round_index=0,
        max_iterations=4,
        seen_queries=["q"],
        last_query="q",
        new_results_this_round=3,
        independent_sources_this_round=2,
        total_facts=4,
        covered_questions={"q"},
        expected_questions={"q"},
        prior_failures=0,
        cancel_requested=False,
        budget_ratio_remaining=0.5,
    )
    defaults.update(kw)
    return defaults


def test_reflection_sufficient_category() -> None:
    r = decide_reflection(**_base_reflect())
    assert r.should_stop is True
    assert r.rewrite == "sufficient"


def test_reflection_too_broad_category_rewrite() -> None:
    r = decide_reflection(
        **_base_reflect(
            new_results_this_round=5,
            independent_sources_this_round=2,
            total_facts=1,
            covered_questions=set(),
            expected_questions={"q"},
        )
    )
    assert r.rewrite == "too_broad"
    assert "focus" in r.next_queries[0] or "sub-aspect" in r.next_queries[0]


def test_reflection_too_narrow_category_rewrite() -> None:
    r = decide_reflection(
        **_base_reflect(new_results_this_round=1, total_facts=1, expected_questions={"q", "q2"})
    )
    assert r.rewrite == "too_narrow"
    assert "broaden" in r.next_queries[0]


def test_reflection_off_track_category_rewrite() -> None:
    r = decide_reflection(**_base_reflect(drift_detected=True, total_facts=2))
    assert r.rewrite == "off_track"
    assert "original" in r.next_queries[0]


def test_reflection_conflicting_evidence_category_rewrite() -> None:
    r = decide_reflection(
        **_base_reflect(
            independent_sources_this_round=1,
            total_facts=3,
            covered_questions={"q"},
            expected_questions={"q", "q2"},
        )
    )
    assert r.rewrite == "conflicting_evidence"
    assert "conflicting" in r.next_queries[0]


def test_reflection_empty_result_category_rewrite() -> None:
    r = decide_reflection(**_base_reflect(new_results_this_round=0, total_facts=0))
    assert r.rewrite == "empty_result"
    assert "alternative" in r.next_queries[0]
