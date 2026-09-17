"""Abstract LLM provider interface.

Business modules depend on `BaseLLMProvider`, never on a concrete vendor.
Phase 0 ships two implementations:
- FakeLLM: deterministic, offline, used in tests and by default when no key.
- QwenProvider: minimal initialisable adapter; it validates config but does
  NOT make any network call yet (real HTTP wiring lands in phase 1).

The interface is intentionally tiny — just enough for the health probe and
future unit tests. It will grow (streaming, tool calls, structured output)
when the graph layer arrives.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True)
class Message:
    role: Literal["system", "user", "assistant"]
    content: str


@dataclass(frozen=True)
class LLMResponse:
    text: str
    model: str
    provider: str
    # Minimal usage hint; FakeLLM fills it deterministically.
    prompt_tokens: int = 0
    completion_tokens: int = 0
    # True when the token counts are estimated/synthetic (e.g. FakeLLM deriving
    # counts from character length), False when a real provider reported usage
    # from the API. Default True keeps every existing constructor source- and
    # backwards-compatible: an unflagged response is treated as estimated.
    usage_estimated: bool = True
    metadata: dict[str, str] = field(default_factory=dict)


class BaseLLMProvider(abc.ABC):
    """Protocol every model backend must implement."""

    provider_name: str = "abstract"
    model_id: str = ""

    @abc.abstractmethod
    async def acomplete(
        self,
        messages: list[Message],
        *,
        model_id: str | None = None,
    ) -> LLMResponse:
        """Async chat completion.

        ``model_id`` is a PER-REQUEST model override (stage-3 hard boundary):
        callers may ask this single shared provider to run one request under a
        different model WITHOUT mutating the provider's own ``model_id``.
        Implementations MUST use ``model_id or self.model_id`` and must never
        persist the override onto ``self.model_id``. ``None`` means "use the
        provider default".
        """

    @abc.abstractmethod
    def is_configured(self) -> bool:
        """True if this provider has everything needed to make real calls."""

    def with_model(self, model_id: str) -> BaseLLMProvider:
        """Return a per-request provider clone pinned to ``model_id``.

        The clone MUST share expensive/stateful resources (httpx client, API key)
        but carry its OWN independent ``model_id``. Workers use this to apply a
        router-selected model WITHOUT mutating the shared provider instance —
        mutating ``self.model_id`` on a shared provider races parallel workers.

        The default raises; concrete providers that support per-request models
        (FakeLLM, QwenProvider) override it.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not support per-request model override"
        )

    def describe(self) -> dict[str, str]:
        """Non-secret summary, safe for /health and doctor output."""
        return {
            "provider": self.provider_name,
            "model": self.model_id,
            "configured": str(self.is_configured()),
        }


__all__ = ["BaseLLMProvider", "LLMResponse", "Message"]
