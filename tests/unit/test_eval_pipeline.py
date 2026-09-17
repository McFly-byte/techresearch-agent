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


# --- Mock LLM for the judge -------------------------------------------------
class _MockJudgeLLM:
    """Deterministic fake LLM: returns a JSON verdict per request."""

    provider_name = "mock"

    def __init__(self, verdits: list[int] | None = None) -> None:
        self.model_id = "mock-1"
        self._verdits = verdits if verdits is not None else [1]
        self._i = 0
        self.calls: list[list] = []

    async def acomplete(self, messages, *, model_id=None):  # type: ignore[no-untyped-def]
        self.calls.append(list(messages))
        val = self._verdits[min(self._i, len(self._verdits) - 1)]
        self._i += 1
        from evals.adapter import EvalResult  # noqa: F401  (kept for shape parity)

        from core.providers.base import LLMResponse

        text = json.dumps({"satisfied": val, "reason": "mock reason"})
        return LLMResponse(text=text, model="mock-1", provider="mock")

    def is_configured(self) -> bool:
        return True


class _BrokenLLM:
    """Always returns unparseable text."""

    provider_name = "broken"
    model_id = "broken-1"

    async def acomplete(self, messages, *, model_id=None):  # type: ignore[no-untyped-def]
        from core.providers.base import LLMResponse

        return LLMResponse(text="not json at all", model="broken-1", provider="broken")


# --- P0-3: LLMRubricJudge ---------------------------------------------------
@pytest.mark.asyncio
async def test_llm_rubric_judge_scores_each_rubric():
    judge = LLMRubricJudge(llm=_MockJudgeLLM(verdits=[1, 0, 1]))
    score, per_item, reason = await judge.score("some answer", ["r1", "r2", "r3"])
    assert score == pytest.approx(2 / 3)
    assert len(per_item) == 3
    assert per_item[0]["satisfied"] == 1
    assert per_item[1]["satisfied"] == 0
    assert "2/3" in reason


@pytest.mark.asyncio
async def test_llm_rubric_judge_empty_rubrics():
    judge = LLMRubricJudge(llm=_MockJudgeLLM())
    score, per_item, reason = await judge.score("answer", [])
    assert score == 1.0
    assert per_item == []
    assert "no rubrics" in reason


@pytest.mark.asyncio
async def test_llm_rubric_judge_parse_failure_scores_zero_no_crash():
    judge = LLMRubricJudge(llm=_BrokenLLM(), max_retries=1, timeout=1.0)
    score, per_item, reason = await judge.score("answer", ["r1"])
    assert score == 0.0
    assert per_item[0]["satisfied"] == 0
    assert "error:" in per_item[0]["reason"]


# --- P0-4: answerer isolation ----------------------------------------------
def test_eval_prompt_has_no_reference_fields():
    p = EvalPrompt(qid="q1", question="what?")
    assert not hasattr(p, "reference_answer")
    assert not hasattr(p, "rubrics")


# --- P0-1: ResearchRunner answerer (fake mode, real system) -----------------
@pytest.mark.asyncio
async def test_research_runner_answerer_fake_mode():
    answerer = make_research_runner_answerer(mode="fake")
    answer, rounds, tokens = await answerer(
        EvalPrompt(qid="t1", question="Tell me about LangGraph.")
    )
    assert isinstance(answer, str)
    assert len(answer) > 0
    assert isinstance(rounds, int)
    assert isinstance(tokens, int)


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
