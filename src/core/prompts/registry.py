"""PromptRegistry: render runtime prompts with local / langsmith / hybrid modes.

Modes:
- ``local``: force local manifest, zero network. This is the default and the
  only mode tests use.
- ``langsmith``: force remote. Any failure raises (no fallback).
- ``hybrid``: try remote first; on any failure fall back to local and mark
  the rendered prompt ``source=local_fallback``.

The registry caches remote pulls in memory with a TTL so repeated renders
don't hit the network on every call.

Variable contract:
- Rendering requires every ``required`` variable in the spec. Missing ones
  raise ``ValueError``.
- Extra variables (not in the spec) are rejected with ``ValueError`` unless
  ``allow_extra=True``.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Literal

from .langsmith_repo import LangSmithPromptRepository, PromptRepoError
from .manifest import load_lock, load_manifest
from .spec import PromptSpec, RenderedPrompt

log = logging.getLogger(__name__)

PromptMode = Literal["local", "langsmith", "hybrid"]


class PromptRenderError(RuntimeError):
    """Raised when a prompt cannot be rendered (missing var, unknown name)."""


@dataclass
class _CacheEntry:
    spec: PromptSpec
    fetched_at: float
    commit: str | None
    tag: str | None


class PromptRegistry:
    """Render prompts from the local manifest or a LangSmith repo."""

    def __init__(
        self,
        *,
        mode: PromptMode = "local",
        repo: LangSmithPromptRepository | None = None,
        cache_ttl: float = 300.0,
        manifest: dict[str, PromptSpec] | None = None,
    ) -> None:
        """Construct the registry.

        Args:
            mode: local / langsmith / hybrid.
            repo: LangSmith repo (required when mode != local). Injected for
                tests; production uses ``build_default_registry()``.
            cache_ttl: remote-pull cache TTL in seconds.
            manifest: optional override of local manifest entries.
        """
        self._mode = mode
        self._repo = repo
        self._ttl = cache_ttl
        self._local: dict[str, PromptSpec] = (
            manifest if manifest is not None else self._load_local()
        )
        self._cache: dict[str, _CacheEntry] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _load_local() -> dict[str, PromptSpec]:
        entries = load_manifest()
        return {name: e.to_spec() for name, e in entries.items()}

    # -- introspection --------------------------------------------------------
    @property
    def mode(self) -> PromptMode:
        return self._mode

    def all_specs(self) -> list[PromptSpec]:
        """List all locally known specs (manifest names)."""
        return list(self._local.values())

    def get_local(self, name: str) -> PromptSpec:
        """Return the local spec for ``name`` (raises if unknown)."""
        if name not in self._local:
            raise PromptRenderError(f"unknown prompt: {name!r}")
        return self._local[name]

    # -- resolution -----------------------------------------------------------
    def _resolve(self, name: str) -> tuple[PromptSpec, str, str | None, str | None]:
        """Return (spec, source, commit, tag)."""
        if self._mode == "local":
            spec = self.get_local(name)
            return spec, "local", None, None

        # Try cache first (hybrid or langsmith).
        with self._lock:
            cached = self._cache.get(name)
            if cached and (time.time() - cached.fetched_at) < self._ttl:
                return cached.spec, "langsmith", cached.commit, cached.tag

        # Cold pull. Pin to manifest.lock commit when available.
        try:
            assert self._repo is not None  # noqa: S101
            lock = load_lock()
            pinned_commit: str | None = None
            if name in lock:
                pinned_commit = lock[name].commit_hash
            spec = self._repo.pull(name, commit_hash=pinned_commit)
            with self._lock:
                self._cache[name] = _CacheEntry(
                    spec=spec,
                    fetched_at=time.time(),
                    commit=pinned_commit,
                    tag=None,
                )
            return spec, "langsmith", pinned_commit, None
        except PromptRepoError as e:
            if self._mode == "hybrid":
                log.info("prompt_hub_fallback name=%s reason=%s", name, type(e).__name__)
                spec = self.get_local(name)
                return spec, "local_fallback", None, None
            raise PromptRenderError(f"langsmith mode failed for {name}: {e}") from e

    # -- rendering ------------------------------------------------------------
    def render(self, name: str, **variables: Any) -> RenderedPrompt:
        """Render ``name`` with ``variables``.

        Raises ``PromptRenderError`` on unknown name, missing required vars,
        or (by default) extra vars.
        """
        spec, source, commit, tag = self._resolve(name)
        missing = [v for v in spec.variables if v not in variables]
        if missing:
            raise PromptRenderError(
                f"prompt {name!r} missing required variables: {sorted(missing)}"
            )
        extra = [k for k in variables if k not in spec.variables]
        if extra:
            raise PromptRenderError(f"prompt {name!r} got unexpected variables: {sorted(extra)}")
        # Render each message template.
        rendered_msgs: list[tuple[str, str]] = []
        for role, template in spec.messages:
            try:
                content = template.format(**{k: variables[k] for k in spec.variables})
            except KeyError as e:
                raise PromptRenderError(f"prompt {name!r} template missing var {e!s}") from e
            rendered_msgs.append((role, content))
        return RenderedPrompt(
            name=name,
            source=source,  # type: ignore[arg-type]
            identifier=name,
            commit=commit,
            tag=tag,
            variables=tuple(spec.variables),
            messages=tuple(rendered_msgs),
        )

    # -- CLI helpers ----------------------------------------------------------
    def validate(self) -> list[str]:
        """Validate the local manifest. Returns a list of error strings (empty = OK)."""
        errors: list[str] = []
        for name, spec in self._local.items():
            if not spec.messages:
                errors.append(f"{name}: no messages")
            # Try rendering with dummy values to catch template typos.
            dummy = {v: f"__{v}__" for v in spec.variables}
            try:
                for _role, tpl in spec.messages:
                    tpl.format(**dummy)
            except Exception as e:  # noqa: BLE001
                errors.append(f"{name}: render failed: {e}")
        return errors

    def cache_clear(self) -> None:
        """Drop the remote-pull cache (tests use this)."""
        with self._lock:
            self._cache.clear()

    # -- async preload (Stage4 prompt timeout) -------------------------------
    async def apreload(self, *, timeout: float | None = None) -> None:
        """Warm the remote prompt cache OFF the event loop.

        ``build_default_registry`` used to call ``repo.pull`` synchronously in
        ``LLMFactExtractor.__init__``, blocking the event loop. This method
        runs the cold-pull on a worker thread with ``asyncio.wait_for`` so a
        hung LangSmith cannot freeze the runner. Local mode is a no-op.
        """
        if self._mode == "local" or self._repo is None:
            return
        names = [s.name for s in self._local.values()]
        ttl = timeout if timeout is not None else self._ttl
        import asyncio

        # Pin every preload pull to the manifest.lock commit when present.
        lock = load_lock()

        async def _pull_one(name: str) -> None:
            try:
                assert self._repo is not None  # noqa: S101
                pinned_commit: str | None = None
                if name in lock:
                    pinned_commit = lock[name].commit_hash
                spec = await asyncio.wait_for(
                    asyncio.to_thread(self._repo.pull, name, commit_hash=pinned_commit),
                    timeout=ttl,
                )
                with self._lock:
                    self._cache[name] = _CacheEntry(
                        spec=spec,
                        fetched_at=time.time(),
                        commit=pinned_commit,
                        tag=None,
                    )
            except (TimeoutError, Exception) as e:  # noqa: BLE001
                log.info("prompt_preload_skip name=%s reason=%s", name, type(e).__name__)

        await asyncio.gather(*(_pull_one(n) for n in names))


# -- factory -----------------------------------------------------------------

_default_registry: PromptRegistry | None = None
_default_lock = threading.Lock()


def build_default_registry(*, force_local: bool = False) -> PromptRegistry:
    """Build a registry from current Settings.

    - If ``force_local`` or no LangSmith key is configured -> local mode.
    - Otherwise -> mode from ``PROMPT_SOURCE`` (local / langsmith / hybrid).
    """
    from ..config import get_settings

    s = get_settings()
    if force_local or not s.has_langsmith_key:
        return PromptRegistry(mode="local")
    mode: PromptMode = s.prompt_source
    if mode == "local":
        return PromptRegistry(mode="local")
    repo = LangSmithPromptRepository(
        api_key=s.langchain_api_key.get_secret_value(),
        timeout=s.langsmith_prompt_timeout,
    )
    return PromptRegistry(mode=mode, repo=repo, cache_ttl=s.prompt_cache_ttl)


def get_default_registry() -> PromptRegistry:
    """Cached singleton registry. Tests call ``reset_default_registry()``."""
    global _default_registry  # noqa: PLW0603
    with _default_lock:
        if _default_registry is None:
            _default_registry = build_default_registry()
        return _default_registry


def reset_default_registry() -> None:
    """Drop the cached singleton (called by conftest / tests)."""
    global _default_registry  # noqa: PLW0603
    with _default_lock:
        _default_registry = None


__all__ = [
    "PromptMode",
    "PromptRegistry",
    "PromptRenderError",
    "build_default_registry",
    "get_default_registry",
    "reset_default_registry",
]
