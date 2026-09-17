"""Fact extraction.

Phase 1 ships a deterministic HeuristicFactExtractor: it splits each fetched
document's content into sentences and emits one Fact per non-trivial sentence,
with `source_citation_ids=[doc.citation_id]`. This is offline, free, and —
critically for the citation-traceability contract — every Fact is a verbatim
substring of its source, so the writer can never fabricate support.

Phase 0-1 P0 correction adds ``LLMFactExtractor``: an optional LLM-backed
extraction path that calls a real provider to extract structured facts. It
preserves the same "every fact cites a valid source_citation_id" invariant, and
the stage-0-1 boundary corrections add:

- Strict extractive support: every returned fact's claim MUST be a locatable
  substring of at least one of its cited source bodies. Facts that cite a known
  id but are not actually supported by the source are rejected (not silently
  kept).
- Root schema validation: the LLM response root MUST be a JSON object carrying a
  list under ``facts``. ``[]`` / ``null`` / scalar / wrong field type trigger
  ONE controlled repair request; failure after repair falls back to heuristic.
- Total input bound: the exact message contents actually sent to the provider
  (system + user [+ assistant echo + repair]) never exceed
  ``MAX_TOTAL_INPUT_CHARS``. Oversized inputs are truncated deterministically by
  dropping whole documents in order (never mid-document).
- Safe error logging: provider exceptions are logged by stable error type /
  stage only — the exception message and request body never enter logs.

Stage-3 hard boundaries:
- **Per-call usage (boundary 8):** ``extract()`` returns an ``ExtractionResult``
  which IS a ``list[Fact]`` AND carries that call's own ``TokenUsage``. Worker
  reads usage straight off the result instead of racing on shared ``self.last_*``.
  The legacy ``last_*`` attributes are still written (for older tests) but the
  concurrency-safe contract is the returned object.
- **Per-request model override (boundary 3):** ``extract(..., model_id=...)``
  forwards a request-level model id to the provider WITHOUT mutating any shared
  ``provider.model_id``.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
from typing import Any

from agents.budget import TokenUsage
from core.prompts import PromptRegistry, get_default_registry, load_lock
from core.providers.base import BaseLLMProvider, LLMResponse, Message
from core.tracing import TracingContext, noop_tracing
from domain.models import Fact, SourceDocument

log = logging.getLogger(__name__)

_SENTENCE_RE = re.compile(r"(?<=[。！？.!?])\s+|\n+")
MIN_CHARS = 20
MAX_CHARS_PER_FACT = 400
MAX_FACTS_PER_DOC = 8

# How much of each source document we send to the LLM (chars).
_MAX_SOURCE_CHARS_FOR_LLM = 6000

# Hard total character bound on the messages actually transmitted to the
# provider (system + user [+ assistant + repair]). 16K chars ~= 4K tokens,
# well within Qwen's context window and the 180s per-request timeout.
# The previous 120K value caused ProviderError (timeout) on every extractor
# call because the request body was too large for the API to process in time.
# 24K still timed out on real web pages (Tavily fetches can be large); 16K
# is the sweet spot that reliably completes in <60s on qwen3.8-flash.
MAX_TOTAL_INPUT_CHARS = 16_000


class ExtractionResult(list):
    """The facts produced by one extract() call, PLUS that call's own usage.

    It IS a ``list[Fact]`` (backwards-compatible: existing callers iterate /
    ``len()`` / index it exactly as before) but it also carries the per-call
    ``TokenUsage`` as an attribute. This removes the shared mutable
    ``self.last_*`` cross-task leak: each concurrent extract() returns its OWN
    usage object, so WorkerNode reads its usage straight off the result instead
    of racing on shared instance state.
    """

    def __init__(self, facts: list[Fact], usage: TokenUsage) -> None:
        super().__init__(facts)
        self.usage = usage


class HeuristicFactExtractor:
    name = "heuristic"

    async def extract(
        self,
        docs: list[SourceDocument],
        *,
        user_context: str = "",
        model_id: str | None = None,
    ) -> ExtractionResult:
        facts = await asyncio.to_thread(self._extract_sync, docs)
        # No real LLM call was made: usage is an estimate derived from the
        # documents actually processed. It is scoped to THIS call (returned on
        # the result), never stored on shared instance state.
        prompt_chars = sum(len(d.content) for d in docs)
        completion_chars = sum(len(f.claim) for f in facts)
        usage = TokenUsage(
            prompt_tokens=(prompt_chars + 3) // 4,
            completion_tokens=(completion_chars + 3) // 4,
            estimated=True,
        )
        return ExtractionResult(facts, usage)

    def _extract_sync(self, docs: list[SourceDocument]) -> list[Fact]:
        facts: list[Fact] = []
        counter = 0
        for doc in docs:
            if not doc.fetched_ok or not doc.content.strip():
                continue
            sentences = [s.strip() for s in _SENTENCE_RE.split(doc.content) if s.strip()]
            emitted = 0
            for sent in sentences:
                if emitted >= MAX_FACTS_PER_DOC:
                    break
                if len(sent) < MIN_CHARS:
                    continue
                counter += 1
                emitted += 1
                facts.append(
                    Fact(
                        fact_id=f"f{counter}",
                        claim=sent[:MAX_CHARS_PER_FACT],
                        source_citation_ids=[doc.citation.citation_id],
                    )
                )
        return facts


# --- LLM-based extraction ---------------------------------------------------

# The actual prompt templates now live in the Prompt Hub manifest
# (src/core/prompts/manifest.json) and are rendered via PromptRegistry.
# See docs/prompt-hub.md for the hosting / sync / promote workflow.


def _claim_is_supported(claim: str, source_texts: dict[str, str]) -> bool:
    """Return True iff claim is a locatable substring of a cited source body."""
    if not claim:
        return False
    return any(claim in body for body in source_texts.values())


class LLMFactExtractor:
    """LLM-backed fact extractor with citation + extractive validation.

    - Sends source documents to the LLM and asks for structured facts.
    - Validates that every returned fact's citation_ids exist in the input.
    - Validates that every returned fact's claim is a verbatim substring of at
      least one of its cited source bodies (no paraphrase / extrapolation).
    - Facts failing either validation are rejected with a logged reason.
    - Root schema not matching ``{"facts": [...]}`` → one repair request; if
      that still fails → fallback to HeuristicFactExtractor.
    - Outgoing messages are always bounded by ``MAX_TOTAL_INPUT_CHARS``.
    - Returns an ``ExtractionResult`` carrying this call's own usage (boundary 8).
    """

    name = "llm"

    def __init__(
        self,
        llm: BaseLLMProvider,
        *,
        fallback: HeuristicFactExtractor | None = None,
        registry: PromptRegistry | None = None,
        tracing: TracingContext | None = None,
    ) -> None:
        self._llm = llm
        self._fallback = fallback or HeuristicFactExtractor()
        # Prompt Hub: resolve system + repair templates from the registry.
        # Default registry is local mode (zero network) so offline tests keep
        # working unchanged.
        self._registry = registry or get_default_registry()
        self._system_prompt = self._registry.render("fact_extraction_system").system_text()
        self._repair_prompt = self._registry.render("fact_extraction_repair").messages[0][1]
        # LangSmith prompt association: wrap each real LLM call in an LLM-type
        # run whose extra.metadata carries lc_hub_repo / lc_hub_commit_hash.
        # When tracing is None we fall back to noop_tracing() (zero network).
        # Commit hashes come from manifest.lock.json (the last pushed revision).
        self._tracing = tracing or noop_tracing()
        self._prompt_commits: dict[str, str] = {}
        try:
            for name, entry in load_lock().items():
                self._prompt_commits[name] = entry.commit_hash
        except Exception:  # noqa: BLE001
            # Lock file missing / unreadable must never break extraction.
            self._prompt_commits = {}
        # Aggregated usage across the first call and the optional repair call.
        # last_usage_estimated is True only when the provider returned no real
        # token counts for either call.
        # NOTE: these are a LEGACY sync snapshot kept for older tests. The
        # concurrency-safe path is the ExtractionResult returned by extract();
        # the WorkerNode reads usage from there and never from these attributes.
        self.last_usage_estimated = True
        self.last_prompt_tokens = 0
        self.last_completion_tokens = 0
        self._accepts_model_id: bool | None = None

    # -- prompt construction with hard total input bound --------------------
    @staticmethod
    def _doc_block(doc: SourceDocument) -> str:
        cid = doc.citation.citation_id
        content = doc.content[:_MAX_SOURCE_CHARS_FOR_LLM]
        return f"\n--- citation_id: {cid} ---\n{content}\n"

    def _build_user_prompt_bounded(
        self,
        docs: list[SourceDocument],
        *,
        user_budget: int,
        user_context: str = "",
    ) -> str:
        """Build the user prompt, dropping whole documents that would overflow.

        Documents are kept in input order. A document is never cut in the
        middle; we either include its full block (provenance citation_id +
        content) or skip it. ``user_context`` (the user's research scope) is
        folded in front of the documents so it is visible in the outgoing
        extraction prompt, and its length counts against the budget.
        """
        intro = "Source documents:\n"
        if user_context.strip():
            intro = f"User context / scope: {user_context.strip()}\n\nSource documents:\n"
        user = intro
        for doc in docs:
            if not doc.fetched_ok:
                continue
            block = self._doc_block(doc)
            # Hard budget: zero/negative budget must include NO document. We
            # never force the first doc in, and never cut a doc in the middle.
            if len(user) + len(block) > user_budget:
                break
            user += block
        return user

    def _build_messages_bounded(
        self,
        valid_docs: list[SourceDocument],
        *,
        reserve_chars: int = 0,
        user_context: str = "",
    ) -> list[Message]:
        """Build system + user messages whose total stays within the global bound.

        ``reserve_chars`` accounts for bytes that will be appended after the user
        message on this call (e.g. the assistant echo + repair prompt on the
        repair call), so the FULL outgoing message list stays bounded.
        """
        system_len = len(self._system_prompt)
        user_budget = MAX_TOTAL_INPUT_CHARS - system_len - reserve_chars
        if user_budget < 0:
            user_budget = 0
        user = self._build_user_prompt_bounded(
            valid_docs, user_budget=user_budget, user_context=user_context
        )
        return [
            Message(role="system", content=self._system_prompt),
            Message(role="user", content=user),
        ]

    # -- usage ---------------------------------------------------------------
    def _commit_usage(
        self,
        *,
        prompt_tokens: int,
        completion_tokens: int,
        estimated: bool,
    ) -> None:
        """Write the per-call usage snapshot back to the public (legacy) attrs."""
        self.last_prompt_tokens = prompt_tokens
        self.last_completion_tokens = completion_tokens
        self.last_usage_estimated = estimated

    # -- bounded sending -----------------------------------------------------
    @staticmethod
    def _total_chars(messages: list[Message]) -> int:
        return sum(len(m.content) for m in messages)

    @staticmethod
    def _fit_to_budget(messages: list[Message]) -> list[Message]:
        """Defensive last-resort trim: never send more than the hard bound.

        The builders already bound the payload; this is a safety net. The only
        safe-to-trim payload is a trailing assistant echo (the model's own
        previous output). System/user/repair messages carry provenance or
        instructions and are never cut.
        """
        total = LLMFactExtractor._total_chars(messages)
        if total <= MAX_TOTAL_INPUT_CHARS:
            return messages
        out = list(messages)
        for idx in range(len(out) - 1, -1, -1):
            if out[idx].role == "assistant":
                other = sum(len(m.content) for j, m in enumerate(out) if j != idx)
                room = MAX_TOTAL_INPUT_CHARS - other
                if room < 0:
                    room = 0
                content = out[idx].content
                if len(content) > room:
                    content = content[:room]
                out[idx] = Message(role="assistant", content=content)
                break
        return out

    def _provider_accepts_model_id(self, provider: Any | None = None) -> bool:
        """True when the provider's acomplete() declares a ``model_id`` kwarg.

        Computed once per provider and cached. Duck-typed test doubles that only
        implement ``accomplete(messages)`` are called without the kwarg so
        legacy doubles keep working unchanged.
        """
        target = provider if provider is not None else self._llm
        if target is self._llm and self._accepts_model_id is not None:
            return self._accepts_model_id
        try:
            params = inspect.signature(target.acomplete).parameters
            result = "model_id" in params
        except (TypeError, ValueError):
            result = False
        if target is self._llm:
            self._accepts_model_id = result
        return result

    async def _complete_bounded(
        self,
        messages: list[Message],
        *,
        model_id: str | None = None,
        llm: Any | None = None,
        prompt_name: str = "fact_extraction_system",
    ) -> LLMResponse:
        """Single send entry: enforce the hard total input bound, then call.

        When ``llm`` (a per-request, model-scoped provider clone) is supplied it
        is used directly and records its own model. Otherwise the per-request
        ``model_id`` override is forwarded to ``self._llm`` when it supports the
        kwarg (FakeLLM / QwenProvider). The shared provider's own ``model_id`` is
        NEVER mutated (boundary 3).

        ``prompt_name`` selects which Prompt Hub prompt this call is attributed
        to for LangSmith Application↔Prompt association.
        """
        bounded = self._fit_to_budget(messages)
        if llm is not None:
            return await self._call_provider(llm, bounded, model_id, prompt_name)
        return await self._call_provider(self._llm, bounded, model_id, prompt_name)

    async def _call_provider(
        self,
        provider: Any,
        messages: list[Message],
        model_id: str | None,
        prompt_name: str = "fact_extraction_system",
    ) -> LLMResponse:
        """Issue the single provider call, wrapped in a prompt-tagged LLM span.

        The span's LLM-type run carries ``lc_hub_repo=<prompt_name>`` and
        ``lc_hub_commit_hash`` from the lock file, which is the signal
        LangSmith uses to auto-associate this prompt to the Application.
        """
        commit = self._prompt_commits.get(prompt_name, "")
        with self._tracing.llm_prompt_span(prompt_name, commit):
            if model_id is not None and self._provider_accepts_model_id(provider):
                return await provider.acomplete(messages, model_id=model_id)
            return await provider.acomplete(messages)

    def _build_repair_messages(
        self,
        valid_docs: list[SourceDocument],
        prev_text: str,
        user_context: str = "",
    ) -> list[Message]:
        """Build the full repair message list within the hard total bound.

        system + user(docs) + assistant(prev_text, truncated) + repair_prompt
        must all fit in MAX_TOTAL_INPUT_CHARS. The invalid previous response is
        treated as disposable: it is echoed only up to the remaining room, and
        dropped entirely when no room remains.
        """
        system = Message(role="system", content=self._system_prompt)
        repair_prompt_msg = Message(role="user", content=self._repair_prompt)
        fixed_len = len(system.content) + len(repair_prompt_msg.content)
        user_budget = MAX_TOTAL_INPUT_CHARS - fixed_len
        if user_budget < 0:
            user_budget = 0
        user_content = self._build_user_prompt_bounded(
            valid_docs, user_budget=user_budget, user_context=user_context
        )
        fixed_len += len(user_content)
        echo_budget = MAX_TOTAL_INPUT_CHARS - fixed_len
        if echo_budget < 0:
            echo_budget = 0
        echo = prev_text if len(prev_text) <= echo_budget else prev_text[:echo_budget]
        return [
            system,
            Message(role="user", content=user_content),
            Message(role="assistant", content=echo),
            repair_prompt_msg,
        ]

    # -- main extraction ----------------------------------------------------
    async def extract(
        self,
        docs: list[SourceDocument],
        *,
        user_context: str = "",
        model_id: str | None = None,
        llm: Any | None = None,
    ) -> ExtractionResult:
        """Extract facts from ``docs``. Returns ``ExtractionResult(facts, usage)``.

        Usage is accumulated in LOCAL closure variables for THIS call and both
        (a) written to the legacy ``self.last_*`` snapshot and (b) returned on
        the result. The WorkerNode uses the per-result usage (boundary 8); the
        legacy attrs remain for older tests. Do NOT read ``self.last_*`` from
        the worker path.

        ``llm`` is an optional per-request, model-scoped provider clone
        (``provider.with_model(...)``). When supplied it is used INSTEAD of
        ``self._llm``; ``model_id`` is then applied as a request-level override
        only when ``llm`` is omitted.
        """
        prompt_total = 0
        completion_total = 0
        usage_reported_calls = 0
        usage_calls = 0

        def _build_usage() -> TokenUsage:
            estimated = (usage_calls == 0) or (usage_reported_calls != usage_calls)
            return TokenUsage(
                prompt_tokens=prompt_total,
                completion_tokens=completion_total,
                estimated=estimated,
            )

        def _commit_usage_snapshot() -> TokenUsage:
            usage = _build_usage()
            self._commit_usage(
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                estimated=usage.estimated,
            )
            return usage

        valid_docs = [d for d in docs if d.fetched_ok and d.content.strip()]
        if not valid_docs:
            usage = _commit_usage_snapshot()
            return ExtractionResult([], usage)

        known_ids = {d.citation.citation_id for d in valid_docs}
        # Map each known id to the (possibly truncated) body actually shown to
        # the LLM, so extractive validation checks what the model could see.
        source_texts = {
            d.citation.citation_id: d.content[:_MAX_SOURCE_CHARS_FOR_LLM] for d in valid_docs
        }

        # First call: reserve nothing extra (no echo / repair appended yet).
        messages = self._build_messages_bounded(
            valid_docs, reserve_chars=0, user_context=user_context
        )

        try:
            resp = await self._complete_bounded(
                messages, model_id=model_id, llm=llm, prompt_name="fact_extraction_system"
            )
        except Exception as e:  # noqa: BLE001
            # Stable, redacted error: never the exception text or request body.
            log.warning(
                "llm_extractor_call_failed stage=first error_type=%s",
                type(e).__name__,
            )
            result = await self._fallback.extract(docs)
            usage = _commit_usage_snapshot()
            # Preserve the fallback's own per-call usage too.
            return ExtractionResult(list(result), result.usage)

        prompt_total += resp.prompt_tokens
        completion_total += resp.completion_tokens
        usage_calls += 1
        if not getattr(resp, "usage_estimated", True):
            usage_reported_calls += 1

        facts = self._parse_facts(resp.text, known_ids, source_texts)
        if facts is not None:
            usage = _commit_usage_snapshot()
            return ExtractionResult(facts, usage)

        # Controlled repair: ONE retry. The full repair message list is built
        # within the same hard total input bound.
        log.info("llm_extractor_unparseable attempting_one_repair")
        repair_messages = self._build_repair_messages(valid_docs, resp.text, user_context)
        try:
            repair_resp = await self._complete_bounded(
                repair_messages,
                model_id=model_id,
                llm=llm,
                prompt_name="fact_extraction_repair",
            )
        except Exception as e:  # noqa: BLE001
            log.warning(
                "llm_extractor_call_failed stage=repair error_type=%s",
                type(e).__name__,
            )
            log.info("llm_extractor_fallback reason=parse_failed_after_repair")
            result = await self._fallback.extract(docs)
            usage = _commit_usage_snapshot()
            return ExtractionResult(list(result), result.usage)

        prompt_total += repair_resp.prompt_tokens
        completion_total += repair_resp.completion_tokens
        usage_calls += 1
        if not getattr(repair_resp, "usage_estimated", True):
            usage_reported_calls += 1
        facts = self._parse_facts(repair_resp.text, known_ids, source_texts)
        if facts is not None:
            usage = _commit_usage_snapshot()
            return ExtractionResult(facts, usage)

        # Explicit fallback reason: still unparseable after the one repair.
        log.info("llm_extractor_fallback reason=parse_failed_after_repair")
        result = await self._fallback.extract(docs)
        usage = _commit_usage_snapshot()
        return ExtractionResult(list(result), result.usage)

    # -- parsing -------------------------------------------------------------
    def _parse_facts(
        self,
        text: str,
        known_ids: set[str],
        source_texts: dict[str, str],
    ) -> list[Fact] | None:
        """Parse LLM output.

        Returns ``None`` when the response cannot be treated as the required
        root object (triggers the one controlled repair). Returns a list
        (possibly empty) when the root object was structurally valid; facts that
        fail citation or extractive support validation are dropped with a logged
        reason.
        """
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
            cleaned = re.sub(r"\s*```$", "", cleaned)

        try:
            data = json.loads(cleaned)
        except (json.JSONDecodeError, ValueError):
            return None

        # Root MUST be an object (not a list / null / scalar).
        if not isinstance(data, dict):
            log.info(
                "llm_extractor_reject_root root_type=%s reason=not_object",
                type(data).__name__,
            )
            return None

        raw_facts = data.get("facts")
        if not isinstance(raw_facts, list):
            log.info("llm_extractor_reject_root reason=facts_not_list")
            return None

        facts: list[Fact] = []
        counter = 0
        for item in raw_facts:
            if not isinstance(item, dict):
                log.info("llm_extractor_reject_fact reason=item_not_object")
                continue
            claim = str(item.get("claim", "")).strip()
            cids = item.get("citation_ids", [])
            if not claim or not isinstance(cids, list):
                log.info("llm_extractor_reject_fact reason=missing_claim_or_cids")
                continue
            # Citation set must be ENTIRELY known/valid. A mixed set
            # (e.g. ["c1", "missing"]) is NOT silently rewritten to the valid
            # subset — the whole fact is rejected.
            unknown = [c for c in cids if not (isinstance(c, str) and c in known_ids)]
            if unknown:
                log.info(
                    "llm_extractor_reject_fact reason=unknown_citation_in_set "
                    "n_cids=%d n_unknown=%d",
                    len(cids),
                    len(unknown),
                )
                continue
            valid_cids = [c for c in cids if isinstance(c, str)]
            # Strict extractive support: the claim must be a verbatim substring of
            # at least one cited source body. A known citation id is NOT evidence.
            cited_texts = {cid: source_texts[cid] for cid in valid_cids if cid in source_texts}
            if not _claim_is_supported(claim, cited_texts):
                log.info(
                    "llm_extractor_reject_fact reason=claim_not_source_supported n_cids=%d",
                    len(valid_cids),
                )
                continue
            counter += 1
            facts.append(
                Fact(
                    fact_id=f"f{counter}",
                    claim=claim[:MAX_CHARS_PER_FACT],
                    source_citation_ids=valid_cids,
                )
            )
        return facts


__all__ = [
    "ExtractionResult",
    "HeuristicFactExtractor",
    "LLMFactExtractor",
    "MAX_TOTAL_INPUT_CHARS",
]
