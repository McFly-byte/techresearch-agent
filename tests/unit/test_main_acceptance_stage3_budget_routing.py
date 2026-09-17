"""MainAgent stage-3 *final gap* acceptance tests.

These pin the three final gaps the MainAgent reproduced with the PUBLIC
BudgetManager / Worker / provider surface AFTER the 25 stage-3 acceptance tests
already passed. They are hard, non-regression counter-examples: do not delete or
weaken an assertion.

Gaps covered:
1. BudgetManager boundary error: 80% / 95% thresholds were off by an inverted
   remaining-ratio comparison (``<`` instead of ``>=`` on the USED ratio).
2. No per-task token hard budget existed (only global + rounds).
3. Model downgrade mutated the SHARED provider's ``model_id``, racing parallel
   workers. Must use an immutable per-request clone via ``provider.with_model``.

Plus the application-composition wiring: Runner must read budget/router knobs
from Settings instead of ignoring them.

No real LLM, no real network, no real sleep. Overlap is forced with
``asyncio.Barrier`` / ``asyncio.Event``.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from agents.budget import BudgetManager, TokenUsage
from agents.worker import ModelRouter, WorkerNode
from core.providers.base import BaseLLMProvider, LLMResponse, Message
from core.providers.fake import FakeLLM
from core.providers.qwen import QwenProvider
from domain.models import SearchResult
from graph.state import SubTask
from tools.fetchers import FakeFetcher


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _hit(url: str, title: str = "t") -> SearchResult:
    return SearchResult(title=title, url=url, snippet="s")


class _OneHitWeb:
    """Deterministic search: always returns one canned hit."""

    def __init__(self, url: str) -> None:
        self._url = url
        self.calls = 0

    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:  # type: ignore[override]
        self.calls += 1
        return [_hit(self._url)]


class _EmptyFactsLLM(BaseLLMProvider):
    """Returns a valid-but-empty fact list with fixed, reported usage.

    Empty list parses cleanly (no citation/extractive validation needed) while
    still going through the real ``accomplete`` path, so token usage is recorded.
    It honours the per-request ``model_id`` override WITHOUT mutating itself.
    """

    provider_name = "empty"

    def __init__(self, *, prompt_tokens: int = 10, completion_tokens: int = 0) -> None:
        self.model_id = "base"
        self._pt = prompt_tokens
        self._ct = completion_tokens
        self.calls: list[str] = []

    def is_configured(self) -> bool:
        return True

    def with_model(self, model_id: str) -> _Scoped:  # type: ignore[override]
        return _Scoped(self, model_id)

    async def _record(self, model_id: str) -> LLMResponse:
        self.calls.append(model_id)
        return LLMResponse(
            text=json.dumps({"facts": []}),
            model=model_id,
            provider=self.provider_name,
            prompt_tokens=self._pt,
            completion_tokens=self._ct,
            usage_estimated=False,
        )

    async def acomplete(
        self, messages: list[Message], *, model_id: str | None = None
    ) -> LLMResponse:
        return await self._record(model_id or self.model_id)


class _Scoped(BaseLLMProvider):
    """Per-request clone: pins its own model_id, delegates the recording."""

    provider_name = "empty"

    def __init__(self, base: _EmptyFactsLLM, model_id: str) -> None:
        self._base = base
        self.model_id = model_id

    def is_configured(self) -> bool:
        return True

    async def acomplete(
        self, messages: list[Message], *, model_id: str | None = None
    ) -> LLMResponse:
        return await self._base._record(self.model_id)


# ---------------------------------------------------------------------------
# Gap 1: exact 80% / 95% / 100% boundaries on the USED ratio.
# ---------------------------------------------------------------------------
def test_exact_80pct_is_warning() -> None:
    b = BudgetManager(global_token_budget=1000)
    b.record_usage("t", TokenUsage(prompt_tokens=800, completion_tokens=0, estimated=True))
    assert b.status() == "warning", b.status()


def test_exact_95pct_is_critical() -> None:
    b = BudgetManager(global_token_budget=1000)
    b.record_usage("t", TokenUsage(prompt_tokens=950, completion_tokens=0, estimated=True))
    assert b.status() == "critical", b.status()


def test_exact_100pct_is_exhausted() -> None:
    b = BudgetManager(global_token_budget=1000)
    b.record_usage("t", TokenUsage(prompt_tokens=1000, completion_tokens=0, estimated=True))
    assert b.status() == "exhausted", b.status()


def test_799_just_below_warning_is_ok() -> None:
    b = BudgetManager(global_token_budget=1000)
    b.record_usage("t", TokenUsage(prompt_tokens=799, completion_tokens=0, estimated=True))
    assert b.status() == "ok", b.status()


def test_between_warning_and_critical_is_warning_not_critical() -> None:
    b = BudgetManager(global_token_budget=1000)
    b.record_usage("t", TokenUsage(prompt_tokens=900, completion_tokens=0, estimated=True))
    assert b.status() == "warning", b.status()


def test_ratio_constructor_validation() -> None:
    # 0 < warning < critical <= 1
    with pytest.raises(ValueError):
        BudgetManager(warning_ratio=0.95, critical_ratio=0.8)  # warning must be < critical
    with pytest.raises(ValueError):
        BudgetManager(warning_ratio=0.0)  # warning must be > 0
    with pytest.raises(ValueError):
        BudgetManager(critical_ratio=1.5)  # critical must be <= 1
    with pytest.raises(ValueError):
        BudgetManager(warning_ratio=0.8, critical_ratio=0.8)  # must be STRICTLY ordered


def test_budgets_limits_must_be_reasonable() -> None:
    with pytest.raises(ValueError):
        BudgetManager(global_token_budget=-1)
    with pytest.raises(ValueError):
        BudgetManager(global_token_budget=0)
    with pytest.raises(ValueError):
        BudgetManager(max_search_rounds=-1)
    with pytest.raises(ValueError):
        BudgetManager(max_iterations=0)


def test_record_usage_rejects_negative() -> None:
    b = BudgetManager(global_token_budget=1000)
    with pytest.raises(ValueError):
        b.record_usage("t", TokenUsage(prompt_tokens=-5, completion_tokens=0, estimated=True))


# ---------------------------------------------------------------------------
# Gap 2: per-task token budget.
# ---------------------------------------------------------------------------
def test_per_task_tracking_isolated_from_other_tasks() -> None:
    b = BudgetManager(global_token_budget=100_000, per_task_token_budget=100)
    b.record_usage("task_1", TokenUsage(prompt_tokens=60, completion_tokens=0, estimated=True))
    assert b.task_tokens_used("task_1") == 60
    assert b.task_remaining("task_1") == 40
    assert not b.task_exhausted("task_1")
    # reserve gate: 60+30=90 <=100 allowed; 60+50=110 rejected
    assert b.reserve_task_tokens("task_1", 30) is True
    assert b.reserve_task_tokens("task_1", 50) is False
    b.record_usage("task_1", TokenUsage(prompt_tokens=60, completion_tokens=0, estimated=True))
    assert b.task_exhausted("task_1") is True
    # task_2 has its own, independent allowance
    assert b.task_tokens_used("task_2") == 0
    assert not b.task_exhausted("task_2")
    # global still accumulates across tasks (global cap 100_000 is not hit)
    assert b.tokens_used() == 120
    # snapshot carries per-task data clamped to its cap (100), not raw 120
    snap = b.snapshot()
    assert snap.per_task_tokens["task_1"] == 100
    assert snap.per_task_tokens["task_1"] <= 100


@pytest.mark.asyncio
async def test_per_task_budget_exhausts_one_task_others_continue() -> None:
    """One task hitting its per-task hard budget must stop (NOT completed) while
    an unrelated task keeps running on the SHARED budget, and global usage keeps
    accumulating the surviving task's tokens."""
    budget = BudgetManager(
        global_token_budget=100_000,
        per_task_token_budget=1000,
        max_iterations=1,
        max_search_rounds=2,
    )
    # Drain task_1's per-task allowance exactly. It must not be able to make the
    # next extract call; task_2 starts with a fresh per-task allowance.
    budget.record_usage(
        "task_1", TokenUsage(prompt_tokens=1000, completion_tokens=0, estimated=True)
    )
    global_before = budget.tokens_used()

    llm = _EmptyFactsLLM(prompt_tokens=10, completion_tokens=0)
    url = "https://example.com/x"
    fetch = FakeFetcher({url: "Some source body with enough characters to be a real document."})
    worker = WorkerNode(
        web_search=_OneHitWeb(url),
        fetcher=fetch,
        llm_provider=llm,
        budget=budget,
    )

    task1 = SubTask(task_id="task_1", title="t1", description="query one", priority=1)
    cmd1 = await worker.run_task(task1)
    sub1 = cmd1.update["subtasks"][0]  # type: ignore[index]
    # Stopped, NOT completed, with an explicit task_budget_exhausted signal.
    assert sub1.status != "completed", sub1.status
    blob = f"{sub1.result_summary} {sub1.error or ''}"
    assert "task_budget_exhausted" in blob, blob

    task2 = SubTask(task_id="task_2", title="t2", description="query two", priority=2)
    cmd2 = await worker.run_task(task2)
    sub2 = cmd2.update["subtasks"][0]  # type: ignore[index]
    # The unrelated task keeps going and terminates normally (max_iterations).
    assert sub2.status == "completed", sub2.status
    # Global budget still counted task_2's usage on top of task_1's.
    assert budget.tokens_used() > global_before, (budget.tokens_used(), global_before)
    assert budget.task_tokens_used("task_2") > 0


