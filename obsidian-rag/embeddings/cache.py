"""Persistent embedding cache — reuse embeddings for unchanged text across runs.

Keyed by ``(text_sha256, model)``. Survives process restarts, so re-ingesting
a file whose chunks did not change costs zero Ollama calls. This is the single
biggest lever against re-embedding work at 300GB+ scale: even when a file's
mtime/size change (triggering a reparse), only the chunks whose *text* actually
changed incur an embedding call.

Backed by SQLite (WAL) — point lookups are the hot path, which is exactly what
SQLite excels at. Values are stored as little-endian float32 blobs to keep the
DB compact (≈4 bytes/dim vs ≈20 for JSON).
"""

from __future__ import annotations

import array
import hashlib
import logging
import sqlite3
import threading
from pathlib import Path

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


def text_sha256(text: str) -> str:
    """Stable content hash of a chunk's text (used as the cache key)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class EmbeddingCache:
    """SQLite-backed persistent cache mapping ``(text_hash, model) -> vector``.

    Thread-safe: a single connection guarded by a lock (writes serialised),
    which matches the streaming pipeline's access pattern.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self._db_path, timeout=10, check_same_thread=False)
            self._conn.execute(_sql("execute_60.sql"))
            self._conn.execute(_sql("execute_61.sql"))
            self._conn.executescript(_SCHEMA)
        return self._conn

    @staticmethod
    def _encode(vec: list[float]) -> bytes:
        return array.array("f", vec).tobytes()

    @staticmethod
    def _decode(blob: bytes) -> list[float]:
        a = array.array("f")
        a.frombytes(blob)
        return list(a)

    def get_many(self, hashes: list[str], model: str) -> dict[str, list[float]]:
        """Return cached vectors for the subset of *hashes* present."""
        if not hashes:
            return {}
        out: dict[str, list[float]] = {}
        with self._lock:
            conn = self._get_conn()
            # Chunk the IN clause to stay well under SQLite's variable limit.
            for i in range(0, len(hashes), 500):
                window = hashes[i : i + 500]
                placeholders = ",".join("?" for _ in window)
                rows = conn.execute(
                    _sql("fstring_90.sql").format(placeholders),
                    [model, *window],
                ).fetchall()
                for h, blob in rows:
                    out[h] = self._decode(blob)
        self.hits += len(out)
        self.misses += len(hashes) - len(out)
        return out

    def put_many(self, items: list[tuple[str, list[float]]], model: str) -> None:
        """Insert or replace cached vectors for ``(text_hash, vector)`` items."""
        if not items:
            return
        with self._lock:
            conn = self._get_conn()
            conn.executemany(
                _sql("executemany_104.sql"),
                [(h, model, len(vec), self._encode(vec)) for h, vec in items],
            )
            conn.commit()

    def stats(self) -> dict[str, int]:
        with self._lock:
            conn = self._get_conn()
            total = conn.execute(_sql("execute_113.sql")).fetchone()[0]
        return {"entries": total, "hits": self.hits, "misses": self.misses}

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
