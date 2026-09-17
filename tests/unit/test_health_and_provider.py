"""Tests for the /api/health endpoint and provider selection."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from core.config import Settings, get_settings
from core.providers.base import Message
from core.providers.factory import build_provider
from core.providers.fake import FakeLLM


@pytest.fixture(autouse=True)
def _reset_settings():  # type: ignore[no-untyped-def]
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_health_returns_stable_json(monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    app = create_app()
    client = TestClient(app)
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["phase"] == 6
    assert body["service"] == "techresearch-agent"
    assert body["llm"]["provider"] == "fake"
    # No secret field anywhere.
    blob = repr(body)
    for needle in ("dashscope_api_key", "DASHSCOPE_API_KEY", "api_key"):
        assert needle not in blob.lower()


def test_health_echoes_request_id(monkeypatch):  # type: ignore[no-untyped-def]
    app = create_app()
    client = TestClient(app)
    r = client.get("/api/health", headers={"X-Request-Id": "req-xyz"})
    assert r.headers["X-Request-Id"] == "req-xyz"


@pytest.mark.asyncio
async def test_fake_llm_echoes():  # type: ignore[no-untyped-def]
    llm = FakeLLM()
    resp = await llm.acomplete([Message(role="user", content="hello world")])
    assert "hello world" in resp.text
    assert resp.provider == "fake"
    assert resp.prompt_tokens > 0


def test_factory_auto_picks_fake_without_key(monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "auto")
    s = Settings(_env_file=None)
    p = build_provider(settings=s)
    assert isinstance(p, FakeLLM)
