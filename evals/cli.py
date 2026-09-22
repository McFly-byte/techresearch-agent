"""`tra-eval` CLI: run the DeepResearch eval harness.

Usage (from project root):
    python -m evals.cli run --dataset data/drb2.jsonl --mode live --judge llm
    python -m evals.cli run                      # offline smoke on the fixture (3 qs)
    python -m evals.cli run --resume --output-dir runs/full_2026...

Full 132-question runs are guarded: without ``--limit`` AND ``--confirm-full``
the harness runs a 3-question smoke slice.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import click

from evals.adapter import DeepResearchBench2Adapter, FixtureDataset
from evals.baselines import make_research_runner_answerer
from evals.configs import CONFIGS
from evals.metrics import KeywordJudge, LLMRubricJudge
from evals.runner import (
    LANGSMITH_DATASET_NAME,
    EvalRunner,
    LangSmithExperimentSink,
    _git_commit,
    _prompt_commits,
)
from evals.subset import apply_subset, load_subset


def _resolve_judge(kind: str):
    """Build the judge. LLM judge reuses the research system's provider."""
    if kind == "keyword":
        return KeywordJudge(), "keyword", "offline-keyword"
    from core.config import get_settings
    from core.providers.factory import build_provider

    llm = build_provider()
    settings = get_settings()
    if hasattr(llm, "with_model"):
        llm = llm.with_model(settings.judge_model())
    if hasattr(llm, "with_thinking"):
        llm = llm.with_thinking(False)
    provider_name = settings.resolved_provider()
    return LLMRubricJudge(llm=llm), "llm", f"{provider_name}/{getattr(llm, 'model_id', '')}"


@click.group()
def main() -> None:
    """DeepResearch eval harness CLI."""


