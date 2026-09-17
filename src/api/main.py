"""FastAPI application: health + research task API + SSE.

Schema (frozen):
  POST /api/research                       -> create task
  GET  /api/research/{task_id}             -> task status
  GET  /api/research/{task_id}/report      -> markdown + html
  GET  /api/research/{task_id}/stream       -> SSE
  GET  /api/tasks                          -> list recent
  POST /api/research/{task_id}/export/feishu -> stub
  POST /api/research/{task_id}/cancel      -> request cancel
  GET  /api/health
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from api.runner import ResearchRunner
from api.task_store import TaskRecord, TaskStore
from core.config import get_settings, has_any_real_key
from core.correlation import REQUEST_ID_HEADER, new_request_id, set_request_id
from core.exceptions import TRAError
from core.logging import configure_logging
from core.providers.factory import build_provider
from service.exporters import FeishuExporter


# ---------- Pydantic schemas (explicit response models) -------------------
class ResearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    user_context: str = ""
    research_depth: str = "standard"


class TaskSummary(BaseModel):
    task_id: str
    query: str
    status: str
    created_at: float
    updated_at: float
    error: str = ""


class TaskDetail(TaskSummary):
    user_context: str = ""
    research_depth: str = "standard"
    n_events: int = 0


class ReportResponse(BaseModel):
    task_id: str
    markdown: str
    html: str
    status: str
    mode: str = ""


class EventEnvelope(BaseModel):
    event_id: str
    event_type: str
    task_id: str
    timestamp: float
    stage: str
    data: dict
    schema_version: str


# ---------- app factory ---------------------------------------------------
def create_app() -> FastAPI:
    configure_logging()
    settings = get_settings()

    app = FastAPI(title="TechResearch Agent", version="0.6.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    store = TaskStore(
        persist_path=Path(settings.task_store_path) if settings.task_store_path else None
    )
    # On startup, any task still "running"/"queued" from a previous process is
    # orphaned — the background asyncio worker died with the old process. Mark
    # them failed with a stable reason so the UI and history show a clear state.
    for rec in store.list():
        if rec.status in {"running", "queued"}:
            rec.status = "failed"
            rec.error = "interrupted_by_service_restart"
            rec.updated_at = time.time()
    store._save()  # persist the corrected states
    runner = ResearchRunner(store)
    feishu = FeishuExporter()
    app.state._store = store  # test hook; not part of public API

    @app.middleware("http")
    async def bind_request_id(request: Request, call_next):
        incoming = request.headers.get(REQUEST_ID_HEADER)
        rid = incoming or new_request_id()
        token = set_request_id(rid)
        request.state.request_id = rid
        try:
            response = await call_next(request)
        finally:
            from core.correlation import reset_request_id

            reset_request_id(token)
        response.headers[REQUEST_ID_HEADER] = rid
        return response

    @app.exception_handler(TRAError)
    async def _tra_error(_: Request, exc: TRAError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status, content=exc.to_public_dict())

    # ---- health ----
    @app.get("/api/health")
    async def health() -> dict:
        s = get_settings()
        provider = build_provider()
        return {
            "status": "ok",
            "phase": 6,
            "service": "techresearch-agent",
            "timestamp": int(time.time()),
            "has_external_keys": has_any_real_key(),
            "llm": provider.describe(),
            "config": {
                "qwen_model": s.qwen_model,
                "cors_origins": s.cors_origin_list,
            },
        }

    # ---- create task ----
    @app.post("/api/research", response_model=TaskDetail)
    async def create_task(req: ResearchRequest) -> TaskDetail:
        rec = store.create(query=req.query, user_context=req.user_context, depth=req.research_depth)
        # Fire background task.
        asyncio.create_task(run_safe(runner, rec))
        d = rec.to_public()
        return TaskDetail(**d)

    # ---- get task ----
    @app.get("/api/research/{task_id}", response_model=TaskDetail)
    async def get_task(task_id: str) -> TaskDetail:
        rec = store.get(task_id)
        if rec is None:
            raise HTTPException(status_code=404, detail="task not found")
        return TaskDetail(**rec.to_public())

    # ---- list tasks ----
    @app.get("/api/tasks", response_model=list[TaskSummary])
    async def list_tasks() -> list[TaskSummary]:
        return [TaskSummary(**r.to_public()) for r in store.list()]

    # ---- report ----
    @app.get("/api/research/{task_id}/report", response_model=ReportResponse)
    async def get_report(task_id: str) -> ReportResponse:
        rec = store.get(task_id)
        if rec is None:
            raise HTTPException(status_code=404, detail="task not found")
        if rec.status not in {"completed", "failed", "cancelled"}:
            raise HTTPException(status_code=409, detail="task not finished yet")
        return ReportResponse(
            task_id=rec.task_id,
            markdown=rec.report_markdown,
            html=rec.report_html,
            status=rec.status,
            mode=rec.mode,
        )

    # ---- cancel ----
    @app.post("/api/research/{task_id}/cancel")
    async def cancel_task(task_id: str) -> dict:
        rec = store.get(task_id)
        if rec is None:
            raise HTTPException(status_code=404, detail="task not found")
        rec._cancel_requested = True
        rec.status = "cancelled"
        return {"task_id": task_id, "status": "cancelled"}

    # ---- feishu export stub ----
    @app.post("/api/research/{task_id}/export/feishu")
    async def export_feishu(task_id: str) -> dict:
        rec = store.get(task_id)
        if rec is None:
            raise HTTPException(status_code=404, detail="task not found")
        result = await feishu.export_markdown(title=rec.query, markdown=rec.report_markdown)
        return {"exported": result.exported, "reason": result.reason, "doc_url": result.doc_url}

    # ---- SSE stream ----
    @app.get("/api/research/{task_id}/stream")
    async def stream(
        task_id: str,
        last_event_id: Annotated[str | None, Header()] = None,
    ) -> StreamingResponse:
        rec = store.get(task_id)
        if rec is None:
            raise HTTPException(status_code=404, detail="task not found")

        async def gen():
            # Replay from last_event_id if provided.
            start_idx = 0
            if last_event_id:
                for i, ev in enumerate(rec.events):
                    if ev["event_id"] == last_event_id:
                        start_idx = i + 1
                        break
            for ev in rec.events[start_idx:]:
                yield _sse(ev)
            # If already terminal, close immediately.
            if rec.status in {"completed", "failed", "cancelled"}:
                return
            # Otherwise poll for new events (simple, no redis).
            last_sent = len(rec.events)
            for _ in range(600):  # up to ~60s
                await asyncio.sleep(0.1)
                if len(rec.events) > last_sent:
                    for ev in rec.events[last_sent:]:
                        yield _sse(ev)
                    last_sent = len(rec.events)
                if rec.status in {"completed", "failed", "cancelled"}:
                    return

        return StreamingResponse(gen(), media_type="text/event-stream")

    return app


def _sse(ev: dict) -> str:
    return f"id: {ev['event_id']}\nevent: {ev['event_type']}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n"


async def run_safe(runner: ResearchRunner, rec: TaskRecord) -> None:
    try:
        await runner.run(rec)
    except Exception as e:  # noqa: BLE001
        rec.status = "failed"
        rec.error = str(e)


app = create_app()


__all__ = ["app", "create_app"]
