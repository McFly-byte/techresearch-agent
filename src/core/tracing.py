"""Tracing adapter: noop by default, real LangSmith when configured.

Design contract:
- When no ``LANGCHAIN_API_KEY`` is configured, ``build_tracing()`` returns a
  ``noop_tracing()`` that makes zero network calls. This keeps the offline
  test suite hermetic.
- When configured, ``LangSmithTracing`` wraps a ``langsmith.Client`` (injectable
  for tests) and creates real runs via ``client.create_run`` /
  ``client.update_run``.
- Span metadata is redacted by a blacklist + whitelist: document bodies,
  prompts, and API keys never appear in inputs/outputs/error.
"""

from __future__ import annotations

import contextlib
import logging
import time
import uuid
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

log = logging.getLogger(__name__)

# Tags that identify the stage a span belongs to.
ALLOWED_STAGES = frozenset(
    {"planner", "worker", "search", "fetch", "extract", "reflect", "verify", "write"}
)

# --- Sensitivity control ----------------------------------------------------
# Blacklist: any metadata key whose lowercased form contains one of these
# substrings is DROPPED from span inputs/outputs.
_SENSITIVE_PARTS = (
    "prompt",
    "key",
    "secret",
    "document",
    "body",
    "content",
    "snippet",
    "text",
    "message",
    "header",
    "token",
    "password",
    "authorization",
)

# Whitelist: only these metadata keys are allowed through (in addition to
# task_id and stage which are always safe).
_SAFE_KEYS = frozenset(
    {
        "n_results",
        "n_facts",
        "n_citations",
        "duration",
        "status",
        "round",
        "iteration",
        "error_type",
        "stop_reason",
        "model",
        "provider",
        "mode",
    }
)


def _redact_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    """Blacklist + whitelist filter for span metadata.

    - Drop any key matching a sensitive substring.
    - Keep only keys in the safe whitelist (plus task_id/stage which are
      always present and safe).
    """
    out: dict[str, Any] = {}
    for k, v in metadata.items():
        kl = k.lower()
        if any(bad in kl for bad in _SENSITIVE_PARTS):
            continue
        if (k in ("task_id", "stage") or k in _SAFE_KEYS) and isinstance(
            v, str | int | float | bool
        ):
            out[k] = v
    return out


# --- Span record type -------------------------------------------------------


@dataclass(frozen=True)
class SpanRecord:
    """A single start/end event. Replaces the old inconsistent tuple."""

    action: str  # "start" or "end"
    stage: str
    task_id: str
    metadata: dict[str, Any] = field(default_factory=dict)


# --- Base TracingContext ----------------------------------------------------


class TracingContext:
    """Minimal span recorder. Default no-op; subclasses record or forward."""

    def start_span(self, stage: str, *, task_id: str, **metadata: Any) -> SpanRecord:
        if stage not in ALLOWED_STAGES:
            raise ValueError(f"unknown tracing stage: {stage}")
        return SpanRecord(action="start", stage=stage, task_id=task_id, metadata={})

    def end_span(
        self,
        stage: str,
        *,
        task_id: str,
        status: str = "ok",
        error: Exception | str | None = None,
        **metadata: Any,
    ) -> SpanRecord:
        return SpanRecord(action="end", stage=stage, task_id=task_id, metadata={"status": status})

    def llm_prompt_span(
        self,
        prompt_name: str,
        prompt_commit: str,
        *,
        task_id: str = "",
    ) -> contextlib.AbstractContextManager[None]:
        """Context manager that wraps a single LLM call bound to a Prompt Hub prompt.

        The default implementation is a zero-cost noop (``nullcontext``) so that
        offline / noop tracing makes ZERO network calls. Real subclasses
        (``LangSmithTracing``) create an LLM-type run whose
        ``extra.metadata`` carries ``lc_hub_repo`` / ``lc_hub_commit_hash`` —
        this is exactly the signal LangSmith uses to auto-associate a prompt
        to an Application.
        """
        return contextlib.nullcontext()


