"""Unit tests for the eval pipeline fixes (offline, no paid calls).

Covers:
- EvalPrompt answerer isolation (no reference_answer leak).
- LLMRubricJudge with a mock LLM (per-rubric 0/1, retry/parse-failure path).
- ResearchRunner answerer in fake mode (real shipped system, offline).
- Resume: completed skipped, failed RE-RUN.
- Adapter schema validation + dataset sha256.
- CLI smoke slice (3 questions without --confirm-full).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from evals.adapter import (
    DeepResearchBench2Adapter,
    EvalPrompt,
    FixtureDataset,
    sha256_file,
)
from evals.baselines import make_research_runner_answerer
from evals.configs import CONFIGS
from evals.metrics import LLMRubricJudge
from evals.runner import EvalRunner


# --- Mock LLM for the batch judge -------------------------------------------
class _MockBatchLLM:
    """按请求返回批量评分 JSON 数组（离线、确定性）。

    ``score_sequence`` 是跨所有批次按顺序消费的 1/0 序列；每个请求根据用户消息
    里带序号的 rubric 条数，依次从序列里取值，构造 ``[{index, score, reason}]``。
    """

    model_id = "mock-1"

    def __init__(self, score_sequence: list[int] | None = None) -> None:
        self._seq = list(score_sequence if score_sequence is not None else [1])
        self._pos = 0
        # 记录每次 LLM 请求里评了几条 rubric（用于断言分批）。
        self.requests: list[int] = []

    async def acomplete(self, messages, *, model_id=None, max_tokens=None):  # type: ignore[no-untyped-def]
        from core.providers.base import LLMResponse

        self.max_tokens_seen = max_tokens
        user = messages[-1].content
        n = sum(
            1
            for line in user.splitlines()
            if line.strip() and line.strip()[0].isdigit() and ". " in line
        )
        self.requests.append(n)
        items = []
        for i in range(n):
            val = self._seq[self._pos] if self._pos < len(self._seq) else 0
            self._pos += 1
            items.append({"index": i + 1, "score": val, "reason": f"reason {i + 1}"})
        return LLMResponse(text=json.dumps(items), model="mock-1", provider="mock")


class _BrokenBatchLLM:
    """永远返回无法解析的文本（整批解析失败路径）。"""

    model_id = "broken-1"
    requests: list[int] = []

    async def acomplete(self, messages, *, model_id=None, max_tokens=None):  # type: ignore[no-untyped-def]
        from core.providers.base import LLMResponse

        return LLMResponse(text="not json at all", model="broken-1", provider="broken")


class _ScriptedBatchLLM:
    """按请求顺序返回预设响应文本，离线、确定性；记录 max_tokens 与请求条数。

    用于测试解析器容错（包装/NDJSON/截断）与单项重试的有界性。
    """

    model_id = "scripted-1"

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self._pos = 0
        self.requests: list[int] = []
        self.max_tokens_calls: list[object] = []

    async def acomplete(self, messages, *, model_id=None, max_tokens=None):  # type: ignore[no-untyped-def]
        from core.providers.base import LLMResponse

        self.max_tokens_calls.append(max_tokens)
        user = messages[-1].content
        n = sum(
            1
            for line in user.splitlines()
            if line.strip() and line.strip()[0].isdigit() and ". " in line
        )
        self.requests.append(n)
        text = self._responses[self._pos] if self._pos < len(self._responses) else "not json"
        self._pos += 1
        return LLMResponse(text=text, model="scripted-1", provider="scripted")


# --- 任务1: 三态批量评分 ----------------------------------------------------
@pytest.mark.asyncio
async def test_batch_judge_three_state_scores():
    judge = LLMRubricJudge(llm=_MockBatchLLM([1, 0, 1]))
    score, per_item, reason = await judge.score("ans", ["r1", "r2", "r3"])
    assert score == pytest.approx(2 / 3)
    assert [p["score"] for p in per_item] == [1, 0, 1]
    assert "2/3" in reason


@pytest.mark.asyncio
async def test_batch_judge_groups_into_batches():
    # 55 条 rubric，批大小 50 -> 2 次请求（50 + 5）。
    llm = _MockBatchLLM([1] * 55)
    judge = LLMRubricJudge(llm=llm, batch_size=50, max_retries=0)
    score, per_item, _ = await judge.score("ans", [f"r{i}" for i in range(55)])
    assert llm.requests == [50, 5]
    assert len(per_item) == 55
    assert score == 1.0


@pytest.mark.asyncio
async def test_batch_judge_blocked_scores_minus_one_no_llm():
    llm = _MockBatchLLM([1, 1])
    judge = LLMRubricJudge(llm=llm, max_retries=0)
    score, per_item, reason = await judge.score(
        "ans", ["r_a", "r_blocked", "r_c"], blocked=["r_blocked"]
    )
    by_text = {p["rubric"]: p for p in per_item}
    # blocked 项直接 -1，不进 LLM。
    assert by_text["r_blocked"]["score"] == -1
    assert by_text["r_blocked"]["reason"] == "blocked"
    # LLM 只评了 r_a / r_c 两条。
    assert llm.requests == [2]
    # 非 blocked 都 pass -> 1.0；blocked 不计入分母。
    assert score == 1.0
    assert "1 blocked" in reason


@pytest.mark.asyncio
async def test_batch_judge_mean_excludes_blocked():
    # 4 条 rubric，1 条 blocked，live 三条得分 1/1/0 -> 均值 2/3。
    llm = _MockBatchLLM([1, 1, 0])
    judge = LLMRubricJudge(llm=llm, max_retries=0)
    score, per_item, _ = await judge.score(
        "ans", ["a", "b", "c", "d"], blocked=["d"]
    )
    assert score == pytest.approx(2 / 3)
    assert len(per_item) == 4
    assert per_item[3]["score"] == -1


@pytest.mark.asyncio
async def test_batch_judge_dimensions_propagated():
    llm = _MockBatchLLM([1, 0])
    judge = LLMRubricJudge(llm=llm, max_retries=0)
    score, per_item, _ = await judge.score(
        "ans", ["r1", "r2"], dimensions=["info_recall", "analysis"]
    )
    assert per_item[0]["dimension"] == "info_recall"
    assert per_item[1]["dimension"] == "analysis"


@pytest.mark.asyncio
async def test_batch_judge_empty_rubrics():
    judge = LLMRubricJudge(llm=_MockBatchLLM())
    score, per_item, reason = await judge.score("answer", [])
    assert score == 1.0
    assert per_item == []
    assert "no rubrics" in reason


@pytest.mark.asyncio
async def test_batch_judge_parse_failure_scores_zero_no_crash():
    judge = LLMRubricJudge(llm=_BrokenBatchLLM(), max_retries=0, timeout=1.0)
    score, per_item, reason = await judge.score("answer", ["r1", "r2"])
    assert score == 0.0
    assert [p["score"] for p in per_item] == [0, 0]
    assert per_item[0]["reason"] == "parse_error"
    assert "0/2" in reason


@pytest.mark.asyncio
async def test_batch_judge_default_batch_size_is_twelve():
    # 30 条 rubric，默认 batch_size=12 -> 三次请求（12 + 12 + 6）。
    llm = _MockBatchLLM([1] * 30)
    judge = LLMRubricJudge(llm=llm, max_retries=0)
    score, per_item, _ = await judge.score("ans", [f"r{i}" for i in range(30)])
    assert llm.requests == [12, 12, 6]
    assert len(per_item) == 30
    assert score == 1.0


@pytest.mark.asyncio
async def test_batch_judge_parses_results_wrapper():
    # {"results": [...]} 包装形态。
    wrapped = json.dumps(
        {
            "results": [
                {"index": 1, "score": 1, "reason": "a"},
                {"index": 2, "score": 0, "reason": "b"},
                {"index": 3, "score": 1, "reason": "c"},
            ]
        }
    )
    llm = _ScriptedBatchLLM([wrapped, "not json", "not json"])
    judge = LLMRubricJudge(llm=llm, max_retries=0)
    score, per_item, _ = await judge.score("ans", ["r1", "r2", "r3"])
    assert [p["score"] for p in per_item] == [1, 0, 1]
    assert score == pytest.approx(2 / 3)


@pytest.mark.asyncio
async def test_batch_judge_parses_code_fence_and_ndjson():
    fenced = "```json\n" + json.dumps(
        [
            {"index": 1, "score": 1, "reason": "a"},
            {"index": 2, "score": 0, "reason": "b"},
        ]
    ) + "\n```"
    ndjson = (
        json.dumps({"index": 1, "score": 1, "reason": "x"}) + "\n"
        + json.dumps({"index": 2, "score": 1, "reason": "y"})
    )
    # 第一批代码块包装，第二批 NDJSON（batch_size=2 -> 2 批）。
    llm = _ScriptedBatchLLM([fenced, ndjson])
    judge = LLMRubricJudge(llm=llm, max_retries=0, batch_size=2)
    score, per_item, _ = await judge.score("ans", ["r1", "r2", "r3", "r4"])
    assert llm.requests == [2, 2]
    assert [p["score"] for p in per_item] == [1, 0, 1, 1]
    assert score == pytest.approx(3 / 4)


@pytest.mark.asyncio
async def test_batch_judge_partial_truncation_does_not_zero_whole_batch():
    # 输出被 max_tokens 截断：只完整返回前 3 条，第 4 条写到一半。
    truncated = (
        '[{"index":1,"score":1,"reason":"r1 ok"},'
        '{"index":2,"score":0,"reason":"r2 miss"},'
        '{"index":3,"score":1,"reason":"r3 ok"},'
        '{"index":4,"score":0,"rea'
    )
    # 单项重试（第 4、5 条）各返回一条合法 JSON。
    retry_4 = json.dumps([{"index": 1, "score": 1, "reason": "r4 retry"}])
    retry_5 = json.dumps([{"index": 1, "score": 1, "reason": "r5 retry"}])
    llm = _ScriptedBatchLLM([truncated, retry_4, retry_5])
    judge = LLMRubricJudge(llm=llm, max_retries=0)
    score, per_item, _ = await judge.score("ans", [f"r{i}" for i in range(5)])
    # 前 3 条来自截断批次本身（reason 是原文），证明整批没被记 0。
    assert per_item[0]["reason"] == "r1 ok"
    assert per_item[1]["reason"] == "r2 miss"
    assert per_item[2]["reason"] == "r3 ok"
    # 第 4、5 条通过单项重试恢复。
    assert [p["score"] for p in per_item] == [1, 0, 1, 1, 1]
    assert score == pytest.approx(4 / 5)
    # 1 次整批 + 2 次单项重试。
    assert llm.requests == [5, 1, 1]


@pytest.mark.asyncio
async def test_batch_judge_single_item_retry_is_bounded():
    # 整批永远失败：1 次整批 + 每个缺失项各 1 次单项重试 = 1 + 5 = 6 次请求，不无限重试。
    llm = _ScriptedBatchLLM(["not json"] * 10)
    judge = LLMRubricJudge(llm=llm, max_retries=0, timeout=1.0)
    score, per_item, reason = await judge.score("ans", [f"r{i}" for i in range(5)])
    assert [p["score"] for p in per_item] == [0] * 5
    assert all(p["reason"] == "parse_error" for p in per_item)
    assert score == 0.0
    assert llm.requests == [5, 1, 1, 1, 1, 1]


@pytest.mark.asyncio
async def test_batch_judge_passes_max_tokens_to_provider():
    llm = _MockBatchLLM([1, 0, 1])
    judge = LLMRubricJudge(llm=llm, max_retries=0)
    await judge.score("ans", ["r1", "r2", "r3"])
    # 3 条 rubric -> max_tokens = 3*300 + 500 = 1400。
    assert getattr(llm, "max_tokens_seen", None) == 1400


# --- P0-4: answerer isolation ----------------------------------------------
def test_eval_prompt_has_no_reference_fields():
    p = EvalPrompt(qid="q1", question="what?")
    assert not hasattr(p, "reference_answer")
    assert not hasattr(p, "rubrics")


# --- P0-1: ResearchRunner answerer (fake mode, real system) -----------------
@pytest.mark.asyncio
async def test_research_runner_answerer_fake_mode():
    answerer = make_research_runner_answerer(mode="fake")
    out = await answerer(EvalPrompt(qid="t1", question="Tell me about LangGraph."))
    # Production answerer now returns the rich dict (citations/usage/quality).
    assert isinstance(out, dict)
    assert isinstance(out["answer"], str) and len(out["answer"]) > 0
    assert isinstance(out["rounds"], int)
    assert isinstance(out["tokens"], int)
    assert isinstance(out["citations"], list)
    # Fake mode: no LLM synthesis, so no quality gate was run -> passed=True.
    assert out["quality_passed"] is True
    # Fake mode uses char-derived estimates.
    assert out["usage_estimated"] is True
    # Production answerer now exposes real provenance + usage to the eval.
    assert len(out["citations"]) > 0
    assert all("url" in c for c in out["citations"])
    assert out["tokens"] > 0


# --- P1-3: resume retries failed questions ---------------------------------
def test_resume_retries_failed_questions(tmp_path: Path):
    ds = FixtureDataset()
    state = {"calls": 0, "fail_once": True}

    def answerer(prompt):  # type: ignore[no-untyped-def]
        state["calls"] += 1
        if prompt.qid == "fx-002" and state["fail_once"]:
            state["fail_once"] = False
            raise RuntimeError("transient boom")
        return f"answer for {prompt.qid}", 1, 10

    out = tmp_path / "run"
    runner = EvalRunner(
        out_dir=out, config=CONFIGS["full"], questions=ds.questions(), answerer=answerer
    )
    results1 = runner.run_sync()
    by1 = {r["qid"]: r for r in results1}
    assert by1["fx-002"]["status"] == "failed"
    calls_after_first = state["calls"]

    # Resume: completed (fx-001, fx-003) skipped, failed (fx-002) RE-RUN.
    runner2 = EvalRunner(
        out_dir=out, config=CONFIGS["full"], questions=ds.questions(), answerer=answerer
    )
    results2 = runner2.run_sync()
    by2 = {r["qid"]: r for r in results2}
    assert by2["fx-001"]["status"] == "completed"  # loaded from disk
    assert by2["fx-002"]["status"] == "completed"  # retried now
    # Only fx-002 was re-answered (one new call).
    assert state["calls"] == calls_after_first + 1


# --- P1-4: adapter schema validation ---------------------------------------
def test_adapter_records_malformed_lines(tmp_path: Path):
    p = tmp_path / "drb2.jsonl"
    p.write_text(
        json.dumps({"prompt": "What is X?", "content": {"rubric": {"info_recall": ["names X"]}}})
        + "\n"
        + "this is not json\n"
        + json.dumps({"content": {"rubric": {}}})
        + "\n"  # missing prompt
        + json.dumps({"prompt": "  ", "content": {"rubric": {}}})
        + "\n",  # empty prompt
        encoding="utf-8",
    )
    adapter = DeepResearchBench2Adapter(p)
    qs = adapter.questions()
    assert len(qs) == 1
    assert qs[0].question == "What is X?"
    assert len(qs[0].rubrics) == 1
    # 3 malformed lines recorded (not silently skipped): bad json, missing
    # prompt, and whitespace-only prompt.
    assert len(adapter.errors) == 3


def test_dataset_sha256_stable(tmp_path: Path):
    p = tmp_path / "drb2.jsonl"
    p.write_text(json.dumps({"task": "q", "rubric_items": {}}) + "\n", encoding="utf-8")
    h1 = DeepResearchBench2Adapter(p).dataset_sha256()
    h2 = sha256_file(p)
    assert h1 == h2
    assert len(h1) == 64


# --- P1-2: metadata snapshot -------------------------------------------------
def test_run_snapshot_records_metadata(tmp_path: Path):
    ds = FixtureDataset()

    def answerer(prompt):  # type: ignore[no-untyped-def]
        return f"answer for {prompt.qid}", 1, 10

    out = tmp_path / "run"
    runner = EvalRunner(
        out_dir=out,
        config=CONFIGS["full"],
        questions=ds.questions(),
        answerer=answerer,
        dataset_hash="abc123",
        provider="qwen",
        model="qwen3.8-max",
    )
    runner.run_sync(max_questions=1)
    snap = json.loads((out / "config_snapshot.json").read_text(encoding="utf-8"))
    assert snap["run"]["provider"] == "qwen"
    assert snap["run"]["model"] == "qwen3.8-max"
    assert snap["run"]["dataset_hash"] == "abc123"
    assert "prompt_commits" in snap["run"]
    assert "start_time" in snap["run"]
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert summary["end_time"]
    assert summary["usage"]["total_tokens"] == 10


# --- P1-1: CLI smoke slice ---------------------------------------------------
def test_cli_run_smoke_slice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from click.testing import CliRunner
    from evals.cli import main

    cli = CliRunner()
    out = tmp_path / "out"
    result = cli.invoke(
        main,
        ["run", "--output-dir", str(out), "--judge", "keyword", "--mode", "fake"],
    )
    assert result.exit_code == 0, result.output
    # No --limit / --confirm-full -> 3-question smoke on the fixture.
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert summary["n"] == 3


# --- LangSmith Experiment 集成（mock client，不联网） -----------------------
class _FakeDS:
    def __init__(self, name, id_):
        self.name = name
        self.id = id_


class _FakeExample:
    def __init__(self, id_, inputs):
        self.id = id_
        self.inputs = inputs


class _FakeLangSmithClient:
    """Minimal mock for the new aevaluate-based sink construction."""

    def __init__(self, dataset_name="DeepResearch Bench II official-2b29012d0842"):
        self._dataset_name = dataset_name

    def list_datasets(self, *, dataset_name=None, limit=None):
        return [_FakeDS(self._dataset_name, "ds-123")]

    def list_examples(self, *, dataset_id=None, limit=None):
        return [
            _FakeExample("ex-1", {"qid": "fx-001", "prompt": "What is LangGraph?"}),
            _FakeExample("ex-2", {"qid": "fx-002", "prompt": "Does LlamaIndex focus on retrieval?"}),
        ]


def _make_sink(client):
    from evals.runner import LangSmithExperimentSink

    return LangSmithExperimentSink(
        experiment_prefix="drb2-pilot-v2",
        dataset_name="DeepResearch Bench II official-2b29012d0842",
        dataset_hash="2b29012d0842",
        judge_model="qwen-mock",
        prompt_commits={"eval_rubric_judge_system": "78595230"},
        git_commit="abc1234",
        research_mode="live",
        client=client,
    )


def test_experiment_sink_construction_links_dataset():
    """New aevaluate-based sink: construction resolves dataset + qid->example map."""
    client = _FakeLangSmithClient()
    sink = _make_sink(client)
    assert sink.enabled is True
    assert sink._dataset_id == "ds-123"
    assert "fx-001" in sink._example_by_qid
    assert "fx-002" in sink._example_by_qid


def test_experiment_sink_disabled_when_dataset_missing():
    """Sink self-disables when dataset not found (no exception raised)."""
    class _EmptyClient(_FakeLangSmithClient):
        def list_datasets(self, *, dataset_name=None, limit=None):
            return []

    sink = _make_sink(_EmptyClient())
    assert sink.enabled is False
