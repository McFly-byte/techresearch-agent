"""MainAgent stage-3 RESERVATION transaction-semantics acceptance tests.

These pin the concrete reservation transaction-semantics gaps the MainAgent
reproduced with the PUBLIC ``BudgetManager`` / ``WorkerNode`` surface AFTER the
25 stage-3 + 16 routing + 29 boundary tests already passed. They are hard,
non-regression counter-examples: do not delete or weaken an assertion.

Gaps covered (transactional reservation semantics):
1. Reserve must be ALL-OR-NOTHING: requesting 50 when only 10 remains must
   raise ``BudgetExceededError`` — never return a partial grant of 10 that
   later lets ``commit`` push ``used`` through the hard cap.
2. ``BudgetReservation`` ticket with a strict reserved -> committed/released
   state machine. Exception / cancellation mid-extract MUST release the
   reservation (no permanent ``reserved`` leak starves other tasks).
3. ``record_usage`` never manufactures ``used > cap``: it clamps and reports an
   explicit ``overflow`` instead of silently overshooting (or accumulating raw
   per-task beyond the cap).
4. Precise boundaries + async concurrency: exact fit is legal, one token over
   is refused; concurrent reservations sum to <= the hard cap and each ticket
   releases independently.
5. Worker must check the reserve return value and release on EVERY exit
   (extract exception, cancel mid-extract, parse/fallback exception).

No real LLM, no real network, no real sleep. Overlap is forced with
``asyncio.Event`` / ``asyncio.Barrier``.
"""

from __future__ import annotations

import asyncio

import pytest

from agents.budget import BudgetExceededError, BudgetManager, BudgetReservation, TokenUsage
from agents.worker import WorkerNode
from domain.models import Citation, SearchResult, SourceDocument, SourceKind
from graph.state import SubTask
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


def _u(prompt: int, completion: int = 0) -> TokenUsage:
    return TokenUsage(prompt_tokens=prompt, completion_tokens=completion, estimated=True)


# ===========================================================================
# Gap 1: reserve is ALL-OR-NOTHING (no partial grant that breaks the hard cap).
# ===========================================================================
@pytest.mark.asyncio
async def test_reserve_refused_when_only_partial_room_remains() -> None:
    """used=90 of a 100 budget: reserve(11) must raise — never hand back 10."""
    b = BudgetManager(global_token_budget=100)
    b.record_usage("setup", _u(90))
    assert b.tokens_used() == 90
    # Only 10 room remains. Asking for 11 must be refused outright.
    with pytest.raises(BudgetExceededError):
        await b.reserve("task_1", 11)
    # And nothing was reserved by the failed call.
    assert b.tokens_reserved() == 0


@pytest.mark.asyncio
async def test_reserve_exact_fit_is_legal() -> None:
    """used=90 of 100: reserve(10) must be granted exactly (all-or-nothing)."""
    b = BudgetManager(global_token_budget=100)
    b.record_usage("setup", _u(90))
    ticket = await b.reserve("task_1", 10)
    assert isinstance(ticket, BudgetReservation)
    assert ticket.tokens == 10  # full amount granted, no partial
    assert b.tokens_reserved() == 10


@pytest.mark.asyncio
async def test_commit_rejects_usage_over_reservation_and_never_breaks_cap() -> None:
    """used=90/reserve(10): commit(usage=11) must be refused and ``used`` must
    never exceed 100 (clamped + overflow, never silent overshoot)."""
    b = BudgetManager(global_token_budget=100)
    b.record_usage("setup", _u(90))
    ticket = await b.reserve("task_1", 10)
    with pytest.raises(BudgetExceededError):
        await ticket.commit(_u(11))
    # Hard cap respected: used is clamped at 100, never 101.
    assert b.tokens_used() <= 100
    # The reservation was reconciled (released) on the rejected commit.
    assert b.tokens_reserved() == 0
    # Overflow is observable, not silent.
    assert b.snapshot().overflow_tokens >= 1


@pytest.mark.asyncio
async def test_commit_exact_usage_reconciles_cleanly() -> None:
    b = BudgetManager(global_token_budget=100)
    b.record_usage("setup", _u(90))
    ticket = await b.reserve("task_1", 10)
    await ticket.commit(_u(10))
    assert b.tokens_used() == 100
    assert b.tokens_reserved() == 0
    assert b.status() == "exhausted"


@pytest.mark.asyncio
async def test_reserve_never_returns_partial_when_global_cap_binds() -> None:
    """The old partial-grant bug: request 50, only 10 room -> must raise."""
    b = BudgetManager(global_token_budget=100)
    b.record_usage("setup", _u(90))
    with pytest.raises(BudgetExceededError):
        await b.reserve("task_1", 50)
    # No partial token slice leaked.
    assert b.tokens_reserved() == 0


