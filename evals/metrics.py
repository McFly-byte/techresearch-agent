"""Eval metrics. All ratios handle zero denominators explicitly."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass

from core.prompts import PromptRegistry, get_default_registry
from core.providers.base import BaseLLMProvider, Message
from evals.adapter import EvalResult

log = logging.getLogger(__name__)

# Threshold at/below which a question counts as "passed" for pass_rate.
PASS_THRESHOLD = 0.5


@dataclass
class EvalMetrics:
    n: int = 0
    n_failed: int = 0
    avg_judge_score: float = 0.0
    avg_citation_precision: float = 0.0
    avg_search_rounds: float = 0.0
    avg_tokens: float = 0.0
    avg_latency_s: float = 0.0
    n_passed: int = 0

    @property
    def pass_rate(self) -> float:
        """Fraction of ALL questions with judge_score >= threshold.

        Failed questions are NOT counted as passed (they stay in the
        denominator). Zero if n==0.
        """
        if self.n == 0:
            return 0.0
        return self.n_passed / self.n


def compute_metrics(results: list[EvalResult]) -> EvalMetrics:
    n = len(results)
    if n == 0:
        return EvalMetrics()
    completed = [r for r in results if r.status == "completed"]
    n_done = len(completed)
    n_failed = n - n_done
    avg_judge = sum(r.judge_score for r in completed) / n if n else 0.0
    avg_rounds = sum(r.n_search_rounds for r in completed) / n if n else 0.0
    avg_tokens = sum(r.tokens_estimated for r in completed) / n if n else 0.0
    avg_lat = sum(r.latency_s for r in completed) / n if n else 0.0
    n_passed = sum(1 for r in completed if r.judge_score >= PASS_THRESHOLD)
    return EvalMetrics(
        n=n,
        n_failed=n_failed,
        avg_judge_score=avg_judge,
        avg_search_rounds=avg_rounds,
        avg_tokens=avg_tokens,
        avg_latency_s=avg_lat,
        n_passed=n_passed,
    )


class KeywordJudge:
    """Offline judge: fraction of required keywords present in the answer.

    NOT an LLM judge. It is a deterministic, reproducible stand-in so the
    runner works without paid calls. Kept as a fallback; the default judge is
    ``LLMRubricJudge``.
    """

    name = "keyword-v1"
    kind = "keyword"

    def score(self, answer: str, required_keywords: list[str]) -> tuple[float, str]:
        if not required_keywords:
            return 1.0, "no required keywords"
        a = answer.lower()
        hits = sum(1 for k in required_keywords if k.lower() in a)
        ratio = hits / len(required_keywords)
        return ratio, f"{hits}/{len(required_keywords)} keywords"


class _JudgeError(RuntimeError):
    """Redacted error for a single rubric scoring failure."""


class LLMRubricJudge:
    """LLM rubric judge: score each rubric item 0/1 and average.

    Contract:
    - Input: ``(answer: str, rubrics: list[str])``.
    - For EACH rubric item, call the same Qwen provider used by the research
      system (injected via ``llm``) and ask whether the answer satisfies it.
    - Returns ``(mean_score, per_item_scores, reason)``:
        * ``mean_score`` in [0, 1] (fraction of satisfied rubrics).
        * ``per_item_scores`` is a list of ``{"rubric", "satisfied", "reason"}``.
        * ``reason`` is a short human-readable summary.
    - Timeout + bounded retry per rubric. A single failed/undecodable rubric
      scores 0 (never crashes the whole question) and is flagged in the reason.

    The judge prompts live in the Prompt Hub manifest (``eval_rubric_judge_system``
    / ``eval_rubric_judge_user``) — no hardcoded prompt text at the call site.
    """

    name = "llm-rubric-v1"
    kind = "llm"

    def __init__(
        self,
        llm: BaseLLMProvider,
        *,
        registry: PromptRegistry | None = None,
        timeout: float = 30.0,
        max_retries: int = 2,
    ) -> None:
        self._llm = llm
        self._registry = registry or get_default_registry()
        self._timeout = timeout
        self._max_retries = max_retries
        # Render the system prompt once (local mode is zero-network).
        self._sys = self._registry.render("eval_rubric_judge_system").system_text()
        self.model_id = getattr(llm, "model_id", "")

    async def score(
        self, answer: str, rubrics: list[str]
    ) -> tuple[float, list[dict[str, object]], str]:
        if not rubrics:
            return 1.0, [], "no rubrics to score"
        per_item: list[dict[str, object]] = []
        for rubric in rubrics:
            satisfied, reason = await self._score_one(answer, rubric)
            per_item.append({"rubric": rubric, "satisfied": satisfied, "reason": reason})
        n_hit = sum(1 for p in per_item if p["satisfied"] == 1)
        mean = n_hit / len(per_item)
        failed = [p for p in per_item if str(p["reason"]).startswith("error:")]
        suffix = f"; {len(failed)} errored rubric(s)" if failed else ""
        reason = f"{n_hit}/{len(per_item)} rubrics satisfied{suffix}"
        return mean, per_item, reason

    async def _score_one(self, answer: str, rubric: str) -> tuple[int, str]:
        rendered = self._registry.render("eval_rubric_judge_user", rubric=rubric, answer=answer)
        messages = [Message(role="system", content=self._sys)]
        for _role, content in rendered.messages:
            messages.append(Message(role="user", content=content))

        last_err = ""
        for _attempt in range(self._max_retries + 1):
            try:
                resp = await asyncio.wait_for(self._llm.acomplete(messages), timeout=self._timeout)
            except TimeoutError as e:
                last_err = f"timeout: {type(e).__name__}"
                continue
            except Exception as e:  # noqa: BLE001 - redact provider body
                last_err = f"provider: {type(e).__name__}"
                continue
            verdict = _parse_judge_json(resp.text)
            if verdict is not None:
                return verdict
            last_err = "parse_error"
        # All retries exhausted: score 0, never crash the question.
        return 0, f"error: judge failed ({last_err})"


def _parse_judge_json(text: str) -> tuple[int, str] | None:
    """Extract {satisfied, reason} from a judge response. None if unparseable."""
    cleaned = (text or "").strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        data = json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        return None
    sat = data.get("satisfied")
    if isinstance(sat, (bool, int)):
        val = 1 if sat else 0
    else:
        # Tolerate "yes"/"no"/"true"/"false".
        low = str(sat).strip().lower()
        if low in ("1", "yes", "true", "satisfied"):
            val = 1
        elif low in ("0", "no", "false", "not satisfied"):
            val = 0
        else:
            return None
    reason = str(data.get("reason", ""))[:300]
    return val, reason


__all__ = ["EvalMetrics", "KeywordJudge", "LLMRubricJudge", "compute_metrics"]
