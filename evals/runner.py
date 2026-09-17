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
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.prompts.manifest import load_lock
from evals.adapter import EvalPrompt, EvalQuestion, EvalResult
from evals.configs import EvalConfig
from evals.failures import classify_failure
from evals.metrics import KeywordJudge, compute_metrics

# Truncate stored answers so a runaway report does not bloat the result JSON.
_MAX_STORED_ANSWER = 8000


def _prompt_commits() -> dict[str, str]:
    """Map every pinned prompt -> commit hash from manifest.lock.json."""
    try:
        lock = load_lock()
    except Exception:  # noqa: BLE001
        return {}
    return {name: entry.commit_hash for name, entry in lock.items()}


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
    ) -> None:
        self._out = out_dir
        self._results_dir = out_dir / "results"
        self._results_dir.mkdir(parents=True, exist_ok=True)
        self._config = config
        self._questions = questions
        # Default judge is the offline KeywordJudge (deterministic, hermetic for
        # tests). The CLI injects LLMRubricJudge when --judge llm.
        self._judge = judge if judge is not None else KeywordJudge()
        self._answerer = answerer  # optional callable(EvalPrompt) -> (answer, rounds, tokens)
        self._dataset_hash = dataset_hash
        self._provider = provider
        self._model = model

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

    async def _judge_answer(self, answer: str, q: EvalQuestion):
        """Score one answer. Handles keyword (sync) and LLM (async) judges.

        Returns (score, reason, detail_json).
        """
        judge = self._judge
        if judge is None:
            return 0.0, "no judge configured", ""
        kind = getattr(judge, "kind", "keyword")
        if kind == "llm":
            raw_verdict: Any = judge.score(answer, q.rubrics)
            verdict = await raw_verdict
            if len(verdict) == 3:
                score, per_item, reason = verdict
            else:
                score, reason = verdict
                per_item = []
            return score, reason, json.dumps(per_item, ensure_ascii=False)
        # Offline keyword judge (sync).
        score, reason = judge.score(answer, q.required_keywords)
        return score, reason, ""

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
                score, reason, detail = await self._judge_answer(answer, q)
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
