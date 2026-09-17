"""Contract tests for the LangSmith tracing adapter.

We do NOT import langchain here — the adapter must be a no-op when disabled
and only set env vars when enabled. No real tracing calls.
"""

from __future__ import annotations

import os

from core.tracing import build_tracing_config, install_tracing_env


def test_tracing_disabled_when_no_key(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    cfg = build_tracing_config(api_key="", project="p")
    assert cfg.enabled is False
    monkeypatch.delenv("LANGCHAIN_TRACING_V2", raising=False)
    install_tracing_env(cfg)
    assert "LANGCHAIN_TRACING_V2" not in os.environ


def test_tracing_enabled_sets_env(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("LANGCHAIN_TRACING_V2", raising=False)
    monkeypatch.delenv("LANGCHAIN_PROJECT", raising=False)
    cfg = build_tracing_config(api_key="sk-real", project="myproj")
    assert cfg.enabled is True
    install_tracing_env(cfg)
    assert os.environ["LANGCHAIN_TRACING_V2"] == "true"
    assert os.environ["LANGCHAIN_PROJECT"] == "myproj"


def test_metadata_is_safe() -> None:
    cfg = build_tracing_config(api_key="k", project="p")
    md = cfg.metadata_safe(task_id="task_1")
    assert md == {"task_id": "task_1"}
    # No document body, no key.
    assert "key" not in md
    assert "prompt" not in md
