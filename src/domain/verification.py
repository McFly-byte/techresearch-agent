"""Claim and verification data contract (phase 4).

A Claim is a structured statement produced by the Writer, not an arbitrary
sentence regex. Each claim carries:
  - claim_id
  - claim_text (the sentence as it appears in the report)
  - section_id (which report section)
  - citation_ids (must all exist in the citation_map)
  - claim_type (factual / comparison / opinion / unknown)
  - verification_status (pending / verified / contradicted / neutral / refetch_failed)
  - verification_note (human-readable reason)

Status transitions (explicit):
  pending -> verified | contradicted | neutral | refetch_failed
  (after one revision) contradicted/neutral -> verified | neutral | dropped
  A claim may be DROPPED (not rendered) if revision cannot fix it.

The CitationVerifier does NOT call an LLM by default. It uses an injectable
`nlifn` callable so tests can be deterministic. When a real NLI provider is
wired in later, it must return the same shape.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

ClaimType = Literal["factual", "comparison", "opinion", "unknown"]
Verdict = Literal["entailment", "contradiction", "neutral"]
ClaimStatus = Literal[
    "pending",
    "verified",
    "contradicted",
    "neutral",
    "refetch_failed",
    "dropped",
]


class Claim(BaseModel):
    claim_id: str = Field(pattern=r"^c\d+$")
    claim_text: str
    section_id: str = "body"
    citation_ids: list[str] = Field(default_factory=list, min_length=1)
    claim_type: ClaimType = "factual"
    verification_status: ClaimStatus = "pending"
    verification_note: str = ""


class VerificationEvidence(BaseModel):
    """What the verifier actually fetched and compared."""

    citation_id: str
    refetch_ok: bool
    excerpt: str = ""  # the evidence window pulled from the re-fetched source
    fetched_at: str = ""
    # Audit metadata (Stage4 P0-2): where the window came from.
    source_locator: str = ""  # the re-fetched locator (URL / path)
    char_offset: int = -1  # start offset of the excerpt in the full source body
    content_hash: str = ""  # sha256[:12] of the excerpt, for回查 / dedup
    prompt_version: str = ""  # which verifier prompt version produced this
    model_id: str = ""  # which NLI model produced the verdict
    verdict: Verdict | None = None  # per-citation verdict (aggregate on result)
    nli_error: str = ""  # stable error type if the NLI call itself failed


class VerificationResult(BaseModel):
    """Outcome of verifying one claim against its cited sources."""

    claim_id: str
    verdict: Verdict | None = None  # None if refetch failed
    evidence: list[VerificationEvidence] = Field(default_factory=list)
    reason: str = ""
    verifier_version: str = "heuristic-v1"
    verified_at: str = ""
    round: str = "initial"  # "initial" | "revision" — which pass produced this
    conflict: bool = False  # True when sources disagree (entailment + contradiction)

    @property
    def passed(self) -> bool:
        return self.verdict == "entailment"


class VerificationMetrics(BaseModel):
    """Aggregate stats. Zero-denominator is explicitly defined as 0.0."""

    total_claims: int = 0
    verified: int = 0
    contradicted: int = 0
    neutral: int = 0
    refetch_failed: int = 0
    dropped: int = 0

    @property
    def citation_precision(self) -> float:
        """Fraction of claims that are verified (entailment). 0 if no claims."""
        if self.total_claims == 0:
            return 0.0
        return self.verified / self.total_claims

    @property
    def claim_coverage(self) -> float:
        """Fraction of claims with a conclusive verdict (entailment OR contradiction).
        Neutral / refetch_failed / dropped are not conclusive.
        """
        conclusive = self.verified + self.contradicted
        if self.total_claims == 0:
            return 0.0
        return conclusive / self.total_claims

    @property
    def contradiction_rate(self) -> float:
        if self.total_claims == 0:
            return 0.0
        return self.contradicted / self.total_claims


__all__ = [
    "Claim",
    "ClaimStatus",
    "ClaimType",
    "Verdict",
    "VerificationEvidence",
    "VerificationMetrics",
    "VerificationResult",
]
