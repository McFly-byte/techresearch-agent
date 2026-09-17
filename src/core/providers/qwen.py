"""Real Qwen / DashScope provider via OpenAI-compatible HTTP API.

Uses httpx.AsyncClient against the DashScope-compatible endpoint. The model id
and base URL come from settings (no hardcoded model name). Usage metadata
(prompt_tokens / completion_tokens / total_tokens) is preserved from the
response. If usage is missing, it is omitted (not faked).
"""

from __future__ import annotations

from typing import Any

import httpx

from core.exceptions import ProviderError, ProviderNotConfiguredError
from core.providers.base import BaseLLMProvider, LLMResponse, Message


class QwenProvider(BaseLLMProvider):
    provider_name = "qwen"

    def __init__(
        self,
        *,
        model_id: str,
        base_url: str,
        api_key: str,
        timeout: float = 30.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.model_id = model_id
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout
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
            client=self._client,
        )

    async def acomplete(
        self,
        messages: list[Message],
        *,
        model_id: str | None = None,
    ) -> LLMResponse:
        if not self.is_configured():
            raise ProviderNotConfiguredError(
                "DASHSCOPE_API_KEY is empty; set it in .env or use LLM_PROVIDER=fake."
            )

        # Request-level override: NEVER mutate self.model_id.
        effective_model = model_id or self.model_id
        url = f"{self._base_url}/chat/completions"
        payload = {
            "model": effective_model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        close_client = False
        client = self._client
        if client is None:
            client = httpx.AsyncClient(timeout=self._timeout)
            close_client = True

        try:
            resp = await client.post(url, json=payload, headers=headers)
        except httpx.TimeoutException as e:
            raise ProviderError(f"Qwen request timed out: {e}") from e
        except httpx.HTTPError as e:
            raise ProviderError(f"Qwen request failed: {e}") from e
        finally:
            if close_client:
                await client.aclose()

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