@pytest.mark.asyncio
async def test_fresh_task_id_gets_fresh_per_task_allowance() -> None:
    """A replan follow-up (new task_id) gets a fresh per-task allowance while the
    GLOBAL budget still bounds it via can_replan."""
    b = BudgetManager(global_token_budget=100, per_task_token_budget=50)
    b.record_usage("task_1", TokenUsage(prompt_tokens=100, completion_tokens=0, estimated=True))
    # Global is exhausted -> no replan, regardless of per-task allowance.
    assert b.can_replan() is False
    # A new task id starts with a clean per-task ledger (it would be allowed to
    # begin IF the global budget were not exhausted).
    assert b.task_tokens_used("task_2") == 0
    assert b.task_remaining("task_2") == 50


# ---------------------------------------------------------------------------
# Gap 3: downgrade must not mutate the shared provider's model_id.
# ---------------------------------------------------------------------------
class _RaceProbe(BaseLLMProvider):
    """Base provider SHARED by two parallel workers.

    ``model_id`` starts as ``big`` and must NEVER change. The per-request
    ``model_id`` kwarg decides the model for EACH call without touching the
    shared field. Two in-flight calls rendezvous on an ``asyncio.Barrier`` so
    they overlap deterministically (this is what exposes the mutation race).
    """

    provider_name = "race"

    def __init__(self, barrier: asyncio.Barrier) -> None:
        self.model_id = "big"  # must stay "big" for the whole run
        self._barrier = barrier
        self.calls: list[str] = []

    def is_configured(self) -> bool:
        return True

    def with_model(self, model_id: str) -> _RaceScoped:  # type: ignore[override]
        return _RaceScoped(self, model_id)

    async def acomplete(
        self, messages: list[Message], *, model_id: str | None = None
    ) -> LLMResponse:
        return await self._record(model_id or self.model_id)

    async def _record(self, model_id: str) -> LLMResponse:
        # Force the two in-flight calls to overlap.
        await self._barrier.wait()
        self.calls.append(model_id)
        return LLMResponse(
            text=json.dumps({"facts": []}),
            model=model_id,
            provider=self.provider_name,
            prompt_tokens=5,
            completion_tokens=1,
            usage_estimated=False,
        )


