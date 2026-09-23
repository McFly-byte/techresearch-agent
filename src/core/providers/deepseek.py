"""DeepSeek provider via its OpenAI-compatible chat-completions API."""

from __future__ import annotations

from typing import Any

import httpx

from core.providers.qwen import QwenProvider


class DeepSeekProvider(QwenProvider):
    """Provider with explicit chat/reasoner stage routing.

    DeepSeek uses a vendor-specific ``thinking.type`` object rather than
    Qwen's ``enable_thinking`` request field. ``with_thinking`` switches the
    stage model and sends the matching DeepSeek request contract.
    """

    provider_name = "deepseek"
    provider_label = "DeepSeek"
    api_key_env = "DEEPSEEK_API_KEY"
    supports_enable_thinking = False

    def __init__(
        self,
        *,
        model_id: str,
        base_url: str,
        api_key: str,
        timeout: float = 120.0,
        max_retries: int = 1,
        enable_thinking: bool | None = None,
        client: httpx.AsyncClient | None = None,
        chat_model: str = "deepseek-flash",
        reasoner_model: str = "deepseek-v4-pro",
        json_mode: bool = False,
        thinking_enabled: bool | None = None,
    ) -> None:
        super().__init__(
            model_id=model_id,
            base_url=base_url,
            api_key=api_key,
            timeout=timeout,
            max_retries=max_retries,
            enable_thinking=enable_thinking,
            client=client,
        )
        self._chat_model = chat_model
        self._reasoner_model = reasoner_model
        self._json_mode = json_mode
        self._thinking_enabled = thinking_enabled

    def with_thinking(self, enabled: bool) -> DeepSeekProvider:
        clone = self._clone(model_id=self._reasoner_model if enabled else self._chat_model)
        clone._thinking_enabled = bool(enabled)
        return clone

    def with_json_mode(self, enabled: bool = True) -> DeepSeekProvider:
        clone = self._clone(model_id=self.model_id)
        clone._json_mode = bool(enabled)
        return clone

    def _clone(
        self,
        *,
        model_id: str,
        timeout: float | None = None,
        enable_thinking: bool | None = None,
    ) -> DeepSeekProvider:
        return type(self)(
            model_id=model_id,
            base_url=self._base_url,
            api_key=self._api_key,
            timeout=self._timeout if timeout is None else timeout,
            max_retries=self._max_retries,
            enable_thinking=enable_thinking,
            client=self._client,
            chat_model=self._chat_model,
            reasoner_model=self._reasoner_model,
            json_mode=self._json_mode,
            thinking_enabled=self._thinking_enabled,
        )

    def _augment_payload(self, payload: dict[str, Any]) -> None:
        if self._thinking_enabled is not None:
            payload["thinking"] = {"type": "enabled" if self._thinking_enabled else "disabled"}
        if self._json_mode:
            payload["response_format"] = {"type": "json_object"}


__all__ = ["DeepSeekProvider"]
