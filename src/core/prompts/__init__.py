"""Prompt Hub: unified hosting for runtime LLM prompts.

Public API:
- ``PromptSpec`` / ``RenderedPrompt``: data models.
- ``PromptRegistry``: local / langsmith / hybrid renderer.
- ``LangSmithPromptRepository``: push/pull against LangSmith.
- ``build_default_registry()`` / ``get_default_registry()``: wiring from Settings.
- ``reset_default_registry()``: test teardown.

Quick start:
    from core.prompts import get_default_registry
    reg = get_default_registry()
    rendered = reg.render("fact_extraction_system")
    system_text = rendered.system_text()
"""

from .langsmith_repo import LangSmithPromptRepository, PromptRepoError, PushResult
from .manifest import (
    LockEntry,
    ManifestEntry,
    load_lock,
    load_manifest,
    now_iso,
    save_lock,
    save_manifest,
)
from .registry import (
    PromptMode,
    PromptRegistry,
    PromptRenderError,
    build_default_registry,
    get_default_registry,
    reset_default_registry,
)
from .spec import PromptSource, PromptSpec, RenderedPrompt

__all__ = [
    "LangSmithPromptRepository",
    "LockEntry",
    "ManifestEntry",
    "PromptMode",
    "PromptRegistry",
    "PromptRenderError",
    "PromptSource",
    "PromptSpec",
    "PromptRepoError",
    "PushResult",
    "RenderedPrompt",
    "build_default_registry",
    "get_default_registry",
    "load_lock",
    "load_manifest",
    "now_iso",
    "reset_default_registry",
    "save_lock",
    "save_manifest",
]
