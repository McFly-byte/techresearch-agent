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
import os
import subprocess
import time
import uuid
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.deadlines import run_with_hard_timeout
from core.prompts.manifest import load_lock
from core.usage import capture_usage
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
        concurrency: int = 1,
        research_workers: int = 1,
        subset_metadata: dict[str, object] | None = None,
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
        self._concurrency = max(1, int(concurrency))
        self._research_workers = max(1, int(research_workers))
        self._subset_metadata = subset_metadata or {}
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
            "concurrency": self._concurrency,
            "research_workers": self._research_workers,
            "subset": self._subset_metadata,
        }

    def _comparison_url(self, project: Any) -> str:
        project_url = str(getattr(project, "url", "") or "").split("?")[0]
        if not project_url:
            return ""
        base_url = project_url.split("/projects/p/")[0]
        return f"{base_url}/datasets/{self._dataset_id}/compare?selectedSessions={project.id}"

    def _load_or_create_experiment(
        self, *, out: Path, qids: list[str], runner: Any
    ) -> tuple[Any, dict[str, object]]:
        """Persist and reuse one exact LangSmith Experiment across resumes."""
        path = out / "experiment.json"
        client = self._client
        if client is None:
            raise RuntimeError("LangSmith client is unavailable")
        if path.is_file():
            saved_manifest = json.loads(path.read_text(encoding="utf-8"))
            if saved_manifest.get("dataset_hash") != self._dataset_hash:
                raise ValueError("resume experiment dataset hash mismatch")
            if saved_manifest.get("subset") != self._subset_metadata:
                raise ValueError("resume experiment subset metadata mismatch")
            project = client.read_project(project_id=str(saved_manifest["experiment_id"]))
            self._experiment_name = str(saved_manifest["experiment_name"])
            if saved_manifest.get("qids") != qids:
                saved_manifest["qids"] = qids
                runner._atomic_write_json(path, saved_manifest)
            return project, saved_manifest

        metadata = {**self._metadata(), "__ls_runner": "py_sdk_evaluate"}
        project = client.create_project(
            self._prefix,
            description="DeepResearch Bench II evaluation",
            reference_dataset_id=self._dataset_id,
            metadata=metadata,
            num_examples=len(qids),
            num_repetitions=1,
            evaluator_keys=["judge_score"],
        )
        self._experiment_name = str(project.name)
        manifest: dict[str, object] = {
            "schema_version": "1.0",
            "experiment_name": self._experiment_name,
            "experiment_id": str(project.id),
            "experiment_url": self._comparison_url(project),
            "dataset_name": self._dataset_name,
            "dataset_id": str(self._dataset_id),
            "dataset_hash": self._dataset_hash,
            "subset": self._subset_metadata,
            "qids": qids,
            "git_commit": self._git_commit,
            "prompt_commits": self._prompt_commits,
            "provider": self._provider,
            "model": self._model,
            "judge": "qwen-nonofficial",
            "official_judge": "not_run (requires GPT-5.5)",
            "concurrency": self._concurrency,
            "research_workers": self._research_workers,
            "timeouts": {
                "question_seconds": runner._question_timeout,
                "judge_seconds": runner._judge_timeout,
            },
        }
        runner._atomic_write_json(path, manifest)
        self._configure_view_overrides()
        return project, manifest

    def _configure_view_overrides(self) -> None:
        """Best-effort dataset view: show eval fields and hide cost columns."""
        request = getattr(self._client, "request_with_retries", None)
        if not callable(request):
            return
        path = f"/datasets/{self._dataset_id}/experiment-view-overrides"
        desired = [
            {"column": "outputs.qid", "hide": False},
            {"column": "outputs.status", "hide": False},
            {"column": "outputs.judge_score", "hide": False, "precision": 4},
            {"column": "outputs.answer_latency_s", "hide": False, "precision": 2},
            {"column": "outputs.judge_latency_s", "hide": False, "precision": 2},
            {"column": "outputs.total_tokens", "hide": False},
            {"column": "outputs.input_tokens", "hide": False},
            {"column": "outputs.output_tokens", "hide": False},
            {"column": "metrics.total_cost", "hide": True},
            {"column": "metrics.prompt_cost", "hide": True},
            {"column": "metrics.completion_cost", "hide": True},
        ]
        try:
            # LangSmith returns 404 when the dataset has no override yet. Treat
            # that as an empty configuration so the following POST can create it.
            from langsmith.utils import LangSmithNotFoundError

            response = request("GET", path, to_ignore=[LangSmithNotFoundError])
            existing: object = response.json() if response.status_code == 200 else None
            if isinstance(existing, list) and existing:
                current = existing[0]
            elif isinstance(existing, dict) and existing.get("id"):
                current = existing
            else:
                current = None
            if isinstance(current, dict):
                merged = {
                    str(item.get("column")): item for item in current.get("column_overrides", [])
                }
                merged.update({str(item["column"]): item for item in desired})
                request(
                    "PATCH",
                    f"{path}/{current['id']}",
                    request_kwargs={"json": {"column_overrides": list(merged.values())}},
                )
            else:
                request("POST", path, request_kwargs={"json": {"column_overrides": desired}})
        except Exception as e:  # noqa: BLE001
            log.warning("langsmith view override 配置失败（不影响评测）: %s", type(e).__name__)

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
        for index, q in enumerate(questions):
            if index >= limit:
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
        selected_qids = [q.qid for q in questions]
        project, experiment_manifest = self._load_or_create_experiment(
            out=out, qids=selected_qids, runner=runner
        )
        self._experiment_name = str(experiment_manifest["experiment_name"])

        async def predict(inputs: dict) -> dict:
            from langsmith.run_helpers import get_current_run_tree

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
            root = get_current_run_tree()
            if root is not None:
                root.add_tags([qid, self._experiment_name])
                root.add_metadata({"qid": qid, "language": q.language, "theme": q.theme})
            payload = await runner._process_one(q)
            payload["experiment_name"] = self._experiment_name
            payload["experiment_id"] = str(experiment_manifest["experiment_id"])
            if root is not None:
                payload["run_id"] = str(root.id)
                payload["trace_id"] = str(root.trace_id)
                try:
                    if self._client is None:
                        raise RuntimeError("LangSmith client is unavailable")
                    payload["trace_url"] = self._client.get_run_url(run=root)
                except Exception:  # noqa: BLE001
                    payload["trace_url"] = ""
                root.add_metadata(
                    {
                        "attempt": int(payload.get("attempt", 1)),
                        "status": str(payload.get("status", "")),
                        "failure_category": str(payload.get("failure_category", "")),
                    }
                )
            runner._atomic_write_json(runner._state_path(qid), payload)
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
                "answer_latency_s": float(payload.get("answer_latency_s", 0.0) or 0.0),
                "judge_latency_s": float(payload.get("judge_latency_s", 0.0) or 0.0),
                "input_tokens": int(payload.get("input_tokens", 0) or 0),
                "output_tokens": int(payload.get("output_tokens", 0) or 0),
                "total_tokens": int(payload.get("total_tokens", 0) or 0),
                "usage_by_stage": payload.get("usage_by_stage", {}),
                "failure_category": str(payload.get("failure_category", "")),
                "attempt": int(payload.get("attempt", 1)),
                "language": q.language,
                "theme": q.theme,
                "run_id": str(payload.get("run_id", "")),
                "trace_id": str(payload.get("trace_id", "")),
                "trace_url": str(payload.get("trace_url", "")),
                "usage_metadata": {
                    "input_tokens": int(payload.get("input_tokens", 0) or 0),
                    "output_tokens": int(payload.get("output_tokens", 0) or 0),
                    "total_tokens": int(payload.get("total_tokens", 0) or 0),
                },
            }

        def record_judge(run: Any, example: Any) -> dict:
            """零计算 pass-through：把本地已算好的分数提升为官方 feedback。"""
            out = getattr(run, "outputs", None) or {}
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
            experiment=project,
            max_concurrency=self._concurrency,
            client=self._client,
            error_handling="log",
        )
        self._experiment_name = results.experiment_name
        url = await results.get_comparison_url()
        if url:
            (out / "experiment_url.txt").write_text(url + "\n", encoding="utf-8")
        log.info("langsmith experiment 完成: %s -> %s", results.experiment_name, url)

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
        judge_timeout: float | None = None,
        question_timeout: float | None = None,
        concurrency: int = 1,
        research_workers: int = 1,
        subset_metadata: dict[str, object] | None = None,
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
        self._judge_timeout = judge_timeout
        self._question_timeout = question_timeout
        self._concurrency = max(1, int(concurrency))
        self._research_workers = max(1, int(research_workers))
        self._subset_metadata = subset_metadata or {}
        # 可选的 LangSmith experiment 上传器（None 表示不上传）。
        self._experiment_sink = experiment_sink

    def _state_path(self, qid: str) -> Path:
        return self._results_dir / f"{qid}.json"

    @staticmethod
    def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, path)
        finally:
            if tmp.exists():
                tmp.unlink()

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
            "judge_timeout_seconds": self._judge_timeout,
            "question_timeout_seconds": self._question_timeout,
            "concurrency": self._concurrency,
            "research_workers": self._research_workers,
            "subset": self._subset_metadata,
        }
        self._atomic_write_json(self._out / "config_snapshot.json", snap)

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
            verdict = await judge.score(
                answer,
                rubrics,
                blocked=blocked,
                dimensions=dims,
                total_timeout=self._judge_timeout,
            )
            score, per_item, reason = verdict
            return score, reason, per_item
        # Offline keyword judge (sync).
        score, reason = judge.score(answer, q.required_keywords)
        return score, reason, []

    async def _execute_one(self, q: EvalQuestion, *, started_at: float) -> EvalResult:
        """Execute one question without failure isolation or persistence."""
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
        if not quality_passed:
            raise RuntimeError("report quality gate failed")
        answer_latency = time.monotonic() - started_at
        judge_started = time.monotonic()
        score, reason, per_item = await self._judge_answer(answer, q)
        judge_latency = time.monotonic() - judge_started
        detail = json.dumps(per_item, ensure_ascii=False)
        citation_urls = [
            c.get("url", "") if isinstance(c, dict) else str(c) for c in citations_list
        ]
        return EvalResult(
            qid=q.qid,
            status="completed",
            question=q.question,
            answer=answer[:_MAX_STORED_ANSWER],
            citations=citation_urls,
            n_search_rounds=rounds,
            tokens_estimated=tokens,
            usage_estimated=usage_estimated,
            latency_s=time.monotonic() - started_at,
            answer_latency_s=answer_latency,
            judge_latency_s=judge_latency,
            judge_score=score,
            judge_reason=reason,
            judge_detail=detail,
        )

    async def _execute_one_bounded(self, q: EvalQuestion, *, started_at: float) -> EvalResult:
        """Run one question with a true end-to-end wall-clock deadline.

        The timeout covers research, report verification/synthesis and judging.
        A separate timer task is used instead of ``wait_for`` so a child that
        suppresses cancellation cannot turn the deadline into a generic error.
        Both tasks are always cancelled and reaped before this method returns.
        """
        if self._question_timeout is None:
            return await self._execute_one(q, started_at=started_at)

        return await run_with_hard_timeout(
            self._execute_one(q, started_at=started_at),
            timeout=self._question_timeout,
            label="question_timeout",
            cancel_grace=2.0,
        )

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
        previous_attempt = 0
        state_path = self._state_path(q.qid)
        if state_path.is_file():
            try:
                previous_attempt = int(
                    json.loads(state_path.read_text(encoding="utf-8")).get("attempt", 1)
                )
            except Exception:  # noqa: BLE001
                previous_attempt = 0
        with capture_usage(q.qid) as usage:
            try:
                res = await self._execute_one_bounded(q, started_at=t0)
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
        usage_snapshot = usage.snapshot()
        total_tokens = int(usage_snapshot["total_tokens"])
        if total_tokens > 0:
            res.input_tokens = int(usage_snapshot["input_tokens"])
            res.output_tokens = int(usage_snapshot["output_tokens"])
            res.total_tokens = total_tokens
            res.tokens_estimated = total_tokens
            res.usage_estimated = bool(usage_snapshot["estimated"])
            res.usage_by_stage = {
                stage: dict(values) for stage, values in usage_snapshot["by_stage"].items()
            }
        else:
            res.total_tokens = res.tokens_estimated
        res.attempt = previous_attempt + 1
        payload = asdict(res)
        # Flush immediately so a crash mid-run never loses a finished item.
        self._atomic_write_json(self._state_path(q.qid), payload)
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
        self._atomic_write_json(self._out / "summary.json", summary)

    async def run(self, *, max_questions: int | None = None) -> list[dict]:
        start = time.time()
        self._write_snapshot(start_time=start)
        done = self._load_done()
        limit = max_questions or self._config.max_questions_per_run

        selected = self._questions[:limit]
        pending = [q for q in selected if q.qid not in done]
        semaphore = asyncio.Semaphore(self._concurrency)

        async def process(q: EvalQuestion) -> tuple[str, dict]:
            async with semaphore:
                return q.qid, await self._process_one(q)

        fresh = dict(await asyncio.gather(*(process(q) for q in pending))) if pending else {}
        results: list[dict] = []
        for q in selected:
            payload = done.get(q.qid) or fresh[q.qid]
            # 可选：上传到 LangSmith Experiment（失败只记 warning，不阻塞）。
            # 注意：experiment 模式走 LangSmithExperimentSink.arun_experiment，
            # 这里仅保留对老式 sink 的兼容钩子。
            if self._experiment_sink is not None and hasattr(self._experiment_sink, "log_result"):
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