def noop_tracing() -> TracingContext:
    return TracingContext()


# --- Recording tracing (tests) ----------------------------------------------


@dataclass
class RecordingTracing(TracingContext):
    """Records (SpanRecord) for contract tests. Metadata is pre-redacted."""

    spans: list[SpanRecord] = field(default_factory=list)

    def start_span(self, stage: str, *, task_id: str, **metadata: Any) -> SpanRecord:
        super().start_span(stage, task_id=task_id)
        safe = _redact_metadata(metadata)
        rec = SpanRecord(action="start", stage=stage, task_id=task_id, metadata=safe)
        self.spans.append(rec)
        return rec

    def end_span(
        self,
        stage: str,
        *,
        task_id: str,
        status: str = "ok",
        error: Exception | str | None = None,
        **metadata: Any,
    ) -> SpanRecord:
        super().end_span(stage, task_id=task_id, status=status, error=error, **metadata)
        meta: dict[str, Any] = {"status": status}
        if error is not None:
            # Only record the error TYPE, never the message content.
            meta["error_type"] = type(error).__name__ if isinstance(error, Exception) else "error"
        # Merge safe extra metadata (n_results, n_facts, etc.)
        for k, v in _redact_metadata(metadata).items():
            meta[k] = v
        rec = SpanRecord(action="end", stage=stage, task_id=task_id, metadata=meta)
        self.spans.append(rec)
        return rec


# --- LangSmith real adapter -------------------------------------------------


