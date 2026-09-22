"""In-memory task store for phase 6.

Limitations (explicit):
- Single-process, in-memory. Restart loses running tasks (checkpoint on disk
  can resume completed/failed state, but we don't auto-recover background
  workers here).
- No auth, no multi-user.
- Bounded history: keep last N tasks to avoid unbounded memory.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

TaskStatus = Literal["queued", "running", "completed", "failed", "cancelled"]


@dataclass
class TaskRecord:
    task_id: str
    query: str
    user_context: str = ""
    research_depth: str = "standard"
    status: TaskStatus = "queued"
    mode: str = ""  # "fake" or "live", set by ResearchRunner at runtime
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    events: list[dict[str, Any]] = field(default_factory=list)
    report_markdown: str = ""
    report_html: str = ""
    error: str = ""
    # Research-time source constraints (from EvalPrompt). Empty by default so
    # interactive / non-eval tasks are unaffected. The worker + report builder
    # consult these to drop blocked sources and post-cutoff material.
    blocked_urls: list[str] = field(default_factory=list)
    blocked_domains: list[str] = field(default_factory=list)
    blocked_titles: list[str] = field(default_factory=list)
    as_of_date: str | None = None
    # Exposed to the eval harness: real citations the report relied on and the
    # token usage of the run. ``usage_estimated`` is True when the counts are
    # character-derived estimates (fake / heuristic), False when the provider
    # reported real usage from the API.
    citations: list[dict[str, Any]] = field(default_factory=list)
    usage_tokens: int = 0
    usage_estimated: bool = True
    # Quality-gate result of the LLM-synthesized report (see
    # service.verified_report.check_report_quality). Empty in template/fake mode.
    report_quality: dict[str, Any] = field(default_factory=dict)
    coverage_matrix: list[dict[str, Any]] = field(default_factory=list)
    # Per-task asyncio handle so cancel can request stop.
    _cancel_requested: bool = False

    def to_public(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "query": self.query,
            "user_context": self.user_context,
            "research_depth": self.research_depth,
            "status": self.status,
            "mode": self.mode,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "n_events": len(self.events),
            "error": self.error,
            "n_citations": len(self.citations),
            "usage_tokens": self.usage_tokens,
            "usage_estimated": self.usage_estimated,
            "coverage_matrix": self.coverage_matrix,
        }


class TaskStore:
    def __init__(self, *, max_tasks: int = 100, persist_path: Path | None = None) -> None:
        self._tasks: dict[str, TaskRecord] = {}
        self._max = max_tasks
        self._lock = asyncio.Lock()
        self._persist_path = persist_path
        if self._persist_path and self._persist_path.exists():
            try:
                raw = json.loads(self._persist_path.read_text(encoding="utf-8"))
                for rec in raw:
                    self._tasks[rec["task_id"]] = TaskRecord(**rec)
            except Exception:
                pass

    def create(self, *, query: str, user_context: str, depth: str) -> TaskRecord:
        tid = f"task_{uuid.uuid4().hex[:8]}"
        rec = TaskRecord(task_id=tid, query=query, user_context=user_context, research_depth=depth)
        self._tasks[tid] = rec
        self._evict()
        self._save()
        return rec

    def get(self, task_id: str) -> TaskRecord | None:
        return self._tasks.get(task_id)

    def list(self) -> list[TaskRecord]:
        return sorted(self._tasks.values(), key=lambda t: -t.created_at)

    def append_event(self, task_id: str, event: dict[str, Any]) -> None:
        rec = self._tasks.get(task_id)
        if rec is None:
            return
        rec.events.append(event)
        rec.updated_at = time.time()
        self._save()

    def _evict(self) -> None:
        if len(self._tasks) <= self._max:
            return
        oldest = sorted(self._tasks.values(), key=lambda t: t.created_at)[
            : len(self._tasks) - self._max
        ]
        for rec in oldest:
            self._tasks.pop(rec.task_id, None)

    def _save(self) -> None:
        if not self._persist_path:
            return
        try:
            payload = [
                {
                    "task_id": t.task_id,
                    "query": t.query,
                    "user_context": t.user_context,
                    "research_depth": t.research_depth,
                    "status": t.status,
                    "created_at": t.created_at,
                    "updated_at": t.updated_at,
                    "events": t.events,
                    "report_markdown": t.report_markdown,
                    "report_html": t.report_html,
                    "coverage_matrix": t.coverage_matrix,
                    "error": t.error,
                }
                for t in self._tasks.values()
            ]
            self._persist_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass


__all__ = ["TaskRecord", "TaskStatus", "TaskStore"]
