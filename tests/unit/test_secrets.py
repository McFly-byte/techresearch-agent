"""Test that secrets never leak through logs or serialisation paths."""

from __future__ import annotations

import json
import logging

from core.config import REDACTED, Settings, get_settings


def setup_function(_):  # noqa: ANN001
    get_settings.cache_clear()


def teardown_function(_):  # noqa: ANN001
    get_settings.cache_clear()


def test_settings_repr_does_not_expose_secret(monkeypatch, caplog):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-WILL-NEVER-APPEAR")
    s = Settings(_env_file=None)
    with caplog.at_level(logging.INFO):
        logging.getLogger("test").info("settings loaded: %r", s)
    blob = caplog.text
    assert "WILL-NEVER-APPEAR" not in blob


def test_safe_dict_is_json_serialisable(monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-x")
    s = Settings(_env_file=None)
    text = json.dumps(s.safe_dict())
    assert REDACTED in text
    assert "sk-x" not in text


def test_provider_describe_hides_key(monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-SECRET")
    monkeypatch.setenv("LLM_PROVIDER", "qwen")
    from core.providers.factory import build_provider

    s = Settings(_env_file=None)
    p = build_provider(settings=s)
    desc = p.describe()
    blob = json.dumps(desc)
    assert "SECRET" not in blob
    assert "sk-" not in blob