class _RaceScoped(BaseLLMProvider):
    provider_name = "race"

    def __init__(self, base: _RaceProbe, model_id: str) -> None:
        self._base = base
        self.model_id = model_id

    def is_configured(self) -> bool:
        return True

    async def acomplete(
        self, messages: list[Message], *, model_id: str | None = None
    ) -> LLMResponse:
        return await self._base._record(self.model_id)


@pytest.mark.asyncio
async def test_parallel_downgrade_does_not_mutate_shared_provider() -> None:
    barrier = asyncio.Barrier(2)
    base = _RaceProbe(barrier=barrier)

    # Worker A: healthy budget -> router picks "big".
    budget_healthy = BudgetManager(global_token_budget=100_000, max_iterations=1)
    # Worker B: warning budget -> router picks "small".
    budget_warning = BudgetManager(global_token_budget=1000, max_iterations=1)
    budget_warning.record_usage(
        "setup", TokenUsage(prompt_tokens=850, completion_tokens=0, estimated=True)
    )

    url = "https://example.com/race"
    doc = "A source body long enough to be treated as a real fetched document."
    fetcher = FakeFetcher({url: doc})

    worker_healthy = WorkerNode(
        web_search=_OneHitWeb(url),
        fetcher=fetcher,
        llm_provider=base,
        budget=budget_healthy,
        router=ModelRouter(preferred="big", cheap="small"),
    )
    worker_warning = WorkerNode(
        web_search=_OneHitWeb(url),
        fetcher=fetcher,
        llm_provider=base,
        budget=budget_warning,
        router=ModelRouter(preferred="big", cheap="small"),
    )

    task_a = SubTask(task_id="task_1", title="a", description="healthy", priority=1)
    task_b = SubTask(task_id="task_2", title="b", description="warning", priority=2)

    await asyncio.gather(
        worker_healthy.run_task(task_a),
        worker_warning.run_task(task_b),
    )

    # Exactly one big call (healthy) and one small call (warning). The shared
    # log must contain BOTH — the old mutation code collapsed both to "small".
    assert sorted(base.calls) == ["big", "small"], base.calls
    # The shared base provider's model_id must be untouched.
    assert base.model_id == "big", base.model_id


