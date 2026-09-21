"""Tool error classification, sanitization, and retry policy.

Pure functions. No sleep, no I/O. The worker loop calls `classify_tool_error()`
on every exception and decides what to do next.

Stage-3 hard boundary (sensitive-error hygiene): the raw exception text, full
URLs (which may embed query credentials / userinfo), and private request bodies
MUST NOT flow into ``errors[]`` strings, the subtask error, logs, or traces. We
emit stable, sanitized diagnostics instead.
"""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass
from typing import Literal

from core.exceptions import (
    ToolError,
    ToolQuotaExceededError,
    ToolTimeoutError,
    TransientToolError,
)

ErrorCategory = Literal[
    "timeout",
    "transient_network",
    "rate_limit",
    "quota_exhausted",
    "auth_permission",
    "parse_error",
    "permanent_fetch",
    "cancelled",
    "unknown",
]

# Upper bound on a honoured Retry-After delay (seconds). A malicious or buggy
# adapter cannot make the worker sleep for an absurdly long time.
MAX_RETRY_AFTER_S = 60.0


def sanitize_url(url: str) -> str:
    """Strip sensitive material from a URL for safe logging/diagnostics.

    Removes the query string, userinfo (user:pass@), and fragment, so a URL like
    ``https://user:pw@host/p?token=SECRET#f`` becomes ``https://host/p``. The
    scheme, host and path are preserved for human diagnosis. Non-URL input is
    returned unchanged (best effort).
    """
    if not url or "://" not in url:
        return url
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return url
    netloc = parts.hostname or parts.netloc
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    cleaned = urllib.parse.urlunsplit(
        (parts.scheme, netloc, parts.path, "", "")  # query & fragment dropped
    )
    return cleaned


@dataclass(frozen=True)
class ToolFailure:
    """Structured, redacted description of a tool failure.

    ``error_code`` is the stable category; ``error_type`` is the exception class
    name (never the message); ``sanitized_locator`` is a credential-free URL;
    ``attempt`` is which attempt this was. No original exception string, no
    private body, no query credential is ever carried here.
    """

    error_code: str
    error_type: str
    sanitized_locator: str = ""
    attempt: int = 0

    def render(self) -> str:
        loc = f" locator={self.sanitized_locator}" if self.sanitized_locator else ""
        return (
            f"error_code={self.error_code} error_type={self.error_type}{loc} attempt={self.attempt}"
        )


class ClassifiedError(Exception):
    """Wraps an original exception with a category + retry hint."""

    def __init__(
        self,
        category: ErrorCategory,
        original: Exception,
        *,
        retry_after_s: float | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(f"{category}: {original}")
        self.category = category
        self.original = original
        # Clamp Retry-After to a sane, non-negative bound so a hostile or buggy
        # adapter cannot induce a multi-hour sleep.
        if retry_after_s is not None:
            if retry_after_s < 0:
                retry_after_s = 0.0
            elif retry_after_s > MAX_RETRY_AFTER_S:
                retry_after_s = MAX_RETRY_AFTER_S
        self.retry_after_s = retry_after_s
        self.retryable = retryable


def classify_tool_error(exc: Exception) -> ClassifiedError:
    """Map an arbitrary exception to a category + retry policy.

    If the exception is already a ClassifiedError (e.g. constructed in a test
    or by a higher layer), pass it through unchanged so its retry_after_s and
    retryable flags are preserved.
    """
    if isinstance(exc, ClassifiedError):
        return exc

    msg = str(exc).lower()

    if isinstance(exc, ToolTimeoutError):
        return ClassifiedError("timeout", exc, retryable=True)
    if isinstance(exc, ToolQuotaExceededError):
        return ClassifiedError("quota_exhausted", exc, retryable=False)
    if isinstance(exc, TransientToolError):
        # 429 / 5xx / connection reset -> transient
        return ClassifiedError("transient_network", exc, retryable=True)

    # Heuristics on message text (adapters that don't subclass cleanly).
    if "429" in msg or "rate limit" in msg or "retry-after" in msg:
        # Prefer an explicit Retry-After carried by the exception (from an HTTP
        # adapter's response header) over a 2.0s default.
        retry_after = getattr(exc, "retry_after_s", None)
        if retry_after is None:
            retry_after = 2.0
        return ClassifiedError("rate_limit", exc, retryable=True, retry_after_s=retry_after)
    if "401" in msg or "403" in msg or "unauthorized" in msg or "forbidden" in msg:
        return ClassifiedError("auth_permission", exc, retryable=False)
    if "timeout" in msg or "timed out" in msg:
        return ClassifiedError("timeout", exc, retryable=True)
    if "cancelled" in msg or "cancel" in msg:
        return ClassifiedError("cancelled", exc, retryable=False)
    # Explicit parentheses make the operator precedence unambiguous:
    # (parse / decode) OR (json AND error).
    if ("parse" in msg or "decode" in msg) or ("json" in msg and "error" in msg):
        return ClassifiedError("parse_error", exc, retryable=False)

    if isinstance(exc, ToolError):
        return ClassifiedError("permanent_fetch", exc, retryable=False)

    return ClassifiedError("unknown", exc, retryable=False)


def backoff_seconds(
    attempt: int, *, base: float = 0.5, factor: float = 2.0, cap: float = 8.0
) -> float:
    """Deterministic exponential backoff. Tests should NOT actually sleep.

    attempt is 0-based. attempt=0 -> base, attempt=1 -> base*factor, ...
    capped at `cap`.
    """
    if attempt < 0:
        attempt = 0
    return min(cap, base * (factor**attempt))


__all__ = [
    "MAX_RETRY_AFTER_S",
    "ClassifiedError",
    "ErrorCategory",
    "ToolFailure",
    "backoff_seconds",
    "classify_tool_error",
    "sanitize_url",
]
