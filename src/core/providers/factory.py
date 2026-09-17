"""Model factory. Business code calls `get_llm(level)` and never imports a
concrete vendor directly.
"""

from __future__ import annotations

from typing import Literal

from ..config import Settings, get_settings
from ..exceptions import ConfigurationError
from .base import BaseLLMProvider
from .fake import FakeLLM
from .qwen import QwenProvider

Level = Literal["default", "fast"]


def build_provider(
    level: Level = "default", *, settings: Settings | None = None
) -> BaseLLMProvider:
    """Construct an LLM provider based on current Settings.

    Resolution:
    1. settings.llm_provider == "fake"  -> FakeLLM
    2. settings.llm_provider == "qwen"  -> QwenProvider (raises if no key)
    3. "auto"                           -> QwenProvider iff key present,
                                          otherwise FakeLLM (offline-friendly)
    """
    s = settings or get_settings()
    provider = s.resolved_provider()

    if provider == "fake":
        return FakeLLM(model_id="fake-1")

    # qwen
    model = s.qwen_model if level == "default" else s.qwen_model_fast
    if not s.has_dashscope_key:
        raise ConfigurationError(
            f"llm_provider=qwen but DASHSCOPE_API_KEY is empty (level={level})."
        )
    return QwenProvider(
        model_id=model,
        base_url=s.qwen_base_url,
        api_key=s.dashscope_api_key.get_secret_value(),
    )


__all__ = ["Level", "build_provider"]
