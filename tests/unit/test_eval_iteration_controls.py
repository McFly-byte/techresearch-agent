from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from evals.adapter import DeepResearchBench2Adapter, EvalQuestion
from evals.configs import CONFIGS
from evals.runner import EvalRunner, LangSmithExperimentSink
from evals.subset import apply_subset, load_subset

from core.deadlines import run_with_hard_timeout
from core.usage import capture_usage, record_llm_usage, usage_stage


def test_core10_manifest_is_fixed_and_matches_official_dataset() -> None:
    dataset = Path("evals/data/deepresearch_bench_ii/tasks_and_rubrics.jsonl")
    adapter = DeepResearchBench2Adapter(dataset)
    manifest = load_subset("core10")
    selected = apply_subset(adapter.questions(), manifest, dataset_hash=adapter.dataset_sha256())
    assert len(selected) == 10
    assert [q.qid for q in selected] == list(manifest.qids)
    assert len({q.language for q in selected}) == 2
    assert len({q.theme for q in selected}) == 10


@pytest.mark.asyncio
async def test_hard_timeout_returns_when_child_suppresses_cancellation() -> None:
    release = asyncio.Event()
    finished = asyncio.Event()

    async def stubborn() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()
        finally:
            finished.set()

    started = time.monotonic()
    with pytest.raises(TimeoutError, match="question_timeout after 0.01s"):
        await run_with_hard_timeout(
            stubborn(), timeout=0.01, label="question_timeout", cancel_grace=0.01
        )
    assert time.monotonic() - started < 0.2
    release.set()
    await asyncio.wait_for(finished.wait(), timeout=0.2)


@pytest.mark.asyncio
async def test_question_concurrency_is_bounded_and_results_keep_order(tmp_path: Path) -> None:
    active = 0
    peak = 0
    lock = asyncio.Lock()

    async def answerer(prompt):  # type: ignore[no-untyped-def]
        nonlocal active, peak
        async with lock:
            active += 1
            peak = max(peak, active)
        await asyncio.sleep(0.03)
        async with lock:
            active -= 1
        return f"answer {prompt.qid}", 1, 3

    questions = [
        EvalQuestion(qid=f"q{i}", question=f"question {i}", reference_answer="") for i in range(3)
    ]
    runner = EvalRunner(
        out_dir=tmp_path,
        config=CONFIGS["full"],
        questions=questions,
        answerer=answerer,
        concurrency=2,
    )
    results = await runner.run(max_questions=3)
    assert peak == 2
    assert [result["qid"] for result in results] == ["q0", "q1", "q2"]
    assert not list((tmp_path / "results").glob("*.tmp"))


@pytest.mark.asyncio
async def test_usage_is_isolated_by_question_and_stage() -> None:
    async def one(qid: str, n: int) -> dict[str, object]:
        with capture_usage(qid) as collector:
            record_llm_usage(
                provider="qwen", model="m", input_tokens=n, output_tokens=1, estimated=False
            )
            with usage_stage("judge"):
                await asyncio.sleep(0)
                record_llm_usage(
                    provider="qwen",
                    model="m",
                    input_tokens=n * 10,
                    output_tokens=2,
                    estimated=False,
                )
        return collector.snapshot()

    left, right = await asyncio.gather(one("left", 3), one("right", 7))
    assert left["total_tokens"] == 36
    assert right["total_tokens"] == 80
    assert left["by_stage"]["research"]["input_tokens"] == 3  # type: ignore[index]
    assert right["by_stage"]["judge"]["input_tokens"] == 70  # type: ignore[index]


class _DatasetClient:
    def __init__(self) -> None:
        self.created = 0
        self.read = 0
        self.project = SimpleNamespace(
            id="exp-id", name="core10-experiment", url="https://smith.example/projects/p/exp-id"
        )

    def list_datasets(self, **_kwargs):  # type: ignore[no-untyped-def]
        return [SimpleNamespace(id="dataset-id", name="dataset")]

    def list_examples(self, **_kwargs):  # type: ignore[no-untyped-def]
        return [SimpleNamespace(id="ex", inputs={"qid": "q1"})]

    def create_project(self, name, **_kwargs):  # type: ignore[no-untyped-def]
        self.created += 1
        self.project.name = name
        return self.project

    def read_project(self, **_kwargs):  # type: ignore[no-untyped-def]
        self.read += 1
        return self.project


def test_experiment_manifest_reuses_exact_project(tmp_path: Path) -> None:
    client = _DatasetClient()
    subset = {"name": "drb2_core10", "qid_sha256": "qid-hash"}
    sink = LangSmithExperimentSink(
        experiment_prefix="core10-experiment",
        dataset_name="dataset",
        dataset_hash="dataset-hash",
        subset_metadata=subset,
        concurrency=2,
        client=client,
    )
    runner = EvalRunner(
        out_dir=tmp_path,
        config=CONFIGS["full"],
        questions=[],
        dataset_hash="dataset-hash",
        subset_metadata=subset,
    )
    first, manifest = sink._load_or_create_experiment(out=tmp_path, qids=["q1"], runner=runner)
    second, loaded = sink._load_or_create_experiment(out=tmp_path, qids=["q1"], runner=runner)
    assert first.id == second.id == "exp-id"
    assert manifest["experiment_name"] == "core10-experiment"
    assert loaded["experiment_id"] == "exp-id"
    assert client.created == 1
    assert client.read == 1
    persisted = json.loads((tmp_path / "experiment.json").read_text(encoding="utf-8"))
    assert persisted["concurrency"] == 2
