"""FastAPI route tests for phase 6."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from api.main import create_app


@pytest.fixture
def client():  # type: ignore[no-untyped-def]
    with TestClient(create_app()) as c:
        yield c


def test_health(client: TestClient) -> None:
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_create_and_wait_for_task(client: TestClient) -> None:
    r = client.post("/api/research", json={"query": "test question", "research_depth": "quick"})
    assert r.status_code == 200
    task = r.json()
    tid = task["task_id"]
    assert tid.startswith("task_")

    # Poll until done.
    for _ in range(60):
        time.sleep(0.2)
        s = client.get(f"/api/research/{tid}").json()
        if s["status"] in {"completed", "failed", "cancelled"}:
            break
    assert s["status"] == "completed", f"task ended with {s['status']}: {s.get('error')}"

    # Report endpoint.
    rep = client.get(f"/api/research/{tid}/report")
    assert rep.status_code == 200
    body = rep.json()
    assert "调研报告" in body["markdown"]
    assert body["html"].startswith("<!doctype html>")


def test_get_unknown_task_404(client: TestClient) -> None:
    r = client.get("/api/research/nope")
    assert r.status_code == 404


def test_list_tasks(client: TestClient) -> None:
    client.post("/api/research", json={"query": "list test"})
    r = client.get("/api/tasks")
    assert r.status_code == 200
    assert isinstance(r.json(), list)
    assert len(r.json()) >= 1


def test_feishu_export_unconfigured_is_noop(client: TestClient) -> None:
    r = client.post("/api/research", json={"query": "feishu test"})
    tid = r.json()["task_id"]
    for _ in range(60):
        time.sleep(0.2)
        s = client.get(f"/api/research/{tid}").json()
        if s["status"] in {"completed", "failed", "cancelled"}:
            break
    r2 = client.post(f"/api/research/{tid}/export/feishu")
    assert r2.status_code == 200
    body = r2.json()
    assert body["exported"] is False
    assert body["reason"] == "not_configured"


def test_sse_stream_events_are_enveloped(client: TestClient) -> None:
    r = client.post("/api/research", json={"query": "sse test", "research_depth": "quick"})
    tid = r.json()["task_id"]
    # Give the task a moment to produce events.
    time.sleep(1.0)
    with client.stream("GET", f"/api/research/{tid}/stream") as resp:
        assert resp.status_code == 200
        body = ""
        for chunk in resp.iter_text():
            body += chunk
            if "event:" in body and "data:" in body:
                break
    assert "event_id" in body
    assert "schema_version" in body


def test_cancel_task(client: TestClient) -> None:
    r = client.post("/api/research", json={"query": "cancel test"})
    tid = r.json()["task_id"]
    r2 = client.post(f"/api/research/{tid}/cancel")
    assert r2.status_code == 200
    assert r2.json()["status"] == "cancelled"


def test_task_store_persistence_and_restart_interruption(tmp_path, monkeypatch) -> None:
    """E2E regression: completed tasks survive restart; running tasks are
    marked failed with a stable reason when the service restarts."""
    import json

    store_file = tmp_path / "tasks.json"
    monkeypatch.setenv("TASK_STORE_PATH", str(store_file))

    # First app instance: create and complete a task.
    from api.main import create_app

    with TestClient(create_app()) as c1:
        r = c1.post("/api/research", json={"query": "persist test", "research_depth": "quick"})
        tid = r.json()["task_id"]
        for _ in range(60):
            time.sleep(0.2)
            s = c1.get(f"/api/research/{tid}").json()
            if s["status"] in {"completed", "failed", "cancelled"}:
                break
        assert s["status"] == "completed"

    # Verify the task was persisted to disk.
    assert store_file.exists()
    with open(store_file, encoding="utf-8") as f:
        persisted = json.load(f)
    assert any(t["task_id"] == tid and t["status"] == "completed" for t in persisted)

    # Simulate a crash: manually mark the completed task as "running" in the
    # persisted file, as if the process died mid-execution.
    for t in persisted:
        if t["task_id"] == tid:
            t["status"] = "running"
            t["error"] = None
    with open(store_file, "w", encoding="utf-8") as f:
        json.dump(persisted, f, ensure_ascii=False)

    # Second app instance (simulating restart): the running task must be
    # marked failed with a stable, well-known reason.
    with TestClient(create_app()) as c2:
        s2 = c2.get(f"/api/research/{tid}").json()
        assert s2["status"] == "failed"
        assert s2["error"] == "interrupted_by_service_restart"

        # The task still appears in the list with its corrected state.
        listing = c2.get("/api/tasks").json()
        match = [t for t in listing if t["task_id"] == tid]
        assert len(match) == 1
        assert match[0]["status"] == "failed"
