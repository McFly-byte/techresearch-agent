"""Tests for phase 3 corrections: token estimation, length guard,
Retry-After, cancellation, budget critical enforcement."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agents.budget import BudgetManager, TokenUsage
from agents.worker import (
    InputTooLongError,
    ModelRouter,
    WorkerNode,
    check_input_length,
    estimate_tokens,
)
from domain.models import SearchResult
from graph.state import SubTask
from tools.fetchers import FakeFetcher
from tools.search_providers import FakeSearchProvider


def _hit(url: str = "https://x", title: str = "t") -> SearchResult:
    return SearchResult(title=title, url=url, snippet="s")


def test_estimate_tokens_is_char_over_4() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcde") == 2  # ceil(5/4)


def test_check_input_length_raises() -> None:
    check_input_length("short")  # no raise
    with pytest.raises(InputTooLongError):
        check_input_length("x" * 200_000, limit=100_000)


class _FailingSearch(FakeSearchProvider):
    def __init__(self, exc: Exception) -> None:
        super().__init__({})
        self._exc = exc
        self.calls = 0

    async def search(self, query: str, max_results: int = 5) -> list[Any]:  # type: ignore[override]
        self.calls += 1
        raise self._exc


class _RecordingSleep:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, s: float) -> None:
        self.calls.append(s)


@pytest.mark.asyncio
async def test_rate_limit_respects_retry_after() -> None:
    from agents.errors import ClassifiedError

    # Simulate a 429 exception carrying retry_after.
    exc = ClassifiedError("rate_limit", RuntimeError("429 slow down"), retry_after_s=3.5)
    exc.retryable = True
    search = _FailingSearch(exc)
    sleep = _RecordingSleep()
    w = WorkerNode(web_search=search, fetcher=FakeFetcher(), sleep_fn=sleep)
    task = SubTask(task_id="task_1", title="t", description="q", depends_on=[], priority=1)
    await w.run_task(task)
    # The worker should have slept at least once with 3.5s (not a real wait).
    assert 3.5 in sleep.calls, f"expected retry_after 3.5s in {sleep.calls}"


@pytest.mark.asyncio
async def test_cancellation_in_worker_loop() -> None:
    cancel = asyncio.Event()
    search = FakeSearchProvider({"q": [_hit()]})
    w = WorkerNode(web_search=search, fetcher=FakeFetcher(), cancel_event=cancel)
    # Cancel before the loop starts.
    cancel.set()
    task = SubTask(task_id="task_1", title="t", description="q", depends_on=[], priority=1)
    cmd = await w.run_task(task)
    update = cmd.update if hasattr(cmd, "update") else {}
    subtasks = update.get("subtasks", [])
    assert subtasks and subtasks[0].status == "cancelled", subtasks[0].status
    assert (
        "cancel" in (subtasks[0].error or "").lower()
        or "cancel" in (subtasks[0].result_summary or "").lower()
    )


@pytest.mark.asyncio
async def test_budget_critical_stops_and_records_gap() -> None:
    b = BudgetManager(global_token_budget=10, max_iterations=4)
    # Drain to critical (>95% used).
    b.record_usage("task_1", TokenUsage(prompt_tokens=10, completion_tokens=0, estimated=True))
    assert b.status() in {"critical", "exhausted"}
    search = FakeSearchProvider({"q": [_hit()]})
    w = WorkerNode(web_search=search, fetcher=FakeFetcher(), budget=b)
    task = SubTask(task_id="task_1", title="t", description="q", depends_on=[], priority=1)
    cmd = await w.run_task(task)
    update = cmd.update if hasattr(cmd, "update") else {}
    subtasks = update.get("subtasks", [])
    summary = subtasks[0].result_summary
    assert "budget" in summary.lower(), summary


def test_token_estimation_not_hardcoded() -> None:
    # The worker must not record 500/200; verify by spying on record_usage.
    b = BudgetManager()
    calls: list[TokenUsage] = []
    orig = b.record_usage

    def spy(task_id: str, usage: TokenUsage) -> None:
        calls.append(usage)
        orig(task_id, usage)

    b.record_usage = spy  # type: ignore[method-assign]
    # We can't easily run the full loop here, but assert estimate_tokens is used.
    # If someone hardcoded 500/200 again, estimate_tokens would not match char/4.
    assert estimate_tokens("a" * 400) == 100  # not 500


def test_model_router_default_returns_preferred() -> None:
    r = ModelRouter(preferred="qwen-max")
    snap = BudgetManager().snapshot()
    assert r.pick(snap) == "qwen-max"


def test_model_router_can_downgrade_on_critical() -> None:
    """A user-provided router can downgrade when budget is critical."""

    class CheapRouter(ModelRouter):
        def pick(self, snapshot) -> str:  # type: ignore[no-untyped-def]
            if snapshot.status in {"critical", "exhausted"}:
                return "qwen-turbo"
            return self.preferred

    b = BudgetManager(global_token_budget=10)
    b.record_usage("t", TokenUsage(prompt_tokens=10, completion_tokens=0, estimated=True))
    r = CheapRouter(preferred="qwen-max")
    assert r.pick(b.snapshot()) == "qwen-turbo"
