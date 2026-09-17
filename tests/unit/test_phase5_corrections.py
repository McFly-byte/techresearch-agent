"""Phase 5 correction tests: dataset adapter, baselines, charts, failure
classification, config snapshot, cost guard."""

from __future__ import annotations

import json
from pathlib import Path

from evals.adapter import DeepResearchBench2Adapter, EvalResult, FixtureDataset
from evals.baselines import BASELINES
from evals.charts import build_charts
from evals.configs import CONFIGS
from evals.failures import classify_failure


# --- Dataset adapter --------------------------------------------------------
def test_fixture_dataset_has_rubrics() -> None:
    qs = FixtureDataset().questions()
    assert len(qs) == 3
    assert qs[0].rubrics, "fixture should carry rubrics"


def test_drbench2_adapter_parses_official_format(tmp_path: Path) -> None:
    p = tmp_path / "drb2.jsonl"
    p.write_text(
        json.dumps(
            {
                "id": "task1",
                "prompt": "What is X?",
                "language": "en",
                "theme": "Science",
                "content": {
                    "task": "What is X?",
                    "rubric": {
                        "info_recall": ["names X", "cites source"],
                        "analysis": ["compares Y"],
                        "presentation": ["uses table"],
                    },
                    "blocked": {
                        "title": "Source Article",
                        "authors": ["A"],
                        "urls": ["https://example.com"],
                    },
                },
                "license": "CC BY 4.0",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    adapter = DeepResearchBench2Adapter(p)
    qs = adapter.questions()
    assert len(qs) == 1
    assert qs[0].question == "What is X?"
    assert qs[0].qid == "task1"
    assert len(qs[0].rubrics) == 4  # 2+1+1
    assert qs[0].rubrics_by_dimension["info_recall"] == ["names X", "cites source"]
    assert qs[0].blocked.get("title") == "Source Article"
    assert qs[0].language == "en"
    assert qs[0].theme == "Science"


# --- Baselines --------------------------------------------------------------
def test_baselines_registered() -> None:
    assert set(BASELINES) == {"llm_only", "naive_rag", "full"}


# --- Charts -----------------------------------------------------------------
def test_build_charts_writes_html(tmp_path: Path) -> None:
    results = tmp_path / "results"
    results.mkdir()
    (results / "q1.json").write_text(
        json.dumps({"qid": "q1", "judge_score": 0.8, "status": "completed"}),
        encoding="utf-8",
    )
    out = tmp_path / "charts"
    paths = build_charts(results, out)
    assert paths["bar"].exists()
    html = paths["bar"].read_text(encoding="utf-8")
    assert "FIXTURE" in html


# --- Failure classification -------------------------------------------------
def test_classify_failure_tool() -> None:
    r = EvalResult(qid="q", status="failed", error="429 rate limit exceeded")
    assert classify_failure(r) == "tool_failure"


def test_classify_failure_retrieval() -> None:
    r = EvalResult(qid="q", status="failed", error="fetch https://x failed")
    assert classify_failure(r) == "retrieval_failure"


def test_classify_failure_success_is_empty() -> None:
    r = EvalResult(qid="q", status="completed", error="")
    assert classify_failure(r) == ""


# --- Config snapshot + cost guard -------------------------------------------
def test_config_snapshot_has_required_fields() -> None:
    snap = CONFIGS["full"].snapshot()
    for k in ("python", "platform", "git_commit", "prompt_version", "code_version", "config"):
        assert k in snap, f"missing {k}"


def test_cost_guard_default_is_10() -> None:
    assert CONFIGS["full"].max_questions_per_run == 10
    assert CONFIGS["full"].max_questions_per_run <= 10


def test_ablation_configs_only_change_one_knob() -> None:
    full = CONFIGS["full"]
    for name in ("no_verifier", "no_reflection", "single_agent", "no_long_term_memory"):
        c = CONFIGS[name]
        # Same seed, max_workers, max_iterations, max_search_rounds.
        assert c.seed == full.seed
        assert c.max_workers == full.max_workers
