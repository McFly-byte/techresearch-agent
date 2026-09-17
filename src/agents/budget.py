"""BudgetManager: global + per-task token/iteration budget.

Hard-enforced, injectable, concurrency-safe.

Reservation transaction model (stage-3 reservation-semantics fix):

Every new LLM call MUST first ``await reserve(task_id, worst_case_tokens)``.
``reserve`` is ALL-OR-NOTHING: it atomically (``asyncio.Lock``) checks that the
FULL requested worst-case slice fits under BOTH the GLOBAL and the PER-TASK
token budget, and either grants the whole slice as a ``BudgetReservation``
ticket or raises ``BudgetExceededError`` BEFORE the call is made. It never
returns a partial grant — a partial slice would let ``commit`` push real usage
through the hard cap (the exact bug this module fixes). The lock is held only
for the short check-and-reserve, never across the provider call, so LLM calls
stay concurrent.

The caller MUST close the ticket on EVERY exit path:

- success -> ``await ticket.commit(real_usage)``: moves the slice from
  ``reserved`` into ``used`` (clamped to the hard caps), and reconciles the
  reservation. If recording the real usage would breach the global/per-task
  hard cap, the ledger is clamped to the cap, the excess is recorded in
  ``overflow_tokens``, and ``BudgetExceededError`` is raised (never a silent
  overshoot).
- exception / cancellation -> ``await ticket.release()``: frees the slice
  without charging any usage.

A ticket is a one-shot state machine: ``reserved -> committed | released``.
Closing it twice is an explicit idempotent no-op (never double-charges, never
double-frees). Because each reservation carries its own ``reservation_id`` and
token slice, two concurrent reservations for the SAME task release
independently — releasing one never frees the other's tokens.

``record_usage`` remains a synchronous, CLAMPING post-hoc accounting helper
kept for test setups / evals: it never raises on negative input, never lets
``used`` exceed the global cap (truncation), clamps the per-task ledger to its
cap, and records any attempted excess in ``overflow_tokens``. The authoritative
gate is ``reserve``/``commit``.

Hard limits:
- global_token_budget (+ optional per_task_token_budget)
- max_search_rounds per task (search loop cap)
- max_iterations (worker loop cap)
- max_replans (global)
- total token budget (warning / critical / exhausted bands)

Thresholds:
- warning_ratio: flag, keep going
- critical_ratio: stop non-essential work / replans, write evidence gaps
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel

from core.exceptions import BudgetExceededError, ConfigurationError

__all__ = [
    "BudgetManager",
    "BudgetReservation",
    "BudgetSnapshot",
    "TokenUsage",
    "BudgetExceededError",
]


class TokenUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    estimated: bool = False  # True when we had to guess (no usage from provider)

    @property
    def total(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class BudgetReservation:
    """A one-shot ticket to a reserved token slice.

    State machine: ``reserved -> committed | released`` (both terminal). The
    ticket is the ONLY way to reconcile a reserve: ``commit`` records the real
    usage and closes the ticket as ``committed``; ``release`` frees the slice
    without charging usage and closes it as ``released``. Closing a terminal
    ticket is an idempotent no-op (never double-charges, never double-frees).

    It is also an async context manager: on ``__aexit__`` any still-open ticket
    is released, so ``async with await budget.reserve(...) as ticket:`` can never
    leak a reservation even on an unhandled exception.
    """

    reservation_id: str
    task_id: str
    tokens: int
    _manager: BudgetManager = field(repr=False)
    _state: Literal["reserved", "committed", "released"] = "reserved"

    @property
    def state(self) -> str:
        return self._state

    async def commit(self, usage: TokenUsage) -> None:
        """Record real usage for this reservation and close the ticket."""
        await self._manager.commit(self, usage)

    async def release(self) -> None:
        """Free this reservation's slice without charging usage."""
        await self._manager.release(self)

    async def __aenter__(self) -> BudgetReservation:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
        if self._state == "reserved":
            await self.release()
        return False  # never suppress the exception


@dataclass
class BudgetSnapshot:
    global_used_tokens: int
    global_budget_tokens: int
    # New (stage-3 hard boundary): explicit accounting triad.
    used_tokens: int
    reserved_tokens: int
    # Tokens that a post-hoc record/commit attempt tried to charge ABOVE the
    # hard cap (clamped away). Observable, never silently dropped.
    overflow_tokens: int
    remaining_ratio: float
    search_rounds_used: int
    search_rounds_limit: int
    replans_used: int
    replans_limit: int
    status: Literal["ok", "warning", "critical", "exhausted"]
    #: clamped tokens per task_id (never exceeds per_task_token_budget).
    per_task_tokens: dict[str, int]


