"""Unified exception hierarchy for TechResearch Agent.

All errors raised by business modules should derive from TRAError so the API
layer can map them to a stable HTTP shape without leaking internals.
"""

from __future__ import annotations

from typing import Any


class TRAError(Exception):
    """Base class for all project-specific errors."""

    http_status: int = 500
    error_code: str = "internal_error"

    def __init__(self, message: str = "", *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message or self.__class__.__doc__ or self.error_code
        self.details: dict[str, Any] = dict(details or {})

    def to_public_dict(self) -> dict[str, Any]:
        """Shape safe to return to API clients. Never includes stack traces."""
        return {
            "error": self.error_code,
            "message": self.message,
            "details": self.details,
        }


class ConfigurationError(TRAError, ValueError):
    """Configuration is missing or invalid.

    Subclasses ``ValueError`` so callers that expect a plain ``ValueError`` for
    invalid construction (e.g. ``BudgetManager(-1)``) are compatible.
    """

    http_status = 500
    error_code = "configuration_error"


class ProviderError(TRAError):
    """Model provider call failed."""

    http_status = 502
    error_code = "provider_error"


class ProviderNotConfiguredError(ProviderError):
    """A provider was requested but its API key is missing."""

    http_status = 400
    error_code = "provider_not_configured"


class ToolError(TRAError):
    """A tool / adapter call failed (search, fetch, parse, ...)."""

    http_status = 502
    error_code = "tool_error"


class ToolTimeoutError(ToolError):
    """A tool call timed out."""

    http_status = 504
    error_code = "tool_timeout"


class ToolQuotaExceededError(ToolError):
    """A tool account has exhausted its request or credit allowance."""

    http_status = 429
    error_code = "tool_quota_exhausted"


class ToolAuthenticationError(ToolError):
    """A tool credential was rejected or lacks permission."""

    http_status = 401
    error_code = "tool_authentication_failed"


class TransientToolError(ToolError):
    """A retryable tool failure (5xx, rate limit, network blip)."""

    error_code = "tool_transient"


class HealthCheckError(TRAError):
    """Raised when a dependency used by the /health probe is unhealthy."""

    http_status = 503
    error_code = "health_check_failed"


class InvalidPlanError(TRAError):
    """The Planner produced a structurally invalid sub-task plan.

    Raised by ``graph.state.validate_plan`` (wired into the graph right after
    the planner node) so a bad plan fails fast with a structured diagnostic
    instead of degrading into a graph recursion-limit blow-up (e.g. an unknown
    dependency or a dependency cycle). ``details`` carries the list of
    human-readable diagnostics.
    """

    http_status = 500
    error_code = "invalid_plan"


class BudgetExceededError(TRAError):
    """A pre-call token reservation could not be granted.

    Raised by ``BudgetManager.reserve`` when the remaining global (or per-task)
    token budget cannot cover even the worst-case estimate of the next LLM call.
    This is a HARD pre-call gate: the call is refused before it happens, so a
    task can never overshoot the hard budget by issuing a new expensive call.
    """

    http_status = 409
    error_code = "budget_exceeded"


__all__ = [
    "BudgetExceededError",
    "ConfigurationError",
    "HealthCheckError",
    "InvalidPlanError",
    "ProviderError",
    "ProviderNotConfiguredError",
    "ToolError",
    "ToolAuthenticationError",
    "ToolQuotaExceededError",
    "ToolTimeoutError",
    "TRAError",
    "TransientToolError",
]
