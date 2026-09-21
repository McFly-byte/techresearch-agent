"""Tests for config loading and secret redaction."""

from __future__ import annotations

import re

import pytest

from core.config import REDACTED, Settings, get_settings


def setup_function(_):  # noqa: ANN001
    get_settings.cache_clear()


def teardown_function(_):  # noqa: ANN001
    get_settings.cache_clear()


def test_settings_loads_with_defaults(monkeypatch):  # type: ignore[no-untyped-def]
    # Strip any inherited env so defaults apply.
    for var in [
        "DASHSCOPE_API_KEY",
        "LANGCHAIN_API_KEY",
        "TAVILY_API_KEY",
        "TAVILY_API_KEYS",
        "FEISHU_APP_SECRET",
        "LLM_PROVIDER",
    ]:
        monkeypatch.delenv(var, raising=False)
    s = Settings(_env_file=None)
    assert s.qwen_model == "qwen3.8-max"
    assert s.qwen_base_url.startswith("https://")
    assert s.max_total_tokens == 500_000
    assert s.resolved_provider() == "fake"  # no key -> fake


def test_missing_dashscope_key_does_not_raise(monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    s = Settings(_env_file=None)
    assert s.has_dashscope_key is False
    assert s.resolved_provider() == "fake"


def test_explicit_qwen_without_key_is_flagged(monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("LLM_PROVIDER", "qwen")
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    s = Settings(_env_file=None)
    assert s.resolved_provider() == "qwen"
    # The factory must raise, not silently fall back to fake.
    from core.exceptions import ConfigurationError
    from core.providers.factory import build_provider

    try:
        build_provider(settings=s)
    except ConfigurationError:
        pass
    else:
        raise AssertionError("expected ConfigurationError when qwen provider has no key")


def test_safe_dict_masks_secrets(monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-super-secret-value")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-secret-value")
    s = Settings(_env_file=None)
    out = s.safe_dict()
    assert out["dashscope_api_key"] == REDACTED
    assert out["tavily_api_key"] == REDACTED
    # The raw secret must never appear anywhere in the serialised dict.
    blob = repr(out)
    assert "sk-super-secret-value" not in blob
    assert "tvly-secret-value" not in blob


def test_tavily_key_pool_combines_deduplicates_and_redacts(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "key-a")
    monkeypatch.setenv("TAVILY_API_KEYS", "key-a,key-b; key-c\nkey-b")
    s = Settings(_env_file=None)

    assert s.tavily_key_pool() == ("key-a", "key-b", "key-c")
    assert s.has_tavily_key is True
    assert s.safe_dict()["tavily_api_keys"] == REDACTED
    assert "key-b" not in repr(s.safe_dict())


def test_secret_str_value_not_in_json(monkeypatch):  # type: ignore[no-untyped-def]
    import json

    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-LEAK-ME")
    s = Settings(_env_file=None)
    text = json.dumps(s.safe_dict(), default=str)
    assert "LEAK-ME" not in text
    assert REDACTED in text


def test_ratio_validator_rejects_bad_value(monkeypatch):  # type: ignore[no-untyped-def]
    from pydantic import ValidationError

    monkeypatch.setenv("WARNING_TOKEN_RATIO", "1.5")
    try:
        Settings(_env_file=None)
    except ValidationError:
        pass
    else:
        raise AssertionError("expected ValidationError for ratio > 1")


def test_research_timeout_must_be_positive(monkeypatch):  # type: ignore[no-untyped-def]
    from pydantic import ValidationError

    monkeypatch.setenv("RESEARCH_TIMEOUT_SECONDS", "0")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_no_real_key_leak_in_source_tree():  # type: ignore[no-untyped-def]
    """Cheap heuristic: scan our own config module for obvious secret patterns."""
    import pathlib

    src = pathlib.Path(__file__).parent.parent.parent / "src"
    pattern = re.compile(r"sk-[A-Za-z0-9]{8,}")
    for py in src.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        assert not pattern.search(text), f"possible hard-coded key in {py}"
