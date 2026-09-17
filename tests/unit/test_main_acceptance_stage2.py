"""Stage 2 contract acceptance tests.

These pin the six concrete contract counter-examples that MainAgent reproduced
through the PUBLIC graph / planner / supervisor / reducers surface. They are
written against the public API only (no reaching into private internals):

1. Unified Planner protocol + case-robust comparison splitting.
2. Planner actually consumes ``user_context`` and the exact depth budgets.
3. Supervisor wait/finish decision + ``max_workers > 0`` validation.
4. Pure ``validate_plan`` + structured ``InvalidPlanError`` (no recursion blow-up).
5. Reducers are commutative / associative / idempotent under concurrency, with
   "success wins" for terminal task conflicts.
6. (Covered by docs/stage2-orchestration-state.html staying in sync with code.)

These assertions must NOT be weakened: each encodes a previously-broken
offline counter-example.
"""

from __future__ import annotations

import pytest

from agents.planner import (
    DEPTH_COUNTS,
    HeuristicPlanner,
    RetryingPlanner,
)
from agents.supervisor import decide_dispatch
from core.exceptions import ConfigurationError, InvalidPlanError
from domain.models import Citation, Fact, SourceKind
from graph.builder import build_graph
from graph.state import (
    SubTask,
    merge_citations,
    merge_errors,
    merge_facts,
    merge_subtasks,
    validate_plan,
)


def _t(tid: str, status: str = "pending", deps: list[str] | None = None) -> SubTask:
    return SubTask(
        task_id=tid,
        title=tid,
        description=tid,
        depends_on=deps or [],
        status=status,  # type: ignore[arg-type]
        priority=int(tid.split("_")[1]),
    )


# ---------------------------------------------------------------------------
# Item 1: unified Planner protocol + case-robust comparison splitting
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_heuristic_planner_vs_uppercase_does_not_raise() -> None:
    p = HeuristicPlanner()
    # "VS" used to crash with a ValueError because split was case-sensitive.
    tasks = await p.decompose("LangGraph VS LlamaIndex", research_depth="standard")
    assert tasks, "uppercase comparison query must still decompose"


@pytest.mark.asyncio
async def test_heuristic_planner_vs_case_insensitive_same_structure() -> None:
    p = HeuristicPlanner()
    lower = await p.decompose("LangGraph vs LlamaIndex", research_depth="standard")
    upper = await p.decompose("LangGraph VS LlamaIndex", research_depth="standard")
    assert [t.task_id for t in lower] == [t.task_id for t in upper]
    assert [t.depends_on for t in lower] == [t.depends_on for t in upper]
    assert [t.title for t in lower] == [t.title for t in upper]


@pytest.mark.asyncio
async def test_retrying_planner_accepts_unified_signature() -> None:
    captured: list[str] = []

    def inner(raw: str) -> list[SubTask]:
        captured.append(raw)
        return [SubTask(task_id="task_1", title="x", description=raw)]

    # Previously: RetryingPlanner.decompose(raw: str) rejected keyword
    # user_context/research_depth with a TypeError. The unified planner
    # protocol is decompose(query, *, user_context, research_depth).
    rp = RetryingPlanner(inner)
    out = await rp.decompose(
        "LangGraph vs LlamaIndex", user_context="focus on cost", research_depth="deep"
    )
    assert len(out) == 1
    # The inner callable actually received the propagated context, not just query.
    assert any("focus on cost" in c for c in captured)


@pytest.mark.asyncio
async def test_build_graph_accepts_unified_planner_protocol() -> None:
    from agents.worker import WorkerNode

    seen: dict[str, object] = {}

    class UnifiedPlanner:
        name = "unified_test_planner"

        async def decompose(
            self, query: str, *, user_context: str = "", research_depth: str = "standard"
        ):  # type: ignore[override]
            seen["query"] = query
            seen["user_context"] = user_context
            seen["research_depth"] = research_depth
            return [SubTask(task_id="task_1", title="t", description=f"{query} {user_context}")]

    g = build_graph(worker=WorkerNode(), planner=UnifiedPlanner(), max_workers=2)
    await g.ainvoke({"query": "RAG", "user_context": "offline", "research_depth": "quick"})
    assert seen["user_context"] == "offline"
    assert seen["research_depth"] == "quick"


# ---------------------------------------------------------------------------
# Item 2: planner consumes user_context + exact depth budgets
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_depth_counts_exact_3_4_5() -> None:
    p = HeuristicPlanner()
    assert DEPTH_COUNTS == {"quick": 3, "standard": 4, "deep": 5}
    assert len(await p.decompose("RAG survey", research_depth="quick")) == 3
    assert len(await p.decompose("RAG survey", research_depth="standard")) == 4
    assert len(await p.decompose("RAG survey", research_depth="deep")) == 5


@pytest.mark.asyncio
async def test_user_context_changes_plan_but_keeps_query() -> None:
    p = HeuristicPlanner()
    a = await p.decompose(
        "LangGraph vs LlamaIndex",
        user_context="focus on production cost",
        research_depth="standard",
    )
    b = await p.decompose(
        "LangGraph vs LlamaIndex", user_context="focus on latency", research_depth="standard"
    )
    desc_a = [t.description for t in a]
    desc_b = [t.description for t in b]
    # The two contexts must produce distinguishable plans.
    assert desc_a != desc_b
    # ...and neither may drop the original query subjects: across the whole
    # plan, both comparison subjects are still covered (per-side tasks focus on
    # one side each, which is exactly the intended decomposition).
    plan_a = " ".join(desc_a)
    plan_b = " ".join(desc_b)
    assert "LangGraph" in plan_a and "LlamaIndex" in plan_a
    assert "LangGraph" in plan_b and "LlamaIndex" in plan_b


