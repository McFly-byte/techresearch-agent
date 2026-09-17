"""Hybrid retrieval: BM25 + fake vector + RRF fusion.

We deliberately implement a tiny BM25 and a deterministic fake vector encoder
so the fusion logic is testable without pulling in Chroma/embedding SDK.
A real embedding provider can replace `FakeEncoder` later; the RRF fusion and
dedup logic stays the same.

Each chunk keeps source_id, position, and a content hash so we can re-fetch
the original.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

import httpx

from domain.models import SourceDocument


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Protocol for any embedding backend (fake or real).

    Real network embeddings are async; implement ``aencode``. The sync
    ``encode`` is provided for the deterministic fake and must encode ALL
    inputs in ONE call so query and chunks share the same vocabulary space.
    """

    def encode(self, texts: list[str]) -> list[list[float]]: ...

    async def aencode(self, texts: list[str]) -> list[list[float]]:
        """Async encode. Default implementation runs sync encode on a thread."""
        import asyncio

        return await asyncio.to_thread(self.encode, texts)


@runtime_checkable
class Reranker(Protocol):
    """Protocol for an optional reranker. None means no rerank step."""

    def rerank(self, query: str, chunks: list[Chunk]) -> list[Chunk]: ...


class FakeReranker:
    """Identity reranker: returns chunks in the order given. No-op."""

    def rerank(self, query: str, chunks: list[Chunk]) -> list[Chunk]:
        return chunks


@dataclass
class Chunk:
    chunk_id: str
    source_id: str  # citation_id
    position: int
    text: str
    content_hash: str
    locator: str = ""


def chunk_document(doc: SourceDocument, *, chunk_chars: int = 400) -> list[Chunk]:
    """Split a fetched doc into fixed-char chunks. Keeps citation_id + locator."""
    text = doc.content
    if not text:
        return []
    out: list[Chunk] = []
    for i in range(0, len(text), chunk_chars):
        piece = text[i : i + chunk_chars]
        h = hashlib.sha256(piece.encode("utf-8")).hexdigest()[:12]
        out.append(
            Chunk(
                chunk_id=f"{doc.citation.citation_id}_{i // chunk_chars}",
                source_id=doc.citation.citation_id,
                position=i // chunk_chars,
                text=piece,
                content_hash=h,
                locator=doc.citation.locator,
            )
        )
    return out


# --- BM25 ------------------------------------------------------------------
def _tokenize(s: str) -> list[str]:
    return [w for w in s.lower().split() if len(w) > 2]


def bm25_scores(
    query: str, chunks: list[Chunk], *, k1: float = 1.5, b: float = 0.75
) -> dict[str, float]:
    """Tiny BM25 over chunks. Returns chunk_id -> score."""
    if not chunks:
        return {}
    q_tokens = _tokenize(query)
    avgdl = sum(len(c.text) for c in chunks) / len(chunks) or 1.0
    # df
    df: dict[str, int] = {}
    for c in chunks:
        for t in set(_tokenize(c.text)):
            df[t] = df.get(t, 0) + 1
    n = len(chunks)
    scores: dict[str, float] = {}
    for c in chunks:
        doc_tokens = _tokenize(c.text)
        score = 0.0
        for q in q_tokens:
            if q not in df:
                continue
            idf = math.log(1 + (n - df[q] + 0.5) / (df[q] + 0.5))
            tf = doc_tokens.count(q)
            denom = tf + k1 * (1 - b + b * len(doc_tokens) / avgdl)
            score += idf * tf * (k1 + 1) / (denom or 1)
        scores[c.chunk_id] = score
    return scores


