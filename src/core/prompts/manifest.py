"""Local manifest for the Prompt Hub.

The manifest is the auditable, reviewable source of truth for the LOCAL
fallback prompts. It is a JSON file committed to the repo. Every prompt that
can be served by the registry in ``local`` or ``hybrid`` mode MUST have an
entry here with its full template text.

A separate *lock* file (``manifest.lock.json``) records, for each prompt, the
last commit hash and tags that were pushed to LangSmith. It is also safe to
commit (no secrets) and lets ``tra prompts sync`` be idempotent.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .spec import PromptSpec

# Default manifest location: src/core/prompts/manifest.json
_MANIFEST_PATH = Path(__file__).resolve().parent / "manifest.json"
_LOCK_PATH = Path(__file__).resolve().parent / "manifest.lock.json"


@dataclass(frozen=True)
class ManifestEntry:
    """One entry in the manifest file."""

    name: str
    description: str
    version: str
    messages: tuple[tuple[str, str], ...]
    variables: tuple[str, ...]

    def to_spec(self) -> PromptSpec:
        return PromptSpec(
            name=self.name,
            description=self.description,
            version=self.version,
            messages=self.messages,
            variables=self.variables,
        )


@dataclass(frozen=True)
class LockEntry:
    """One entry in the lock file: what was last pushed to LangSmith."""

    name: str
    commit_hash: str
    tags: tuple[str, ...]
    pushed_at: str
    identifier: str


def load_manifest(path: Path | None = None) -> dict[str, ManifestEntry]:
    """Load all manifest entries keyed by prompt name."""
    p = path or _MANIFEST_PATH
    if not p.is_file():
        return {}
    data = json.loads(p.read_text(encoding="utf-8"))
    out: dict[str, ManifestEntry] = {}
    for name, raw in data.get("prompts", {}).items():
        messages = tuple((m["role"], m["template"]) for m in raw.get("messages", []))
        variables = tuple(raw.get("variables", []))
        out[name] = ManifestEntry(
            name=name,
            description=raw.get("description", ""),
            version=raw.get("version", "0.0.0"),
            messages=messages,
            variables=variables,
        )
    return out


def save_manifest(entries: dict[str, ManifestEntry], path: Path | None = None) -> None:
    """Write the manifest file (used by sync when we pull a remote prompt down)."""
    p = path or _MANIFEST_PATH
    payload: dict[str, Any] = {
        "version": "1",
        "prompts": {
            name: {
                "description": e.description,
                "version": e.version,
                "variables": list(e.variables),
                "messages": [{"role": r, "template": t} for r, t in e.messages],
            }
            for name, e in sorted(entries.items())
        },
    }
    p.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def load_lock(path: Path | None = None) -> dict[str, LockEntry]:
    """Load the lock file: prompt name -> last pushed commit/tags."""
    p = path or _LOCK_PATH
    if not p.is_file():
        return {}
    data = json.loads(p.read_text(encoding="utf-8"))
    out: dict[str, LockEntry] = {}
    for name, raw in data.get("prompts", {}).items():
        out[name] = LockEntry(
            name=name,
            commit_hash=raw.get("commit_hash", ""),
            tags=tuple(raw.get("tags", [])),
            pushed_at=raw.get("pushed_at", ""),
            identifier=raw.get("identifier", name),
        )
    return out


def save_lock(entries: dict[str, LockEntry], path: Path | None = None) -> None:
    """Write the lock file (called after a successful sync/promote)."""
    p = path or _LOCK_PATH
    payload: dict[str, Any] = {
        "version": "1",
        "prompts": {
            name: {
                "commit_hash": e.commit_hash,
                "tags": list(e.tags),
                "pushed_at": e.pushed_at,
                "identifier": e.identifier,
            }
            for name, e in sorted(entries.items())
        },
    }
    p.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


__all__ = [
    "LockEntry",
    "ManifestEntry",
    "load_lock",
    "load_manifest",
    "now_iso",
    "save_lock",
    "save_manifest",
]
