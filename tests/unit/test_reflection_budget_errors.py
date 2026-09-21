"""Phase 3 pure-logic tests: reflection, error classifier, budget."""

from __future__ import annotations

from agents.budget import BudgetManager, TokenUsage
from agents.errors import backoff_seconds, classify_tool_error
from agents.reflection import decide_reflection
from core.exceptions import ToolQuotaExceededError, ToolTimeoutError, TransientToolError


# --- reflection ------------------------------------------------------------
def _base(**kw):  # type: ignore[no-untyped-def]
    defaults = dict(
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


def test_reflection_sufficient_stops():  # type: ignore[no-untyped-def]
    r = decide_reflection(**_base())
    assert r.should_stop is True
    assert r.stop_reason == "evidence_sufficient"
    assert r.confidence > 0


def test_reflection_empty_result_rewrites():  # type: ignore[no-untyped-def]
    r = decide_reflection(**_base(new_results_this_round=0, total_facts=0))
    assert r.should_stop is False
    assert r.rewrite == "empty_result"
    assert r.next_queries[0] != "q"  # deterministic rewrite, not same query


def test_reflection_max_iterations_hard_stop():  # type: ignore[no-untyped-def]
    r = decide_reflection(**_base(round_index=3, max_iterations=4))
    assert r.should_stop is True
    assert r.stop_reason == "max_iterations"


def test_reflection_budget_exhausted():  # type: ignore[no-untyped-def]
    r = decide_reflection(**_base(budget_ratio_remaining=0.0))
    assert r.should_stop is True
    assert r.stop_reason == "budget_exhausted"


def test_reflection_cancel():  # type: ignore[no-untyped-def]
    r = decide_reflection(**_base(cancel_requested=True))
    assert r.should_stop is True
    assert r.stop_reason == "cancelled"


def test_reflection_too_narrow():  # type: ignore[no-untyped-def]
    r = decide_reflection(
        **_base(new_results_this_round=1, total_facts=1, expected_questions={"q", "q2"})
    )
    assert r.should_stop is False
    assert r.rewrite == "too_narrow"


def test_reflection_conflicting_single_source():  # type: ignore[no-untyped-def]
    r = decide_reflection(
        **_base(
            independent_sources_this_round=1,
            total_facts=3,
            expected_questions={"q", "q2"},
        )
    )
    assert r.rewrite in {"conflicting_evidence", "too_broad"}


def test_reflection_no_progress_after_failures():  # type: ignore[no-untyped-def]
    r = decide_reflection(**_base(prior_failures=2, total_facts=0))
    assert r.should_stop is True
    assert r.stop_reason == "no_progress"


# --- error classifier ------------------------------------------------------
def test_classify_timeout():  # type: ignore[no-untyped-def]
    ce = classify_tool_error(ToolTimeoutError("x"))
    assert ce.category == "timeout"
    assert ce.retryable is True


def test_classify_transient():  # type: ignore[no-untyped-def]
    ce = classify_tool_error(TransientToolError("5xx"))
    assert ce.category == "transient_network"
    assert ce.retryable is True


def test_classify_rate_limit():  # type: ignore[no-untyped-def]
    ce = classify_tool_error(RuntimeError("429 Too Many Requests"))
    assert ce.category == "rate_limit"
    assert ce.retryable is True


def test_classify_quota_exhaustion_does_not_retry():  # type: ignore[no-untyped-def]
    ce = classify_tool_error(ToolQuotaExceededError("safe stable message"))
    assert ce.category == "quota_exhausted"
    assert ce.retryable is False


def test_classify_auth_no_retry():  # type: ignore[no-untyped-def]
    ce = classify_tool_error(RuntimeError("401 unauthorized"))
    assert ce.category == "auth_permission"
    assert ce.retryable is False


def test_backoff_is_deterministic():  # type: ignore[no-untyped-def]
    assert backoff_seconds(0, base=0.5) == 0.5
    assert backoff_seconds(1, base=0.5) == 1.0
    assert backoff_seconds(10, base=0.5, cap=8.0) == 8.0


# --- budget ----------------------------------------------------------------
def test_budget_warning_and_critical():  # type: ignore[no-untyped-def]
    b = BudgetManager(global_token_budget=1000, warning_ratio=0.8, critical_ratio=0.95)
    assert b.status() == "ok"
    b.record_usage("task_1", TokenUsage(prompt_tokens=850, completion_tokens=0))
    assert b.status() == "warning"  # 85% used -> 15% remaining, below 20% threshold
    b.record_usage("task_1", TokenUsage(prompt_tokens=120, completion_tokens=0))
    assert b.status() == "critical"  # 97% used
    b.record_usage("task_1", TokenUsage(prompt_tokens=100, completion_tokens=0))
    assert b.status() == "exhausted"


def test_budget_search_round_hard_cap():  # type: ignore[no-untyped-def]
    b = BudgetManager(max_search_rounds=2)
    assert b.begin_round("task_1") is True
    assert b.begin_round("task_1") is True
    assert b.begin_round("task_1") is False


def test_budget_replan_cap():  # type: ignore[no-untyped-def]
    b = BudgetManager(max_replans=1)
    assert b.can_replan() is True
    b.record_replan()
    assert b.can_replan() is False


def test_budget_estimated_usage_marked():  # type: ignore[no-untyped-def]
    u = TokenUsage(prompt_tokens=500, completion_tokens=200, estimated=True)
    assert u.estimated is True
    assert u.total == 700