@main.command("run")
@click.option(
    "--dataset",
    "dataset",
    type=click.Path(path_type=Path),
    default=None,
    help="Path to a DeepResearch Bench II JSONL dataset. Omit to use the fixture.",
)
@click.option(
    "--mode",
    type=click.Choice(["fake", "live"]),
    default="fake",
    help="fake = offline deterministic; live = real configured LLM + Tavily.",
)
@click.option(
    "--judge",
    "judge_kind",
    type=click.Choice(["keyword", "llm"]),
    default="llm",
    help="Scoring method. llm = LLM rubric judge (default); keyword = offline fallback.",
)
@click.option(
    "--judge-timeout",
    type=float,
    default=240.0,
    help="Per-question LLM judge hard timeout in seconds (default 240).",
)
@click.option(
    "--question-timeout",
    type=click.FloatRange(min=0.0, min_open=True),
    default=900.0,
    help="End-to-end wall-clock timeout per question in seconds (default 900 = 15min).",
)
@click.option(
    "--research-timeout",
    type=click.FloatRange(min=0.0, min_open=True),
    default=420.0,
    help="Research-stage hard timeout in seconds (default 420).",
)
@click.option(
    "--write-timeout",
    type=click.FloatRange(min=0.0, min_open=True),
    default=180.0,
    help="Verification and synthesis hard timeout in seconds (default 180).",
)
@click.option("--limit", type=int, default=None, help="Run only the first N questions.")
@click.option(
    "--concurrency",
    type=click.IntRange(min=1),
    default=2,
    help="Maximum questions evaluated concurrently (default 2).",
)
@click.option(
    "--research-workers",
    type=click.IntRange(min=1),
    default=2,
    help="Maximum research workers inside each question (default 2).",
)
@click.option(
    "--subset",
    type=str,
    default=None,
    help="Fixed qid manifest, e.g. core10. Validates source dataset hash.",
)
@click.option(
    "--output-dir",
    "output_dir",
    type=click.Path(path_type=Path),
    default=None,
    help="Directory for results/summary. Defaults to runs/<config>_<ts>.",
)
@click.option(
    "--resume",
    is_flag=True,
    default=False,
    help="Resume into an existing --output-dir: skip completed, retry failed.",
)
@click.option(
    "--confirm-full",
    is_flag=True,
    default=False,
    help="Allow running the full dataset without --limit.",
)
@click.option(
    "--experiment/--no-experiment",
    default=True,
    help="完成每题后上传到 LangSmith Experiment（默认开启；需指定 --dataset）。",
)
@click.option(
    "--experiment-name",
    default=None,
    help="可选：自定义 LangSmith Experiment 名前缀。默认按 full/pilot-v2 + 时间戳生成。",
)
def run(
    dataset: Path | None,
    mode: str,
    judge_kind: str,
    judge_timeout: float,
    question_timeout: float,
    research_timeout: float,
    write_timeout: float,
    limit: int | None,
    concurrency: int,
    research_workers: int,
    subset: str | None,
    output_dir: Path | None,
    resume: bool,
    confirm_full: bool,
    experiment: bool,
    experiment_name: str | None,
) -> None:
    """Run the eval harness over a dataset."""
    # --- Load dataset -------------------------------------------------------
    adapter_errors: list[dict[str, object]] = []
    dataset_hash = ""
    subset_metadata: dict[str, object] = {}
    if dataset is not None:
        if not dataset.is_file():
            click.echo(f"dataset not found: {dataset}", err=True)
            sys.exit(2)
        adapter = DeepResearchBench2Adapter(dataset)
        questions = adapter.questions()
        adapter_errors = adapter.errors
        dataset_hash = adapter.dataset_sha256()
        config_name = "drb2"
        if subset:
            try:
                manifest = load_subset(subset)
                questions = apply_subset(questions, manifest, dataset_hash=dataset_hash)
                subset_metadata = manifest.metadata()
                config_name = manifest.name
            except ValueError as subset_error:
                click.echo(str(subset_error), err=True)
                sys.exit(2)
    else:
        if subset:
            click.echo("--subset requires --dataset", err=True)
            sys.exit(2)
        questions = FixtureDataset().questions()
        config_name = "fixture"

    if adapter_errors:
        click.echo(f"warning: {len(adapter_errors)} malformed dataset line(s):", err=True)
        for adapter_error in adapter_errors:
            click.echo(
                f"  line {adapter_error.get('line')}: {adapter_error.get('error')}", err=True
            )

    n_total = len(questions)

    # --- Budget guard: default to a 3-question smoke unless confirmed --------
    if limit is None and subset:
        limit = n_total
    elif limit is None and not confirm_full:
        limit = 3
        click.echo(
            f"no --limit and no --confirm-full: running a 3-question smoke slice "
            f"(of {n_total} total). Re-run with --confirm-full for the full set.",
            err=True,
        )
    elif limit is None:
        limit = n_total  # confirmed full

    # --- Output dir ---------------------------------------------------------
    if resume:
        if output_dir is None:
            click.echo("--resume requires --output-dir to resume into.", err=True)
            sys.exit(2)
        out = output_dir
    else:
        if output_dir is None:
            ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            out = Path("evals") / "runs" / f"{config_name}_{mode}_{ts}"
        else:
            out = output_dir
    out.mkdir(parents=True, exist_ok=True)
    if resume:
        snapshot_path = out / "config_snapshot.json"
        if snapshot_path.is_file():
            try:
                prior_run = json.loads(snapshot_path.read_text(encoding="utf-8")).get("run", {})
            except Exception as e:  # noqa: BLE001
                click.echo(f"invalid resume config snapshot: {type(e).__name__}", err=True)
                sys.exit(2)
            if prior_run.get("dataset_hash") != dataset_hash:
                click.echo("resume rejected: dataset hash mismatch", err=True)
                sys.exit(2)
            if prior_run.get("subset", {}) != subset_metadata:
                click.echo("resume rejected: subset metadata mismatch", err=True)
                sys.exit(2)
            if int(prior_run.get("research_workers", 1)) != research_workers:
                click.echo("resume rejected: research worker count mismatch", err=True)
                sys.exit(2)

    # --- Assemble runner ----------------------------------------------------
    config = CONFIGS["full"]
    answerer = make_research_runner_answerer(
        mode=mode,
        research_timeout=research_timeout,
        write_timeout=write_timeout,
        research_workers=research_workers,
    )
    judge, judge_kind_resolved, provider_model = _resolve_judge(judge_kind)
    provider = provider_model.split("/")[0]
    model = provider_model.split("/", 1)[1] if "/" in provider_model else "unknown"

    # --- Optional: LangSmith Experiment 上传 --------------------------------
    # 用官方 aevaluate 驱动：指定 --dataset 即启用。live = 真实 Qwen+Tavily；
    # fake = 离线 answerer/judge，用于不烧钱地验证 experiment 管线。
    experiment_sink = None
    if experiment and dataset is not None:
        ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        if experiment_name:
            exp_prefix = experiment_name
        elif subset:
            exp_prefix = (
                f"{config_name}-q{concurrency}-w{research_workers}-"
                f"{_git_commit()[:8]}-{ts}"
            )
        elif confirm_full and limit >= n_total:
            # v2 名：与旧坏结果隔离，不覆盖。
            exp_prefix = f"drb2-full-live-qwen-v2-{ts}"
        else:
            exp_prefix = f"drb2-pilot-live-qwen-v2-{ts}"
        experiment_sink = LangSmithExperimentSink(
            experiment_prefix=exp_prefix,
            dataset_name=LANGSMITH_DATASET_NAME,
            dataset_hash=dataset_hash,
            provider=provider,
            model=model,
            judge_model=model,
            prompt_commits=_prompt_commits(),
            git_commit=_git_commit(),
            research_mode=mode,
            concurrency=concurrency,
            research_workers=research_workers,
            subset_metadata=subset_metadata,
        )
        click.echo(
            f"experiment: {'enabled' if experiment_sink.enabled else 'disabled'} "
            f"(prefix={exp_prefix})"
        )

    runner = EvalRunner(
        out_dir=out,
        config=config,
        questions=questions,
        answerer=answerer,
        judge=judge,
        dataset_hash=dataset_hash,
        provider=provider,
        model=model,
        experiment_sink=None,  # experiment 模式走 sink.arun_experiment，不走内嵌钩子
        judge_timeout=judge_timeout,
        question_timeout=question_timeout,
        concurrency=concurrency,
        research_workers=research_workers,
        subset_metadata=subset_metadata,
    )

    click.echo(
        f"eval run: dataset={config_name} n={n_total} limit={limit} mode={mode} "
        f"judge={judge_kind_resolved} concurrency={concurrency} "
        f"research_workers={research_workers} "
        f"question_timeout={question_timeout:g}s subset={subset or 'none'} out={out}"
    )
    import asyncio

    if experiment_sink is not None and experiment_sink.enabled:
        # 官方 aevaluate 驱动整个实验（含 resume/写 summary）。
        results = asyncio.run(
            experiment_sink.arun_experiment(
                runner=runner,
                questions=questions,
                limit=limit,
                start_time=datetime.now(UTC).timestamp(),
            )
        )
        if getattr(experiment_sink, "experiment_name", ""):
            click.echo(f"experiment_name: {experiment_sink.experiment_name}")
    else:
        results = runner.run_sync(max_questions=limit)

    completed = sum(1 for r in results if r.get("status") == "completed")
    failed = sum(1 for r in results if r.get("status") == "failed")
    click.echo(f"done: {completed} completed, {failed} failed -> {out / 'summary.json'}")


if __name__ == "__main__":
    main()
