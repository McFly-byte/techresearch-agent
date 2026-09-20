"""Eval dataset adapter.

This module defines the minimal question/answer format we use for offline
evals. We also ship a DeepResearchBench2Adapter that parses the official
JSONL format from https://github.com/imlrz/DeepResearch-Bench-II (CC BY 4.0 /
CC BY-NC 4.0, non-commercial; the dataset itself is NOT committed to git).

We do NOT ship the official dataset in git. The fixture dataset here is
synthetic and clearly labeled as test data.

Answerer isolation contract (P0-4):
- Research answerers receive ONLY an ``EvalPrompt`` (qid + question). They
  never see ``reference_answer`` or ``rubrics``.
- ``reference_answer`` / ``rubrics`` are read exclusively by the judge stage
  from the full ``EvalQuestion``. This prevents self-scoring leakage.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse


@dataclass(frozen=True)
class EvalPrompt:
    """The ONLY object a research answerer may see.

    Deliberately narrow: ``qid`` + ``question`` plus the research-time source
    constraints (``blocked_*`` / ``as_of_date``). These are NOT gold-label
    leakage: they are safety rules that already live verbatim in the prompt's
    ``**important**`` block. The answerer needs them to actually enforce the
    benchmark's "do not view this article" / "as-of year" rules; the judge still
    never reads them.
    """

    qid: str
    question: str
    # Sources the research system must never fetch/cite. From content.blocked
    # (and double-confirmed from the prompt's **important** block).
    blocked_urls: list[str] = field(default_factory=list)
    blocked_domains: list[str] = field(default_factory=list)
    blocked_titles: list[str] = field(default_factory=list)
    # Cutoff year (e.g. "2021"). Material dated after this year is dropped.
    as_of_date: str | None = None


@dataclass
class EvalQuestion:
    qid: str
    question: str
    # Reference facts / rubric items the answer must cover.
    # NOTE: these are judge-only. Answerers receive ``EvalPrompt``, not this.
    reference_answer: str
    required_keywords: list[str] = field(default_factory=list)
    # Tags for failure analysis.
    tags: list[str] = field(default_factory=list)
    # DeepResearch Bench II rubric items (one EvalQuestion per task).
    rubrics: list[str] = field(default_factory=list)
    # Per-dimension rubric breakdown: {"info_recall": [...], "analysis": [...], "presentation": [...]}
    rubrics_by_dimension: dict[str, list[str]] = field(default_factory=dict)
    # Blocked source references (title/authors/urls) — judge must penalize
    # answers that cite these. From the LEGACY DRB2 content.blocked dict shape.
    blocked: dict[str, object] = field(default_factory=dict)
    # 官方 DRB2 口径：content.blocked 是被屏蔽/不适用的 rubric 文本列表。
    # 评分器会把命中这些文本的 rubric 直接记 -1，不调用 LLM、不计入均值。
    blocked_rubrics: list[str] = field(default_factory=list)
    # Resolved source constraints for the RESEARCH system (not the judge).
    # Parsed from content.blocked urls + the prompt **important** block.
    # The EvalRunner copies these onto the EvalPrompt handed to the answerer.
    blocked_urls: list[str] = field(default_factory=list)
    blocked_domains: list[str] = field(default_factory=list)
    blocked_titles: list[str] = field(default_factory=list)
    as_of_date: str | None = None
    # Metadata from official dataset.
    language: str = ""  # "zh" / "en"
    theme: str = ""
    license: str = ""


@dataclass
class EvalResult:
    qid: str
    status: Literal["completed", "failed", "skipped"]
    question: str = ""
    answer: str = ""
    citations: list[str] = field(default_factory=list)
    n_search_rounds: int = 0
    tokens_estimated: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    usage_by_stage: dict[str, dict[str, object]] = field(default_factory=dict)
    # True when tokens_estimated is a character-derived estimate (fake /
    # heuristic), False when the provider reported real API usage.
    usage_estimated: bool = True
    latency_s: float = 0.0
    answer_latency_s: float = 0.0
    judge_latency_s: float = 0.0
    attempt: int = 1
    error: str = ""
    # Per-config metrics get computed downstream.
    judge_score: float = 0.0  # 0..1, set by judge
    judge_reason: str = ""
    # Per-rubric breakdown (JSON string) produced by LLMRubricJudge. Empty for
    # the offline KeywordJudge fallback.
    judge_detail: str = ""
    failure_category: str = ""  # retrieval / reasoning / hallucination / freshness / tool
    experiment_name: str = ""
    experiment_id: str = ""
    run_id: str = ""
    trace_id: str = ""
    trace_url: str = ""


class FixtureDataset:
    """A tiny synthetic dataset for offline tests. NOT a real benchmark."""

    name = "fixture"
    version = "v0.1"
    license_note = "synthetic, generated for tests; no external dataset"

    def questions(self) -> list[EvalQuestion]:
        return [
            EvalQuestion(
                qid="fx-001",
                question="What is LangGraph's primary abstraction?",
                reference_answer="A graph of nodes with typed state.",
                required_keywords=["graph", "state"],
                tags=["factual"],
                rubrics=["mentions typed state", "names graph nodes"],
            ),
            EvalQuestion(
                qid="fx-002",
                question="Does LlamaIndex focus on retrieval?",
                reference_answer="Yes, over external data.",
                required_keywords=["retrieval"],
                tags=["factual"],
                rubrics=["states retrieval focus"],
            ),
            EvalQuestion(
                qid="fx-003",
                question="Compare LangGraph and LlamaIndex briefly.",
                reference_answer="LangGraph is state-machine oriented; LlamaIndex is retrieval oriented.",
                required_keywords=["graph", "retrieval"],
                tags=["comparison"],
                rubrics=["compares both", "names graph", "names retrieval"],
            ),
        ]


def sha256_file(path: Path, *, chunk: int = 1 << 20) -> str:
    """Stream a file through SHA-256. Used to pin dataset identity in snapshots."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


