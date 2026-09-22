from __future__ import annotations

import json

import httpx
import pytest
from pydantic import SecretStr

from core.config import REDACTED, Settings
from core.providers.base import Message
from core.providers.deepseek import DeepSeekProvider
from core.providers.factory import build_provider


def _client_with_bodies(bodies: list[dict[str, object]]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "model": bodies[-1]["model"],
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
            },
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_deepseek_routes_thinking_by_model_without_qwen_flag() -> None:
    bodies: list[dict[str, object]] = []
    client = _client_with_bodies(bodies)
    provider = DeepSeekProvider(
        model_id="deepseek-v4-pro",
        base_url="https://api.deepseek.com",
        api_key="sk-test",
        client=client,
    )

    await provider.with_thinking(False).acomplete([Message(role="user", content="extract")])
    await provider.with_thinking(True).acomplete([Message(role="user", content="plan")])

    assert [body["model"] for body in bodies] == ["deepseek-flash", "deepseek-v4-pro"]
    assert all("enable_thinking" not in body for body in bodies)
    await client.aclose()


def test_deepseek_factory_and_stage_models_are_explicit() -> None:
    settings = Settings(
        _env_file=None,
        llm_provider="deepseek",
        deepseek_api_key=SecretStr("sk-secret"),
    )

    provider = build_provider(settings=settings)

    assert isinstance(provider, DeepSeekProvider)
    assert provider.model_id == "deepseek-v4-pro"
    assert settings.fast_model() == "deepseek-flash"
    assert settings.verifier_model() == "deepseek-flash"
    assert settings.synthesis_model() == "deepseek-v4-pro"
    assert settings.judge_model() == "deepseek-flash"
    assert settings.safe_dict()["deepseek_api_key"] == REDACTED
    assert "sk-secret" not in repr(settings.safe_dict())
