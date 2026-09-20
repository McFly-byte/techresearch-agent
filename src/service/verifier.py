"""CitationVerifier: re-fetch cited sources and run 3-way NLI.

Stage 4 P0 rewrites:
- Async NLI contract: the verifier awaits every NLI call. A sync double is
  wrapped so it is awaitable; an async double is awaited directly. No
  coroutine objects ever land in ``verdicts``.
- Prompt hosting: the system + user NLI templates come from the Prompt Hub
  manifest (``citation_verifier_system`` / ``citation_verifier_user``). No
  hardcoded prompt constant in source.
- Relevant evidence window: instead of always ``doc.content[:800]``, we locate
  the claim's content words inside the body and return the surrounding window.
  When no keyword lands, the verdict is neutral (we never rely on the first
  paragraph). Each evidence record keeps locator / offset / hash / model.
- Conflict marking: when one cited source entails and another contradicts, the
  aggregate is flagged ``conflict=True`` instead of silently picking one.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from core.prompts import PromptRegistry, get_default_registry, load_lock
from core.providers.base import BaseLLMProvider, Message
from core.tracing import TracingContext, noop_tracing
from core.usage import usage_stage
from domain.models import Citation
from domain.verification import (
    Claim,
    Verdict,
    VerificationEvidence,
    VerificationResult,
)

log = logging.getLogger(__name__)

_MAX_EXCERPT_CHARS = 1200
_WINDOW_AROUND_HIT = 400

# Generic filler words that must NOT alone decide the evidence window. A single
# hit of one of these (e.g. "system", "support") is not enough relevance.
_GENERIC_WORDS: frozenset[str] = frozenset(
    {
        "system",
        "systems",
        "support",
        "supported",
        "supporting",
        "using",
        "this",
        "that",
        "these",
        "those",
        "with",
        "from",
        "have",
        "been",
        "will",
        "would",
        "could",
        "should",
        "their",
        "there",
        "which",
        "while",
        "where",
        "when",
        "what",
        "also",
        "only",
        "just",
        "very",
        "more",
        "most",
        "other",
        "others",
        "than",
        "then",
        "them",
        "they",
        "were",
        "after",
        "before",
        "between",
    }
)


class NLIProviderError(RuntimeError):
    """Stable, redacted error raised when the NLI LLM call itself fails.

    Carries a machine-readable ``error_code`` (never the raw exception body,
    which may contain prompts / network details). The CitationVerifier catches
    this per-citation, records it on the Evidence, and excludes it from the
    verdicts aggregate — it must NEVER be washed into ``"neutral"``.
    """

    def __init__(
        self,
        error_code: str,
        *,
        error_type: str = "",
        model_id: str = "",
    ) -> None:
        super().__init__(error_code)
        self.error_code = error_code
        self.error_type = error_type
        self.model_id = model_id


class NLIParseError(NLIProviderError):
    """Stable error raised when the NLM response cannot be parsed into a verdict."""


@runtime_checkable
class AsyncNLI(Protocol):
    """Contract for an NLI judge. Implementations must be awaitable."""

    async def classify(self, claim: str, evidence: str) -> Verdict: ...


def _to_awaitable_nli(nli: Any) -> AsyncNLI:
    """Wrap any callable into an awaitable AsyncNLI exposing ``.classify``.

    - A duck-typed AsyncNLI (has ``.classify``) is used as-is.
    - A plain async function is wrapped so ``.classify`` works.
    - A sync function is wrapped and run on the default executor.
    """
    if hasattr(nli, "classify") and callable(nli.classify):
        return nli

    # Plain async function: wrap it.
    if inspect.iscoroutinefunction(nli):

        class _AsyncFnWrapper:
            def __init__(self, fn: Any) -> None:
                self._fn = fn

            async def classify(self, claim: str, evidence: str) -> Verdict:
                return await self._fn(claim, evidence)

            def __call__(self, claim: str, evidence: str) -> Any:
                return self.classify(claim, evidence)

        return _AsyncFnWrapper(nli)

    # Sync function: wrap it.
    class _SyncWrapper:
        def __init__(self, fn: Callable[[str, str], Verdict]) -> None:
            self._fn = fn

        async def classify(self, claim: str, evidence: str) -> Verdict:
            return self._fn(claim, evidence)

        def __call__(self, claim: str, evidence: str) -> Any:
            return self.classify(claim, evidence)

    return _SyncWrapper(nli)


def _heuristic_nli(claim: str, excerpt: str) -> Verdict:
    """Tiny deterministic NLI for tests.

    entailment: most content words of claim appear in excerpt.
    contradiction: "not X" in claim but "X" (without not) in excerpt, or vice versa.
    neutral: otherwise.
    """
    c_low = claim.lower()
    e_low = excerpt.lower()

    # Contradiction: negation mismatch.
    if " not " in f" {c_low} " and any(
        word in e_low for word in c_low.replace(" not ", " ").split() if len(word) > 3
    ):
        return "contradiction"

    words = [w for w in c_low.replace(",", " ").replace(".", " ").split() if len(w) > 3]
    if not words:
        return "neutral"
    hits = sum(1 for w in words if w in e_low)
    ratio = hits / len(words)
    if ratio >= 0.5:
        return "entailment"
    return "neutral"


def _content_words(text: str) -> list[str]:
    """Claim content words used for relevance scoring.

    Drops short tokens (<=3 chars) and generic filler words so a single
    incidental word like "system" or "support" cannot pick the window.
    """
    return [
        w for w in re.split(r"[^a-z0-9]+", text.lower()) if len(w) > 3 and w not in _GENERIC_WORDS
    ]


def _find_relevant_window(
    body: str, claim: str, *, max_chars: int = _MAX_EXCERPT_CHARS
) -> tuple[str, int, dict[str, Any]]:
    """Return ``(excerpt, char_offset, meta)`` for the most claim-relevant window.

    ``meta`` carries:
      - ``found``: True only when at least one claim content word lands in the
        body. When False the caller MUST short-circuit to neutral and MUST NOT
        call the NLI judge (this is the ``no_keyword_forced_entail`` fix).
      - ``score``: number of distinct claim content words present in the window.
      - ``matched``: the distinct claim content words actually found.

    Strategy: enumerate candidate windows centered on every hit position and
    pick the one covering the MOST distinct claim content words (best coverage,
    NOT the earliest incidental generic hit). Ties break by earliest start so
    results are deterministic.
    """
    if not body:
        return "", 0, {"found": False, "score": 0.0, "matched": []}
    words = _content_words(claim)
    if not words:
        return "", 0, {"found": False, "score": 0.0, "matched": []}
    body_low = body.lower()
    # Map each claim content word -> list of hit positions in the body.
    hits: dict[str, list[int]] = {}
    for w in words:
        positions: list[int] = []
        start = 0
        while True:
            idx = body_low.find(w, start)
            if idx == -1:
                break
            positions.append(idx)
            start = idx + 1
        if positions:
            hits[w] = positions
    if not hits:
        # No claim content word anywhere -> no relevant evidence.
        return "", 0, {"found": False, "score": 0.0, "matched": []}

    matched = sorted(hits)
    # Candidate centers: every hit position. Pick the window that covers the
    # most distinct matched words.
    best_window_start = 0
    best_covered = -1
    best_excerpt = body[:max_chars]
    best_offset = 0
    for positions in hits.values():
        for center in positions:
            start = max(0, center - _WINDOW_AROUND_HIT)
            end = min(len(body), start + max_chars)
            start = max(0, end - max_chars)
            window_low = body_low[start:end]
            covered = sum(1 for w in matched if w in window_low)
            if covered > best_covered or (covered == best_covered and start < best_window_start):
                best_covered = covered
                best_window_start = start
                best_offset = start
                best_excerpt = body[start:end]
    return (
        best_excerpt,
        best_offset,
        {
            "found": True,
            "score": float(best_covered),
            "matched": matched,
        },
    )


class CitationVerifier:
    name = "heuristic"

    def __init__(
        self,
        fetcher: object | None = None,
        nli: Any = None,
        *,
        excerpt_window_chars: int = _MAX_EXCERPT_CHARS,
    ) -> None:
        self._fetcher = fetcher
        self._nli = _to_awaitable_nli(nli or _heuristic_nli)
        self._window = excerpt_window_chars

    async def verify(
        self,
        claim: Claim,
        citations: list[Citation],
        *,
        round_label: str = "initial",
    ) -> VerificationResult:
        by_id = {c.citation_id: c for c in citations}
        evidence: list[VerificationEvidence] = []
        verdicts: list[Verdict] = []
        fetch_failed = False
        nli_failed_any = False
        n_fetched_ok = 0

        for cid in claim.citation_ids:
            c = by_id.get(cid)
            if c is None:
                evidence.append(
                    VerificationEvidence(
                        citation_id=cid,
                        refetch_ok=False,
                        excerpt="",
                        fetched_at="",
                        nli_error="unknown_citation_id",
                    )
                )
                fetch_failed = True
                continue

            try:
                doc = await self._fetcher.fetch(c.locator, citation_id=c.citation_id)  # type: ignore[union-attr]
            except Exception:
                fetch_failed = True
                evidence.append(
                    VerificationEvidence(
                        citation_id=cid,
                        refetch_ok=False,
                        fetched_at="",
                        source_locator=c.locator,
                        nli_error="fetch_exception",
                    )
                )
                continue

            if not doc.fetched_ok:
                fetch_failed = True
                evidence.append(
                    VerificationEvidence(
                        citation_id=cid,
                        refetch_ok=False,
                        excerpt=doc.error or "",
                        fetched_at=doc.citation.fetched_at,
                        source_locator=c.locator,
                        nli_error="fetch_not_ok",
                    )
                )
                continue

            n_fetched_ok += 1
            excerpt, offset, rel = _find_relevant_window(
                doc.content, claim.claim_text, max_chars=self._window
            )
            h = hashlib.sha256(excerpt.encode("utf-8")).hexdigest()[:12]

            # P0#2: no relevant window -> forced neutral, DO NOT call NLI.
            if not rel["found"]:
                evidence.append(
                    VerificationEvidence(
                        citation_id=cid,
                        refetch_ok=True,
                        excerpt=excerpt,
                        fetched_at=doc.citation.fetched_at,
                        source_locator=c.locator,
                        char_offset=offset,
                        content_hash=h,
                        verdict="neutral",
                        nli_error="no_relevant_evidence",
                    )
                )
                verdicts.append("neutral")
                continue

            # P0#1: await the NLI call. Provider / parse failures raise a stable
            # NLIProviderError; we record it on the evidence and EXCLUDE it from
            # the verdicts aggregate (never wash it into "neutral").
            try:
                v: Verdict = await self._nli.classify(claim.claim_text, excerpt)
                err_code = ""
                per_verdict: Verdict | None = v
            except NLIProviderError as e:
                log.warning(
                    "nli_call_failed error_code=%s error_type=%s", e.error_code, e.error_type
                )
                err_code = e.error_code
                per_verdict = None
                nli_failed_any = True
            except Exception as e:  # noqa: BLE001
                log.warning("nli_call_failed error_type=%s", type(e).__name__)
                err_code = "nli_provider_failure"
                per_verdict = None
                nli_failed_any = True

            evidence.append(
                VerificationEvidence(
                    citation_id=cid,
                    refetch_ok=True,
                    excerpt=excerpt,
                    fetched_at=doc.citation.fetched_at,
                    source_locator=c.locator,
                    char_offset=offset,
                    content_hash=h,
                    verdict=per_verdict,
                    nli_error=err_code,
                )
            )
            if per_verdict is not None:
                verdicts.append(per_verdict)

        # All citations failed to fetch -> refetch_failed (verdict None).
        if fetch_failed and not verdicts and n_fetched_ok == 0:
            return VerificationResult(
                claim_id=claim.claim_id,
                verdict=None,
                evidence=evidence,
                reason="all refetches failed",
                verifier_version=f"{self.name}-v1",
                verified_at=datetime.now(UTC).isoformat(),
                round=round_label,
            )

        # P0#1: every fetched citation's NLI failed -> distinct nli_failed state,
        # NOT refetch_failed, NOT a washed neutral.
        if not verdicts and nli_failed_any:
            return VerificationResult(
                claim_id=claim.claim_id,
                verdict=None,
                evidence=evidence,
                reason="nli_failed: all NLI calls failed",
                verifier_version=f"{self.name}-v1",
                verified_at=datetime.now(UTC).isoformat(),
                round=round_label,
            )

        # Aggregate: explicit conflict marking.
        has_entailment = "entailment" in verdicts
        has_contradiction = "contradiction" in verdicts
        conflict = has_entailment and has_contradiction
        if conflict:
            final: Verdict = "neutral"  # unresolved; surfaced as conflict
            reason = (
                f"conflict: {len(verdicts)} sources disagree "
                f"({verdicts.count('entailment')} entail vs "
                f"{verdicts.count('contradiction')} contradict)"
            )
        elif has_contradiction:
            final = "contradiction"
            reason = f"aggregated over {len(verdicts)} refetched sources (contradiction)"
        elif has_entailment:
            final = "entailment"
            reason = f"aggregated over {len(verdicts)} refetched sources (entailment)"
        else:
            final = "neutral"
            reason = f"aggregated over {len(verdicts)} refetched sources (neutral)"

        return VerificationResult(
            claim_id=claim.claim_id,
            verdict=final,
            evidence=evidence,
            reason=reason,
            verifier_version=f"{self.name}-v1",
            verified_at=datetime.now(UTC).isoformat(),
            round=round_label,
            conflict=conflict,
        )


# --- LLM-backed independent NLI (Stage 4) ------------------------------------


class LLMNLI:
    """Independent NLI provider backed by an injectable LLM.

    Implements the async ``AsyncNLI.classify`` contract. The system + user
    prompts are rendered from the Prompt Hub manifest, never hardcoded.
    """

    def __init__(
        self,
        llm: BaseLLMProvider,
        *,
        registry: PromptRegistry | None = None,
        tracing: TracingContext | None = None,
    ) -> None:
        self._llm = llm
        self._registry = registry or get_default_registry()
        # Render the templates once (local mode is zero-network).
        self._sys = self._registry.render("citation_verifier_system").system_text()
        # LangSmith prompt association: each NLI call uses BOTH the
        # citation_verifier_system template and the citation_verifier_user
        # template, so we wrap the single provider call in TWO nested LLM-type
        # runs (one per prompt repo). Commit hashes come from manifest.lock.json.
        self._tracing = tracing or noop_tracing()
        self._prompt_commits: dict[str, str] = {}
        try:
            for name, entry in load_lock().items():
                self._prompt_commits[name] = entry.commit_hash
        except Exception:  # noqa: BLE001
            self._prompt_commits = {}

    def __call__(self, claim: str, evidence: str) -> Any:
        """Allow both ``nli(claim, evidence)`` (awaitable) and
        ``await nli.classify(claim, evidence)`` call shapes."""
        return self.classify(claim, evidence)

    async def classify(self, claim: str, evidence: str) -> Verdict:
        rendered = self._registry.render("citation_verifier_user", claim=claim, evidence=evidence)
        messages = [Message(role="system", content=self._sys)]
        for _role, content in rendered.messages:
            messages.append(Message(role="user", content=content))
        try:
            # Wrap the single NLI call in two nested LLM-type runs: the outer
            # attributed to citation_verifier_system, the inner to
            # citation_verifier_user. Each carries its own lc_hub_repo + commit
            # metadata so LangSmith associates BOTH prompts to the Application.
            sys_commit = self._prompt_commits.get("citation_verifier_system", "")
            user_commit = self._prompt_commits.get("citation_verifier_user", "")
            with (
                usage_stage("verifier"),
                self._tracing.llm_prompt_span("citation_verifier_system", sys_commit),
                self._tracing.llm_prompt_span("citation_verifier_user", user_commit),
            ):
                resp = await self._llm.acomplete(messages)
        except NLIProviderError:
            # Re-raise our own stable errors unchanged (they already carry the
            # redacted code); the wrapping context managers close cleanly.
            raise
        except Exception as e:  # noqa: BLE001
            # P0#1: raise a stable, redacted error. Never return "neutral" (that
            # washes a provider outage into a plausible verdict), never log the
            # exception body (may contain prompts / network details).
            raise NLIProviderError(
                "nli_provider_failure",
                error_type=type(e).__name__,
                model_id=getattr(self._llm, "model_id", ""),
            ) from e
        cleaned = resp.text.strip()
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
        try:
            data = json.loads(cleaned)
        except (json.JSONDecodeError, ValueError) as e:
            raise NLIParseError(
                "nli_parse_error",
                error_type=type(e).__name__,
                model_id=getattr(self._llm, "model_id", ""),
            ) from e
        v = str(data.get("verdict", "")).lower()
        if v in ("entailment", "contradiction", "neutral"):
            return v  # type: ignore[return-value]
        raise NLIParseError(
            "nli_invalid_verdict",
            error_type="invalid_verdict",
            model_id=getattr(self._llm, "model_id", ""),
        )


__all__ = [
    "AsyncNLI",
    "CitationVerifier",
    "LLMNLI",
    "NLIParseError",
    "NLIProviderError",
    "_heuristic_nli",
]
