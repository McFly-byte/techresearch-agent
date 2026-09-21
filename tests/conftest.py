"""Global test isolation for local credentials.

The real project `.env` may contain live API credentials. The default test suite
must remain hermetic and must never select a paid/network provider implicitly.
Individual contract tests can instantiate `Settings(_env_file=None, ...)`
explicitly when they need to exercise live-provider configuration.
"""

from __future__ import annotations

import pytest

from core.config import get_settings


@pytest.fixture(autouse=True)
def isolate_live_credentials(monkeypatch: pytest.MonkeyPatch):
    """Force the default application configuration into offline fake mode."""
    for name in (
        "DASHSCOPE_API_KEY",
        "LANGCHAIN_API_KEY",
        "TAVILY_API_KEY",
        "TAVILY_API_KEYS",
        "FEISHU_APP_ID",
        "FEISHU_APP_SECRET",
        "FEISHU_FOLDER_TOKEN",
    ):
        monkeypatch.setenv(name, "")
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
