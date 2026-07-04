"""SQLite cache for per-file graph extraction results.

Avoids re-running LLM extraction on unchanged files — the single largest
performance gain for large repos (300GB+).

Cache key: (file_hash, model_name, schema_version, graphify_version)
Cache value: JSON fragment of extracted nodes + edges for that file.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

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


def _compute_file_hash(path: Path) -> str:
    """Compute SHA256 of full file content for extraction cache key."""
    h = hashlib.sha256()
    try:
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


def _compute_schema_version(
    allowed_node_types: tuple[str, ...] | None = None,
    allowed_relation_types: tuple[str, ...] | None = None,
) -> str:
    """Compute a version hash from the schema definition."""
    parts = []
    if allowed_node_types:
        parts.append("n:" + ",".join(sorted(allowed_node_types)))
    if allowed_relation_types:
        parts.append("r:" + ",".join(sorted(allowed_relation_types)))
    if not parts:
        return "unversioned"
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:12]


class ExtractionCache:
    """SQLite-backed cache for graph extraction fragments."""

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
            self._conn.execute(_sql("execute_81.sql"))
            self._conn.executescript(_SCHEMA)
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def get_cached_fragment(
        self,
        file_hash: str,
        model_name: str,
        schema_version: str,
        graphify_version: str,
    ) -> dict[str, Any] | None:
        """Retrieve cached extraction fragment, or None if not cached."""
        with self._lock:
            conn = self._get_conn()
            row = conn.execute(
                _sql("execute_101.sql"),
                (file_hash, model_name, schema_version, graphify_version),
            ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            return None

    def store_fragment(
        self,
        file_hash: str,
        model_name: str,
        schema_version: str,
        graphify_version: str,
        fragment: dict[str, Any],
    ) -> None:
        """Store an extraction fragment in the cache."""
        with self._lock:
            conn = self._get_conn()
            conn.execute(
                _sql("execute_124.sql"),
                (file_hash, model_name, schema_version, graphify_version, json.dumps(fragment, ensure_ascii=False), time.time()),
            )
            conn.commit()

    def store_fragments_batch(
        self,
        entries: list[tuple[str, str, str, str, dict[str, Any]]],
    ) -> None:
        """Batch store multiple extraction fragments."""
        if not entries:
            return
        now = time.time()
        with self._lock:
            conn = self._get_conn()
            conn.executemany(
                _sql("executemany_145.sql"),
                [
                    (fh, mn, sv, gv, json.dumps(frag, ensure_ascii=False), now)
                    for fh, mn, sv, gv, frag in entries
                ],
            )
            conn.commit()

    def get_cached_file_hashes(
        self,
        model_name: str,
        schema_version: str,
        graphify_version: str,
    ) -> set[str]:
        """Return all file_hashes currently cached for a model+schema+version combo."""
        with self._lock:
            conn = self._get_conn()
            rows = conn.execute(
                _sql("execute_168.sql"),
                (model_name, schema_version, graphify_version),
            ).fetchall()
        return {r[0] for r in rows}

    def cleanup_old(self, max_age_days: int = 90) -> int:
        """Remove entries older than max_age_days. Returns count of removed entries."""
        cutoff = time.time() - (max_age_days * 86400)
        with self._lock:
            conn = self._get_conn()
            cur = conn.execute(
                _sql("execute_180.sql"), (cutoff,)
            )
            conn.commit()
            return cur.rowcount

    @property
    def stats(self) -> dict[str, int]:
        """Return cache statistics."""
        with self._lock:
            conn = self._get_conn()
            total = conn.execute(_sql("execute_190.sql")).fetchone()[0]
            models = conn.execute(_sql("execute_191.sql")).fetchone()[0]
        return {"total_entries": total, "distinct_models": models}


# Module-level convenience
compute_file_hash = _compute_file_hash
compute_schema_version = _compute_schema_version
