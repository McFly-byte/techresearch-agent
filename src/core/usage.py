"""Per-question LLM token accounting isolated with ``ContextVar``.

Every provider call records reported usage into the collector active in its
async task context.  Child tasks inherit the context while concurrent benchmark
questions keep independent collectors.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from typing import TypedDict


class StageUsage(TypedDict):
    input_tokens: int
    output_tokens: int
    total_tokens: int
    estimated: bool


class UsageSnapshot(TypedDict):
    qid: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    estimated: bool
    by_stage: dict[str, StageUsage]
    events: list[dict[str, object]]


@dataclass(frozen=True)
class UsageEvent:
    stage: str
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    estimated: bool = False

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class UsageCollector:
    def __init__(self, qid: str) -> None:
        self.qid = qid
        self._events: list[UsageEvent] = []

    def record(self, event: UsageEvent) -> None:
        self._events.append(event)

    def snapshot(self) -> UsageSnapshot:
        by_stage: dict[str, StageUsage] = defaultdict(
            lambda: {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "estimated": False}
        )
        for event in self._events:
            stage = by_stage[event.stage]
            stage["input_tokens"] += event.input_tokens
            stage["output_tokens"] += event.output_tokens
            stage["total_tokens"] += event.total_tokens
            stage["estimated"] = stage["estimated"] or event.estimated
        input_tokens = sum(event.input_tokens for event in self._events)
        output_tokens = sum(event.output_tokens for event in self._events)
        return {
            "qid": self.qid,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "estimated": any(event.estimated for event in self._events),
            "by_stage": dict(by_stage),
            "events": [
                asdict(event) | {"total_tokens": event.total_tokens} for event in self._events
            ],
        }


_collector_var: ContextVar[UsageCollector | None] = ContextVar("usage_collector", default=None)
_stage_var: ContextVar[str] = ContextVar("usage_stage", default="research")


@contextmanager
def capture_usage(qid: str) -> Iterator[UsageCollector]:
    collector = UsageCollector(qid)
    token = _collector_var.set(collector)
    try:
        yield collector
    finally:
        _collector_var.reset(token)


@contextmanager
def usage_stage(stage: str) -> Iterator[None]:
    token = _stage_var.set(stage)
    try:
        yield
    finally:
        _stage_var.reset(token)


def record_llm_usage(
    *, provider: str, model: str, input_tokens: int, output_tokens: int, estimated: bool
) -> None:
    collector = _collector_var.get()
    if collector is None:
        return
    collector.record(
        UsageEvent(
            stage=_stage_var.get(),
            provider=provider,
            model=model,
            input_tokens=max(0, int(input_tokens)),
            output_tokens=max(0, int(output_tokens)),
            estimated=bool(estimated),
        )
    )


__all__ = [
    "UsageCollector",
    "UsageEvent",
    "capture_usage",
    "record_llm_usage",
    "usage_stage",
]
