"""Planner: turn a user query into a list of SubTasks.

Phase 2 uses a deterministic heuristic planner (no LLM) so routing is
testable offline. The planner recognizes comparison-style queries and emits
per-perspective sub-tasks. The boundary below is the **unified planner
contract**: every planner must implement

    async def decompose(query: str, *, user_context: str = "",
                        research_depth: ResearchDepth = "standard") -> list[SubTask]

An LLM-backed planner with `with_structured_output` will replace this in
phase 3+, but the boundary (decompose() -> list[SubTask], one controlled
retry on parse failure) is the contract. A plan is then structurally validated
by ``graph.state.validate_plan`` before dispatch.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Protocol, runtime_checkable

from core.exceptions import ConfigurationError
from graph.state import ResearchDepth, SubTask

# Number of sub-tasks per depth.
# Design doc (ch.4 task decomposition): quick=3, standard=4-5, deep=6-8 (future).
# Phase 2 pins the 3-5 boundary: quick=3, standard=4, deep=5. This is NOT the
# design doc's future deep=6-8 and must not regress to quick=2.
DEPTH_COUNTS: dict[ResearchDepth, int] = {
    "quick": 3,
    "standard": 4,
    "deep": 5,
}

# Robust comparison split: matches "vs", "VS", "Vs", "v.s.", "versus", "对比"
# surrounded by whitespace, case-insensitively. A plain ``str.split(" vs ")``
# used to crash on uppercase "VS".
_VS_SPLIT_RE = re.compile(r"\s+(?:vs|VS|Vs|v\.s\.|对比|versus)\s+", re.IGNORECASE)


@runtime_checkable
class Planner(Protocol):
    """Unified planner protocol (structural)."""

    name: str

    async def decompose(
        self,
        query: str,
        *,
        user_context: str = "",
        research_depth: ResearchDepth = "standard",
    ) -> list[SubTask]: ...  # pragma: no cover - protocol


class PlannerParseError(ConfigurationError):
    """Planner output could not be parsed even after one controlled retry."""

    error_code = "planner_parse_error"


# A parser takes a composed raw string and returns list[SubTask].
# Raises ValueError on bad input.
Parser = Callable[[str], list[SubTask]]


def _split_comparison(query: str) -> tuple[str, str] | None:
    """Split "A vs B" (any case / full-width delimiter) into (A, B).

    Returns ``None`` when the query is not a clean two-sided comparison so the
    caller falls back to generic perspectives.
    """
    parts = [p.strip() for p in _VS_SPLIT_RE.split(query) if p.strip()]
    if len(parts) == 2:
        return parts[0], parts[1]
    return None


class HeuristicPlanner:
    """Offline deterministic planner.

    Produces sub-tasks by splitting the query into perspectives. For
    "A vs B" style queries it emits per-side tasks plus synthesis tasks that
    depend on BOTH per-side tasks (so wave-1 parallelism is exactly the two
    sides). ``user_context`` is folded into every task description as an
    explicit scope constraint, so two different contexts yield distinguishable
    plans.
    """

    name = "heuristic_planner"

    async def decompose(
        self,
        query: str,
        *,
        user_context: str = "",
        research_depth: ResearchDepth = "standard",
    ) -> list[SubTask]:
        n = DEPTH_COUNTS[research_depth]
        q = query.strip()

        comparison = _split_comparison(q)
        if comparison is not None:
            left, right = comparison
            perspectives = [
                ("architecture", f"{left}: architecture and state model"),
                ("retrieval", f"{right}: architecture and retrieval model"),
                ("ecosystem", f"ecosystem and tooling around {left} vs {right}"),
                ("deployment", f"deployment and operational trade-offs of {left} vs {right}"),
                ("adoption", f"{left} vs {right}: learning curve and adoption barriers"),
            ]
        else:
            perspectives = [
                ("architecture", f"{q}: architecture overview"),
                ("ecosystem", f"{q}: ecosystem and maturity"),
                ("tradeoffs", f"{q}: trade-offs and limitations"),
                ("production", f"{q}: production readiness"),
                ("future", f"{q}: future outlook and alternatives"),
            ]

        # Trim / pad to the depth budget.
        perspectives = perspectives[:n]

        # Fold user_context into every task description as an explicit scope.
        scope = f" (scope: {user_context.strip()})" if user_context.strip() else ""
        # Per-side tasks are independent; every later comparison task depends on
        # BOTH sides. Generic (non-comparison) perspectives have no deps.
        side_deps: list[str] = ["task_1", "task_2"] if comparison is not None else []

        tasks: list[SubTask] = []
        for i, (perspective, body) in enumerate(perspectives, start=1):
            if comparison is not None and i <= 2:
                deps: list[str] = []
            elif comparison is not None:
                deps = list(side_deps)
            else:
                deps = []
            description = f"{body}{scope}"
            tasks.append(
                SubTask(
                    task_id=f"task_{i}",
                    title=body.split(":", 1)[0] if ":" in body else body,
                    description=description,
                    perspective=perspective,
                    depends_on=deps,
                    priority=i,
                )
            )
        return tasks


class RetryingPlanner:
    """Wraps a parser with one controlled retry on parse failure.

    Implements the unified planner protocol. The inner callable is expected to
    raise ``ValueError`` on bad output. We compose ``query + user_context +
    research_depth`` into a single raw string before calling the parser, so an
    LLM structured-output wrapper always receives the full planning request.
    """

    name = "retrying_planner"

    def __init__(self, inner: Parser) -> None:
        self._inner = inner

    @staticmethod
    def _compose(query: str, user_context: str, research_depth: ResearchDepth) -> str:
        parts = [f"QUERY: {query}"]
        if user_context.strip():
            parts.append(f"USER_CONTEXT: {user_context.strip()}")
        parts.append(f"DEPTH: {research_depth}")
        return "\n".join(parts)

    async def decompose(
        self,
        query: str,
        *,
        user_context: str = "",
        research_depth: ResearchDepth = "standard",
    ) -> list[SubTask]:
        raw = self._compose(query, user_context, research_depth)
        try:
            return self._inner(raw)
        except ValueError:
            # One controlled retry: pass the raw string back with a "fix this" hint.
            repaired = f"PARSE_ERROR_REPAIR({raw})"
            try:
                return self._inner(repaired)
            except ValueError as e:
                raise PlannerParseError(f"planner output unparseable: {e}") from e


__all__ = [
    "DEPTH_COUNTS",
    "HeuristicPlanner",
    "Planner",
    "PlannerParseError",
    "RetryingPlanner",
]
