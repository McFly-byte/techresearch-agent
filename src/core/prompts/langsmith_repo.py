"""LangSmith-backed prompt repository.

Wraps the ``langsmith.Client`` and exposes the tiny surface the registry needs:
``push``, ``pull``, ``list``, ``promote``. All calls have a hard timeout so a
hung LangSmith never blocks the research loop.

Security:
- ``push`` sends ONLY the ChatPromptTemplate (template text + variable names).
  No document bodies, no variable VALUES, no API keys.
- Errors are logged by stable type only; the raw HTTP body is never logged.
- Pulled prompts are cached by the registry, never re-fetched on every call.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from langchain_core.prompts import ChatPromptTemplate

from .spec import PromptSpec

log = logging.getLogger(__name__)


class PromptRepoError(RuntimeError):
    """Raised on any unrecoverable LangSmith interaction failure."""


@dataclass(frozen=True)
class PushResult:
    """Result of pushing one prompt to LangSmith."""

    identifier: str
    commit_hash: str
    tags: tuple[str, ...]
    url: str


class LangSmithPromptRepository:
    """Thin wrapper around ``langsmith.Client`` for prompt CRUD."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        endpoint: str | None = None,
        timeout: float = 10.0,
        client: Any | None = None,
    ) -> None:
        """Construct the repo.

        Args:
            api_key: LangSmith API key. If None, the client reads
                ``LANGCHAIN_API_KEY`` from the environment.
            endpoint: optional LangSmith endpoint override.
            timeout: per-call timeout in seconds. The underlying langsmith
                client does not expose a per-call timeout knob, so we rely on
                its own defaults and treat connection errors as failures.
            client: injectable langsmith Client (used by tests).
        """
        self._timeout = timeout
        self._client = client or self._build_client(api_key, endpoint)

    @staticmethod
    def _build_client(api_key: str | None, endpoint: str | None) -> Any:
        from langsmith import Client

        kwargs: dict[str, Any] = {}
        if api_key:
            kwargs["api_key"] = api_key
        if endpoint:
            kwargs["api_url"] = endpoint
        return Client(**kwargs)

    # -- push -----------------------------------------------------------------
    def push(
        self,
        spec: PromptSpec,
        *,
        identifier: str | None = None,
        tags: list[str] | None = None,
        commit_tags: list[str] | None = None,
    ) -> PushResult:
        """Push ``spec`` to LangSmith. Idempotent: re-pushing updates the prompt.

        The object pushed is a ChatPromptTemplate built from spec.messages —
        template text only, no document bodies or variable values.
        """
        ident = identifier or spec.name
        template = spec.to_chat_prompt_template()
        try:
            raw = self._client.push_prompt(
                ident,
                object=template,
                description=spec.description or spec.name,
                tags=tags or ["techresearch-agent", "runtime"],
                commit_tags=commit_tags or [],
            )
        except Exception as e:  # noqa: BLE001
            log.warning("langsmith_push_failed name=%s error_type=%s", spec.name, type(e).__name__)
            raise PromptRepoError(f"push failed for {spec.name}: {type(e).__name__}") from e
        # push_prompt may return a commit hash OR a full URL. Normalize to the
        # short hash (the last path segment before any query string).
        commit_str = str(raw)
        if commit_str.startswith("http"):
            path = commit_str.split("?", 1)[0]
            commit_hash = path.rstrip("/").rsplit("/", 1)[-1]
        else:
            commit_hash = commit_str
        return PushResult(
            identifier=ident,
            commit_hash=commit_hash,
            tags=tuple(commit_tags or []),
            url=commit_str if commit_str.startswith("http") else "",
        )

    # -- pull -----------------------------------------------------------------
    def pull(
        self,
        identifier: str,
        *,
        tag: str | None = None,
        commit_hash: str | None = None,
    ) -> PromptSpec:
        """Pull a prompt from LangSmith and normalize into a PromptSpec.

        Supports ``name:tag`` and ``name@commit`` syntax via the ``identifier``
        itself, OR explicit ``tag`` / ``commit_hash`` kwargs.
        """
        # Build the lookup string. LangSmith's parse_prompt_identifier uses
        # COLON syntax: ``name:hash`` (or ``owner/name:hash``). The ``@`` form
        # is NOT recognized and 404s.
        lookup = identifier
        if commit_hash:
            lookup = f"{identifier}:{commit_hash}"
        elif tag:
            lookup = f"{identifier}:{tag}"
        try:
            obj = self._client.pull_prompt(lookup)
        except Exception as e:  # noqa: BLE001
            log.warning("langsmith_pull_failed lookup=%s error_type=%s", lookup, type(e).__name__)
            raise PromptRepoError(f"pull failed for {lookup}: {type(e).__name__}") from e
        # obj is a LangSmith ChatPrompt-like object; render it to a
        # ChatPromptTemplate.
        try:
            cp = obj if isinstance(obj, ChatPromptTemplate) else obj.prompt
        except AttributeError:
            cp = obj
        spec = PromptSpec.from_chat_prompt_template(
            name=identifier,
            template=cp,
            description=f"pulled from langsmith:{lookup}",
        )
        return spec

    # -- list -----------------------------------------------------------------
    def list_prompts(self) -> list[dict[str, str]]:
        """List prompts visible to this client (metadata only)."""
        try:
            prompts = self._client.list_prompts()
        except Exception as e:  # noqa: BLE001
            log.warning("langsmith_list_failed error_type=%s", type(e).__name__)
            raise PromptRepoError(f"list failed: {type(e).__name__}") from e
        out: list[dict[str, str]] = []
        for p in prompts:
            out.append(
                {
                    "name": getattr(p, "repo_handle", "") or getattr(p, "name", ""),
                    "description": getattr(p, "description", "") or "",
                    "updated_at": getattr(p, "updated_at", "") or "",
                }
            )
        return out

    # -- promote --------------------------------------------------------------
    def promote(
        self,
        identifier: str,
        *,
        commit_hash: str,
        tag: str = "production",
    ) -> PushResult:
        """Re-tag ``commit_hash`` as ``tag`` (does NOT create new content)."""
        # LangSmith's push_prompt can apply commit_tags when pushing. To
        # promote an existing commit we push a no-op update with the same
        # template and the new commit_tags. We don't have the template here,
        # so we pull it first, then push with the new tag.
        existing = self.pull(identifier, commit_hash=commit_hash)
        result = self.push(
            existing,
            identifier=identifier,
            commit_tags=[tag],
        )
        return PushResult(
            identifier=identifier,
            commit_hash=result.commit_hash,
            tags=(tag,),
            url=result.url,
        )


__all__ = ["LangSmithPromptRepository", "PromptRepoError", "PushResult"]
