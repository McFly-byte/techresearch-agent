"""Contract tests for QwenProvider (mock httpx transport, no real network)."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from langsmith.run_helpers import tracing_context
from langsmith.run_trees import RunTree

from core.exceptions import ProviderError, ProviderNotConfiguredError
from core.providers import qwen as qwen_module
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
async def test_qwen_attaches_native_usage_to_active_langsmith_llm_run() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "qwen-test",
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 4, "total_tokens": 11},
            },
        )

    client = _client_with_handler(handler)
    provider = QwenProvider(
        model_id="qwen-test",
        base_url="https://dashscope.example.com",
        api_key="sk-fake",
        client=client,
    )
    run = RunTree(
        name="prompt",
        run_type="llm",
        inputs={},
        ls_client=object(),  # type: ignore[arg-type]
        project_name="test",
    )
    with tracing_context(parent=run, enabled=True):
        await provider.acomplete([Message(role="user", content="hi")])

    assert run.outputs["usage_metadata"] == {
        "input_tokens": 7,
        "output_tokens": 4,
        "total_tokens": 11,
    }
    assert run.metadata["ls_provider"] == "qwen"
    assert run.metadata["ls_model_name"] == "qwen-test"
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


@pytest.mark.asyncio
async def test_qwen_total_deadline_includes_waiting_for_global_slot() -> None:
    """A saturated limiter must not leave a provider call queued forever."""
    started = asyncio.Event()
    release = asyncio.Event()

    class BlockingClient:
        async def post(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            started.set()
            await release.wait()
            return httpx.Response(200, json={})

    loop = asyncio.get_running_loop()
    qwen_module._LOOP_LIMITERS[loop] = asyncio.Semaphore(1)
    client = BlockingClient()
    holder = QwenProvider(
        model_id="m",
        base_url="https://x",
        api_key="sk-x",
        timeout=1.0,
        max_retries=0,
        client=client,  # type: ignore[arg-type]
    )
    queued = holder.with_timeout(0.05)

    first = asyncio.create_task(holder.acomplete([Message(role="user", content="hold")]))
    await started.wait()
    before = loop.time()
    with pytest.raises(ProviderError, match="total deadline"):
        await queued.acomplete([Message(role="user", content="queued")])
    assert loop.time() - before < 0.25

    first.cancel()
    await asyncio.gather(first, return_exceptions=True)
    assert qwen_module._LOOP_LIMITERS[loop]._value == 1
    qwen_module._LOOP_LIMITERS.pop(loop, None)
