"""Fixed benchmark subset manifests.

Manifests contain only stable qids and source hashes.  Benchmark prompts stay in
the canonical dataset, so a subset cannot silently drift or duplicate content.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from evals.adapter import EvalQuestion


@dataclass(frozen=True)
class SubsetManifest:
    name: str
    source_sha256: str
    qid_sha256: str
    qids: tuple[str, ...]
    path: Path

    def metadata(self) -> dict[str, object]:
        return {
            "name": self.name,
            "source_sha256": self.source_sha256,
            "qid_sha256": self.qid_sha256,
            "qids": list(self.qids),
        }


def _qid_hash(qids: list[str]) -> str:
    raw = json.dumps(qids, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def load_subset(name: str, *, root: Path | None = None) -> SubsetManifest:
    """Load and validate a named manifest from ``evals/subsets``."""
    safe_name = name.strip().lower().replace("-", "_")
    if not safe_name or any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789_" for ch in safe_name):
        raise ValueError(f"invalid subset name: {name!r}")
    base = root or Path(__file__).resolve().parent / "subsets"
    path = (
        base / f"drb2_{safe_name}.json"
        if not safe_name.startswith("drb2_")
        else base / f"{safe_name}.json"
    )
    if not path.is_file():
        raise ValueError(f"unknown subset: {name!r}")
    obj = json.loads(path.read_text(encoding="utf-8"))
    qids = [str(qid) for qid in obj.get("qids", []) if str(qid)]
    if not qids or len(qids) != len(set(qids)):
        raise ValueError(f"subset {name!r} has empty or duplicate qids")
    expected = str(obj.get("qid_sha256", ""))
    actual = _qid_hash(qids)
    if expected != actual:
        raise ValueError(f"subset {name!r} qid hash mismatch")
    return SubsetManifest(
        name=str(obj.get("name", safe_name)),
        source_sha256=str(obj.get("source_sha256", "")),
        qid_sha256=actual,
        qids=tuple(qids),
        path=path,
    )


def apply_subset(
    questions: list[EvalQuestion], manifest: SubsetManifest, *, dataset_hash: str
) -> list[EvalQuestion]:
    """Return questions in manifest order, rejecting source or id drift."""
    if dataset_hash != manifest.source_sha256:
        raise ValueError(
            f"subset {manifest.name} requires dataset sha256 {manifest.source_sha256}, "
            f"got {dataset_hash}"
        )
    by_id = {q.qid: q for q in questions}
    missing = [qid for qid in manifest.qids if qid not in by_id]
    if missing:
        raise ValueError(f"subset {manifest.name} qids missing from dataset: {missing}")
    return [by_id[qid] for qid in manifest.qids]


__all__ = ["SubsetManifest", "apply_subset", "load_subset"]
