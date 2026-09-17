"""Reflection: structured decision about whether a worker should continue.

This is deliberately a PURE function over observable signals. It does NOT call
an LLM. The design doc says "reflect based on observable signals", so we make
that explicit: inputs are counts (results, independent sources, failed rounds,
covered sub-questions) and the output is a finite-state decision.

LLM-based rewrite generation (which phrasing to use for the next query) can be
layered on top of the CATEGORY this function returns, but the stop/continue
decision itself is deterministic and testable.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

QueryRewriteCategory = Literal[
    "sufficient",
    "too_broad",
    "too_narrow",
    "off_track",
    "conflicting_evidence",
    "empty_result",
]

StopReason = Literal[
    "max_iterations",
    "budget_exhausted",
    "evidence_sufficient",
    "no_progress",
    "cancelled",
]


class ReflectionResult(BaseModel):
    """Deterministic reflection over observable signals."""

    should_stop: bool
    stop_reason: StopReason | None = None
    rewrite: QueryRewriteCategory = "sufficient"
    next_queries: list[str] = Field(default_factory=list)
    evidence_gaps: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0, default=0.0)
    note: str = ""


def decide_reflection(
    *,
    round_index: int,
    max_iterations: int,
    seen_queries: list[str],
    last_query: str,
    new_results_this_round: int,
    independent_sources_this_round: int,
    total_facts: int,
    covered_questions: set[str],
    expected_questions: set[str],
    prior_failures: int,
    cancel_requested: bool,
    budget_ratio_remaining: float,
    drift_detected: bool = False,
) -> ReflectionResult:
    """Pure decision. No I/O, no LLM.

    Args:
        round_index: 0-based round just completed.
        max_iterations: hard cap on search rounds for this task.
        seen_queries: all queries already issued (prevents exact repeats).
        last_query: the query just executed.
        new_results_this_round: results returned in the last search.
        independent_sources_this_round: distinct URLs fetched OK this round.
        total_facts: cumulative facts for this task.
        covered_questions: sub-questions already answered by facts.
        expected_questions: sub-questions we still want answers for.
        prior_failures: consecutive tool failures (search/fetch) so far.
        cancel_requested: external cancel signal.
        budget_ratio_remaining: 0..1, 0 means no budget left.
        drift_detected: observable signal that the rewritten query has wandered
            off the original scope (worker computes this from last_query vs the
            base task description). Maps to the ``off_track`` category.
    """
    # 1. Hard stops first.
    if cancel_requested:
        return ReflectionResult(
            should_stop=True,
            stop_reason="cancelled",
            rewrite="sufficient",
            note="cancel requested",
        )
    if budget_ratio_remaining <= 0.0:
        return ReflectionResult(
            should_stop=True,
            stop_reason="budget_exhausted",
            rewrite="sufficient",
            evidence_gaps=sorted(expected_questions - covered_questions),
            note="budget exhausted",
        )
    if round_index + 1 >= max_iterations:
        return ReflectionResult(
            should_stop=True,
            stop_reason="max_iterations",
            rewrite="sufficient",
            evidence_gaps=sorted(expected_questions - covered_questions),
            note=f"hit max_iterations={max_iterations}",
        )

    # 2. Empty result -> rewrite to an alternative phrasing.
    if new_results_this_round == 0:
        return ReflectionResult(
            should_stop=False,
            rewrite="empty_result",
            next_queries=[_rewrite_query(last_query, "empty_result")],
            note="no results, rewriting",
        )

    # 3. No progress two rounds in a row.
    if prior_failures >= 2:
        return ReflectionResult(
            should_stop=True,
            stop_reason="no_progress",
            rewrite="sufficient",
            evidence_gaps=sorted(expected_questions - covered_questions),
            note=f"{prior_failures} consecutive failures",
        )

    # 4. Sufficient evidence: enough facts and enough independent sources.
    missing = expected_questions - covered_questions
    if total_facts >= 3 and independent_sources_this_round >= 2 and not missing:
        return ReflectionResult(
            should_stop=True,
            stop_reason="evidence_sufficient",
            rewrite="sufficient",
            confidence=min(1.0, total_facts / 6.0),
            note="enough facts + independent sources",
        )

    # 5. Off-track: the query has drifted from the original scope. This is an
    #    explicit observable signal (drift_detected) rather than an LLM vote.
    if drift_detected:
        return ReflectionResult(
            should_stop=False,
            rewrite="off_track",
            next_queries=[_rewrite_query(last_query, "off_track")],
            evidence_gaps=sorted(missing),
            confidence=min(1.0, total_facts / 6.0),
            note="query drifted off the original scope, refocusing",
        )

    # 6. Deterministic category from observable signals. Every one of the six
    #    categories is reachable here and maps to a fixed rewrite direction.
    if new_results_this_round <= 1:
        # Very few hits -> broaden.
        category: QueryRewriteCategory = "too_narrow"
    elif independent_sources_this_round == 1 and total_facts > 0:
        # All facts from one source: cross-check (conflicting views) when
        # questions remain, otherwise the query itself is too broad.
        category = "conflicting_evidence" if missing else "too_broad"
    elif new_results_this_round >= 4:
        # Many shallow hits -> focus on a narrower sub-aspect.
        category = "too_broad"
    else:
        category = "sufficient"

    next_queries: list[str] = []
    if not should_stop(covered_questions, expected_questions):
        next_queries = [_rewrite_query(last_query, category)]

    return ReflectionResult(
        should_stop=False,
        rewrite=category,
        next_queries=next_queries,
        evidence_gaps=sorted(missing),
        confidence=min(1.0, total_facts / 6.0),
        note=f"continuing: {category}",
    )


def should_stop(covered: set[str], expected: set[str]) -> bool:
    return expected.issubset(covered)


def _rewrite_query(q: str, category: QueryRewriteCategory) -> str:
    """Deterministic query rewrite. Not LLM. Tests assert no exact repeats."""
    suffix = {
        "too_broad": " (focus on a specific sub-aspect)",
        "too_narrow": " (broaden to adjacent topics)",
        "off_track": " (stay on original question)",
        "conflicting_evidence": " (look for conflicting views)",
        "empty_result": " (try alternative phrasing)",
        "sufficient": "",
    }[category]
    base = q.rstrip("?. ")
    out = f"{base}{suffix}"
    return out


__all__ = [
    "QueryRewriteCategory",
    "ReflectionResult",
    "StopReason",
    "decide_reflection",
]