# ===========================================================================
# Gap 2: BudgetReservation ticket state machine + no leak on cancel/exception.
# ===========================================================================
@pytest.mark.asyncio
async def test_ticket_state_machine_reserved_then_committed_is_terminal() -> None:
    b = BudgetManager(global_token_budget=100)
    ticket = await b.reserve("task_1", 40)
    assert ticket.state == "reserved"
    assert ticket.reservation_id
    await ticket.commit(_u(40))
    assert ticket.state == "committed"
    # Terminal: a second commit is a no-op (idempotent), never raises.
    await ticket.commit(_u(1))
    # A release after commit is also a no-op.
    await ticket.release()
    assert b.tokens_reserved() == 0


@pytest.mark.asyncio
async def test_ticket_release_records_no_usage() -> None:
    b = BudgetManager(global_token_budget=100)
    ticket = await b.reserve("task_1", 40)
    assert b.tokens_reserved() == 40
    await ticket.release()
    assert ticket.state == "released"
    assert b.tokens_reserved() == 0
    assert b.tokens_used() == 0  # nothing charged


@pytest.mark.asyncio
async def test_concurrent_reservations_same_task_release_independently() -> None:
    """Two concurrent reservations for the SAME task must not release each
    other's tokens (the old ``_per_task_reserved.pop(task_id)`` bug)."""
    b = BudgetManager(global_token_budget=100)
    t1 = await b.reserve("task_1", 30)
    t2 = await b.reserve("task_1", 30)
    assert b.tokens_reserved() == 60
    # Release the FIRST ticket only; the second must stay reserved.
    await t1.release()
    assert b.tokens_reserved() == 30
    assert b.tokens_reserved_for_task("task_1") == 30
    await t2.release()
    assert b.tokens_reserved() == 0


@pytest.mark.asyncio
async def test_provider_exception_releases_reservation() -> None:
    """Worker: extract raises -> the reservation must be released, no leak."""
    content = "Source body text long enough to be a real fetched document here."

    class _RaisingExtractor:
        def __init__(self) -> None:
            self.calls = 0

        async def extract(self, docs, *, user_context="", model_id=None, llm=None):  # type: ignore[no-untyped-def]
            self.calls += 1
            raise RuntimeError("provider exploded mid-extract")

    budget = BudgetManager(global_token_budget=10_000, max_iterations=1, max_search_rounds=1)
    extractor = _RaisingExtractor()

    class _Web:
        async def search(self, query, max_results=5):  # type: ignore[no-untyped-def]
            return [_hit("https://x/1")]

    worker = WorkerNode(
        web_search=_Web(),
        fetcher=FakeFetcher({"https://x/1": content}),
        fact_extractor=extractor,
        budget=budget,
    )
    await worker.run_task(SubTask(task_id="task_1", title="t", description="q", priority=1))
    assert extractor.calls == 1
    # The reservation MUST be released despite the exception.
    assert budget.tokens_reserved() == 0, f"reserved leaked: {budget.tokens_reserved()}"
    # And a subsequent task can reserve (no starvation).
    t = await budget.reserve("task_2", 10)
    await t.release()


@pytest.mark.asyncio
async def test_cancel_during_extract_releases_reservation() -> None:
    """Worker: cancel fires mid-extract -> reservation released, not starved."""
    cancel = asyncio.Event()
    content = "Source body text long enough to be a real fetched document here."

    class _CancelOnEntryExtractor:
        def __init__(self) -> None:
            self.calls = 0

        async def extract(self, docs, *, user_context="", model_id=None, llm=None):  # type: ignore[no-untyped-def]
            self.calls += 1
            cancel.set()
            await asyncio.sleep(10_000)  # interrupted by the cancel event

    budget = BudgetManager(global_token_budget=10_000, max_iterations=2, max_search_rounds=2)
    extractor = _CancelOnEntryExtractor()

    class _Web:
        async def search(self, query, max_results=5):  # type: ignore[no-untyped-def]
            return [_hit("https://x/1")]

    worker = WorkerNode(
        web_search=_Web(),
        fetcher=FakeFetcher({"https://x/1": content}),
        fact_extractor=extractor,
        budget=budget,
        cancel_event=cancel,
    )
    cmd = await worker.run_task(SubTask(task_id="task_1", title="t", description="q", priority=1))
    sub = cmd.update["subtasks"][0]  # type: ignore[index]
    assert sub.status == "cancelled", sub.status
    assert budget.tokens_reserved() == 0, f"reserved leaked on cancel: {budget.tokens_reserved()}"
    # Subsequent task can still reserve.
    t = await budget.reserve("task_2", 10)
    await t.release()


