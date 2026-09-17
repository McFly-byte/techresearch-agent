"""Long-term memory store with namespace isolation + async adapter.

Stage 4 P0 hardening:
- Pydantic ``MemoryRecord`` with type (preference / conclusion_summary),
  provenance, explicit consent flag, and timestamps. Only records with
  ``consent=True`` and an allowed type AND an allow-listed key are persisted.
- The ONLY public write path is ``put_record`` / ``aput_record``. The old raw
  ``put`` / ``aput`` (which accepted arbitrary key/value and was the bypass that
  wrote ``raw_document_body={'text':'PRIVATE'}``) is REMOVED. There is no way to
  persist a raw document body.
- The DB row stores ``type`` / ``provenance`` / ``consent`` (previously ``put``
  hard-coded ``type='preference'`` and ``get`` returned only the value, so the
  audit fields were lost). ``get_record`` / ``list_records`` return the full
  ``MemoryRecord``.
- Namespace validation: must be ``user/<id>`` / ``project/<id>`` /
  ``session/<id>``. No bare keys, no cross-namespace leakage.
- ``AsyncLongTermStore`` wraps the sync SQLite store via
  ``asyncio.to_thread`` so the asyncio event loop is never blocked.
- SQLite uses WAL mode + a connection lock for thread safety.
- Default OFF: the store only persists what callers explicitly opt in to.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

MemoryType = Literal["preference", "conclusion_summary"]
_ALLOWED_TYPES: frozenset[str] = frozenset({"preference", "conclusion_summary"})
_ALLOWED_NAMESPACE_PREFIXES: tuple[str, ...] = ("user/", "project/", "session/")


class MemoryRecord(BaseModel):
    """One persisted memory entry. Only records with consent=True are written."""

    type: MemoryType
    key: str
    value: dict[str, Any]
    provenance: str = "explicit_user"  # who / what consented
    consent: bool = False
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


def _validate_namespace(ns: str) -> str:
    if not ns.startswith(_ALLOWED_NAMESPACE_PREFIXES):
        raise ValueError(
            f"invalid namespace {ns!r}: must start with one of {_ALLOWED_NAMESPACE_PREFIXES}"
        )
    for prefix in _ALLOWED_NAMESPACE_PREFIXES:
        if ns.startswith(prefix):
            id_part = ns[len(prefix) :]
            if not id_part or "/" in id_part:
                raise ValueError(f"invalid namespace {ns!r}: expected {prefix}<id>")
            return ns
    raise ValueError(f"invalid namespace {ns!r}")


class LongTermStore:
    """Namespace-isolated key-value store, optionally SQLite-backed.

    Public write API: ONLY ``put_record``. Raw arbitrary writes are not
    available — the old ``put`` was the consent-bypass path (it persisted
    ``raw_document_body={'text':'PRIVATE'}``). Reads: ``get`` (value only,
    back-compat), ``get_record`` (full MemoryRecord), ``list_records``.
    """

    # Keys callers are allowed to persist (allow-list gate). Raw document
    # bodies are NOT allowed — only explicit prefs / conclusion summaries.
    allowed_keys: frozenset[str] = frozenset(
        {
            "user_theme",
            "user_scope",
            "conclusion_summary",
            "project_pref",
        }
    )

    def __init__(self, *, db_path: str | Path | None = None) -> None:
        self._data: dict[str, dict[str, MemoryRecord]] = {}
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()
        if db_path is not None:
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS kv ("
                " namespace TEXT NOT NULL,"
                " key TEXT NOT NULL,"
                " value_json TEXT NOT NULL,"
                " type TEXT NOT NULL,"
                " provenance TEXT NOT NULL,"
                " consent INTEGER NOT NULL,"
                " created_at TEXT NOT NULL,"
                " updated_at TEXT NOT NULL,"
                " PRIMARY KEY (namespace, key)"
                ")"
            )
            self._conn.commit()

    def is_allowed(self, key: str) -> bool:
        """Gate: only allow-listed keys may be persisted."""
        return key in self.allowed_keys

    # -- writes (the ONLY public write path is put_record) --------------------
    def put_record(self, *, namespace: str, record: MemoryRecord) -> None:
        """Persist a typed MemoryRecord. Rejects non-consented / disallowed."""
        ns = _validate_namespace(namespace)
        if record.type not in _ALLOWED_TYPES:
            raise ValueError(f"record type {record.type!r} not allowed")
        if not record.consent:
            raise PermissionError("refusing to persist a record without explicit consent")
        if not self.is_allowed(record.key):
            raise PermissionError(
                f"key {record.key!r} not in allowed_keys allow-list; raw document "
                "bodies are never persisted"
            )
        self._write_record(ns, record)

    def _write_record(self, ns: str, record: MemoryRecord) -> None:
        """Internal: persist a validated record with full audit columns."""
        now = datetime.now(UTC).isoformat()
        if self._conn is not None:
            with self._lock:
                self._conn.execute(
                    "INSERT OR REPLACE INTO kv"
                    " (namespace, key, value_json, type, provenance, consent,"
                    "  created_at, updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (
                        ns,
                        record.key,
                        json.dumps(record.value, ensure_ascii=False),
                        record.type,
                        record.provenance,
                        1 if record.consent else 0,
                        record.created_at or now,
                        now,
                    ),
                )
                self._conn.commit()
            return
        self._data.setdefault(ns, {})[record.key] = record.model_copy(update={"updated_at": now})

    # -- reads ----------------------------------------------------------------
    @staticmethod
    def _row_to_record(row: tuple) -> MemoryRecord:
        (key, value_json, rtype, provenance, consent, created_at, updated_at) = row
        return MemoryRecord(
            type=rtype,
            key=key,
            value=json.loads(value_json),
            provenance=provenance,
            consent=bool(consent),
            created_at=created_at,
            updated_at=updated_at,
        )

    def get_record(self, *, namespace: str, key: str) -> MemoryRecord | None:
        ns = _validate_namespace(namespace)
        if self._conn is not None:
            with self._lock:
                row = self._conn.execute(
                    "SELECT key, value_json, type, provenance, consent,"
                    " created_at, updated_at FROM kv WHERE namespace=? AND key=?",
                    (ns, key),
                ).fetchone()
            if row is None:
                return None
            return self._row_to_record(row)
        rec = self._data.get(ns, {}).get(key)
        return rec.model_copy() if rec is not None else None

    def get(self, *, namespace: str, key: str) -> Any | None:
        """Back-compat read: returns only the record's value dict."""
        rec = self.get_record(namespace=namespace, key=key)
        return rec.value if rec is not None else None

    def list_records(self, *, namespace: str) -> list[MemoryRecord]:
        ns = _validate_namespace(namespace)
        if self._conn is not None:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT key, value_json, type, provenance, consent,"
                    " created_at, updated_at FROM kv WHERE namespace=? ORDER BY key",
                    (ns,),
                ).fetchall()
            return [self._row_to_record(r) for r in rows]
        return [r.model_copy() for r in self._data.get(ns, {}).values()]

    def delete(self, *, namespace: str, key: str) -> None:
        ns = _validate_namespace(namespace)
        if self._conn is not None:
            with self._lock:
                self._conn.execute("DELETE FROM kv WHERE namespace=? AND key=?", (ns, key))
                self._conn.commit()
            return
        self._data.get(ns, {}).pop(key, None)

    def keys(self, *, namespace: str) -> list[str]:
        ns = _validate_namespace(namespace)
        if self._conn is not None:
            with self._lock:
                rows = self._conn.execute("SELECT key FROM kv WHERE namespace=?", (ns,)).fetchall()
            return [r[0] for r in rows]
        return list(self._data.get(ns, {}).keys())

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None


