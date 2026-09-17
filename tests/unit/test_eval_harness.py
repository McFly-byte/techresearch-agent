"""Phase 5 eval harness tests (offline, no paid calls)."""

from __future__ import annotations

import json
from pathlib import Path

from evals.adapter import FixtureDataset
from evals.configs import CONFIGS
from evals.metrics import KeywordJudge, compute_metrics
from evals.runner import EvalRunner


def test_configs_toggle_one_component_each():  # type: ignore[no-untyped-def]
    full = CONFIGS["full"]
    for name in ["llm_only", "naive_rag", "no_verifier", "no_reflection", "single_agent"]:
        c = CONFIGS[name]
        # Every ablation must share seed / max_workers / max_iterations with full.
        assert c.seed == full.seed
        assert c.max_workers == full.max_workers
        assert c.max_iterations == full.max_iterations


def test_config_snapshot_is_json_serializable():  # type: ignore[no-untyped-def]
    snap = CONFIGS["full"].snapshot()
    json.dumps(snap)  # must not raise
    assert "config" in snap
    assert snap["config"]["name"] == "full"


def test_metrics_zero_denominator():  # type: ignore[no-untyped-def]
    m = compute_metrics([])
    assert m.n == 0
    assert m.avg_judge_score == 0.0


def test_keyword_judge():  # type: ignore[no-untyped-def]
    j = KeywordJudge()
    s, _ = j.score("LangGraph is a graph of state", ["graph", "state"])
    assert s == 1.0
    s2, _ = j.score("LlamaIndex does retrieval", ["graph", "state"])
    assert s2 == 0.0


def test_runner_resumable_no_double_billing(tmp_path: Path):  # type: ignore[no-untyped-def]
    ds = FixtureDataset()
    calls = {"n": 0}

    def answerer(prompt):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        # Answerer sees ONLY qid + question (EvalPrompt). No reference_answer.
        return f"answer for {prompt.qid}", 1, 50

    out = tmp_path / "run1"
    runner = EvalRunner(
        out_dir=out, config=CONFIGS["full"], questions=ds.questions(), answerer=answerer
    )
    runner.run_sync()
    first = calls["n"]
    assert first == len(ds.questions())

    # Re-run: completed questions are skipped, no new answerer calls.
    runner2 = EvalRunner(
        out_dir=out, config=CONFIGS["full"], questions=ds.questions(), answerer=answerer
    )
    runner2.run_sync()
    assert calls["n"] == first, "resume should not re-bill completed questions"

    # Config snapshot exists.
    assert (out / "config_snapshot.json").exists()
    assert (out / "summary.json").exists()


def test_runner_isolates_single_failure(tmp_path: Path):  # type: ignore[no-untyped-def]
    ds = FixtureDataset()

    def answerer(prompt):  # type: ignore[no-untyped-def]
        if prompt.qid == "fx-002":
            raise RuntimeError("boom")
        return f"answer for {prompt.qid}", 1, 50

    out = tmp_path / "run2"
    runner = EvalRunner(
        out_dir=out, config=CONFIGS["full"], questions=ds.questions(), answerer=answerer
    )
    results = runner.run_sync()
    by_id = {r["qid"]: r for r in results}
    assert by_id["fx-001"]["status"] == "completed"
    assert by_id["fx-002"]["status"] == "failed"
    assert by_id["fx-003"]["status"] == "completed"
    # Failed question stays in denominator (summary.n_failed == 1).
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert summary["n"] == 3
    assert summary["n_failed"] == 1


def test_fixture_dataset_has_license_note():  # type: ignore[no-untyped-def]
    ds = FixtureDataset()
    assert ds.license_note
    assert len(ds.questions()) >= 3
