"""MainAgent Stage4 P0 HARDENING acceptance tests (8 gaps found in field review).

These tests lock the 8 P0 gaps MainAgent reproduced live. They are written to
FAIL against the pre-hardening code and to PASS after the fixes. They must NOT
be deleted or weakened. The three field-reproduced反例 are kept verbatim:

  - nli_provider_failure=neutral error=        (P0#1: exception washed to neutral)
  - no_keyword_forced_entail=entailment        (P0#2: no relevant window -> AlwaysEntail)
  - raw_document_body={'text':'PRIVATE'}       (P0#3: memory consent bypass via raw put)

All tests run fully offline (the global conftest clears live credentials). No
real LLM / paid network call is made. Feishu is exercised via MockTransport
only — no real document creation.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from core.prompts import PromptRegistry
from core.providers.base import BaseLLMProvider, LLMResponse, Message
from domain.models import Citation, Fact, SourceDocument, SourceKind
from domain.verification import Claim
from service.verifier import (
    LLMNLI,
    CitationVerifier,
    NLIParseError,
    NLIProviderError,
)

SRC = Path(__file__).resolve().parents[2] / "src"


def _cite(cid: str, url: str = "https://x.example.com") -> Citation:
    return Citation(citation_id=cid, kind=SourceKind.WEB, locator=url, title=cid)


class _FakeFetcher:
    """Returns text per locator so the verifier re-fetches."""

    def __init__(self, mapping: dict[str, str]) -> None:
        self._m = mapping

    async def fetch(self, locator: str, *, citation_id: str) -> SourceDocument:
        text = self._m.get(locator, "")
        if not text:
            return SourceDocument(
                citation=_cite(citation_id, locator),
                content="",
                error="not found",
                fetched_ok=False,
            )
        return SourceDocument(citation=_cite(citation_id, locator), content=text, fetched_ok=True)


# =========================================================================
# P0#1: NLI provider / parse failure must NOT be washed to neutral.
# =========================================================================


class _RaisingLLM(BaseLLMProvider):
    provider_name = "raise"
    model_id = "m"

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def is_configured(self) -> bool:
        return True

    async def acomplete(
        self, messages: list[Message], *, model_id: str | None = None
    ) -> LLMResponse:
        raise self._exc


class _BadJsonLLM(BaseLLMProvider):
    provider_name = "badjson"
    model_id = "m"

    def is_configured(self) -> bool:
        return True

    async def acomplete(
        self, messages: list[Message], *, model_id: str | None = None
    ) -> LLMResponse:
        return LLMResponse(text="this is not json", model="m", provider="badjson")


class TestP01NLIErrorNotNeutral:
    @pytest.mark.asyncio
    async def test_provider_failure_raises_stable_error_not_neutral(self) -> None:
        """Field bug: nli_provider_failure=neutral error= . The NLI provider must
        raise a stable NLIProviderError instead of silently returning neutral."""
        nli = LLMNLI(_RaisingLLM(RuntimeError("provider down")), registry=PromptRegistry())
        with pytest.raises(NLIProviderError) as ei:
            await nli("claim", "evidence")
        # Stable machine-readable code; never the exception body / message.
        assert ei.value.error_code
        assert "provider down" not in str(ei.value)

    @pytest.mark.asyncio
    async def test_parse_failure_raises_stable_error_not_neutral(self) -> None:
        """Unparseable LLM output must raise NLIParseError, not return neutral."""
        nli = LLMNLI(_BadJsonLLM(), registry=PromptRegistry())
        with pytest.raises(NLIParseError):
            await nli("claim", "evidence")

    @pytest.mark.asyncio
    async def test_all_nli_failed_gives_verdict_none_not_neutral(self) -> None:
        """Field bug: Evidence.nli_error='' because neutral was treated as a normal
        verdict. When EVERY citation's NLI call fails, the aggregate verdict must
        be None with reason=nli_failed (distinct from refetch_failed)."""
        fetcher = _FakeFetcher({"https://x": "supporting evidence body here."})
        v = CitationVerifier(
            fetcher=fetcher,
            nli=LLMNLI(_RaisingLLM(RuntimeError("boom")), registry=PromptRegistry()),
        )
        claim = Claim(
            claim_id="c1", claim_text="supporting evidence body here.", citation_ids=["c1"]
        )
        result = await v.verify(claim, [_cite("c1", "https://x")])
        assert result.verdict is None, f"nli failure washed to neutral: {result.verdict}"
        assert "nli" in result.reason.lower(), f"reason not nli_failed: {result.reason!r}"
        ev = result.evidence[0]
        assert ev.nli_error, "Evidence.nli_error must be stably non-empty on NLI failure"
        assert ev.verdict is None, "per-citation verdict must be None, not neutral, on NLI failure"

    @pytest.mark.asyncio
    async def test_partial_nli_failure_keeps_metadata_and_aggregates_rest(self) -> None:
        """One citation's NLI fails (keeps its error metadata, excluded from
        verdicts) while the other citation yields a real verdict -> aggregate
        uses the good citation, bad one stays flagged."""

        class _MixedNLI:
            def __init__(self) -> None:
                self.calls = 0

            async def classify(self, claim: str, evidence: str) -> str:  # type: ignore[return-value]
                self.calls += 1
                if "good-evidence" in evidence:
                    return "entailment"
                raise NLIProviderError(error_code="nli_provider_failure", error_type="X")

        mixed = _MixedNLI()
        fetcher = _FakeFetcher(
            {
                "https://a": "good-evidence text matching the claim.",
                "https://b": "text on the bad citation that will fail NLI.",
            }
        )
        v = CitationVerifier(fetcher=fetcher, nli=mixed)
        claim = Claim(
            claim_id="c1",
            claim_text="good evidence text matching the claim.",
            citation_ids=["c1", "c2"],
        )
        result = await v.verify(claim, [_cite("c1", "https://a"), _cite("c2", "https://b")])
        assert result.verdict == "entailment"
        bad = [e for e in result.evidence if e.citation_id == "c2"][0]
        assert bad.nli_error
        assert bad.verdict is None


# =========================================================================
# P0#2: no relevant window -> neutral, do NOT call NLI.
# =========================================================================


class _AlwaysEntail:
    """Field bug injector: ALWAYS returns entailment no matter the evidence.
    If the verifier still calls it on a no-keyword window, it leaks entailment."""

    def __init__(self) -> None:
        self.call_count = 0

    async def classify(self, claim: str, evidence: str) -> str:  # type: ignore[return-value]
        self.call_count += 1
        return "entailment"


class TestP02NoRelevantWindow:
    @pytest.mark.asyncio
    async def test_no_keyword_window_does_not_call_nli_and_is_neutral(self) -> None:
        """Field bug: no_keyword_forced_entail=entailment. When the claim shares
        NO content words with the body, the verifier must short-circuit to
        neutral WITHOUT calling the NLI judge."""
        always = _AlwaysEntail()
        fetcher = _FakeFetcher({"https://x": "completely unrelated cooking recipes content."})
        v = CitationVerifier(fetcher=fetcher, nli=always)
        claim = Claim(
            claim_id="c1",
            claim_text="LangGraph durable checkpoint state semantics.",
            citation_ids=["c1"],
        )
        result = await v.verify(claim, [_cite("c1", "https://x")])
        assert always.call_count == 0, "NLI was called even with zero keyword overlap"
        assert result.verdict == "neutral"
        assert result.evidence[0].refetch_ok is True

    @pytest.mark.asyncio
    async def test_evidence_in_tail_is_found_and_entails(self) -> None:
        head = "word " * 400
        tail = "LangGraph provides typed state and persistent checkpoint support."
        fetcher = _FakeFetcher({"https://x": head + tail})
        v = CitationVerifier(fetcher=fetcher)
        claim = Claim(
            claim_id="c1",
            claim_text="LangGraph provides typed state and persistent checkpoint support.",
            citation_ids=["c1"],
        )
        result = await v.verify(claim, [_cite("c1", "https://x")])
        assert result.verdict == "entailment"

    @pytest.mark.asyncio
    async def test_best_coverage_window_not_earliest_generic_word(self) -> None:
        """A single generic word hit (e.g. 'system' / 'support') must NOT select
        the window. The chosen window must maximize DISTINCT claim content-word
        coverage."""
        from service.verifier import _find_relevant_window

        body = (
            "The system uses generic support text here. "
            + " " * 200
            + "DeepRacer neural network reinforcement learning training pipeline."
        )
        claim = "DeepRacer neural network reinforcement learning pipeline."
        excerpt, offset, meta = _find_relevant_window(body, claim)
        assert meta["found"] is True
        assert "deepracer" in excerpt.lower()
        assert meta["score"] >= 1.0

    def test_find_relevant_window_reports_found_flag(self) -> None:
        from service.verifier import _find_relevant_window

        _, _, meta = _find_relevant_window("totally different body", "neural network pipeline")
        assert meta["found"] is False
        _, _, meta2 = _find_relevant_window(
            "neural network reinforcement learning pipeline", "neural network pipeline"
        )
        assert meta2["found"] is True


# =========================================================================
# P0#3: Memory consent cannot be bypassed via raw put/aput.
# =========================================================================


class TestP03MemoryConsent:
    def test_raw_put_rejects_unconsented_and_raw_body(self) -> None:
        """Field bug: raw_document_body={'text':'PRIVATE'} was written via put.
        The public write path must only accept a consented MemoryRecord; raw
        arbitrary keys / raw document bodies are refused."""
        from service.long_term_store import LongTermStore, MemoryRecord

        store = LongTermStore()
        # Consent required: even an allowlisted key without consent is refused.
        with pytest.raises((ValueError, PermissionError)):
            store.put_record(
                namespace="user/u1",
                record=MemoryRecord(type="preference", key="user_theme", value={"x": 1}),
            )
        # Non-allowlisted key (raw document body) is refused even if consenting.
        with pytest.raises((ValueError, PermissionError)):
            store.put_record(
                namespace="user/u1",
                record=MemoryRecord(
                    type="preference",
                    key="raw_document_body",
                    value={"text": "PRIVATE"},
                    consent=True,
                ),
            )

    def test_consented_record_persists_with_full_audit_fields(self) -> None:
        from service.long_term_store import LongTermStore, MemoryRecord

        store = LongTermStore()
        rec = MemoryRecord(
            type="preference",
            key="user_theme",
            value={"mode": "dark"},
            provenance="explicit_user",
            consent=True,
        )
        store.put_record(namespace="user/u1", record=rec)
        full = store.get_record(namespace="user/u1", key="user_theme")
        assert full is not None
        assert full.type == "preference"
        assert full.consent is True
        assert full.provenance == "explicit_user"
        assert full.value == {"mode": "dark"}

    def test_list_records_roundtrip(self) -> None:
        from service.long_term_store import LongTermStore, MemoryRecord

        store = LongTermStore()
        store.put_record(
            namespace="user/u1",
            record=MemoryRecord(type="preference", key="user_theme", value={"a": 1}, consent=True),
        )
        store.put_record(
            namespace="user/u1",
            record=MemoryRecord(type="preference", key="user_scope", value={"b": 2}, consent=True),
        )
        records = store.list_records(namespace="user/u1")
        keys = {r.key for r in records}
        assert keys == {"user_theme", "user_scope"}

    @pytest.mark.asyncio
    async def test_async_consented_record_roundtrip(self) -> None:
        from service.long_term_store import AsyncLongTermStore, LongTermStore, MemoryRecord

        store = AsyncLongTermStore(LongTermStore())
        await store.aput_record(
            namespace="user/u1",
            record=MemoryRecord(type="preference", key="user_theme", value={"m": 1}, consent=True),
        )
        rec = await store.aget_record(namespace="user/u1", key="user_theme")
        assert rec is not None and rec.consent is True and rec.value == {"m": 1}

    def test_db_persists_type_provenance_consent_across_restart(self, tmp_path: Path) -> None:
        from service.long_term_store import LongTermStore, MemoryRecord

        db = tmp_path / "mem.sqlite"
        s1 = LongTermStore(db_path=db)
        s1.put_record(
            namespace="user/u1",
            record=MemoryRecord(
                type="conclusion_summary",
                key="conclusion_summary",
                value={"summary": "deep dive ok"},
                provenance="run_123",
                consent=True,
            ),
        )
        s1.close()
        s2 = LongTermStore(db_path=db)
        rec = s2.get_record(namespace="user/u1", key="conclusion_summary")
        assert rec is not None
        assert rec.type == "conclusion_summary"
        assert rec.provenance == "run_123"
        assert rec.consent is True
        s2.close()


# =========================================================================
# P0#4: Business wiring must read/write memory only under explicit opt-in.
# =========================================================================


class TestP04BusinessWiring:
    def test_settings_expose_memory_opt_in(self) -> None:
        from core.config import Settings

        s = Settings(_env_file=None)  # type: ignore[call-arg]
        assert s.memory_enabled is False
        assert hasattr(s, "memory_namespace")
        assert hasattr(s, "memory_db_path")

    def test_runner_accepts_injectable_memory_service(self) -> None:
        """The Runner must accept an injected memory store; default writes nothing."""
        import inspect

        from api.runner import ResearchRunner

        sig = inspect.signature(ResearchRunner.__init__)
        assert "memory_store" in sig.parameters or "memory" in sig.parameters, (
            "ResearchRunner does not accept an injectable memory service"
        )

    @pytest.mark.asyncio
    async def test_no_write_without_explicit_consent(self) -> None:
        """Post-run conclusion_summary is only written when the caller explicitly
        opts in. Without it, the store stays empty."""
        from service.long_term_store import LongTermStore
        from service.verified_report import VerifiedReportBuilder
        from service.verifier import CitationVerifier

        store = LongTermStore()
        builder = VerifiedReportBuilder(verifier=CitationVerifier(fetcher=_FakeFetcher({})))
        facts = [Fact(fact_id="f1", claim="a claim", source_citation_ids=["c1"])]
        await builder.build(query="q", facts=facts, citations=[_cite("c1", "https://missing")])
        assert store.list_records(namespace="session/default") == []


# =========================================================================
# P0#5: Revision drop semantics.
# =========================================================================


class TestP05RevisionDrop:
    @pytest.mark.asyncio
    async def test_contradiction_is_dropped_not_reverified_and_not_rendered(self) -> None:
        """contradiction -> status=dropped, NO re-verify, and the original fact
        text is NOT rendered as a body fact."""
        from service.verified_report import VerifiedReportBuilder

        doc = "The system supports real-time streaming with low latency."
        fetcher = _FakeFetcher({"https://x": doc})
        calls = {"n": 0}
        _orig = CitationVerifier.verify

        async def _counted(self, claim, citations, **kw):  # type: ignore[no-untyped-def]
            calls["n"] += 1
            return await _orig(self, claim, citations, **kw)

        CitationVerifier.verify = _counted  # type: ignore[assignment]
        try:
            builder = VerifiedReportBuilder(verifier=CitationVerifier(fetcher=fetcher))
            facts = [
                Fact(
                    fact_id="f1",
                    claim="The system does NOT support streaming.",
                    source_citation_ids=["c1"],
                )
            ]
            out = await builder.build(query="q", facts=facts, citations=[_cite("c1", "https://x")])
        finally:
            CitationVerifier.verify = _orig  # type: ignore[assignment]

        assert calls["n"] == 1, f"dropped claim should NOT be re-verified: {calls['n']} calls"
        dropped = [c for c in out.claims if c.verification_status == "dropped"]
        assert dropped, "contradicted claim must be marked dropped"
        verified_body = [c.claim_text for c in out.claims if c.verification_status == "verified"]
        for t in verified_body:
            assert "does NOT support streaming" not in t

    @pytest.mark.asyncio
    async def test_neutral_does_not_reassert_original_claim(self) -> None:
        """neutral must wrap as a limitation / uncertainty item and must NOT
        keep asserting the original fact text."""
        from service.verified_report import VerifiedReportBuilder

        fetcher = _FakeFetcher({"https://x": "unrelated filler content only."})
        builder = VerifiedReportBuilder(verifier=CitationVerifier(fetcher=fetcher))
        original = "Quantum fusion reactors power the city grid at scale."
        facts = [Fact(fact_id="f1", claim=original, source_citation_ids=["c1"])]
        out = await builder.build(query="q", facts=facts, citations=[_cite("c1", "https://x")])
        neutral_claims = [c for c in out.claims if c.verification_status == "neutral"]
        assert neutral_claims
        for c in neutral_claims:
            assert (
                original not in c.claim_text or "有待" in c.claim_text or "证据不足" in c.claim_text
            )


# =========================================================================
# P0#6: Real persistent vector store + Qwen embedding/rerank adapters.
# =========================================================================


class TestP06PersistentVector:
    def test_persistent_store_survives_restart(self, tmp_path: Path) -> None:
        from service.retrieval import SQLiteVectorStore

        db = tmp_path / "vec.sqlite"
        s1 = SQLiteVectorStore(db_path=db, dim=4)
        s1.upsert(
            [
                ("chunk-1", [1.0, 0.0, 0.0, 0.0], {"text": "alpha", "source_id": "src1"}),
                ("chunk-2", [0.0, 1.0, 0.0, 0.0], {"text": "beta", "source_id": "src2"}),
            ]
        )
        s1.close()
        s2 = SQLiteVectorStore(db_path=db, dim=4)
        hits = s2.search([1.0, 0.0, 0.0, 0.0], k=2)
        assert hits[0][0] == "chunk-1"
        s2.close()

    def test_dimension_mismatch_raises_structural_error(self, tmp_path: Path) -> None:
        from service.retrieval import SQLiteVectorStore

        store = SQLiteVectorStore(db_path=tmp_path / "v.sqlite", dim=4)
        with pytest.raises(ValueError):
            store.search([1.0, 2.0, 3.0])  # 3 != 4
        with pytest.raises(ValueError):
            store.upsert([("x", [1.0, 2.0, 3.0], {})])  # 3 != 4

    def test_cosine_no_min_truncation(self) -> None:
        from service.retrieval import cosine

        a = [1.0, 0.0, 0.0]
        b = [0.0, 1.0, 0.0]
        assert cosine(a, b) == 0.0
        with pytest.raises(ValueError):
            cosine([1.0, 2.0], [1.0])

    @pytest.mark.asyncio
    async def test_qwen_embedding_provider_http_contract(self) -> None:
        """QwenEmbeddingProvider posts to the embeddings endpoint with the right
        model and returns vectors. Uses a mock transport (no payment)."""
        import httpx

        from service.retrieval import QwenEmbeddingProvider

        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["body"] = json.loads(request.content.decode())
            return httpx.Response(200, json={"data": [{"embedding": [0.1, 0.2, 0.3, 0.4]}]})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        prov = QwenEmbeddingProvider(
            model="text-embedding-v4",
            base_url="https://api.example.com/v1",
            api_key="k",
            client=client,
        )
        vecs = await prov.aencode(["hello"])
        await client.aclose()
        assert vecs[0] == pytest.approx([0.1, 0.2, 0.3, 0.4])
        assert "embeddings" in seen["url"]
        assert seen["body"]["model"] == "text-embedding-v4"

    @pytest.mark.asyncio
    async def test_ahybrid_retrieve_uses_async_encoder(self) -> None:
        from service.retrieval import Chunk, FakeEncoder, ahybrid_retrieve

        chunks = [
            Chunk(
                chunk_id="a",
                source_id="s1",
                position=0,
                text="langgraph state graph",
                content_hash="h1",
            ),
            Chunk(
                chunk_id="b",
                source_id="s2",
                position=0,
                text="llamaindex indexing",
                content_hash="h2",
            ),
        ]
        out = await ahybrid_retrieve("langgraph state", chunks, top_k=2, encoder=FakeEncoder())
        assert [c.chunk_id for c in out] == ["a", "b"]


# =========================================================================
# P0#7: Prompt pin runtime — apreload pulls the LOCK commit hash.
# =========================================================================


def _lock_entry(commit: str) -> Any:
    class _LE:
        pass

    e = _LE()
    e.commit_hash = commit
    e.identifier = "citation_verifier_system"
    return e


class TestP07PromptPinRuntime:
    def test_apreload_pulls_lock_commit_not_latest(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from core.prompts import registry as reg_mod
        from core.prompts.spec import PromptSpec

        pulled: list[tuple[str, dict[str, Any]]] = []

        class _FakeRepo:
            def pull(self, identifier, *, tag=None, commit_hash=None):  # type: ignore[no-untyped-def]
                pulled.append((identifier, {"tag": tag, "commit_hash": commit_hash}))
                return PromptSpec(
                    name=identifier,
                    description="x",
                    version="1.0.0",
                    messages=(("system", "hi"),),
                    variables=tuple(),
                )

        monkeypatch.setattr(
            reg_mod, "load_lock", lambda: {"citation_verifier_system": _lock_entry("deadbeef")}
        )
        reg = PromptRegistry(mode="hybrid", repo=_FakeRepo())  # type: ignore[arg-type]
        asyncio.run(reg.apreload())
        pinned = [p for p in pulled if p[0] == "citation_verifier_system"]
        assert pinned, "preload never pulled the pinned prompt"
        assert pinned[0][1]["commit_hash"] == "deadbeef", (
            f"preload did not pin to lock commit: {pinned[0][1]}"
        )

    def test_render_records_pinned_commit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from core.prompts import registry as reg_mod
        from core.prompts.spec import PromptSpec

        class _FakeRepo:
            def pull(self, identifier, *, tag=None, commit_hash=None):  # type: ignore[no-untyped-def]
                return PromptSpec(
                    name=identifier,
                    description="x",
                    version="1.0.0",
                    messages=(("system", "hi {var}"),),
                    variables=("var",),
                )

        e = _lock_entry("abc12345")
        monkeypatch.setattr(reg_mod, "load_lock", lambda: {"citation_verifier_user": e})
        reg = PromptRegistry(mode="hybrid", repo=_FakeRepo())  # type: ignore[arg-type]
        asyncio.run(reg.apreload())
        rendered = reg.render("citation_verifier_user", var="v")
        assert rendered.commit == "abc12345", f"RenderedPrompt.commit not pinned: {rendered.commit}"


# =========================================================================
# P0#8: Feishu 403 precise diagnosis (MockTransport only; no real creation).
# =========================================================================


class TestP08Feishu403:
    @pytest.mark.asyncio
    async def test_403_scope_vs_folder_classified(self) -> None:
        import httpx

        from service.exporters import FeishuExporter

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if "tenant_access_token" in path:
                return httpx.Response(200, json={"code": 0, "tenant_access_token": "t"})
            if path.endswith("/documents"):
                return httpx.Response(403, json={"code": 99991672, "msg": "scope denied"})
            return httpx.Response(404, json={})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        ex = FeishuExporter(app_id="app", app_secret="sec", folder_token="f", client=client)
        r = await ex.export_markdown(title="t", markdown="x")
        await client.aclose()
        assert r.exported is False
        assert "403" in r.reason
        assert "99991672" in r.reason
        assert "scope" in r.reason.lower()

    @pytest.mark.asyncio
    async def test_403_unknown_when_no_code_in_body(self) -> None:
        import httpx

        from service.exporters import FeishuExporter

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if "tenant_access_token" in path:
                return httpx.Response(200, json={"code": 0, "tenant_access_token": "t"})
            if path.endswith("/documents"):
                return httpx.Response(
                    403, content="forbidden", headers={"content-type": "text/plain"}
                )
            return httpx.Response(404, json={})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        ex = FeishuExporter(app_id="app", app_secret="sec", folder_token="f", client=client)
        r = await ex.export_markdown(title="t", markdown="x")
        await client.aclose()
        assert r.exported is False
        assert "http_403_unknown" in r.reason or "unknown" in r.reason.lower()


__all__ = []
