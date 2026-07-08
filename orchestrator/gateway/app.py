"""FastAPI application — symbiont HTTP server on port 8585."""

from __future__ import annotations

import asyncio
import logging
import os
import queue
import secrets
import threading
import time as _time
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from sharedai.llm.utils import strip_think

from config.storage_paths import symbiont_data_path
from orchestrator import __version__
from orchestrator.config import configure_logging, get_settings
from orchestrator.core.metrics import metrics
from orchestrator.core.sanitize import (
    sanitize_query,
    validate_history,
    validate_session_id,
)
from orchestrator.core.session import SessionStore
from orchestrator.factory import create_engine
from orchestrator.gateway.schemas import (
    BackendHealthSchema,
    ClassifyResponse,
    HealthResponse,
    QueryRequest,
    QueryResponse,
    ToolListResponse,
    ToolSchema,
)

log = logging.getLogger(__name__)

# Module-level engine — created at startup
_engine = None
# LangGraph compiled workflow — new unified orchestration
_graph = None
# Semaphore limiting concurrent LLM calls (Ollama processes 1 at a time on GPU)
_llm_semaphore: asyncio.Semaphore | None = None
# Session store — initialised in lifespan if enabled
_session_store: SessionStore | None = None
# Admission controller — replaces simple semaphore when configured
_admission_controller = None
# Agentic durable runner — optional, disabled by default
_agentic_runner = None
# Agentic safe autonomous event loop — optional, gated behind autonomous_safe
_agentic_event_loop = None
# Agentic reversible actuator — optional, gated behind governed improvement
_agentic_actuator = None

# Paths exempt from API key authentication. Keep this list intentionally tiny:
# liveness/health are required by local healthchecks; inventory, metrics,
# lifecycle, OpenAPI and dashboard data must authenticate.
_AUTH_EXEMPT_PATHS = frozenset({"/live", "/health", "/favicon.ico"})
_INTERNAL_EVENT_BUS_PATHS = frozenset({"/agentic/ai-events"})
_INTERNAL_API_TOKEN_PREFIXES = ("/resources", "/telemetry", "/scheduler")


def _read_secret_file(path: str) -> str:
    if not path:
        return ""
    try:
        from pathlib import Path

        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _configured_internal_api_key() -> str:
    for env_name in ("INTERNAL_API_KEY", "ORC_INTERNAL_API_KEY"):
        token = os.environ.get(env_name, "").strip()
        if token:
            return token
    for env_name in ("INTERNAL_API_KEY_FILE", "ORC_INTERNAL_API_KEY_FILE", "STORAGE_GUARDIAN_INTERNAL_TOKEN_FILE"):
        token = _read_secret_file(os.environ.get(env_name, ""))
        if token:
            return token
    return ""


def _internal_api_token_valid(request: Request) -> bool:
    if request.url.path not in _INTERNAL_EVENT_BUS_PATHS and not any(
        request.url.path.startswith(prefix) for prefix in _INTERNAL_API_TOKEN_PREFIXES
    ):
        return False
    expected = _configured_internal_api_key()
    if not expected:
        return False
    provided = request.headers.get("X-Internal-Token", "") or request.headers.get("X-Internal-API-Key", "")
    return bool(provided) and secrets.compare_digest(provided, expected)


def _internal_event_bus_token_valid(request: Request) -> bool:
    if request.url.path not in _INTERNAL_EVENT_BUS_PATHS:
        return False
    expected = _configured_internal_api_key()
    if not expected:
        return False
    provided = request.headers.get("X-Internal-Token", "") or request.headers.get("X-Internal-API-Key", "")
    return bool(provided) and secrets.compare_digest(provided, expected)


def _model_dump(model):
    if hasattr(model, "model_dump"):
        return model.model_dump()
    if hasattr(model, "dict"):
        return model.dict()
    return model


def _get_graph():
    global _graph
    if _graph is None:
        from orchestrator.factory import create_graph
        _graph = create_graph()
    return _graph


def _get_engine():
    global _engine
    if _engine is None:
        _engine = create_engine()
    return _engine


def _timeout_from_env(name: str, default: float) -> float:
    try:
        return max(0.1, float(os.getenv(name, str(default))))
    except ValueError:
        return default


def _call_with_timeout(label: str, fn, *, timeout: float, fallback):
    result_queue: queue.Queue[tuple[bool, object]] = queue.Queue(maxsize=1)

    def _runner():
        try:
            result_queue.put((True, fn()), block=False)
        except Exception as exc:
            result_queue.put((False, exc), block=False)

    thread = threading.Thread(target=_runner, name=f"orc-{label.replace(' ', '-')}", daemon=True)
    thread.start()
    try:
        ok, result = result_queue.get(timeout=timeout)
    except queue.Empty:
        log.warning("%s timed out after %.1fs", label, timeout)
        return fallback(f"timed out after {timeout:.1f}s")
    if ok:
        return result
    log.warning("%s failed: %s", label, result)
    return fallback(str(result))


def _ensure_lifecycle_service(service_name: str) -> bool:
    """Best-effort lifecycle start for cross-cutting services."""
    try:
        graph = _get_graph()
        registry = getattr(graph, "_service_registry", None)
        lifecycle = getattr(registry, "_lifecycle", None) if registry is not None else None
        if lifecycle is None or not lifecycle.available:
            return False
        return bool(lifecycle.ensure_running(service_name))
    except Exception as exc:
        log.debug("Lifecycle ensure skipped for %s: %s", service_name, exc)
        return False


def _language_runtime_config(cfg) -> dict[str, object]:
    raw = getattr(cfg, "i18n_raw", {}) or {}
    i18n = raw.get("i18n", {})
    latency = raw.get("latency", {})
    final_response = raw.get("final_response", {})
    return {
        "enabled": _env_bool(os.environ.get("ORC_I18N_ENABLED"), bool(i18n.get("enabled", True))),
        "mode": _language_mode(os.environ.get("ORC_I18N_MODE") or i18n.get("mode", "shadow")),
        "default_response_language": str(i18n.get("default_response_language", "same_as_user")),
        "translation_budget_normal_ms": int(latency.get("translation_budget_normal_ms", 250)),
        "translation_budget_shadow_ms": int(latency.get("translation_budget_shadow_ms", 2000)),
        "ptpt_linter_timeout_ms": int(latency.get("ptpt_linter_timeout_ms", 250)),
        "ptpt_linter_enabled": bool(final_response.get("ptpt_linter_enabled", True)),
    }


def _language_mode(value: object) -> str:
    mode = str(value or "shadow").lower()
    return mode if mode in {"off", "shadow", "assisted", "enforce"} else "shadow"


def _env_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _translation_feature_client():
    graph = _get_graph()
    return getattr(graph, "_feature_client", None)


def _build_language_context(text: str, data: dict, *, mode: str, default_response_language: str) -> dict:
    from orchestrator.pipeline.language_context import normalize_language_context

    contract = data.get("language_context")
    if isinstance(contract, dict):
        source_language = str(contract.get("source_language") or data.get("source_language") or "unknown")
        source_variant = str(contract.get("source_variant") or data.get("source_variant") or "unknown")
        fallback_reason = contract.get("fallback_reason")
        protected_spans = contract.get("protected_spans") if isinstance(contract.get("protected_spans"), dict) else {}
        translation_applied = bool(contract.get("translation_applied", data.get("translation_applied", False)))
        context = {
            "original_text": str(contract.get("original_query") or data.get("original") or text),
            "normalized_text": str(contract.get("normalized_query") or data.get("normalized") or text),
            "english_text": str(contract.get("working_query") or data.get("translated") or text),
            "source_language": source_language,
            "source_variant": source_variant,
            "target_language": str(contract.get("target_language") or data.get("target_language") or "en"),
            "user_language": source_variant if source_variant != "unknown" else source_language,
            "response_language": "pt-PT" if source_variant == "pt-PT" else default_response_language,
            "translation_available": translation_applied,
            "translation_latency_ms": float(data.get("latency_ms") or 0.0),
            "translation_cache_hit": bool(contract.get("cache_hit", data.get("cache_hit", False))),
            "protected_spans_count": int(protected_spans.get("count") or data.get("protected_spans_count") or 0),
            "fallback_used": bool(contract.get("fallback_used", fallback_reason is not None)),
            "fallback_reason": str(fallback_reason) if fallback_reason else None,
            "mode": mode,
            "contract_version": str(contract.get("contract_version") or ""),
            "semantic_drift_score": contract.get("semantic_drift_score"),
            "confidence": contract.get("confidence"),
            "translation_safe": bool(contract.get("translation_safe", True)),
            "quality": contract.get("quality") if isinstance(contract.get("quality"), dict) else data.get("quality"),
            "safety_error": contract.get("safety_error") if contract.get("safety_error") else data.get("safety_error"),
            "language_context_contract": contract,
        }
        return normalize_language_context(context, original_query=text) or context
    source_language = str(data.get("source_language") or "unknown")
    source_variant = str(data.get("source_variant") or "unknown")
    fallback_reason = data.get("fallback_reason")
    translation_applied = bool(data.get("translation_applied", False))
    context = {
        "original_text": str(data.get("original") or text),
        "normalized_text": str(data.get("normalized") or text),
        "english_text": str(data.get("translated") or text),
        "source_language": source_language,
        "source_variant": source_variant,
        "target_language": str(data.get("target_language") or "en"),
        "user_language": source_variant if source_variant != "unknown" else source_language,
        "response_language": "pt-PT" if source_variant == "pt-PT" else default_response_language,
        "translation_available": translation_applied,
        "translation_latency_ms": float(data.get("latency_ms") or 0.0),
        "translation_cache_hit": bool(data.get("cache_hit", False)),
        "protected_spans_count": int(data.get("protected_spans_count") or 0),
        "fallback_used": bool(data.get("fallback_used", fallback_reason is not None)),
        "fallback_reason": str(fallback_reason) if fallback_reason else None,
        "mode": mode,
        "semantic_drift_score": data.get("semantic_drift_score"),
        "confidence": data.get("confidence"),
        "translation_safe": bool(data.get("translation_safe", True)),
        "quality": data.get("quality") if isinstance(data.get("quality"), dict) else None,
        "safety_error": data.get("safety_error"),
    }
    return normalize_language_context(context, original_query=text) or context


