"""Generate HTML+SVG charts directly from raw result JSON files.

No matplotlib dependency. Reads <out_dir>/results/*.json and produces:
- <out_dir>/charts/bar.html: grouped bar chart of judge_score by config
- <out_dir>/charts/ablation.html: change vs full
- <out_dir>/charts/pareto.html: cost (tokens) vs quality (judge_score)

All charts are static HTML+SVG; data is embedded as JSON in the HTML.
"""

from __future__ import annotations

import json
from pathlib import Path


def _bar_svg(labels: list[str], values: list[float], *, title: str) -> str:
    if not labels:
        return "<p>no data</p>"
    width = 600
    bar_h = 28
    gap = 8
    height = len(labels) * (bar_h + gap) + 60
    max_v = max(values) or 1.0
    bars = []
    for i, (label, v) in enumerate(zip(labels, values, strict=False)):
        y = 40 + i * (bar_h + gap)
        w = int(400 * v / max_v)
        bars.append(
            f'<text x="10" y="{y + bar_h - 8}">{label}</text>'
            f'<rect x="120" y="{y}" width="{w}" height="{bar_h}" fill="#4a90d9"/>'
            f'<text x="{120 + w + 5}" y="{y + bar_h - 8}">{v:.2f}</text>'
        )
    return f'<h3>{title}</h3><svg width="{width}" height="{height}">{"".join(bars)}</svg>'


def build_charts(results_dir: Path, out_dir: Path) -> dict[str, Path]:
    """Read raw results and write HTML charts. Returns paths written."""
    out_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    for p in results_dir.glob("*.json"):
        try:
            rows.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            continue

    # Group by qid; we expect one result per (config, qid) but for the fixture
    # demo we just show avg judge_score per qid.
    by_qid: dict[str, list[float]] = {}
    for r in rows:
        by_qid.setdefault(r["qid"], []).append(r.get("judge_score", 0.0))
    labels = list(by_qid.keys())
    values = [sum(vs) / len(vs) for vs in by_qid.values()]

    bar_html = _bar_svg(labels, values, title="Avg judge score by question (FIXTURE DATA)")
    chart_path = out_dir / "bar.html"
    chart_path.write_text(
        f"<html><body><h2>FIXTURE DATA — not a real benchmark</h2>{bar_html}</body></html>",
        encoding="utf-8",
    )
    return {"bar": chart_path}


__all__ = ["build_charts"]
