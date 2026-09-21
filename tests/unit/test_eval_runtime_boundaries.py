"""Regression tests for paid-eval wall-clock and cancellation boundaries."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from evals.adapter import EvalQuestion, EvalResult
from evals.configs import CONFIGS
from evals.failures import classify_failure
from evals.runner import EvalRunner

from agents.worker import WorkerNode
from api.runner import ProviderKit, ResearchRunner, _fake_kit
from api.task_store import TaskStore
from core.config import Settings


@pytest.mark.asyncio
async def test_question_timeout_cancels_answerer_and_persists_failure(
    tmp_path: Path,
) -> None:
    started = asyncio.Event()
    cleaned = asyncio.Event()

    async def hanging_answerer(_prompt):  # type: ignore[no-untyped-def]
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    question = EvalQuestion(qid="hang-1", question="q", reference_answer="")
    runner = EvalRunner(
        out_dir=tmp_path,
        config=CONFIGS["full"],
        questions=[question],
        answerer=hanging_answerer,
        question_timeout=0.05,
    )

    payload = await runner._process_one(question)

    assert started.is_set()
    assert cleaned.is_set()
    assert payload["status"] == "failed"
    assert payload["failure_category"] == "tool_failure"
    assert payload["error"] == "question_timeout after 0.05s"
    stored = json.loads((tmp_path / "results" / "hang-1.json").read_text(encoding="utf-8"))
    assert stored["error"] == payload["error"]


@pytest.mark.asyncio
async def test_worker_external_cancellation_reaps_event_wait_task() -> None:
    worker = WorkerNode()
    started = asyncio.Event()
    cleaned = asyncio.Event()

    async def body() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    outer = asyncio.create_task(worker._await_cancellable(body()))
    await started.wait()
    outer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await outer
    await asyncio.sleep(0)

    assert cleaned.is_set()
    leaked = {
        task.get_name()
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task()
        and task.get_name() in {"worker-cancel-wait", "worker-cancellable-body"}
    }
    assert leaked == set()


@pytest.mark.asyncio
async def test_research_timeout_records_stable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class HangingGraph:
        async def ainvoke(self, _inputs):  # type: ignore[no-untyped-def]
            await asyncio.Event().wait()

    monkeypatch.setattr("api.runner.build_graph", lambda **_kwargs: HangingGraph())
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        llm_provider="fake",
        research_timeout_seconds=0.02,
    )
    kit: ProviderKit = _fake_kit(settings)
    store = TaskStore()
    rec = store.create(query="q", user_context="", depth="quick")

    await ResearchRunner(store, settings=settings, kit=kit).run(rec)

    assert rec.status == "failed"
    assert rec.error == "research_timeout after 0.02s"


@pytest.mark.asyncio
async def test_research_runner_overrides_internal_worker_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, int] = {}

    class EmptyGraph:
        async def ainvoke(self, _inputs):  # type: ignore[no-untyped-def]
            return {"facts": [], "citations": []}

    def fake_build_graph(*, worker, budget, max_workers):  # type: ignore[no-untyped-def]
        captured["max_workers"] = max_workers
        return EmptyGraph()

    monkeypatch.setattr("api.runner.build_graph", fake_build_graph)
    settings = Settings(_env_file=None, llm_provider="fake", max_workers=6)  # type: ignore[call-arg]
    store = TaskStore()
    rec = store.create(query="q", user_context="", depth="quick")

    await ResearchRunner(
        store,
        settings=settings,
        kit=_fake_kit(settings),
        max_workers=1,
    ).run(rec)

    assert captured["max_workers"] == 1


def test_provider_errors_are_tool_failures() -> None:
    result = EvalResult(
        qid="q",
        status="failed",
        error="provider_error: upstream unavailable",
    )
    assert classify_failure(result) == "tool_failure"
