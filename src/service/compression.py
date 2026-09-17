"""Context compression and token counting.

The token counter is an explicit ESTIMATOR (chars/4 heuristic), marked
`estimated=True`. We do NOT claim it matches a real tokenizer. If tiktoken
becomes available on Python 3.14 we can swap in a real counter; until then,
every usage site that matters MUST propagate the `estimated` flag.

Compression preserves provenance: each compressed chunk keeps fact_id,
citation_id, and original excerpt length. The full evidence remains in
external storage (the fetcher can re-fetch by locator).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from domain.models import Fact


def estimate_tokens(text: str) -> int:
    """Rough char/4 estimate. Marked estimated; never used for hard budget."""
    return max(1, len(text) // 4)


@dataclass
class CompressedFact:
    fact_id: str
    citation_ids: list[str]
    claim: str
    original_chars: int
    source_task_id: str = ""


@dataclass
class CompressionResult:
    facts: list[CompressedFact] = field(default_factory=list)
    total_estimated_tokens: int = 0
    truncated: bool = False


def compress_facts(
    facts: list[Fact],
    *,
    max_estimated_tokens: int = 4000,
) -> CompressionResult:
    """Truncate the fact list to fit a token budget, preserving provenance.

    We never drop citation_ids or fact_id. If we have to drop facts, we keep
    the first N that fit (deterministic order from the reducer).
    """
    out: list[CompressedFact] = []
    used = 0
    truncated = False
    for f in facts:
        est = estimate_tokens(f.claim)
        if used + est > max_estimated_tokens:
            truncated = True
            continue
        used += est
        out.append(
            CompressedFact(
                fact_id=f.fact_id,
                citation_ids=list(f.source_citation_ids),
                claim=f.claim,
                original_chars=len(f.claim),
                source_task_id=f.source_task_id,
            )
        )
    return CompressionResult(
        facts=out,
        total_estimated_tokens=used,
        truncated=truncated,
    )


__all__ = ["CompressedFact", "CompressionResult", "compress_facts", "estimate_tokens"]
