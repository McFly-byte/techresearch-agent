"""Independent phase 0-1 acceptance tests. All clients are offline doubles.

These tests encode concrete counterexamples found through the public runner
and the extractor interface, rather than relying on implementation details.
"""

from __future__ import annotations

import json
import logging
from unittest.mock import MagicMock, patch

import pytest

from api.runner import ResearchRunner
from api.task_store import TaskStore
from core.config import Settings
from core.providers.base import LLMResponse, Message
from domain.models import Citation, SourceDocument
from service.extractor import LLMFactExtractor


class RecordingProvider:
    """Never sends network requests; records precisely what would be sent."""

    provider_name = "offline-acceptance"
    model_id = "offline-acceptance"

    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.calls: list[list[Message]] = []

    async def acomplete(self, messages: list[Message]) -> LLMResponse:
        index = min(len(self.calls), len(self.responses) - 1)
        self.calls.append(list(messages))
        return LLMResponse(
            text=self.responses[index],
            model=self.model_id,
            provider=self.provider_name,
            prompt_tokens=0,
            completion_tokens=0,
        )

    def is_configured(self) -> bool:
        return True


def document(cid: str, content: str) -> SourceDocument:
    return SourceDocument(
        citation=Citation(citation_id=cid, kind="web", locator=f"https://example.com/{cid}"),
        content=content,
    )


def fact_json(claim: str, cid: str = "c1") -> str:
    return json.dumps({"facts": [{"claim": claim, "citation_ids": [cid], "snippet": claim}]})


@pytest.mark.asyncio
async def test_fake_run_never_constructs_remote_tracer_with_stored_key() -> None:
    settings = Settings(
        _env_file=None,
        tavily_api_key="offline-test-sentinel",
        dashscope_api_key="offline-test-sentinel",
        langchain_api_key="offline-test-sentinel",
        langchain_tracing_v2=False,
        feishu_app_secret="",
        llm_provider="qwen",
    )
    store = TaskStore()
    record = store.create(query="LangGraph vs LlamaIndex", user_context="", depth="quick")
    client = MagicMock()
    with (
        patch("langsmith.Client", return_value=client) as constructor,
        patch("httpx.AsyncClient.send", side_effect=AssertionError("network prohibited")) as send,
    ):
        await ResearchRunner(store, settings=settings, mode="fake").run(record)
    assert record.status == "completed"
    assert "fake" in record.report_markdown.lower()
    assert send.call_count == 0
    assert constructor.call_count == 0, "fake mode must not create an uploading tracer"
    assert client.create_run.call_count == 0


@pytest.mark.asyncio
async def test_known_citation_does_not_justify_unsupported_claim() -> None:
    source = "The service supports caching. Maximum supported retention is 10 days."
    unsupported = "The service guarantees infinite free storage forever."
    provider = RecordingProvider([fact_json(unsupported)])
    facts = await LLMFactExtractor(provider).extract([document("c1", source)])
    assert all(f.claim != unsupported for f in facts), "a valid citation ID is not evidence"
    assert all(f.claim in source for f in facts), "extractive fallback must retain source support"


@pytest.mark.asyncio
async def test_nonobject_json_gets_one_controlled_repair() -> None:
    supported = "The service supports caching and has a retention limit of ten days."
    provider = RecordingProvider(["[]", fact_json(supported)])
    facts = await LLMFactExtractor(provider).extract([document("c1", supported)])
    assert len(provider.calls) == 2
    assert [f.claim for f in facts] == [supported]


@pytest.mark.asyncio
async def test_actual_provider_messages_obey_total_input_limit() -> None:
    provider = RecordingProvider(['{"facts": []}'])
    documents = [document(f"c{i}", "a" * 6000) for i in range(25)]
    await LLMFactExtractor(provider).extract(documents)
    assert all(
        sum(len(message.content) for message in call) <= 120_000 for call in provider.calls
    ), "total system/user/repair messages, not individual document sizes, must be bounded"


