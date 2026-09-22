"""Eval metrics. All ratios handle zero denominators explicitly."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass

from core.prompts import PromptRegistry, get_default_registry
from core.providers.base import BaseLLMProvider, Message
from core.usage import usage_stage
from evals.adapter import EvalResult

log = logging.getLogger(__name__)

# Threshold at/above which a question counts as "passed" for pass_rate.
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
# batch_size=50 在真实 Qwen 上常因输出过长被截断（finish_reason=length），
# 导致整批 JSON 不完整、大量 parse_error。降到 12 是经过 pilot 验证的可靠值：
# 输出长度可控，配合 max_tokens 与解析器容错，parse_error 率可降到 ~0。
_BATCH_SIZE = 12
# 每批 LLM 调用超时（秒）。与 QwenProvider 的 120s timeout 对齐：
# 长答案 + 12 条 rubric 的批次在大模型上可能超过 60s，避免合法调用被误判超时。
_BATCH_TIMEOUT = 90.0
# 三态分值。
SCORE_PASS = 1
SCORE_FAIL = 0
SCORE_BLOCKED = -1
# 每条 rubric 估算的输出 token 预算；整批 max_tokens = n*PER_RUBRIC_TOKENS + BATCH_TOKENS_FLOOR。
# 留足 reason 句与 JSON 结构的余量，避免 finish_reason=length 截断。
_PER_RUBRIC_TOKENS = 300
_BATCH_TOKENS_FLOOR = 500
# 整题默认全局硬上限（秒）。109 rubrics / batch_size=12 = 9 批，串行最坏
# 900s*9 ≈ 2.25h；2 路并发 + 30min 硬上限是质量优先但有界的折中。
_DEFAULT_TOTAL_TIMEOUT = 240.0
# 批次并发上限：同时最多 2 个批次在飞，避免打爆 Qwen 限流。
_BATCH_CONCURRENCY = 2
# 单项缺失重试并发上限。
_RETRY_CONCURRENCY = 3


class LLMRubricJudge:
    """官方 DeepResearch Bench II 风格的三态批量 rubric 评分器。

    与旧版逐条 0/1 评分不同，本实现按官方口径工作：

    - 三态：1=通过(pass)，0=失败(fail)，-1=屏蔽/不适用(blocked/N/A)。
    - ``blocked`` 列表中的 rubric 直接记 -1（reason="blocked"），不调用 LLM，
      且不计入均值分母。
    - 其余 rubric 按每批最多 :data:`_BATCH_SIZE` 条分组，一次 LLM 请求打一批；
      LLM 返回 JSON 数组，元素为 ``{"index": 1-based, "score": 1|0, "reason": str}``。
    - 解析器对常见脏输出有容错：`````json`` 代码块、``{"results":[...]}`` 包装、
      NDJSON（每行一个对象）、以及被 ``max_tokens`` 截断的不完整数组（扫描出其中
      完整的对象，损坏的尾对象丢弃）。能解析的项正常评分，不整批记 0。
    - 整批/单项解析失败的 rubric 记 0（reason="parse_error"）。对整批里缺失的项，
      单独把该条 rubric 重发一次（最多 1 次，不重发整批），仍失败才落 parse_error。
    - 每批 :data:`_BATCH_TIMEOUT` 秒超时，批次级最多重试 :attr:`max_retries` 次。

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
        total_timeout: float = _DEFAULT_TOTAL_TIMEOUT,
        batch_concurrency: int = _BATCH_CONCURRENCY,
        retry_concurrency: int = _RETRY_CONCURRENCY,
    ) -> None:
        self._llm = llm
        self._registry = registry or get_default_registry()
        self._timeout = timeout
        self._max_retries = max_retries
        self._batch_size = batch_size
        self._total_timeout = total_timeout
        self._batch_concurrency = max(1, batch_concurrency)
        self._retry_concurrency = max(1, retry_concurrency)
        # 本地模式下渲染系统提示零网络。
        self._sys = self._registry.render("eval_rubric_judge_batch_system").system_text()
        self.model_id = getattr(llm, "model_id", "")

    async def score(
        self,
        answer: str,
        rubrics: list[str],
        blocked: list[str] | None = None,
        dimensions: list[str] | None = None,
        *,
        total_timeout: float | None = None,
    ) -> tuple[float, list[dict[str, object]], str]:
        """对一组 rubric 做三态批量评分。

        Args:
            answer: 研究报告正文。
            rubrics: 待评 rubric 文本列表。
            blocked: 被屏蔽/不适用的 rubric 文本列表（视为空列表当为 None）。
            dimensions: 与 ``rubrics`` 平行的维度标注列表（如 None 则不标注维度）。
            total_timeout: 整题硬上限（秒），覆盖构造函数默认值。None 时用默认。
        """
        deadline = total_timeout if total_timeout is not None else self._total_timeout
        try:
            return await asyncio.wait_for(
                self._score_inner(answer, rubrics, blocked, dimensions),
                timeout=deadline,
            )
        except TimeoutError:
            # 全局超时：已完成的 rubric 保留分数，未完成项标记 judge_timeout。
            # _score_inner 在被取消时 per_item 中已完成项有 score/reason，
            # 未完成项保持初始 score=0/reason=""，这里统一标记为 judge_timeout。
            return self._timeout_fallback(answer, rubrics, blocked, dimensions)

    def _timeout_fallback(
        self,
        answer: str,
        rubrics: list[str],
        blocked: list[str] | None,
        dimensions: list[str] | None,
    ) -> tuple[float, list[dict[str, object]], str]:
        """全局超时后的兜底：blocked 记 -1，其余全部记 0 + judge_timeout。"""
        blocked_set = set(blocked or [])
        dims = dimensions if dimensions is not None else [None] * len(rubrics)
        per_item: list[dict[str, object]] = []
        n_blocked = 0
        for i, rubric in enumerate(rubrics):
            dim = dims[i] if i < len(dims) else None
            if rubric in blocked_set:
                per_item.append(
                    {
                        "rubric": rubric,
                        "score": SCORE_BLOCKED,
                        "reason": "blocked",
                        "dimension": dim,
                    }
                )
                n_blocked += 1
            else:
                per_item.append(
                    {
                        "rubric": rubric,
                        "score": SCORE_FAIL,
                        "reason": "judge_timeout",
                        "dimension": dim,
                    }
                )
        live_scores = [SCORE_FAIL] * (len(rubrics) - n_blocked)
        mean = sum(live_scores) / len(live_scores) if live_scores else 1.0
        reason = f"0/{len(live_scores)} rubrics passed; {n_blocked} blocked; judge_timeout after {self._total_timeout:.0f}s"
        return mean, per_item, reason

    async def _score_inner(
        self,
        answer: str,
        rubrics: list[str],
        blocked: list[str] | None,
        dimensions: list[str] | None,
    ) -> tuple[float, list[dict[str, object]], str]:
        """实际评分逻辑（被全局 wait_for 包裹）。批次并发 + 单项重试并发。"""
        blocked_set = set(blocked or [])
        dims = dimensions if dimensions is not None else [None] * len(rubrics)

        per_item: list[dict[str, object]] = []
        live: list[tuple[int, str]] = []  # (per_item 下标, rubric 文本)
        for i, rubric in enumerate(rubrics):
            dim = dims[i] if i < len(dims) else None
            if rubric in blocked_set:
                per_item.append(
                    {
                        "rubric": rubric,
                        "score": SCORE_BLOCKED,
                        "reason": "blocked",
                        "dimension": dim,
                    }
                )
            else:
                per_item.append(
                    {"rubric": rubric, "score": SCORE_FAIL, "reason": "", "dimension": dim}
                )
                live.append((len(per_item) - 1, rubric))

        if not rubrics:
            return 1.0, [], "no rubrics to score"

        # 分批：每批 batch_size 条。
        batches: list[list[tuple[int, str]]] = []
        for start in range(0, len(live), self._batch_size):
            batches.append(live[start : start + self._batch_size])

        # 批次并发：Semaphore 限制同时在飞的批次数。
        batch_sem = asyncio.Semaphore(self._batch_concurrency)
        # batch_results[i] = {批内0基下标: (score, reason)}，按 batches 顺序。
        batch_results: list[dict[int, tuple[int, str]]] = [{} for _ in batches]

        async def _run_batch(batch_idx: int) -> None:
            batch = batches[batch_idx]
            rubric_texts = [text for _idx, text in batch]
            async with batch_sem:
                scored = await self._score_batch(answer, rubric_texts)
            batch_results[batch_idx] = scored

        # 并发跑所有批次。
        await asyncio.gather(*[_run_batch(i) for i in range(len(batches))])

        # 收集缺失项，并发逐条重试。
        retry_sem = asyncio.Semaphore(self._retry_concurrency)
        missing: list[tuple[int, int, str]] = []  # (batch_idx, local_pos, rubric_text)
        for batch_idx, batch in enumerate(batches):
            scored = batch_results[batch_idx]
            for local_pos, (_per_idx, text) in enumerate(batch):
                if local_pos not in scored:
                    missing.append((batch_idx, local_pos, text))

        async def _retry_one(item: tuple[int, int, str]) -> None:
            batch_idx, local_pos, rubric_text = item
            async with retry_sem:
                single = await self._score_single(answer, rubric_text)
            if single is not None:
                batch_results[batch_idx][local_pos] = single

        if missing:
            await asyncio.gather(*[_retry_one(m) for m in missing])

        # 合并结果到 per_item（按原 rubric 顺序）。
        for batch_idx, batch in enumerate(batches):
            scored = batch_results[batch_idx]
            for local_pos, (per_idx, _text) in enumerate(batch):
                if local_pos in scored:
                    val, why = scored[local_pos]
                else:
                    val, why = SCORE_FAIL, "parse_error"
                per_item[per_idx]["score"] = val
                per_item[per_idx]["reason"] = why

        # 均值：blocked 不计入分母。
        live_scores = [
            s for p in per_item if isinstance(s := p["score"], int) and s != SCORE_BLOCKED
        ]
        n_pass = sum(1 for s in live_scores if s == SCORE_PASS)
        mean = sum(live_scores) / len(live_scores) if live_scores else 1.0
        n_blocked = sum(1 for p in per_item if p["score"] == SCORE_BLOCKED)
        n_timeout = sum(1 for p in per_item if p.get("reason") == "judge_timeout")
        reason = f"{n_pass}/{len(live_scores)} rubrics passed; {n_blocked} blocked"
        if n_timeout:
            reason += f"; {n_timeout} timeout"
        return mean, per_item, reason

    async def _score_batch(
        self, answer: str, rubric_texts: list[str]
    ) -> dict[int, tuple[int, str]]:
        """对一批 rubric 发起一次 LLM 请求，返回 {批内0基下标: (score, reason)}。

        返回的 dict 只包含成功解析出的元素；调用方对缺失项按 parse_error 处理。
        """
        return await self._score_batch_once(answer, rubric_texts)

    async def _score_batch_once(
        self, answer: str, rubric_texts: list[str], *, attempts: int | None = None
    ) -> dict[int, tuple[int, str]]:
        """带重试的批次请求。``attempts`` 为 None 时用 :attr:`max_retries`+1。"""
        if attempts is None:
            attempts = self._max_retries + 1
        # 按批大小估算 max_tokens，避免长输出被服务端截断（finish_reason=length）。
        max_tokens = len(rubric_texts) * _PER_RUBRIC_TOKENS + _BATCH_TOKENS_FLOOR
        # 带序号格式化 rubric（提示词中使用 1-based 编号）。
        numbered = "\n".join(f"{i + 1}. {r}" for i, r in enumerate(rubric_texts))
        rendered = self._registry.render(
            "eval_rubric_judge_batch_user", answer=answer, rubrics_batch=numbered
        )
        messages = [Message(role="system", content=self._sys)]
        for _role, content in rendered.messages:
            messages.append(Message(role="user", content=content))

        for _attempt in range(max(1, attempts)):
            try:
                with usage_stage("judge"):
                    resp = await asyncio.wait_for(
                        self._llm.acomplete(messages, max_tokens=max_tokens),
                        timeout=self._timeout,
                    )
            except TimeoutError:
                continue
            except Exception:  # noqa: BLE001 - 屏蔽供应商错误体
                continue
            parsed = _parse_batch_judge_json(resp.text)
            if parsed:
                return parsed
            # 解析失败则进入下一次重试；重试耗尽后落到空 dict。
        return {}

    async def _score_single(self, answer: str, rubric_text: str) -> tuple[int, str] | None:
        """把一条解析失败的 rubric 单独重发一次（有界：恰好一次网络请求）。

        用于整批里个别项损坏/缺失时，只重发该项而非重发整批。返回 ``(score, reason)``，
        仍然失败（无法解析）时返回 None。
        """
        out = await self._score_batch_once(answer, [rubric_text], attempts=1)
        return out.get(0)


def _strip_code_fence(text: str) -> str:
    """剥掉 LLM 常见的 ```json ... ``` 代码块包装，容忍前后多余文字。"""
    cleaned = (text or "").strip()
    m = re.search(r"```(?:json|JSON)?\s*(.*?)\s*```", cleaned, re.DOTALL)
    if m:
        return m.group(1).strip()
    # 开了 ```json 但没闭合的情况：只剥开头标记。
    cleaned = re.sub(r"^```(?:json|JSON)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned.strip()


def _unwrap_results(data: object) -> object:
    """容忍 ``{"results": [...]}`` / ``{"items": [...]}`` 等包装形态。"""
    if isinstance(data, dict):
        for key in ("results", "items", "data", "output", "judgements", "scores"):
            if isinstance(data.get(key), list):
                return data[key]
    return data


def _scan_array_objects(inner: str) -> list[dict]:
    """扫描数组内容（``[`` 之后的文本）中完整的顶层 JSON 对象。

    被 ``max_tokens`` 截断时，末尾那个未闭合的对象会被丢弃，其余完整对象照常返回。
    这样"能解析的项正常评分"，而不是整批记 0。只返回 dict 对象。
    """
    objs: list[dict] = []
    n = len(inner)
    i = 0
    while i < n:
        while i < n and inner[i] in " \t\r\n,":
            i += 1
        if i >= n or inner[i] != "{":
            break
        start = i
        depth = 0
        in_str = False
        esc = False
        closed = False
        while i < n:
            c = inner[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            else:
                if c == '"':
                    in_str = True
                elif c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        i += 1
                        closed = True
                        break
            i += 1
        if not closed:
            # 末尾对象被截断：丢弃它，停止扫描。
            break
        try:
            obj = json.loads(inner[start:i])
        except json.JSONDecodeError:
            obj = None
        if isinstance(obj, dict):
            objs.append(obj)
    return objs


def _parse_batch_judge_json(text: str) -> dict[int, tuple[int, str]] | None:
    """从批量评分响应中解析 ``{批内0基下标: (score, reason)}``。

    容错顺序：
    1. 整段直接 JSON 解析（数组，或 ``{"results":[...]}`` 包装）。
    2. NDJSON：每行一个 JSON 对象。
    3. 截断恢复：从第一个 ``[`` 起扫描其中完整的对象，丢弃损坏尾对象。

    整段都无法提取任何对象时返回 None；单个元素非法时跳过该元素
    （调用方会把它当 parse_error）。index 按提示词约定为 1-based，这里转成 0-based。
    """
    cleaned = _strip_code_fence(text)
    items: list[dict] | None = None

    # 1) 整段 JSON。
    try:
        data: object = json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        data = None
    if isinstance(data, dict):
        data = _unwrap_results(data)
    if isinstance(data, list):
        items = [el for el in data if isinstance(el, dict)]

    # 2) NDJSON：每行一个对象。
    if items is None:
        nd: list[dict] = []
        for line in cleaned.splitlines():
            line = line.strip().rstrip(",").strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(obj, dict):
                nd.append(obj)
        if nd:
            items = nd

    # 3) 截断恢复：扫描不完整数组里的完整对象。
    if items is None:
        lb = cleaned.find("[")
        if lb != -1:
            recovered = _scan_array_objects(cleaned[lb + 1 :])
            if recovered:
                items = recovered

    if items is None:
        return None

    out: dict[int, tuple[int, str]] = {}
    for el in items:
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