def _normalize_language_sync(text: str, *, language_config: dict[str, object]) -> dict:
    from orchestrator.pipeline.language_context import language_context_fallback

    mode = _language_mode(language_config.get("mode"))
    if not bool(language_config.get("enabled")) or mode == "off":
        return language_context_fallback(text, reason="language_normalization_disabled", mode=mode)
    if mode in {"assisted", "enforce"}:
        _ensure_lifecycle_service("translation")
    timeout_ms = int(
        language_config.get(
            "translation_budget_shadow_ms" if mode == "shadow" else "translation_budget_normal_ms",
            250,
        )
    )
    client = _translation_feature_client()
    if client is None:
        return language_context_fallback(text, reason="translation_dispatch_unavailable", mode=mode)
    response = client.invoke_endpoint(
        "translation",
        method="POST",
        path="/v1/normalize",
        payload={
            "text": text,
            "source_language_hint": None,
            "target_language": "en",
            "mode": mode,
            "max_latency_ms": timeout_ms,
            "protect_spans": True,
            "spellcheck": False,
            "translate": True,
            "return_debug": False,
        },
        timeout=max(0.05, timeout_ms / 1000),
        policy_action="translation.normalize",
    )
    if not response.success:
        return language_context_fallback(text, reason=f"translation_feature_unavailable:{response.error}", mode=mode)
    return _build_language_context(
        text,
        response.data,
        mode=mode,
        default_response_language=str(language_config.get("default_response_language") or "same_as_user"),
    )


def _create_language_task(text: str, *, cfg) -> tuple[asyncio.Task[dict] | None, dict[str, object]]:
    language_config = _language_runtime_config(cfg)
    mode = _language_mode(language_config.get("mode"))
    if not bool(language_config.get("enabled")) or mode == "off":
        return None, language_config
    if mode in {"assisted", "enforce"}:
        asyncio.create_task(asyncio.to_thread(_ensure_lifecycle_service, "translation"))
    task = asyncio.create_task(asyncio.to_thread(_normalize_language_sync, text, language_config=language_config))
    task.add_done_callback(_language_task_done_callback)
    return task, language_config


async def _resolve_language_context(
    task: asyncio.Task[dict] | None,
    *,
    text: str,
    language_config: dict[str, object],
) -> dict:
    from orchestrator.pipeline.language_context import language_context_fallback

    mode = _language_mode(language_config.get("mode"))
    if task is None:
        return language_context_fallback(text, reason="language_normalization_disabled", mode=mode)
    if mode == "shadow":
        if task.done():
            return task.result()
        return language_context_fallback(text, reason="shadow_not_waited", mode=mode)
    timeout_ms = int(language_config.get("translation_budget_normal_ms", 250))
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=max(0.0, timeout_ms / 1000))
    except asyncio.TimeoutError:
        return language_context_fallback(text, reason="translation_timeout", mode=mode)
    except Exception as exc:
        return language_context_fallback(text, reason=f"translation_failed:{type(exc).__name__}", mode=mode)


async def _resolve_material_language_context(
    task: asyncio.Task[dict] | None,
    *,
    text: str,
    language_config: dict[str, object] | None,
) -> dict:
    from orchestrator.pipeline.language_context import has_usable_english, language_context_fallback

    if language_config is None:
        return language_context_fallback(text, reason="language_normalization_not_configured")
    context = await _resolve_language_context(task, text=text, language_config=language_config)
    user_language = str(context.get("user_language") or "")
    if not user_language.startswith("pt") or has_usable_english(context, text):
        return context

    retry_config = dict(language_config)
    retry_config["translation_budget_normal_ms"] = max(
        int(retry_config.get("translation_budget_normal_ms") or 0),
        180000,
    )
    retry_context = await asyncio.to_thread(_normalize_language_sync, text, language_config=retry_config)
    if has_usable_english(retry_context, text):
        return retry_context
    return context


def _language_task_done_callback(task: asyncio.Task[dict]) -> None:
    try:
        task.result()
    except Exception as exc:
        log.debug("language normalization background task failed: %s", exc)


def _material_language_metadata(
    *,
    original_query: str,
    working_query: str,
    language_context: dict[str, object],
) -> dict[str, object]:
    user_language = str(language_context.get("user_language") or "unknown")
    response_language = str(language_context.get("response_language") or "same_as_user")
    return {
        "original_query": original_query,
        "working_query": working_query,
        "working_language": "en",
        "response_language": response_language,
        "user_language": user_language,
        "internal_contract_language": "en",
        "language_context": language_context,
    }