class LangSmithTracing(TracingContext):
    """Real LangSmith adapter backed by ``langsmith.Client``.

    The client is injectable for tests; in production it is constructed from
    (api_key, project_name, endpoint). When api_key is empty, ``build_tracing``
    returns ``noop_tracing()`` instead, so this class is never instantiated
    without a client.
    """

    def __init__(
        self,
        *,
        api_key: str = "",
        project_name: str = "tech-research-agent",
        endpoint: str | None = None,
        client: Any | None = None,
    ) -> None:
        if client is not None:
            self._client = client
        elif api_key:
            from langsmith import Client

            self._client = Client(api_key=api_key, api_url=endpoint)
        else:
            # Caller must use noop_tracing() — but guard anyway.
            raise ValueError("LangSmithTracing requires either api_key or an injected client")
        self._project = project_name
        self._open: dict[tuple[str, str], str] = {}  # (stage, task_id) -> run_id
        self._start_times: dict[tuple[str, str], float] = {}
        self._run_stack: list[str] = []  # legacy top-level fallback only
        self._active_tree: ContextVar[Any | None] = ContextVar(
            f"langsmith_active_tree_{id(self)}", default=None
        )
        self._tree_open: dict[tuple[str, str], tuple[Any, Token[Any | None], float]] = {}

    def _current_tree(self) -> Any | None:
        active = self._active_tree.get()
        if active is not None:
            return active
        try:
            from langsmith.run_helpers import get_current_run_tree

            return get_current_run_tree()
        except Exception:  # noqa: BLE001 - tracing is best effort
            return None

    def start_span(self, stage: str, *, task_id: str, **metadata: Any) -> SpanRecord:
        super().start_span(stage, task_id=task_id)
        safe = _redact_metadata(metadata)
        run_id = str(uuid.uuid4())
        key = (stage, task_id)
        parent = self._current_tree()
        if parent is not None and hasattr(parent, "create_child"):
            try:
                child = parent.create_child(
                    stage,
                    run_type="chain",
                    run_id=run_id,
                    inputs=safe,
                    tags=[stage, task_id],
                    extra={"metadata": {"task_id": task_id, "stage": stage}},
                )
                child.post()
                token = self._active_tree.set(child)
                self._tree_open[key] = (child, token, time.time())
                return SpanRecord(action="start", stage=stage, task_id=task_id, metadata=safe)
            except Exception as e:  # noqa: BLE001
                log.warning("langsmith_child_run_start_failed error_type=%s", type(e).__name__)
        self._open[key] = run_id
        self._start_times[key] = time.time()
        self._run_stack.append(run_id)
        with contextlib.suppress(Exception):
            self._client.create_run(
                id=run_id,
                name=stage,
                run_type="chain",
                project_name=self._project,
                inputs=safe,
                tags=[stage, task_id],
                extra={"metadata": {"task_id": task_id, "stage": stage}},
            )
        return SpanRecord(action="start", stage=stage, task_id=task_id, metadata=safe)

    def end_span(
        self,
        stage: str,
        *,
        task_id: str,
        status: str = "ok",
        error: Exception | str | None = None,
        **metadata: Any,
    ) -> SpanRecord:
        super().end_span(stage, task_id=task_id, status=status, error=error, **metadata)
        key = (stage, task_id)
        tree_rec = self._tree_open.pop(key, None)
        if tree_rec is not None:
            child, token, start_ts = tree_rec
            tree_outputs: dict[str, Any] = {
                "status": status,
                "duration": round(time.time() - start_ts, 4),
            }
            if error is not None:
                tree_outputs["error_type"] = (
                    type(error).__name__ if isinstance(error, Exception) else "error"
                )
            tree_outputs.update(_redact_metadata(metadata))
            try:
                child.end(outputs=tree_outputs, error=tree_outputs.get("error_type"))
                child.patch()
            except Exception as e:  # noqa: BLE001
                log.warning("langsmith_child_run_end_failed error_type=%s", type(e).__name__)
            finally:
                with contextlib.suppress(Exception):
                    self._active_tree.reset(token)
            return SpanRecord(action="end", stage=stage, task_id=task_id, metadata=tree_outputs)
        run_id = self._open.pop(key, None)
        fallback_start_ts = self._start_times.pop(key) if key in self._start_times else None
        if run_id and run_id in self._run_stack:
            self._run_stack.remove(run_id)
        outputs: dict[str, Any] = {"status": status}
        if fallback_start_ts is not None:
            outputs["duration"] = round(time.time() - fallback_start_ts, 4)
        if error is not None:
            # Only error type/class name, never the message body.
            outputs["error_type"] = (
                type(error).__name__ if isinstance(error, Exception) else "error"
            )
        # Merge safe extra metadata.
        for k, v in _redact_metadata(metadata).items():
            outputs[k] = v
        if run_id is not None:
            # The installed langsmith SDK expects ``end_time`` as a timezone-aware
            # datetime (it calls .isoformat() on it), not a float epoch.
            try:
                self._client.update_run(
                    run_id,
                    outputs=outputs,
                    end_time=datetime.now(UTC),
                )
            except Exception as e:  # noqa: BLE001
                # Visible failure (logged), but never let a tracing client error
                # prevent the span from completing locally.
                log.warning(
                    "langsmith_update_run_failed error_type=%s",
                    type(e).__name__,
                )
        return SpanRecord(action="end", stage=stage, task_id=task_id, metadata=outputs)

    @contextlib.contextmanager
    def llm_prompt_span(
        self,
        prompt_name: str,
        prompt_commit: str,
        *,
        task_id: str = "",
    ):
        """Create an LLM-type LangSmith run tagged with the Prompt Hub prompt.

        On enter we ``create_run(run_type="llm", ...)`` whose
        ``extra.metadata`` contains ``lc_hub_repo`` (the prompt name) and
        ``lc_hub_commit_hash`` (the lock-file commit). On exit we
        ``update_run(..., end_time=...)`` to close it.

        The run is a TOP-LEVEL run in the project (we do not set parent_id):
        LangSmith associates it to the project/application purely from the
        prompt metadata. A ``task_id`` is accepted for symmetry with the stage
        spans but is only attached as a tag — it does not force a parent link.
        Every network call is wrapped in ``suppress(Exception)`` so a tracing
        failure never breaks the real research call.
        """
        run_id = str(uuid.uuid4())
        start_ts = time.time()
        parent_tree = self._current_tree()
        if parent_tree is not None and hasattr(parent_tree, "create_child"):
            child = None
            token = None
            try:
                child = parent_tree.create_child(
                    prompt_name,
                    run_type="llm",
                    run_id=run_id,
                    inputs={},
                    tags=[prompt_name, task_id] if task_id else [prompt_name],
                    extra={
                        "metadata": {
                            "lc_hub_repo": prompt_name,
                            "lc_hub_commit_hash": prompt_commit,
                        }
                    },
                )
                child.post()
                token = self._active_tree.set(child)
            except Exception as e:  # noqa: BLE001
                log.warning("langsmith_llm_child_start_failed error_type=%s", type(e).__name__)
            try:
                yield
            finally:
                if child is not None:
                    with contextlib.suppress(Exception):
                        child.end(outputs={"status": "ok"})
                        child.patch()
                if token is not None:
                    with contextlib.suppress(Exception):
                        self._active_tree.reset(token)
            return
        parent_id = self._run_stack[-1] if self._run_stack else None
        with contextlib.suppress(Exception):
            # NOTE: ``inputs`` is a REQUIRED positional of create_run; passing an
            # empty dict keeps the run redaction policy (no prompt bodies leak)
            # while satisfying the SDK signature. Association only depends on the
            # extra.metadata below.
            kwargs: dict[str, Any] = dict(
                id=run_id,
                name=prompt_name,
                run_type="llm",
                project_name=self._project,
                inputs={},
                tags=[prompt_name],
                extra={
                    "metadata": {
                        "lc_hub_repo": prompt_name,
                        "lc_hub_commit_hash": prompt_commit,
                    }
                },
            )
            if parent_id is not None:
                kwargs["parent_run_id"] = parent_id
            self._client.create_run(**kwargs)
        try:
            yield
        finally:
            with contextlib.suppress(Exception):
                self._client.update_run(
                    run_id,
                    end_time=datetime.now(UTC),
                )
                _ = start_ts  # duration available for future use; not sent today


