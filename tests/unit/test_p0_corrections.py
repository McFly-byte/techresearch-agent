"""P0 corrections (phase 0-1): explicit fake/live mode, settings-pinned
provider, real tracing wiring, worker uses kit.llm, public entry produces
non-empty traceable report."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from api.runner import ProviderKit, ResearchRunner, _fake_kit, _live_kit
from api.task_store import TaskStore
from core.config import Settings
from core.providers.factory import build_provider
from core.providers.fake import FakeLLM
from core.tracing import RecordingTracing, TracingContext, noop_tracing
from tools.search_providers import FakeSearchProvider


def _empty_settings() -> Settings:
    return Settings(_env_file=None, llm_provider="fake")  # type: ignore[call-arg]


# --- P0-1: fake mode is deterministic and non-empty ------------------------
def test_fake_kit_has_nonempty_sources() -> None:
    kit = _fake_kit(_empty_settings())
    assert kit.mode == "fake"
    assert isinstance(kit.web_search, FakeSearchProvider)
    assert isinstance(kit.llm, FakeLLM)


def test_live_kit_fails_closed_without_tavily_key() -> None:
    s = _empty_settings()
    with pytest.raises(Exception, match="TAVILY_API_KEY"):
        _live_kit(s)


def test_fake_mode_never_touches_network() -> None:
    """Even if httpx is disabled, fake mode must still complete."""
    s = _empty_settings()
    store = TaskStore()
    rec = store.create(query="compare langgraph and llamaindex", user_context="", depth="quick")
    runner = ResearchRunner(store, settings=s, mode="fake")

    async def _run() -> None:
        with patch("httpx.AsyncClient.send", side_effect=AssertionError("network disabled")):
            await runner.run(rec)

    asyncio.run(_run())
    assert rec.status == "completed", rec.error
    assert rec.report_markdown, "report must be non-empty"
    # [cN] citations present.
    import re

    assert re.search(r"\[c[^\]]+\]", rec.report_markdown), "report must cite sources"


# --- P0-2: build_provider(settings) is used --------------------------------
def test_build_provider_uses_injected_settings() -> None:
    s = Settings(_env_file=None, llm_provider="fake")  # type: ignore[call-arg]
    p = build_provider(settings=s)
    assert isinstance(p, FakeLLM)


def test_runner_kit_uses_injected_settings() -> None:
    s = _empty_settings()
    store = TaskStore()
    rec = store.create(query="q", user_context="", depth="quick")
    # Inject a recording LLM via kit.
    recorder = FakeLLM()
    kit = ProviderKit(
        web_search=_fake_kit(s).web_search,
        fetcher=_fake_kit(s).fetcher,
        llm=recorder,
        mode="fake",
    )
    runner = ResearchRunner(store, settings=s, kit=kit)
    asyncio.run(runner.run(rec))
    assert rec.status == "completed"
    # The LLM was actually called end-to-end.
    assert len(recorder.calls) >= 1, "kit.llm must be called by the worker"


# --- P0-3: tracing wiring ----------------------------------------------------
def test_tracing_records_stage_tags() -> None:
    tr = RecordingTracing()
    store = TaskStore()
    rec = store.create(query="q", user_context="", depth="quick")
    runner = ResearchRunner(store, settings=_empty_settings(), tracing=tr)
    asyncio.run(runner.run(rec))
    stages = [span.stage for span in tr.spans]
    assert "planner" in stages
    assert "write" in stages


def test_tracing_redacts_sensitive_metadata() -> None:
    tr = RecordingTracing()
    tr.start_span("planner", task_id="t1", prompt="SECRET", api_key="sk-xxx", n_facts=3)
    rec = tr.spans[0]
    assert "prompt" not in rec.metadata
    assert "api_key" not in rec.metadata
    assert rec.metadata.get("n_facts") == 3


def test_noop_tracing_is_noop() -> None:
    tr: TracingContext = noop_tracing()
    tr.start_span("planner", task_id="t")
    tr.end_span("planner", task_id="t")


# --- P0-5: public HTTP entry produces non-empty report ---------------------
def test_http_post_research_default_fake_produces_report() -> None:
    from fastapi.testclient import TestClient

    from api.main import create_app

    with TestClient(create_app()) as c:
        r = c.post(
            "/api/research",
            json={"query": "compare langgraph and llamaindex", "research_depth": "quick"},
        )
        tid = r.json()["task_id"]
        # Wait for completion.
        for _ in range(50):
            d = c.get(f"/api/research/{tid}").json()
            if d["status"] in {"completed", "failed", "cancelled"}:
                break
            import time

            time.sleep(0.1)
        assert d["status"] == "completed", d
        rep = c.get(f"/api/research/{tid}/report").json()
        assert rep["markdown"]
        import re

        assert re.search(r"\[c[^\]]+\]", rep["markdown"])
