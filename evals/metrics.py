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


# 每条 LLM 请求最多打分的 rubric 数量（官方 DRB2 每题约 71 条，分批调用）。
_BATCH_SIZE = 50
# 每批 LLM 调用超时（秒）。与 QwenProvider 的 120s timeout 对齐：
# 长答案 + 50 条 rubric 的批次在大模型上可能超过 60s，避免合法调用被误判超时。
_BATCH_TIMEOUT = 120.0
# 三态分值。
SCORE_PASS = 1
SCORE_FAIL = 0
SCORE_BLOCKED = -1


class LLMRubricJudge:
    """官方 DeepResearch Bench II 风格的三态批量 rubric 评分器。

    与旧版逐条 0/1 评分不同，本实现按官方口径工作：

    - 三态：1=通过(pass)，0=失败(fail)，-1=屏蔽/不适用(blocked/N/A)。
    - ``blocked`` 列表中的 rubric 直接记 -1（reason="blocked"），不调用 LLM，
      且不计入均值分母。
    - 其余 rubric 按每批最多 :data:`_BATCH_SIZE` 条分组，一次 LLM 请求打一批；
      LLM 返回 JSON 数组，元素为 ``{"index": 1-based, "score": 1|0, "reason": str}``。
    - 解析失败的 rubric 记 0（reason="parse_error"），单批/单条失败不崩溃。
    - 每批 :data:`_BATCH_TIMEOUT` 秒超时，最多重试 :attr:`max_retries` 次。

    返回 ``(mean_score, per_item, reason)``：
        * ``mean_score`` = sum(非 blocked 的 score) / count(非 blocked)，
          blocked 既不计入分子也不计入分母；无待评 rubric 时返回 1.0。
        * ``per_item`` 包含全部 rubric（含 blocked），每项形如
          ``{"rubric": str, "score": int, "reason": str, "dimension": str}``。
        * ``reason`` 为简短可读摘要。

    评分 prompt 来自 Prompt Hub（``eval_rubric_judge_batch_system`` /
    ``eval_rubric_judge_batch_user``），调用点不硬编码任何提示词。
    """

    name = "llm-rubric-batch-v2"
    kind = "llm"

    def __init__(
        self,
        llm: BaseLLMProvider,
        *,
        registry: PromptRegistry | None = None,
        timeout: float = _BATCH_TIMEOUT,
        max_retries: int = 2,
        batch_size: int = _BATCH_SIZE,
    ) -> None:
        self._llm = llm
        self._registry = registry or get_default_registry()
        self._timeout = timeout
        self._max_retries = max_retries
        self._batch_size = batch_size
        # 本地模式下渲染系统提示零网络。
        self._sys = self._registry.render("eval_rubric_judge_batch_system").system_text()
        self.model_id = getattr(llm, "model_id", "")

    async def score(
        self,
        answer: str,
        rubrics: list[str],
        blocked: list[str] | None = None,
        dimensions: list[str] | None = None,
    ) -> tuple[float, list[dict[str, object]], str]:
        """对一组 rubric 做三态批量评分。

        Args:
            answer: 研究报告正文。
            rubrics: 待评 rubric 文本列表。
            blocked: 被屏蔽/不适用的 rubric 文本列表（视为空列表当为 None）。
            dimensions: 与 ``rubrics`` 平行的维度标注列表（如 None 则不标注维度）。
        """
        blocked_set = set(blocked or [])
        dims = dimensions if dimensions is not None else [None] * len(rubrics)

        per_item: list[dict[str, object]] = []
        live: list[tuple[int, str]] = []  # (per_item 下标, rubric 文本)
        for i, rubric in enumerate(rubrics):
            dim = dims[i] if i < len(dims) else None
            if rubric in blocked_set:
                per_item.append(
                    {"rubric": rubric, "score": SCORE_BLOCKED, "reason": "blocked", "dimension": dim}
                )
            else:
                per_item.append(
                    {"rubric": rubric, "score": SCORE_FAIL, "reason": "", "dimension": dim}
                )
                live.append((len(per_item) - 1, rubric))

        if not rubrics:
            return 1.0, [], "no rubrics to score"

        # 分批调用 LLM，每批一次请求。
        for start in range(0, len(live), self._batch_size):
            batch = live[start : start + self._batch_size]
            rubric_texts = [text for _idx, text in batch]
            scored = await self._score_batch(answer, rubric_texts)
            for local_pos, (per_idx, _text) in enumerate(batch):
                if local_pos in scored:
                    val, why = scored[local_pos]
                else:
                    # 整批解析失败 / 缺该 index：按容错规则记 0。
                    val, why = SCORE_FAIL, "parse_error"
                per_item[per_idx]["score"] = val
                per_item[per_idx]["reason"] = why

        # 均值：blocked 不计入分母。全部被屏蔽时无可评项，按 1.0 处理（与空 rubrics 一致）。
        live_scores = [
            s for p in per_item if isinstance(s := p["score"], int) and s != SCORE_BLOCKED
        ]
        n_pass = sum(1 for s in live_scores if s == SCORE_PASS)
        mean = sum(live_scores) / len(live_scores) if live_scores else 1.0
        n_blocked = sum(1 for p in per_item if p["score"] == SCORE_BLOCKED)
        reason = f"{n_pass}/{len(live_scores)} rubrics passed; {n_blocked} blocked"
        return mean, per_item, reason

    async def _score_batch(
        self, answer: str, rubric_texts: list[str]
    ) -> dict[int, tuple[int, str]]:
        """对一批 rubric 发起一次 LLM 请求，返回 {批内0基下标: (score, reason)}。

        返回的 dict 只包含成功解析出的元素；调用方对缺失项按 parse_error 处理。
        """
        # 带序号格式化 rubric（提示词中使用 1-based 编号）。
        numbered = "\n".join(f"{i + 1}. {r}" for i, r in enumerate(rubric_texts))
        rendered = self._registry.render(
            "eval_rubric_judge_batch_user", answer=answer, rubrics_batch=numbered
        )
        messages = [Message(role="system", content=self._sys)]
        for _role, content in rendered.messages:
            messages.append(Message(role="user", content=content))

        for _attempt in range(self._max_retries + 1):
            try:
                resp = await asyncio.wait_for(self._llm.acomplete(messages), timeout=self._timeout)
            except TimeoutError:
                continue
            except Exception:  # noqa: BLE001 - 屏蔽供应商错误体
                continue
            parsed = _parse_batch_judge_json(resp.text)
            if parsed is not None:
                return parsed
            # 解析失败则进入下一次重试；重试耗尽后落到空 dict。
        return {}