# --- Factory ----------------------------------------------------------------


def build_tracing(
    *,
    api_key: str,
    project_name: str = "tech-research-agent",
    endpoint: str | None = None,
    client: Any | None = None,
) -> TracingContext:
    """Build a TracingContext. Returns noop when api_key is blank.

    Args:
        api_key: LangSmith API key. Blank/whitespace → noop, zero network.
        project_name: LangSmith project name.
        endpoint: Optional custom LangSmith API URL.
        client: Injected langsmith.Client (for tests). When provided,
            api_key may be empty.
    """
    if client is not None:
        return LangSmithTracing(
            api_key=api_key or "injected",
            project_name=project_name,
            endpoint=endpoint,
            client=client,
        )
    if not api_key or not api_key.strip():
        return noop_tracing()
    return LangSmithTracing(api_key=api_key, project_name=project_name, endpoint=endpoint)


# --- Legacy config helpers (kept for backward compatibility) ----------------


@dataclass(frozen=True)
class TracingConfig:
    enabled: bool
    project: str
    tags: tuple[str, ...] = ()

    def metadata_safe(self, task_id: str) -> dict[str, str]:
        return {"task_id": task_id}


def build_tracing_config(*, api_key: str | None, project: str) -> TracingConfig:
    enabled = bool(api_key and api_key.strip())
    return TracingConfig(enabled=enabled, project=project)


def install_tracing_env(cfg: TracingConfig) -> None:
    """Set langchain tracing env vars. No-op when disabled."""
    if not cfg.enabled:
        return
    import os

    os.environ.setdefault("LANGCHAIN_TRACING_V2", "true")
    os.environ.setdefault("LANGCHAIN_PROJECT", cfg.project)


__all__ = [
    "ALLOWED_STAGES",
    "LangSmithTracing",
    "RecordingTracing",
    "SpanRecord",
    "TracingConfig",
    "TracingContext",
    "build_tracing",
    "build_tracing_config",
    "install_tracing_env",
    "noop_tracing",
]