async def _lint_final_response_via_translation(
    text: str,
    *,
    language_config: dict[str, object],
    language_context: dict[str, object] | None = None,
) -> tuple[str, int]:
    mode = _language_mode(language_config.get("mode"))
    if not bool(language_config.get("enabled")) or mode in {"off", "shadow"}:
        return text, 0
    if not bool(language_config.get("ptpt_linter_enabled")):
        return text, 0
    response_language = str((language_context or {}).get("response_language") or "")
    user_language = str((language_context or {}).get("user_language") or "")
    if response_language != "pt-PT" and user_language != "pt-PT":
        return text, 0
    client = _translation_feature_client()
    if client is None:
        return text, 0
    response = await asyncio.to_thread(
        client.invoke_endpoint,
        "translation",
        method="POST",
        path="/v1/lint-ptpt",
        payload={"text": text, "protect_spans": True},
        timeout=max(0.05, int(language_config.get("ptpt_linter_timeout_ms", 250)) / 1000),
        policy_action="translation.lint_ptpt",
    )
    if not response.success:
        return text, 0
    return str(response.data.get("corrected", text)), len(response.data.get("changes", []) or [])


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    global _llm_semaphore, _session_store, _agentic_runner, _agentic_event_loop, _agentic_actuator
    cfg = get_settings()
    configure_logging(cfg)

    # Determine concurrent LLM slots: use adaptive config if available
    concurrent_slots = cfg.ollama.max_concurrent_llm
    try:
        from orchestrator.core.adaptive_config import get_adaptive_overrides
        overrides = get_adaptive_overrides()
        if overrides.max_concurrent_llm > concurrent_slots:
            concurrent_slots = overrides.max_concurrent_llm
            log.info("Adaptive: max_concurrent_llm=%d (from hardware profile)", concurrent_slots)
    except Exception:
        pass

    _llm_semaphore = asyncio.Semaphore(concurrent_slots)

    # Admission controller (replaces simple semaphore when configured)
    global _admission_controller
    if cfg.admission is not None:
        from orchestrator.core.admission import init_admission_controller
        _admission_controller = init_admission_controller(cfg.admission)

    # Routing policy (maps task types to preferred backends)
    if cfg.routing_policy is not None:
        from orchestrator.llm.routing_policy import init_routing_policy
        init_routing_policy(cfg.routing_policy)

    # Session store
    if cfg.session.enabled:
        _session_store = SessionStore(
            db_path=cfg.session.db_path or None,
            max_messages=cfg.session.max_messages,
        )
        _session_store.cleanup(cfg.session.ttl_seconds)
        log.info("Session store enabled (ttl=%ds)", cfg.session.ttl_seconds)

    log.info(
        "Symbiont starting on %s:%d (max_concurrent_llm=%d)",
        cfg.symbiont.host, cfg.symbiont.port, cfg.ollama.max_concurrent_llm,
    )
    _get_engine()

    try:
        from orchestrator.resource_governor import get_resource_governor_service

        governor = get_resource_governor_service()
        governor.start()
        log.info("Resource Governor started (mode=%s)", governor.effective_policy().mode)
    except Exception as exc:
        log.warning("Resource Governor init failed (non-critical): %s", exc)

    # Default metrics database path (shared between observability and analytics)
    _default_metrics_db = str(symbiont_data_path("symbiont", "metrics.db"))

    # Observability
    try:
        from orchestrator.observability.collector import init_observability
        from orchestrator.observability.metrics_config import DashboardConfig, MetricsConfig
        metrics_cfg = MetricsConfig(
            enabled=cfg.metrics.enabled,
            db_path=cfg.metrics.db_path or _default_metrics_db,
            retention_days=cfg.metrics.retention_days,
            flush_interval_seconds=cfg.metrics.flush_interval_seconds,
            resource_monitor_enabled=cfg.metrics.resource_monitor_enabled,
            resource_interval_seconds=cfg.metrics.resource_interval_seconds,
            vram_warning_mb=cfg.metrics.vram_warning_mb,
            vram_critical_mb=cfg.metrics.vram_critical_mb,
            swap_warning_mb=cfg.metrics.swap_warning_mb,
            swap_critical_mb=cfg.metrics.swap_critical_mb,
        )
        dashboard_cfg = DashboardConfig(enabled=cfg.dashboard.enabled)
        init_observability(metrics_cfg, dashboard_cfg)
    except Exception as exc:
        log.warning("Observability init failed (non-critical): %s", exc)

    # New observability stack (OTel + ClickHouse + JSONL)
    try:
        from orchestrator.observability import init_observability as init_obs_v2
        from orchestrator.observability.config import ObservabilityConfig
        obs_config = ObservabilityConfig.from_dict(cfg.observability_raw)
        init_obs_v2(obs_config)
        log.info("Observability v2 initialized (backend=%s)", obs_config.backend)
    except Exception as exc:
        log.warning("Observability v2 init failed (non-critical): %s", exc)

    # Demo data is useful for dashboard development, but should not run during
    # normal local orchestration because it adds startup work and noisy telemetry.
    if os.environ.get("ORC_GEMILYNI_SEED_DEMO", "").lower() in {"1", "true", "yes", "on"}:
        try:
            from orchestrator.observability.gemilyni import seed_demo_data
            seed_demo_data()
        except Exception as exc:
            log.debug("Gemilyni seed skipped: %s", exc)

    # Analytics service (unified sessions.db + metrics.db reader, or ClickHouse)
    try:
        from orchestrator.analytics import AnalyticsService
        from orchestrator.observability.config import ObservabilityConfig as _ObsCfg
        from orchestrator.observability.dashboard import set_analytics_service

        obs_cfg = _ObsCfg.from_dict(cfg.observability_raw)
        sessions_db_path = cfg.session.db_path if cfg.session.enabled else None
        metrics_db_path = cfg.metrics.db_path or _default_metrics_db

        ch_password = os.environ.get(obs_cfg.clickhouse.password_env, "")

        analytics_svc = AnalyticsService(
            sessions_db_path=sessions_db_path,
            metrics_db_path=metrics_db_path,
            backend=obs_cfg.backend,
            clickhouse_url=obs_cfg.clickhouse.url,
            clickhouse_database=obs_cfg.clickhouse.database,
            clickhouse_username=obs_cfg.clickhouse.username,
            clickhouse_password=ch_password,
        )
        set_analytics_service(analytics_svc)
        log.info(
            "Analytics service: backend=%s available=%s",
            obs_cfg.backend,
            "yes" if analytics_svc.metrics_available else "no",
        )
    except Exception as exc:
        log.warning("Analytics service init failed (non-critical): %s", exc)

    # Model warmup on startup (non-blocking)
    try:
        if cfg.performance.warmup_enabled and cfg.performance.warmup_on_startup:
            import threading

            from orchestrator.core.warmup import get_warmup_manager

            def _do_warmup():
                mgr = get_warmup_manager()
                results = mgr.warm_all()
                warmed = sum(1 for v in results.values() if v)
                log.info("Startup warmup: %d/%d models loaded", warmed, len(results))

            threading.Thread(target=_do_warmup, daemon=True, name="startup-warmup").start()
    except Exception as exc:
        log.warning("Startup warmup failed (non-critical): %s", exc)

    # Predictive prewarming initialization (async — pre-compute embeddings)
    # NOTE: _get_graph() must be called first — create_graph() builds and registers
    # the PrewarmEngine singleton as a side-effect.
    try:
        _get_graph()
    except Exception as exc:
        log.warning("Eager graph init failed (non-critical): %s", exc)
    try:
        from orchestrator.prewarming import get_prewarm_engine
        pw_engine = get_prewarm_engine()
        if pw_engine is not None:
            await pw_engine.initialize()
    except Exception as exc:
        log.warning("Prewarm engine init failed (non-critical): %s", exc)

    # Agentic shadow ledger recovery: mark interrupted work as recovering, but
    # do not execute or replay any task during startup.
    try:
        if cfg.agentic_runtime.enabled and cfg.agentic_runtime.recover_on_startup:
            from orchestrator.agentic.store import get_agentic_store

            recovered = get_agentic_store().recover_non_terminal()
            if recovered:
                log.info("Agentic runtime recovered %d non-terminal task(s)", recovered)
    except Exception as exc:
        log.warning("Agentic runtime init failed (non-critical): %s", exc)

    try:
        if cfg.agentic_runtime.enabled and cfg.agentic_runtime.runner_enabled:
            from orchestrator.agentic.runner import AgenticRunner, set_agentic_runner

            _agentic_runner = AgenticRunner(graph_factory=_get_graph)
            set_agentic_runner(_agentic_runner)
            await _agentic_runner.start()
            log.info("Agentic runner started (max_concurrent=%d)", cfg.agentic_runtime.max_concurrent_tasks)
    except Exception as exc:
        log.warning("Agentic runner start failed (non-critical): %s", exc)

    try:
        if (
            cfg.agentic_runtime.enabled
            and cfg.agentic_runtime.autonomous_safe_enabled
            and cfg.agentic_runtime.event_loop_enabled
        ):
            from orchestrator.agentic.event_loop import AgenticEventLoop, set_agentic_event_loop

            _agentic_event_loop = AgenticEventLoop(health_factory=lambda: _get_engine().health_report())
            set_agentic_event_loop(_agentic_event_loop)
            await _agentic_event_loop.start()
            log.info("Agentic safe event loop started")
    except Exception as exc:
        log.warning("Agentic event loop start failed (non-critical): %s", exc)

    try:
        if (
            cfg.agentic_runtime.enabled
            and cfg.agentic_runtime.autonomous_safe_enabled
            and cfg.agentic_runtime.governed_improvement_enabled
            and cfg.agentic_runtime.actuator_enabled
        ):
            from orchestrator.agentic.actuator import AgenticActuator, set_agentic_actuator

            _agentic_actuator = AgenticActuator()
            set_agentic_actuator(_agentic_actuator)
            await _agentic_actuator.start()
            log.info("Agentic reversible actuator started")
    except Exception as exc:
        log.warning("Agentic actuator start failed (non-critical): %s", exc)

    yield
    if _agentic_actuator is not None:
        try:
            from orchestrator.agentic.actuator import set_agentic_actuator

            await _agentic_actuator.stop()
            set_agentic_actuator(None)
        except Exception:
            pass
        _agentic_actuator = None

    if _agentic_event_loop is not None:
        try:
            from orchestrator.agentic.event_loop import set_agentic_event_loop

            await _agentic_event_loop.stop()
            set_agentic_event_loop(None)
        except Exception:
            pass
        _agentic_event_loop = None

    if _agentic_runner is not None:
        try:
            from orchestrator.agentic.runner import set_agentic_runner

            await _agentic_runner.stop()
            set_agentic_runner(None)
        except Exception:
            pass
        _agentic_runner = None

    if _session_store is not None:
        _session_store.close()

    try:
        from orchestrator.resource_governor import get_resource_governor_service

        get_resource_governor_service().stop()
    except Exception:
        pass

    # Close HTTP connection pool (v1.2)
    try:
        from orchestrator.llm.http_pool import close_pool
        await close_pool()
    except Exception:
        pass

    # Observability shutdown
    try:
        from orchestrator.observability.collector import shutdown as obs_shutdown
        obs_shutdown()
    except Exception:
        pass

    # Observability v2 shutdown
    try:
        from orchestrator.observability import shutdown_observability
        shutdown_observability()
    except Exception:
        pass

    log.info("Symbiont shutting down")


app = FastAPI(
    title="AI Symbiont",
    version=__version__,
    lifespan=lifespan,
)

# Mount dashboard router
try:
    from orchestrator.observability.dashboard import router as dashboard_router
    app.include_router(dashboard_router)
except ImportError:
    pass

# Mount OpenAI-compatible router.
from orchestrator.gateway.openai_compat import router as openai_router  # noqa: E402

app.include_router(openai_router)

# Mount internal Resource Governor routes.
try:
    from orchestrator.resource_governor.app_routes import router as resource_governor_router
    from orchestrator.resource_governor.app_routes import telemetry_router

    app.include_router(resource_governor_router)
    app.include_router(telemetry_router)
except Exception as exc:
    log.warning("Resource Governor router unavailable: %s", exc)

# Mount agentic operational runtime routes.
try:
    from orchestrator.agentic.api import router as agentic_router

    app.include_router(agentic_router)
except Exception as exc:
    log.warning("Agentic runtime router unavailable: %s", exc)

# Mount scheduler/admission routes.
try:
    from orchestrator.scheduler.app_routes import router as scheduler_router

    app.include_router(scheduler_router)
