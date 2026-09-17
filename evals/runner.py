"""Resumable eval runner.

Design rules:
- Per-question state is saved as a JSON file under <out_dir>/results/<qid>.json
  and flushed to disk immediately after each question (no crash loss).
- On resume, COMPLETED questions are skipped (no re-billing). FAILED questions
  are RE-RUN (not silently skipped).
- The config snapshot + run metadata are written at start; they never change
  mid-run. End time / usage are appended at finish.
- Single-question failure does not abort the run.
- A question marked `failed` stays in the denominator (not silently dropped).
- Answerer isolation (P0-4): answerers receive an ``EvalPrompt`` (qid +
  question) ONLY. ``reference_answer`` / ``rubrics`` are read exclusively by
  the judge from the full ``EvalQuestion``.
- ``run()`` is async (answerers are async). ``run_sync()`` wraps it with
  ``asyncio.run()`` for the CLI / synchronous callers.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import subprocess
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from core.prompts.manifest import load_lock
from evals.adapter import EvalPrompt, EvalQuestion, EvalResult
from evals.configs import EvalConfig
from evals.failures import classify_failure
from evals.metrics import KeywordJudge, compute_metrics

log = logging.getLogger(__name__)

# Truncate stored answers so a runaway report does not bloat the result JSON.
_MAX_STORED_ANSWER = 8000
# Experiment feedback 里附带的 per_item 明细截断条数。
_MAX_DETAIL_IN_FEEDBACK = 200
# 已存在的官方 Dataset 名称。
LANGSMITH_DATASET_NAME = "DeepResearch Bench II official-2b29012d0842"


def _prompt_commits() -> dict[str, str]:
    """Map every pinned prompt -> commit hash from manifest.lock.json."""
    try:
        lock = load_lock()
    except Exception:  # noqa: BLE001
        return {}
    return {name: entry.commit_hash for name, entry in lock.items()}


def _git_commit() -> str:
    """当前 git commit hash（取不到时返回 "unknown"，不报错）。"""
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL, text=True
        ).strip()
    except Exception:  # noqa: BLE001
        return "unknown"


class LangSmithExperimentSink:
    """把每题评测结果上传到 LangSmith Experiment（官方低层 API）。

    背景：本 SDK（langsmith 0.12.x）的 ``Client`` 没有独立的 ``create_experiment``；
    Experiment 在 LangSmith 里本质是一个关联到 Dataset 的 TracerSession(project)。
    因此采用官方推荐的低层写法：

    1. ``client.create_project(project_name=..., reference_dataset_id=...)`` 建立
       Experiment；
    2. 每题 ``client.create_run(id=..., reference_example_id=...)`` 记录 input/output；
    3. ``client.create_feedback(run_id=..., key="judge_score", score=...)`` 挂分。

    健壮性原则：任何联网 / 鉴权 / 字段不匹配的失败都只记 warning 并自我禁用，
    绝不阻塞本地评测。``client`` 可注入，供离线测试用 mock。
    """

    def __init__(
        self,
        *,
        experiment_name: str,
        dataset_name: str = LANGSMITH_DATASET_NAME,
        dataset_hash: str = "",
        judge_model: str = "",
        prompt_commits: dict[str, str] | None = None,
        git_commit: str = "",
        research_mode: str = "live",
        client: Any | None = None,
    ) -> None:
        self._name = experiment_name
        self._dataset_name = dataset_name
        self._dataset_hash = dataset_hash
        self._judge_model = judge_model
        self._prompt_commits = prompt_commits or {}
        self._git_commit = git_commit
        self._research_mode = research_mode
        self._enabled = False
        self._example_by_key: dict[str, str] = {}
        self._client = client
        try:
            if self._client is None:
                from langsmith import Client

                self._client = Client()
            # 按名称查找已存在的 Dataset。
            ds = None
            for d in self._client.list_datasets(dataset_name=dataset_name, limit=25):
                if getattr(d, "name", None) == dataset_name:
                    ds = d
                    break
            if ds is None:
                log.warning("langsmith 数据集未找到: %s（experiment 上传禁用）", dataset_name)
                return
            dataset_id = ds.id
            # 建立 qid/idx/prompt -> example_id 的索引，供逐题匹配。
            for ex in self._client.list_examples(dataset_id=dataset_id):
                self._index_example(ex)
            self._client.create_project(
                project_name=experiment_name,
                reference_dataset_id=dataset_id,
                metadata=self._metadata(),
            )
            self._enabled = True
            log.info("langsmith experiment 已创建: %s", experiment_name)
        except Exception as e:  # noqa: BLE001 - 屏蔽鉴权/网络细节
            log.warning("langsmith experiment 初始化失败（上传禁用）: %s", e)
            self._enabled = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    def _metadata(self) -> dict[str, object]:
        """Experiment / run 的公共 metadata（明确标注非官方 Qwen 评分）。"""
        return {
            "judge": "qwen-nonofficial",
            "judge_model": self._judge_model,
            "dataset_hash": self._dataset_hash,
            "prompt_commits": self._prompt_commits,
            "git_commit": self._git_commit,
            "research_mode": self._research_mode,
            "official_judge": "not_run (requires GPT-5.5)",
        }

    def _index_example(self, ex: Any) -> None:
        """把一个 dataset example 按多种可能的 key 建索引。"""
        ex_id = str(ex.id)
        inputs = getattr(ex, "inputs", None) or {}
        for key in ("qid", "id", "task_id", "idx"):
            v = inputs.get(key) if isinstance(inputs, dict) else None
            if v is not None and str(v):
                self._example_by_key[str(v)] = ex_id
        if isinstance(inputs, dict):
            prompt = inputs.get("prompt") or inputs.get("question") or inputs.get("input")
            if isinstance(prompt, str) and prompt.strip():
                self._example_by_key[prompt.strip()] = ex_id

    def log_result(
        self,
        *,
        qid: str,
        question: str,
        answer: str,
        status: str,
        score: float,
        latency_s: float,
        tokens: int,
        judge_detail: list[dict[str, object]],
        judge_reason: str,
    ) -> None:
        """上传单题结果。任何失败都吞掉，只记 warning。"""
        if not self._enabled or self._client is None:
            return
        try:
            ex_id = self._example_by_key.get(qid) or self._example_by_key.get((question or "").strip())
            run_id = str(uuid4())
            truncated_detail = (judge_detail or [])[:_MAX_DETAIL_IN_FEEDBACK]
            self._client.create_run(
                id=run_id,
                name=qid,
                run_type="chain",
                project_name=self._name,
                inputs={"prompt": question},
                outputs={"answer": (answer or "")[:8000]},
                reference_example_id=ex_id,
                start_time=datetime.now(UTC),
                end_time=datetime.now(UTC),
                extra={
                    "metadata": {**self._metadata(), "qid": qid},
                    "latency_s": latency_s,
                    "usage": {"tokens": tokens},
                },
            )
            self._client.create_feedback(
                run_id=run_id,
                key="judge_score",
                score=float(score),
                value={"status": status, "judge_detail": truncated_detail},
                comment=judge_reason[:500],
            )
        except Exception as e:  # noqa: BLE001
            log.warning("langsmith 上传失败 qid=%s: %s", qid, e)


class EvalRunner:
    def __init__(
        self,
        *,
        out_dir: Path,
        config: EvalConfig,
        questions: list[EvalQuestion],
        answerer: Any | None = None,
        judge: Any | None = None,
        dataset_hash: str = "",
        provider: str = "unknown",
        model: str = "unknown",
        experiment_sink: Any | None = None,
    ) -> None:
        self._out = out_dir
        self._results_dir = out_dir / "results"
        self._results_dir.mkdir(parents=True, exist_ok=True)
        self._config = config
        self._questions = questions
        # Default judge is the offline KeywordJudge (deterministic, hermetic for
        # tests). The CLI injects LLMRubricJudge when --judge llm.
        self._judge: Any = judge if judge is not None else KeywordJudge()
        self._answerer = answerer  # optional callable(EvalPrompt) -> (answer, rounds, tokens)
        self._dataset_hash = dataset_hash
        self._provider = provider
        self._model = model
        # 可选的 LangSmith experiment 上传器（None 表示不上传）。
        self._experiment_sink = experiment_sink

    def _state_path(self, qid: str) -> Path:
        return self._results_dir / f"{qid}.json"

    def _load_done(self) -> dict[str, dict]:
        """Load ONLY previously-completed questions (resume set).

        Failed questions are deliberately excluded so they are re-run.
        """
        done: dict[str, dict] = {}
        for p in self._results_dir.glob("*.json"):
            try:
                rec = json.loads(p.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            if rec.get("status") == "completed":
                done[p.stem] = rec
        return done

    def _write_snapshot(self, *, start_time: float) -> None:
        snap = self._config.snapshot()
        snap["run"] = {
            "provider": self._provider,
            "model": self._model,
            "prompt_commits": _prompt_commits(),
            "dataset_hash": self._dataset_hash,
            "start_time": datetime.fromtimestamp(start_time, tz=UTC).isoformat(),
        }
        (self._out / "config_snapshot.json").write_text(
            json.dumps(snap, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    @staticmethod
    def _rubric_payload(q: EvalQuestion) -> tuple[list[str], list[str] | None, list[str]]:
        """构造评分器需要的 (rubrics, dimensions, blocked)。

        优先用 ``rubrics_by_dimension``（官方三维度 dict），与 rubric 顺序对齐地
        产出 dimensions；否则退回扁平 ``rubrics``（不标注维度）。
        """
        if q.rubrics_by_dimension:
            rubrics: list[str] = []
            dims: list[str] = []
            for dim, items in q.rubrics_by_dimension.items():
                for r in items:
                    rubrics.append(r)
                    dims.append(dim)
            return rubrics, dims, list(q.blocked_rubrics)
        return list(q.rubrics), None, list(q.blocked_rubrics)

    async def _judge_answer(self, answer: str, q: EvalQuestion):
        """Score one answer. Handles keyword (sync) and LLM (async) judges.

        Returns (score, reason, per_item). per_item 为评分明细列表（dict），
        离线 keyword judge 为空列表。
        """
        judge = self._judge
        if judge is None:
            return 0.0, "no judge configured", []
        kind = getattr(judge, "kind", "keyword")
        if kind == "llm":
            rubrics, dims, blocked = self._rubric_payload(q)
            verdict = await judge.score(answer, rubrics, blocked=blocked, dimensions=dims)
            score, per_item, reason = verdict
            return score, reason, per_item
        # Offline keyword judge (sync).
        score, reason = judge.score(answer, q.required_keywords)
        return score, reason, []

    async def run(self, *, max_questions: int | None = None) -> list[dict]:
        start = time.time()
        self._write_snapshot(start_time=start)
        done = self._load_done()
        limit = max_questions or self._config.max_questions_per_run

        results: list[dict] = []
        total_tokens = 0
        for i, q in enumerate(self._questions):
            if i >= limit:
                break
            if q.qid in done:
                results.append(done[q.qid])
                total_tokens += done[q.qid].get("tokens_estimated", 0)
                continue
            t0 = time.monotonic()
            try:
                # P0-4: answerer sees ONLY qid + question.
                prompt = EvalPrompt(qid=q.qid, question=q.question)
                if self._answerer is None:
                    # Offline fixture answerer: NO reference-answer leak.
                    answer = f"Offline answer: {q.question}"
                    rounds = 1
                    tokens = 0
                else:
                    assert callable(self._answerer)
                    raw: Any = self._answerer(prompt)
                    if inspect.isawaitable(raw):
                        answer, rounds, tokens = await raw
                    else:
                        answer, rounds, tokens = raw  # sync answerer
                score, reason, per_item = await self._judge_answer(answer, q)
                detail = json.dumps(per_item, ensure_ascii=False)
                res = EvalResult(
                    qid=q.qid,
                    status="completed",
                    question=q.question,
                    answer=answer[:_MAX_STORED_ANSWER],
                    citations=[],
                    n_search_rounds=rounds,
                    tokens_estimated=tokens,
                    latency_s=time.monotonic() - t0,
                    judge_score=score,
                    judge_reason=reason,
                    judge_detail=detail,
                )
            except Exception as e:  # single-question failure isolation
                per_item = []
                res = EvalResult(
                    qid=q.qid,
                    status="failed",
                    question=q.question,
                    error=str(e),
                    latency_s=time.monotonic() - t0,
                    failure_category=classify_failure(
                        EvalResult(qid=q.qid, status="failed", error=str(e))
                    ),
                )
            payload = asdict(res)
            # Flush immediately so a crash mid-run never loses a finished item.
            self._state_path(q.qid).write_text(
                json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            # 可选：上传到 LangSmith Experiment（失败只记 warning，不阻塞）。
            if self._experiment_sink is not None:
                self._experiment_sink.log_result(
                    qid=q.qid,
                    question=q.question,
                    answer=payload.get("answer", ""),
                    status=payload.get("status", ""),
                    score=payload.get("judge_score", 0.0),
                    latency_s=payload.get("latency_s", 0.0),
                    tokens=payload.get("tokens_estimated", 0),
                    judge_detail=per_item,
                    judge_reason=payload.get("judge_reason", ""),
                )
            results.append(payload)
            total_tokens += payload.get("tokens_estimated", 0)

        end = time.time()

        # Summary.
        metrics = compute_metrics([EvalResult(**r) for r in results])
        summary = {
            "config": self._config.name,
            "n": metrics.n,
            "n_failed": metrics.n_failed,
            "avg_judge_score": metrics.avg_judge_score,
            "pass_rate": metrics.pass_rate,
            "avg_search_rounds": metrics.avg_search_rounds,
            "avg_tokens": metrics.avg_tokens,
            "avg_latency_s": metrics.avg_latency_s,
            "provider": self._provider,
            "model": self._model,
            "dataset_hash": self._dataset_hash,
            "prompt_commits": _prompt_commits(),
            "start_time": datetime.fromtimestamp(start, tz=UTC).isoformat(),
            "end_time": datetime.fromtimestamp(end, tz=UTC).isoformat(),
            "usage": {"total_tokens": total_tokens},
        }
        (self._out / "summary.json").write_text(
            json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return results

    def run_sync(self, *, max_questions: int | None = None) -> list[dict]:
        """Synchronous entry point for CLI / non-async callers."""
        return asyncio.run(self.run(max_questions=max_questions))


__all__ = ["EvalRunner"]
