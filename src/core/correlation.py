"""Correlation / request / task id infrastructure.

One id flows through a single user request:
- HTTP layer pulls (or generates) an X-Request-Id header and binds it to
  the current context.
- Business modules read it when logging so all log lines for one request
  share the same id, making tracing easy.
"""

from __future__ import annotations

import contextvars
import uuid

_request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "tra_request_id", default=None
)

TASK_ID_HEADER = "X-Task-Id"
REQUEST_ID_HEADER = "X-Request-Id"


def new_request_id() -> str:
    return f"req_{uuid.uuid4().hex[:12]}"


def new_task_id() -> str:
    return f"task_{uuid.uuid4().hex[:12]}"


def get_request_id() -> str | None:
    return _request_id.get()


def set_request_id(value: str | None) -> contextvars.Token:
    return _request_id.set(value)


def reset_request_id(token: contextvars.Token) -> None:
    _request_id.reset(token)


__all__ = [
    "REQUEST_ID_HEADER",
    "TASK_ID_HEADER",
    "get_request_id",
    "new_request_id",
    "new_task_id",
    "reset_request_id",
    "set_request_id",
]