def test_fake_llm_with_model_is_independent_clone() -> None:
    base = FakeLLM(model_id="fake-big")
    scoped = base.with_model("fake-small")
    assert isinstance(scoped, FakeLLM)
    assert scoped.model_id == "fake-small"
    # Base untouched; clones do not alias the base's own field.
    assert base.model_id == "fake-big"


def test_qwen_provider_with_model_shares_client_and_key() -> None:
    shared_client = object()  # marker object identity proves the client is reused
    base = QwenProvider(
        model_id="qwen-big",
        base_url="https://example.com/v1",
        api_key="sk-x",
        client=shared_client,  # type: ignore[arg-type]
    )
    scoped = base.with_model("qwen-fast")
    assert isinstance(scoped, QwenProvider)
    assert scoped.model_id == "qwen-fast"
    assert base.model_id == "qwen-big"
    # The HTTPX client (and key/base_url) are SHARED; only the model differs.
    assert scoped._client is shared_client  # noqa: SLF001
    assert scoped._api_key == base._api_key  # noqa: SLF001
    assert scoped._base_url == base._base_url  # noqa: SLF001


# ---------------------------------------------------------------------------
# Application composition wiring: Runner must read budget/router knobs from
# Settings (previously BudgetManager() ignored all of them).
# ---------------------------------------------------------------------------
def test_runner_build_budget_from_settings() -> None:
    from api.runner import _build_budget
    from core.config import Settings

    s = Settings(
        _env_file=None,
        max_total_tokens=12_345,
        max_search_rounds=7,
        warning_token_ratio=0.8,
        critical_token_ratio=0.95,
        per_task_token_budget=42,
        llm_provider="fake",
    )
    b = _build_budget(s)
    assert b.global_token_budget == 12_345
    assert b.max_search_rounds == 7
    assert b.warning_ratio == 0.8
    assert b.critical_ratio == 0.95
    assert b.per_task_token_budget == 42


def test_runner_wires_router_cheap_from_settings() -> None:
    from api.runner import _build_router
    from core.config import Settings

    s = Settings(_env_file=None, qwen_model="qwen3.8-max", qwen_model_fast="qwen3.8-flash")
    # The provider's own model is the preferred model (in live mode this IS
    # settings.qwen_model); the cheap model must come from settings.
    kit_llm = FakeLLM(model_id="qwen3.8-max")
    router = _build_router(s, kit_llm)
    assert router.preferred == "qwen3.8-max"
    assert router.cheap == "qwen3.8-flash"


__all__: list[str] = []