def _parse_batch_judge_json(text: str) -> dict[int, tuple[int, str]] | None:
    """从批量评分响应中解析 ``{批内0基下标: (score, reason)}``。

    整个响应无法解析为 JSON 数组时返回 None；单个元素非法时跳过该元素
    （调用方会把它当 parse_error）。index 按提示词约定为 1-based，这里转成 0-based。
    """
    cleaned = (text or "").strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        data = json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, list):
        return None

    out: dict[int, tuple[int, str]] = {}
    for el in data:
        if not isinstance(el, dict):
            continue
        raw_index = el.get("index")
        if isinstance(raw_index, bool) or not isinstance(raw_index, int):
            continue
        idx0 = raw_index - 1  # 1-based -> 0-based
        if idx0 < 0:
            continue
        val = _normalize_score(el.get("score"))
        if val is None:
            continue
        reason = str(el.get("reason", ""))[:300]
        out[idx0] = (val, reason)
    return out


def _normalize_score(raw: object) -> int | None:
    """把 LLM 返回的 score 归一到 1/0；不可识别返回 None。"""
    if isinstance(raw, bool):
        return SCORE_PASS if raw else SCORE_FAIL
    if isinstance(raw, int):
        return SCORE_PASS if raw == SCORE_PASS else SCORE_FAIL
    low = str(raw).strip().lower()
    if low in ("1", "yes", "true", "pass", "satisfied", "satisfy"):
        return SCORE_PASS
    if low in ("0", "no", "false", "fail", "unsatisfied", "unsatisfy"):
        return SCORE_FAIL
    return None


__all__ = ["EvalMetrics", "KeywordJudge", "LLMRubricJudge", "compute_metrics"]
