"""Tests for Planner: depth budgets, structure, and parse-failure recovery."""

from __future__ import annotations

import json

import pytest

from agents.planner import (
    DEPTH_COUNTS,
    HeuristicPlanner,
    PlannerParseError,
    RetryingPlanner,
)
from graph.state import SubTask


@pytest.mark.asyncio
async def test_depth_budget_quick_standard_deep():  # type: ignore[no-untyped-def]
    p = HeuristicPlanner()
    for depth, n in DEPTH_COUNTS.items():
        tasks = await p.decompose("LangGraph vs LlamaIndex", research_depth=depth)
        assert len(tasks) == n, f"{depth}: expected {n}, got {len(tasks)}"


@pytest.mark.asyncio
async def test_vs_query_emits_synthesis_dependency():  # type: ignore[no-untyped-def]
    p = HeuristicPlanner()
    tasks = await p.decompose("A vs B", research_depth="standard")
    # standard depth = 4 sub-tasks: two per-side + two synthesis tasks.
    assert len(tasks) == 4
    # Every synthesis task depends on BOTH per-side tasks.
    for t in tasks[2:]:
        assert t.depends_on == ["task_1", "task_2"]
    # The two per-side tasks are independent and run in the first wave.
    assert tasks[0].depends_on == []
    assert tasks[1].depends_on == []


@pytest.mark.asyncio
async def test_all_task_ids_unique_and_well_formed():  # type: ignore[no-untyped-def]
    p = HeuristicPlanner()
    tasks = await p.decompose("RAG survey", research_depth="deep")
    ids = [t.task_id for t in tasks]
    assert len(ids) == len(set(ids))
    for t in tasks:
        # pattern r'^task_\d+$' is enforced by pydantic
        SubTask.model_validate(t.model_dump())


def test_retrying_planner_recovers_on_second_try():  # type: ignore[no-untyped-def]
    import asyncio

    calls = {"n": 0}

    def parser(raw: str) -> list[SubTask]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("bad json")
        return [SubTask(task_id="task_1", title="x", description="x")]

    rp = RetryingPlanner(parser)
    out = asyncio.run(rp.decompose("raw"))
    assert len(out) == 1
    assert calls["n"] == 2


def test_retrying_planner_raises_after_two_failures():  # type: ignore[no-untyped-def]
    import asyncio

    def parser(raw: str) -> list[SubTask]:
        raise ValueError("still bad")

    rp = RetryingPlanner(parser)
    with pytest.raises(PlannerParseError):
        asyncio.run(rp.decompose("raw"))


def _strict_json_plan_parser(raw: str) -> list[SubTask]:
    """A stand-in for an LLM-output parser that enforces structure.

    Raises ValueError (triggering the one controlled retry) when the raw string
    is not a JSON object carrying a list of task objects with the required
    fields. This mirrors the contract a real structured-output planner uses.
    """
    data = json.loads(raw)
    if not isinstance(data, dict) or not isinstance(data.get("subtasks"), list):
        raise ValueError("root must be an object with a 'subtasks' list")
    out: list[SubTask] = []
    for item in data["subtasks"]:
        if not isinstance(item, dict) or "title" not in item:
            raise ValueError("each task must be an object with a title")
        out.append(
            SubTask(
                task_id=item.get("task_id", "task_1"),
                title=item["title"],
                description=item.get("description", item["title"]),
            )
        )
    return out


def test_retrying_planner_recovers_from_nonobject_missing_field():  # type: ignore[no-untyped-def]
    import asyncio

    calls = {"n": 0}

    def parser(raw: str) -> list[SubTask]:
        calls["n"] += 1
        if calls["n"] == 1:
            # First attempt: invalid structure (a bare array, missing root obj).
            raise ValueError("non-object root / missing subtasks field")
        # Second attempt: the repaired payload is valid.
        assert "PARSE_ERROR_REPAIR" in raw
        return _strict_json_plan_parser(
            json.dumps({"subtasks": [{"title": "x", "task_id": "task_1"}]})
        )

    rp = RetryingPlanner(parser)
    out = asyncio.run(rp.decompose("garbage"))
    assert calls["n"] == 2
    assert [t.task_id for t in out] == ["task_1"]


def test_strict_parser_rejects_missing_required_field():  # type: ignore[no-untyped-def]
    # A task missing its required 'title' must be rejected by the parser.
    with pytest.raises(ValueError):
        _strict_json_plan_parser(json.dumps({"subtasks": [{"task_id": "task_1"}]}))
    # A non-JSON string must also be rejected.
    with pytest.raises(ValueError):
        _strict_json_plan_parser("this is not json")


def test_retrying_planner_propagates_when_repair_still_invalid():  # type: ignore[no-untyped-def]
    import asyncio

    # Even the repaired payload fails validation -> PlannerParseError.
    rp = RetryingPlanner(_strict_json_plan_parser)
    with pytest.raises(PlannerParseError):
        asyncio.run(rp.decompose("not json at all"))
