"""Phase 2 graph state, sub-task model, and reducers.

Concurrency semantics (this file is the contract):

- `SubTask.status` transitions: pending -> running -> completed | failed.
  A task that has reached a terminal state (completed/failed) MUST NOT move
  back to pending/running. The reducer `merge_subtasks()` enforces this.
- Duplicate `task_id` updates: a non-terminal update overwrites the in-flight
  fields; a terminal update wins once and locks the task. A second completion
  for an already-completed task is a no-op (idempotent), so a worker retry
  cannot corrupt a successful result.
- `facts` / `citations` / `errors`: append-only, de-duplicated by id
  (fact_id / citation_id). Output is SORTED by id (errors by content) so the
  merge is commutative / associative / idempotent and never depends on the
  order in which parallel workers happened to finish.
- Parallel worker results merge deterministically: two workers completing in
  either order produce the same final ResearchState.
- Terminal task conflict (same task_id delivered as completed AND failed):
  **completed always wins** deterministically — a failure must never
  overwrite a successful task, whichever worker arrived first.
"""

from __future__ import annotations

from typing import Literal, TypeVar

from pydantic import BaseModel, Field

from core.exceptions import InvalidPlanError
from domain.models import Citation, Fact

T = TypeVar("T", bound=BaseModel)

TaskStatus = Literal["pending", "running", "completed", "failed", "cancelled", "partial"]
ResearchDepth = Literal["quick", "standard", "deep"]

#: All terminal statuses. A task in one of these MUST NOT move back to
#: pending/running (enforced by ``merge_subtasks``).
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "partial"})


class SubTask(BaseModel):
    """One unit of research work dispatched by the Supervisor."""

    task_id: str = Field(pattern=r"^task_\d+$")
    title: str
    description: str
    perspective: str = ""
    depends_on: list[str] = Field(default_factory=list)
    priority: int = 0  # lower = higher priority
    status: TaskStatus = "pending"
    result_summary: str = ""
    error: str = ""
    # Stage-3 hard boundaries (9 / 5 / 7):
    # stop_reason is a STRUCTURED terminal cause (not a free-form string baked
    # into the summary). status maps onto it:
    #   completed            -> evidence_sufficient / max_iterations / completed
    #   failed               -> auth_permission / consecutive_failures /
    #                           input_too_long / auth_error
    #   cancelled            -> cancelled
    #   partial              -> budget_exhausted (had some facts) /
    #                           budget_exhausted with zero facts -> failed
    stop_reason: str = ""
    # Structured replan linkage (boundary 7): a follow-up task names the zero-fact
    # source tasks and WHY it was created, instead of string-matching
    # ``result_summary``.
    replan_of: str = ""
    gap_reason: str = ""
    gap_source_task_ids: list[str] = Field(default_factory=list)


# ---------- reducers -------------------------------------------------------
#
# These functions implement the merge semantics above. They are written as pure
# functions (no globals, no randomness) so routing rules can be unit-tested
# without spinning up the graph.


def _canonical(obj: BaseModel) -> str:
    """Stable serialization used to pick a deterministic winner on id conflicts.

    Non-semantic Pydantic timestamps (``extracted_at`` / ``fetched_at``) are
    excluded so the winner cannot be decided by *when* a worker happened to
    finish. Two semantically-equal models serialize identically; two
    same-id-but-different-body models fall back to lexicographic order, which is
    commutative, associative and idempotent (``min`` over strings).
    """
    return obj.model_dump_json(exclude={"extracted_at", "fetched_at"})


def _pick(existing: T | None, candidate: T) -> T:
    """Return the deterministic winner between two same-id payloads."""
    if existing is None:
        return candidate
    return existing if _canonical(existing) <= _canonical(candidate) else candidate


def merge_subtasks(left: list[SubTask], right: list[SubTask]) -> list[SubTask]:
    """Merge two sub-task lists, preserving the status transition lock.

    - Right wins on non-terminal updates (in-flight status flips).
    - A terminal status (completed/failed/cancelled/partial) locks the task; a
      subsequent non-terminal update for the same task_id is dropped (no
      resurrection).
    - Terminal conflict (both sides terminal for the same id):
      **completed always wins** over failed/cancelled/partial, deterministically.
      Two deliveries that are already on the same terminal status (e.g. two
      completed results with different ``result_summary``) are resolved by
      canonical serialization (lexicographically smallest body) rather than
      first-arrival, so a worker's finish order cannot pick the winner.
    - Output is sorted by task_id so the result is independent of arrival order.
    """
    terminal = set(TERMINAL_STATUSES)
    by_id: dict[str, SubTask] = {t.task_id: t.model_copy(deep=True) for t in left}
    for new in right:
        old = by_id.get(new.task_id)
        if old is None:
            by_id[new.task_id] = new
            continue
        if old.status in terminal and new.status not in terminal:
            # Already done/failed: cannot be resurrected.
            continue
        if old.status in terminal and new.status in terminal:
            # Success beats any non-success terminal outcome (failed / cancelled
            # / partial), deterministically.
            if old.status == "completed" and new.status != "completed":
                continue
            if old.status != "completed" and new.status == "completed":
                by_id[new.task_id] = new
                continue
            by_id[new.task_id] = _pick(old, new)  # same tier; canonical winner
            continue
        by_id[new.task_id] = new
    return sorted(by_id.values(), key=lambda t: t.task_id)