except Exception as exc:
    log.warning("Scheduler router unavailable: %s", exc)


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    """Serve favicon or return 204 to avoid 401 spam in logs."""
    from pathlib import Path
    ico = Path(__file__).parent.parent.parent.parent / "web" / "orc" / "favicon.ico"
    if ico.exists():
        from fastapi.responses import FileResponse
        return FileResponse(ico, media_type="image/x-icon")
    return Response(status_code=204)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """Enforce API key on every non-health endpoint."""
    cfg = get_settings()
    if request.url.path not in _AUTH_EXEMPT_PATHS:
        # Accept X-API-Key header or Authorization: Bearer <key>
        provided = request.headers.get("X-API-Key", "")
        if not provided:
            auth = request.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                provided = auth.removeprefix("Bearer ").strip()
        if not provided or not secrets.compare_digest(provided, cfg.symbiont.api_key):
            if _internal_api_token_valid(request):
                return await call_next(request)
            return JSONResponse(
                status_code=401,
                content={"detail": "Missing or invalid API key"},
            )
    return await call_next(request)


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    """Add security headers to all responses."""
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    """Catch unhandled exceptions — return 500 without leaking internals."""
    log.error("Unhandled exception on %s: %s", request.url.path, exc)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
    )


async def _acquire_llm_slot() -> None:
    """Try to acquire the LLM semaphore without waiting.

    Raises HTTP 429 if all slots are busy.
    """
    if _admission_controller is not None:
        # Admission controller handles capacity — skip simple semaphore
        return
    if _llm_semaphore is None:
        return  # no semaphore yet (e.g. during tests without lifespan)
    if _llm_semaphore.locked():
        raise HTTPException(
            status_code=429,
            detail="LLM busy — all slots occupied, try again shortly",
            headers={"Retry-After": "5"},
        )
    await _llm_semaphore.acquire()


def _release_llm_slot() -> None:
    if _admission_controller is not None:
        return  # admission controller handles release via its own API
    if _llm_semaphore is not None:
        _llm_semaphore.release()


def _register_interactive_activity(
    *,
    request_id: str,
    session_id: str,
    streaming: bool,
    task_id: str | None = None,
) -> str | None:
    try:
        from orchestrator.resource_governor import get_resource_governor_service
        from orchestrator.resource_governor.schemas import ActivityRequest, ActivityType, Capability

        activity_request = ActivityRequest(
            idempotency_key=f"symbiont:{request_id}:interactive",
            activity_type=ActivityType.INTERACTIVE_CHAT_STREAM if streaming else ActivityType.INTERACTIVE_QUERY,
            requester="symbiont",
            capability=Capability.CHAT_STREAM,
            request_id=request_id,
            session_id=session_id,
            ttl_seconds=30,
        )
        record = get_resource_governor_service().register_activity(activity_request)
        if task_id:
            try:
                from orchestrator.agentic.store import get_agentic_store

                payload = activity_request.model_dump(mode="json") if hasattr(activity_request, "model_dump") else activity_request.dict()
                get_agentic_store().record_resource_lease(
                    task_id=task_id,
                    lease_id=record.activity_id,
                    capability=str(payload.get("capability") or ""),
                    decision="GRANTED",
                    status="active",
                    payload={"activity": payload},
                    expires_at=_time.time() + float(payload.get("ttl_seconds") or 0),
                )
            except Exception:
                pass
        return record.activity_id
    except Exception as exc:
        log.debug("Resource activity registration skipped: %s", exc)
        return None


def _heartbeat_interactive_activity(activity_id: str | None) -> None:
    if not activity_id:
        return
    try:
        from orchestrator.resource_governor import get_resource_governor_service

        get_resource_governor_service().heartbeat_activity(activity_id)
        try:
            from orchestrator.agentic.store import get_agentic_store

            get_agentic_store().renew_resource_lease(activity_id)
        except Exception:
            pass
    except Exception:
        pass


def _release_interactive_activity(activity_id: str | None) -> None:
    if not activity_id:
        return
    try:
        from orchestrator.resource_governor import get_resource_governor_service

        get_resource_governor_service().release_activity(activity_id)
        try:
            from orchestrator.agentic.store import get_agentic_store

            get_agentic_store().release_resource_lease(activity_id)
        except Exception:
            pass
    except Exception:
        pass


def _heartbeat_scheduler_lease(lease_id: str | None) -> None:
    if not lease_id:
        return
    try:
        from orchestrator.resource_governor import get_resource_governor_service

        get_resource_governor_service().heartbeat_lease(lease_id)
    except Exception:
        pass


def _release_scheduler_lease(lease_id: str | None) -> None:
    if not lease_id:
        return
    try:
        from orchestrator.resource_governor import get_resource_governor_service

        get_resource_governor_service().release_lease(lease_id)
    except Exception:
        pass


