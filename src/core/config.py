"""Application configuration loaded from environment / .env via pydantic-settings.

Design contract (docs/技术设计文档.md ch.11):
- All secrets are OPTIONAL in Phase 0. The app must boot and all tests must
  pass with zero external API keys present.
- Secret-bearing fields must never be rendered in logs, reprs, or JSON output.
  We achieve this with pydantic `SecretStr` plus a custom `safe_dict()` helper
  that returns masked values.
- Model ids come from config, never hard-coded at call sites.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Marker used when we emit a masked secret. Chosen to be obviously non-secret.
REDACTED = "***REDACTED***"

# Keys whose values must never appear in plain text in logs / doctor output.
SECRET_FIELD_NAMES: frozenset[str] = frozenset(
    {
        "dashscope_api_key",
        "langchain_api_key",
        "tavily_api_key",
        "feishu_app_secret",
    }
)


class Settings(BaseSettings):
    """Strongly-typed application settings.

    Precedence (highest first): environment variable > .env file > default.
    Field names are matched case-insensitively to env vars.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- LLM / DashScope ---------------------------------------------------
    dashscope_api_key: SecretStr = Field(default=SecretStr(""))
    qwen_model: str = "qwen3.8-max"
    qwen_model_fast: str = "qwen3.8-flash"
    qwen_embedding_model: str = "text-embedding-v4"
    qwen_rerank_model: str = "qwen3-rerank"
    qwen_verifier_model: str = "qwen3.8-max"
    qwen_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"

    # ---- Provider selection ------------------------------------------------
    # "auto" -> pick fake when no real key, else qwen.
    llm_provider: Literal["auto", "fake", "qwen"] = "auto"

    # ---- Prompt Hub --------------------------------------------------------
    # local    : force local manifest, zero network (default).
    # langsmith: force remote; any failure raises.
    # hybrid   : try remote, fall back to local on error.
    prompt_source: Literal["local", "langsmith", "hybrid"] = "local"
    prompt_cache_ttl: float = 300.0  # seconds
    langsmith_prompt_timeout: float = 10.0  # seconds

    # ---- LangSmith ---------------------------------------------------------
    langchain_tracing_v2: bool = False
    langchain_api_key: SecretStr = Field(default=SecretStr(""))
    langchain_project: str = "tech-research-agent"

    # ---- Search ------------------------------------------------------------
    tavily_api_key: SecretStr = Field(default=SecretStr(""))

    # ---- Feishu ------------------------------------------------------------
    feishu_app_id: str = ""
    feishu_app_secret: SecretStr = Field(default=SecretStr(""))
    feishu_folder_token: str = ""

    # ---- Storage -----------------------------------------------------------
    vector_db_path: str = "./data/chroma"
    memory_db_path: str = "./data/memory"
    # TaskStore JSON persistence. When set (non-empty), completed/failed/cancelled
    # tasks survive a service restart. Tasks still "running"/"queued" at restart
    # are marked failed with error="interrupted_by_service_restart" because the
    # background asyncio worker no longer exists. Empty = in-memory only (default).
    task_store_path: str = ""
    # Memory is OPT-IN (Stage4 P0-4): default OFF. When memory_enabled=True the
    # runner may READ consented preferences to build user_context, but it only
    # WRITES a conclusion_summary when the caller explicitly passes consent.
    memory_enabled: bool = False
    memory_namespace: str = "session/default"

    # ---- Budget ------------------------------------------------------------
    max_total_tokens: int = 500_000
    max_search_rounds: int = 10
    max_workers: int = 3
    # Optional per-task token hard budget. None / 0 disables it (global only).
    per_task_token_budget: int | None = None
    warning_token_ratio: float = 0.8
    critical_token_ratio: float = 0.95

    # ---- API server --------------------------------------------------------
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    cors_origins: str = "http://localhost:5173"

    @field_validator("warning_token_ratio", "critical_token_ratio")
    @classmethod
    def _ratio_in_unit_interval(cls, v: float) -> float:
        if not 0.0 < v <= 1.0:
            raise ValueError("ratio must be in (0, 1]")
        return v

    # ---- Helpers -----------------------------------------------------------
    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def has_dashscope_key(self) -> bool:
        return bool(self.dashscope_api_key.get_secret_value().strip())

    @property
    def has_tavily_key(self) -> bool:
        return bool(self.tavily_api_key.get_secret_value().strip())

    @property
    def has_langsmith_key(self) -> bool:
        return bool(self.langchain_api_key.get_secret_value().strip())

    def resolved_provider(self) -> Literal["fake", "qwen"]:
        """Which LLM provider the factory should instantiate right now."""
        if self.llm_provider == "fake":
            return "fake"
        if self.llm_provider == "qwen":
            return "qwen"
        # auto: qwen only when a key is actually present.
        return "qwen" if self.has_dashscope_key else "fake"

    def safe_dict(self) -> dict[str, object]:
        """Return a JSON-serialisable view with all secrets masked.

        Used by CLI doctor and /api/health so we never leak a key.
        """
        out: dict[str, object] = {}
        for name, _field in type(self).model_fields.items():
            raw = getattr(self, name)
            if name in SECRET_FIELD_NAMES or isinstance(raw, SecretStr):
                present = (
                    bool(raw.get_secret_value().strip()) if isinstance(raw, SecretStr) else False
                )
                out[name] = REDACTED if present else ""
            elif name == "cors_origins":
                out[name] = self.cors_origin_list
            else:
                out[name] = raw
        return out


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings singleton. Tests clear this via get_settings.cache_clear()."""
    return Settings()


def project_root() -> Path:
    """Repository root (two levels up from this file: src/core/config.py)."""
    return Path(__file__).resolve().parents[2]


# Convenience singleton matching design-doc example, but use get_settings()
# in tests / request handlers so env overrides work.
settings: Settings = get_settings()


def env_file_present() -> bool:
    return (project_root() / ".env").is_file()


def env_var_names() -> list[str]:
    """Env var names pydantic-settings will read for this Settings class."""
    return [f.upper() for f in Settings.model_fields]


def has_any_real_key() -> bool:
    s = get_settings()
    return (
        s.has_dashscope_key
        or s.has_tavily_key
        or s.has_langsmith_key
        or bool(s.feishu_app_secret.get_secret_value().strip())
    )


__all__ = [
    "REDACTED",
    "SECRET_FIELD_NAMES",
    "Settings",
    "env_file_present",
    "env_var_names",
    "get_settings",
    "has_any_real_key",
    "project_root",
    "settings",
]

# Silence linters that flag unused os import; we keep it for downstream code
# that may inspect os.environ directly in tests.
_ = os.environ
