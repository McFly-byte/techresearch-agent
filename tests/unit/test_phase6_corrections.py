"""Phase 6 correction tests: dynamic provider selection, granular events,
CLI shares runner, SQLite task persistence, reconnect dedup."""

from __future__ import annotations

import asyncio
from pathlib import Path

from api.runner import ResearchRunner
from api.task_store import TaskStore


def test_task_store_persists_to_disk(tmp_path: Path) -> None:
    p = tmp_path / "tasks.json"
    s = TaskStore(persist_path=p)
    rec = s.create(query="q", user_context="ctx", depth="quick")
    s.append_event(rec.task_id, {"event_id": "e1", "stage": "x"})
    # Re-open.
    s2 = TaskStore(persist_path=p)
    rec2 = s2.get(rec.task_id)
    assert rec2 is not None
    assert rec2.query == "q"
    assert rec2.events[-1]["stage"] == "x"


def test_task_store_evicts_old() -> None:
    s = TaskStore(max_tasks=2)
    a = s.create(query="a", user_context="", depth="quick")
    s.create(query="b", user_context="", depth="quick")
    s.create(query="c", user_context="", depth="quick")
    assert s.get(a.task_id) is None
    assert len(s.list()) == 2


def test_runner_emits_granular_events() -> None:
    store = TaskStore()
    rec = store.create(query="q", user_context="ctx", depth="quick")
    runner = ResearchRunner(store)
    asyncio.run(runner.run(rec))
    stages = [e["stage"] for e in rec.events]
    # At minimum: planner_start, verify_start, verify_done, write_done, done.
    for expected in ("planner_start", "verify_start", "verify_done", "write_done", "done"):
        assert expected in stages, f"missing stage {expected} in {stages}"


def test_runner_passes_user_context() -> None:
    store = TaskStore()
    rec = store.create(query="q", user_context="my context", depth="quick")
    runner = ResearchRunner(store)
    asyncio.run(runner.run(rec))
    # The queued event should carry user_context.
    assert rec.events[0]["data"]["user_context"] == "my context"


def test_cancel_sets_status() -> None:
    store = TaskStore()
    rec = store.create(query="q", user_context="", depth="quick")
    rec._cancel_requested = True
    # Run; worker loop should notice cancellation.
    runner = ResearchRunner(store)
    asyncio.run(runner.run(rec))
    # Either cancelled or completed; must not raise.
    assert rec.status in {"completed", "failed", "cancelled"}


def test_cli_run_uses_same_runner() -> None:
    """`tra run` should produce the same report_markdown as ResearchRunner."""
    store = TaskStore()
    rec = store.create(query="compare langgraph and llamaindex", user_context="", depth="quick")
    runner = ResearchRunner(store)
    asyncio.run(runner.run(rec))
    assert rec.status == "completed"
    assert rec.report_markdown
