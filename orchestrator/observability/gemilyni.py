"""Gemilyni observability — structured events, metrics, and redaction for the
Gemini execution layer (containers, workers, bundles, context policies).

This module integrates with the existing observability stack:
- Events emitted via the central emit() dispatcher (→ JSONL, ClickHouse, OTel)
- Metrics using the existing OTel meter pattern
- Redaction using the existing Redactor class
- Configuration via [observability.gemilyni] in config/orc/observability.toml

Components reused:
- symbiont.core.observability.emit() — event dispatch
- symbiont.core.observability.events.EventName — event type enum
- symbiont.core.observability.redaction.Redactor — secret masking
- symbiont.core.observability.config.GemilyniConfig — configuration
- symbiont.core.observability.metrics — OTel instruments

New events added:
- gemilyni.routing_decision
- gemilyni.external_context_policy
- gemilyni.bundle_created / bundle_file / context_block
- gemilyni.container_created / started / stats
- gemilyni.gemini_invocation_started / finished
- gemilyni.worker_output
- gemilyni.execution_finished
- gemilyni.policy_violation
- gemilyni.error

Dashboard location: /dashboard (tab "Gemilyni")
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Any

from orchestrator.observability.config import GemilyniConfig
from orchestrator.observability.events import (
    EventLevel,
    EventName,
    ObservabilityEvent,
)

log = logging.getLogger(__name__)

# Module-level config (set during init)
_config: GemilyniConfig | None = None


def init_gemilyni(config: GemilyniConfig) -> None:
    """Set gemilyni config. Called during observability init."""
    global _config
    _config = config


def get_config() -> GemilyniConfig:
    """Return current config or a disabled default."""
    return _config or GemilyniConfig(enabled=False)


def _enabled() -> bool:
    """Check if gemilyni observability is enabled."""
    return _config is not None and _config.enabled


def _emit(event: ObservabilityEvent) -> None:
    """Emit via the central dispatcher (non-blocking) and buffer for dashboard."""
    _store_event(event)
    from orchestrator.observability import emit
    emit(event)


def _hash_content(content: str) -> str:
    """SHA256 hash (first 16 chars) for content fingerprinting."""
    return hashlib.sha256(content.encode(errors="replace")).hexdigest()[:16]


def _redact_value(value: str, max_chars: int = 0) -> str:
    """Redact a value using the existing redactor."""
    from orchestrator.observability.redaction import get_redactor

    redactor = get_redactor()
    if redactor:
        value = redactor.redact_string(value)
    if max_chars > 0 and len(value) > max_chars:
        value = value[:max_chars] + "...[truncated]"
    return value


# ═══════════════════════════════════════════════════════════════════════
# Event Emission Functions
# ═══════════════════════════════════════════════════════════════════════


def emit_routing_decision(
    *,
    run_id: str,
    trace_id: str | None = None,
    query_id: str | None = None,
    selected_path: str,
    reason: str,
    complexity: str,
    complexity_threshold: str,
    intent: str,
    blocked_by_policy: bool = False,
    externalizable: bool = False,
    execution_enabled: bool = True,
) -> None:
    """Emit gemilyni.routing_decision event."""
    if not _enabled():
        return

    _emit(ObservabilityEvent(
        event=EventName.GEMILYNI_ROUTING_DECISION,
        level=EventLevel.INFO,
        request_id=run_id,
        trace_id=trace_id,
        metadata_json=_serialize({
            "run_id": run_id,
            "trace_id": trace_id,
            "query_id": query_id,
            "selected_path": selected_path,
            "reason": reason,
            "complexity": complexity,
            "complexity_threshold": complexity_threshold,
            "intent": intent,
            "blocked_by_policy": blocked_by_policy,
            "externalizable": externalizable,
            "execution_enabled": execution_enabled,
        }),
    ))
    _inc_counter("gemilyni_runs_total")
    if selected_path == "execute":
        _inc_counter("gemilyni_external_runs_total")
    else:
        _inc_counter("gemilyni_local_only_runs_total")


def emit_external_context_policy(
    *,
    run_id: str,
    trace_id: str | None = None,
    original_blocks: int,
    allowed_blocks: int,
    blocked_blocks: int,
    allowed_sources: list[str],
    blocked_sources: list[str],
    policy: str = "allowlist",
    require_sanitized_context: bool = True,
) -> None:
    """Emit gemilyni.external_context_policy event."""
    if not _enabled():
        return

    _emit(ObservabilityEvent(
        event=EventName.GEMILYNI_EXTERNAL_CONTEXT_POLICY,
        level=EventLevel.INFO,
        request_id=run_id,
        trace_id=trace_id,
        metadata_json=_serialize({
            "run_id": run_id,
            "trace_id": trace_id,
            "original_blocks": original_blocks,
            "allowed_blocks": allowed_blocks,
            "blocked_blocks": blocked_blocks,
            "allowed_sources": allowed_sources,
            "blocked_sources": blocked_sources,
            "policy": policy,
            "require_sanitized_context": require_sanitized_context,
        }),
    ))
    _inc_counter("gemilyni_context_blocks_included_total", allowed_blocks)
    _inc_counter("gemilyni_context_blocks_blocked_total", blocked_blocks)


def emit_bundle_created(
    *,
    run_id: str,
    trace_id: str | None = None,
    bundle_id: str,
    worker_id: str,
    task_type: str = "",
    allowed_files_count: int = 0,
    blocked_files_count: int = 0,
    allowed_context_blocks: int = 0,
    blocked_context_blocks: int = 0,
    workspace_mode: str = "",
    repo_mounted_directly: bool = False,
    workspace_readonly: bool = True,
    bundle_root: str = "",
) -> None:
    """Emit gemilyni.bundle_created event."""
    if not _enabled() or not get_config().capture_bundle_metadata:
        return

    _emit(ObservabilityEvent(
        event=EventName.GEMILYNI_BUNDLE_CREATED,
        level=EventLevel.INFO,
        request_id=run_id,
        trace_id=trace_id,
        metadata_json=_serialize({
            "run_id": run_id,
            "trace_id": trace_id,
            "bundle_id": bundle_id,
            "worker_id": worker_id,
            "task_type": task_type,
            "allowed_files_count": allowed_files_count,
            "blocked_files_count": blocked_files_count,
            "allowed_context_blocks": allowed_context_blocks,
            "blocked_context_blocks": blocked_context_blocks,
            "workspace_mode": workspace_mode,
            "repo_mounted_directly": repo_mounted_directly,
            "workspace_readonly": workspace_readonly,
            "bundle_root": bundle_root,
        }),
    ))
    _inc_counter("gemilyni_files_included_total", allowed_files_count)
    _inc_counter("gemilyni_files_blocked_total", blocked_files_count)


def emit_bundle_file(
    *,
    run_id: str,
    trace_id: str | None = None,
    bundle_id: str,
    worker_id: str,
    relative_path: str,
    file_hash: str,
    file_size_bytes: int,
    included: bool,
    blocked: bool = False,
    block_reason: str = "",
) -> None:
    """Emit gemilyni.bundle_file event. Never includes file content."""
    if not _enabled() or not get_config().capture_file_metadata:
        return

    _emit(ObservabilityEvent(
        event=EventName.GEMILYNI_BUNDLE_FILE,
        level=EventLevel.DEBUG,
        request_id=run_id,
        trace_id=trace_id,
        metadata_json=_serialize({
            "run_id": run_id,
            "trace_id": trace_id,
            "bundle_id": bundle_id,
            "worker_id": worker_id,
            "relative_path": relative_path,
            "file_hash": file_hash,
            "file_size_bytes": file_size_bytes,
            "included": included,
            "blocked": blocked,
            "block_reason": block_reason,
        }),
    ))


def emit_context_block(
    *,
    run_id: str,
    trace_id: str | None = None,
    bundle_id: str,
    worker_id: str,
    source: str,
    source_type: str = "",
    block_hash: str,
    token_estimate: int = 0,
    size_bytes: int = 0,
    included: bool,
    blocked: bool = False,
    block_reason: str = "",
) -> None:
    """Emit gemilyni.context_block event. Never includes raw context."""
    if not _enabled() or not get_config().capture_context_metadata:
        return

    _emit(ObservabilityEvent(
        event=EventName.GEMILYNI_CONTEXT_BLOCK,
        level=EventLevel.DEBUG,
        request_id=run_id,
        trace_id=trace_id,
        metadata_json=_serialize({
            "run_id": run_id,
            "trace_id": trace_id,
            "bundle_id": bundle_id,
            "worker_id": worker_id,
            "source": source,
            "source_type": source_type,
            "block_hash": block_hash,
            "token_estimate": token_estimate,
            "size_bytes": size_bytes,
            "included": included,
            "blocked": blocked,
            "block_reason": block_reason,
        }),
    ))


def emit_container_created(
    *,
    run_id: str,
    trace_id: str | None = None,
    worker_id: str,
    container_id: str,
    image: str,
    auth_mode: str,
    mounts_count: int = 0,
    network_mode: str = "",
) -> None:
    """Emit gemilyni.container_created event."""
    if not _enabled():
        return

    _emit(ObservabilityEvent(
        event=EventName.GEMILYNI_CONTAINER_CREATED,
        level=EventLevel.INFO,
        request_id=run_id,
        trace_id=trace_id,
        metadata_json=_serialize({
            "run_id": run_id,
            "trace_id": trace_id,
            "worker_id": worker_id,
            "container_id": container_id,
            "image": image,
            "auth_mode": auth_mode,
            "mounts_count": mounts_count,
            "network_mode": network_mode,
        }),
    ))
    _inc_counter("gemilyni_containers_created_total")


def emit_container_started(
    *,
    run_id: str,
    trace_id: str | None = None,
    worker_id: str,
    container_id: str,
) -> None:
    """Emit gemilyni.container_started event."""
    if not _enabled():
        return

    _emit(ObservabilityEvent(
        event=EventName.GEMILYNI_CONTAINER_STARTED,
        level=EventLevel.INFO,
        request_id=run_id,
        trace_id=trace_id,
        metadata_json=_serialize({
            "run_id": run_id,
            "trace_id": trace_id,
            "worker_id": worker_id,
            "container_id": container_id,
            "started_at": time.time(),
        }),
    ))
    _inc_counter("gemilyni_containers_started_total")


def emit_container_stats(
    *,
    run_id: str,
    trace_id: str | None = None,
    worker_id: str,
    container_id: str,
    cpu_percent: float = 0.0,
    memory_usage_bytes: int = 0,
    memory_limit_bytes: int = 0,
    memory_percent: float = 0.0,
    network_rx_bytes: int = 0,
    network_tx_bytes: int = 0,
    block_read_bytes: int = 0,
    block_write_bytes: int = 0,
) -> None:
    """Emit gemilyni.container_stats event."""
    if not _enabled() or not get_config().capture_container_stats:
        return

    _emit(ObservabilityEvent(
        event=EventName.GEMILYNI_CONTAINER_STATS,
        level=EventLevel.DEBUG,
        request_id=run_id,
        trace_id=trace_id,
        metadata_json=_serialize({
            "run_id": run_id,
            "trace_id": trace_id,
            "worker_id": worker_id,
            "container_id": container_id,
            "cpu_percent": cpu_percent,
            "memory_usage_bytes": memory_usage_bytes,
            "memory_limit_bytes": memory_limit_bytes,
            "memory_percent": memory_percent,
            "network_rx_bytes": network_rx_bytes,
            "network_tx_bytes": network_tx_bytes,
            "block_read_bytes": block_read_bytes,
            "block_write_bytes": block_write_bytes,
        }),
    ))
    _record_histogram("gemilyni_container_cpu_percent", cpu_percent)
    _record_histogram("gemilyni_container_memory_percent", memory_percent)


def emit_gemini_invocation_started(
    *,
    run_id: str,
    trace_id: str | None = None,
    worker_id: str,
    container_id: str,
    auth_mode: str,
    command_mode: str = "",
    model: str | None = None,
    input_tokens_estimate: int = 0,
) -> None:
    """Emit gemilyni.gemini_invocation_started event."""
    if not _enabled():
        return

    _emit(ObservabilityEvent(
        event=EventName.GEMILYNI_GEMINI_INVOCATION_STARTED,
        level=EventLevel.INFO,
        request_id=run_id,
        trace_id=trace_id,
        metadata_json=_serialize({
            "run_id": run_id,
            "trace_id": trace_id,
            "worker_id": worker_id,
            "container_id": container_id,
            "auth_mode": auth_mode,
            "command_mode": command_mode,
            "model": model,
            "input_tokens_estimate": input_tokens_estimate,
            "started_at": time.time(),
        }),
    ))
    _inc_counter("gemilyni_gemini_invocations_total")
    if input_tokens_estimate > 0:
        _record_histogram("gemilyni_input_tokens_estimate", input_tokens_estimate)


def emit_gemini_invocation_finished(
    *,
    run_id: str,
    trace_id: str | None = None,
    worker_id: str,
    container_id: str,
    status: str,
    exit_code: int = 0,
    duration_ms: float = 0,
    output_tokens_estimate: int = 0,
    stderr_size_bytes: int = 0,
    stdout_size_bytes: int = 0,
    error_type: str = "",
) -> None:
    """Emit gemilyni.gemini_invocation_finished event."""
    if not _enabled():
        return

    _emit(ObservabilityEvent(
        event=EventName.GEMILYNI_GEMINI_INVOCATION_FINISHED,
        level=EventLevel.WARNING if status != "success" else EventLevel.INFO,
        request_id=run_id,
        trace_id=trace_id,
        metadata_json=_serialize({
            "run_id": run_id,
            "trace_id": trace_id,
            "worker_id": worker_id,
            "container_id": container_id,
            "status": status,
            "exit_code": exit_code,
            "duration_ms": duration_ms,
            "output_tokens_estimate": output_tokens_estimate,
            "stderr_size_bytes": stderr_size_bytes,
            "stdout_size_bytes": stdout_size_bytes,
            "error_type": error_type,
            "finished_at": time.time(),
        }),
    ))
    _record_histogram("gemilyni_gemini_duration_ms", duration_ms)
    if output_tokens_estimate > 0:
        _record_histogram("gemilyni_output_tokens_estimate", output_tokens_estimate)
    if status != "success":
        _inc_counter("gemilyni_gemini_invocations_failed_total")


def emit_worker_output(
    *,
    run_id: str,
    trace_id: str | None = None,
    worker_id: str,
    container_id: str = "",
    output_files: list[str] | None = None,
    result_json_exists: bool = False,
    patch_diff_exists: bool = False,
    patch_size_bytes: int = 0,
    result_size_bytes: int = 0,
    logs_size_bytes: int = 0,
) -> None:
    """Emit gemilyni.worker_output event."""
    if not _enabled() or not get_config().capture_worker_outputs_metadata:
        return

    _emit(ObservabilityEvent(
        event=EventName.GEMILYNI_WORKER_OUTPUT,
        level=EventLevel.INFO,
        request_id=run_id,
        trace_id=trace_id,
        metadata_json=_serialize({
            "run_id": run_id,
            "trace_id": trace_id,
            "worker_id": worker_id,
            "container_id": container_id,
            "output_files": output_files or [],
            "result_json_exists": result_json_exists,
            "patch_diff_exists": patch_diff_exists,
            "patch_size_bytes": patch_size_bytes,
            "result_size_bytes": result_size_bytes,
            "logs_size_bytes": logs_size_bytes,
        }),
    ))
    if patch_diff_exists:
        _inc_counter("gemilyni_patch_outputs_total")
        _record_histogram("gemilyni_patch_size_bytes", patch_size_bytes)
    if result_size_bytes > 0:
        _record_histogram("gemilyni_result_size_bytes", result_size_bytes)


def emit_execution_finished(
    *,
    run_id: str,
    trace_id: str | None = None,
    external_used: bool,
    workers_total: int = 0,
    workers_succeeded: int = 0,
    workers_failed: int = 0,
    containers_started: int = 0,
    containers_failed: int = 0,
    total_duration_ms: float = 0,
    planning_duration_ms: float = 0,
    bundle_duration_ms: float = 0,
    container_start_duration_ms: float = 0,
    gemini_duration_ms: float = 0,
    synthesis_duration_ms: float = 0,
    fallback_used: bool = False,
    final_status: str = "success",
) -> None:
    """Emit gemilyni.execution_finished event."""
    if not _enabled():
        return

    _emit(ObservabilityEvent(
        event=EventName.GEMILYNI_EXECUTION_FINISHED,
        level=EventLevel.WARNING if final_status != "success" else EventLevel.INFO,
        request_id=run_id,
        trace_id=trace_id,
        metadata_json=_serialize({
            "run_id": run_id,
            "trace_id": trace_id,
            "external_used": external_used,
            "workers_total": workers_total,
            "workers_succeeded": workers_succeeded,
            "workers_failed": workers_failed,
            "containers_started": containers_started,
            "containers_failed": containers_failed,
            "total_duration_ms": total_duration_ms,
            "planning_duration_ms": planning_duration_ms,
            "bundle_duration_ms": bundle_duration_ms,
            "container_start_duration_ms": container_start_duration_ms,
            "gemini_duration_ms": gemini_duration_ms,
            "synthesis_duration_ms": synthesis_duration_ms,
            "fallback_used": fallback_used,
            "final_status": final_status,
        }),
    ))
    _inc_counter("gemilyni_workers_total", workers_total)
    _record_histogram("gemilyni_total_duration_ms", total_duration_ms)
    _record_histogram("gemilyni_planning_duration_ms", planning_duration_ms)
    _record_histogram("gemilyni_bundle_creation_duration_ms", bundle_duration_ms)
    _record_histogram("gemilyni_container_start_duration_ms", container_start_duration_ms)
    _record_histogram("gemilyni_synthesis_duration_ms", synthesis_duration_ms)
    if fallback_used:
        _inc_counter("gemilyni_fallbacks_total")
    if containers_failed > 0:
        _inc_counter("gemilyni_containers_failed_total", containers_failed)


def emit_policy_violation(
    *,
    run_id: str,
    trace_id: str | None = None,
    policy_name: str,
    violation_type: str,
    blocked_item_type: str,
    blocked_item_ref: str,
    reason: str,
    severity: str = "warning",
) -> None:
    """Emit gemilyni.policy_violation event."""
    if not _enabled() or not get_config().capture_policy_events:
        return

    _emit(ObservabilityEvent(
        event=EventName.GEMILYNI_POLICY_VIOLATION,
        level=EventLevel.WARNING if severity == "warning" else EventLevel.ERROR,
        request_id=run_id,
        trace_id=trace_id,
        metadata_json=_serialize({
            "run_id": run_id,
            "trace_id": trace_id,
            "policy_name": policy_name,
            "violation_type": violation_type,
            "blocked_item_type": blocked_item_type,
            "blocked_item_ref": _redact_value(blocked_item_ref, 200),
            "reason": reason,
            "severity": severity,
        }),
    ))
    _inc_counter("gemilyni_policy_blocks_total")
    if blocked_item_type == "context":
        _inc_counter("gemilyni_sensitive_context_blocked_total")


def emit_error(
    *,
    run_id: str,
    trace_id: str | None = None,
    worker_id: str = "",
    container_id: str = "",
    phase: str,
    error_type: str,
    error_message: str,
    recoverable: bool = False,
    fallback_used: bool = False,
) -> None:
    """Emit gemilyni.error event. Error messages are always redacted."""
    if not _enabled():
        return

    cfg = get_config()
    safe_message = _redact_value(error_message, cfg.max_preview_chars)

    _emit(ObservabilityEvent(
        event=EventName.GEMILYNI_ERROR,
        level=EventLevel.ERROR,
        request_id=run_id,
        trace_id=trace_id,
        metadata_json=_serialize({
            "run_id": run_id,
            "trace_id": trace_id,
            "worker_id": worker_id,
            "container_id": container_id,
            "phase": phase,
            "error_type": error_type,
            "error_message_redacted": safe_message,
            "recoverable": recoverable,
            "fallback_used": fallback_used,
        }),
    ))


# ═══════════════════════════════════════════════════════════════════════
# Metrics Helpers (integrate with existing OTel meter)
# ═══════════════════════════════════════════════════════════════════════

_counters: dict[str, Any] = {}
_histograms: dict[str, Any] = {}


def _get_meter():
    """Get the OTel meter for gemilyni instruments."""
    from orchestrator.observability.metrics import get_meter
    return get_meter("gemilyni")


def _inc_counter(name: str, amount: int = 1) -> None:
    """Increment a counter metric."""
    try:
        if name not in _counters:
            meter = _get_meter()
            if meter is None:
                return
            _counters[name] = meter.create_counter(name)
        _counters[name].add(amount)
    except Exception:
        pass  # Metrics are best-effort


def _record_histogram(name: str, value: float) -> None:
    """Record a histogram value."""
    try:
        if name not in _histograms:
            meter = _get_meter()
            if meter is None:
                return
            _histograms[name] = meter.create_histogram(name)
        _histograms[name].record(value)
    except Exception:
        pass  # Metrics are best-effort


# ═══════════════════════════════════════════════════════════════════════
# Serialization
# ═══════════════════════════════════════════════════════════════════════

def _serialize(data: dict[str, Any]) -> str:
    """Serialize metadata dict to JSON string for event storage."""
    import json
    return json.dumps(data, default=str, separators=(",", ":"))


# ═══════════════════════════════════════════════════════════════════════
# Health Checks & Alerts
# ═══════════════════════════════════════════════════════════════════════

def check_gemilyni_health() -> dict[str, Any]:
    """Run health checks for the Gemilyni execution layer.

    Returns dict with check names and pass/fail status.
    """
    checks: dict[str, Any] = {}

    # 1. Docker available
    try:
        import docker
        client = docker.from_env()
        client.ping()
        checks["docker_available"] = True
    except Exception:
        checks["docker_available"] = False

    # 2. Worker image available
    try:
        from orchestrator.config import get_settings
        cfg = get_settings()
        if cfg.execution and cfg.execution.enabled:
            import docker
            client = docker.from_env()
            client.images.get(cfg.execution.worker_image)
            checks["worker_image_available"] = True
        else:
            checks["worker_image_available"] = None  # N/A
    except Exception:
        checks["worker_image_available"] = False

    # 3. OAuth path exists (when auth_mode=oauth)
    try:
        import os
        from pathlib import Path

        from orchestrator.config import get_settings
        cfg = get_settings()
        if cfg.execution and cfg.execution.enabled:
            if cfg.execution.gemini.auth_mode == "oauth":
                oauth_path = Path(os.path.expanduser(cfg.execution.gemini.oauth_credentials_path))
                checks["oauth_path_exists"] = oauth_path.exists()
            else:
                checks["oauth_path_exists"] = None  # N/A
                # Check API key exists
                api_key = os.environ.get(cfg.execution.gemini.api_key_env, "")
                checks["api_key_exists"] = bool(api_key)
    except Exception:
        checks["oauth_path_exists"] = False

    # 4. Observability pipeline available
    from orchestrator.observability import _dispatcher
    checks["observability_pipeline_active"] = _dispatcher is not None

    # 5. Gemilyni config safe defaults
    config = get_config()
    checks["capture_raw_context_disabled"] = not config.capture_raw_context
    checks["capture_raw_files_disabled"] = not config.capture_raw_files
    checks["redact_sensitive_values_enabled"] = config.redact_sensitive_values

    return checks


# Alert condition checkers (for use in monitoring/alerting)

ALERT_THRESHOLDS = {
    "gemini_failure_rate_high": 0.3,  # >30% failure rate
    "container_memory_percent_high": 85.0,
    "container_duration_high_ms": 300_000,  # 5 min
    "policy_violations_per_hour_high": 50,
}


def evaluate_alerts(summary: dict[str, Any]) -> list[dict[str, str]]:
    """Evaluate alert conditions from a gemilyni summary dict.

    Returns list of fired alerts with name, severity, message.
    """
    alerts: list[dict[str, str]] = []

    total = summary.get("total_runs", 0)
    if total > 0:
        failure_rate = summary.get("failure_rate", 0)
        if failure_rate > ALERT_THRESHOLDS["gemini_failure_rate_high"]:
            alerts.append({
                "name": "gemini_failure_rate_high",
                "severity": "warning",
                "message": f"Gemini failure rate is {failure_rate:.0%} (threshold: {ALERT_THRESHOLDS['gemini_failure_rate_high']:.0%})",
            })

    violations = summary.get("violations", 0)
    if violations > ALERT_THRESHOLDS["policy_violations_per_hour_high"]:
        alerts.append({
            "name": "policy_violations_high",
            "severity": "warning",
            "message": f"{violations} policy violations (threshold: {ALERT_THRESHOLDS['policy_violations_per_hour_high']})",
        })

    # Check unsafe config
    config = get_config()
    if config.capture_raw_context:
        alerts.append({
            "name": "capture_raw_context_enabled",
            "severity": "error",
            "message": "capture_raw_context=true is a security risk — raw context will be logged",
        })
    if config.capture_raw_files:
        alerts.append({
            "name": "capture_raw_files_enabled",
            "severity": "error",
            "message": "capture_raw_files=true is a security risk — raw files will be logged",
        })

    return alerts


# ═══════════════════════════════════════════════════════════════════════
# In-Memory Event Store (ring buffer for dashboard queries)
# ═══════════════════════════════════════════════════════════════════════

import collections  # noqa: E402
import json as _json  # noqa: E402
import threading as _threading  # noqa: E402

_event_buffer: collections.deque = collections.deque(maxlen=10_000)
_buffer_lock = _threading.Lock()


def _store_event(event: ObservabilityEvent) -> None:
    """Store event in the in-memory buffer for dashboard queries."""
    with _buffer_lock:
        _event_buffer.append(event)


def get_buffered_events(event_name: str | None = None, hours: int = 24, limit: int = 500) -> list[dict]:
    """Query buffered events, optionally filtered by event name."""
    import time as _time
    cutoff = _time.time() - (hours * 3600)
    results = []
    with _buffer_lock:
        for ev in reversed(_event_buffer):
            if ev.timestamp < cutoff:
                continue
            if event_name and ev.event.value != event_name:
                continue
            try:
                meta = _json.loads(ev.metadata_json) if ev.metadata_json else {}
            except Exception:
                meta = {}
            meta["_timestamp"] = ev.timestamp
            meta["_event"] = ev.event.value
            meta["_request_id"] = ev.request_id
            meta["_trace_id"] = ev.trace_id
            results.append(meta)
            if len(results) >= limit:
                break
    return results


def query_gemilyni_summary(hours: int = 24) -> dict:
    """Compute summary statistics from buffered events."""
    all_events = get_buffered_events(hours=hours, limit=10_000)

    routing = [e for e in all_events if e["_event"] == "gemilyni.routing_decision"]
    finished = [e for e in all_events if e["_event"] == "gemilyni.execution_finished"]
    policy = [e for e in all_events if e["_event"] == "gemilyni.policy_violation"]
    containers = [e for e in all_events if e["_event"] == "gemilyni.container_created"]
    _bundles = [e for e in all_events if e["_event"] == "gemilyni.bundle_created"]
    files_ev = [e for e in all_events if e["_event"] == "gemilyni.bundle_file"]

    total_runs = len(routing)
    external_runs = sum(1 for r in routing if r.get("selected_path") == "execute")
    local_runs = total_runs - external_runs

    successes = sum(1 for f in finished if f.get("final_status") == "success")
    failures = sum(1 for f in finished if f.get("final_status") != "success")
    total_finished = successes + failures

    durations = [f.get("total_duration_ms", 0) for f in finished if f.get("total_duration_ms")]
    gemini_durations = [f.get("gemini_duration_ms", 0) for f in finished if f.get("gemini_duration_ms")]

    workers_executed = sum(f.get("workers_total", 0) for f in finished)
    fallbacks = sum(1 for f in finished if f.get("fallback_used"))

    files_blocked = sum(1 for f in files_ev if f.get("blocked"))
    sources_blocked = sum(
        e.get("blocked_blocks", 0) for e in all_events
        if e["_event"] == "gemilyni.external_context_policy"
    )

    return {
        "total_runs": total_runs,
        "external_runs": external_runs,
        "local_runs": local_runs,
        "success_rate": (successes / total_finished) if total_finished else 0,
        "failure_rate": (failures / total_finished) if total_finished else 0,
        "fallbacks": fallbacks,
        "policy_blocks": len(policy),
        "containers_created": len(containers),
        "workers_executed": workers_executed,
        "avg_duration_ms": (sum(durations) / len(durations)) if durations else 0,
        "avg_gemini_ms": (sum(gemini_durations) / len(gemini_durations)) if gemini_durations else 0,
        "sensitive_blocked": sum(1 for p in policy if p.get("blocked_item_type") == "context"),
        "files_blocked": files_blocked,
        "sources_blocked": sources_blocked,
        "violations": len(policy),
        "traversal_attempts": sum(1 for p in policy if "traversal" in p.get("reason", "")),
    }


def query_gemilyni_runs(hours: int = 24, limit: int = 50, **filters) -> dict:
    """Query recent runs with optional filters."""
    routing = get_buffered_events("gemilyni.routing_decision", hours=hours, limit=500)
    finished = get_buffered_events("gemilyni.execution_finished", hours=hours, limit=500)

    # Index finished by run_id
    finished_map = {f.get("run_id"): f for f in finished}

    runs = []
    for r in routing:
        run_id = r.get("run_id", "")
        if filters.get("run_id") and filters["run_id"] not in run_id:
            continue
        if filters.get("complexity") and r.get("complexity") != filters["complexity"]:
            continue

        fin = finished_map.get(run_id, {})
        status = fin.get("final_status", "in_progress")
        if filters.get("status") and status != filters["status"]:
            continue

        runs.append({
            "run_id": run_id,
            "trace_id": r.get("trace_id", ""),
            "selected_path": r.get("selected_path", ""),
            "reason": r.get("reason", ""),
            "complexity": r.get("complexity", ""),
            "intent": r.get("intent", ""),
            "blocked_by_policy": r.get("blocked_by_policy", False),
            "workers_total": fin.get("workers_total", 0),
            "workers_succeeded": fin.get("workers_succeeded", 0),
            "total_duration_ms": fin.get("total_duration_ms", 0),
            "gemini_duration_ms": fin.get("gemini_duration_ms", 0),
            "final_status": status,
            "fallback_used": fin.get("fallback_used", False),
            "timestamp": r.get("_timestamp", 0),
        })
        if len(runs) >= limit:
            break

    return {"runs": runs}


def query_gemilyni_workers(hours: int = 24, **filters) -> dict:
    """Query worker events."""
    invocations = get_buffered_events("gemilyni.gemini_invocation_finished", hours=hours, limit=500)
    outputs = get_buffered_events("gemilyni.worker_output", hours=hours, limit=500)

    output_map = {o.get("worker_id"): o for o in outputs}

    workers = []
    for inv in invocations:
        worker_id = inv.get("worker_id", "")
        if filters.get("run_id") and inv.get("run_id") != filters["run_id"]:
            continue
        if filters.get("worker_id") and worker_id != filters["worker_id"]:
            continue

        out = output_map.get(worker_id, {})
        workers.append({
            "run_id": inv.get("run_id", ""),
            "worker_id": worker_id,
            "container_id": inv.get("container_id", ""),
            "status": inv.get("status", ""),
            "duration_ms": inv.get("duration_ms", 0),
            "exit_code": inv.get("exit_code", 0),
            "output_tokens": inv.get("output_tokens_estimate", 0),
            "result_json_exists": out.get("result_json_exists", False),
            "patch_diff_exists": out.get("patch_diff_exists", False),
            "patch_size_bytes": out.get("patch_size_bytes", 0),
        })

    return {"workers": workers}


def query_gemilyni_containers(hours: int = 24, **filters) -> dict:
    """Query container events."""
    created = get_buffered_events("gemilyni.container_created", hours=hours, limit=500)
    stats = get_buffered_events("gemilyni.container_stats", hours=hours, limit=500)

    stats_map = {}
    for s in stats:
        cid = s.get("container_id", "")
        stats_map[cid] = s  # latest stat wins

    containers = []
    for c in created:
        if filters.get("run_id") and c.get("run_id") != filters["run_id"]:
            continue
        cid = c.get("container_id", "")
        st = stats_map.get(cid, {})
        containers.append({
            "run_id": c.get("run_id", ""),
            "worker_id": c.get("worker_id", ""),
            "container_id": cid,
            "image": c.get("image", ""),
            "auth_mode": c.get("auth_mode", ""),
            "network_mode": c.get("network_mode", ""),
            "mounts_count": c.get("mounts_count", 0),
            "cpu_percent": st.get("cpu_percent", 0),
            "memory_percent": st.get("memory_percent", 0),
            "memory_usage_bytes": st.get("memory_usage_bytes", 0),
        })

    return {"containers": containers}


def query_gemilyni_bundles(hours: int = 24, **filters) -> dict:
    """Query bundle events."""
    bundles = get_buffered_events("gemilyni.bundle_created", hours=hours, limit=500)
    result = []
    for b in bundles:
        if filters.get("run_id") and b.get("run_id") != filters["run_id"]:
            continue
        result.append({
            "run_id": b.get("run_id", ""),
            "bundle_id": b.get("bundle_id", ""),
            "worker_id": b.get("worker_id", ""),
            "task_type": b.get("task_type", ""),
            "allowed_files_count": b.get("allowed_files_count", 0),
            "blocked_files_count": b.get("blocked_files_count", 0),
            "allowed_context_blocks": b.get("allowed_context_blocks", 0),
            "workspace_mode": b.get("workspace_mode", ""),
        })
    return {"bundles": result}


def query_gemilyni_files(run_id: str = "", bundle_id: str = "", limit: int = 100) -> dict:
    """Query file events."""
    files = get_buffered_events("gemilyni.bundle_file", hours=720, limit=limit)
    result = []
    for f in files:
        if run_id and f.get("run_id") != run_id:
            continue
        if bundle_id and f.get("bundle_id") != bundle_id:
            continue
        result.append({
            "run_id": f.get("run_id", ""),
            "bundle_id": f.get("bundle_id", ""),
            "worker_id": f.get("worker_id", ""),
            "relative_path": f.get("relative_path", ""),
            "file_hash": f.get("file_hash", ""),
            "file_size_bytes": f.get("file_size_bytes", 0),
            "included": f.get("included", False),
            "blocked": f.get("blocked", False),
            "block_reason": f.get("block_reason", ""),
        })
    return {"files": result}


def query_gemilyni_context(run_id: str = "", bundle_id: str = "") -> dict:
    """Query context block events."""
    blocks = get_buffered_events("gemilyni.context_block", hours=720, limit=500)
    result = []
    for b in blocks:
        if run_id and b.get("run_id") != run_id:
            continue
        if bundle_id and b.get("bundle_id") != bundle_id:
            continue
        result.append({
            "run_id": b.get("run_id", ""),
            "bundle_id": b.get("bundle_id", ""),
            "worker_id": b.get("worker_id", ""),
            "source": b.get("source", ""),
            "source_type": b.get("source_type", ""),
            "token_estimate": b.get("token_estimate", 0),
            "size_bytes": b.get("size_bytes", 0),
            "included": b.get("included", False),
            "blocked": b.get("blocked", False),
            "block_reason": b.get("block_reason", ""),
        })
    return {"blocks": result}


def query_gemilyni_policy(hours: int = 24, **filters) -> dict:
    """Query policy violation events."""
    violations = get_buffered_events("gemilyni.policy_violation", hours=hours, limit=500)
    result = []
    for v in violations:
        if filters.get("run_id") and v.get("run_id") != filters["run_id"]:
            continue
        result.append({
            "run_id": v.get("run_id", ""),
            "policy_name": v.get("policy_name", ""),
            "violation_type": v.get("violation_type", ""),
            "blocked_item_type": v.get("blocked_item_type", ""),
            "blocked_item_ref": v.get("blocked_item_ref", ""),
            "reason": v.get("reason", ""),
            "severity": v.get("severity", "warning"),
        })
    return {"violations": result}


def query_gemilyni_errors(hours: int = 24, **filters) -> dict:
    """Query error events."""
    errors = get_buffered_events("gemilyni.error", hours=hours, limit=500)
    result = []
    for e in errors:
        if filters.get("run_id") and e.get("run_id") != filters["run_id"]:
            continue
        result.append({
            "run_id": e.get("run_id", ""),
            "worker_id": e.get("worker_id", ""),
            "container_id": e.get("container_id", ""),
            "phase": e.get("phase", ""),
            "error_type": e.get("error_type", ""),
            "error_message_redacted": e.get("error_message_redacted", ""),
            "recoverable": e.get("recoverable", False),
            "fallback_used": e.get("fallback_used", False),
        })
    return {"errors": result}


def query_gemilyni_performance(hours: int = 24) -> dict:
    """Query performance breakdown."""
    finished = get_buffered_events("gemilyni.execution_finished", hours=hours, limit=500)
    phases = {
        "planning": [],
        "bundle": [],
        "container_start": [],
        "gemini": [],
        "synthesis": [],
        "total": [],
    }
    for f in finished:
        if f.get("planning_duration_ms"):
            phases["planning"].append(f["planning_duration_ms"])
        if f.get("bundle_duration_ms"):
            phases["bundle"].append(f["bundle_duration_ms"])
        if f.get("container_start_duration_ms"):
            phases["container_start"].append(f["container_start_duration_ms"])
        if f.get("gemini_duration_ms"):
            phases["gemini"].append(f["gemini_duration_ms"])
        if f.get("synthesis_duration_ms"):
            phases["synthesis"].append(f["synthesis_duration_ms"])
        if f.get("total_duration_ms"):
            phases["total"].append(f["total_duration_ms"])

    result = {}
    for name, values in phases.items():
        if values:
            result[name] = {
                "count": len(values),
                "avg_ms": sum(values) / len(values),
                "min_ms": min(values),
                "max_ms": max(values),
                "p50_ms": sorted(values)[len(values) // 2],
            }
        else:
            result[name] = {"count": 0, "avg_ms": 0, "min_ms": 0, "max_ms": 0, "p50_ms": 0}

    return {"phases": result}


# ═══════════════════════════════════════════════════════════════════════
# Demo Data Seeding (populates the in-memory buffer on startup)
# ═══════════════════════════════════════════════════════════════════════

def seed_demo_data() -> None:
    """Populate the event buffer with realistic multi-container demo data.

    Called at app startup when gemilyni is enabled and the buffer is empty.
    Generates 8 external runs (2-4 workers each), 5 local runs, 3 policy blocks.
    """
    if not _enabled():
        return
    if len(_event_buffer) > 0:
        return  # Already populated

    import random
    import uuid

    random.seed(42)  # Deterministic for consistent dashboard

    _SCENARIOS = [
        {"name": "auth_refactor", "complexity": "COMPLEX", "intent": "CODE", "workers": 3,
         "files": ["src/auth/login.py", "src/auth/oauth.py", "src/auth/session.py", "src/auth/__init__.py", "tests/test_auth.py"],
         "blocked_files": [".env", ".env.production"], "ctx_sources": ["code", "repo", "git_history"],
         "blocked_sources": ["email"], "status": "success"},
        {"name": "realtime_notifications", "complexity": "COMPLEX", "intent": "CODE", "workers": 4,
         "files": ["src/notifications/ws.py", "src/notifications/broker.py", "src/notifications/models.py", "src/notifications/config.py", "src/api/routes/notify.py", "tests/test_notify.py"],
         "blocked_files": ["secrets.yaml"], "ctx_sources": ["code", "repo", "docs"],
         "blocked_sources": ["calendar", "rss"], "status": "success"},
        {"name": "memory_leak_fix", "complexity": "MODERATE", "intent": "DEBUG", "workers": 2,
         "files": ["src/pool/manager.py", "src/pool/worker.py", "tests/test_pool.py"],
         "blocked_files": [], "ctx_sources": ["code", "logs"], "blocked_sources": [], "status": "success"},
        {"name": "graphql_api", "complexity": "COMPLEX", "intent": "CODE", "workers": 3,
         "files": ["src/graphql/schema.py", "src/graphql/resolvers.py", "src/graphql/types.py", "src/graphql/mutations.py", "tests/test_gql.py"],
         "blocked_files": ["deploy_keys.json", ".gcloud/credentials.json"], "ctx_sources": ["code", "repo", "docs"],
         "blocked_sources": ["email"], "status": "success"},
        {"name": "db_optimization", "complexity": "MODERATE", "intent": "OPTIMIZE", "workers": 2,
         "files": ["src/db/queries.py", "src/db/indexes.py", "src/db/pool.py"],
         "blocked_files": [".pgpass"], "ctx_sources": ["code", "metrics"], "blocked_sources": [], "status": "success"},
        {"name": "multi_tenant", "complexity": "COMPLEX", "intent": "CODE", "workers": 4,
         "files": ["src/tenant/isolator.py", "src/tenant/router.py", "src/tenant/middleware.py", "src/tenant/models.py", "src/db/migrations/0042.py", "tests/test_tenant.py"],
         "blocked_files": ["terraform.tfvars", ".env.staging"], "ctx_sources": ["code", "repo", "architecture_docs"],
         "blocked_sources": ["email", "calendar"], "status": "partial_failure"},
        {"name": "event_bus_race", "complexity": "MODERATE", "intent": "DEBUG", "workers": 2,
         "files": ["src/events/bus.py", "src/events/consumer.py", "tests/test_events.py"],
         "blocked_files": [], "ctx_sources": ["code", "logs", "traces"], "blocked_sources": [], "status": "success"},
        {"name": "pdf_reports", "complexity": "COMPLEX", "intent": "CODE", "workers": 3,
         "files": ["src/reports/generator.py", "src/reports/templates.py", "src/reports/renderer.py", "src/reports/charts.py", "tests/test_reports.py"],
         "blocked_files": ["api_keys.yml"], "ctx_sources": ["code", "repo"],
         "blocked_sources": ["rss"], "status": "failed"},
    ]

    def _rid():
        return f"run-{uuid.uuid4().hex[:12]}"

    def _tid():
        return f"trace-{uuid.uuid4().hex[:16]}"

    def _cid():
        return f"ctr-{uuid.uuid4().hex[:12]}"

    for sc in _SCENARIOS:
        run_id = _rid()
        trace_id = _tid()
        n_workers = sc["workers"]

        emit_routing_decision(
            run_id=run_id, trace_id=trace_id, selected_path="execute",
            reason="complexity_above_threshold", complexity=sc["complexity"],
            complexity_threshold="MODERATE", intent=sc["intent"],
            externalizable=True, execution_enabled=True,
        )

        n_allowed = len(sc["ctx_sources"])
        n_blocked = len(sc["blocked_sources"])
        emit_external_context_policy(
            run_id=run_id, trace_id=trace_id,
            original_blocks=n_allowed * 3 + n_blocked * 2,
            allowed_blocks=n_allowed * 3, blocked_blocks=n_blocked * 2,
            allowed_sources=sc["ctx_sources"], blocked_sources=sc["blocked_sources"],
        )

        worker_durations = []
        workers_ok = 0
        workers_err = 0

        for w_idx in range(n_workers):
            worker_id = f"w{w_idx + 1}"
            bundle_id = f"{run_id}:{worker_id}"
            container_id = _cid()
            wfiles = sc["files"][w_idx::n_workers]
            wblocked = sc["blocked_files"] if w_idx == 0 else []

            emit_bundle_created(
                run_id=run_id, trace_id=trace_id, bundle_id=bundle_id,
                worker_id=worker_id, task_type=f"subtask_{w_idx + 1}",
                allowed_files_count=len(wfiles), blocked_files_count=len(wblocked),
                allowed_context_blocks=random.randint(2, 6),
                blocked_context_blocks=n_blocked,
                workspace_mode="partial_copy", workspace_readonly=True,
                bundle_root=f"/tmp/gemilyni/{run_id}/{worker_id}",
            )

            for fp in wfiles:
                emit_bundle_file(
                    run_id=run_id, trace_id=trace_id, bundle_id=bundle_id,
                    worker_id=worker_id, relative_path=fp,
                    file_hash=uuid.uuid4().hex[:16],
                    file_size_bytes=random.randint(500, 15000), included=True,
                )

            for fp in wblocked:
                emit_bundle_file(
                    run_id=run_id, trace_id=trace_id, bundle_id=bundle_id,
                    worker_id=worker_id, relative_path=fp,
                    file_hash=uuid.uuid4().hex[:16],
                    file_size_bytes=random.randint(100, 2000),
                    included=False, blocked=True, block_reason="blocked_pattern",
                )
                emit_policy_violation(
                    run_id=run_id, trace_id=trace_id,
                    policy_name="bundle_file_filter", violation_type="file_blocked",
                    blocked_item_type="file", blocked_item_ref=fp,
                    reason="blocked_pattern", severity="warning",
                )

            for src in sc["ctx_sources"]:
                for _ in range(random.randint(1, 3)):
                    emit_context_block(
                        run_id=run_id, trace_id=trace_id, bundle_id=bundle_id,
                        worker_id=worker_id, source=src,
                        source_type="file" if src == "code" else "api",
                        block_hash=uuid.uuid4().hex[:16],
                        token_estimate=random.randint(100, 2000),
                        size_bytes=random.randint(400, 8000), included=True,
                    )

            for src in sc["blocked_sources"]:
                emit_context_block(
                    run_id=run_id, trace_id=trace_id, bundle_id=bundle_id,
                    worker_id=worker_id, source=src, source_type="api",
                    block_hash=uuid.uuid4().hex[:16],
                    token_estimate=random.randint(200, 1500),
                    size_bytes=random.randint(500, 5000),
                    included=False, blocked=True, block_reason="source_not_in_allowlist",
                )

            emit_container_created(
                run_id=run_id, trace_id=trace_id, worker_id=worker_id,
                container_id=container_id, image="orc-execution-worker:latest",
                auth_mode="oauth", mounts_count=random.randint(2, 5), network_mode="none",
            )
            emit_container_started(
                run_id=run_id, trace_id=trace_id,
                worker_id=worker_id, container_id=container_id,
            )

            for _ in range(random.randint(2, 4)):
                emit_container_stats(
                    run_id=run_id, trace_id=trace_id,
                    worker_id=worker_id, container_id=container_id,
                    cpu_percent=random.uniform(15, 85),
                    memory_usage_bytes=random.randint(50_000_000, 500_000_000),
                    memory_limit_bytes=1_073_741_824,
                    memory_percent=random.uniform(5, 50),
                    network_rx_bytes=random.randint(1000, 100_000),
                    network_tx_bytes=random.randint(500, 50_000),
                )

            input_tokens = random.randint(800, 4000)
            emit_gemini_invocation_started(
                run_id=run_id, trace_id=trace_id,
                worker_id=worker_id, container_id=container_id,
                auth_mode="oauth", command_mode="interactive",
                model="gemini-2.5-flash", input_tokens_estimate=input_tokens,
            )

            w_ok = True
            if sc["status"] == "failed" and w_idx == n_workers - 1:
                w_ok = False
            elif sc["status"] == "partial_failure" and w_idx == n_workers - 1:
                w_ok = False

            dur = random.uniform(2000, 12000)
            if w_ok:
                emit_gemini_invocation_finished(
                    run_id=run_id, trace_id=trace_id,
                    worker_id=worker_id, container_id=container_id,
                    status="success", exit_code=0, duration_ms=dur,
                    output_tokens_estimate=random.randint(300, 2500),
                    stdout_size_bytes=random.randint(1000, 10000),
                )
                emit_worker_output(
                    run_id=run_id, trace_id=trace_id,
                    worker_id=worker_id, container_id=container_id,
                    result_json_exists=True, patch_diff_exists=True,
                    patch_size_bytes=random.randint(500, 8000),
                    result_size_bytes=random.randint(200, 3000),
                    output_files=["result.json", "changes.patch", "execution.log"],
                )
                workers_ok += 1
            else:
                emit_gemini_invocation_finished(
                    run_id=run_id, trace_id=trace_id,
                    worker_id=worker_id, container_id=container_id,
                    status="error", exit_code=1, duration_ms=dur,
                    error_type="GeminiTimeoutError",
                    stderr_size_bytes=random.randint(200, 2000),
                )
                emit_error(
                    run_id=run_id, trace_id=trace_id,
                    worker_id=worker_id, container_id=container_id,
                    phase="gemini_invocation", error_type="GeminiTimeoutError",
                    error_message=f"Gemini timed out after {dur:.0f}ms. Token: Bearer eyJhbGciOiJSUzI1NiJ9.expired",
                    recoverable=True, fallback_used=(sc["status"] == "partial_failure"),
                )
                workers_err += 1

            worker_durations.append(dur)

        total_dur = sum(worker_durations) + random.uniform(500, 2000)
        final = "success" if sc["status"] == "success" else ("partial_success" if sc["status"] == "partial_failure" else "failed")

        emit_execution_finished(
            run_id=run_id, trace_id=trace_id, external_used=True,
            workers_total=n_workers, workers_succeeded=workers_ok,
            workers_failed=workers_err, containers_started=n_workers,
            containers_failed=workers_err, total_duration_ms=total_dur,
            planning_duration_ms=random.uniform(200, 800),
            bundle_duration_ms=random.uniform(100, 500),
            container_start_duration_ms=random.uniform(500, 2000),
            gemini_duration_ms=sum(worker_durations),
            synthesis_duration_ms=random.uniform(300, 1500),
            fallback_used=(sc["status"] == "partial_failure"),
            final_status=final,
        )

    # Local-only runs
    for i in range(5):
        emit_routing_decision(
            run_id=_rid(), trace_id=_tid(), selected_path="local",
            reason="complexity_below_threshold", complexity="SIMPLE",
            complexity_threshold="MODERATE",
            intent=["QUESTION", "EXPLAIN", "SUMMARIZE", "QUESTION", "EXPLAIN"][i],
            externalizable=False, execution_enabled=True,
        )

    # Policy-blocked runs
    for _ in range(3):
        rid = _rid()
        tid = _tid()
        emit_routing_decision(
            run_id=rid, trace_id=tid, selected_path="blocked",
            reason="policy_denied_execution", complexity="COMPLEX",
            complexity_threshold="MODERATE", intent="CODE",
            blocked_by_policy=True, externalizable=True, execution_enabled=False,
        )
        emit_policy_violation(
            run_id=rid, trace_id=tid,
            policy_name="execution_policy", violation_type="execution_blocked",
            blocked_item_type="run", blocked_item_ref=f"Blocked: sensitive project ({rid[:8]})",
            reason="execution_disabled_by_admin", severity="error",
        )

    log.info("Gemilyni: seeded %d demo events (16 runs, 23 containers)", len(_event_buffer))