class AsyncLongTermStore:
    """Async wrapper around LongTermStore using asyncio.to_thread.

    SQLite is synchronous; calling it directly on the event loop would block.
    This adapter runs every sync operation on the default thread pool.
    """

    def __init__(self, inner: LongTermStore) -> None:
        self._inner = inner

    async def aput_record(self, *, namespace: str, record: MemoryRecord) -> None:
        await asyncio.to_thread(self._inner.put_record, namespace=namespace, record=record)

    async def aget(self, *, namespace: str, key: str) -> Any | None:
        return await asyncio.to_thread(self._inner.get, namespace=namespace, key=key)

    async def aget_record(self, *, namespace: str, key: str) -> MemoryRecord | None:
        return await asyncio.to_thread(self._inner.get_record, namespace=namespace, key=key)

    async def alist_records(self, *, namespace: str) -> list[MemoryRecord]:
        return await asyncio.to_thread(self._inner.list_records, namespace=namespace)

    async def adelete(self, *, namespace: str, key: str) -> None:
        await asyncio.to_thread(self._inner.delete, namespace=namespace, key=key)

    async def akeys(self, *, namespace: str) -> list[str]:
        return await asyncio.to_thread(self._inner.keys, namespace=namespace)

    async def aclose(self) -> None:
        await asyncio.to_thread(self._inner.close)


__all__ = ["AsyncLongTermStore", "LongTermStore", "MemoryRecord"]
