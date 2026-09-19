"""Classify a failed eval result into one of five buckets.

Buckets (per phase 5 spec):
- retrieval_failure: search returned nothing / fetch failed
- reasoning_failure: LLM raised or returned unparseable
- hallucination: answer looks plausible but no citations / unsupported claims
- freshness: question asks for time-sensitive info that we couldn't verify
- tool_failure: an adapter raised (timeout, 429, 5xx)
"""

from __future__ import annotations

from evals.adapter import EvalResult

FailureCategory = str


def classify_failure(result: EvalResult) -> str:
    """Return one of the five buckets. Empty string if status is not failed."""
    if result.status != "failed":
        return ""
    err = (result.error or "").lower()
    if any(
        k in err
        for k in (
            "timeout",
            "timed out",
            "429",
            "rate limit",
            "5xx",
            "500",
            "providererror",
            "provider_error",
            "provider failure",
        )
    ):
        return "tool_failure"
    if any(k in err for k in ("fetch", "refetch", "citation", "url")):
        return "retrieval_failure"
    if any(k in err for k in ("parse", "json", "unparseable")):
        return "reasoning_failure"
    if any(k in err for k in ("fresh", "outdated", "2024", "2025")):
        return "freshness"
    if any(k in err for k in ("hallucinat", "unsupported", "made up")):
        return "hallucination"
    return "unknown"


__all__ = ["FailureCategory", "classify_failure"]