@pytest.mark.asyncio
async def test_subtasks_carry_complete_fields() -> None:
    p = HeuristicPlanner()
    tasks = await p.decompose("vector databases", research_depth="deep")
    for t in tasks:
        assert t.task_id
        assert t.title
        assert t.description  # contains goal / scope
        assert t.perspective  # perspective label present
        assert t.depends_on is not None
        assert isinstance(t.priority, int)


# ---------------------------------------------------------------------------
# Item 3: supervisor wait/finish + max_workers validation
# ---------------------------------------------------------------------------
def test_supervisor_waits_when_running() -> None:
    decision = decide_dispatch([_t("task_1", "running")], max_workers=1)
    assert decision.action == "wait"


def test_supervisor_finishes_when_completed() -> None:
    decision = decide_dispatch([_t("task_1", "completed")], max_workers=1)
    assert decision.action == "finish"


def test_supervisor_max_workers_non_positive_raises() -> None:
    with pytest.raises((ConfigurationError, ValueError)):
        decide_dispatch([_t("task_1")], max_workers=0)
    with pytest.raises((ConfigurationError, ValueError)):
        decide_dispatch([_t("task_1")], max_workers=-1)


def test_supervisor_finishes_when_only_failed_no_running() -> None:
    # Deadlock guard: no pending, no running, only failed -> finish, not wait forever.
    decision = decide_dispatch([_t("task_1", "failed")], max_workers=2)
    assert decision.action == "finish"


# ---------------------------------------------------------------------------
# Item 4: validate_plan pure function + InvalidPlanError
# ---------------------------------------------------------------------------
def test_validate_plan_unknown_dependency_rejected() -> None:
    tasks = [_t("task_1", deps=["task_999"])]
    errors = validate_plan(tasks)
    assert errors, "unknown dependency must be diagnosed"


def test_validate_plan_cycle_rejected() -> None:
    tasks = [
        _t("task_1", deps=["task_2"]),
        _t("task_2", deps=["task_1"]),
    ]
    assert validate_plan(tasks), "a 1<->2 cycle must be diagnosed, not recurse"


def test_validate_plan_duplicate_task_id_rejected() -> None:
    tasks = [
        SubTask(task_id="task_1", title="a", description="a"),
        SubTask(task_id="task_1", title="b", description="b"),
    ]
    assert validate_plan(tasks)


def test_validate_plan_self_dependency_rejected() -> None:
    tasks = [_t("task_1", deps=["task_1"])]
    assert validate_plan(tasks)


def test_validate_plan_empty_and_valid_ok() -> None:
    assert validate_plan([]) == []
    ok = [
        _t("task_1"),
        _t("task_2", deps=["task_1"]),
        _t("task_3", deps=["task_1", "task_2"]),
    ]
    assert validate_plan(ok) == []


@pytest.mark.asyncio
async def test_graph_rejects_invalid_plan_with_structured_error() -> None:
    from agents.worker import WorkerNode

    class BadPlanner(HeuristicPlanner):
        async def decompose(self, query, *, user_context="", research_depth="standard"):  # type: ignore[override]
            return [_t("task_1", deps=["task_999"])]

    g = build_graph(worker=WorkerNode(), planner=BadPlanner(), max_workers=2)
    with pytest.raises(InvalidPlanError):
        await g.ainvoke({"query": "x", "research_depth": "quick"})


# ---------------------------------------------------------------------------
# Item 5: reducers commutative / associative / idempotent
# ---------------------------------------------------------------------------
def _fact(fid: str, claim: str) -> Fact:
    return Fact(fact_id=fid, claim=claim, source_citation_ids=["c1"])


def _cit(cid: str) -> Citation:
    return Citation(citation_id=cid, kind=SourceKind.WEB, locator=f"https://{cid}")


def test_merge_facts_commutative_and_idempotent() -> None:
    f1, f2 = _fact("f1", "a"), _fact("f2", "b")
    forward = [x.fact_id for x in merge_facts([f1], [f2])]
    backward = [x.fact_id for x in merge_facts([f2], [f1])]
    assert forward == backward, "merge_facts must not depend on arrival order"
    # Idempotent: merging the same payload twice is a no-op.
    once = merge_facts([f1], [f2])
    twice = merge_facts(once, [f2])
    assert [x.fact_id for x in twice] == forward


def test_merge_citations_commutative() -> None:
    c1, c2 = _cit("c1"), _cit("c2")
    forward = [x.citation_id for x in merge_citations([c1], [c2])]
    backward = [x.citation_id for x in merge_citations([c2], [c1])]
    assert forward == backward


def test_merge_errors_commutative() -> None:
    assert merge_errors(["e1", "e2"], ["e3"]) == merge_errors(["e3"], ["e1", "e2"])


def test_merge_subtasks_completed_beats_failed() -> None:
    failed = _t("task_1", "failed")
    failed = failed.model_copy(update={"error": "boom"})
    done = _t("task_1", "completed")
    done = done.model_copy(update={"result_summary": "ok"})
    # Failure must NOT overwrite a success for the same task id.
    out = merge_subtasks([done], [failed])
    assert out[0].status == "completed"
    # And the reverse arrival order also resolves to completed deterministically.
    out2 = merge_subtasks([failed], [done])
    assert out2[0].status == "completed"


def test_merge_subtasks_result_independent_of_worker_order() -> None:
    a1, a2 = _t("task_1", "completed"), _t("task_2", "completed")
    s1 = [t.task_id for t in merge_subtasks([a1], [a2])]
    s2 = [t.task_id for t in merge_subtasks([a2], [a1])]
    assert s1 == s2