# --- Fake vector -----------------------------------------------------------
class FakeEncoder:
    """Deterministic bag-of-words vector. Not a real embedding.

    Implements the EmbeddingProvider protocol. To keep query and chunks in a
    SHARED space, callers MUST pass all texts (query + chunks) in ONE encode
    call. ``vector_scores`` does this.
    """

    def encode(self, texts: list[str]) -> list[list[float]]:
        vocab = sorted({w for t in texts for w in _tokenize(t)})
        out: list[list[float]] = []
        for t in texts:
            toks = _tokenize(t)
            v = [float(toks.count(w)) for w in vocab]
            norm = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / norm for x in v])
        return out

    async def aencode(self, texts: list[str]) -> list[list[float]]:
        import asyncio

        return await asyncio.to_thread(self.encode, texts)


# Backwards-compatible alias.
FakeEmbeddingProvider = FakeEncoder


def cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        raise ValueError("dimension mismatch")
    return sum(x * y for x, y in zip(a, b, strict=True))


def vector_scores(query: str, chunks: list[Chunk], encoder: EmbeddingProvider) -> dict[str, float]:
    """Encode query + chunks in ONE batch so they share the same vocab space."""
    if not chunks:
        return {}
    # Shared space: build the matrix once.
    matrix = encoder.encode([query, *[c.text for c in chunks]])
    qv = matrix[0]
    return {c.chunk_id: cosine(qv, matrix[i + 1]) for i, c in enumerate(chunks)}


# --- RRF fusion ------------------------------------------------------------
def rrf_fuse(
    *ranked_lists: list[str],
    k: int = 60,
) -> list[tuple[str, float]]:
    """Reciprocal Rank Fusion. Each ranked_list is a list of chunk_ids (best first).

    Ties (identical RRF scores) are broken deterministically by chunk_id
    ascending, so results are stable across runs.
    """
    scores: dict[str, float] = {}
    for lst in ranked_lists:
        for rank, cid in enumerate(lst):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank + 1)
    # Primary: -score. Secondary: chunk_id ascending (deterministic tie-break).
    return sorted(scores.items(), key=lambda x: (-x[1], x[0]))


def hybrid_retrieve(
    query: str,
    chunks: list[Chunk],
    *,
    top_k: int = 5,
    encoder: EmbeddingProvider | None = None,
    reranker: Reranker | None = None,
) -> list[Chunk]:
    """BM25 + vector + RRF, dedup by chunk_id. Optional rerank step."""
    if not chunks:
        return []
    if top_k <= 0:
        return []
    enc = encoder or FakeEncoder()
    bm = bm25_scores(query, chunks)
    vec = vector_scores(query, chunks, enc)
    bm_ranked = [cid for cid, _ in sorted(bm.items(), key=lambda x: -x[1])]
    vec_ranked = [cid for cid, _ in sorted(vec.items(), key=lambda x: -x[1])]
    fused = rrf_fuse(bm_ranked, vec_ranked)
    by_id = {c.chunk_id: c for c in chunks}
    seen: set[str] = set()
    out: list[Chunk] = []
    for cid, _ in fused:
        if cid in seen:
            continue
        seen.add(cid)
        out.append(by_id[cid])
        if len(out) >= top_k:
            break
    # Optional rerank; if none configured, order is RRF output.
    if reranker is not None:
        out = reranker.rerank(query, out)
    return out


