"""Worker node: runs one SubTask through a bounded loop.

Phase 3: plan query -> search -> fetch -> extract -> reflect -> continue/stop.

Hard caps (from BudgetManager):
- per-task search rounds
- per-task worker iterations (max_iterations)
- global token budget (warning / critical / exhausted)

Stage-3 hard boundaries:
- HARD budget: every LLM call first ``await budget.reserve(...)`` an ALL-OR-NOTHING
  worst-case token slice, returned as a ``BudgetReservation`` ticket. The actual
  usage is ``await ticket.commit(...)`` after the call; any exception or cancel
  path closes it with ``await ticket.release()`` so no reservation ever leaks.
  A new call is REFUSED (BudgetExceededError) when there is no room — the task is
  never marked completed in that case.
- Per-request model routing: the chosen ``model_id`` is forwarded to the
  extractor/provider as a request override. The shared provider's own
  ``model_id`` is NEVER mutated.
- Cancellation is interruptible mid-await (search / fetch / extract) and maps to
  a distinct ``cancelled`` terminal status.
- Sensitive hygiene: raw exception text and credential-bearing URLs never reach
  ``errors[]`` / the subtask error / traces (see agents.errors.sanitize_url).
- Structured stop states: completed / failed / cancelled / partial, with a
  ``stop_reason`` recorded on the subtask.

The reflection step is a PURE decision over observable signals (see
``agents.reflection.decide_reflection``), not an LLM "is it enough" vote.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
from collections.abc import Awaitable, Callable
from typing import Any

from langgraph.types import Command

from agents.budget import BudgetExceededError, BudgetManager, BudgetSnapshot, TokenUsage
from agents.errors import (
    backoff_seconds,
    classify_tool_error,
    sanitize_url,
)
from agents.reflection import decide_reflection
from core.exceptions import ConfigurationError
from core.tracing import TracingContext, noop_tracing
from domain.models import Citation, Fact, SourceDocument, SourceKind
from graph.state import SubTask
from service.extractor import ExtractionResult, HeuristicFactExtractor, LLMFactExtractor
from service.source_filter import SourcePolicy
from tools.fetchers import FakeFetcher
from tools.search_providers import FakeSearchProvider

# --- Pre-call length guard ---------------------------------------------------
MAX_INPUT_CHARS = 120_000  # ~30k tokens at 4 chars/token; hard stop before LLM call

# Tavily (and most search APIs) reject queries longer than ~1500 chars. The
# planner embeds the FULL user question into each subtask description, which can
# be 3000+ chars for DRB2 English tasks. Truncate to a safe bound before search;
# the extractor still sees the full fetched page text, so no evidence is lost.
MAX_SEARCH_QUERY_CHARS = 1400


class InputTooLongError(RuntimeError):
    """Raised when accumulated input would exceed MAX_INPUT_CHARS before a call."""


def estimate_tokens(text: str) -> int:
    """Rough deterministic token estimate: ceil(chars / 4)."""
    if not text:
        return 0
    return math.ceil(len(text) / 4)


def check_input_length(*parts: str, limit: int = MAX_INPUT_CHARS) -> None:
    """Raise InputTooLongError if total chars exceed `limit`.

    Call this before any LLM call so we never ship an oversized prompt.
    """
    total = sum(len(p) for p in parts)
    if total > limit:
        raise InputTooLongError(
            f"input too long: {total} chars > limit {limit}; truncate before calling the provider"
        )


# --- sleep injection (fake clock in tests) -----------------------------------
SleepFn = Callable[[float], Awaitable[None]]


async def _default_sleep(s: float) -> None:
    await asyncio.sleep(s)


# --- budget-aware model downgrade hook ---------------------------------------
class ModelRouter:
    """Callable that picks a model id based on budget snapshot.

    Business nodes never hardcode a model name; they ask the router. The
    default policy returns ``preferred`` when budget is healthy, and a
    configurable ``cheap`` model once the budget is in warning/critical/
    exhausted. ``cheap=None`` disables downgrade (always preferred).
    """

    #: budget statuses at which we are allowed to drop to the cheap model.
    DOWNGRADE_STATUSES = frozenset({"warning", "critical", "exhausted"})

    def __init__(self, *, preferred: str, cheap: str | None = None) -> None:
        # Boundary 2: reject nonsensical routing config at construction.
        if not isinstance(preferred, str) or not preferred.strip():
            raise ConfigurationError("ModelRouter.preferred must be a non-empty model id")
        if cheap is not None and (not isinstance(cheap, str) or not cheap.strip()):
            raise ConfigurationError("ModelRouter.cheap must be a non-empty model id or None")
        if cheap is not None and cheap == preferred:
            raise ConfigurationError(
                f"ModelRouter.cheap ({cheap}) must differ from preferred ({preferred})"
            )
        self.preferred = preferred
        self.cheap = cheap

    def pick(self, snapshot: BudgetSnapshot) -> str:
        if self.cheap and snapshot.status in self.DOWNGRADE_STATUSES:
            return self.cheap
        return self.preferred


class WorkerNode:
    def __init__(
        self,
        *,
        web_search: Any | None = None,
        fetcher: Any | None = None,
        fact_extractor: Any | None = None,
        max_results: int = 3,
        budget: BudgetManager | None = None,
        cancel_event: asyncio.Event | None = None,
        sleep_fn: SleepFn | None = None,
        llm_provider: Any | None = None,
        tracing: TracingContext | None = None,
        router: ModelRouter | None = None,
        max_search_retries: int = 2,
        max_iterations: int | None = None,
        blocked_urls: list[str] | None = None,
        blocked_domains: list[str] | None = None,
        blocked_titles: list[str] | None = None,
        as_of_date: str | None = None,
    ) -> None:
        # Boundary 2: validate the retry cap.
        if (
            not isinstance(max_search_retries, int)
            or isinstance(max_search_retries, bool)
            or max_search_retries < 0
        ):
            raise ConfigurationError(f"max_search_retries must be >= 0, got {max_search_retries!r}")
        self._web = web_search or FakeSearchProvider()
        self._fetcher = fetcher or FakeFetcher()
        # Research-time source constraints (DRB2 blocked-source / as-of rules).
        # Empty policy -> no-op, so existing callers are unaffected.
        self._policy = SourcePolicy(
            blocked_urls=list(blocked_urls or []),
            blocked_domains=list(blocked_domains or []),
            blocked_titles=list(blocked_titles or []),
            as_of_date=as_of_date,
        )
        # Resolve tracing FIRST (before building the extractor) so the
        # LLMFactExtractor receives the SAME noop/real TracingContext the
        # worker itself uses — its LLM calls then emit prompt-tagged LLM runs.
        self._tracing = tracing or noop_tracing()
        # When an LLM provider is available, use LLM-backed extraction.
        # It falls back to heuristic internally on parse failure.
        if fact_extractor is not None:
            self._extractor = fact_extractor
        elif llm_provider is not None:
            self._extractor = LLMFactExtractor(llm_provider, tracing=self._tracing)
        else:
            self._extractor = HeuristicFactExtractor()
        self._max_results = max_results
        self._budget = budget or BudgetManager()
        self._cancel = cancel_event or asyncio.Event()
        self._sleep = sleep_fn or _default_sleep
        self._llm = llm_provider
        if router is not None:
            self._router = router
        else:
            preferred = getattr(llm_provider, "model_id", "default") if llm_provider else "default"
            self._router = ModelRouter(preferred=preferred)
        self._max_search_retries = max_search_retries
        # Optional per-instance loop cap override (backward-compat callers pass
        # max_iterations to the worker rather than the budget).
        if max_iterations is not None:
            if (
                not isinstance(max_iterations, int)
                or isinstance(max_iterations, bool)
                or max_iterations <= 0
            ):
                raise ConfigurationError(
                    f"max_iterations must be a positive integer, got {max_iterations!r}"
                )
            self._budget.max_iterations = max_iterations

    def request_cancel(self) -> None:
        self._cancel.set()

    async def _await_cancellable(self, coro: Awaitable[Any]) -> tuple[Any, bool]:
        """Await ``coro`` but abort promptly when cancel is requested.

        Returns ``(result, False)`` on normal completion, or ``(None, True)``
        when the cancel event fired before/while the await was in flight. This
        makes cancellation interrupt search/fetch/extract awaits, not just the
        loop-top check.
        """
        if self._cancel.is_set():
            coro_task = asyncio.ensure_future(coro)
            coro_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await coro_task
            return None, True
        cancel_wait = asyncio.create_task(self._cancel.wait(), name="worker-cancel-wait")

        async def run_body() -> Any:
            return await coro

        body: asyncio.Task[Any] = asyncio.create_task(run_body(), name="worker-cancellable-body")
        try:
            done, _pending = await asyncio.wait(
                {body, cancel_wait}, return_when=asyncio.FIRST_COMPLETED
            )
            if cancel_wait in done and body not in done:
                body.cancel()
                await asyncio.gather(body, return_exceptions=True)
                return None, True
            return await body, False
        finally:
            # An outer timeout cancels ``_await_cancellable`` itself.  Always
            # reap both helper tasks in that path; otherwise Event.wait()
            # survives until loop shutdown and emits "Task was destroyed but
            # it is pending!".
            for task in (body, cancel_wait):
                if not task.done():
                    task.cancel()
            await asyncio.gather(body, cancel_wait, return_exceptions=True)

    async def _extract(
        self,
        docs: list[SourceDocument],
        *,
        user_context: str = "",
        model_id: str | None = None,
        llm: Any = None,
    ) -> ExtractionResult:
        """Call the extractor, threading user_context + request model override.

        ``llm`` is an optional per-request, model-scoped provider clone
        (``provider.with_model(...)``). When supplied the extractor uses it;
        otherwise it forwards ``model_id`` to its own provider as a request-level
        override. Duck-typed extractors that accept neither are called plainly.
        """
        try:
            return await self._extractor.extract(
                docs, user_context=user_context, model_id=model_id, llm=llm
            )
        except TypeError:
            try:
                return await self._extractor.extract(docs, user_context=user_context, llm=llm)
            except TypeError:
                return await self._extractor.extract(docs, user_context=user_context)

    async def run_task(self, task: SubTask, *, user_context: str = "") -> Command:
        task_id = task.task_id
        base_query = task.search_query or task.description or task.title
        current_query = base_query
        extraction_scope = task.description.strip()
        if user_context.strip():
            extraction_scope = f"{extraction_scope}\nUser context: {user_context.strip()}".strip()

        all_facts: list[Fact] = []
        all_citations: list[Citation] = []
        seen_urls: set[str] = set()
        seen_queries: list[str] = []
        prior_failures = 0
        expected_questions = {base_query}
        covered_questions: set[str] = set()
        errors: list[str] = []
        stop_reason = ""
        info_gap: str = ""

        for iteration in range(self._budget.max_iterations):
            # Cancellation check at the top of every iteration.
            if self._cancel.is_set():
                stop_reason = "cancelled"
                break

            # Budget critical: stop non-core work and record info gap.
            if self._budget.status() in {"critical", "exhausted"}:
                stop_reason = "budget_exhausted"
                info_gap = (
                    f"budget {self._budget.status()} at "
                    f"{self._budget.tokens_used()}/{self._budget.global_token_budget} tokens"
                )
                break

            # Per-task hard budget: THIS task has exhausted its own per-task
            # allowance. Stop only this task; the global ledger keeps
            # accumulating for unrelated tasks.
            if self._budget.task_exhausted(task_id):
                stop_reason = "task_budget_exhausted"
                info_gap = (
                    f"per-task token budget exhausted for {task_id}: "
                    f"{self._budget.task_tokens_used(task_id)}/"
                    f"{self._budget.per_task_token_budget} tokens"
                )
                break

            # Hard cap on search rounds for this task.
            if not self._budget.begin_round(task_id):
                stop_reason = "search_round_limit"
                break

            # Pre-call length guard: query + accumulated facts must not blow up.
            try:
                check_input_length(
                    current_query,
                    *(f.claim for f in all_facts[-20:]),
                )
            except InputTooLongError:
                stop_reason = "input_too_long"
                errors.append(f"{task_id}:error_code=input_too_long")
                break

            # Skip exact repeats.
            if current_query in seen_queries:
                current_query = f"{current_query} (rephrase #{iteration})"
            seen_queries.append(current_query)

            # --- search span (interruptible) ---
            # Truncate over-long search queries: Tavily rejects >1500 chars,
            # and most search engines degrade on very long queries. The full
            # question is still available to the extractor via fetched page text.
            search_query = current_query
            if len(search_query) > MAX_SEARCH_QUERY_CHARS:
                search_query = search_query[:MAX_SEARCH_QUERY_CHARS].rsplit(" ", 1)[0]
                errors.append(
                    f"{task_id}:search_query_truncated from={len(current_query)} to={len(search_query)}"
                )
            self._tracing.start_span("search", task_id=task_id, round=iteration)
            try:
                hits, cancelled = await self._await_cancellable(
                    self._web.search(search_query, max_results=self._max_results)
                )
            except Exception as e:  # classify, don't crash
                ce = classify_tool_error(e)
                self._tracing.end_span(
                    "search",
                    task_id=task_id,
                    status="error",
                    error=e,
                    stop_reason=ce.category,
                )
                # Boundary 6: NEVER put the raw exception text into errors[].
                errors.append(
                    f"{task_id}:error_code={ce.category} error_type={type(ce.original).__name__}"
                )
                if ce.category in {"auth_permission", "quota_exhausted", "cancelled"}:
                    return self._terminal_command(
                        task,
                        status="cancelled" if ce.category == "cancelled" else "failed",
                        stop_reason=ce.category,
                        errors=errors,
                        facts=all_facts,
                        citations=all_citations,
                    )
                # Honor Retry-After for rate-limit errors (fake clock in tests).
                if ce.category == "rate_limit" and ce.retry_after_s:
                    await self._sleep(ce.retry_after_s)
                elif ce.retryable:
                    await self._sleep(backoff_seconds(prior_failures))
                prior_failures += 1
                if prior_failures > self._max_search_retries:
                    stop_reason = "consecutive_failures"
                    break
                continue
            if cancelled:
                self._tracing.end_span("search", task_id=task_id, status="cancelled")
                stop_reason = "cancelled"
                break
            self._tracing.end_span("search", task_id=task_id, n_results=len(hits))

            # --- blocked-source gate (search results) ---
            # Drop hits whose URL/domain/title matches the DRB2 blocked list
            # BEFORE we spend a fetch on them. Record how many we dropped so
            # the run summary can surface it.
            if not self._policy.is_empty():
                hits, n_blocked = self._policy.filter_search_hits(hits)
                if n_blocked:
                    errors.append(
                        f"{task_id}:blocked_sources_filtered n={n_blocked} locator_policy=search"
                    )

            # --- fetch span (interruptible per URL) ---
            self._tracing.start_span("fetch", task_id=task_id, n_results=len(hits))
            docs: list[SourceDocument] = []
            fetch_attempt = 0
            fetch_cancelled = False
            for h in hits:
                # Defense-in-depth fetch gate: even if a blocked hit slipped past
                # the search filter, never fetch it and never cite it.
                if self._policy.is_blocked_url(h.url) or self._policy.is_blocked_title(h.title):
                    seen_urls.add(h.url)
                    errors.append(f"{task_id}:blocked_fetch_skipped locator={sanitize_url(h.url)}")
                    continue
                if h.url in seen_urls:
                    continue
                seen_urls.add(h.url)
                fetch_attempt += 1
                cid = f"c_{task_id}_{len(seen_urls)}"
                try:
                    doc, cancelled = await self._await_cancellable(
                        self._fetcher.fetch(h.url, citation_id=cid)
                    )
                except Exception as fe:  # structured provenance, never silent
                    # Boundary 6: sanitize the locator; never echo the exception.
                    errors.append(
                        f"fetch_failed task={task_id} locator={sanitize_url(h.url)} "
                        f"error_type={type(fe).__name__} error_code={classify_tool_error(fe).category} "
                        f"attempt={fetch_attempt} fetched_ok=False"
                    )
                    failed_cit = Citation(
                        citation_id=cid,
                        kind=SourceKind.WEB,
                        locator=h.url,
                        title=h.title,
                        source_task_id=task_id,
                    )
                    all_citations.append(failed_cit)
                    continue
                if cancelled:
                    fetch_cancelled = True
                    break
                if not doc.fetched_ok:
                    # Boundary 6: doc.error may contain private text; record only
                    # a stable code + the SANITIZED locator.
                    errors.append(
                        f"fetch_failed task={task_id} locator={sanitize_url(h.url)} "
                        f"error_type=fetch_failure error_code=fetched_ok_false "
                        f"attempt={fetch_attempt} fetched_ok=False"
                    )
                    failed_cit = doc.citation.model_copy(
                        update={"source_task_id": task_id, "title": doc.citation.title or h.title}
                    )
                    all_citations.append(failed_cit)
                    continue
                doc.citation = doc.citation.model_copy(update={"source_task_id": task_id})
                if not doc.citation.title and h.title:
                    doc.citation = doc.citation.model_copy(update={"title": h.title})
                docs.append(doc)
                all_citations.append(doc.citation)
            self._tracing.end_span("fetch", task_id=task_id, n_results=len(docs))
            if fetch_cancelled:
                stop_reason = "cancelled"
                break

            # --- extract span (interruptible) ---
            # Per-request model routing: ask the router which model id THIS round
            # should use. Providers that expose ``with_model`` hand back an
            # immutable per-request clone; providers that accept a ``model_id``
            # kwarg are routed via that. Either way we NEVER mutate the shared
            # provider's own model_id (boundary 3).
            model_id = self._router.pick(self._budget.snapshot())
            scoped_llm: Any = self._llm
            if self._llm is not None and hasattr(self._llm, "with_model"):
                try:
                    scoped_llm = self._llm.with_model(model_id)
                except NotImplementedError:
                    # Provider inherits the base (which raises) -> fall back to
                    # the request-level model_id kwarg path.
                    scoped_llm = self._llm
            # Extraction is a bounded JSON transformation over fetched text;
            # Qwen's default chain-of-thought adds thousands of completion
            # tokens and frequently exhausts the 75s provider deadline.
            if scoped_llm is not None and hasattr(scoped_llm, "with_thinking"):
                scoped_llm = scoped_llm.with_thinking(False)

            new_facts: list[Fact] = []
            if docs:
                worst_case = estimate_tokens(current_query + "".join(d.content for d in docs))
                # Boundary 1: hard pre-call reserve (ALL-OR-NOTHING ticket).
                # Refuse the call if no room. The returned ticket MUST be closed
                # on EVERY exit path (success -> commit; exception/cancel ->
                # release) so a reservation never leaks and starves others.
                try:
                    ticket = await self._budget.reserve(task_id, worst_case)
                except BudgetExceededError:
                    stop_reason = "budget_exhausted"
                    info_gap = (
                        f"token budget exhausted before extract for {task_id}: "
                        f"{self._budget.tokens_used()}/{self._budget.global_token_budget} tokens"
                    )
                    break

                self._tracing.start_span(
                    "extract", task_id=task_id, n_results=len(docs), model=model_id
                )
                try:
                    ext_result, cancelled = await self._await_cancellable(
                        self._extract(
                            docs,
                            user_context=extraction_scope,
                            model_id=model_id,
                            llm=scoped_llm,
                        )
                    )
                except Exception as e:
                    # Exception on ANY path: close the ticket WITHOUT charging
                    # usage so the reservation does not leak.
                    await ticket.release()
                    self._tracing.end_span("extract", task_id=task_id, status="error", error=e)
                    errors.append(f"{task_id}:error_code=extract error_type={type(e).__name__}")
                    prior_failures += 1
                    continue
                if cancelled:
                    # Cancel mid-extract: release, never leak the reservation.
                    await ticket.release()
                    self._tracing.end_span("extract", task_id=task_id, status="cancelled")
                    stop_reason = "cancelled"
                    break
                new_facts = list(ext_result)
                # Boundary 8: read THIS call's usage off the result (no shared
                # self.last_* read). Fall back to a char estimate only when the
                # extractor reported zero usage.
                usage = getattr(ext_result, "usage", None)
                if usage is None or usage.total == 0:
                    prompt_chars = current_query + "".join(d.content for d in docs)
                    completion_chars = "".join(f.claim for f in new_facts)
                    usage = TokenUsage(
                        prompt_tokens=estimate_tokens(prompt_chars),
                        completion_tokens=estimate_tokens(completion_chars),
                        estimated=True,
                    )
                try:
                    await ticket.commit(usage)
                except BudgetExceededError:
                    # Real usage breached the hard cap (ledger clamped to the
                    # cap, overflow recorded). The extract itself succeeded, so
                    # keep the facts, but stop on budget_exhausted.
                    self._tracing.end_span("extract", task_id=task_id, n_facts=len(new_facts))
                    stop_reason = "budget_exhausted"
                    info_gap = (
                        f"committed usage exceeded hard cap for {task_id}: "
                        f"{self._budget.tokens_used()}/{self._budget.global_token_budget} tokens"
                    )
                    break
                self._tracing.end_span("extract", task_id=task_id, n_facts=len(new_facts))
            else:
                usage = TokenUsage()

            for f in new_facts:
                tagged = f.model_copy(
                    update={
                        "source_task_id": task_id,
                        "fact_id": f"{f.fact_id}_{task_id}_r{iteration}",
                    }
                )
                all_facts.append(tagged)
            if new_facts:
                covered_questions.add(base_query)
                prior_failures = 0  # productive round resets failure streak

            # Drift detection (observable, not an LLM vote): if the rewritten
            # query no longer contains the base task query, we have wandered
            # off-track.
            drift_detected = bool(base_query) and base_query not in current_query

            reflection = decide_reflection(
                round_index=iteration,
                max_iterations=self._budget.max_iterations,
                seen_queries=seen_queries,
                last_query=current_query,
                new_results_this_round=len(hits),
                independent_sources_this_round=len({d.citation.locator for d in docs}),
                total_facts=len(all_facts),
                covered_questions=covered_questions,
                expected_questions=expected_questions,
                prior_failures=prior_failures,
                cancel_requested=self._cancel.is_set(),
                budget_ratio_remaining=self._budget.remaining_ratio(),
                drift_detected=drift_detected,
            )

            if reflection.should_stop:
                stop_reason = reflection.stop_reason or "unknown"
                break
            if reflection.next_queries:
                current_query = reflection.next_queries[0]
            # else: keep current query (shouldn't happen with current rewrite rules)

        if not stop_reason:
            stop_reason = "completed"

        return self._terminal_command(
            task,
            status=_status_for(stop_reason, had_facts=bool(all_facts)),
            stop_reason=stop_reason,
            errors=errors,
            facts=all_facts,
            citations=all_citations,
            info_gap=info_gap,
        )

    def _terminal_command(
        self,
        task: SubTask,
        *,
        status: str,
        stop_reason: str,
        errors: list[str],
        facts: list[Fact],
        citations: list[Citation],
        info_gap: str = "",
    ) -> Command:
        summary = (
            f"{len(facts)} facts / {len(citations)} sources "
            f"after {self._budget.rounds_used(task.task_id)} rounds ({stop_reason})."
        )
        if info_gap:
            summary += f" INFO_GAP: {info_gap}"
        return Command(
            update={
                "subtasks": [
                    task.model_copy(
                        update={
                            "status": status,
                            "result_summary": summary,
                            "error": msg_for(stop_reason),
                            "stop_reason": stop_reason,
                        }
                    )
                ],
                "facts": facts,
                "citations": citations,
                "errors": errors,
            }
        )


def _status_for(stop_reason: str, *, had_facts: bool) -> str:
    """Map a structured stop reason onto a terminal status (boundary 9)."""
    if stop_reason == "cancelled":
        return "cancelled"
    if stop_reason == "budget_exhausted":
        # partial when we got SOMETHING; failed when nothing was produced.
        return "partial" if had_facts else "failed"
    if stop_reason in {
        "auth_permission",
        "quota_exhausted",
        "consecutive_failures",
        "input_too_long",
        "task_budget_exhausted",
    }:
        return "failed"
    return "completed"


def msg_for(stop_reason: str) -> str:
    if stop_reason == "completed":
        return ""
    if stop_reason == "consecutive_failures":
        return "blocked_by_consecutive_failures"
    if stop_reason == "budget_exhausted":
        return "budget_exhausted"
    if stop_reason == "input_too_long":
        return "input_too_long"
    if stop_reason == "cancelled":
        return "cancelled"
    return stop_reason


__all__ = [
    "WorkerNode",
    "InputTooLongError",
    "ModelRouter",
    "estimate_tokens",
    "check_input_length",
]
