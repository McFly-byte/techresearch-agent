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

_IMPORTANT_SPLIT_RE = re.compile(r"\*\*important\*\*", re.IGNORECASE)
_LIST_PREFIX_RE = re.compile(
    r"^\s*(?:[-*•]\s+|\d{1,2}[.)、]\s*|[a-zA-Z][.)、）]\s*|"
    r"[A-ZＡ-Ｚ][)）]\s*|[一二三四五六七八九十]+[、.．）]\s*)"
)
_SECTION_PREFIX_RE = re.compile(
    r"^\s*(?:part\s+(?:one|two|three|four|\d+)|the\s+(?:first|second|third|fourth)\s+part|"
    r"第[一二三四五六七八九十]+部分|[A-ZＡ-Ｚ][)）])",
    re.IGNORECASE,
)
_INLINE_NUMBER_RE = re.compile(r"(?<!\w)(\d{1,2})[)）]\s*")
_MAX_SEARCH_QUERY_CHARS = 800


def _clean_requirement(text: str) -> str:
    cleaned = _LIST_PREFIX_RE.sub("", text.strip())
    cleaned = re.sub(r"[*_`#]+", "", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip(" :-：；;")


def extract_coverage_requirements(query: str) -> list[str]:
    """Extract an ordered checklist from the visible user question only.

    DRB2 prompts usually carry explicit numbered sections/bullets. Keeping this
    deterministic makes the plan inspectable and prevents hidden-rubric leakage.
    The function is intentionally conservative: when no useful structure is
    present the caller falls back to the legacy generic perspectives.
    """

    visible = _IMPORTANT_SPLIT_RE.split(query, maxsplit=1)[0].strip()
    lines = [line.strip() for line in visible.splitlines() if line.strip()]
    candidates: list[str] = []
    for line in lines[1:]:
        if _LIST_PREFIX_RE.match(line) or _SECTION_PREFIX_RE.match(line):
            item = _clean_requirement(line)
            if len(item) >= 4:
                candidates.append(item)

    # Some prompts put six or more numbered requirements in one introductory
    # sentence (for example "1) ... 2) ..."). Recover those items as well.
    for line in lines:
        matches = list(_INLINE_NUMBER_RE.finditer(line))
        if len(matches) < 3:
            continue
        for idx, match in enumerate(matches):
            end = matches[idx + 1].start() if idx + 1 < len(matches) else len(line)
            item = _clean_requirement(line[match.end() : end])
            if len(item) >= 4:
                candidates.append(item)

    out: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        key = item.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out[:16]


def _question_anchor(query: str) -> str:
    visible = _IMPORTANT_SPLIT_RE.split(query, maxsplit=1)[0].strip()
    first = next((line.strip() for line in visible.splitlines() if line.strip()), visible)
    return re.sub(r"\s+", " ", first)[:220].strip()


def _group_requirements(items: list[str], n: int) -> list[list[str]]:
    """Split ordered requirements into ``n`` contiguous, balanced groups."""

    groups: list[list[str]] = []
    start = 0
    for idx in range(n):
        remaining_items = len(items) - start
        remaining_groups = n - idx
        take = max(1, (remaining_items + remaining_groups - 1) // remaining_groups)
        groups.append(items[start : start + take])
        start += take
    return [group for group in groups if group]


def _compact_search_query(anchor: str, requirements: list[str]) -> str:
    # Put the distinguishing requirement first so any provider-side truncation
    # preserves query diversity. Keep enough topic context to disambiguate it.
    body = "; ".join(req[:260] for req in requirements)
    query = f"{body} | topic: {anchor}" if anchor else body
    return query[:_MAX_SEARCH_QUERY_CHARS].rsplit(" ", 1)[0].strip()


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

        requirements = extract_coverage_requirements(q)
        if len(requirements) >= 2:
            groups = _group_requirements(requirements, min(n, len(requirements)))
            anchor = _question_anchor(q)
            coverage_tasks: list[SubTask] = []
            requirement_index = 0
            for i, group in enumerate(groups, start=1):
                ids = [f"R{j}" for j in range(requirement_index + 1, requirement_index + len(group) + 1)]
                requirement_index += len(group)
                description = "Coverage requirements:\n" + "\n".join(
                    f"- {rid}: {item}" for rid, item in zip(ids, group, strict=True)
                )
                coverage_tasks.append(
                    SubTask(
                        task_id=f"task_{i}",
                        title=group[0][:80],
                        description=description,
                        perspective="coverage",
                        priority=i,
                        requirement_ids=ids,
                        coverage_requirements=group,
                        search_query=_compact_search_query(anchor, group),
                    )
                )
            return coverage_tasks

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
                    search_query=_compact_search_query(_question_anchor(q), [body]),
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
    "extract_coverage_requirements",
]