@pytest.mark.asyncio
async def test_provider_exception_does_not_log_private_request_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sensitive = "PRIVATE_REQUEST_SENTINEL_DO_NOT_LOG"

    class FailingProvider(RecordingProvider):
        async def acomplete(self, messages: list[Message]) -> LLMResponse:
            raise RuntimeError(sensitive)

    provider = FailingProvider([])
    with caplog.at_level(logging.WARNING):
        await LLMFactExtractor(provider).extract(
            [document("c1", "A sufficiently long supported sentence for fallback extraction.")]
        )
    assert sensitive not in caplog.text, "log error type/code rather than raw provider exception"


@pytest.mark.asyncio
async def test_repair_echo_obeys_same_total_input_limit() -> None:
    supported = "The service supports caching and has a retention limit of ten days."
    provider = RecordingProvider(["x" * 120_000, fact_json(supported)])
    await LLMFactExtractor(provider).extract([document("c1", supported)])
    assert len(provider.calls) <= 2
    assert all(
        sum(len(message.content) for message in call) <= 120_000 for call in provider.calls
    ), "an oversized invalid response cannot bypass the repair input limit"


@pytest.mark.asyncio
async def test_fake_usage_is_estimated_even_when_token_counts_are_nonzero() -> None:
    from core.providers.fake import FakeLLM

    extractor = LLMFactExtractor(FakeLLM())
    await extractor.extract(
        [document("c1", "The service supports caching and retains data for ten days.")]
    )
    assert extractor.last_usage_estimated is True, "nonzero synthetic usage is not measured usage"


@pytest.mark.asyncio
async def test_extraction_usage_is_per_call_not_lifetime_cumulative() -> None:
    from dataclasses import replace

    supported = "The service supports caching and retains data for ten days."

    class FixedUsageProvider(RecordingProvider):
        async def acomplete(self, messages: list[Message]) -> LLMResponse:
            response = await super().acomplete(messages)
            return replace(response, prompt_tokens=100, completion_tokens=50)

    extractor = LLMFactExtractor(FixedUsageProvider([fact_json(supported)]))
    docs = [document("c1", supported)]
    await extractor.extract(docs)
    first = (extractor.last_prompt_tokens, extractor.last_completion_tokens)
    await extractor.extract(docs)
    second = (extractor.last_prompt_tokens, extractor.last_completion_tokens)
    assert first == second, "reusing an extractor must not charge earlier calls again"
    await extractor.extract([])
    assert extractor.last_prompt_tokens == extractor.last_completion_tokens == 0


@pytest.mark.asyncio
async def test_mixed_unknown_citation_is_not_silently_removed() -> None:
    supported = "The service supports caching and retains data for ten days."
    provider = RecordingProvider(
        [
            json.dumps(
                {
                    "facts": [
                        {
                            "claim": supported,
                            "citation_ids": ["c1", "missing_citation"],
                            "snippet": supported,
                        }
                    ]
                }
            )
        ]
    )
    facts = await LLMFactExtractor(provider).extract([document("c1", supported)])
    assert not facts, "an invalid citation set must be rejected rather than silently rewritten"


def test_trace_end_time_matches_installed_sdk_datetime_contract() -> None:
    from datetime import datetime

    from core.tracing import LangSmithTracing

    accepted: list[datetime] = []

    class StrictClient:
        def create_run(self, **kwargs: object) -> None:
            return None

        def update_run(self, run_id: str, **kwargs: object) -> None:
            end_time = kwargs["end_time"]
            assert isinstance(end_time, datetime), "SDK requires datetime, not epoch float"
            assert end_time.tzinfo is not None
            end_time.isoformat()
            accepted.append(end_time)

    tracer = LangSmithTracing(client=StrictClient())
    tracer.start_span("search", task_id="offline-acceptance")
    tracer.end_span("search", task_id="offline-acceptance")
    assert len(accepted) == 1, "SDK errors must not silently prevent span completion"