# --- Persistent vector store ------------------------------------------------
class SQLiteVectorStore:
    """Minimal persistent vector store backed by SQLite.

    Vectors are packed as little-endian double BLOBs. Dimension is enforced on
    every read/write so a wrong-shape vector raises ``ValueError`` instead of
    silently truncating. SQLite survives process restarts.
    """

    def __init__(self, db_path: Path, dim: int) -> None:
        self.dim = dim
        self._conn = sqlite3.connect(str(db_path))
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS vectors ("
            "chunk_id TEXT PRIMARY KEY, "
            "vector BLOB, "
            "metadata TEXT, "
            "dim INT)"
        )
        self._conn.commit()

    def upsert(self, items: list[tuple[str, list[float], dict]]) -> None:
        rows: list[tuple[str, bytes, str, int]] = []
        for chunk_id, vec, meta in items:
            if len(vec) != self.dim:
                raise ValueError(f"dimension mismatch: expected {self.dim}, got {len(vec)}")
            blob = struct.pack(f"<{len(vec)}d", *vec)
            rows.append((chunk_id, blob, json.dumps(meta), self.dim))
        self._conn.executemany(
            "INSERT INTO vectors (chunk_id, vector, metadata, dim) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(chunk_id) DO UPDATE SET "
            "vector=excluded.vector, metadata=excluded.metadata, dim=excluded.dim",
            rows,
        )
        self._conn.commit()

    def search(self, query_vec: list[float], k: int = 10) -> list[tuple[str, float]]:
        if len(query_vec) != self.dim:
            raise ValueError(f"dimension mismatch: expected {self.dim}, got {len(query_vec)}")
        cur = self._conn.execute("SELECT chunk_id, vector FROM vectors")
        scored: list[tuple[str, float]] = []
        for chunk_id, blob in cur.fetchall():
            vec = list(struct.unpack(f"<{self.dim}d", blob))
            scored.append((chunk_id, cosine(query_vec, vec)))
        scored.sort(key=lambda x: -x[1])
        return scored[:k]

    def close(self) -> None:
        self._conn.close()


# --- Real embedding provider ------------------------------------------------
class QwenEmbeddingProvider:
    """OpenAI-compatible embedding provider (Qwen DashScope compatible mode).

    Posts ``{"model": ..., "input": texts}`` to ``{base_url}/embeddings`` and
    parses ``{"data": [{"embedding": [...]}, ...]}``. An injectable
    ``httpx.AsyncClient`` allows tests to use a ``MockTransport``.
    """

    def __init__(
        self,
        *,
        model: str,
        base_url: str,
        api_key: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._client = client

    async def aencode(self, texts: list[str]) -> list[list[float]]:
        url = f"{self.base_url}/embeddings"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {"model": self.model, "input": texts}
        if self._client is not None:
            resp = await self._client.post(url, json=payload, headers=headers)
        else:
            async with httpx.AsyncClient() as client:
                resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        return [item["embedding"] for item in data["data"]]


# --- Async hybrid retrieve --------------------------------------------------
async def ahybrid_retrieve(
    query: str,
    chunks: list[Chunk],
    *,
    top_k: int = 5,
    encoder: EmbeddingProvider | None = None,
    reranker: Reranker | None = None,
) -> list[Chunk]:
    """Async version of ``hybrid_retrieve``. Uses ``encoder.aencode()``.

    Encodes query + chunks in ONE batch so they share the same vector space,
    then reuses BM25 + cosine + RRF fusion.
    """
    if not chunks:
        return []
    if top_k <= 0:
        return []
    enc = encoder or FakeEncoder()
    bm = bm25_scores(query, chunks)
    matrix = await enc.aencode([query, *[c.text for c in chunks]])
    qv = matrix[0]
    vec = {c.chunk_id: cosine(qv, matrix[i + 1]) for i, c in enumerate(chunks)}
    bm_ranked = [cid for cid, _ in sorted(bm.items(), key=lambda x: -x[1])]
    vec_ranked = [cid for cid, _ in sorted(vec.items(), key=lambda x: -x[1])]
    fused = rrf_fuse(bm_ranked, vec_ranked)
    by_id = {c.chunk_id: c for c in chunks}
    seen: set[str] = set()
    out: list[Chunk] = []
    for cid, _ in fused:
        if cid in seen:
            continue
        seen.add(cid)
        out.append(by_id[cid])
        if len(out) >= top_k:
            break
    if reranker is not None:
        out = reranker.rerank(query, out)
    return out


__all__ = [
    "Chunk",
    "EmbeddingProvider",
    "FakeEncoder",
    "FakeEmbeddingProvider",
    "FakeReranker",
    "QwenEmbeddingProvider",
    "Reranker",
    "SQLiteVectorStore",
    "ahybrid_retrieve",
    "chunk_document",
    "hybrid_retrieve",
]
