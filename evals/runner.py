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


def _unpack_answerer(raw: Any) -> tuple[str, int, int, list[dict], bool, bool]:
    """Accept BOTH answerer return shapes.

    - Legacy 3-tuple ``(answer, rounds, tokens)`` (old baselines / offline
      fixture answerers): citations unknown, no quality gate signal.
    - Dict (production ``make_research_runner_answerer``): exposes citations,
      real token usage, the quality-gate result and the estimated flag.
    """
    if isinstance(raw, dict):
        citations = raw.get("citations", [])
        return (
            str(raw.get("answer", "")),
            int(raw.get("rounds", 0)),
            int(raw.get("tokens", 0)),
            list(citations) if isinstance(citations, list) else [],
            bool(raw.get("quality_passed", True)),
            bool(raw.get("usage_estimated", True)),
        )
    answer, rounds, tokens = raw
    return str(answer), int(rounds), int(tokens), [], True, True


def _git_commit() -> str:
    """当前 git commit hash（取不到时返回 "unknown"，不报错）。"""
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL, text=True
        ).strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def _project_env_path() -> str:
    """项目根目录下 .env 的绝对路径（无论 CWD 在哪都能定位）。"""
    try:
        from core.config import project_root

        return str(project_root() / ".env")
    except Exception:  # noqa: BLE001
        return ".env"