# --- Source-constraint parsing (blocked urls / as-of year) -------------------
# These rules are not judge signals; they are research-time safety constraints
# that already appear verbatim in the prompt (the **important** block + the
# as-of sentence). We parse them so the research pipeline can enforce them.

#: Years we treat as plausible publication years when scanning citation text.
_YEAR_RE = re.compile(r"(19[89]\d|20[0-4]\d)")

#: as-of / cutoff date phrasings seen in DRB2 prompts. Order matters: try the
#: most specific (year-end) phrasings first so we never grab the wrong year.
_AS_OF_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"截至\s*(\d{4})\s*年"),
    re.compile(r"截止\s*到\s*(\d{4})\s*年底"),
    re.compile(r"截止\s*到\s*(\d{4})\s*年"),
    re.compile(r"截止\s*(\d{4})\s*年底"),
    re.compile(r"as of\s+(\d{4})", re.IGNORECASE),
    re.compile(r"through\s+(\d{4})", re.IGNORECASE),
)

#: URLs embedded in the prompt's "not allowed to view" block.
_PROMPT_URL_RE = re.compile(r"https?://[^\s'\"<>\]\)}>，。；]+", re.IGNORECASE)
_BLOCKED_PHRASE_RE = re.compile(r"not allowed to view", re.IGNORECASE)


def _normalize_domain(url: str) -> str:
    """Extract a lowercase, portless, www-stripped domain from a URL.

    ``www.mdpi.com`` -> ``mdpi.com``; ``ideas.repec.org`` stays as-is;
    ``pubmed.ncbi.nlm.nih.gov`` stays as-is. Returns "" on parse failure.
    """
    try:
        netloc = urlparse(url).netloc.lower()
    except Exception:  # noqa: BLE001 - never crash the adapter on a weird URL
        return ""
    if not netloc:
        return ""
    if ":" in netloc:
        netloc = netloc.split(":", 1)[0]
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return netloc


def _parse_blocked_urls_from_prompt(prompt: str) -> list[str]:
    """Pull the forbidden article URLs out of the prompt's **important** block.

    The official prompt appends a sentence like "you are not allowed to view
    the following article and urls: {...}". We locate that phrase and harvest
    every http(s) URL in the remainder. This is a double-check on
    ``content.blocked`` — same URLs, different source.
    """
    m = _BLOCKED_PHRASE_RE.search(prompt)
    if not m:
        return []
    tail = prompt[m.start() :]
    return [u.rstrip(".,;，。；") for u in _PROMPT_URL_RE.findall(tail)]


def _parse_as_of_date(prompt: str) -> str | None:
    """Extract the cutoff year from a DRB2 prompt, e.g. "截至2021年" -> "2021"."""
    for pat in _AS_OF_PATTERNS:
        m = pat.search(prompt)
        if m:
            return m.group(1)
    return None


