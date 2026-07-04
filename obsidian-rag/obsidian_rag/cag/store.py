"""SQLite-backed store for CAG context packs.

Same patterns as IngestManifest: WAL mode, threading.Lock,
transactional writes, lazy connection.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

_SQL_DIR = Path(__file__).resolve().parent / "sql"
_SQL_CACHE = {}


def _sql(name: str) -> str:
    text = _SQL_CACHE.get(name)
    if text is None:
        text = (_SQL_DIR / name).read_text(encoding="utf-8").strip()
        _SQL_CACHE[name] = text
    return text


log = logging.getLogger(__name__)

_SCHEMA = _sql("schema.sql")


@dataclass(frozen=True)
class Pack:
    """A single cached context pack."""
    pack_type: str
    scope: str
    content: str
    content_hash: str
    source_hash: str
    config_version: str
    model_version: str
    ttl_seconds: int
    created_at: float
    expires_at: float
    metadata: dict


def content_hash(text: str) -> str:
    """SHA-256 hash of pack content."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class PackStore:
    """SQLite store for context packs with TTL-based expiry."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(
                self._db_path,
                timeout=10,
                check_same_thread=False,
            )
            self._conn.execute(_sql("execute_87.sql"))
            self._conn.executescript(_SCHEMA)
        return self._conn

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Cursor]:
        with self._lock:
            conn = self._get_conn()
            cursor = conn.cursor()
            try:
                yield cursor
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # -- Pack CRUD --

    def store_pack(
        self,
        pack_type: str,
        content: str,
        *,
        scope: str = "global",
        source_hash: str = "",
        config_version: str = "",
        model_version: str = "",
        ttl_seconds: int = 3600,
        metadata: dict | None = None,
    ) -> Pack:
        """Store or update a context pack. Returns the stored Pack."""
        now = time.time()
        c_hash = content_hash(content)
        expires = now + ttl_seconds
        meta_json = json.dumps(metadata or {}, ensure_ascii=False)

        with self._tx() as cur:
            cur.execute(
                _sql("execute_130.sql"),
                (pack_type, scope, content, c_hash, source_hash,
                 config_version, model_version, ttl_seconds,
                 now, expires, meta_json),
            )

        log.info("CAG: stored pack %s/%s (ttl=%ds, hash=%s)", pack_type, scope, ttl_seconds, c_hash[:8])

        from obsidian_rag.observability import emit, is_enabled
        if is_enabled():
            from obsidian_rag.observability import EventName, RAGEvent
            emit(RAGEvent(
                event=EventName.CAG_PACK_STORE,
                operation="store",
                pack_type=pack_type,
                pack_scope=scope,
                ttl_remaining=float(ttl_seconds),
            ))

        return Pack(
            pack_type=pack_type, scope=scope, content=content,
            content_hash=c_hash, source_hash=source_hash,
            config_version=config_version, model_version=model_version,
            ttl_seconds=ttl_seconds, created_at=now, expires_at=expires,
            metadata=metadata or {},
        )

    def get_pack(self, pack_type: str, scope: str = "global") -> Pack | None:
        """Retrieve a pack by type and scope. Returns None if not found."""
        t0 = time.time()
        with self._lock:
            conn = self._get_conn()
            row = conn.execute(
                _sql("execute_177.sql"),
                (pack_type, scope),
            ).fetchone()

        hit = row is not None
        from obsidian_rag.observability import emit, is_enabled
        if is_enabled():
            from obsidian_rag.observability import EventName, RAGEvent
            emit(RAGEvent(
                event=EventName.CAG_PACK_GET,
                operation="get",
                pack_type=pack_type,
                pack_scope=scope,
                cache_hit=hit,
                latency_ms=(time.time() - t0) * 1000,
            ))

        if row is None:
            return None

        return Pack(
            pack_type=pack_type, scope=scope,
            content=row[0], content_hash=row[1], source_hash=row[2],
            config_version=row[3], model_version=row[4],
            ttl_seconds=row[5], created_at=row[6], expires_at=row[7],
            metadata=json.loads(row[8]),
        )

    def is_fresh(self, pack_type: str, scope: str = "global") -> bool:
        """Check if a pack exists and has not expired (TTL only)."""
        pack = self.get_pack(pack_type, scope)
        if pack is None:
            return False
        return time.time() < pack.expires_at

    def invalidate(self, pack_type: str, scope: str = "global") -> None:
        """Delete a specific pack."""
        with self._tx() as cur:
            cur.execute(_sql("execute_217.sql"), (pack_type, scope))

    def invalidate_type(self, pack_type: str) -> None:
        """Delete all packs of a given type (all scopes)."""
        with self._tx() as cur:
            cur.execute(_sql("execute_222.sql"), (pack_type,))

    def invalidate_all(self) -> None:
        """Delete all packs."""
        with self._tx() as cur:
            cur.execute(_sql("execute_227.sql"))
        log.info("CAG: all packs invalidated")

    def list_packs(self) -> list[Pack]:
        """Return all stored packs (including expired)."""
        with self._lock:
            conn = self._get_conn()
            rows = conn.execute(
                _sql("execute_235.sql")
            ).fetchall()

        return [
            Pack(
                pack_type=r[0], scope=r[1], content=r[2], content_hash=r[3],
                source_hash=r[4], config_version=r[5], model_version=r[6],
                ttl_seconds=r[7], created_at=r[8], expires_at=r[9],
                metadata=json.loads(r[10]),
            )
            for r in rows
        ]

    def count_packs(self) -> int:
        """Return total number of stored packs."""
        with self._lock:
            conn = self._get_conn()
            row = conn.execute(_sql("execute_254.sql")).fetchone()
        return row[0] if row else 0

    def count_fresh(self) -> int:
        """Return number of non-expired packs."""
        now = time.time()
        with self._lock:
            conn = self._get_conn()
            row = conn.execute(
                _sql("execute_263.sql"), (now,)
            ).fetchone()
        return row[0] if row else 0

    def cleanup_expired(self) -> int:
        """Delete expired packs. Returns count deleted."""
        now = time.time()
        with self._tx() as cur:
            cur.execute(_sql("execute_271.sql"), (now,))
            deleted = cur.rowcount
        if deleted:
            log.info("CAG: cleaned up %d expired packs", deleted)
        return deleted

    # -- Response cache --

    def cache_response(
        self,
        query_hash: str,
        response: str,
        context_hash: str,
        model: str,
        config_version: str = "",
        ttl_seconds: int = 600,
    ) -> None:
        """Cache a response for a specific query+context combination."""
        now = time.time()
        with self._tx() as cur:
            cur.execute(
                _sql("execute_292.sql"),
                (query_hash, response, context_hash, model, config_version,
                 now, ttl_seconds),
            )

    def get_cached_response(
        self,
        query_hash: str,
        context_hash: str,
        model: str,
    ) -> str | None:
        """Retrieve a cached response if fresh and matching context+model."""
        now = time.time()
        with self._lock:
            conn = self._get_conn()
            row = conn.execute(
                _sql("execute_318.sql"),
                (query_hash,),
            ).fetchone()

        if row is None:
            self._emit_cache_event(hit=False)
            return None

        resp, created_at, ttl, cached_ctx, cached_model = row
        if now > created_at + ttl:
            self._emit_cache_event(hit=False)
            return None
        if cached_ctx != context_hash or cached_model != model:
            self._emit_cache_event(hit=False)
            return None
        self._emit_cache_event(hit=True)
        return resp

    def _emit_cache_event(self, hit: bool) -> None:
        from obsidian_rag.observability import emit, is_enabled
        if is_enabled():
            from obsidian_rag.observability import EventName, RAGEvent
            emit(RAGEvent(
                event=EventName.CAG_RESPONSE_CACHE,
                operation="response_cache",
                cache_hit=hit,
            ))

    def clear_response_cache(self) -> int:
        """Delete all cached responses."""
        with self._tx() as cur:
            cur.execute(_sql("execute_350.sql"))
            return cur.rowcount
