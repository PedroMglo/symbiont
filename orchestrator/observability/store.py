"""Persistent metrics store — SQLite with WAL mode, async queue, aggregation queries."""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

from orchestrator.observability.metrics_config import MetricsConfig
from orchestrator.observability.models import MetricsEvent

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
_INSERT_SQL = _sql("insert_llm_call_log.sql")


class MetricsStore:
    """SQLite-backed persistent metrics store.

    Thread-safe for single-writer usage. Uses WAL mode for non-blocking reads.
    Designed to never block the Engine — all writes go through a queue
    managed by the Collector.
    """

    def __init__(self, cfg: MetricsConfig) -> None:
        self._cfg = cfg
        self._db_path = cfg.resolved_db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None
        self._init_db()

    def _init_db(self) -> None:
        """Create database and apply schema."""
        self._conn = sqlite3.connect(
            str(self._db_path),
            check_same_thread=False,
            timeout=self._cfg.sqlite.busy_timeout_ms / 1000.0,
        )
        self._conn.row_factory = sqlite3.Row
        schema = _SCHEMA.format(busy_timeout_ms=self._cfg.sqlite.busy_timeout_ms)
        self._conn.executescript(schema)
        self._conn.commit()
        self._migrate_schema()

    def _migrate_schema(self) -> None:
        """Add new columns to existing databases (safe ALTER TABLE)."""
        if self._conn is None:
            return
        # Get existing columns
        cursor = self._conn.execute(_sql("execute_204.sql"))
        existing_cols = {row["name"] for row in cursor.fetchall()}

        new_columns = [
            ("router_latency_ms", "REAL"),
            ("context_build_latency_ms", "REAL"),
            ("model_load_latency_ms", "REAL"),
            ("prompt_eval_latency_ms", "REAL"),
            ("generation_latency_ms", "REAL"),
            ("total_latency_ms", "REAL"),
            ("cold_start", "BOOLEAN NOT NULL DEFAULT 0"),
            ("prompt_tokens_per_second", "REAL"),
            ("generation_tokens_per_second", "REAL"),
            ("total_tokens_per_second", "REAL"),
            ("ollama_total_duration", "INTEGER"),
            ("ollama_load_duration", "INTEGER"),
            ("ollama_prompt_eval_count", "INTEGER"),
            ("ollama_prompt_eval_duration", "INTEGER"),
            ("ollama_eval_count", "INTEGER"),
            ("ollama_eval_duration", "INTEGER"),
            ("profile_key", "TEXT"),
        ]

        for col_name, col_type in new_columns:
            if col_name not in existing_cols:
                try:
                    self._conn.execute(
                        _sql("fstring_134_2.sql").format(col_name, col_type)
                    )
                except sqlite3.OperationalError:
                    pass  # Column already exists (race condition)

        self._conn.commit()

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def insert(self, event: MetricsEvent) -> None:
        """Insert a single MetricsEvent. Called from the flush thread."""
        if self._conn is None:
            return
        rd = event.router_decision
        row = (
            event.request_id,
            event.session_id,
            event.timestamp,
            event.date,
            event.hour,
            event.weekday,
            event.entrypoint,
            event.model,
            event.backend,
            rd.intent,
            rd.complexity,
            rd.requested_model,
            rd.fallback_used,
            rd.fallback_reason,
            json.dumps(list(rd.blocked_backends)) if rd.blocked_backends else None,
            rd.privacy_mode,
            rd.decision_reason,
            event.usage.prompt_tokens,
            event.usage.completion_tokens,
            event.usage.total_tokens,
            event.usage.usage_source,
            event.stream,
            event.chunks_count,
            event.latency_ms,
            event.first_token_latency_ms,
            event.context_latency_ms,
            event.llm_latency_ms,
            event.router_latency_ms,
            event.context_build_latency_ms,
            event.model_load_latency_ms,
            event.prompt_eval_latency_ms,
            event.generation_latency_ms,
            event.total_latency_ms,
            event.cold_start,
            event.prompt_tokens_per_second,
            event.generation_tokens_per_second,
            event.total_tokens_per_second,
            event.ollama_total_duration,
            event.ollama_load_duration,
            event.ollama_prompt_eval_count,
            event.ollama_prompt_eval_duration,
            event.ollama_eval_count,
            event.ollama_eval_duration,
            event.profile_key,
            event.query_length,
            event.response_length,
            event.query_hash,
            event.prompt_preview,
            event.response_preview,
            event.rag_used,
            event.graph_used,
            json.dumps(list(event.tools_used)) if event.tools_used else None,
            event.agentic,
            event.iterations,
            event.success,
            event.error_type,
            event.error_message,
        )
        try:
            self._conn.execute(_INSERT_SQL, row)
            self._conn.commit()
        except sqlite3.Error as exc:
            log.warning("MetricsStore: insert failed: %s", exc)

    def insert_batch(self, events: list[MetricsEvent]) -> None:
        """Insert multiple events in a single transaction."""
        if not events or self._conn is None:
            return
        try:
            for event in events:
                rd = event.router_decision
                row = (
                    event.request_id,
                    event.session_id,
                    event.timestamp,
                    event.date,
                    event.hour,
                    event.weekday,
                    event.entrypoint,
                    event.model,
                    event.backend,
                    rd.intent,
                    rd.complexity,
                    rd.requested_model,
                    rd.fallback_used,
                    rd.fallback_reason,
                    json.dumps(list(rd.blocked_backends)) if rd.blocked_backends else None,
                    rd.privacy_mode,
                    rd.decision_reason,
                    event.usage.prompt_tokens,
                    event.usage.completion_tokens,
                    event.usage.total_tokens,
                    event.usage.usage_source,
                    event.stream,
                    event.chunks_count,
                    event.latency_ms,
                    event.first_token_latency_ms,
                    event.context_latency_ms,
                    event.llm_latency_ms,
                    event.router_latency_ms,
                    event.context_build_latency_ms,
                    event.model_load_latency_ms,
                    event.prompt_eval_latency_ms,
                    event.generation_latency_ms,
                    event.total_latency_ms,
                    event.cold_start,
                    event.prompt_tokens_per_second,
                    event.generation_tokens_per_second,
                    event.total_tokens_per_second,
                    event.ollama_total_duration,
                    event.ollama_load_duration,
                    event.ollama_prompt_eval_count,
                    event.ollama_prompt_eval_duration,
                    event.ollama_eval_count,
                    event.ollama_eval_duration,
                    event.profile_key,
                    event.query_length,
                    event.response_length,
                    event.query_hash,
                    event.prompt_preview,
                    event.response_preview,
                    event.rag_used,
                    event.graph_used,
                    json.dumps(list(event.tools_used)) if event.tools_used else None,
                    event.agentic,
                    event.iterations,
                    event.success,
                    event.error_type,
                    event.error_message,
                )
                self._conn.execute(_INSERT_SQL, row)
            self._conn.commit()
        except sqlite3.Error as exc:
            log.warning("MetricsStore: batch insert failed: %s", exc)

    def cleanup(self, retention_days: int | None = None) -> int:
        """Delete records older than retention_days. Returns count deleted."""
        days = retention_days or self._cfg.retention_days
        cutoff = time.time() - (days * 86400)
        if self._conn is None:
            return 0
        try:
            cur = self._conn.execute(
                _sql("execute_396.sql"), (cutoff,)
            )
            self._conn.execute(
                _sql("execute_399.sql"), (cutoff,)
            )
            self._conn.commit()
            return cur.rowcount
        except sqlite3.Error as exc:
            log.warning("MetricsStore: cleanup failed: %s", exc)
            return 0

    # ------------------------------------------------------------------
    # Read — Aggregation queries
    # ------------------------------------------------------------------

    def _cutoff(self, days: int) -> float:
        return time.time() - (days * 86400)

    def summary(self, days: int = 7) -> dict[str, Any]:
        """High-level summary for the dashboard."""
        if self._conn is None:
            return {}
        cutoff = self._cutoff(days)
        row = self._conn.execute(_sql("execute_419.sql"), (cutoff,)).fetchone()

        total = row["total_queries"] or 0

        # Top model
        top_model_row = self._conn.execute(_sql("execute_438.sql"), (cutoff,)).fetchone()

        # Top backend
        top_backend_row = self._conn.execute(_sql("execute_445.sql"), (cutoff,)).fetchone()

        # Busiest hour
        busiest_hour_row = self._conn.execute(_sql("execute_452.sql"), (cutoff,)).fetchone()

        return {
            "period_days": days,
            "total_queries": total,
            "total_tokens": row["total_tokens"] or 0,
            "total_prompt_tokens": row["total_prompt_tokens"] or 0,
            "total_completion_tokens": row["total_completion_tokens"] or 0,
            "avg_tokens_per_query": round(row["avg_tokens_per_query"] or 0),
            "avg_latency_ms": round(row["avg_latency_ms"] or 0, 1),
            "unique_sessions": row["unique_sessions"] or 0,
            "agentic_queries": row["agentic_queries"] or 0,
            "agentic_ratio": round((row["agentic_queries"] or 0) / max(total, 1), 3),
            "fallback_count": row["fallback_count"] or 0,
            "fallback_rate": round((row["fallback_count"] or 0) / max(total, 1), 3),
            "error_count": row["error_count"] or 0,
            "error_rate": round((row["error_count"] or 0) / max(total, 1), 3),
            "stream_queries": row["stream_queries"] or 0,
            "top_model": {
                "name": top_model_row["model"] if top_model_row else None,
                "queries": top_model_row["cnt"] if top_model_row else 0,
                "tokens": top_model_row["tokens"] if top_model_row else 0,
            },
            "top_backend": {
                "name": top_backend_row["backend"] if top_backend_row else None,
                "queries": top_backend_row["cnt"] if top_backend_row else 0,
            },
            "busiest_hour": busiest_hour_row["hour"] if busiest_hour_row else None,
        }

    def timeline(self, days: int = 7, resolution: str = "day") -> list[dict]:
        """Time-series data grouped by hour or day."""
        if self._conn is None:
            return []
        cutoff = self._cutoff(days)
        if resolution == "hour":
            group_expr = "date || ' ' || printf('%02d', hour) || ':00'"
        else:
            group_expr = "date"

        rows = self._conn.execute(_sql("fstring_373.sql").format(group_expr), (cutoff,)).fetchall()
        return [dict(r) for r in rows]

    def by_model(self, days: int = 7) -> list[dict]:
        """Per-model breakdown."""
        if self._conn is None:
            return []
        cutoff = self._cutoff(days)
        rows = self._conn.execute(_sql("execute_513.sql"), (cutoff,)).fetchall()

        result = []
        for r in rows:
            # Get intent distribution for this model
            intents = self._conn.execute(_sql("execute_531.sql"), (cutoff, r["model"])).fetchall()
            result.append({
                **dict(r),
                "avg_latency_ms": round(r["avg_latency_ms"], 1),
                "intent_distribution": {i["intent"]: i["cnt"] for i in intents},
            })
        return result

    def by_backend(self, days: int = 7) -> list[dict]:
        """Per-backend breakdown."""
        if self._conn is None:
            return []
        cutoff = self._cutoff(days)
        rows = self._conn.execute(_sql("execute_548.sql"), (cutoff,)).fetchall()
        return [{**dict(r), "avg_latency_ms": round(r["avg_latency_ms"], 1)} for r in rows]

    def by_session(self, days: int = 7, limit: int = 50) -> list[dict]:
        """Per-session analytics."""
        if self._conn is None:
            return []
        cutoff = self._cutoff(days)
        rows = self._conn.execute(_sql("execute_566.sql"), (cutoff, limit)).fetchall()
        return [dict(r) for r in rows]

    def by_intent(self, days: int = 7) -> list[dict]:
        """Intent × model matrix — explains why each model is used."""
        if self._conn is None:
            return []
        cutoff = self._cutoff(days)
        rows = self._conn.execute(_sql("execute_586.sql"), (cutoff,)).fetchall()

        result = []
        for r in rows:
            model_dist = self._conn.execute(_sql("execute_597.sql"), (cutoff, r["intent"])).fetchall()
            result.append({
                "intent": r["intent"],
                "total_queries": r["total_queries"],
                "model_distribution": {m["model"]: m["cnt"] for m in model_dist},
            })
        return result

    def fallbacks(self, days: int = 7) -> list[dict]:
        """Fallback event analysis."""
        if self._conn is None:
            return []
        cutoff = self._cutoff(days)
        rows = self._conn.execute(_sql("execute_614.sql"), (cutoff,)).fetchall()
        return [dict(r) for r in rows]

    def errors(self, days: int = 7) -> list[dict]:
        """Error breakdown."""
        if self._conn is None:
            return []
        cutoff = self._cutoff(days)
        rows = self._conn.execute(_sql("execute_632.sql"), (cutoff,)).fetchall()
        return [dict(r) for r in rows]

    def performance(self, days: int = 7) -> dict[str, Any]:
        """Latency percentiles and performance stats."""
        if self._conn is None:
            return {}
        cutoff = self._cutoff(days)

        # Overall latencies
        latencies = self._conn.execute(_sql("execute_652.sql"), (cutoff,)).fetchall()

        if not latencies:
            return {"p50_ms": 0, "p95_ms": 0, "p99_ms": 0, "by_model": []}

        lats = [r["latency_ms"] for r in latencies]
        n = len(lats)

        # First token latencies
        ftl_rows = self._conn.execute(_sql("execute_665.sql"), (cutoff,)).fetchall()
        ftl = [r["first_token_latency_ms"] for r in ftl_rows]

        # Per-model breakdown
        by_model = self._conn.execute(_sql("execute_673.sql"), (cutoff,)).fetchall()

        # Latency buckets for histogram
        buckets = [
            (0, 1000, "<1s"),
            (1000, 2000, "1-2s"),
            (2000, 5000, "2-5s"),
            (5000, 10000, "5-10s"),
            (10000, 20000, "10-20s"),
            (20000, 50000, "20-50s"),
            (50000, float("inf"), ">50s"),
        ]
        latency_buckets = []
        for lo, hi, label in buckets:
            count = sum(1 for lat_ in lats if lo <= lat_ < hi)
            if count > 0:
                latency_buckets.append({"range": label, "count": count})

        return {
            "p50_ms": round(lats[int(n * 0.50)], 1),
            "p95_ms": round(lats[int(n * 0.95) - 1], 1) if n > 1 else round(lats[0], 1),
            "p99_ms": round(lats[int(n * 0.99) - 1], 1) if n > 1 else round(lats[0], 1),
            "avg_ms": round(sum(lats) / n, 1),
            "first_token_p50_ms": round(ftl[len(ftl) // 2], 1) if ftl else None,
            "total_measured": n,
            "latency_buckets": latency_buckets,
            "by_model": [{**dict(r), "avg_ms": round(r["avg_ms"], 1)} for r in by_model],
        }

    def recent(self, limit: int = 20) -> list[dict]:
        """Most recent events for the live feed."""
        if self._conn is None:
            return []
        rows = self._conn.execute(_sql("execute_716.sql"), (limit,)).fetchall()
        return [dict(r) for r in rows]

    def export_csv_rows(self, days: int = 30) -> list[sqlite3.Row]:
        """Return all rows for CSV export."""
        if self._conn is None:
            return []
        cutoff = self._cutoff(days)
        return self._conn.execute(_sql("execute_729.sql"), (cutoff,)).fetchall()

    def record_backend_health(self, backend: str, status: str,
                              latency_ms: float | None, models_detected: int,
                              error: str | None = None) -> None:
        """Record a backend health check result."""
        if self._conn is None:
            return
        try:
            self._conn.execute(_sql("execute_740.sql"), (time.time(), backend, status, latency_ms, models_detected, error))
            self._conn.commit()
        except sqlite3.Error as exc:
            log.debug("MetricsStore: health log failed: %s", exc)

    # ------------------------------------------------------------------
    # System Resources
    # ------------------------------------------------------------------

    def insert_resource_snapshot(self, snapshot: dict) -> None:
        """Insert a system resource snapshot."""
        if self._conn is None:
            return
        try:
            self._conn.execute(_sql("execute_757.sql"), (
                time.time(),
                snapshot.get("gpu_name"),
                snapshot.get("gpu_vram_total_mb"),
                snapshot.get("gpu_vram_used_mb"),
                snapshot.get("gpu_vram_free_mb"),
                snapshot.get("gpu_utilization_pct"),
                snapshot.get("gpu_temperature_c"),
                snapshot.get("gpu_power_w"),
                snapshot.get("ram_total_mb"),
                snapshot.get("ram_used_mb"),
                snapshot.get("ram_available_mb"),
                snapshot.get("ram_percent"),
                snapshot.get("swap_total_mb"),
                snapshot.get("swap_used_mb"),
                snapshot.get("cpu_count"),
                snapshot.get("cpu_percent"),
                snapshot.get("ollama_models_loaded", 0),
                snapshot.get("ollama_vram_used_mb", 0),
                snapshot.get("models_loaded_json"),
            ))
            self._conn.commit()
        except sqlite3.Error as exc:
            log.debug("MetricsStore: resource snapshot failed: %s", exc)

    def get_latest_resource_snapshot(self) -> dict | None:
        """Get the most recent system resource snapshot."""
        if self._conn is None:
            return None
        row = self._conn.execute(_sql("execute_795.sql")).fetchone()
        return dict(row) if row else None

    def get_resource_history(self, hours: int = 6) -> list[dict]:
        """Get resource snapshots for the last N hours."""
        if self._conn is None:
            return []
        cutoff = time.time() - (hours * 3600)
        rows = self._conn.execute(_sql("execute_805.sql"), (cutoff,)).fetchall()
        return [dict(r) for r in rows]

    def cleanup_resources(self, max_age_days: int = 7) -> None:
        """Purge resource snapshots older than max_age_days."""
        if self._conn is None:
            return
        cutoff = time.time() - (max_age_days * 86400)
        try:
            self._conn.execute(_sql("execute_818.sql"), (cutoff,))
            self._conn.commit()
        except sqlite3.Error:
            pass


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_store: MetricsStore | None = None


def get_store() -> MetricsStore | None:
    """Return the global MetricsStore instance (None if not initialised)."""
    return _store


def init_store(cfg: MetricsConfig) -> MetricsStore:
    """Create and set the global MetricsStore."""
    global _store
    _store = MetricsStore(cfg)
    _store.cleanup()
    log.info("MetricsStore initialised at %s", cfg.resolved_db_path)
    return _store


def _reset_store() -> None:
    """Reset singleton — for testing."""
    global _store
    if _store:
        _store.close()
    _store = None
