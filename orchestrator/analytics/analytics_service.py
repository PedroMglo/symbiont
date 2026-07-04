"""Unified analytics service — merges sessions.db + metrics.db data.

Every response includes `data_sources_used` indicating where the data came from.
sessions.db is NEVER modified. metrics.db may not exist — results degrade gracefully.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from orchestrator.analytics.metrics_store_reader import MetricsStoreReader
from orchestrator.analytics.session_store_reader import SessionStoreReader

log = logging.getLogger(__name__)


class AnalyticsService:
    """Unified analytics combining sessions.db (activity) and metrics.db (LLM telemetry).

    Data sources:
        - sessions_db: session counts, message counts, activity timeline
        - metrics_db: tokens, models, backends, latency, errors, fallbacks
        - clickhouse: all-in-one when backend="clickhouse"
        - estimated: derived/computed values when exact data unavailable
    """

    def __init__(
        self,
        sessions_db_path: str | Path | None = None,
        metrics_db_path: str | Path | None = None,
        backend: str = "sqlite",
        clickhouse_url: str = "",
        clickhouse_database: str = "ai_symbiont",
        clickhouse_username: str = "default",
        clickhouse_password: str = "",
    ) -> None:
        self._backend = backend

        if backend == "clickhouse":
            from orchestrator.analytics.clickhouse_reader import ClickHouseReader

            self._ch = ClickHouseReader(
                url=clickhouse_url,
                database=clickhouse_database,
                username=clickhouse_username,
                password=clickhouse_password,
            )
            self._sessions = None
            self._metrics = None
        else:
            self._ch = None
            self._sessions = SessionStoreReader(sessions_db_path) if sessions_db_path else None
            self._metrics = MetricsStoreReader(metrics_db_path)

    @property
    def sessions_available(self) -> bool:
        if self._ch:
            return self._ch.available
        return self._sessions is not None and self._sessions.available

    @property
    def metrics_available(self) -> bool:
        if self._ch:
            return self._ch.available
        return self._metrics.available

    @property
    def metrics_reader(self):
        """Return the active metrics reader (ClickHouse or SQLite)."""
        if self._ch:
            return self._ch
        return self._metrics

    def close(self) -> None:
        if self._ch:
            self._ch.close()
        if self._sessions:
            self._sessions.close()
        if self._metrics:
            self._metrics.close()

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------

    def summary(self, days: int = 7) -> dict[str, Any]:
        """Comprehensive summary combining both data sources."""
        # ClickHouse-only path
        if self._ch:
            if not self._ch.available:
                return {"error": "ClickHouse unavailable", "data_sources_used": []}
            ch_summary = self._ch.summary(days=days)
            perf = self._ch.performance(days=days)
            return {
                "total_sessions": self._ch.count_sessions(days=days),
                "total_messages": self._ch.count_messages(days=days),
                "active_sessions": self._ch.active_sessions(since_seconds=3600),
                "total_requests": ch_summary.get("total_queries", 0),
                "total_tokens": ch_summary.get("total_tokens", 0),
                "prompt_tokens": ch_summary.get("total_prompt_tokens", 0),
                "completion_tokens": ch_summary.get("total_completion_tokens", 0),
                "avg_tokens_per_query": ch_summary.get("avg_tokens_per_query", 0),
                "avg_latency_ms": ch_summary.get("avg_latency_ms", 0),
                "top_model": ch_summary.get("top_model", {}),
                "top_backend": ch_summary.get("top_backend", {}),
                "fallback_count": ch_summary.get("fallback_count", 0),
                "fallback_rate": ch_summary.get("fallback_rate", 0),
                "error_count": ch_summary.get("error_count", 0),
                "error_rate": ch_summary.get("error_rate", 0),
                "stream_queries": ch_summary.get("stream_queries", 0),
                "agentic_queries": ch_summary.get("agentic_queries", 0),
                "unique_sessions_with_metrics": ch_summary.get("unique_sessions", 0),
                "p50_latency_ms": perf.get("p50_ms", 0),
                "p95_latency_ms": perf.get("p95_ms", 0),
                "p99_latency_ms": perf.get("p99_ms", 0),
                "period_days": days,
                "data_sources_used": ["clickhouse"],
            }

        sources: list[str] = []
        result: dict[str, Any] = {}

        # Sessions data
        if self.sessions_available:
            sources.append("sessions_db")
            result["total_sessions"] = self._sessions.count_sessions(days=days)
            result["total_messages"] = self._sessions.count_messages(days=days)
            result["active_sessions"] = self._sessions.active_sessions(since_seconds=3600)
        else:
            result["total_sessions"] = 0
            result["total_messages"] = 0
            result["active_sessions"] = 0

        # Metrics data
        if self.metrics_available:
            sources.append("metrics_db")
            metrics_summary = self._metrics.summary(days=days)
            result["total_requests"] = metrics_summary.get("total_queries", 0)
            result["total_tokens"] = metrics_summary.get("total_tokens", 0)
            result["prompt_tokens"] = metrics_summary.get("total_prompt_tokens", 0)
            result["completion_tokens"] = metrics_summary.get("total_completion_tokens", 0)
            result["avg_tokens_per_query"] = metrics_summary.get("avg_tokens_per_query", 0)
            result["avg_latency_ms"] = metrics_summary.get("avg_latency_ms", 0)
            result["top_model"] = metrics_summary.get("top_model", {})
            result["top_backend"] = metrics_summary.get("top_backend", {})
            result["fallback_count"] = metrics_summary.get("fallback_count", 0)
            result["fallback_rate"] = metrics_summary.get("fallback_rate", 0)
            result["error_count"] = metrics_summary.get("error_count", 0)
            result["error_rate"] = metrics_summary.get("error_rate", 0)
            result["stream_queries"] = metrics_summary.get("stream_queries", 0)
            result["agentic_queries"] = metrics_summary.get("agentic_queries", 0)
            result["unique_sessions_with_metrics"] = metrics_summary.get("unique_sessions", 0)

            # Performance data
            perf = self._metrics.performance(days=days)
            result["p50_latency_ms"] = perf.get("p50_ms", 0)
            result["p95_latency_ms"] = perf.get("p95_ms", 0)
            result["p99_latency_ms"] = perf.get("p99_ms", 0)
        else:
            result["total_requests"] = 0
            result["total_tokens"] = 0
            result["prompt_tokens"] = 0
            result["completion_tokens"] = 0
            result["avg_tokens_per_query"] = 0
            result["avg_latency_ms"] = 0
            result["top_model"] = {}
            result["top_backend"] = {}
            result["fallback_count"] = 0
            result["fallback_rate"] = 0
            result["error_count"] = 0
            result["error_rate"] = 0
            result["stream_queries"] = 0
            result["agentic_queries"] = 0
            result["p50_latency_ms"] = 0
            result["p95_latency_ms"] = 0
            result["p99_latency_ms"] = 0

        # Estimated request count from sessions if no metrics
        if not self.metrics_available and self.sessions_available:
            sources.append("estimated")
            # Each user message is approximately one LLM request
            result["total_requests"] = result["total_messages"] // 2

        result["period_days"] = days
        result["data_sources_used"] = sources
        return result

    # ------------------------------------------------------------------
    # Timeline
    # ------------------------------------------------------------------

    def timeline(self, days: int = 7, resolution: str = "day") -> dict[str, Any]:
        """Combined timeline from both sources."""
        # ClickHouse-only path
        if self._ch:
            if not self._ch.available:
                return {"data": [], "data_sources_used": []}
            data = self._ch.timeline(days=days, resolution=resolution)
            return {
                "data": data,
                "resolution": resolution,
                "data_sources_used": ["clickhouse"],
            }

        sources: list[str] = []
        session_timeline: list[dict] = []
        metrics_timeline: list[dict] = []

        if self.sessions_available:
            sources.append("sessions_db")
            if resolution == "hour":
                session_timeline = self._sessions.sessions_per_hour(days=days)
            else:
                session_timeline = self._sessions.sessions_per_day(days=days)

        if self.metrics_available:
            sources.append("metrics_db")
            metrics_timeline = self._metrics.timeline(days=days, resolution=resolution)

        # Merge by date/period
        merged: dict[str, dict] = {}
        for entry in session_timeline:
            period = entry.get("date") or entry.get("period", "")
            merged[period] = {
                "period": period,
                "sessions": entry.get("sessions", 0),
                "messages": entry.get("messages", 0),
                "requests": 0,
                "tokens": 0,
                "errors": 0,
                "fallbacks": 0,
            }

        for entry in metrics_timeline:
            period = entry.get("period", "")
            if period in merged:
                merged[period]["requests"] = entry.get("queries", 0)
                merged[period]["tokens"] = entry.get("tokens", 0)
                merged[period]["errors"] = entry.get("errors", 0)
            else:
                merged[period] = {
                    "period": period,
                    "sessions": 0,
                    "messages": 0,
                    "requests": entry.get("queries", 0),
                    "tokens": entry.get("tokens", 0),
                    "errors": entry.get("errors", 0),
                    "fallbacks": 0,
                }

        timeline_list = sorted(merged.values(), key=lambda x: x["period"])
        return {
            "data": timeline_list,
            "resolution": resolution,
            "data_sources_used": sources,
        }

    # ------------------------------------------------------------------
    # Models
    # ------------------------------------------------------------------

    def models(self, days: int = 7) -> dict[str, Any]:
        """Per-model breakdown."""
        if self._ch:
            if not self._ch.available:
                return {"data": [], "data_sources_used": []}
            return {"data": self._ch.by_model(days=days), "data_sources_used": ["clickhouse"]}
        if not self.metrics_available:
            return {"data": [], "data_sources_used": []}
        data = self._metrics.by_model(days=days)
        return {"data": data, "data_sources_used": ["metrics_db"]}

    # ------------------------------------------------------------------
    # Backends
    # ------------------------------------------------------------------

    def backends(self, days: int = 7) -> dict[str, Any]:
        """Per-backend breakdown."""
        if self._ch:
            if not self._ch.available:
                return {"data": [], "data_sources_used": []}
            return {"data": self._ch.by_backend(days=days), "data_sources_used": ["clickhouse"]}
        if not self.metrics_available:
            return {"data": [], "data_sources_used": []}
        data = self._metrics.by_backend(days=days)
        return {"data": data, "data_sources_used": ["metrics_db"]}

    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------

    def sessions_list(
        self, days: int = 7, limit: int = 50, offset: int = 0
    ) -> dict[str, Any]:
        """Paginated list of sessions enriched with metrics."""
        # ClickHouse-only path
        if self._ch:
            if not self._ch.available:
                return {"data": [], "total": 0, "data_sources_used": []}
            sessions = self._ch.by_session(days=days, limit=limit)
            total = self._ch.count_sessions(days=days)
            return {
                "data": sessions,
                "total": total,
                "limit": limit,
                "offset": offset,
                "data_sources_used": ["clickhouse"],
            }

        sources: list[str] = []

        if not self.sessions_available:
            return {"data": [], "total": 0, "data_sources_used": []}

        sources.append("sessions_db")
        sessions = self._sessions.sessions_list(days=days, limit=limit, offset=offset)
        total = self._sessions.count_sessions(days=days)

        # Enrich with metrics data if available
        if self.metrics_available:
            sources.append("metrics_db")
            for session in sessions:
                metrics = self._metrics.session_metrics(session["session_id"])
                if metrics:
                    session["request_count"] = metrics.get("request_count", 0)
                    session["total_tokens"] = metrics.get("total_tokens", 0)
                    session["models_used"] = metrics.get("models_used", [])
                    session["backends_used"] = metrics.get("backends_used", [])
                    session["errors"] = metrics.get("errors", 0)
                    session["fallbacks"] = metrics.get("fallbacks", 0)
                    session["avg_latency_ms"] = metrics.get("avg_latency_ms", 0)
                    session["has_metrics"] = True
                else:
                    session["has_metrics"] = False

        return {
            "data": sessions,
            "total": total,
            "limit": limit,
            "offset": offset,
            "data_sources_used": sources,
        }

    def session_detail(self, session_id: str) -> dict[str, Any] | None:
        """Detailed view of a single session."""
        # ClickHouse-only path
        if self._ch:
            if not self._ch.available:
                return None
            metrics = self._ch.session_metrics(session_id)
            if not metrics:
                return None
            return {
                "session_id": session_id,
                "metrics": metrics,
                "data_sources_used": ["clickhouse"],
            }

        if not self.sessions_available:
            return None

        detail = self._sessions.session_detail(session_id)
        if detail is None:
            return None

        sources = ["sessions_db"]

        # Enrich with metrics
        if self.metrics_available:
            metrics = self._metrics.session_metrics(session_id)
            if metrics:
                sources.append("metrics_db")
                detail["metrics"] = metrics

        detail["data_sources_used"] = sources
        return detail

    # ------------------------------------------------------------------
    # Performance
    # ------------------------------------------------------------------

    def performance(self, days: int = 7) -> dict[str, Any]:
        """Latency percentiles and performance breakdown."""
        if self._ch:
            if not self._ch.available:
                return {"data": {}, "data_sources_used": []}
            return {"data": self._ch.performance(days=days), "data_sources_used": ["clickhouse"]}
        if not self.metrics_available:
            return {"data": {}, "data_sources_used": []}
        data = self._metrics.performance(days=days)
        return {"data": data, "data_sources_used": ["metrics_db"]}

    # ------------------------------------------------------------------
    # Errors & Fallbacks
    # ------------------------------------------------------------------

    def fallbacks(self, days: int = 7) -> dict[str, Any]:
        if self._ch:
            if not self._ch.available:
                return {"data": [], "data_sources_used": []}
            return {"data": self._ch.fallbacks(days=days), "data_sources_used": ["clickhouse"]}
        if not self.metrics_available:
            return {"data": [], "data_sources_used": []}
        return {"data": self._metrics.fallbacks(days=days), "data_sources_used": ["metrics_db"]}

    def errors(self, days: int = 7) -> dict[str, Any]:
        if self._ch:
            if not self._ch.available:
                return {"data": [], "data_sources_used": []}
            return {"data": self._ch.errors(days=days), "data_sources_used": ["clickhouse"]}
        if not self.metrics_available:
            return {"data": [], "data_sources_used": []}
        return {"data": self._metrics.errors(days=days), "data_sources_used": ["metrics_db"]}

    # ------------------------------------------------------------------
    # Recent / Live
    # ------------------------------------------------------------------

    def recent(self, limit: int = 20) -> dict[str, Any]:
        if self._ch:
            if not self._ch.available:
                return {"data": [], "data_sources_used": []}
            return {"data": self._ch.recent(limit=limit), "data_sources_used": ["clickhouse"]}
        if not self.metrics_available:
            return {"data": [], "data_sources_used": []}
        return {"data": self._metrics.recent(limit=limit), "data_sources_used": ["metrics_db"]}

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def diagnostics(self) -> dict[str, Any]:
        """Return info about available data sources."""
        if self._ch:
            return {
                "backend": "clickhouse",
                "clickhouse": {
                    "available": self._ch.available,
                },
            }
        result: dict[str, Any] = {
            "backend": "sqlite",
            "sessions_db": {
                "available": self.sessions_available,
            },
            "metrics_db": {
                "available": self.metrics_available,
            },
        }
        if self.sessions_available:
            result["sessions_db"]["schema"] = self._sessions.detect_schema()
        return result