@pytest.mark.asyncio
async def test_fallback_exception_releases_reservation() -> None:
    """Worker: extractor raises on BOTH the primary and the TypeError-fallback
    path -> reservation still released."""
    content = "Source body text long enough to be a real fetched document here."

    class _AlwaysRaising:
        def __init__(self) -> None:
            self.calls = 0

        async def extract(self, docs, *, user_context=""):  # type: ignore[no-untyped-def]
            self.calls += 1
            raise ValueError("parse/fallback blew up")

    budget = BudgetManager(global_token_budget=10_000, max_iterations=1, max_search_rounds=1)
    extractor = _AlwaysRaising()

    class _Web:
        async def search(self, query, max_results=5):  # type: ignore[no-untyped-def]
            return [_hit("https://x/1")]

    worker = WorkerNode(
        web_search=_Web(),
        fetcher=FakeFetcher({"https://x/1": content}),
        fact_extractor=extractor,
        budget=budget,
    )
    await worker.run_task(SubTask(task_id="task_1", title="t", description="q", priority=1))
    assert budget.tokens_reserved() == 0, (
        f"reserved leaked on fallback error: {budget.tokens_reserved()}"
    )


# ===========================================================================
# Gap 3: record_usage clamps + reports overflow (never manufactures used>cap).
# ===========================================================================
def test_record_usage_clamps_to_global_cap_and_reports_overflow() -> None:
    b = BudgetManager(global_token_budget=100)
    b.record_usage("task_1", _u(80))
    b.record_usage("task_1", _u(40))  # would push to 120
    # Clamped to the hard cap, never 120.
    assert b.tokens_used() == 100
    snap = b.snapshot()
    assert snap.used_tokens <= snap.global_budget_tokens
    # The attempted overflow is observable (not silently dropped).
    assert snap.overflow_tokens == 20


def test_record_usage_clamps_per_task_to_cap() -> None:
    b = BudgetManager(global_token_budget=100_000, per_task_token_budget=50)
    b.record_usage("task_1", _u(60))
    # Per-task ledger clamped to its cap, never raw 60.
    assert b.task_tokens_used("task_1") == 50
    assert b.task_exhausted("task_1") is True
    snap = b.snapshot()
    assert snap.per_task_tokens["task_1"] <= 50


# ===========================================================================
# Gap 4: precise boundaries + async concurrency safety.
# ===========================================================================
@pytest.mark.asyncio
async def test_exact_global_boundary_is_legal_one_over_refused() -> None:
    b = BudgetManager(global_token_budget=100)
    b.record_usage("setup", _u(90))
    t = await b.reserve("task_1", 10)  # exact fit
    await t.commit(_u(10))
    assert b.tokens_used() == 100
    assert b.status() == "exhausted"
    # One more token is refused.
    with pytest.raises(BudgetExceededError):
        await b.reserve("task_2", 1)


@pytest.mark.asyncio
async def test_per_task_precise_boundary() -> None:
    b = BudgetManager(global_token_budget=100_000, per_task_token_budget=100)
    b.record_usage("task_1", _u(90))
    t = await b.reserve("task_1", 10)
    await t.commit(_u(10))
    assert b.task_tokens_used("task_1") == 100
    with pytest.raises(BudgetExceededError):
        await b.reserve("task_1", 1)


@pytest.mark.asyncio
async def test_concurrent_reservations_sum_within_cap_and_each_releases() -> None:
    b = BudgetManager(global_token_budget=100)
    tickets: list[BudgetReservation] = []

    async def grab(tid: str) -> None:
        try:
            t = await b.reserve(tid, 10)
            tickets.append(t)
        except BudgetExceededError:
            pass

    await asyncio.gather(*(grab(f"task_{i}") for i in range(10)))
    # Total granted never exceeds the 100 hard cap.
    granted_total = sum(t.tokens for t in tickets)
    assert granted_total <= 100, granted_total
    assert b.tokens_reserved() == granted_total
    # Release every ticket independently; reserved returns to exactly 0.
    await asyncio.gather(*(t.release() for t in tickets))
    assert b.tokens_reserved() == 0
    # And another reservation succeeds after the full release.
    again = await b.reserve("task_again", 50)
    await again.release()


# ===========================================================================
# Gap 5: worker actually consumes the ticket and never ignores reserve.
# ===========================================================================
@pytest.mark.asyncio
async def test_worker_reserve_failure_never_calls_provider() -> None:
    """When reserve raises (no room), the worker must NOT call the extractor."""
    content = "Source body long enough to support a verbatim fact claim here."

    class _CountingExtractor:
        def __init__(self) -> None:
            self.calls = 0

        async def extract(self, docs, *, user_context="", model_id=None, llm=None):  # type: ignore[no-untyped-def]
            self.calls += 1
            return []

    budget = BudgetManager(global_token_budget=10, max_iterations=2)
    budget.record_usage("setup", _u(10))  # drain the whole budget
    extractor = _CountingExtractor()

    class _Web:
        async def search(self, query, max_results=5):  # type: ignore[no-untyped-def]
            return [_hit("https://x/1")]

    worker = WorkerNode(
        web_search=_Web(),
        fetcher=FakeFetcher({"https://x/1": content}),
        fact_extractor=extractor,
        budget=budget,
    )
    cmd = await worker.run_task(SubTask(task_id="task_1", title="t", description="q", priority=1))
    sub = cmd.update["subtasks"][0]  # type: ignore[index]
    assert sub.status != "completed", sub.status
    assert extractor.calls == 0, "extractor must not run when reserve refused"