class LangSmithExperimentSink:
    """把每题评测结果上传到 LangSmith Experiment（官方高层 ``aevaluate`` API）。

    背景与迁移动机
    --------------
    旧实现用低层 ``create_project`` + 逐题 ``create_run`` + ``create_feedback`` 手工拼装。
    该路径有两个问题：(1) ``create_run`` 失败只吞 warning，不在本地 EvalResult 落错；
    (2) 手工建的 run 不经过 SDK 的 tracer，``reference_example_id`` 关联与 project 的
    ``reference_dataset_id`` / ``num_repetitions`` / ``evaluator_keys`` /
    ``__ls_runner=py_sdk_evaluate`` 元数据不齐，导致在 Experiments UI 里不被识别为
    "experiment run"，每题结果不可见。

    新实现直接调用 langsmith SDK 官方 ``aevaluate(target, data=..., evaluators=...)``：

    1. ``data`` 传入已存在 Dataset 的 Example 列表（按 qid 匹配本地题）；
    2. ``target`` 是一个 async callable，逐题跑 answerer + judge，并写本地结果 JSON；
       SDK 的 tracer 自动把 run 挂到新建 experiment project 上、并正确设置
       ``reference_example_id``，因此 UI 一定可见；
    3. ``evaluators`` 里只放一个零计算的 pass-through：把本地已算好的 judge_score
       原样提升为官方 feedback（不在这里调 LLM，judge 逻辑完全不变）。

    健壮性：联网/鉴权/字段不匹配失败只记 warning 并自我禁用（``enabled=False``），
    绝不阻塞本地评测。``client`` 可注入，供离线测试用 mock。
    """

    def __init__(
        self,
        *,
        experiment_prefix: str,
        dataset_name: str = LANGSMITH_DATASET_NAME,
        dataset_hash: str = "",
        provider: str = "unknown",
        model: str = "unknown",
        judge_model: str = "",
        prompt_commits: dict[str, str] | None = None,
        git_commit: str = "",
        research_mode: str = "live",
        client: Any | None = None,
    ) -> None:
        self._prefix = experiment_prefix
        self._dataset_name = dataset_name
        self._dataset_hash = dataset_hash
        self._provider = provider
        self._model = model
        self._judge_model = judge_model
        self._prompt_commits = prompt_commits or {}
        self._git_commit = git_commit
        self._research_mode = research_mode
        self._enabled = False
        self._dataset_id: Any = None
        self._example_by_qid: dict[str, Any] = {}
        self._experiment_name: str = ""
        self._client = client
        try:
            if self._client is None:
                # 显式从本地 .env 加载（pydantic-settings 不会把值塞进 os.environ，
                # 而 langsmith.Client 只读 os.environ）。绝不打印/外泄 key。
                import dotenv

                dotenv.load_dotenv(_project_env_path())
                from langsmith import Client

                self._client = Client()
            # 按名称查找已存在的 Dataset。
            ds = None
            for d in self._client.list_datasets(dataset_name=dataset_name, limit=250):
                if getattr(d, "name", None) == dataset_name:
                    ds = d
                    break
            if ds is None:
                log.warning("langsmith 数据集未找到: %s（experiment 上传禁用）", dataset_name)
                return
            self._dataset_id = ds.id
            # 建立 qid -> example 索引（Dataset example.inputs 里带 qid）。
            for ex in self._client.list_examples(dataset_id=ds.id, limit=2000):
                inp = getattr(ex, "inputs", None) or {}
                qid = inp.get("qid") if isinstance(inp, dict) else None
                if qid is not None and str(qid):
                    self._example_by_qid[str(qid)] = ex
            if not self._example_by_qid:
                log.warning("langsmith 数据集 %s 没有 example（experiment 上传禁用）", dataset_name)
                return
            self._enabled = True
            log.info(
                "langsmith experiment sink 就绪: dataset=%s examples=%d",
                dataset_name,
                len(self._example_by_qid),
            )
        except Exception as e:  # noqa: BLE001 - 屏蔽鉴权/网络细节
            log.warning("langsmith experiment sink 初始化失败（上传禁用）: %s", e)
            self._enabled = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def experiment_name(self) -> str:
        return self._experiment_name

    def _metadata(self) -> dict[str, object]:
        """Experiment 公共 metadata（明确标注非官方 Qwen 评分）。"""
        return {
            "judge": "qwen-nonofficial",
            "judge_model": self._judge_model,
            "provider": self._provider,
            "model": self._model,
            "dataset_hash": self._dataset_hash,
            "prompt_commits": self._prompt_commits,
            "git_commit": self._git_commit,
            "research_mode": self._research_mode,
            "official_judge": "not_run (requires GPT-5.5)",
        }

    async def arun_experiment(
        self,
        *,
        runner: Any,
        questions: list[EvalQuestion],
        limit: int,
        start_time: float,
    ) -> list[dict]:
        """用官方 ``aevaluate`` 驱动整个实验；返回所有已完成结果 payload。

        流程：选题（limit + resume 跳过已完成）-> 调 ``aevaluate`` -> 写
        ``experiment_url.txt`` -> 汇总 metrics 写 ``summary.json``。
        answerer/judge/writer/blocked 过滤全部复用 ``EvalRunner._process_one``，
        不改动其内部逻辑。
        """
        from langsmith.evaluation import aevaluate

        out: Any = runner._out
        q_by_id: dict[str, EvalQuestion] = {q.qid: q for q in questions}
        # resume：跳过本地已 completed 的题。
        done: dict[str, dict] = runner._load_done()
        ordered: list[EvalQuestion] = []
        skipped_no_example: list[str] = []
        for q in questions:
            if len(ordered) >= limit:
                break
            if q.qid in done:
                continue
            if q.qid not in self._example_by_qid:
                skipped_no_example.append(q.qid)
                continue
            ordered.append(q)
        if skipped_no_example:
            log.warning(
                "跳过 %d 题（本地有但 Dataset 里找不到对应 example）: %s",
                len(skipped_no_example),
                skipped_no_example[:10],
            )
        examples = [self._example_by_qid[q.qid] for q in ordered]
        if not examples:
            log.warning("没有可跑的 example（全部已完成或无匹配），不创建 experiment。")
            return self._collect_results(runner, questions)

        # 写 config 快照（与 EvalRunner.run 一致）。
        runner._write_snapshot(start_time=start_time)

        async def predict(inputs: dict) -> dict:
            qid = str(inputs.get("qid", ""))
            q = q_by_id.get(qid)
            if q is None:
                return {
                    "output": "",
                    "qid": qid,
                    "status": "failed",
                    "judge_score": 0.0,
                    "judge_reason": "no local EvalQuestion for qid",
                    "judge_detail": [],
                    "latency_s": 0.0,
                    "tokens": 0,
                }
            payload = await runner._process_one(q)
            detail_raw = payload.get("judge_detail", "")
            try:
                detail_parsed: object = json.loads(detail_raw) if detail_raw else []
            except Exception:  # noqa: BLE001
                detail_parsed = []
            return {
                "output": str(payload.get("answer", ""))[:8000],
                "qid": qid,
                "status": str(payload.get("status", "")),
                "judge_score": float(payload.get("judge_score", 0.0) or 0.0),
                "judge_reason": str(payload.get("judge_reason", ""))[:500],
                "judge_detail": detail_parsed,
                "latency_s": float(payload.get("latency_s", 0.0) or 0.0),
                "tokens": int(payload.get("tokens_estimated", 0) or 0),
            }

        def record_judge(run: Any, example: Any) -> dict:
            """零计算 pass-through：把本地已算好的分数提升为官方 feedback。"""
            out = (getattr(run, "outputs", None) or {})
            detail = out.get("judge_detail", [])
            if not isinstance(detail, list):
                detail = []
            return {
                "key": "judge_score",
                "score": float(out.get("judge_score", 0.0) or 0.0),
                "value": {
                    "status": out.get("status", ""),
                    "judge_detail": detail[:_MAX_DETAIL_IN_FEEDBACK],
                },
                "comment": str(out.get("judge_reason", ""))[:500],
            }

        results = await aevaluate(
            predict,
            data=examples,
            evaluators=[record_judge],
            metadata=self._metadata(),
            experiment_prefix=self._prefix,
            max_concurrency=0,  # 0 = 串行，与原 runner 行为一致
            client=self._client,
            error_handling="log",
        )
        self._experiment_name = results.experiment_name
        url = await results.get_comparison_url()
        if url:
            (out / "experiment_url.txt").write_text(url + "\n", encoding="utf-8")
        log.info(
            "langsmith experiment 完成: %s -> %s", results.experiment_name, url
        )

        all_payloads = self._collect_results(runner, questions)
        runner._write_summary(all_payloads, start=start_time, end=time.time())
        return all_payloads

    @staticmethod
    def _collect_results(runner: Any, questions: list[EvalQuestion]) -> list[dict]:
        """汇总所有本地结果 JSON（completed + failed），保持与 run() 一致口径。"""
        out: list[dict] = []
        for q in questions:
            p = runner._state_path(q.qid)
            try:
                out.append(json.loads(p.read_text(encoding="utf-8")))
            except Exception:  # noqa: BLE001
                continue
        return out


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

    async def _process_one(self, q: EvalQuestion) -> dict:
        """Process a single question end-to-end; NEVER raises (failure isolation).

        Builds the answerer-only EvalPrompt, runs the answerer, enforces the
        report-quality gate, scores with the judge, builds an EvalResult and
        flushes it to <out>/results/<qid>.json. Returns the result payload dict.

        This is the single source of truth for one question's work; both the
        serial ``run()`` loop and the official ``aevaluate`` orchestrator call
        it, so judge / writer / blocked-filtering logic lives in exactly one place.
        """
        t0 = time.monotonic()
        per_item: list = []
        try:
            # P0-4: answerer sees ONLY qid + question (+ source-constraint
            # metadata that already lives in the prompt's **important**
            # block). No reference_answer / rubrics.
            prompt = EvalPrompt(
                qid=q.qid,
                question=q.question,
                blocked_urls=list(q.blocked_urls),
                blocked_domains=list(q.blocked_domains),
                blocked_titles=list(q.blocked_titles),
                as_of_date=q.as_of_date,
            )
            if self._answerer is None:
                # Offline fixture answerer: NO reference-answer leak.
                answer = f"Offline answer: {q.question}"
                rounds = 1
                tokens = 0
                citations_list: list[dict] = []
                quality_passed = True
                usage_estimated = True
            else:
                assert callable(self._answerer)
                raw: Any = self._answerer(prompt)
                if inspect.isawaitable(raw):
                    raw = await raw
                (
                    answer,
                    rounds,
                    tokens,
                    citations_list,
                    quality_passed,
                    usage_estimated,
                ) = _unpack_answerer(raw)
            # Quality gate: a synthesis the production system flagged as
            # failed does NOT reach the judge; the question is marked
            # failed below by the raised exception.
            if not quality_passed:
                raise RuntimeError("report quality gate failed")
            score, reason, per_item = await self._judge_answer(answer, q)
            detail = json.dumps(per_item, ensure_ascii=False)
            # EvalResult.citations holds the provenance URLs the answerer
            # actually cited (post blocked/post-cutoff filtering).
            citation_urls = [
                c.get("url", "") if isinstance(c, dict) else str(c)
                for c in citations_list
            ]
            res = EvalResult(
                qid=q.qid,
                status="completed",
                question=q.question,
                answer=answer[:_MAX_STORED_ANSWER],
                citations=citation_urls,
                n_search_rounds=rounds,
                tokens_estimated=tokens,
                usage_estimated=usage_estimated,
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
        return payload

    def _write_summary(self, results: list[dict], *, start: float, end: float) -> None:
        """Compute aggregate metrics from result payloads and write summary.json."""
        total_tokens = sum(int(r.get("tokens_estimated", 0) or 0) for r in results)
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

    async def run(self, *, max_questions: int | None = None) -> list[dict]:
        start = time.time()
        self._write_snapshot(start_time=start)
        done = self._load_done()
        limit = max_questions or self._config.max_questions_per_run

        results: list[dict] = []
        for i, q in enumerate(self._questions):
            if i >= limit:
                break
            if q.qid in done:
                results.append(done[q.qid])
                continue
            payload = await self._process_one(q)
            # 可选：上传到 LangSmith Experiment（失败只记 warning，不阻塞）。
            # 注意：experiment 模式走 LangSmithExperimentSink.arun_experiment，
            # 这里仅保留对老式 sink 的兼容钩子。
            if self._experiment_sink is not None and hasattr(
                self._experiment_sink, "log_result"
            ):
                self._experiment_sink.log_result(
                    qid=q.qid,
                    question=q.question,
                    answer=payload.get("answer", ""),
                    status=payload.get("status", ""),
                    score=payload.get("judge_score", 0.0),
                    latency_s=payload.get("latency_s", 0.0),
                    tokens=payload.get("tokens_estimated", 0),
                    judge_detail=[],
                    judge_reason=payload.get("judge_reason", ""),
                )
            results.append(payload)

        self._write_summary(results, start=start, end=time.time())
        return results

    def run_sync(self, *, max_questions: int | None = None) -> list[dict]:
        """Synchronous entry point for CLI / non-async callers."""
        return asyncio.run(self.run(max_questions=max_questions))


__all__ = ["EvalRunner"]
