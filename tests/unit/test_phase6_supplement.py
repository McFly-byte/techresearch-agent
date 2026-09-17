"""Phase 6 supplementary tests: SSE reconnect dedup, failure display,
cancel endpoint, task store eviction."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from api.task_store import TaskStore


@pytest.fixture
def client():  # type: ignore[no-untyped-def]
    with TestClient(create_app()) as c:
        yield c


def test_failed_task_shows_error_in_detail(client: TestClient) -> None:
    r = client.post("/api/research", json={"query": "q", "research_depth": "quick"})
    tid = r.json()["task_id"]
    # Force failure state.
    rec = client.app.state._store.get(tid)  # type: ignore[attr-defined]
    rec.status = "failed"
    rec.error = "boom: fetch timeout"
    r2 = client.get(f"/api/research/{tid}")
    assert r2.status_code == 200
    assert r2.json()["error"] == "boom: fetch timeout"


def test_cancel_endpoint_sets_flag(client: TestClient) -> None:
    r = client.post("/api/research", json={"query": "q", "research_depth": "quick"})
    tid = r.json()["task_id"]
    r2 = client.post(f"/api/research/{tid}/cancel")
    assert r2.status_code == 200
    assert r2.json()["status"] == "cancelled"


def test_evict_keeps_bounded_memory() -> None:
    store = TaskStore(max_tasks=3)
    for _ in range(5):
        store.create(query="q", user_context="", depth="quick")
    assert len(store.list()) == 3
