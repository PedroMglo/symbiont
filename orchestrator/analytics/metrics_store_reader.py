"""Wrapper around MetricsStore for analytics with source tagging.

Handles the case where metrics.db doesn't exist gracefully.
"""

from __future__ import annotations

import logging
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


class MetricsStoreReader:
    """Read-only analytics access to the observability metrics store.

    Wraps the existing MetricsStore and adds source tagging.
    If metrics.db doesn't exist or the store isn't initialized, returns empty results.
    """

    def __init__(self, db_path: str | Path | None = None) -> None:
        self._db_path = Path(db_path).expanduser() if db_path else None
        self._store = None
        self._available = False
        self._connect()

    def _connect(self) -> None:
        """Try to connect to metrics.db."""
        # First try the global store singleton
        try:
            from orchestrator.observability.store import get_store
            store = get_store()
            if store is not None:
                self._store = store
                self._available = True
                return
        except ImportError:
            pass

        # Fall back to opening directly if path provided and file exists
        if self._db_path and self._db_path.exists():
            try:
                from orchestrator.observability.metrics_config import MetricsConfig
                from orchestrator.observability.store import MetricsStore
                cfg = MetricsConfig(db_path=str(self._db_path))
                self._store = MetricsStore(cfg)
                self._available = True
            except Exception as exc:
                log.warning("MetricsStoreReader: failed to open %s: %s", self._db_path, exc)

    @property
    def available(self) -> bool:
        return self._available and self._store is not None

    def close(self) -> None:
        # Don't close the global singleton — only close if we opened it ourselves
        pass

    # ------------------------------------------------------------------
    # Delegated queries with source tagging
    # ------------------------------------------------------------------

    def summary(self, days: int = 7) -> dict[str, Any]:
        if not self.available:
            return {}
        result = self._store.summary(days=days)
        result["source"] = "metrics_db"
        return result

    def timeline(self, days: int = 7, resolution: str = "day") -> list[dict]:
        if not self.available:
            return []
        return self._store.timeline(days=days, resolution=resolution)

    def by_model(self, days: int = 7) -> list[dict]:
        if not self.available:
            return []
        return self._store.by_model(days=days)

    def by_backend(self, days: int = 7) -> list[dict]:
        if not self.available:
            return []
        return self._store.by_backend(days=days)

    def by_session(self, days: int = 7, limit: int = 50) -> list[dict]:
        if not self.available:
            return []
        return self._store.by_session(days=days, limit=limit)

    def by_intent(self, days: int = 7) -> list[dict]:
        if not self.available:
            return []
        return self._store.by_intent(days=days)

    def fallbacks(self, days: int = 7) -> list[dict]:
        if not self.available:
            return []
        return self._store.fallbacks(days=days)

    def errors(self, days: int = 7) -> list[dict]:
        if not self.available:
            return []
        return self._store.errors(days=days)

    def performance(self, days: int = 7) -> dict[str, Any]:
        if not self.available:
            return {}
        return self._store.performance(days=days)

    def recent(self, limit: int = 20) -> list[dict]:
        if not self.available:
            return []
        return self._store.recent(limit=limit)

    def export_csv_rows(self, days: int = 30) -> list:
        if not self.available:
            return []
        return self._store.export_csv_rows(days=days)

    def session_metrics(self, session_id: str) -> dict[str, Any]:
        """Get metrics for a specific session from metrics.db."""
        if not self.available:
            return {}
        try:
            # Query metrics for this session
            rows = self._store._conn.execute(_sql("execute_127.sql"), (session_id,)).fetchone()

            if not rows or rows["request_count"] == 0:
                return {}

            return {
                "request_count": rows["request_count"],
                "total_tokens": rows["total_tokens"],
                "prompt_tokens": rows["prompt_tokens"],
                "completion_tokens": rows["completion_tokens"],
                "avg_latency_ms": round(rows["avg_latency_ms"], 1),
                "models_used": rows["models_used"].split(",") if rows["models_used"] else [],
                "backends_used": rows["backends_used"].split(",") if rows["backends_used"] else [],
                "fallbacks": rows["fallbacks"],
                "errors": rows["errors"],
                "agentic_calls": rows["agentic_calls"],
                "rag_calls": rows["rag_calls"],
                "stream_calls": rows["stream_calls"],
                "source": "metrics_db",
            }
        except Exception:
            return {}
