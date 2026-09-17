"""Contract tests for QwenProvider (mock httpx transport, no real network)."""

from __future__ import annotations

import httpx
import pytest

from core.exceptions import ProviderError, ProviderNotConfiguredError
from core.providers.base import Message
from core.providers.qwen import QwenProvider


def _make_app() -> httpx.ASGITransport:
    raise NotImplementedError  # replaced below


def _client_with_handler(handler) -> httpx.AsyncClient:
    def handle(request: httpx.Request) -> httpx.Response:
        return handler(request)

    transport = httpx.MockTransport(handle)
    return httpx.AsyncClient(transport=transport, base_url="https://dashscope.example.com")


@pytest.mark.asyncio
async def test_qwen_sends_model_and_key() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        import json as _json

        body = _json.loads(request.content)
        seen["model"] = body["model"]
        seen["messages"] = body["messages"]
        return httpx.Response(
            200,
            json={
                "model": "qwen-test",
                "choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
            },
        )

    client = _client_with_handler(handler)
    p = QwenProvider(
        model_id="qwen-test",
        base_url="https://dashscope.example.com",
        api_key="sk-fake",
        client=client,
    )
    resp = await p.acomplete([Message(role="user", content="hi")])
    assert resp.text == "hello"
    assert resp.model == "qwen-test"
    assert resp.prompt_tokens == 5
    assert resp.completion_tokens == 3
    assert seen["model"] == "qwen-test"
    assert seen["auth"] == "Bearer sk-fake"
    await client.aclose()


@pytest.mark.asyncio
async def test_qwen_missing_key_raises_not_configured() -> None:
    p = QwenProvider(model_id="m", base_url="https://x", api_key="")
    with pytest.raises(ProviderNotConfiguredError):
        await p.acomplete([Message(role="user", content="hi")])


@pytest.mark.asyncio
async def test_qwen_401_raises_not_configured() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="bad key")

    client = _client_with_handler(handler)
    p = QwenProvider(model_id="m", base_url="https://x", api_key="sk-x", client=client)
    with pytest.raises(ProviderNotConfiguredError):
        await p.acomplete([Message(role="user", content="hi")])
    await client.aclose()


@pytest.mark.asyncio
async def test_qwen_500_raises_provider_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    client = _client_with_handler(handler)
    p = QwenProvider(model_id="m", base_url="https://x", api_key="sk-x", client=client)
    with pytest.raises(ProviderError):
        await p.acomplete([Message(role="user", content="hi")])
    await client.aclose()
