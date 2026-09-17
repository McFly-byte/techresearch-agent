"""`tra-eval` CLI: run the DeepResearch eval harness.

Usage (from project root):
    python -m evals.cli run --dataset data/drb2.jsonl --mode live --judge llm
    python -m evals.cli run                      # offline smoke on the fixture (3 qs)
    python -m evals.cli run --resume --output-dir runs/full_2026...

Full 132-question runs are guarded: without ``--limit`` AND ``--confirm-full``
the harness runs a 3-question smoke slice.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

import click

from evals.adapter import DeepResearchBench2Adapter, FixtureDataset
from evals.baselines import make_research_runner_answerer
from evals.configs import CONFIGS
from evals.metrics import KeywordJudge, LLMRubricJudge
from evals.runner import EvalRunner


def _resolve_judge(kind: str):
    """Build the judge. LLM judge reuses the research system's provider."""
    if kind == "keyword":
        return KeywordJudge(), "keyword", "offline-keyword"
    from core.config import get_settings
    from core.providers.factory import build_provider

    llm = build_provider()
    settings = get_settings()
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
    help="fake = offline deterministic; live = real Qwen + Tavily.",
)
@click.option(
    "--judge",
    "judge_kind",
    type=click.Choice(["keyword", "llm"]),
    default="llm",
    help="Scoring method. llm = LLM rubric judge (default); keyword = offline fallback.",
)
@click.option("--limit", type=int, default=None, help="Run only the first N questions.")
@click.option(
    "--concurrency", type=int, default=1, help="Reserved: parallelism. Currently serial (1)."
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
def run(
    dataset: Path | None,
    mode: str,
    judge_kind: str,
    limit: int | None,
    concurrency: int,
    output_dir: Path | None,
    resume: bool,
    confirm_full: bool,
) -> None:
    """Run the eval harness over a dataset."""
    if concurrency != 1:
        click.echo("note: --concurrency>1 is reserved; running serial (1).", err=True)

    # --- Load dataset -------------------------------------------------------
    adapter_errors: list[dict[str, object]] = []
    dataset_hash = ""
    if dataset is not None:
        if not dataset.is_file():
            click.echo(f"dataset not found: {dataset}", err=True)
            sys.exit(2)
        adapter = DeepResearchBench2Adapter(dataset)
        questions = adapter.questions()
        adapter_errors = adapter.errors
        dataset_hash = adapter.dataset_sha256()
        config_name = "drb2"
    else:
        questions = FixtureDataset().questions()
        config_name = "fixture"

    if adapter_errors:
        click.echo(f"warning: {len(adapter_errors)} malformed dataset line(s):", err=True)
        for e in adapter_errors:
            click.echo(f"  line {e.get('line')}: {e.get('error')}", err=True)

    n_total = len(questions)

    # --- Budget guard: default to a 3-question smoke unless confirmed --------
    if limit is None and not confirm_full:
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

    # --- Assemble runner ----------------------------------------------------
    config = CONFIGS["full"]
    answerer = make_research_runner_answerer(mode=mode)
    judge, judge_kind_resolved, provider_model = _resolve_judge(judge_kind)
    provider = provider_model.split("/")[0]
    model = provider_model.split("/", 1)[1] if "/" in provider_model else "unknown"

    runner = EvalRunner(
        out_dir=out,
        config=config,
        questions=questions,
        answerer=answerer,
        judge=judge,
        dataset_hash=dataset_hash,
        provider=provider,
        model=model,
    )

    click.echo(
        f"eval run: dataset={config_name} n={n_total} limit={limit} mode={mode} "
        f"judge={judge_kind_resolved} out={out}"
    )
    results = runner.run_sync(max_questions=limit)

    completed = sum(1 for r in results if r.get("status") == "completed")
    failed = sum(1 for r in results if r.get("status") == "failed")
    click.echo(f"done: {completed} completed, {failed} failed -> {out / 'summary.json'}")


if __name__ == "__main__":
    main()
