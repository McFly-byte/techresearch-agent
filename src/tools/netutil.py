"""URL / path validation shared by all fetchers."""

from __future__ import annotations

from urllib.parse import urlparse

from core.exceptions import ToolError


def is_http_url(value: str) -> bool:
    try:
        p = urlparse(value)
    except ValueError:
        return False
    return p.scheme in {"http", "https"} and bool(p.netloc)


def require_http_url(value: str) -> str:
    if not is_http_url(value):
        raise ToolError(f"not an http(s) URL: {value!r}")
    return value


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + f"\n...[truncated {len(text) - limit} chars]"


__all__ = ["is_http_url", "require_http_url", "truncate"]