def _dedupe(seq: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for s in seq:
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


class DeepResearchBench2Adapter:
    """Parse the official DeepResearch Bench II JSONL.

    Actual per-line JSON (verified against upstream commit):
      {
        "id": "task7",
        "idx": 1,
        "language": "zh",
        "theme": "Finance & Business",
        "description": "...",
        "prompt": "请就...撰写一份研究报告。...",
        "content": {
          "task": "<same as prompt>",
          "rubric": {
            "info_recall": ["rubric 1", ...],
            "analysis": ["rubric 1", ...],
            "presentation": ["rubric 1", ...]
          },
          "blocked": {"title": "...", "authors": [...], "urls": [...]}
        },
        "license": "CC BY 4.0"
      }

    Source: https://github.com/imlrz/DeepResearch-Bench-II
    License: per-task — 129 CC BY 4.0, 2 CC BY-NC 4.0 (idx=26,110), 1 CC0 (idx=119).
    The dataset is NOT committed to this repo; users must download it themselves
    and point this adapter at the local path.

    Schema validation (P1-4):
    - Every non-empty line MUST parse as JSON and MUST carry a non-empty
      ``prompt`` (question). Malformed lines are NOT silently skipped: they are
      recorded in ``self.errors`` (line number + reason) and reported by the
      CLI. ``questions()`` only returns well-formed items.
    - ``content.rubric`` may be absent/empty (then the question carries no
      rubrics and the judge falls back to keyword scoring); it is not fatal.
    """

    name = "deepresearch_bench_ii"
    version = "v1.0"
    license_note = "Per-task: 129 CC BY 4.0, 2 CC BY-NC 4.0, 1 CC0; see upstream DATA_LICENSE"

    def __init__(self, path: Path) -> None:
        self._path = path
        self.errors: list[dict[str, object]] = []

    def dataset_sha256(self) -> str:
        """SHA-256 of the raw dataset file, for run metadata."""
        return sha256_file(self._path)

    def questions(self) -> list[EvalQuestion]:
        out: list[EvalQuestion] = []
        with self._path.open(encoding="utf-8") as f:
            for i, line in enumerate(f):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError as e:
                    self.errors.append({"line": i + 1, "error": f"invalid json: {e}"})
                    continue
                # Official format: question is in "prompt" (and duplicated in content.task)
                task = obj.get("prompt", "") or (obj.get("content") or {}).get("task", "")
                if not isinstance(task, str) or not task.strip():
                    self.errors.append({"line": i + 1, "error": "missing or empty 'prompt' field"})
                    continue
                # Rubrics live in content.rubric as a dimension-keyed dict
                content = obj.get("content") or {}
                rubrics_raw = content.get("rubric", {}) or {}
                all_rubrics: list[str] = []
                rubrics_by_dim: dict[str, list[str]] = {}
                if isinstance(rubrics_raw, dict):
                    for dim, items in rubrics_raw.items():
                        dim_items: list[str] = []
                        if isinstance(items, list):
                            dim_items = [str(x) for x in items if str(x).strip()]
                        rubrics_by_dim[dim] = dim_items
                        all_rubrics.extend(dim_items)
                # Blocked：官方口径是被屏蔽/不适用的 rubric 文本列表；
                # 旧版本地形态是 {"title","authors","urls"} 来源引用 dict。
                blocked_raw = content.get("blocked", {})
                blocked_refs: dict[str, object] = {}
                blocked_rubrics: list[str] = []
                if isinstance(blocked_raw, list):
                    blocked_rubrics = [str(x) for x in blocked_raw if str(x).strip()]
                elif isinstance(blocked_raw, dict):
                    blocked_refs = blocked_raw

                # --- Research-time source constraints (NOT judge leakage) ----
                # 1) Exact blocked URLs: from content.blocked.urls.
                c_blocked_urls: list[str] = []
                urls_field = blocked_refs.get("urls")
                if isinstance(urls_field, list):
                    c_blocked_urls = [str(u) for u in urls_field if str(u).strip()]
                # 2) Double protection: also harvest URLs from the prompt's
                #    "not allowed to view" block (same URLs, redundant source).
                prompt_blocked_urls = _parse_blocked_urls_from_prompt(task)
                blocked_urls = _dedupe(c_blocked_urls + prompt_blocked_urls)
                # 3) Domains: normalized netloc of every blocked URL.
                blocked_domains = _dedupe(
                    [d for d in (_normalize_domain(u) for u in blocked_urls) if d]
                )
                # 4) Blocked titles (substring match on search/citation titles).
                blocked_titles: list[str] = []
                title_field = blocked_refs.get("title")
                if isinstance(title_field, str) and title_field.strip():
                    blocked_titles = [title_field.strip()]
                # 5) As-of cutoff year parsed from the prompt body.
                as_of_date = _parse_as_of_date(task)

                # qid: stable per-row id. Prefer upstream "id", else row ordinal.
                upstream_id = obj.get("id") or obj.get("qid")
                qid = str(upstream_id) if upstream_id else f"drb2-{i:04d}"
                out.append(
                    EvalQuestion(
                        qid=qid,
                        question=task.strip(),
                        reference_answer="",  # DRB2 has no reference_answer; rubrics are the gold signal
                        required_keywords=[],
                        tags=["deepresearch_bench_ii"],
                        rubrics=all_rubrics,
                        rubrics_by_dimension=rubrics_by_dim,
                        blocked=blocked_refs,
                        blocked_rubrics=blocked_rubrics,
                        blocked_urls=blocked_urls,
                        blocked_domains=blocked_domains,
                        blocked_titles=blocked_titles,
                        as_of_date=as_of_date,
                        language=str(obj.get("language", "")),
                        theme=str(obj.get("theme", "")),
                        license=str(obj.get("license", "")),
                    )
                )
        return out


__all__ = [
    "DeepResearchBench2Adapter",
    "EvalPrompt",
    "EvalQuestion",
    "EvalResult",
    "FixtureDataset",
    "_dedupe",
    "_normalize_domain",
    "_parse_as_of_date",
    "_parse_blocked_urls_from_prompt",
    "sha256_file",
]
