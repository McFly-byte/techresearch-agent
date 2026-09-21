"""Assemble the Supervisor-Worker graph with bounded replan loop.

Layout:
    START -> planner -> supervisor
    supervisor --Command(update=..., goto=[Send("worker", ...)])--> worker
    supervisor --(no work left)--> replan
    worker --> collector
    collector --> supervisor   (loop)
    replan --(evidence gap + replans left)--> supervisor
    replan --(enough evidence / replans exhausted)--> writer
    writer -> END

Phase 3 additions vs phase 2:
- Optional `checkpointer` (MemorySaver or AsyncSqliteSaver).
- Optional `budget` (BudgetManager) threaded through workers.
- A bounded `replan_node` that adds at most `max_replans` follow-up tasks
  when facts are sparse.
"""

from __future__ import annotations

from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, Send

from agents.budget import BudgetManager
from agents.planner import HeuristicPlanner, Planner
from agents.supervisor import decide_dispatch, fail_blocked_tasks
from agents.worker import WorkerNode
from core.exceptions import ConfigurationError
from domain.models import Citation, Fact
from graph.state import (
    SubTask,
    ensure_valid_plan,
    merge_citations,
    merge_errors,
    merge_facts,
    merge_subtasks,
)
from service.report_writer import MarkdownReportWriter


class GraphState(TypedDict, total=False):
    """Top-level graph state with reducer-annotated list fields."""

    query: str
    user_context: str
    research_depth: str
    subtasks: Annotated[list[SubTask], merge_subtasks]
    facts: Annotated[list[Fact], merge_facts]
    citations: Annotated[list[Citation], merge_citations]
    errors: Annotated[list[str], merge_errors]
    report_markdown: str
    replan_count: int