def merge_facts(left: list[Fact], right: list[Fact]) -> list[Fact]:
    """Dedup facts by fact_id; same-id/different-body -> canonical winner.

    The list is sorted by fact_id and the winner for a duplicated id is the
    lexicographically smallest canonical body (timestamps excluded), so the
    merge is commutative / associative / idempotent regardless of worker
    completion order.
    """
    best: dict[str, Fact] = {}
    for f in (*left, *right):
        best[f.fact_id] = _pick(best.get(f.fact_id), f)
    return [best[k] for k in sorted(best)]


def merge_citations(left: list[Citation], right: list[Citation]) -> list[Citation]:
    """Dedup citations by citation_id; same-id/different-body -> canonical winner."""
    best: dict[str, Citation] = {}
    for c in (*left, *right):
        best[c.citation_id] = _pick(best.get(c.citation_id), c)
    return [best[k] for k in sorted(best)]


def merge_errors(left: list[str], right: list[str]) -> list[str]:
    """Union of non-empty error strings, sorted deterministically.

    Sorted (rather than first-arrival) so concurrent completion order cannot
    reorder the error list.
    """
    seen = {e for e in (*left, *right) if e}
    return sorted(seen)


def validate_plan(subtasks: list[SubTask]) -> list[str]:
    """Pure structural validation of a planned sub-task graph.

    Returns a list of human-readable diagnostics. An empty list means the plan
    is structurally valid (an empty plan ``[]`` is valid: it simply means "no
    sources found", handled downstream by the writer).

    Rules checked:
    - every ``task_id`` is unique;
    - every id referenced in ``depends_on`` exists in the plan;
    - no task depends on itself;
    - the dependency graph is a DAG (no cycles), via Kahn's topological sort.
    """
    errors: list[str] = []

    ids = [t.task_id for t in subtasks]
    unique_ids = set(ids)

    # 1. uniqueness
    if len(ids) != len(unique_ids):
        seen: set[str] = set()
        for tid in ids:
            if tid in seen:
                errors.append(f"duplicate task_id: {tid}")
            seen.add(tid)

    # 2. dependency existence + self-dependency
    for t in subtasks:
        for dep in t.depends_on:
            if dep == t.task_id:
                errors.append(f"task '{t.task_id}' depends on itself")
            elif dep not in unique_ids:
                errors.append(f"task '{t.task_id}' depends on unknown task_id '{dep}'")

    # 3. DAG via Kahn's algorithm (only over known, non-self edges)
    indeg: dict[str, int] = {tid: 0 for tid in unique_ids}
    adj: dict[str, list[str]] = {tid: [] for tid in unique_ids}
    for t in subtasks:
        for dep in t.depends_on:
            if dep in unique_ids and dep != t.task_id:
                adj[dep].append(t.task_id)
                indeg[t.task_id] += 1
    queue = [n for n, d in indeg.items() if d == 0]
    visited = 0
    while queue:
        node = queue.pop(0)
        visited += 1
        for nxt in adj[node]:
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                queue.append(nxt)
    if unique_ids and visited != len(unique_ids):
        errors.append("dependency graph contains a cycle")

    return errors


def ensure_valid_plan(subtasks: list[SubTask]) -> list[SubTask]:
    """``validate_plan`` that raises ``InvalidPlanError`` when invalid.

    Wired into the graph immediately after the planner node so a bad plan
    fails fast with diagnostics instead of degrading into a recursion-limit
    blow-up at dispatch time.
    """
    problems = validate_plan(subtasks)
    if problems:
        raise InvalidPlanError(
            f"invalid plan with {len(problems)} problem(s)",
            details={"errors": problems},
        )
    return subtasks


class ResearchState(BaseModel):
    """Top-level state for the Supervisor-Worker graph (phase 2 subset)."""

    query: str = ""
    user_context: str = ""
    research_depth: ResearchDepth = "standard"

    subtasks: list[SubTask] = Field(default_factory=list)
    facts: list[Fact] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)

    report_markdown: str = ""


# Annotated state for LangGraph. We use plain dicts for the graph payload (not
# the Pydantic model itself) so reducers wire up cleanly.
def empty_state(query: str, *, depth: ResearchDepth = "standard", user_context: str = "") -> dict:
    return {
        "query": query,
        "user_context": user_context,
        "research_depth": depth,
        "subtasks": [],
        "facts": [],
        "citations": [],
        "errors": [],
        "report_markdown": "",
    }


# Reducer specs for StateGraph.
REDUCERS = {
    "subtasks": merge_subtasks,
    "facts": merge_facts,
    "citations": merge_citations,
    "errors": merge_errors,
}

# A type alias for the dict shape, used in node signatures.
StateDict = dict


__all__ = [
    "REDUCERS",
    "ResearchDepth",
    "ResearchState",
    "StateDict",
    "SubTask",
    "TaskStatus",
    "TERMINAL_STATUSES",
    "empty_state",
    "ensure_valid_plan",
    "merge_citations",
    "merge_errors",
    "merge_facts",
    "merge_subtasks",
    "validate_plan",
]
