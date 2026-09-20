"""Real Qwen / DashScope provider via OpenAI-compatible HTTP API.

Uses httpx.AsyncClient against the DashScope-compatible endpoint. The model id
and base URL come from settings (no hardcoded model name). Usage metadata
(prompt_tokens / completion_tokens / total_tokens) is preserved from the
response. If usage is missing, it is omitted (not faked).
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import weakref
from typing import Any

import httpx

from core.exceptions import ProviderError, ProviderNotConfiguredError
from core.providers.base import BaseLLMProvider, LLMResponse, Message
from core.usage import record_llm_usage

# 可重试的 HTTP 状态码：限流与服务端瞬时错误。401/403（鉴权/配额）不重试。
_RETRYABLE_STATUS: frozenset[int] = frozenset({429, 500, 502, 503, 504})
_GLOBAL_INFLIGHT = 4
_LOOP_LIMITERS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = (
    weakref.WeakKeyDictionary()
)


def _global_limiter() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    limiter = _LOOP_LIMITERS.get(loop)
    if limiter is None:
        limiter = asyncio.Semaphore(_GLOBAL_INFLIGHT)
        _LOOP_LIMITERS[loop] = limiter
    return limiter


class QwenProvider(BaseLLMProvider):
    provider_name = "qwen"

    def __init__(
        self,
        *,
        model_id: str,
        base_url: str,
        api_key: str,
        timeout: float = 75.0,
        max_retries: int = 1,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.model_id = model_id
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        # 默认 120s：extractor 单次可发送上万字文档、judge 批次含长答案，
        # 60s 容易在大上下文下误超时。
        self._timeout = timeout
        # 瞬时错误（超时/连接错误/429/5xx）的重试次数；401/403 不重试。
        self._max_retries = max(0, int(max_retries))
        self._client = client  # injectable for tests

    def is_configured(self) -> bool:
        return bool(self._api_key.strip()) and bool(self._base_url.strip())

    def with_model(self, model_id: str) -> QwenProvider:
        """Per-request clone pinned to ``model_id``.

        Shares the (possibly injected) httpx client, base URL and API key, but
        carries its own ``model_id``. The base provider is never mutated, so
        parallel workers selecting different models do not race.
        """
        return QwenProvider(
            model_id=model_id,
            base_url=self._base_url,
            api_key=self._api_key,
            timeout=self._timeout,
            max_retries=self._max_retries,
            client=self._client,
        )

    def with_timeout(self, timeout: float) -> QwenProvider:
        """Clone the provider with a stage-specific request timeout."""
        return QwenProvider(
            model_id=self.model_id,
            base_url=self._base_url,
            api_key=self._api_key,
            timeout=timeout,
            max_retries=self._max_retries,
            client=self._client,
        )

    async def acomplete(
        self,
        messages: list[Message],
        *,
        model_id: str | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        if not self.is_configured():
            raise ProviderNotConfiguredError(
                "DASHSCOPE_API_KEY is empty; set it in .env or use LLM_PROVIDER=fake."
            )

        # Request-level override: NEVER mutate self.model_id.
        effective_model = model_id or self.model_id
        url = f"{self._base_url}/chat/completions"
        payload: dict[str, Any] = {
            "model": effective_model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
        }
        # 输出 token 预算：judge 批量评分需要足够长度完整返回 JSON，
        # 否则会因 finish_reason=length 被截断、解析失败。None 时不传，走服务端默认。
        if max_tokens is not None:
            payload["max_tokens"] = int(max_tokens)
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        close_client = False
        client = self._client
        if client is None:
            client = httpx.AsyncClient(timeout=self._timeout)
            close_client = True

        # 带退避的重试：全进程最多四个 Qwen 请求同时在飞。429 优先遵循
        # Retry-After，其余瞬时错误使用指数退避和少量 jitter。
        resp: httpx.Response | None = None
        last_exc: Exception | None = None
        max_attempts = self._max_retries + 1
        try:
            for attempt in range(max_attempts):
                try:
                    async with _global_limiter():
                        resp = await client.post(url, json=payload, headers=headers)
                except httpx.TimeoutException as e:
                    last_exc = e
                    if attempt < max_attempts - 1:
                        await asyncio.sleep(2.0**attempt + random.uniform(0.0, 0.25))
                        continue
                    raise ProviderError(
                        f"Qwen request timed out after {max_attempts} attempts: {e}"
                    ) from e
                except httpx.HTTPError as e:
                    last_exc = e
                    if attempt < max_attempts - 1:
                        await asyncio.sleep(2.0**attempt + random.uniform(0.0, 0.25))
                        continue
                    raise ProviderError(f"Qwen request failed: {e}") from e

                if resp.status_code in _RETRYABLE_STATUS and attempt < max_attempts - 1:
                    retry_after = resp.headers.get("Retry-After", "")
                    try:
                        delay = max(0.0, float(retry_after))
                    except ValueError:
                        delay = 2.0**attempt
                    await asyncio.sleep(delay + random.uniform(0.0, 0.25))
                    continue
                break
        finally:
            if close_client:
                with contextlib.suppress(Exception):
                    await client.aclose()

        if resp is None:  # 理论上不会走到这里，防御性兜底
            raise ProviderError(f"Qwen request failed: {last_exc}")

        if resp.status_code == 401:
            raise ProviderNotConfiguredError("Qwen returned 401 — check DASHSCOPE_API_KEY.")
        if resp.status_code >= 400:
            raise ProviderError(f"Qwen returned HTTP {resp.status_code}: {resp.text[:200]}")

        data: dict[str, Any] = resp.json()
        choices = data.get("choices") or []
        if not choices:
            raise ProviderError("Qwen response had no choices")
        content = choices[0].get("message", {}).get("content", "")
        usage = data.get("usage") or {}
        prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
        completion_tokens = int(usage.get("completion_tokens", 0) or 0)
        # Mark reported only when the API actually supplied usage counts.
        usage_reported = prompt_tokens > 0 or completion_tokens > 0
        record_llm_usage(
            provider=self.provider_name,
            model=str(data.get("model", effective_model)),
            input_tokens=prompt_tokens,
            output_tokens=completion_tokens,
            estimated=not usage_reported,
        )
        return LLMResponse(
            text=content,
            model=data.get("model", effective_model),
            provider=self.provider_name,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            usage_estimated=not usage_reported,
            metadata={
                "finish_reason": str(choices[0].get("finish_reason", "")),
                "total_tokens": str(usage.get("total_tokens", "")),
            },
        )

    def describe(self) -> dict[str, str]:
        out = super().describe()
        out["base_url"] = self._base_url.split("?")[0]
        return out


__all__ = ["QwenProvider"]