def build_graph(
    *,
    worker: WorkerNode,
    max_workers: int = 3,
    planner: Planner | None = None,
    report_writer: MarkdownReportWriter | None = None,
    budget: BudgetManager | None = None,
    checkpointer: Any | None = None,
    min_facts_to_stop_replan: int = 2,
) -> Any:
    """Compile the Supervisor-Worker graph.

    Args:
        worker: shared WorkerNode (has its own BudgetManager).
        max_workers: concurrency cap; must be a positive integer.
        planner: any object implementing the unified Planner protocol
            (``decompose(query, *, user_context, research_depth)``).
        report_writer: markdown writer.
        budget: global BudgetManager (replan cap lives here).
        checkpointer: optional LangGraph checkpointer (MemorySaver / AsyncSqliteSaver).
        min_facts_to_stop_replan: if facts < this after a wave, replan once.
    """
    if max_workers <= 0:
        raise ConfigurationError(f"max_workers must be a positive integer, got {max_workers!r}")
    planner = planner or HeuristicPlanner()
    writer = report_writer or MarkdownReportWriter()
    budget = budget or BudgetManager()

    graph = StateGraph(GraphState)

    async def planner_node(state: dict) -> Command:
        subtasks = await planner.decompose(
            state["query"],
            user_context=state.get("user_context", ""),
            research_depth=state.get("research_depth", "standard"),
        )
        # Structural validation BEFORE dispatch: unknown deps / cycles / dup ids
        # fail fast with InvalidPlanError instead of a graph recursion blow-up.
        ensure_valid_plan(subtasks)
        return Command(update={"subtasks": subtasks})

    async def supervisor_node(state: dict) -> Command:
        subtasks = fail_blocked_tasks(state["subtasks"])
        decision = decide_dispatch(subtasks, max_workers=max_workers)
        if decision.action == "finish":
            return Command(goto="replan")
        if not decision.dispatch:
            return Command(goto="collector")

        by_id = {t.task_id: t for t in subtasks}
        running_updates: list[SubTask] = []
        sends: list[Send] = []
        # Thread user_context through to the worker so it survives the long
        # research loop (search query + extraction prompt) instead of living
        # only in the top-level state.
        user_context = state.get("user_context", "")
        for tid in decision.dispatch:
            t = by_id[tid]
            running_updates.append(t.model_copy(update={"status": "running"}))
            sends.append(Send("worker", {"task": t, "user_context": user_context}))
        return Command(update={"subtasks": running_updates}, goto=sends)

    async def collector_node(state: dict) -> Command:
        subtasks = fail_blocked_tasks(state["subtasks"])
        decision = decide_dispatch(subtasks, max_workers=max_workers)
        old_by_id = {t.task_id: t for t in state["subtasks"]}
        updates: list[SubTask] = []
        for t in subtasks:
            if t.status != "failed":
                continue
            old = old_by_id.get(t.task_id)
            if old is None or old.status == "failed":
                continue
            updates.append(t)
        if decision.action == "finish":
            return Command(update={"subtasks": updates} if updates else {}, goto="replan")
        return Command(
            update={"subtasks": updates} if updates else {},
            goto="supervisor",
        )

    async def replan_node(state: dict) -> Command:
        # All tasks terminal. Decide whether to add a follow-up.
        n_facts = len(state["facts"])
        already = state.get("replan_count", 0)

        # Account/auth failures cannot be repaired by rephrasing the query.
        # Stop before creating another paid/search attempt.
        terminal_provider_failure = any(
            t.stop_reason in {"quota_exhausted", "auth_permission"}
            for t in state["subtasks"]
        )
        if terminal_provider_failure:
            return Command(goto="writer")

        # Boundary 7 + 4: the check-can-replan -> record-replan critical section
        # runs under the budget lock so concurrent replan requests cannot exceed
        # the cap.
        async with budget.replan_guard():
            # Budget gate: stop replanning when facts are sufficient, when the
            # replan count cap is hit, OR when the remaining token budget is gone.
            if n_facts >= min_facts_to_stop_replan or not budget.can_replan():
                return Command(goto="writer")

            # Structured gap association: name the terminal subtasks that produced
            # ZERO facts as the concrete evidence gap (not a generic "search
            # again" and not a fragile string parse of result_summary).
            zero_fact: list[SubTask] = []
            for t in state["subtasks"]:
                # A task produced zero facts iff its summary starts with
                # "0 facts". This is a deterministic, observable signal; the
                # STRUCTURAL linkage is carried on the new subtask, not parsed
                # back out of the description.
                if (t.result_summary or "").strip().startswith("0 facts"):
                    zero_fact.append(t)

            source_ids = sorted({t.task_id for t in zero_fact})
            if source_ids:
                gap_labels = sorted({t.perspective or t.title for t in zero_fact})
                gap_reason = "zero_fact_wave: " + ", ".join(gap_labels)
                description = (
                    f"evidence gap: no facts yet for [{', '.join(gap_labels)}]; "
                    f"deep dive into: {state['query']}"
                )
            else:
                gap_reason = "sparse_facts_after_wave"
                description = f"deep dive: {state['query']} (evidence gap after wave)"

            # Structural-hash dedup (boundary 7): never append a follow-up whose
            # gap (source task ids + reason) already exists. This is robust to
            # description rewording.
            gap_hash = "|".join(source_ids) + "#" + gap_reason
            existing_hashes = {
                "|".join(t.gap_source_task_ids) + "#" + t.gap_reason
                for t in state["subtasks"]
                if t.gap_source_task_ids or t.gap_reason
            }
            if gap_hash in existing_hashes:
                return Command(goto="writer")

            # Add one follow-up subtask. task_id must match ^task_\d+$.
            next_id_num = (
                max(
                    (int(t.task_id.split("_")[1]) for t in state["subtasks"]),
                    default=0,
                )
                + 1
            )
            new_task = SubTask(
                task_id=f"task_{next_id_num}",
                title=f"follow-up {next_id_num}",
                description=description,
                depends_on=[],
                priority=99,
                replan_of=source_ids[0] if source_ids else "",
                gap_reason=gap_reason,
                gap_source_task_ids=source_ids,
            )
            budget.record_replan()
            return Command(
                update={"subtasks": [new_task], "replan_count": already + 1},
                goto="supervisor",
            )

    async def worker_node(payload: dict) -> Command:
        task: SubTask = payload["task"]
        user_context = payload.get("user_context", "")
        return await worker.run_task(task, user_context=user_context)

    async def writer_node(state: dict) -> Command:
        md = writer.render(
            query=state["query"],
            facts=state["facts"],
            citations=state["citations"],
        )
        return Command(update={"report_markdown": md})

    graph.add_node("planner", planner_node)  # type: ignore[type-var]
    graph.add_node("supervisor", supervisor_node)  # type: ignore[type-var]
    graph.add_node("worker", worker_node)  # type: ignore[arg-type]
    graph.add_node("collector", collector_node)  # type: ignore[type-var]
    graph.add_node("replan", replan_node)  # type: ignore[type-var]
    graph.add_node("writer", writer_node)  # type: ignore[type-var]

    graph.add_edge(START, "planner")
    graph.add_edge("planner", "supervisor")
    graph.add_edge("worker", "collector")
    graph.add_edge("writer", END)

    return graph.compile(checkpointer=checkpointer)


__all__ = ["GraphState", "build_graph"]