@app.post("/query", response_model=QueryResponse)
async def query(req: QueryRequest):
    """Main query endpoint — LangGraph-based unified orchestration."""
    import time as _time
    import uuid as _uuid

    # --- Input sanitisation ---
    clean_query = sanitize_query(req.query)
    if not clean_query:
        raise HTTPException(status_code=422, detail="Query is empty after sanitisation")
    history = validate_history(req.history)
    session_id = validate_session_id(req.session_id) or str(_uuid.uuid4())

    cfg = get_settings()
    from orchestrator.agentic.runtime import (
        begin_shadow_task,
        complete_shadow_task,
        fail_shadow_task,
        material_task_metadata,
        task_requires_material_output,
    )

    shadow_handle = begin_shadow_task(
        goal=clean_query,
        session_id=session_id,
        entrypoint="api.query",
        metadata={"stream": req.stream, "agentic_requested": req.agentic},
    )

    from orchestrator.gateway.high_risk_guard import block_high_risk_mutation

    high_risk_block = block_high_risk_mutation(clean_query)
    if high_risk_block is not None:
        latency_ms = 0.0
        if req.stream:
            async def blocked_event_stream():
                import json as _json

                if shadow_handle is not None:
                    yield (
                        "event: agentic\n"
                        f"data: {_json.dumps({'task_id': shadow_handle.task_id, 'trace_id': shadow_handle.trace_id})}\n\n"
                    )
                yield f"event: policy\ndata: {high_risk_block.reason}\n\n"
                yield f"data: {high_risk_block.response}\n\n"
                yield "data: [DONE]\n\n"
                if _session_store is not None and cfg.session.enabled:
                    _session_store.append(session_id, "user", clean_query)
                    _session_store.append(session_id, "assistant", high_risk_block.response)
                complete_shadow_task(
                    shadow_handle,
                    final_state={
                        "response": high_risk_block.response,
                        "model_used": "gateway_high_risk_guard",
                        "tokens_used": len(high_risk_block.response) // 4,
                        "policy_decision": high_risk_block.reason,
                    },
                    latency_ms=latency_ms,
                    graph_tracer=None,
                )

            return StreamingResponse(blocked_event_stream(), media_type="text/event-stream")

        if _session_store is not None and cfg.session.enabled:
            _session_store.append(session_id, "user", clean_query)
            _session_store.append(session_id, "assistant", high_risk_block.response)
        complete_shadow_task(
            shadow_handle,
            final_state={
                "response": high_risk_block.response,
                "model_used": "gateway_high_risk_guard",
                "tokens_used": len(high_risk_block.response) // 4,
                "policy_decision": high_risk_block.reason,
            },
            latency_ms=latency_ms,
            graph_tracer=None,
        )
        return QueryResponse(
            response=high_risk_block.response,
            model_used="gateway_high_risk_guard",
            intent="policy",
            complexity="high",
            sources_used=[],
            context_tokens=0,
            latency_ms=latency_ms,
            session_id=session_id,
            task_id=shadow_handle.task_id if shadow_handle is not None else None,
            trace_id=shadow_handle.trace_id if shadow_handle is not None else None,
        )

    language_task = None
    language_config: dict[str, object] | None = None
    try:
        language_task, language_config = _create_language_task(clean_query, cfg=cfg)
    except Exception as exc:
        log.debug("language normalization startup skipped for request: %s", exc)

    if req.agentic is not False and task_requires_material_output(clean_query):
        try:
            from orchestrator.agentic.models import TaskStatus
            from orchestrator.agentic.store import get_agentic_store
            from orchestrator.pipeline.language_context import choose_model_query, has_usable_english

            material_language_context = await _resolve_material_language_context(
                language_task,
                text=clean_query,
                language_config=language_config,
            )
            material_goal = choose_model_query(
                material_language_context,
                mode=(language_config or {}).get("mode"),
            )
            user_language = str(material_language_context.get("user_language") or "")
            if user_language.startswith("pt") and not has_usable_english(material_language_context, clean_query):
                raise HTTPException(
                    status_code=503,
                    detail={
                        "error": "language_normalization_required",
                        "reason": material_language_context.get("fallback_reason") or "translation_unavailable",
                    },
                )
            material_metadata = material_task_metadata(material_goal, client_cwd=req.client_cwd)
            material_metadata["material_evidence_deferred"] = True
            material_metadata["material_evidence_deferred_to"] = "agentic.runner"
            material_metadata["material_evidence_request_seed"] = {
                "original_user_prompt": clean_query,
                "normalized_prompt": material_goal,
                "user_language": user_language,
                "reason_for_acquisition": "deferred local evidence acquisition for material-output task",
            }

            store = get_agentic_store()
            task_metadata = {
                **material_metadata,
                **_material_language_metadata(
                    original_query=clean_query,
                    working_query=material_goal,
                    language_context=material_language_context,
                ),
                "delegated_from": "api.query",
                "stream": req.stream,
                "client_files_count": len(req.client_files or []),
                "shadow_task_id": shadow_handle.task_id if shadow_handle is not None else None,
            }
            delegated = store.create_task(
                goal=material_goal,
                mode="autonomous",
                source="api.query.material",
                session_id=session_id,
                priority="normal",
                metadata=task_metadata,
                status=TaskStatus.QUEUED.value,
            )
            delegated_text = (
                "Tarefa agentic material criada.\n"
                f"task_id: {delegated.id}\n"
                f"trace_id: {delegated.trace_id}\n"
                "A execução e validação continuam no runtime agentic."
            )
            complete_shadow_task(
                shadow_handle,
                final_state={
                    "response": delegated_text,
                    "model_used": "agentic_material_delegation",
                    "tokens_used": len(delegated_text) // 4,
                    "delegated_task_id": delegated.id,
                    "delegated_trace_id": delegated.trace_id,
                },
                latency_ms=0.0,
                graph_tracer=None,
            )
            if req.stream:
                async def delegated_event_stream():
                    import json as _json

                    yield (
                        "event: agentic\n"
                        f"data: {_json.dumps({'task_id': delegated.id, 'trace_id': delegated.trace_id})}\n\n"
                    )
                    for idx, part in enumerate(delegated_text.split("\n")):
                        if idx > 0:
                            yield "data: \\n\n\n"
                        if part:
                            yield f"data: {part}\n\n"
                    yield "data: [DONE]\n\n"

                return StreamingResponse(delegated_event_stream(), media_type="text/event-stream")
            return QueryResponse(
                response=delegated_text,
                model_used="agentic_material_delegation",
                intent="agentic",
                complexity="deep",
                sources_used=[],
                context_tokens=0,
                latency_ms=0.0,
                session_id=session_id,
                task_id=delegated.id,
                trace_id=delegated.trace_id,
            )
        except HTTPException as exc:
            fail_shadow_task(shadow_handle, error=exc)
            raise
        except Exception as exc:
            log.warning("material agentic delegation failed; falling back to query pipeline: %s", exc)

    try:
        from orchestrator.gateway.local_command_bridge import (
            describe_local_command_route,
            maybe_answer_local_command,
        )

        graph_for_local = _get_graph()
        local_feature_client = getattr(graph_for_local, "_feature_client", None)
        local_route = describe_local_command_route(
            clean_query,
            client_cwd=req.client_cwd,
            client_files=req.client_files,
        )
    except Exception as exc:
        log.debug("local command preflight skipped: %s", exc)
        local_route = None
        local_feature_client = None
        maybe_answer_local_command = None  # type: ignore[assignment]

    if local_route and maybe_answer_local_command is not None:
        local_t0 = _time.perf_counter()
        local_answer = await maybe_answer_local_command(
            clean_query,
            client_cwd=req.client_cwd,
            client_system=req.client_system,
            client_files=req.client_files,
            feature_client=local_feature_client,
        )
        if not local_answer:
            local_route = None

    if local_route and maybe_answer_local_command is not None and local_answer:
        if req.stream:
            async def local_event_stream():
                import json as _json

                try:
                    if shadow_handle is not None:
                        yield (
                            "event: agentic\n"
                            f"data: {_json.dumps({'task_id': shadow_handle.task_id, 'trace_id': shadow_handle.trace_id})}\n\n"
                        )
                    yield f"event: status_start\ndata: ferramenta local: {local_route}...\n\n"
                    latency_ms = (_time.perf_counter() - local_t0) * 1000
                    yield f"event: status_done\ndata: ferramenta local: {local_route} ({int(latency_ms)}ms)\n\n"
                    for idx, part in enumerate(local_answer.split("\n")):
                        if idx > 0:
                            yield "data: \\n\n\n"
                        if part:
                            yield f"data: {part}\n\n"
                    yield "data: [DONE]\n\n"
                    if _session_store is not None and cfg.session.enabled:
                        _session_store.append(session_id, "user", clean_query)
                        _session_store.append(session_id, "assistant", local_answer)
                    complete_shadow_task(
                        shadow_handle,
                        final_state={
                            "response": local_answer,
                            "model_used": "agentic_local_command_bridge",
                            "tokens_used": len(local_answer) // 4,
                        },
                        latency_ms=latency_ms,
                        graph_tracer=None,
                    )
                except BaseException as exc:
                    fail_shadow_task(shadow_handle, error=exc)
                    raise

            return StreamingResponse(local_event_stream(), media_type="text/event-stream")

        latency_ms = (_time.perf_counter() - local_t0) * 1000
        if _session_store is not None and cfg.session.enabled:
            _session_store.append(session_id, "user", clean_query)
            _session_store.append(session_id, "assistant", local_answer)
        complete_shadow_task(
            shadow_handle,
            final_state={
                "response": local_answer,
                "model_used": "agentic_local_command_bridge",
                "tokens_used": len(local_answer) // 4,
            },
            latency_ms=latency_ms,
            graph_tracer=None,
        )
        return QueryResponse(
            response=local_answer,
            model_used="agentic_local_command_bridge",
            intent="system_and_local",
            complexity="normal",
            sources_used=[local_route, "client_system" if req.client_system else "runtime"],
            context_tokens=0,
            latency_ms=round(latency_ms, 1),
            session_id=session_id,
            task_id=shadow_handle.task_id if shadow_handle is not None else None,
            trace_id=shadow_handle.trace_id if shadow_handle is not None else None,
        )

    # --- Predictive Prewarming (fire-and-forget, before LLM semaphore) ---
    prewarm_request_id = shadow_handle.request_id if shadow_handle is not None else _uuid.uuid4().hex[:16]
    _prewarm_task = None
    try:
        from orchestrator.prewarming import get_prewarm_engine
        pw_engine = get_prewarm_engine()
        if pw_engine is not None:
            file_names = None
            if hasattr(req, "files") and req.files:
                file_names = [f.filename for f in req.files if hasattr(f, "filename")]
            _prewarm_task = asyncio.create_task(
                pw_engine.predict_and_warm(
                    request_id=prewarm_request_id,
                    query=clean_query,
                    session_id=session_id,
                    file_names=file_names,
                )
            )
    except Exception:
        pass  # Non-critical — never block the request path

    # Audio transcription bypasses LLM semaphore (no LLM involved)
    from orchestrator.gateway.audio_handler import is_audio_query
    if req.stream and is_audio_query(clean_query):
        from orchestrator.gateway.audio_handler import stream_audio_transcription
        audio_activity_id = _register_interactive_activity(
            request_id=prewarm_request_id,
            session_id=session_id,
            streaming=True,
            task_id=shadow_handle.task_id if shadow_handle is not None else None,
        )

        async def audio_stream():
            import time as _time_local

            from orchestrator.agentic.context import reset_agentic_context, set_agentic_context

            context_token = set_agentic_context(shadow_handle.context() if shadow_handle is not None else None)
            t0_audio = _time_local.perf_counter()
            last_activity_heartbeat = _time_local.monotonic()
            try:
                async for token in stream_audio_transcription(
                    clean_query,
                    feature_client=local_feature_client,
                ):
                    now = _time_local.monotonic()
                    if now - last_activity_heartbeat >= 10:
                        _heartbeat_interactive_activity(audio_activity_id)
                        last_activity_heartbeat = now
                    if "\n" in token:
                        for idx, part in enumerate(token.split("\n")):
                            if idx > 0:
                                yield "data: \\n\n\n"
                            if part:
                                yield f"data: {part}\n\n"
                    elif token:
                        yield f"data: {token}\n\n"
                yield "data: [DONE]\n\n"
                complete_shadow_task(
                    shadow_handle,
                    final_state={
                        "response": "audio_stream_completed",
                        "model_used": "audio_transcribe",
                        "tokens_used": 0,
                    },
                    latency_ms=(_time_local.perf_counter() - t0_audio) * 1000,
                    graph_tracer=None,
                )
            except BaseException as exc:
                fail_shadow_task(shadow_handle, error=exc)
                raise
            finally:
                reset_agentic_context(context_token)
                _release_interactive_activity(audio_activity_id)

        return StreamingResponse(audio_stream(), media_type="text/event-stream")

    # --- Scheduler Admission ---
    scheduler_decision = None
    try:
        from orchestrator.scheduler.admission import interactive_chat_plan
        from orchestrator.scheduler.service import get_scheduler_service

        scheduler_decision = get_scheduler_service().admit_route(
            interactive_chat_plan(session_id=session_id)
        )
        if scheduler_decision.decision in {"defer", "reject_policy"}:
            detail = {
                "decision": scheduler_decision.decision,
                "reason": scheduler_decision.reason,
                "retry_after_s": scheduler_decision.retry_after_s,
                "pressure_level": scheduler_decision.pressure_level,
                "pressure_reasons": scheduler_decision.pressure_reasons,
            }
            raise HTTPException(
                status_code=429 if scheduler_decision.decision == "defer" else 403,
                detail=detail,
                headers={"Retry-After": str(scheduler_decision.retry_after_s or 10)},
            )
    except HTTPException:
        raise
    except Exception as exc:
        log.debug("scheduler admission skipped: %s", exc)

    # --- Admission Control ---
    _admission_backend: str | None = None
    _admission_model: str | None = None
    if _admission_controller is not None:
        from orchestrator.core.admission import AdmissionDecision

        # Estimate token budget from query length (rough: 1 token ≈ 4 chars)
        est_tokens_in = max(1, len(clean_query) // 4)
        est_tokens_out = getattr(req, "max_tokens", 2048) or 2048
        # Determine task complexity from heuristic
        _task_complexity = "medium"
        if len(clean_query) < 100:
            _task_complexity = "low"
        elif len(clean_query) > 500:
            _task_complexity = "high"

        adm_result = _admission_controller.evaluate(
            user_id=session_id,
            estimated_tokens_in=est_tokens_in,
            estimated_tokens_out=est_tokens_out,
            preferred_backend="vllm",
            preferred_model="",
            task_complexity=_task_complexity,
        )

        if adm_result.decision == AdmissionDecision.REJECT:
            raise HTTPException(
                status_code=429,
                detail=adm_result.reason,
                headers={"Retry-After": str(int(adm_result.wait_seconds))},
            )
        if adm_result.decision in (AdmissionDecision.ACCEPT, AdmissionDecision.DOWNGRADE):
            _admission_backend = adm_result.backend
            _admission_model = adm_result.model
            if adm_result.decision == AdmissionDecision.DOWNGRADE:
                log.info("Admission: downgrading to %s/%s — %s",
                         adm_result.backend, adm_result.model, adm_result.reason)
        # QUEUE decision: proceed (queueing is a future extension)
        await _admission_controller.acquire(adm_result.backend)

    await _acquire_llm_slot()
    try:
        # Resolve history — session store takes priority if enabled
        if _session_store is not None and cfg.session.enabled:
            stored = _session_store.get(session_id)
            if stored:
                history = stored + (history or [])

        if req.stream:
            # Streaming through the full LangGraph pipeline:
            # classify → route → context → agents → synthesize (stream final LLM)
            async def event_stream():
                import json as _json

                from orchestrator.agentic.context import reset_agentic_context, set_agentic_context

                context_token = set_agentic_context(shadow_handle.context() if shadow_handle is not None else None)
                t0_stream = _time.perf_counter()
                activity_id = _register_interactive_activity(
                    request_id=prewarm_request_id,
                    session_id=session_id,
                    streaming=True,
                    task_id=shadow_handle.task_id if shadow_handle is not None else None,
                )
                try:
                    if shadow_handle is not None:
                        yield (
                            "event: agentic\n"
                            f"data: {_json.dumps({'task_id': shadow_handle.task_id, 'trace_id': shadow_handle.trace_id})}\n\n"
                        )
                    activity_last_heartbeat = _time.monotonic()
                    graph = _get_graph()
                    full_response = []
                    from orchestrator.gateway.streaming import (
                        _SSE_EVENT_MARKER,
                        _build_prewarm_status_text,
                        stream_via_pipeline,
                    )

                    # --- Prewarm status: show which containers were selected ---
                    if _prewarm_task is not None:
                        try:
                            import asyncio as _aio
                            pw_state = await _aio.wait_for(_aio.shield(_prewarm_task), timeout=0.5)
                            pw_status = _build_prewarm_status_text(pw_state)
                            if pw_status:
                                yield f"event: status_done\ndata: {pw_status}\n\n"
                        except Exception:
                            pass  # Never block the stream on prewarm errors

                    model_query = clean_query
                    language_context_dict = {}
                    if language_config is not None:
                        try:
                            from orchestrator.pipeline.language_context import choose_model_query

                            language_context = await _resolve_language_context(
                                language_task,
                                text=clean_query,
                                language_config=language_config,
                            )
                            model_query = choose_model_query(language_context, mode=language_config.get("mode"))
                            language_context_dict = language_context
                        except Exception as exc:
                            log.debug("language normalization streaming fallback: %s", exc)

                    async for token in stream_via_pipeline(
                        graph,
                        query=model_query,
                        original_query=clean_query,
                        language_context=language_context_dict,
                        history=history,
                        session_id=session_id,
                        client_cwd=req.client_cwd,
                        client_system=req.client_system,
                        client_files=req.client_files,
                    ):
                        if token.startswith(_SSE_EVENT_MARKER):
                            # Pre-formatted SSE event (e.g. status) — pass through raw
                            yield token[len(_SSE_EVENT_MARKER):]
                            continue
                        now = _time.monotonic()
                        if now - activity_last_heartbeat >= 10:
                            _heartbeat_interactive_activity(activity_id)
                            _heartbeat_scheduler_lease(scheduler_decision.lease_id if scheduler_decision is not None else None)
                            activity_last_heartbeat = now
                        full_response.append(token)
                        # SSE spec: multi-line data must use separate "data:" per line
                        # Split token on newlines to preserve them for the client
                        if "\n" in token:
                            for i, part in enumerate(token.split("\n")):
                                if i > 0:
                                    yield "data: \\n\n\n"
                                if part:
                                    yield f"data: {part}\n\n"
                        else:
                            yield f"data: {token}\n\n"
                    yield "data: [DONE]\n\n"

                    # Persist session
                    if _session_store is not None and cfg.session.enabled:
                        response_text = "".join(full_response)
                        _session_store.append(session_id, "user", clean_query)
                        _session_store.append(session_id, "assistant", response_text)
                    complete_shadow_task(
                        shadow_handle,
                        final_state={
                            "response": "".join(full_response),
                            "model_used": "streaming_pipeline",
                            "tokens_used": len("".join(full_response)) // 4,
                        },
                        latency_ms=(_time.perf_counter() - t0_stream) * 1000,
                        graph_tracer=None,
                    )
                except BaseException as exc:
                    fail_shadow_task(shadow_handle, error=exc)
                    raise
                finally:
                    reset_agentic_context(context_token)
                    _release_interactive_activity(activity_id)
                    _release_scheduler_lease(scheduler_decision.lease_id if scheduler_decision is not None else None)
                    _release_llm_slot()
                    if _admission_controller is not None and _admission_backend:
                        _admission_controller.release(_admission_backend)
                    # Prewarm cleanup for streaming path
                    try:
                        from orchestrator.prewarming import get_prewarm_engine
                        pw = get_prewarm_engine()
                        if pw is not None:
                            # Fire background cancel for unused GPU containers (reads state before cleanup)
                            asyncio.create_task(pw.cancel_unused(prewarm_request_id, delay_seconds=5.0))
                            pw.cleanup(prewarm_request_id)
                    except Exception:
                        pass

            from starlette.background import BackgroundTask

            return StreamingResponse(
                event_stream(),
                media_type="text/event-stream",
                background=BackgroundTask(
                    _release_scheduler_lease,
                    scheduler_decision.lease_id if scheduler_decision is not None else None,
                ),
            )

        from orchestrator.agentic.context import reset_agentic_context, set_agentic_context

        context_token = set_agentic_context(shadow_handle.context() if shadow_handle is not None else None)
        graph = _get_graph()
        t0 = _time.perf_counter()
        activity_id = _register_interactive_activity(
            request_id=prewarm_request_id,
            session_id=session_id,
            streaming=False,
            task_id=shadow_handle.task_id if shadow_handle is not None else None,
        )

        # Graph tracing callback
        from orchestrator.pipeline.tracer import GraphObservabilityTracer
        graph_tracer = GraphObservabilityTracer(
            request_id=prewarm_request_id,
            session_id=session_id,
        )

        try:
            from orchestrator.gateway.local_command_bridge import maybe_answer_local_command

            local_answer = await maybe_answer_local_command(
                clean_query,
                client_cwd=req.client_cwd,
                client_system=req.client_system,
                client_files=req.client_files,
                feature_client=getattr(graph, "_feature_client", None),
            )
            if local_answer:
                latency_ms = (_time.perf_counter() - t0) * 1000
                if _session_store is not None and cfg.session.enabled:
                    _session_store.append(session_id, "user", clean_query)
                    _session_store.append(session_id, "assistant", local_answer)
                complete_shadow_task(
                    shadow_handle,
                    final_state={
                        "response": local_answer,
                        "model_used": "agentic_local_command_bridge",
                        "tokens_used": len(local_answer) // 4,
                    },
                    latency_ms=latency_ms,
                    graph_tracer=None,
                )
                return QueryResponse(
                    response=local_answer,
                    model_used="agentic_local_command_bridge",
                    intent="system_and_local",
                    complexity="normal",
                    sources_used=[local_route or "local_command", "client_system" if req.client_system else "runtime"],
                    context_tokens=0,
                    latency_ms=round(latency_ms, 1),
                    session_id=session_id,
                    task_id=shadow_handle.task_id if shadow_handle is not None else None,
                    trace_id=shadow_handle.trace_id if shadow_handle is not None else None,
                )

            # Invoke the LangGraph workflow with tracing
            if language_config is not None:
                from orchestrator.pipeline.language_context import choose_model_query

                language_context = await _resolve_language_context(
                    language_task,
                    text=clean_query,
                    language_config=language_config,
                )
            else:
                from orchestrator.pipeline.language_context import language_context_fallback

                language_context = language_context_fallback(clean_query, reason="language_normalization_not_configured")

            model_query = clean_query
            if language_config is not None:
                model_query = choose_model_query(language_context, mode=language_config.get("mode"))

            initial_state = {
                "query": model_query,
                "original_query": clean_query,
                "history": history or [],
                "session_id": session_id,
                "language_context": language_context,
                "iterations": 0,
                "tokens_used": 0,
                "fallback_used": False,
                "client_cwd": req.client_cwd or "",
                "client_system": req.client_system or {},
                "client_files": req.client_files or [],
            }

            final_state = await graph.ainvoke(
                initial_state, {"callbacks": [graph_tracer]}
            )
            graph_tracer.finalize(final_state)
            latency_ms = (_time.perf_counter() - t0) * 1000

            response = strip_think(final_state.get("response", ""))
            if language_config is not None:
                try:
                    response, linter_changes = await _lint_final_response_via_translation(
                        response,
                        language_config=language_config,
                        language_context=language_context,
                    )
                    if linter_changes:
                        log.info("translation final PT-PT linter applied changes=%d", linter_changes)
                except Exception as exc:
                    log.debug("translation final linter skipped: %s", exc)
            intent_val = final_state.get("intent")
            complexity_val = final_state.get("complexity")

            # Persist to session store
            if _session_store is not None and cfg.session.enabled:
                _session_store.append(session_id, "user", clean_query)
                _session_store.append(session_id, "assistant", response)

            # Prewarm cleanup
            try:
                from orchestrator.prewarming import get_prewarm_engine
                pw_engine = get_prewarm_engine()
                if pw_engine is not None:
                    pw_engine.cleanup(prewarm_request_id)
            except Exception:
                pass
            _release_interactive_activity(activity_id)
            complete_shadow_task(
                shadow_handle,
                final_state=final_state,
                latency_ms=latency_ms,
                graph_tracer=graph_tracer,
            )

            return QueryResponse(
                response=response,
                model_used=final_state.get("model_used", ""),
                intent=intent_val.value if intent_val else "general",
                complexity=complexity_val.value if complexity_val else "normal",
                sources_used=[b.source for b in final_state.get("context_blocks", [])],
                context_tokens=final_state.get("tokens_used", 0),
                latency_ms=round(latency_ms, 1),
                session_id=session_id,
                task_id=shadow_handle.task_id if shadow_handle is not None else None,
                trace_id=shadow_handle.trace_id if shadow_handle is not None else None,
                agentic_deliberation=final_state.get("agentic_deliberation"),
            )
        except Exception as exc:
            fail_shadow_task(shadow_handle, error=exc)
            raise
        finally:
            reset_agentic_context(context_token)
    finally:
        # Release only for non-stream (stream releases in event_stream generator)
        if not req.stream:
            try:
                _release_interactive_activity(locals().get("activity_id"))
            except Exception:
                pass
            try:
                _release_scheduler_lease(scheduler_decision.lease_id if scheduler_decision is not None else None)
            except Exception:
                pass
            _release_llm_slot()
            if _admission_controller is not None and _admission_backend:
                _admission_controller.release(_admission_backend)


@app.post("/classify", response_model=ClassifyResponse)
def classify(req: QueryRequest):
    """Classify only — no LLM call."""
    from orchestrator.routing.complexity import HeuristicComplexityClassifier
    from orchestrator.routing.intent import HeuristicIntentClassifier

    clean_query = sanitize_query(req.query)
    if not clean_query:
        raise HTTPException(status_code=422, detail="Query is empty after sanitisation")
    history = validate_history(req.history)
    intent = HeuristicIntentClassifier().classify(clean_query, history=history)
    complexity = HeuristicComplexityClassifier().classify(clean_query)
    return ClassifyResponse(
        intent=intent.value,
        complexity=complexity.value,
    )


@app.get("/prewarm/status")
def prewarm_status():
    """Predictive prewarming status — active predictions, hit rate, config."""
    from orchestrator.prewarming import get_prewarm_engine
    pw_engine = get_prewarm_engine()
    if pw_engine is None:
        return JSONResponse({"enabled": False, "reason": "not_initialized"})
    return JSONResponse(pw_engine.get_status())


@app.get("/live")
def live():
    """Lightweight liveness probe for Docker healthchecks."""
    return {"status": "ok"}


@app.get("/image-info")
def image_info_endpoint():
    """Runtime image/build identity for live verification."""
    from orchestrator.gateway.runtime_identity import image_info

    return image_info()


@app.get("/runtime-info")
def runtime_info_endpoint():
    """Runtime code/config identity for live verification."""
    from orchestrator.gateway.runtime_identity import runtime_info

    return runtime_info()


@app.get("/config-effective")
def config_effective_endpoint():
    """Compare generated env values with the container effective environment."""
    from orchestrator.gateway.runtime_identity import config_effective

    return config_effective()


def _config_health_report() -> dict:
    """Return central config status without duplicating resolver rules."""
    from config.resolver import resolve_config

    resolved = resolve_config()
    return dict(resolved.get("config_health") or {})


def _config_health_fallback(error: str) -> dict[str, object]:
    return {
        "contract": "ai-local.config-health.v1",
        "version": 1,
        "status": "degraded",
        "errors": [error],
        "warnings": [],
    }


@app.get("/health", response_model=HealthResponse)
def health():
    """Health check — reports status of all components including LLM backends."""
    engine = _get_engine()
    timeout = _timeout_from_env("ORC_HEALTH_REPORT_TIMEOUT_SECONDS", 8.0)
    report = _call_with_timeout(
        "health_report",
        engine.health_report,
        timeout=timeout,
        fallback=lambda error: {
            "ollama": False,
            "providers": {},
            "all_ok": False,
            "backends": [
                {
                    "name": "health_report",
                    "status": "unavailable",
                    "url": "internal",
                    "models_configured": [],
                    "models_detected": [],
                    "last_error": error,
                }
            ],
        },
    )

    backends = [BackendHealthSchema(**b) for b in report.get("backends", [])]
    providers = report.get("providers") or {}
    config_health = _call_with_timeout(
        "config health",
        _config_health_report,
        timeout=_timeout_from_env("ORC_HEALTH_CONFIG_TIMEOUT_SECONDS", 3.0),
        fallback=_config_health_fallback,
    )
    config_status = str(config_health.get("status") or "degraded")
    config_ok = config_status in {"ready", "local_fallback"}

    response = HealthResponse(
        status="ok" if report.get("all_ok") and config_ok else "degraded",
        ollama=bool(report.get("ollama", False)),
        rag=bool(providers.get("rag", False)),
        providers=providers,
        backends=backends,
        config_health=config_health,
    )

    # Enrich with GPU/swap metrics (non-blocking, best-effort)
    def _resource_snapshot():
        from orchestrator.observability.resources import get_resource_collector
        rc = get_resource_collector()
        return rc.snapshot()

    snapshot = _call_with_timeout(
        "resource snapshot",
        _resource_snapshot,
        timeout=_timeout_from_env("ORC_HEALTH_RESOURCE_TIMEOUT_SECONDS", 1.0),
        fallback=lambda _error: {},
    )
    if snapshot:
        response.gpu_available = bool(snapshot.get("gpu_name") or snapshot.get("gpu_vram_total_mb", 0) > 0)
        response.gpu_vram_used_mb = snapshot.get("gpu_vram_used_mb")
        response.gpu_vram_free_mb = snapshot.get("gpu_vram_free_mb")
        response.gpu_vram_total_mb = snapshot.get("gpu_vram_total_mb")
        response.gpu_utilization_pct = snapshot.get("gpu_utilization_pct")
        response.swap_used_mb = snapshot.get("swap_used_mb")
        response.models_loaded = snapshot.get("ollama_models_loaded")

    return response


@app.get("/tools", response_model=ToolListResponse)
def get_tools():
    """List all registered tools available for LLM function calling."""
    engine = _get_engine()
    exported = engine.tool_registry.export_for_llm()
    return ToolListResponse(
        tools=[ToolSchema(**t) for t in exported],
    )


@app.get("/hardware")
def get_hardware(refresh: bool = False):
    """Hardware profile and adaptive configuration.

    Returns detected hardware, computed overrides, cache stats,
    and optimization recommendations.
    """
    try:
        from orchestrator.core.adaptive_config import get_adaptive_overrides
        from orchestrator.core.hardware_profile import get_hardware_profile

        profile = get_hardware_profile(force_refresh=refresh)
        overrides = get_adaptive_overrides(profile)

        result = {
            "hardware": profile.summary(),
            "adaptive_config": {
                "max_loaded_models": overrides.max_loaded_models,
                "max_concurrent_llm": overrides.max_concurrent_llm,
                "keep_alive": overrides.keep_alive,
                "preferred_num_ctx": overrides.preferred_num_ctx,
                "gpu_offload": overrides.gpu_offload,
                "context_worker_threads": overrides.context_worker_threads,
                "response_cache_max_size": overrides.response_cache_max_size,
                "context_token_budget": overrides.context_token_budget,
                "context_budget_multiplier": overrides.context_budget_multiplier,
                "degradation_mode": overrides.degradation_mode.value,
            },
            "recommendations": overrides.recommendations,
        }

        # Include cache stats if available
        try:
            from orchestrator.core.response_cache import get_response_cache
            cache = get_response_cache()
            result["response_cache"] = cache.stats
        except Exception:
            pass

        return result
    except Exception as exc:
        log.warning("Hardware profile query failed: %s", exc)
        return {"error": "hardware profile unavailable", "hardware": None}


@app.get("/metrics")
def get_metrics(window: int = 300):
    """Query metrics — latency stats, intent/model distribution, agentic stats, backend stats.

    Args:
        window: Time window in seconds (default 300 = 5 min). 0 = all time.
    """
    data = metrics.summary(window_seconds=window)
    # Include per-backend call stats from router
    try:
        engine = _get_engine()
        backends = engine._llm.health_report()
        data["backend_stats"] = [
            {"name": b["name"], "calls": b.get("calls", 0), "avg_call_latency_ms": b.get("avg_call_latency_ms")}
            for b in backends if b.get("status") != "disabled"
        ]
    except Exception:
        data["backend_stats"] = []
    return data


@app.get("/models")
def get_models():
    """List configured models, aliases, and routing roles from registry."""
    from orchestrator.registry import get_registry

    reg = get_registry()
    cfg = get_settings().models
    return {
        "roles": {
            "default": cfg.default,
            "fast": cfg.fast,
            "code": cfg.code,
            "deep": cfg.deep,
            "embedding": cfg.embedding,
        },
        "profiles": {k: reg.get_model_for_profile(k) for k in ["fast", "default", "code", "deep"]},
        "registry_version": reg.data.get("version", "unknown"),
    }


@app.get("/models/active")
def get_models_active():
    """Query Ollama for currently loaded/available models."""
    import httpx as _httpx

    cfg = get_settings()
    try:
        resp = _httpx.get(f"{cfg.ollama.base_url}/api/tags", timeout=5.0)
        resp.raise_for_status()
        models = resp.json().get("models", [])
        return {
            "models": [
                {
                    "name": m.get("name", ""),
                    "size": m.get("size", 0),
                    "modified_at": m.get("modified_at", ""),
                }
                for m in models
            ]
        }
    except Exception as exc:
        log.warning("Failed to query Ollama models: %s", exc)
        return {"models": [], "error": "model backend unavailable"}


@app.post("/models/warm")
def warm_model(model: str = None):
    """Warm a model by sending a minimal prompt to load it into VRAM.

    If no model specified, warms all configured primary/fallback models.
    """
    from orchestrator.core.warmup import get_warmup_manager

    mgr = get_warmup_manager()

    if model:
        from orchestrator.registry import get_registry  # noqa: F401
        # model param is used directly (no alias resolution)
        ok = mgr.warm_model(model)
        if ok:
            return {"status": "ok", "model": model, "message": f"Model {model} warmed"}
        else:
            return {"status": "error", "model": model, "message": "Failed to warm model"}
    else:
        results = mgr.warm_all()
        return {
            "status": "ok",
            "results": {m: ("warmed" if ok else "failed") for m, ok in results.items()},
        }


@app.get("/models/status")
def models_warm_status():
    """Get warm/cold status of all configured models."""
    from orchestrator.core.warmup import get_warmup_manager

    mgr = get_warmup_manager()
    cfg = get_settings()

    warm_status = mgr.get_warm_status(force_refresh=True)
    all_models = set()
    for b in cfg.llm.backends:
        if b.enabled:
            all_models.update(b.models)

    result = []
    for model in sorted(all_models):
        status = warm_status.get(model)
        result.append({
            "model": model,
            "warm": bool(status and status.warm),
            "vram_bytes": status.vram_bytes if status else 0,
            "expires_at": status.expires_at if status else None,
        })
    return {"models": result}


@app.get("/models/resolve/{name}")
def resolve_model_name(name: str):
    """Resolve a profile key to its configured model name."""
    from orchestrator.registry import get_registry

    reg = get_registry()
    model = reg.get_model_for_profile(name)
    return {"input": name, "resolved": model or name}


# ---------------------------------------------------------------------------
# Feedback & Routing Intelligence Endpoints (Sprint 4)
# ---------------------------------------------------------------------------

@app.post("/feedback")
def submit_feedback(request: Request):
    """Submit user feedback (rating 1-5) for a routing decision.

    Used by the learning system to identify successful routing patterns
    and improve future decisions.
    """

    from orchestrator.gateway.schemas import FeedbackRequest

    try:
        body = asyncio.get_event_loop().run_until_complete(request.json())
        fb = FeedbackRequest(**body)
    except Exception as exc:
        log.warning("Feedback payload rejected: %s", exc)
        raise HTTPException(status_code=422, detail="Invalid feedback payload")

    engine = _get_engine()
    routing_log = getattr(engine, "_routing_log", None)
    if routing_log is None:
        raise HTTPException(status_code=503, detail="Routing log not available")

    updated = routing_log.record_feedback(fb.request_id, fb.rating, fb.feedback)
    if not updated:
        raise HTTPException(status_code=404, detail="Request ID not found")

    # Invalidate pattern cache so new feedback is picked up
    pattern_store = getattr(engine, "_pattern_store", None)
    if pattern_store:
        pattern_store.invalidate_cache()

    return {"status": "ok", "request_id": fb.request_id, "rating": fb.rating}


@app.get("/routing/stats")
def routing_stats(days: int = 7):
    """Routing intelligence statistics for dashboard."""
    engine = _get_engine()
    routing_log = getattr(engine, "_routing_log", None)
    if routing_log is None:
        return {"error": "Routing log not available", "stats": {}}
    return {"stats": routing_log.stats(days=days)}


@app.get("/routing/recent")
def routing_recent(limit: int = 20):
    """Recent routing decisions for live dashboard."""
    limit = min(limit, 100)
    engine = _get_engine()
    routing_log = getattr(engine, "_routing_log", None)
    if routing_log is None:
        return {"error": "Routing log not available", "decisions": []}
    return {"decisions": routing_log.recent(limit=limit)}


@app.get("/routing/patterns")
def routing_patterns():
    """Successful routing patterns currently used for few-shot injection."""
    engine = _get_engine()
    routing_log = getattr(engine, "_routing_log", None)
    if routing_log is None:
        return {"patterns": []}
    return {"patterns": routing_log.successful_patterns(limit=10)}


@app.get("/audit")
def audit_query(
    since: float | None = None,
    agent: str | None = None,
    event_type: str | None = None,
    limit: int = 100,
):
    """Query security audit trail."""
    engine = _get_engine()
    security_layer = getattr(engine, "security_layer", None)
    if security_layer is None or getattr(security_layer, "audit", None) is None:
        return {"entries": [], "message": "audit trail not enabled"}
    entries = security_layer.audit.query(
        since=since, agent=agent, event_type=event_type, limit=min(limit, 1000),
    )
    return {
        "entries": [
            {
                "timestamp": e.timestamp,
                "event_type": e.event_type,
                "agent_name": e.agent_name,
                "session_id": e.session_id,
                "request_id": e.request_id,
                "detail": e.detail,
            }
            for e in entries
        ],
        "total": len(entries),
    }


# =============================================================================
# Container Lifecycle Management
# =============================================================================

@app.get("/lifecycle")
def lifecycle_status():
    """Get the status of all managed service containers."""
    graph = _get_graph()
    registry = getattr(graph, "_service_registry", None)
    if registry is None:
        return {"error": "Service registry not available", "services": []}

    lifecycle = getattr(registry, "_lifecycle", None)
    if lifecycle is None or not lifecycle.available:
        return {
            "enabled": False,
            "message": "Container lifecycle management not active",
            "services": [],
        }

    return {
        "enabled": True,
        "services": lifecycle.status(),
    }


@app.post("/lifecycle/{service_name}/start")
def lifecycle_start(service_name: str):
    """Force-start a service container."""
    graph = _get_graph()
    registry = getattr(graph, "_service_registry", None)
    if registry is None:
        raise HTTPException(status_code=503, detail="Service registry not available")

    lifecycle = getattr(registry, "_lifecycle", None)
    if lifecycle is None or not lifecycle.available:
        raise HTTPException(status_code=503, detail="Lifecycle manager not available")

    success = lifecycle.ensure_running(service_name)
    if success:
        return {"status": "started", "service": service_name}
    raise HTTPException(status_code=500, detail=f"Failed to start {service_name}")


@app.post("/lifecycle/{service_name}/stop")
def lifecycle_stop(service_name: str):
    """Force-stop a service container."""
    graph = _get_graph()
    registry = getattr(graph, "_service_registry", None)
    if registry is None:
        raise HTTPException(status_code=503, detail="Service registry not available")

    lifecycle = getattr(registry, "_lifecycle", None)
    if lifecycle is None or not lifecycle.available:
        raise HTTPException(status_code=503, detail="Lifecycle manager not available")

    success = lifecycle.stop_service(service_name)
    if success:
        return {"status": "stopped", "service": service_name}
    raise HTTPException(status_code=500, detail=f"Failed to stop {service_name}")


def run_server():
    """Run the server programmatically (for CLI use)."""
    import sys

    import uvicorn

    try:
        cfg = get_settings()
    except (ValueError, KeyError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        print("Fix config/orc/ settings or environment variables and retry.", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"Failed to load configuration: {exc}", file=sys.stderr)
        sys.exit(1)

    uvicorn.run(
        "orchestrator.gateway.app:app",
        host=cfg.symbiont.host,
        port=cfg.symbiont.port,
        log_level=cfg.logging.level.lower(),
        ssl_certfile=_required_tls_file("AI_LOCAL_TLS_CERT_FILE"),
        ssl_keyfile=_required_tls_file("AI_LOCAL_TLS_KEY_FILE"),
    )


def _required_tls_file(env_name: str) -> str:
    value = os.environ.get(env_name)
    if not value:
        raise RuntimeError(f"{env_name} is required; HTTP API serving is disabled")
    return value
