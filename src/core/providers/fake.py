"""Offline deterministic LLM used in tests and by default when no key is set."""

from __future__ import annotations

from ..exceptions import ProviderNotConfiguredError
from .base import BaseLLMProvider, LLMResponse, Message


class FakeLLM(BaseLLMProvider):
    provider_name = "fake"

    def __init__(self, model_id: str = "fake-1", echo: bool = True) -> None:
        self.model_id = model_id
        self._echo = echo
        self.calls: list[list[Message]] = []

    def is_configured(self) -> bool:
        return True  # needs no external key

    def with_model(self, model_id: str) -> FakeLLM:
        """Per-request clone: same echo behaviour, independent model_id.

        The debug ``calls`` log is SHARED with the base instance so tests can
        observe every request regardless of which clone sent it; only the
        ``model_id`` differs. The base provider's own ``model_id`` is never
        mutated.
        """
        scoped = FakeLLM(model_id=model_id, echo=self._echo)
        scoped.calls = self.calls
        return scoped

    async def acomplete(
        self,
        messages: list[Message],
        *,
        model_id: str | None = None,
    ) -> LLMResponse:
        # Request-level override (stage-3 hard boundary): use the per-request
        # model when supplied, else the provider default. NEVER mutate
        # self.model_id — concurrent requests must not see each other's choice.
        effective = model_id or self.model_id
        self.calls.append(list(messages))
        last_user = next((m.content for m in reversed(messages) if m.role == "user"), "")
        text = f"[fake:{effective}] echo: {last_user}" if self._echo else f"[fake:{effective}] ok"
        prompt_tokens = sum(len(m.content) for m in messages)
        completion_tokens = len(text)
        return LLMResponse(
            text=text,
            model=effective,
            provider=self.provider_name,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            # Synthetic counts derived from character length are NOT measured
            # usage — flag them as estimated even when non-zero.
            usage_estimated=True,
        )


class FakeLLMThatFails(FakeLLM):
    """A FakeLLM that always raises — for testing error paths."""

    async def acomplete(
        self,
        messages: list[Message],
        *,
        model_id: str | None = None,
    ) -> LLMResponse:
        raise ProviderNotConfiguredError("fake failure injected for tests")


__all__ = ["FakeLLM", "FakeLLMThatFails"]
