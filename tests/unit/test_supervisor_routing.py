"""Unit tests for the pure Supervisor routing function."""

from __future__ import annotations

from agents.supervisor import decide_dispatch, fail_blocked_tasks
from graph.state import SubTask


def _t(tid: str, status: str = "pending", deps: list[str] | None = None, prio: int = 0) -> SubTask:
    return SubTask(
        task_id=tid,
        title=tid,
        description=tid,
        depends_on=deps or [],
        status=status,  # type: ignore[arg-type]
        priority=prio,
    )


def test_finish_when_no_pending():  # type: ignore[no-untyped-def]
    decision = decide_dispatch([_t("task_1", "completed")], max_workers=3)
    assert decision.action == "finish"


def test_dispatch_all_free():  # type: ignore[no-untyped-def]
    decision = decide_dispatch([_t("task_1"), _t("task_2"), _t("task_3")], max_workers=3)
    assert decision.action == "dispatch"
    assert decision.dispatch == ["task_1", "task_2", "task_3"]


def test_max_workers_cap():  # type: ignore[no-untyped-def]
    decision = decide_dispatch([_t(f"task_{i}") for i in range(1, 6)], max_workers=2)
    assert decision.action == "dispatch"
    assert len(decision.dispatch) == 2


def test_dependency_blocks_task():  # type: ignore[no-untyped-def]
    state = [
        _t("task_1", "completed"),
        _t("task_2", "pending", deps=["task_1"]),
        _t("task_3", "pending", deps=["task_2"]),
    ]
    decision = decide_dispatch(state, max_workers=3)
    assert decision.action == "dispatch"
    assert decision.dispatch == ["task_2"]  # task_3 still blocked


def test_wait_when_no_deps_met():  # type: ignore[no-untyped-def]
    state = [
        _t("task_1", "pending", deps=["task_9"]),  # task_9 doesn't exist
    ]
    decision = decide_dispatch(state, max_workers=2)
    assert decision.action == "wait"


def test_running_count_consumes_slot():  # type: ignore[no-untyped-def]
    state = [
        _t("task_1", "running"),
        _t("task_2", "running"),
        _t("task_3", "pending"),
    ]
    decision = decide_dispatch(state, max_workers=2)
    assert decision.action == "wait"


def test_failed_dependency_marks_downstream_failed():  # type: ignore[no-untyped-def]
    state = [
        _t("task_1", "failed"),
        _t("task_2", "pending", deps=["task_1"]),
    ]
    updated = fail_blocked_tasks(state)
    t2 = next(t for t in updated if t.task_id == "task_2")
    assert t2.status == "failed"
    assert "dependency" in t2.error


def test_deterministic_priority_order():  # type: ignore[no-untyped-def]
    state = [
        _t("task_2", prio=5),
        _t("task_1", prio=1),
        _t("task_3", prio=3),
    ]
    decision = decide_dispatch(state, max_workers=2)
    assert decision.dispatch == ["task_1", "task_3"]
