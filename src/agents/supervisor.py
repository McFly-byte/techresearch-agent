"""Self-rolled Supervisor routing logic.

This module is intentionally a set of PURE functions over (subtasks, max_workers).
The LangGraph node is a thin shell around `decide_dispatch()`. Keeping the
router pure lets us unit-test parallelism, dependencies, concurrency caps and
failure isolation without spinning up the graph.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.exceptions import ConfigurationError
from graph.state import SubTask

DispatchAction = str  # "dispatch" | "wait" | "finish"


@dataclass(frozen=True)
class Decision:
    """What the supervisor wants to do this round."""

    action: DispatchAction
    dispatch: list[str]  # task_ids to send to workers now
    reason: str = ""

    def __bool__(self) -> bool:  # convenience in tests
        return bool(self.dispatch)


def _deps_met(task: SubTask, done: set[str]) -> bool:
    return all(dep in done for dep in task.depends_on)


def decide_dispatch(
    subtasks: list[SubTask],
    *,
    max_workers: int,
) -> Decision:
    """Pure routing function.

    Rules (evaluated in order):
    0. ``max_workers <= 0`` is a configuration error, NOT a silent finish.
    1. No pending tasks:
       - if some task is still ``running`` -> ``wait`` (in-flight work),
       - otherwise every task is terminal (completed/failed) -> ``finish``.
         This also covers the deadlock case "no pending, no running, only
         failed/blocked": we finish rather than wait forever.
    2. Pending tasks whose deps are all in `completed` -> candidates.
       Sort by (priority, task_id) for determinism.
    3. Only dispatch up to `max_workers - running_count` of them.
    4. If candidates exist but all slots are busy -> wait.
    5. If candidates exist but their deps are not met (blocked) -> wait.
    """
    if max_workers <= 0:
        raise ConfigurationError(f"max_workers must be a positive integer, got {max_workers!r}")

    pending = [t for t in subtasks if t.status == "pending"]
    running = [t for t in subtasks if t.status == "running"]

    if not pending:
        if running:
            return Decision(
                action="wait",
                dispatch=[],
                reason=f"{len(running)} task(s) still running",
            )
        # Nothing pending and nothing in flight: all terminal -> finish.
        return Decision(action="finish", dispatch=[], reason="no pending tasks")

    # Dependency satisfaction: a dep is "met" when it reached a successful or
    # partial terminal (it DID finish, just possibly without all facts). It is
    # "blocking" when it failed or was cancelled — downstream can never run.
    done = {t.task_id for t in subtasks if t.status in {"completed", "partial"}}
    failed = {t.task_id for t in subtasks if t.status in {"failed", "cancelled"}}

    # Candidates: pending + deps met + deps not all failed (otherwise blocked).
    candidates: list[SubTask] = []
    for t in pending:
        if not _deps_met(t, done):
            continue
        if any(dep in failed for dep in t.depends_on):
            # A dependency failed; this task can never run. Mark it failed via
            # the caller; here we just don't dispatch it.
            continue
        candidates.append(t)

    candidates.sort(key=lambda t: (t.priority, t.task_id))

    free_slots = max_workers - len(running)
    if free_slots <= 0:
        return Decision(
            action="wait",
            dispatch=[],
            reason=f"all {len(running)} workers busy",
        )
    if not candidates:
        return Decision(
            action="wait",
            dispatch=[],
            reason="no pending task with satisfied dependencies",
        )

    chosen = [t.task_id for t in candidates[:free_slots]]
    return Decision(action="dispatch", dispatch=chosen, reason=f"dispatching {len(chosen)}")


def fail_blocked_tasks(subtasks: list[SubTask]) -> list[SubTask]:
    """Mark pending tasks whose deps are permanently non-runnable as terminal.

    A dependency that ``failed`` marks the dependent ``failed``; a dependency
    that was ``cancelled`` marks the dependent ``cancelled`` (cancellation
    propagates). A ``partial`` upstream is treated as satisfied (it did finish).

    Returns a NEW list (does not mutate input). Used by the supervisor node
    before deciding, so a failed/cancelled upstream dependency propagates
    without blocking the whole graph forever.
    """
    failed = {t.task_id for t in subtasks if t.status == "failed"}
    cancelled = {t.task_id for t in subtasks if t.status == "cancelled"}
    out: list[SubTask] = []
    changed = False
    for t in subtasks:
        if t.status == "pending" and any(dep in cancelled for dep in t.depends_on):
            out.append(
                t.model_copy(
                    update={
                        "status": "cancelled",
                        "error": "blocked_by_cancelled_dependency",
                        "stop_reason": "cancelled",
                    }
                )
            )
            changed = True
        elif t.status == "pending" and any(dep in failed for dep in t.depends_on):
            out.append(
                t.model_copy(
                    update={
                        "status": "failed",
                        "error": "blocked_by_failed_dependency",
                    }
                )
            )
            changed = True
        else:
            out.append(t)
    if not changed:
        return subtasks
    return out


__all__ = ["Decision", "decide_dispatch", "fail_blocked_tasks"]
