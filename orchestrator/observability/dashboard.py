"""Dashboard API — FastAPI router mounted under /dashboard/*.

Provides endpoints for unified analytics (sessions.db + metrics.db),
real-time SSE feed, and static dashboard assets.
All read-only — never mutates any database.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import os
import queue
import time
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse

from config.storage_paths import symbiont_data_path

_SQL_DIR = Path(__file__).resolve().parent / "sql"
_SQL_CACHE = {}


def _sql(name: str) -> str:
    text = _SQL_CACHE.get(name)
    if text is None:
        text = (_SQL_DIR / name).read_text(encoding="utf-8").strip()
        _SQL_CACHE[name] = text
    return text


log = logging.getLogger(__name__)

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

# Module-level analytics service — set during app init
_analytics_service = None


def set_analytics_service(service) -> None:
    """Set the AnalyticsService instance (called from app lifespan)."""
    global _analytics_service
    _analytics_service = service


def _get_analytics():
    """Get the AnalyticsService or return None."""
    return _analytics_service


def _get_collector():
    try:
        from orchestrator.observability.collector import get_collector
        return get_collector()
    except Exception:
        return None


def _get_llm_router():
    """Get the LLMRouter instance from the engine (if available)."""
    try:
        from orchestrator.gateway.app import _engine
        if _engine and hasattr(_engine, '_llm'):
            return _engine._llm
    except Exception:
        pass
    return None


def _get_settings():
    """Get config Settings."""
    try:
        from orchestrator.config import get_settings
        return get_settings()
    except Exception:
        return None


# ------------------------------------------------------------------
# Summary / Overview
# ------------------------------------------------------------------


@router.get("/summary")
def dashboard_summary(days: int = Query(default=7, ge=1, le=365)):
    """Comprehensive summary combining sessions.db + metrics.db."""
    svc = _get_analytics()
    if svc is None:
        return {"error": "Analytics service not initialised", "data_sources_used": []}
    return svc.summary(days=days)


# ------------------------------------------------------------------
# Timeline
# ------------------------------------------------------------------


@router.get("/timeline")
def dashboard_timeline(
    days: int = Query(default=7, ge=1, le=365),
    resolution: str = Query(default="day", pattern="^(day|hour)$"),
):
    """Combined time-series data from sessions + metrics."""
    svc = _get_analytics()
    if svc is None:
        return {"data": [], "data_sources_used": []}
    return svc.timeline(days=days, resolution=resolution)


# ------------------------------------------------------------------
# Models — enriched with config + runtime data
# ------------------------------------------------------------------


@router.get("/models")
def dashboard_models(days: int = Query(default=7, ge=1, le=365)):
    """Per-model breakdown combining config, runtime detection, and metrics."""
    svc = _get_analytics()
    cfg = _get_settings()
    llm_router = _get_llm_router()
    sources: list[str] = []

    # Gather metrics data
    metrics_models: list[dict] = []
    if svc and svc.metrics_available:
        sources.append("metrics_db")
        metrics_models = svc.metrics_reader.by_model(days=days)

    # Build model registry from config
    configured_models: dict[str, dict[str, Any]] = {}
    if cfg and cfg.llm.backends:
        sources.append("config")
        for backend in cfg.llm.backends:
            for model_name in backend.models:
                if model_name not in configured_models:
                    configured_models[model_name] = {
                        "model_name": model_name,
                        "backends": [],
                        "backend_types": [],
                        "configured": True,
                        "enabled": backend.enabled,
                        "privacy_level": backend.privacy_level,
                    }
                configured_models[model_name]["backends"].append(backend.name)
                configured_models[model_name]["backend_types"].append("openai_compatible")

    # Runtime detection from LLMRouter health
    detected_models: dict[str, dict] = {}
    if llm_router:
        sources.append("runtime")
        try:
            report = llm_router.health_report()
            for entry in report:
                for m in entry.get("models_detected", []):
                    if m not in detected_models:
                        detected_models[m] = {
                            "detected_runtime": True,
                            "available": entry.get("status") == "healthy",
                            "health_status": entry.get("status", "unknown"),
                            "backend": entry.get("name", ""),
                            "health_latency_ms": entry.get("latency_ms"),
                        }
        except Exception:
            pass

    # Merge all sources into unified model list
    all_model_names = set(configured_models.keys()) | set(detected_models.keys())
    for m in metrics_models:
        all_model_names.add(m.get("model", ""))

    # Build metrics lookup
    metrics_by_name: dict[str, dict] = {}
    for m in metrics_models:
        metrics_by_name[m.get("model", "")] = m

    result: list[dict[str, Any]] = []
    for model_name in sorted(all_model_names):
        if not model_name:
            continue
        entry: dict[str, Any] = {
            "model_name": model_name,
            "display_name": model_name,
            "backends": [],
            "backend_type": "openai_compatible",
            "available": False,
            "health_status": "unknown",
            "configured": False,
            "detected_runtime": False,
            "used_in_period": False,
            "enabled": True,
            # Usage metrics
            "total_calls": 0,
            "unique_sessions": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "estimated_tokens": 0,
            "usage_source": "missing",
            # Latency
            "avg_latency_ms": 0,
            "p95_latency_ms": 0,
            "min_latency_ms": 0,
            "max_latency_ms": 0,
            "tokens_per_second": 0,
            # Errors
            "error_count": 0,
            "error_rate": 0,
            # Fallbacks
            "fallback_in_count": 0,
            "fallback_out_count": 0,
            # Timestamps
            "last_used_at": None,
            "first_used_at": None,
            # Context features
            "context_tokens": None,
            "rag_calls": None,
            "graph_calls": None,
            "tool_calls": None,
            # Resource placeholders
            "avg_cpu_percent": None,
            "peak_memory_mb": None,
            "avg_gpu_util_percent": None,
            "peak_vram_mb": None,
            "energy_estimate_wh": None,
        }

        # Merge config data
        if model_name in configured_models:
            cfg_data = configured_models[model_name]
            entry["configured"] = True
            entry["backends"] = cfg_data["backends"]
            entry["enabled"] = cfg_data["enabled"]
            entry["privacy_level"] = cfg_data.get("privacy_level", "local")

        # Merge runtime data
        if model_name in detected_models:
            rt = detected_models[model_name]
            entry["detected_runtime"] = True
            entry["available"] = rt.get("available", False)
            entry["health_status"] = rt.get("health_status", "unknown")
            if rt.get("backend") and rt["backend"] not in entry["backends"]:
                entry["backends"].append(rt["backend"])
        elif model_name in configured_models:
            entry["available"] = configured_models[model_name].get("enabled", False)
            entry["health_status"] = "configured" if entry["available"] else "disabled"

        # Merge metrics data
        if model_name in metrics_by_name:
            md = metrics_by_name[model_name]
            entry["used_in_period"] = True
            entry["total_calls"] = md.get("queries", 0) or md.get("requests", 0) or 0
            entry["unique_sessions"] = md.get("unique_sessions", 0)
            entry["prompt_tokens"] = md.get("prompt_tokens", 0)
            entry["completion_tokens"] = md.get("completion_tokens", 0)
            entry["total_tokens"] = md.get("total_tokens", 0)
            entry["avg_latency_ms"] = round(md.get("avg_latency_ms", 0) or 0, 1)
            entry["p95_latency_ms"] = round(md.get("p95_latency_ms", 0) or 0, 1)
            entry["min_latency_ms"] = round(md.get("min_latency_ms", 0) or 0, 1)
            entry["max_latency_ms"] = round(md.get("max_latency_ms", 0) or 0, 1)
            entry["error_count"] = md.get("errors", 0) or md.get("error_count", 0) or 0
            calls = entry["total_calls"] or 1
            entry["error_rate"] = round((entry["error_count"] / calls) * 100, 1)
            entry["fallback_in_count"] = md.get("fallback_in", 0) or 0
            entry["fallback_out_count"] = md.get("fallback_out", 0) or 0
            entry["last_used_at"] = md.get("last_used_at")
            entry["first_used_at"] = md.get("first_used_at")
            entry["usage_source"] = "backend"
            if entry["total_tokens"] > 0 and entry["avg_latency_ms"] > 0:
                entry["tokens_per_second"] = round(
                    (entry["completion_tokens"] or entry["total_tokens"]) / (entry["avg_latency_ms"] / 1000), 1
                )

        result.append(entry)

    # Summary cards data
    total_models = len(result)
    available_models = sum(1 for r in result if r["available"])
    used_models = sum(1 for r in result if r["used_in_period"])
    top_by_tokens = max(result, key=lambda r: r["total_tokens"], default=None)
    fastest = min(
        (r for r in result if r["avg_latency_ms"] > 0),
        key=lambda r: r["avg_latency_ms"],
        default=None,
    )
    highest_error = max(
        (r for r in result if r["total_calls"] > 0),
        key=lambda r: r["error_rate"],
        default=None,
    )

    return {
        "data": result,
        "summary": {
            "total_models": total_models,
            "available_models": available_models,
            "used_models": used_models,
            "top_model_by_tokens": top_by_tokens["model_name"] if top_by_tokens and top_by_tokens["total_tokens"] > 0 else None,
            "fastest_model": fastest["model_name"] if fastest else None,
            "fastest_latency_ms": fastest["avg_latency_ms"] if fastest else None,
            "highest_error_model": highest_error["model_name"] if highest_error and highest_error["error_rate"] > 0 else None,
            "highest_error_rate": highest_error["error_rate"] if highest_error and highest_error["error_rate"] > 0 else None,
        },
        "data_sources_used": sources,
    }


# ------------------------------------------------------------------
# Backends — enriched with config + runtime health
# ------------------------------------------------------------------


@router.get("/backends")
def dashboard_backends(days: int = Query(default=7, ge=1, le=365)):
    """Per-backend breakdown combining config, health, and metrics."""
    svc = _get_analytics()
    cfg = _get_settings()
    llm_router = _get_llm_router()
    sources: list[str] = []

    # Metrics data
    metrics_backends: list[dict] = []
    if svc and svc.metrics_available:
        sources.append("metrics_db")
        metrics_backends = svc.metrics_reader.by_backend(days=days)

    metrics_by_name: dict[str, dict] = {}
    for b in metrics_backends:
        metrics_by_name[b.get("backend", "")] = b

    # Config + runtime health
    health_report: list[dict] = []
    if llm_router:
        sources.append("runtime")
        try:
            health_report = llm_router.health_report()
        except Exception:
            pass
    elif cfg and cfg.llm.backends:
        sources.append("config")

    # Build from health report (includes both enabled and disabled)
    result: list[dict[str, Any]] = []
    seen_names: set[str] = set()

    for entry in health_report:
        name = entry.get("name", "")
        seen_names.add(name)
        backend_entry = _build_backend_entry(name, entry, metrics_by_name.get(name, {}), cfg)
        result.append(backend_entry)

    # Add any backends from config that weren't in health report
    if cfg and cfg.llm.backends:
        if "config" not in sources:
            sources.append("config")
        for bcfg in cfg.llm.backends:
            if bcfg.name not in seen_names:
                entry = {
                    "name": bcfg.name,
                    "type": "openai_compatible",
                    "url": _mask_url(bcfg.base_url),
                    "status": "disabled" if not bcfg.enabled else "unknown",
                    "models_configured": list(bcfg.models),
                    "models_detected": [],
                    "latency_ms": None,
                    "last_error": None,
                    "privacy_level": bcfg.privacy_level,
                    "priority": bcfg.priority,
                }
                backend_entry = _build_backend_entry(bcfg.name, entry, metrics_by_name.get(bcfg.name, {}), cfg)
                result.append(backend_entry)

    # Add backends from metrics that weren't in config (historical)
    for bname, mdata in metrics_by_name.items():
        if bname and bname not in seen_names:
            backend_entry = _build_backend_entry(bname, {}, mdata, cfg)
            backend_entry["health_status"] = "historical"
            result.append(backend_entry)

    # Summary
    total = len(result)
    healthy = sum(1 for r in result if r["health_status"] == "healthy")
    degraded = sum(1 for r in result if r["health_status"] in ("degraded", "unavailable"))
    offline = sum(1 for r in result if r["health_status"] in ("disabled", "offline"))
    most_used = max(result, key=lambda r: r["total_calls"], default=None)
    fastest_b = min(
        (r for r in result if (r.get("health_latency_ms") or 0) > 0),
        key=lambda r: r["health_latency_ms"],
        default=None,
    )

    return {
        "data": result,
        "summary": {
            "total_backends": total,
            "healthy_backends": healthy,
            "degraded_backends": degraded,
            "offline_backends": offline,
            "most_used_backend": most_used["backend_name"] if most_used and most_used["total_calls"] > 0 else None,
            "fastest_backend": fastest_b["backend_name"] if fastest_b else None,
            "fastest_latency_ms": fastest_b["health_latency_ms"] if fastest_b else None,
        },
        "data_sources_used": sources,
    }


def _build_backend_entry(name: str, health_data: dict, metrics_data: dict, cfg) -> dict[str, Any]:
    """Build a unified backend entry from health + metrics + config."""
    entry: dict[str, Any] = {
        "backend_name": name,
        "backend_type": health_data.get("type", "openai_compatible"),
        "base_url": health_data.get("url", ""),
        "privacy_level": health_data.get("privacy_level", "local"),
        "enabled": health_data.get("status") != "disabled",
        "health_status": health_data.get("status", "unknown"),
        "last_health_check_at": None,
        "health_latency_ms": health_data.get("latency_ms"),
        "last_error": health_data.get("last_error"),
        "configured_models": health_data.get("models_configured", []),
        "detected_models": health_data.get("models_detected", []),
        "available_models_count": len(health_data.get("models_detected", []) or health_data.get("models_configured", [])),
        "priority": health_data.get("priority", 99),
        # Metrics
        "total_calls": 0,
        "unique_sessions": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "avg_latency_ms": 0,
        "p95_latency_ms": 0,
        "tokens_per_second": 0,
        "error_count": 0,
        "error_rate": 0,
        "fallback_in_count": 0,
        "fallback_out_count": 0,
        "last_used_at": None,
    }

    # Runtime call stats from router (in-memory)
    if health_data.get("calls"):
        entry["total_calls"] = health_data["calls"]
        entry["avg_latency_ms"] = health_data.get("avg_call_latency_ms", 0) or 0

    # Merge metrics
    if metrics_data:
        entry["total_calls"] = max(entry["total_calls"], metrics_data.get("queries", 0) or metrics_data.get("requests", 0) or 0)
        entry["unique_sessions"] = metrics_data.get("unique_sessions", 0)
        entry["prompt_tokens"] = metrics_data.get("prompt_tokens", 0)
        entry["completion_tokens"] = metrics_data.get("completion_tokens", 0)
        entry["total_tokens"] = metrics_data.get("total_tokens", 0)
        entry["avg_latency_ms"] = round(metrics_data.get("avg_latency_ms", 0) or 0, 1)
        entry["p95_latency_ms"] = round(metrics_data.get("p95_latency_ms", 0) or 0, 1)
        entry["error_count"] = metrics_data.get("errors", 0) or metrics_data.get("error_count", 0) or 0
        calls = entry["total_calls"] or 1
        entry["error_rate"] = round((entry["error_count"] / calls) * 100, 1)
        entry["fallback_in_count"] = metrics_data.get("fallback_in", 0) or 0
        entry["fallback_out_count"] = metrics_data.get("fallback_out", 0) or 0
        entry["last_used_at"] = metrics_data.get("last_used_at")
        if entry["total_tokens"] > 0 and entry["avg_latency_ms"] > 0:
            entry["tokens_per_second"] = round(
                (entry["completion_tokens"] or entry["total_tokens"]) / (entry["avg_latency_ms"] / 1000), 1
            )

    return entry


def _mask_url(url: str) -> str:
    """Mask credentials in URL."""
    try:
        from urllib.parse import urlparse
        p = urlparse(url)
        if p.password:
            netloc = f"{p.hostname}:{p.port}" if p.port else (p.hostname or "")
            return p._replace(netloc=netloc).geturl()
    except Exception:
        pass
    return url


# ------------------------------------------------------------------
# Sessions (from sessions.db, enriched with metrics)
# ------------------------------------------------------------------


@router.get("/sessions")
def dashboard_sessions(
    days: int = Query(default=7, ge=1, le=365),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """Paginated list of real sessions from sessions.db."""
    svc = _get_analytics()
    if svc is None:
        return {"data": [], "total": 0, "data_sources_used": []}
    return svc.sessions_list(days=days, limit=limit, offset=offset)


@router.get("/session/{session_id}")
def dashboard_session_detail(session_id: str):
    """Detailed view of a single session."""
    svc = _get_analytics()
    if svc is None:
        raise HTTPException(status_code=503, detail="Analytics service not available")
    detail = svc.session_detail(session_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return detail


# ------------------------------------------------------------------
# Resources (System Hardware)
# ------------------------------------------------------------------


@router.get("/resources")
def dashboard_resources():
    """Current system resource snapshot — GPU, RAM, CPU, loaded models."""
    result: dict[str, Any] = {"data_sources_used": []}

    # Get latest snapshot from DB
    svc = _get_analytics()
    if svc and svc._metrics and hasattr(svc._metrics, '_store') and svc._metrics._store:
        try:
            store = svc._metrics._store
            snapshot = store.get_latest_resource_snapshot()
            if snapshot:
                result["snapshot"] = snapshot
                result["data_sources_used"].append("metrics_db")
        except Exception:
            pass

    # If no DB snapshot or it's stale (>60s), collect live
    import json as _json
    snapshot = result.get("snapshot")
    if not snapshot or (time.time() - (snapshot.get("timestamp") or 0)) > 60:
        try:
            from orchestrator.observability.resources import get_resource_collector
            live = get_resource_collector().snapshot()
            if live:
                result["snapshot"] = {**live, "timestamp": time.time()}
                if "metrics_db" not in result["data_sources_used"]:
                    result["data_sources_used"].append("live")
        except Exception:
            pass

    # Parse models_loaded_json for convenience
    snap = result.get("snapshot", {})
    if snap.get("models_loaded_json"):
        try:
            snap["models_loaded"] = _json.loads(snap["models_loaded_json"])
        except (ValueError, TypeError):
            snap["models_loaded"] = []
    else:
        snap["models_loaded"] = []

    return result


@router.get("/resources/history")
def dashboard_resources_history(hours: int = Query(default=6, ge=1, le=168)):
    """Resource snapshot time-series for the last N hours."""
    svc = _get_analytics()
    if svc and svc._metrics and hasattr(svc._metrics, '_store') and svc._metrics._store:
        try:
            store = svc._metrics._store
            history = store.get_resource_history(hours=hours)
            return {"data": history, "data_sources_used": ["metrics_db"]}
        except Exception:
            pass
    return {"data": [], "data_sources_used": []}


# ------------------------------------------------------------------
# Performance
# ------------------------------------------------------------------


@router.get("/performance")
def dashboard_performance(days: int = Query(default=7, ge=1, le=365)):
    """Latency percentiles and performance breakdown."""
    svc = _get_analytics()
    if svc is None:
        return {"data": {}, "data_sources_used": []}
    return svc.performance(days=days)


@router.get("/performance/detailed")
def dashboard_performance_detailed(days: int = Query(default=7, ge=1, le=365)):
    """Detailed performance metrics — warm/cold status, latency breakdown, tokens/sec."""
    result: dict[str, Any] = {"data_sources_used": []}

    # 1. Warm/cold status per model
    try:
        from orchestrator.core.warmup import get_warmup_manager
        mgr = get_warmup_manager()
        warm_status = mgr.get_warm_status(force_refresh=True)
        cfg = _get_settings()
        all_models = set()
        if cfg:
            for b in cfg.llm.backends:
                if b.enabled:
                    all_models.update(b.models)

        models_status = []
        for model in sorted(all_models):
            status = warm_status.get(model)
            models_status.append({
                "model": model,
                "warm": bool(status and status.warm),
                "vram_bytes": status.vram_bytes if status else 0,
                "expires_at": status.expires_at if status else None,
            })
        result["models_status"] = models_status
        result["data_sources_used"].append("runtime")
    except Exception:
        result["models_status"] = []

    # 2. Per-model latency from router
    try:
        llm_router = _get_llm_router()
        if llm_router:
            model_latencies = {}
            for model in all_models:
                avg = llm_router.get_model_avg_latency(model)
                p95 = llm_router.get_model_p95_latency(model)
                if avg is not None:
                    model_latencies[model] = {
                        "avg_latency_ms": round(avg, 1),
                        "p95_latency_ms": round(p95, 1) if p95 else None,
                    }
            result["model_latencies"] = model_latencies
    except Exception:
        result["model_latencies"] = {}

    # 3. Granular metrics from metrics.db
    svc = _get_analytics()
    if svc and svc._metrics and hasattr(svc._metrics, '_store') and svc._metrics._store:
        try:
            store = svc._metrics._store
            conn = store._conn if store and hasattr(store, "_conn") else None
            if conn:
                cutoff = time.time() - (days * 86400)
                # Cold start rate
                row = conn.execute(_sql("execute_635.sql"), (cutoff,)).fetchone()

                if row:
                    total = row["total"] or 0
                    result["cold_start_rate"] = round(
                        (row["cold_starts"] or 0) / max(total, 1), 3
                    )
                    result["avg_context_build_ms"] = round(row["avg_context_build_ms"] or 0, 1)
                    result["avg_model_load_ms"] = round(row["avg_model_load_ms"] or 0, 1)
                    result["avg_prompt_eval_ms"] = round(row["avg_prompt_eval_ms"] or 0, 1)
                    result["avg_generation_ms"] = round(row["avg_generation_ms"] or 0, 1)
                    result["avg_prompt_tps"] = round(row["avg_prompt_tps"] or 0, 1)
                    result["avg_generation_tps"] = round(row["avg_gen_tps"] or 0, 1)

                # Per-model latency breakdown from DB
                model_breakdown = conn.execute(_sql("execute_661.sql"), (cutoff,)).fetchall()

                result["model_breakdown"] = [
                    {
                        "model": r["model"],
                        "queries": r["queries"],
                        "avg_load_ms": round(r["avg_load_ms"] or 0, 1),
                        "avg_prompt_eval_ms": round(r["avg_prompt_eval_ms"] or 0, 1),
                        "avg_generation_ms": round(r["avg_gen_ms"] or 0, 1),
                        "avg_first_token_ms": round(r["avg_first_token_ms"] or 0, 1),
                        "avg_generation_tps": round(r["avg_gen_tps"] or 0, 1),
                        "avg_prompt_tps": round(r["avg_prompt_tps"] or 0, 1),
                        "cold_start_rate": round(
                            (r["cold_starts"] or 0) / max(r["queries"], 1), 3
                        ),
                    }
                    for r in model_breakdown
                ]
                result["data_sources_used"].append("metrics_db")
        except Exception as exc:
            log.debug("performance/detailed: metrics query failed: %s", exc)

    # 4. Profile recommendations
    cfg = _get_settings()
    if cfg:
        result["profile_models"] = {
            "fast": cfg.models.fast,
            "default": cfg.models.default,
            "code": cfg.models.code,
            "deep": cfg.models.deep,
        }
        result["data_sources_used"].append("config")

    return result


# ------------------------------------------------------------------
# Fallbacks & Errors
# ------------------------------------------------------------------


@router.get("/fallbacks")
def dashboard_fallbacks(days: int = Query(default=7, ge=1, le=365)):
    """Fallback event analysis."""
    svc = _get_analytics()
    if svc is None:
        return {"data": [], "data_sources_used": []}
    return svc.fallbacks(days=days)


@router.get("/errors")
def dashboard_errors(days: int = Query(default=7, ge=1, le=365)):
    """Error breakdown."""
    svc = _get_analytics()
    if svc is None:
        return {"data": [], "data_sources_used": []}
    return svc.errors(days=days)


# ------------------------------------------------------------------
# Recent / Live Feed
# ------------------------------------------------------------------


@router.get("/recent")
def dashboard_recent(limit: int = Query(default=20, ge=1, le=100)):
    """Most recent events from metrics.db."""
    svc = _get_analytics()
    if svc is None:
        return {"data": [], "data_sources_used": []}
    return svc.recent(limit=limit)


# ------------------------------------------------------------------
# Export
# ------------------------------------------------------------------


@router.get("/export.csv")
def dashboard_export(days: int = Query(default=30, ge=1, le=365)):
    """Export raw metrics as CSV."""
    svc = _get_analytics()
    if svc is None or not svc.metrics_available:
        return Response(content="", media_type="text/csv")

    rows = svc.metrics_reader.export_csv_rows(days=days)
    if not rows:
        return Response(content="", media_type="text/csv")

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(rows[0].keys())
    for row in rows:
        writer.writerow(tuple(row))

    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=metrics_{days}d.csv"},
    )


# ------------------------------------------------------------------
# Diagnostics — comprehensive system state
# ------------------------------------------------------------------


@router.get("/diagnostics")
def dashboard_diagnostics():
    """Comprehensive diagnostics — data sources, auth, endpoints, health."""
    svc = _get_analytics()
    cfg = _get_settings()
    llm_router = _get_llm_router()
    collector = _get_collector()

    result: dict[str, Any] = {
        "dashboard_enabled": True,
        "auth_mode": "api_key",
        "auth_exempt_routes": ["/favicon.ico", "/health", "/live"],
        "static_files_mounted": (_WEB_DIR / "dashboard.html").exists(),
        "sessions_db_path": None,
        "sessions_db_exists": False,
        "metrics_db_path": None,
        "metrics_db_exists": False,
        "data_sources_used": [],
        "available_endpoints": [
            "/dashboard/summary", "/dashboard/timeline", "/dashboard/models",
            "/dashboard/backends", "/dashboard/sessions", "/dashboard/performance",
            "/dashboard/diagnostics", "/dashboard/events", "/dashboard/export.csv",
        ],
        "sse_enabled": collector is not None,
        "current_sse_clients": 0,
        "backend_count": 0,
        "model_count": 0,
        "last_error": None,
    }

    if cfg:
        if cfg.session.enabled and cfg.session.db_path:
            result["sessions_db_path"] = cfg.session.db_path
            result["sessions_db_exists"] = Path(cfg.session.db_path).exists()
        metrics_path = cfg.metrics.db_path or str(symbiont_data_path("symbiont", "metrics.db"))
        result["metrics_db_path"] = metrics_path
        result["metrics_db_exists"] = Path(metrics_path).expanduser().exists()
        result["backend_count"] = len(cfg.llm.backends)
        result["model_count"] = len(set(
            m for b in cfg.llm.backends for m in b.models
        ))

    if svc:
        sources = []
        if svc.sessions_available:
            sources.append("sessions_db")
        if svc.metrics_available:
            sources.append("metrics_db")
        result["data_sources_used"] = sources
        result["sessions_db"] = {"available": svc.sessions_available}
        result["metrics_db"] = {"available": svc.metrics_available}
    else:
        result["sessions_db"] = {"available": False}
        result["metrics_db"] = {"available": False}

    if llm_router:
        try:
            report = llm_router.health_report()
            healthy = sum(1 for r in report if r.get("status") == "healthy")
            result["backends_healthy"] = healthy
            result["backends_total"] = len(report)
            result["runtime_models_detected"] = len(llm_router.list_models())
        except Exception as exc:
            result["last_error"] = str(exc)[:200]

    if collector:
        result["current_sse_clients"] = len(getattr(collector, '_sse_subscribers', []))

    return result


# ------------------------------------------------------------------
# SSE Real-time Feed
# ------------------------------------------------------------------


@router.get("/events")
async def dashboard_events(request: Request):
    """Server-Sent Events stream of real-time metrics events."""
    collector = _get_collector()

    async def event_generator() -> AsyncIterator[str]:
        if collector is None:
            yield f'data: {{"type": "connected", "timestamp": {time.time()}, "message": "SSE active (no collector)"}}\n\n'
            while True:
                await asyncio.sleep(10)
                try:
                    if await request.is_disconnected():
                        break
                except Exception:
                    break
                yield f'data: {{"type": "dashboard_ping", "timestamp": {time.time()}}}\n\n'
            return

        sub_q = collector.subscribe_sse()
        try:
            yield f'data: {{"type": "connected", "timestamp": {time.time()}, "message": "SSE connected"}}\n\n'
            ping_counter = 0
            while True:
                try:
                    if await request.is_disconnected():
                        break
                except Exception:
                    break
                try:
                    event = sub_q.get_nowait()
                    data = {
                        "type": "llm_call_completed",
                        "request_id": event.request_id,
                        "timestamp": event.timestamp,
                        "model": event.model,
                        "backend": event.backend,
                        "latency_ms": round(event.latency_ms, 1),
                        "total_tokens": event.usage.total_tokens if event.usage else None,
                        "prompt_tokens": event.usage.prompt_tokens if event.usage else None,
                        "completion_tokens": event.usage.completion_tokens if event.usage else None,
                        "stream": event.stream,
                        "agentic": event.agentic,
                        "success": event.success,
                        "error_type": event.error_type,
                        "intent": event.router_decision.intent if event.router_decision else None,
                        "session_id": event.session_id,
                        "fallback_used": event.router_decision.fallback_used if event.router_decision else False,
                    }
                    yield f"data: {json.dumps(data)}\n\n"
                    ping_counter = 0
                except queue.Empty:
                    await asyncio.sleep(1)
                    ping_counter += 1
                    if ping_counter >= 10:
                        yield f'data: {{"type": "dashboard_ping", "timestamp": {time.time()}}}\n\n'
                        ping_counter = 0
        finally:
            collector.unsubscribe_sse(sub_q)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ------------------------------------------------------------------
# Graph Tracing (LangGraph observability)
# ------------------------------------------------------------------


@router.get("/graph/overview")
def dashboard_graph_overview(days: int = Query(default=7, ge=1, le=365)):
    """Graph execution KPIs."""
    svc = _get_analytics()
    if svc is None or not svc.metrics_available:
        return {"error": "Analytics service not available", "data_sources_used": []}
    reader = svc.metrics_reader
    if not hasattr(reader, "graph_overview"):
        return {"error": "Graph tracing not supported by current backend", "data_sources_used": []}
    data = reader.graph_overview(days=days)
    data["data_sources_used"] = ["clickhouse"]
    return data


@router.get("/graph/traces")
def dashboard_graph_traces(
    days: int = Query(default=7, ge=1, le=365),
    limit: int = Query(default=50, ge=1, le=200),
):
    """List recent graph execution traces."""
    svc = _get_analytics()
    if svc is None or not svc.metrics_available:
        return {"traces": [], "data_sources_used": []}
    reader = svc.metrics_reader
    if not hasattr(reader, "graph_traces"):
        return {"traces": [], "data_sources_used": []}
    traces = reader.graph_traces(days=days, limit=limit)
    return {"traces": traces, "data_sources_used": ["clickhouse"]}


@router.get("/graph/trace/{run_id}")
def dashboard_graph_trace_detail(run_id: str):
    """Waterfall detail for a single graph execution."""
    if not run_id or len(run_id) > 64:
        raise HTTPException(status_code=400, detail="Invalid run_id")
    svc = _get_analytics()
    if svc is None or not svc.metrics_available:
        return {"error": "Analytics service not available", "data_sources_used": []}
    reader = svc.metrics_reader
    if not hasattr(reader, "graph_trace_detail"):
        return {"error": "Not supported", "data_sources_used": []}
    data = reader.graph_trace_detail(run_id)
    data["data_sources_used"] = ["clickhouse"]
    return data


@router.get("/graph/stats")
def dashboard_graph_stats(days: int = Query(default=7, ge=1, le=365)):
    """Per-node aggregated statistics."""
    svc = _get_analytics()
    if svc is None or not svc.metrics_available:
        return {"nodes": [], "data_sources_used": []}
    reader = svc.metrics_reader
    if not hasattr(reader, "graph_node_stats"):
        return {"nodes": [], "data_sources_used": []}
    nodes = reader.graph_node_stats(days=days)
    return {"nodes": nodes, "data_sources_used": ["clickhouse"]}


@router.get("/graph/timeline")
def dashboard_graph_timeline(
    days: int = Query(default=7, ge=1, le=365),
    resolution: str = Query(default="hour", pattern="^(day|hour)$"),
):
    """Graph execution timeline."""
    svc = _get_analytics()
    if svc is None or not svc.metrics_available:
        return {"data": [], "data_sources_used": []}
    reader = svc.metrics_reader
    if not hasattr(reader, "graph_timeline"):
        return {"data": [], "data_sources_used": []}
    data = reader.graph_timeline(days=days, resolution=resolution)
    return {"data": data, "data_sources_used": ["clickhouse"]}


@router.get("/graph/slow")
def dashboard_graph_slow_nodes(
    days: int = Query(default=7, ge=1, le=365),
    threshold_ms: float = Query(default=500, ge=0),
):
    """Nodes exceeding a latency threshold."""
    svc = _get_analytics()
    if svc is None or not svc.metrics_available:
        return {"nodes": [], "data_sources_used": []}
    reader = svc.metrics_reader
    if not hasattr(reader, "graph_slow_nodes"):
        return {"nodes": [], "data_sources_used": []}
    nodes = reader.graph_slow_nodes(days=days, threshold_ms=threshold_ms)
    return {"nodes": nodes, "data_sources_used": ["clickhouse"]}


# ------------------------------------------------------------------
# Static Files (CSS, JS)
# ------------------------------------------------------------------

_WEB_DIR = Path(__file__).parent.parent.parent.parent.parent / "web" / "orc"


def _allowed_dashboard_assets(web_root: Path, allowed_ext: set[str]) -> dict[str, Path]:
    try:
        return {
            item.name: item
            for item in web_root.iterdir()
            if item.is_file() and item.suffix in allowed_ext
        }
    except OSError:
        return {}


@router.get("/assets/{filename}")
def dashboard_asset(filename: str):
    """Serve static assets (CSS, JS) from the web/ directory."""
    allowed_ext = {".css", ".js", ".svg", ".png", ".ico", ".woff2", ".woff"}
    if "\x00" in filename or "/" in filename or "\\" in filename:
        raise HTTPException(status_code=404)
    if Path(filename).name != filename:
        raise HTTPException(status_code=404)
    web_root = Path(os.path.realpath(os.path.abspath(os.fspath(_WEB_DIR))))
    path = _allowed_dashboard_assets(web_root, allowed_ext).get(filename)
    if path is None:
        raise HTTPException(status_code=404)

    media_types = {
        ".css": "text/css",
        ".js": "application/javascript",
        ".svg": "image/svg+xml",
        ".png": "image/png",
        ".ico": "image/x-icon",
        ".woff2": "font/woff2",
        ".woff": "font/woff",
    }
    return FileResponse(path, media_type=media_types.get(path.suffix, "application/octet-stream"))


# ------------------------------------------------------------------
# Dashboard UI
# ------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
def dashboard_ui():
    """Serve the dashboard single-page app."""
    html_path = _WEB_DIR / "dashboard.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text())
    return HTMLResponse(
        "<html><body style='background:#0f1419;color:#e2e8f0;font-family:sans-serif;padding:2rem'>"
        "<h1>Dashboard</h1><p>web/dashboard.html not found. "
        "Run from the project root directory.</p></body></html>"
    )


# ------------------------------------------------------------------
# Gemilyni — Gemini Execution Layer Dashboard API
# ------------------------------------------------------------------


@router.get("/gemilyni/summary")
def gemilyni_summary(hours: int = Query(default=24, ge=1, le=720)):
    """Summary metrics for the Gemilyni dashboard tab."""
    try:
        from orchestrator.observability.gemilyni import query_gemilyni_summary
        return query_gemilyni_summary(hours=hours)
    except Exception as exc:
        log.debug("Gemilyni summary error: %s", exc)
        return _gemilyni_empty_summary()


@router.get("/gemilyni/runs")
def gemilyni_runs(
    hours: int = Query(default=24, ge=1, le=720),
    limit: int = Query(default=50, ge=1, le=500),
    run_id: str = Query(default=""),
    status: str = Query(default=""),
    complexity: str = Query(default=""),
):
    """List recent Gemilyni execution runs."""
    try:
        from orchestrator.observability.gemilyni import query_gemilyni_runs
        return query_gemilyni_runs(
            hours=hours, limit=limit,
            run_id=run_id, status=status, complexity=complexity,
        )
    except Exception as exc:
        log.debug("Gemilyni runs error: %s", exc)
        return {"runs": []}


@router.get("/gemilyni/workers")
def gemilyni_workers(
    hours: int = Query(default=24, ge=1, le=720),
    run_id: str = Query(default=""),
    worker_id: str = Query(default=""),
):
    """List workers for given time range or run."""
    try:
        from orchestrator.observability.gemilyni import query_gemilyni_workers
        return query_gemilyni_workers(
            hours=hours, run_id=run_id, worker_id=worker_id,
        )
    except Exception as exc:
        log.debug("Gemilyni workers error: %s", exc)
        return {"workers": []}


@router.get("/gemilyni/containers")
def gemilyni_containers(
    hours: int = Query(default=24, ge=1, le=720),
    run_id: str = Query(default=""),
):
    """List containers for given time range or run."""
    try:
        from orchestrator.observability.gemilyni import query_gemilyni_containers
        return query_gemilyni_containers(
            hours=hours, run_id=run_id,
        )
    except Exception as exc:
        log.debug("Gemilyni containers error: %s", exc)
        return {"containers": []}


@router.get("/gemilyni/bundles")
def gemilyni_bundles(
    hours: int = Query(default=24, ge=1, le=720),
    run_id: str = Query(default=""),
):
    """List execution bundles."""
    try:
        from orchestrator.observability.gemilyni import query_gemilyni_bundles
        return query_gemilyni_bundles(hours=hours, run_id=run_id)
    except Exception as exc:
        log.debug("Gemilyni bundles error: %s", exc)
        return {"bundles": []}


@router.get("/gemilyni/files")
def gemilyni_files(
    run_id: str = Query(default=""),
    bundle_id: str = Query(default=""),
    limit: int = Query(default=100, ge=1, le=1000),
):
    """List files included/blocked in bundles."""
    try:
        from orchestrator.observability.gemilyni import query_gemilyni_files
        return query_gemilyni_files(
            run_id=run_id, bundle_id=bundle_id, limit=limit,
        )
    except Exception as exc:
        log.debug("Gemilyni files error: %s", exc)
        return {"files": []}


@router.get("/gemilyni/context")
def gemilyni_context(
    run_id: str = Query(default=""),
    bundle_id: str = Query(default=""),
):
    """List context blocks included/blocked."""
    try:
        from orchestrator.observability.gemilyni import query_gemilyni_context
        return query_gemilyni_context(run_id=run_id, bundle_id=bundle_id)
    except Exception as exc:
        log.debug("Gemilyni context error: %s", exc)
        return {"blocks": []}


@router.get("/gemilyni/policy")
def gemilyni_policy(
    hours: int = Query(default=24, ge=1, le=720),
    run_id: str = Query(default=""),
):
    """List policy violations."""
    try:
        from orchestrator.observability.gemilyni import query_gemilyni_policy
        return query_gemilyni_policy(hours=hours, run_id=run_id)
    except Exception as exc:
        log.debug("Gemilyni policy error: %s", exc)
        return {"violations": []}


@router.get("/gemilyni/errors")
def gemilyni_errors(
    hours: int = Query(default=24, ge=1, le=720),
    run_id: str = Query(default=""),
):
    """List errors across execution phases."""
    try:
        from orchestrator.observability.gemilyni import query_gemilyni_errors
        return query_gemilyni_errors(hours=hours, run_id=run_id)
    except Exception as exc:
        log.debug("Gemilyni errors error: %s", exc)
        return {"errors": []}


@router.get("/gemilyni/performance")
def gemilyni_performance(hours: int = Query(default=24, ge=1, le=720)):
    """Performance breakdown by phase."""
    try:
        from orchestrator.observability.gemilyni import query_gemilyni_performance
        return query_gemilyni_performance(hours=hours)
    except Exception as exc:
        log.debug("Gemilyni performance error: %s", exc)
        return {"phases": {}}


def _gemilyni_empty_summary() -> dict:
    """Return empty gemilyni summary."""
    return {
        "total_runs": 0,
        "external_runs": 0,
        "local_runs": 0,
        "success_rate": 0,
        "failure_rate": 0,
        "fallbacks": 0,
        "policy_blocks": 0,
        "containers_created": 0,
        "workers_executed": 0,
        "avg_duration_ms": 0,
        "avg_gemini_ms": 0,
        "sensitive_blocked": 0,
        "files_blocked": 0,
        "sources_blocked": 0,
        "violations": 0,
        "traversal_attempts": 0,
    }


# ---------------------------------------------------------------------------
# Gemilyni simulation endpoint — inject algorithm execution + multi-container
# collaboration scenarios into the live event buffer
# ---------------------------------------------------------------------------

import random  # noqa: E402
import uuid as _uuid  # noqa: E402

_ALGORITHM_SCENARIOS = [
    {
        "name": "Distributed Merge Sort",
        "description": "4 containers split dataset, sort partitions, merge results",
        "containers": 4,
        "phases": ["partition", "sort_local", "exchange", "merge_global"],
        "complexity": "high",
        "intent": "algorithm_execution",
    },
    {
        "name": "MapReduce Word Count",
        "description": "3 mappers + 1 reducer count word frequencies in corpus",
        "containers": 4,
        "phases": ["map_split", "map_count", "shuffle", "reduce_aggregate"],
        "complexity": "high",
        "intent": "distributed_computing",
    },
    {
        "name": "Collaborative RAG Pipeline",
        "description": "Agent A retrieves docs, Agent B ranks, Agent C synthesizes answer",
        "containers": 3,
        "phases": ["retrieve", "rerank", "synthesize"],
        "complexity": "high",
        "intent": "rag_pipeline",
    },
    {
        "name": "Multi-Agent Code Review",
        "description": "Linter agent, Security agent, Style agent review code in parallel",
        "containers": 3,
        "phases": ["parse_ast", "lint_check", "security_scan", "style_review", "merge_report"],
        "complexity": "medium",
        "intent": "code_review",
    },
    {
        "name": "Genetic Algorithm Optimization",
        "description": "Population split across containers, evolve + crossover + select",
        "containers": 5,
        "phases": ["init_population", "fitness_eval", "selection", "crossover", "mutation", "converge"],
        "complexity": "high",
        "intent": "optimization",
    },
    {
        "name": "Microservices API Builder",
        "description": "Frontend agent + Backend agent + DB agent collaborate on REST API",
        "containers": 3,
        "phases": ["schema_design", "backend_impl", "frontend_impl", "integration_test"],
        "complexity": "high",
        "intent": "code_generation",
    },
    {
        "name": "ML Training Pipeline",
        "description": "Data prep container feeds training container feeds eval container",
        "containers": 3,
        "phases": ["data_preprocessing", "feature_engineering", "model_training", "evaluation"],
        "complexity": "high",
        "intent": "ml_pipeline",
    },
    {
        "name": "Consensus Protocol Simulation",
        "description": "5 nodes run Raft-like consensus, elect leader, replicate log",
        "containers": 5,
        "phases": ["node_init", "leader_election", "log_replication", "commit", "snapshot"],
        "complexity": "high",
        "intent": "distributed_systems",
    },
]


def _simulate_algorithm_scenario(scenario: dict) -> dict:
    """Generate full event trace for one algorithm scenario."""
    from orchestrator.observability.gemilyni import (
        emit_bundle_created,
        emit_container_created,
        emit_container_started,
        emit_container_stats,
        emit_error,
        emit_execution_finished,
        emit_external_context_policy,
        emit_gemini_invocation_finished,
        emit_gemini_invocation_started,
        emit_policy_violation,
        emit_routing_decision,
        emit_worker_output,
    )

    run_id = f"sim-{_uuid.uuid4().hex[:12]}"
    trace_id = f"trace-{_uuid.uuid4().hex[:8]}"
    num_containers = scenario["containers"]
    phases = scenario["phases"]

    # 1. Routing decision
    emit_routing_decision(
        run_id=run_id,
        trace_id=trace_id,
        selected_path="execute",
        reason=f"Algorithm: {scenario['name']}",
        complexity=scenario["complexity"],
        complexity_threshold="medium",
        intent=scenario["intent"],
        externalizable=True,
        execution_enabled=True,
    )

    # 2. Context policy
    num_blocks = random.randint(3, 8)
    blocked = random.randint(0, 2)
    emit_external_context_policy(
        run_id=run_id,
        trace_id=trace_id,
        original_blocks=num_blocks,
        allowed_blocks=num_blocks - blocked,
        blocked_blocks=blocked,
        allowed_sources=["workspace", "docs", "local_cache"],
        blocked_sources=["external_api"] if blocked > 0 else [],
    )

    # 3. Bundle
    allowed_files = random.randint(3, 12)
    blocked_files = random.randint(0, 3)
    emit_bundle_created(
        run_id=run_id,
        trace_id=trace_id,
        bundle_id=f"bundle-{_uuid.uuid4().hex[:8]}",
        worker_id=f"worker-{scenario['name'].lower().replace(' ', '_')}-00",
        task_type=scenario["intent"],
        allowed_files_count=allowed_files,
        blocked_files_count=blocked_files,
        allowed_context_blocks=num_blocks - blocked,
        blocked_context_blocks=blocked,
        workspace_mode="sandbox",
    )

    # 4. Create containers and simulate inter-container collaboration
    container_ids = []
    worker_ids = []
    for i in range(num_containers):
        cid = f"gemini-{scenario['intent']}-{i:02d}-{_uuid.uuid4().hex[:6]}"
        wid = f"worker-{scenario['name'].lower().replace(' ', '_')}-{i:02d}"
        container_ids.append(cid)
        worker_ids.append(wid)

        emit_container_created(
            run_id=run_id,
            trace_id=trace_id,
            worker_id=wid,
            container_id=cid,
            image=f"gemini-agent:{scenario['intent']}",
            auth_mode="adc",
            mounts_count=random.randint(1, 3),
            network_mode="bridge" if num_containers > 1 else "none",
        )
        emit_container_started(
            run_id=run_id,
            trace_id=trace_id,
            worker_id=wid,
            container_id=cid,
        )

    # 5. Simulate phase-by-phase execution across containers
    total_gemini_ms = 0.0
    total_duration_ms = 0.0
    workers_succeeded = 0
    workers_failed = 0

    for phase_idx, phase in enumerate(phases):
        # Assign container for this phase (round-robin or all for parallel phases)
        assigned_container_idx = phase_idx % num_containers
        cid = container_ids[assigned_container_idx]
        wid = worker_ids[assigned_container_idx]

        # Container stats showing inter-container networking
        emit_container_stats(
            run_id=run_id,
            trace_id=trace_id,
            worker_id=wid,
            container_id=cid,
            cpu_percent=random.uniform(25.0, 95.0),
            memory_usage_bytes=random.randint(100_000_000, 800_000_000),
            memory_limit_bytes=2_000_000_000,
            memory_percent=random.uniform(10.0, 70.0),
            network_rx_bytes=random.randint(50_000, 5_000_000),
            network_tx_bytes=random.randint(50_000, 5_000_000),
        )

        # Gemini invocation for this phase
        phase_duration_ms = random.uniform(800, 12000)
        emit_gemini_invocation_started(
            run_id=run_id,
            trace_id=trace_id,
            worker_id=wid,
            container_id=cid,
            auth_mode="adc",
            command_mode=f"phase:{phase}",
            model="gemini-2.5-pro",
            input_tokens_estimate=random.randint(500, 8000),
        )

        # Simulate occasional failure
        phase_success = random.random() > 0.1
        emit_gemini_invocation_finished(
            run_id=run_id,
            trace_id=trace_id,
            worker_id=wid,
            container_id=cid,
            status="success" if phase_success else "error",
            exit_code=0 if phase_success else 1,
            duration_ms=phase_duration_ms,
            output_tokens_estimate=random.randint(200, 4000),
            stderr_size_bytes=0 if phase_success else random.randint(100, 2000),
            stdout_size_bytes=random.randint(500, 10000),
        )

        total_gemini_ms += phase_duration_ms
        if phase_success:
            workers_succeeded += 1
        else:
            workers_failed += 1

        # Worker output (pass results to next container)
        emit_worker_output(
            run_id=run_id,
            trace_id=trace_id,
            worker_id=wid,
            container_id=cid,
            output_files=[f"{phase}_result.json", f"{phase}_log.txt"],
            result_json_exists=True,
            patch_diff_exists=phase_idx == len(phases) - 1,
            patch_size_bytes=random.randint(500, 5000) if phase_idx == len(phases) - 1 else 0,
            result_size_bytes=random.randint(1000, 20000),
        )

        total_duration_ms += phase_duration_ms + random.uniform(200, 1000)

    # 6. Occasionally emit policy violation (e.g., data leak attempt)
    if random.random() < 0.3:
        emit_policy_violation(
            run_id=run_id,
            trace_id=trace_id,
            policy_name="data_isolation",
            violation_type="cross_container_leak",
            blocked_item_type="context",
            blocked_item_ref=f"container:{container_ids[-1]}→external",
            reason="Container attempted to send data outside sandbox",
            severity="warning",
        )

    # 7. Occasionally emit error
    if random.random() < 0.15:
        emit_error(
            run_id=run_id,
            trace_id=trace_id,
            worker_id=worker_ids[-1],
            container_id=container_ids[-1],
            phase=phases[-1],
            error_type="timeout",
            error_message=f"Phase {phases[-1]} exceeded 30s deadline",
            recoverable=True,
            fallback_used=True,
        )

    # 8. Execution finished
    fallback = workers_failed > 0
    emit_execution_finished(
        run_id=run_id,
        trace_id=trace_id,
        external_used=True,
        workers_total=len(phases),
        workers_succeeded=workers_succeeded,
        workers_failed=workers_failed,
        containers_started=num_containers,
        containers_failed=0,
        total_duration_ms=total_duration_ms,
        planning_duration_ms=random.uniform(100, 800),
        bundle_duration_ms=random.uniform(50, 400),
        container_start_duration_ms=random.uniform(500, 2000),
        gemini_duration_ms=total_gemini_ms,
        synthesis_duration_ms=random.uniform(200, 1500),
        fallback_used=fallback,
        final_status="success" if workers_failed == 0 else "partial_failure",
    )

    return {
        "run_id": run_id,
        "trace_id": trace_id,
        "scenario": scenario["name"],
        "containers": num_containers,
        "phases": len(phases),
        "workers_succeeded": workers_succeeded,
        "workers_failed": workers_failed,
        "total_duration_ms": round(total_duration_ms, 1),
    }


@router.post("/gemilyni/simulate")
async def gemilyni_simulate(
    scenarios: int = Query(default=4, ge=1, le=8, description="Number of scenarios to run"),
):
    """Inject algorithm execution + multi-container collaboration scenarios.

    Each scenario creates a full lifecycle of events: routing → context policy →
    bundle → N containers created/started → phase-by-phase Gemini invocations
    with inter-container data passing → worker outputs → execution finished.
    """
    selected = random.sample(
        _ALGORITHM_SCENARIOS, k=min(scenarios, len(_ALGORITHM_SCENARIOS))
    )
    results = []
    for scenario in selected:
        result = _simulate_algorithm_scenario(scenario)
        results.append(result)
        log.info(
            "Gemilyni simulate: %s → %d containers, %d phases",
            scenario["name"],
            scenario["containers"],
            len(scenario["phases"]),
        )

    total_containers = sum(r["containers"] for r in results)
    total_phases = sum(r["phases"] for r in results)

    return {
        "status": "injected",
        "scenarios_run": len(results),
        "total_containers_created": total_containers,
        "total_phases_executed": total_phases,
        "details": results,
    }