class BudgetManager:
    def __init__(
        self,
        *,
        global_token_budget: int = 100_000,
        per_task_token_budget: int | None = None,
        max_search_rounds: int = 3,
        max_iterations: int = 4,
        max_replans: int = 2,
        warning_ratio: float = 0.8,
        critical_ratio: float = 0.95,
    ) -> None:
        # --- boundary 2: reject illegal configuration at construction --------
        self._validate_config(
            global_token_budget=global_token_budget,
            per_task_token_budget=per_task_token_budget,
            max_search_rounds=max_search_rounds,
            max_iterations=max_iterations,
            max_replans=max_replans,
            warning_ratio=warning_ratio,
            critical_ratio=critical_ratio,
        )
        self.global_token_budget = global_token_budget
        self.per_task_token_budget = per_task_token_budget
        self.max_search_rounds = max_search_rounds
        self.max_iterations = max_iterations
        self.max_replans = max_replans
        self.warning_ratio = warning_ratio
        self.critical_ratio = critical_ratio

        # --- boundary 4: asyncio.Lock guards check-and-act sequences -----------
        # asyncio.Lock (non-blocking) is used inside async workers; it must NEVER
        # be held across an await on external I/O. Critical sections here only do
        # integer/dict arithmetic, so LLM calls stay concurrent.
        self._lock = asyncio.Lock()
        self._used_tokens = 0
        self._reserved_tokens = 0
        self._overflow_tokens = 0
        self._per_task_used: dict[str, int] = {}
        self._per_task_reserved: dict[str, int] = {}
        # reservation_id -> tokens granted. Each ticket releases ONLY its own
        # slice (no pop-by-task-id that frees all concurrent tickets).
        self._reservations: dict[str, int] = {}
        self._reservation_counter = 0
        self._rounds_per_task: dict[str, int] = {}
        self._replans_used = 0

    # --- configuration validation ----------------------------------------
    @staticmethod
    def _validate_config(**kw: object) -> None:
        def pos_int(name: str, v: object) -> None:
            if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
                raise ConfigurationError(f"{name} must be a positive integer, got {v!r}")

        pos_int("global_token_budget", kw["global_token_budget"])
        pos_int("max_search_rounds", kw["max_search_rounds"])
        pos_int("max_iterations", kw["max_iterations"])
        if (
            not isinstance(kw["max_replans"], int)
            or isinstance(kw["max_replans"], bool)
            or kw["max_replans"] < 0
        ):
            raise ConfigurationError(f"max_replans must be >= 0, got {kw['max_replans']!r}")
        ptb = kw["per_task_token_budget"]
        if ptb is not None and (not isinstance(ptb, int) or isinstance(ptb, bool) or ptb <= 0):
            raise ConfigurationError(
                f"per_task_token_budget must be a positive integer or None, got {ptb!r}"
            )
        w = kw["warning_ratio"]
        c = kw["critical_ratio"]
        if not isinstance(w, (int, float)) or not (0.0 < w < 1.0):
            raise ConfigurationError(f"warning_ratio must be in (0,1), got {w!r}")
        if not isinstance(c, (int, float)) or not (0.0 < c <= 1.0):
            raise ConfigurationError(f"critical_ratio must be in (0,1], got {c!r}")
        if w >= c:
            raise ConfigurationError(
                f"warning_ratio ({w}) must be strictly smaller than critical_ratio ({c})"
            )

    # --- reserve / commit / release (hard, pre-call gate) -----------------
    async def reserve(self, task_id: str, tokens: int) -> BudgetReservation:
        """Atomically reserve the FULL ``tokens`` worst-case slice for one task.

        ALL-OR-NOTHING: the call fits under BOTH the global and the per-task
        budget, or ``BudgetExceededError`` is raised. A partial slice is never
        returned — that would let ``commit`` push real usage through the hard
        cap. The lock is released immediately after the short check-and-reserve;
        the actual provider call runs unlocked so concurrent LLM calls are not
        serialized.
        """
        if tokens < 0:
            tokens = 0
        async with self._lock:
            global_available = self.global_token_budget - self._used_tokens - self._reserved_tokens
            if tokens > global_available:
                raise BudgetExceededError(
                    f"cannot reserve {tokens} tokens: only {max(0, global_available)} "
                    f"of global budget remains"
                )
            if self.per_task_token_budget is not None:
                task_used = self._per_task_used.get(task_id, 0)
                task_reserved = self._per_task_reserved.get(task_id, 0)
                task_available = self.per_task_token_budget - task_used - task_reserved
                if tokens > task_available:
                    raise BudgetExceededError(
                        f"cannot reserve {tokens} tokens for {task_id}: only "
                        f"{max(0, task_available)} of its per-task budget remains"
                    )
            # All-or-nothing: grant the EXACT requested slice.
            granted = tokens
            self._reserved_tokens += granted
            self._per_task_reserved[task_id] = self._per_task_reserved.get(task_id, 0) + granted
            self._reservation_counter += 1
            reservation_id = f"r{self._reservation_counter}_{task_id}"
            self._reservations[reservation_id] = granted
            return BudgetReservation(
                reservation_id=reservation_id,
                task_id=task_id,
                tokens=granted,
                _manager=self,
            )

    async def commit(self, ticket: BudgetReservation, usage: TokenUsage) -> None:
        """Close ``ticket`` as committed: record ``usage`` and reconcile it.

        The ticket's reserved slice is released; the real usage is added to
        ``used`` clamped to the hard caps. If recording the usage would breach
        the global (or per-task) hard cap, the ledger is clamped to the cap,
        the excess is recorded in ``overflow_tokens``, and
        ``BudgetExceededError`` is raised — ``used`` never silently exceeds the
        cap. Closing an already-closed ticket is an idempotent no-op.
        """
        async with self._lock:
            if ticket._state != "reserved":  # noqa: SLF001
                return
            # Release this ticket's own reserved slice (only this ticket).
            granted = self._reservations.pop(ticket.reservation_id, ticket.tokens)
            self._reserved_tokens -= granted
            if self._reserved_tokens < 0:  # defensive
                self._reserved_tokens = 0
            pr = self._per_task_reserved.get(ticket.task_id, 0)
            self._per_task_reserved[ticket.task_id] = pr - granted
            if self._per_task_reserved[ticket.task_id] <= 0:
                self._per_task_reserved.pop(ticket.task_id, None)

            # Record real usage, clamping to the hard caps.
            overflow = self._record_used_locked(ticket.task_id, usage.total)
            ticket._state = "committed"  # noqa: SLF001
            if overflow > 0:
                raise BudgetExceededError(
                    f"committed usage {usage.total} exceeded hard cap by {overflow} tokens"
                )

    async def release(self, ticket: BudgetReservation) -> None:
        """Close ``ticket`` as released: free its slice without charging usage.

        Idempotent: releasing an already-committed/released ticket is a no-op.
        """
        async with self._lock:
            if ticket._state != "reserved":  # noqa: SLF001
                return
            granted = self._reservations.pop(ticket.reservation_id, ticket.tokens)
            self._reserved_tokens -= granted
            if self._reserved_tokens < 0:  # defensive
                self._reserved_tokens = 0
            pr = self._per_task_reserved.get(ticket.task_id, 0)
            self._per_task_reserved[ticket.task_id] = pr - granted
            if self._per_task_reserved[ticket.task_id] <= 0:
                self._per_task_reserved.pop(ticket.task_id, None)
            ticket._state = "released"  # noqa: SLF001

    def _record_used_locked(self, task_id: str, amount: int) -> int:
        """Clamp ``amount`` into the global + per-task ledgers. Returns overflow.

        Caller MUST hold ``self._lock``.
        """
        new_global = self._used_tokens + amount
        overflow = 0
        if new_global > self.global_token_budget:
            overflow = new_global - self.global_token_budget
            self._used_tokens = self.global_token_budget
        else:
            self._used_tokens = new_global
        self._overflow_tokens += overflow

        new_task = self._per_task_used.get(task_id, 0) + amount
        if self.per_task_token_budget is not None and new_task > self.per_task_token_budget:
            self._per_task_used[task_id] = self.per_task_token_budget
        else:
            self._per_task_used[task_id] = new_task
        return overflow

    # --- legacy / setup accounting (clamping, non-raising) ----------------
    def record_usage(self, task_id: str, usage: TokenUsage) -> None:
        """Synchronous post-hoc accounting for tests/evals.

        The hard global cap is enforced by TRUNCATION: ``used`` can never exceed
        ``global_token_budget``; the per-task ledger is likewise clamped to its
        cap. Any attempted excess is accumulated in ``overflow_tokens``
        (observable on the snapshot). Negative usage is rejected (ValueError).
        The authoritative pre-call gate is ``reserve``; this helper never blocks
        a call, it only keeps the books sane.
        """
        if usage.total < 0:
            raise ValueError(f"record_usage tokens must be >= 0, got {usage.total}")
        # record_usage is sync (no internal await) -> atomic under the loop.
        self._record_used_locked(task_id, usage.total)

    def tokens_used(self) -> int:
        return self._used_tokens

    def tokens_reserved(self) -> int:
        return self._reserved_tokens

    def tokens_overflow(self) -> int:
        return self._overflow_tokens

    def tokens_reserved_for_task(self, task_id: str) -> int:
        return self._per_task_reserved.get(task_id, 0)

    # --- per-task token ledger (sync predicates + read accessors) ----------
    def task_tokens_used(self, task_id: str) -> int:
        return self._per_task_used.get(task_id, 0)

    def task_remaining(self, task_id: str) -> int:
        if self.per_task_token_budget is None:
            return self.global_token_budget - self._used_tokens
        return self.per_task_token_budget - self._per_task_used.get(task_id, 0)

    def task_exhausted(self, task_id: str) -> bool:
        if self.per_task_token_budget is None:
            return False
        return self._per_task_used.get(task_id, 0) >= self.per_task_token_budget

    def reserve_task_tokens(self, task_id: str, tokens: int) -> bool:
        """Sync pre-call gate predicate: would ``tokens`` fit this task's allowance?

        Pure check (no mutation). Used at the worker loop-top to refuse a task's
        next LLM call once its per-task hard cap is reached.
        """
        if self.per_task_token_budget is None:
            return True
        if tokens < 0:
            tokens = 0
        return self._per_task_used.get(task_id, 0) + tokens <= self.per_task_token_budget

    def remaining_ratio(self) -> float:
        if self.global_token_budget <= 0:
            return 0.0
        return max(0.0, 1.0 - self._used_tokens / self.global_token_budget)

    def status(self) -> Literal["ok", "warning", "critical", "exhausted"]:
        r = self.remaining_ratio()
        if r <= 0.0:
            return "exhausted"
        if r <= (1.0 - self.critical_ratio):
            return "critical"
        if r <= (1.0 - self.warning_ratio):
            return "warning"
        return "ok"

    # --- per-task rounds --------------------------------------------------
    def begin_round(self, task_id: str) -> bool:
        """Count a search round. Returns False if over limit.

        Synchronous read-modify-write with no internal await, so it is atomic
        under the single-threaded event loop (no coroutine can interleave between
        the read and the write).
        """
        n = self._rounds_per_task.get(task_id, 0)
        if n >= self.max_search_rounds:
            return False
        self._rounds_per_task[task_id] = n + 1
        return True

    def rounds_used(self, task_id: str) -> int:
        return self._rounds_per_task.get(task_id, 0)

    # --- replans ----------------------------------------------------------
    def can_replan(self) -> bool:
        """True only when both the replan COUNT and the remaining token budget
        allow another follow-up wave. Replaning into an exhausted budget is
        forbidden even if the count cap is not yet hit."""
        if self._replans_used >= self.max_replans:
            return False
        return self.remaining_ratio() > 0.0

    def record_replan(self) -> None:
        self._replans_used += 1

    @asynccontextmanager
    async def replan_guard(self) -> AsyncIterator[None]:
        """Hold the budget lock across a check-can-replan -> record_replan
        critical section so concurrent replan requests cannot exceed the cap.

        The caller MUST perform ONLY the synchronous check-and-record inside
        this guard (no I/O, no awaits on external systems): the lock is held
        across ``yield`` and must never be held across an external call.
        """
        async with self._lock:
            yield

    def snapshot(self) -> BudgetSnapshot:
        return BudgetSnapshot(
            global_used_tokens=self._used_tokens,
            global_budget_tokens=self.global_token_budget,
            used_tokens=self._used_tokens,
            reserved_tokens=self._reserved_tokens,
            overflow_tokens=self._overflow_tokens,
            remaining_ratio=self.remaining_ratio(),
            search_rounds_used=max(self._rounds_per_task.values(), default=0),
            search_rounds_limit=self.max_search_rounds,
            replans_used=self._replans_used,
            replans_limit=self.max_replans,
            status=self.status(),
            per_task_tokens=dict(self._per_task_used),
        )
