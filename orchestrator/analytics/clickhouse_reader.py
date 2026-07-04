"""ClickHouse reader — read-only analytics from the ClickHouse llm_events table.

Implements the same interface as MetricsStoreReader so the AnalyticsService
can swap backends transparently.
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


def _safe_int(v, default: int = 0) -> int:
    if v is None:
        return default
    try:
        return int(v)
    except (ValueError, TypeError):
        return default


def _safe_float(v, default: float = 0.0) -> float:
    if v is None:
        return default
    try:
        f = float(v)
        if f != f:  # nan check
            return default
        return f
    except (ValueError, TypeError):
        return default


class ClickHouseReader:
    """Read-only analytics access to ClickHouse llm_events table.

    Uses httpx to query ClickHouse via its HTTP interface (JSON format).
    """

    def __init__(
        self,
        url: str,
        database: str,
        username: str,
        password: str
    ) -> None:
        self._url = url
        self._database = database
        self._username = username
        self._password = password
        self._available = False
        self._check_availability()

    def _check_availability(self) -> None:
        try:
            import httpx

            resp = httpx.get(f"{self._url}/ping", timeout=3.0)
            if resp.status_code == 200 and resp.text.strip() == "Ok.":
                self._available = True
                log.info("ClickHouseReader: connected to %s", self._url)
            else:
                log.warning("ClickHouseReader: ping failed (%s)", resp.text[:50])
        except Exception as exc:
            log.warning("ClickHouseReader: unavailable — %s", exc)

    @property
    def available(self) -> bool:
        return self._available

    def close(self) -> None:
        pass

    def _query(self, sql: str) -> list[dict[str, Any]]:
        """Execute a read-only query and return rows as dicts."""
        if not self._available:
            return []
        try:
            import httpx

            params: dict[str, str] = {
                "database": self._database,
                "default_format": "JSONEachRow",
            }
            if self._username:
                params["user"] = self._username
            if self._password:
                params["password"] = self._password

            resp = httpx.post(
                self._url,
                content=sql.encode("utf-8"),
                params=params,
                timeout=10.0,
            )
            if resp.status_code != 200:
                log.warning("ClickHouseReader query error: %s", resp.text[:200])
                return []

            import json

            rows = []
            for line in resp.text.strip().split("\n"):
                if line:
                    rows.append(json.loads(line))
            return rows
        except Exception as exc:
            log.warning("ClickHouseReader query failed: %s", exc)
            return []

    def _query_one(self, sql: str) -> dict[str, Any] | None:
        rows = self._query(sql)
        return rows[0] if rows else None

    def _cutoff_dt(self, days: int) -> str:
        """Return ISO datetime string for N days ago."""
        from datetime import datetime, timedelta, timezone

        dt = datetime.now(timezone.utc) - timedelta(days=days)
        return dt.strftime("%Y-%m-%d %H:%M:%S")

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------

    def summary(self, days: int = 7) -> dict[str, Any]:
        cutoff = self._cutoff_dt(days)
        row = self._query_one(_sql("extra_130_1.sql").format(cutoff))

        if not row:
            return {"source": "clickhouse"}

        total = _safe_int(row.get("total_queries", 0))

        top_model_row = self._query_one(_sql("extra_153_2.sql").format(cutoff))

        top_backend_row = self._query_one(_sql("extra_160_3.sql").format(cutoff))

        busiest_hour_row = self._query_one(_sql("extra_167_4.sql").format(cutoff))

        return {
            "period_days": days,
            "total_queries": total,
            "total_tokens": _safe_int(row.get("sum_tokens", 0)),
            "total_prompt_tokens": _safe_int(row.get("sum_prompt_tokens", 0)),
            "total_completion_tokens": _safe_int(row.get("sum_completion_tokens", 0)),
            "avg_tokens_per_query": _safe_int(row.get("avg_tpq", 0)),
            "avg_latency_ms": _safe_float(row.get("avg_lat_ms", 0)),
            "unique_sessions": _safe_int(row.get("unique_sessions", 0)),
            "agentic_queries": _safe_int(row.get("agentic_queries", 0)),
            "agentic_ratio": round(_safe_int(row.get("agentic_queries", 0)) / max(total, 1), 3),
            "fallback_count": _safe_int(row.get("fallback_count", 0)),
            "fallback_rate": round(_safe_int(row.get("fallback_count", 0)) / max(total, 1), 3),
            "error_count": _safe_int(row.get("error_count", 0)),
            "error_rate": round(_safe_int(row.get("error_count", 0)) / max(total, 1), 3),
            "stream_queries": _safe_int(row.get("stream_queries", 0)),
            "top_model": {
                "name": top_model_row["model"] if top_model_row else None,
                "queries": int(top_model_row["cnt"]) if top_model_row else 0,
                "tokens": int(top_model_row["tok"]) if top_model_row else 0,
            },
            "top_backend": {
                "name": top_backend_row["backend"] if top_backend_row else None,
                "queries": int(top_backend_row["cnt"]) if top_backend_row else 0,
            },
            "busiest_hour": int(busiest_hour_row["hr"]) if busiest_hour_row else None,
            "source": "clickhouse",
        }

    # ------------------------------------------------------------------
    # Timeline
    # ------------------------------------------------------------------

    def timeline(self, days: int = 7, resolution: str = "day") -> list[dict]:
        cutoff = self._cutoff_dt(days)
        if resolution == "hour":
            group_expr = "formatDateTime(timestamp, '%Y-%m-%d %H:00')"
        else:
            group_expr = "formatDateTime(timestamp, '%Y-%m-%d')"

        rows = self._query(_sql("extra_214_5.sql").format(group_expr, cutoff))
        return [{
            "period": r["period"],
            "queries": _safe_int(r["queries"]),
            "requests": _safe_int(r["queries"]),
            "tokens": _safe_int(r.get("sum_tok", 0)),
            "avg_latency_ms": _safe_float(r.get("avg_lat", 0)),
            "errors": _safe_int(r["errors"]),
        } for r in rows]

    # ------------------------------------------------------------------
    # Models
    # ------------------------------------------------------------------

    def by_model(self, days: int = 7) -> list[dict]:
        cutoff = self._cutoff_dt(days)
        rows = self._query(_sql("extra_240_6.sql").format(cutoff))

        result = []
        for r in rows:
            # Intent distribution per model
            intents = self._query(_sql("extra_259_27.sql").format(cutoff, r["model"]))
            result.append({
                "model": r["model"],
                "queries": _safe_int(r["queries"]),
                "prompt_tokens": _safe_int(r.get("sum_prompt", 0)),
                "completion_tokens": _safe_int(r.get("sum_compl", 0)),
                "total_tokens": _safe_int(r.get("sum_tok", 0)),
                "avg_latency_ms": _safe_float(r.get("avg_lat", 0)),
                "sessions": _safe_int(r["sessions"]),
                "agentic_count": _safe_int(r["agentic_count"]),
                "errors": _safe_int(r["errors"]),
                "intent_distribution": {i["intent"]: int(i["cnt"]) for i in intents},
            })
        return result

    # ------------------------------------------------------------------
    # Backends
    # ------------------------------------------------------------------

    def by_backend(self, days: int = 7) -> list[dict]:
        cutoff = self._cutoff_dt(days)
        rows = self._query(_sql("extra_286_7.sql").format(cutoff))
        return [{
            "backend": r["backend"],
            "queries": _safe_int(r["queries"]),
            "total_tokens": _safe_int(r.get("sum_tok", 0)),
            "avg_latency_ms": _safe_float(r.get("avg_lat", 0)),
            "errors": _safe_int(r["errors"]),
            "fallback_to_count": _safe_int(r["fallback_to_count"]),
        } for r in rows]

    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------

    def by_session(self, days: int = 7, limit: int = 50) -> list[dict]:
        cutoff = self._cutoff_dt(days)
        rows = self._query(_sql("extra_313_8.sql").format(cutoff, limit))
        return [{
            "session_id": r["session_id"],
            "queries": _safe_int(r["queries"]),
            "models_used": ",".join(r.get("models_used_arr", [])) if isinstance(r.get("models_used_arr"), list) else str(r.get("models_used_arr", "")),
            "total_tokens": _safe_int(r.get("sum_tok", 0)),
            "first_query_at": r["first_query_at"],
            "last_query_at": r["last_query_at"],
        } for r in rows]

    def by_intent(self, days: int = 7) -> list[dict]:
        cutoff = self._cutoff_dt(days)
        rows = self._query(_sql("extra_338_9.sql").format(cutoff))

        result = []
        for r in rows:
            model_dist = self._query(_sql("extra_350_28.sql").format(cutoff, r["intent"]))
            result.append({
                "intent": r["intent"],
                "total_queries": _safe_int(r["total_queries"]),
                "model_distribution": {m["model"]: int(m["cnt"]) for m in model_dist},
            })
        return result

    # ------------------------------------------------------------------
    # Fallbacks
    # ------------------------------------------------------------------

    def fallbacks(self, days: int = 7) -> list[dict]:
        cutoff = self._cutoff_dt(days)
        rows = self._query(_sql("extra_370_10.sql").format(cutoff))
        return [{
            "requested_model": r["requested_model"],
            "resolved_model": r["resolved_model"],
            "fallback_reason": r["fallback_reason"],
            "count": _safe_int(r["count"]),
        } for r in rows]

    # ------------------------------------------------------------------
    # Errors
    # ------------------------------------------------------------------

    def errors(self, days: int = 7) -> list[dict]:
        cutoff = self._cutoff_dt(days)
        rows = self._query(_sql("extra_395_11.sql").format(cutoff))
        return [{
            "error_type": r["error_type"],
            "backend": r["backend"],
            "count": _safe_int(r["count"]),
            "last_seen": r["last_seen"],
        } for r in rows]

    # ------------------------------------------------------------------
    # Performance
    # ------------------------------------------------------------------

    def performance(self, days: int = 7) -> dict[str, Any]:
        cutoff = self._cutoff_dt(days)
        row = self._query_one(_sql("extra_419_12.sql").format(cutoff))

        if not row or _safe_int(row.get("total_measured", 0)) == 0:
            return {"p50_ms": 0, "p95_ms": 0, "p99_ms": 0, "by_model": []}

        # Per-model breakdown
        by_model = self._query(_sql("extra_435_13.sql").format(cutoff))

        # Latency buckets
        bucket_rows = self._query(_sql("extra_448_14.sql").format(cutoff))

        return {
            "p50_ms": round(_safe_float(row.get("p50", 0)), 1),
            "p95_ms": round(_safe_float(row.get("p95", 0)), 1),
            "p99_ms": round(_safe_float(row.get("p99", 0)), 1),
            "avg_ms": round(_safe_float(row.get("avg_lat", 0)), 1),
            "first_token_p50_ms": round(_safe_float(row.get("ft_p50", 0)), 1) if row.get("ft_p50") else None,
            "total_measured": _safe_int(row["total_measured"]),
            "latency_buckets": [{"range": r["range"], "count": _safe_int(r["count"])} for r in bucket_rows],
            "by_model": [{
                "model": r["model"],
                "queries": _safe_int(r["queries"]),
                "avg_ms": _safe_float(r["avg_lat"]),
                "min_ms": _safe_float(r["min_lat"]),
                "max_ms": _safe_float(r["max_lat"]),
            } for r in by_model],
        }

    # ------------------------------------------------------------------
    # Recent / Live
    # ------------------------------------------------------------------

    def recent(self, limit: int = 20) -> list[dict]:
        rows = self._query(_sql("extra_488_15.sql").format(limit))
        return [{
            "request_id": r.get("request_id", ""),
            "timestamp": r.get("timestamp", ""),
            "model": r.get("model", ""),
            "backend": r.get("backend", ""),
            "intent": r.get("intent", ""),
            "total_tokens": _safe_int(r.get("total_tokens", 0)),
            "latency_ms": _safe_float(r.get("latency_ms", 0)),
            "stream": _safe_int(r.get("stream", 0)),
            "agentic": _safe_int(r.get("agentic", 0)),
            "success": _safe_int(r.get("success", 1)),
            "error_type": r.get("error_type", ""),
        } for r in rows]

    # ------------------------------------------------------------------
    # CSV Export
    # ------------------------------------------------------------------

    def export_csv_rows(self, days: int = 30) -> list[dict]:
        cutoff = self._cutoff_dt(days)
        return self._query(_sql("extra_518_16.sql").format(cutoff))

    # ------------------------------------------------------------------
    # Session-specific metrics
    # ------------------------------------------------------------------

    def session_metrics(self, session_id: str) -> dict[str, Any]:
        row = self._query_one(_sql("extra_530_17.sql").format(session_id))

        if not row or _safe_int(row.get("request_count", 0)) == 0:
            return {}

        return {
            "request_count": _safe_int(row["request_count"]),
            "total_tokens": _safe_int(row.get("sum_tok", 0)),
            "prompt_tokens": _safe_int(row.get("sum_prompt", 0)),
            "completion_tokens": _safe_int(row.get("sum_compl", 0)),
            "avg_latency_ms": _safe_float(row.get("avg_lat", 0)),
            "models_used": row.get("models_used", []) if isinstance(row.get("models_used"), list) else [],
            "backends_used": row.get("backends_used", []) if isinstance(row.get("backends_used"), list) else [],
            "fallbacks": _safe_int(row["fallbacks"]),
            "errors": _safe_int(row["errors"]),
            "agentic_calls": _safe_int(row["agentic_calls"]),
            "stream_calls": _safe_int(row["stream_calls"]),
        }

    # ------------------------------------------------------------------
    # Sessions from ClickHouse (replaces SessionStoreReader)
    # ------------------------------------------------------------------

    def count_sessions(self, days: int | None = None) -> int:
        if days:
            cutoff = self._cutoff_dt(days)
            row = self._query_one(_sql("extra_571_29.sql").format(cutoff))
        else:
            row = self._query_one(_sql("extra_577_30.sql"))
        return _safe_int(row["cnt"]) if row else 0

    def count_messages(self, days: int | None = None) -> int:
        if days:
            cutoff = self._cutoff_dt(days)
            row = self._query_one(_sql("extra_586_31.sql").format(cutoff))
        else:
            row = self._query_one(_sql("extra_592_32.sql"))
        return _safe_int(row["cnt"]) if row else 0

    def active_sessions(self, since_seconds: int = 3600) -> int:
        row = self._query_one(_sql("extra_598_18.sql").format(since_seconds))
        return _safe_int(row["cnt"]) if row else 0

    # ------------------------------------------------------------------
    # Resource samples from ClickHouse
    # ------------------------------------------------------------------

    def resource_samples(self, minutes: int = 60) -> list[dict]:
        rows = self._query(_sql("extra_611_19.sql").format(minutes))
        return rows

    # ==================================================================
    # LangGraph Tracing Queries
    # ==================================================================

    def graph_overview(self, days: int = 7) -> dict[str, Any]:
        """High-level graph execution KPIs."""
        cutoff = self._cutoff_dt(days)
        row = self._query_one(_sql("extra_626_20.sql").format(cutoff))
        if not row:
            return {"source": "clickhouse", "period_days": days}

        total = _safe_int(row.get("total_runs", 0))
        return {
            "period_days": days,
            "total_runs": total,
            "avg_duration_ms": _safe_float(row.get("avg_duration_ms", 0)),
            "p95_duration_ms": _safe_float(row.get("p95_duration_ms", 0)),
            "error_runs": _safe_int(row.get("error_runs", 0)),
            "error_rate": round(_safe_int(row.get("error_runs", 0)) / max(total, 1), 3),
            "avg_node_count": _safe_float(row.get("avg_node_count", 0)),
            "fallback_runs": _safe_int(row.get("fallback_runs", 0)),
            "fallback_rate": round(_safe_int(row.get("fallback_runs", 0)) / max(total, 1), 3),
            "critic_runs": _safe_int(row.get("critic_runs", 0)),
            "total_critic_loops": _safe_int(row.get("total_critic_loops", 0)),
            "total_tokens": _safe_int(row.get("sum_tokens", 0)),
            "source": "clickhouse",
        }

    def graph_traces(self, days: int = 7, limit: int = 50) -> list[dict]:
        """List recent graph runs (newest first)."""
        cutoff = self._cutoff_dt(days)
        rows = self._query(_sql("extra_663_21.sql").format(cutoff, int(limit)))
        return [{
            "graph_run_id": r["graph_run_id"],
            "timestamp": r["timestamp"],
            "session_id": r.get("session_id", ""),
            "total_duration_ms": _safe_float(r.get("total_duration_ms", 0)),
            "node_count": _safe_int(r.get("node_count", 0)),
            "success": bool(r.get("success", 1)),
            "intent": r.get("intent", ""),
            "complexity": r.get("complexity", ""),
            "model_used": r.get("model_used", ""),
            "fallback_used": bool(r.get("fallback_used", 0)),
            "critic_invoked": bool(r.get("critic_invoked", 0)),
            "path": r.get("path", []),
            "agents_invoked": r.get("agents_invoked", []),
            "context_sources": r.get("context_sources", []),
            "total_tokens": _safe_int(r.get("total_tokens", 0)),
        } for r in rows]

    def graph_trace_detail(self, graph_run_id: str) -> dict[str, Any]:
        """Full detail for a single graph run: all node events + summary."""
        # Sanitize input
        safe_id = graph_run_id.replace("'", "")

        # Get the run summary
        run_row = self._query_one(_sql("extra_709_22.sql").format(safe_id))

        # Get all node events for this run
        nodes = self._query(_sql("extra_716_23.sql").format(safe_id))

        return {
            "graph_run_id": safe_id,
            "run": run_row or {},
            "nodes": [{
                "node_name": n["node_name"],
                "node_type": n.get("node_type", ""),
                "timestamp": n["timestamp"],
                "duration_ms": _safe_float(n.get("duration_ms", 0)),
                "success": bool(n.get("success", 1)),
                "error_type": n.get("error_type", ""),
                "error_message": n.get("error_message", ""),
                "tokens_used": _safe_int(n.get("tokens_used", 0)),
                "parallel_group": n.get("parallel_group", ""),
                "input_keys": n.get("input_keys", []),
                "output_keys": n.get("output_keys", []),
            } for n in nodes],
            "source": "clickhouse",
        }

    def graph_node_stats(self, days: int = 7) -> list[dict]:
        """Per-node aggregated statistics."""
        cutoff = self._cutoff_dt(days)
        rows = self._query(_sql("extra_756_24.sql").format(cutoff))
        return [{
            "node_name": r["node_name"],
            "node_type": r.get("node_type", ""),
            "executions": _safe_int(r["executions"]),
            "avg_ms": _safe_float(r.get("avg_ms", 0)),
            "p50_ms": _safe_float(r.get("p50_ms", 0)),
            "p95_ms": _safe_float(r.get("p95_ms", 0)),
            "p99_ms": _safe_float(r.get("p99_ms", 0)),
            "max_ms": _safe_float(r.get("max_ms", 0)),
            "errors": _safe_int(r.get("errors", 0)),
            "error_rate": round(_safe_int(r.get("errors", 0)) / max(_safe_int(r["executions"]), 1), 3),
            "tokens": _safe_int(r.get("sum_tokens", 0)),
        } for r in rows]

    def graph_slow_nodes(self, days: int = 7, threshold_ms: float = 500) -> list[dict]:
        """Nodes exceeding a latency threshold."""
        cutoff = self._cutoff_dt(days)
        rows = self._query(_sql("extra_790_25.sql").format(cutoff, threshold_ms))
        return [{
            "node_name": r["node_name"],
            "node_type": r.get("node_type", ""),
            "timestamp": r["timestamp"],
            "duration_ms": _safe_float(r["duration_ms"]),
            "graph_run_id": r["graph_run_id"],
            "session_id": r.get("session_id", ""),
            "error_type": r.get("error_type", ""),
        } for r in rows]

    def graph_timeline(self, days: int = 7, resolution: str = "hour") -> list[dict]:
        """Graph execution timeline (hourly or daily)."""
        cutoff = self._cutoff_dt(days)
        if resolution == "day":
            group_expr = "formatDateTime(timestamp, '%Y-%m-%d')"
        else:
            group_expr = "formatDateTime(timestamp, '%Y-%m-%d %H:00')"

        rows = self._query(_sql("extra_823_26.sql").format(group_expr, cutoff))
        return [{
            "period": r["period"],
            "runs": _safe_int(r["runs"]),
            "avg_duration_ms": _safe_float(r.get("avg_duration_ms", 0)),
            "errors": _safe_int(r.get("errors", 0)),
            "fallbacks": _safe_int(r.get("fallbacks", 0)),
        } for r in rows]
