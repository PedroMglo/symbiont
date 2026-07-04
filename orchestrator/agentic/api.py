"""FastAPI routes for the agentic operational runtime."""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import asdict
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from orchestrator.agentic.actuator import get_actuator_status, rollback_actuation
from orchestrator.agentic.event_loop import get_event_loop_status
from orchestrator.agentic.events import build_task_event_feed, sse_encode_control, sse_encode_event
from orchestrator.agentic.improvements import apply_improvement, reject_improvement, request_improvement_approval
from orchestrator.agentic.live import build_live_snapshot
from orchestrator.agentic.models import ApprovalStatus, TaskStatus
from orchestrator.agentic.policy import check_policy, get_policy_engine, normalize_action
from orchestrator.agentic.readiness import cockpit_coverage_fields, evaluate_autonomous_readiness
from orchestrator.agentic.reflection import build_agentic_reflection
from orchestrator.agentic.runner import get_runner_status
from orchestrator.agentic.runtime import cancel_task, resume_task, retry_task
from orchestrator.agentic.store import get_agentic_store
from orchestrator.agentic.timeline import build_task_timeline
from orchestrator.agentic.tools.command.service import CommandToolService
from orchestrator.config import get_settings
from orchestrator.evidence.chaos_import import import_chaos_proposals
from orchestrator.evidence.docker_shield import docker_shield_evidence, docker_shield_summary
from orchestrator.evidence.local_resilience import (
    REPORTS as LOCAL_RESILIENCE_REPORTS,
)
from orchestrator.evidence.local_resilience import (
    local_resilience_evidence,
    local_resilience_summary,
)
from orchestrator.ops.resilience_drills import (
    DrillBlockedError,
    approve_resilience_drill,
    request_resilience_drill,
    rollback_resilience_drill,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/agentic", tags=["agentic"])


class CreateTaskRequest(BaseModel):
    goal: str = Field(..., min_length=1, max_length=16000)
    mode: str | None = None
    priority: str = "normal"
    session_id: str | None = None
    budget: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class PolicyCheckRequest(BaseModel):
    action: str = Field(..., min_length=1, max_length=200)
    payload: dict[str, Any] = Field(default_factory=dict)


class ApprovalApproveRequest(BaseModel):
    approved_by: str = Field("user", min_length=1, max_length=200)


class ApprovalRejectRequest(BaseModel):
    reason: str = Field("", max_length=1000)


class ResumeTaskRequest(BaseModel):
    reason: str = Field("manual_resume", max_length=500)


class RollbackActuationRequest(BaseModel):
    reason: str = Field("manual_rollback", max_length=500)


class CommandClassifyRequest(BaseModel):
    command: str = Field(..., min_length=1, max_length=4000)
    context_profile: str | None = Field(None, max_length=100)


class CreateCommandSessionRequest(BaseModel):
    context_profile: str | None = Field(None, max_length=100)
    cwd: str | None = Field(None, max_length=500)
    task_id: str | None = Field(None, max_length=100)
    trace_id: str | None = Field(None, max_length=100)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RunCommandRequest(BaseModel):
    command: str = Field(..., min_length=1, max_length=4000)
    cwd: str | None = Field(None, max_length=500)
    task_id: str | None = Field(None, max_length=100)
    trace_id: str | None = Field(None, max_length=100)


class CloseCommandSessionRequest(BaseModel):
    reason: str = Field("manual_close", max_length=500)


class CreatePreapprovalWindowRequest(BaseModel):
    action: str = Field(..., min_length=1, max_length=200)
    scope: dict[str, Any] = Field(default_factory=dict)
    ttl_seconds: int | None = Field(None, ge=1)
    max_uses: int | None = Field(None, ge=1)
    reason: str = Field("", max_length=1000)
    created_by: str = Field("user", min_length=1, max_length=200)
    task_id: str | None = Field(None, max_length=100)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RestoreTestRequest(BaseModel):
    volume: str = Field(..., min_length=1, max_length=200)
    requested_by: str = Field("user", min_length=1, max_length=200)


class DrillRequest(BaseModel):
    requested_by: str = Field("user", min_length=1, max_length=200)
    ttl_seconds: int = Field(300, ge=1, le=3600)


class RevokePreapprovalWindowRequest(BaseModel):
    reason: str = Field("manual_revoke", max_length=1000)


class PublishAiLocalEventRequest(BaseModel):
    producer: str = Field(..., min_length=1, max_length=200)
    type: str = Field(..., min_length=1, max_length=200)
    severity: Literal["debug", "info", "low", "medium", "high", "critical"] = "info"
    payload: dict[str, Any] = Field(default_factory=dict)
    evidence_ref: str | None = Field(None, max_length=1000)
    task_id: str | None = Field(None, max_length=160)
    trace_id: str | None = Field(None, max_length=160)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RecordAgenticMemoryRequest(BaseModel):
    kind: Literal["working", "episodic", "semantic_ref", "procedural_ref", "preference_ref"]
    owner: str = Field("orchestrator/agentic", min_length=1, max_length=200)
    source: str = Field(..., min_length=1, max_length=200)
    content: str = Field(..., min_length=1, max_length=8000)
    evidence_refs: list[str] = Field(default_factory=list)
    expires_at: float | None = Field(None, ge=0)
    sensitivity: Literal["normal", "sensitive", "secret"] = "normal"
    redaction_status: Literal["not_required", "redacted", "redacted_only"] = "not_required"
    storage_artifact_ref: str | None = Field(None, max_length=1000)
    semantic_ref: dict[str, Any] = Field(default_factory=dict)
    task_id: str | None = Field(None, max_length=160)
    trace_id: str | None = Field(None, max_length=160)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RetrieveAgenticMemoryRequest(BaseModel):
    query: str = Field("", max_length=4000)
    kinds: list[Literal["working", "episodic", "semantic_ref", "procedural_ref", "preference_ref"]] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    metadata_filter: dict[str, Any] = Field(default_factory=dict)
    include_expired: bool = False
    limit: int = Field(12, ge=1, le=100)
    min_score: float = Field(0.1, ge=0.0, le=10.0)
    task_id: str | None = Field(None, max_length=160)
    trace_id: str | None = Field(None, max_length=160)


def _preapproval_allowed_actions(cfg: Any) -> set[str]:
    raw = str(getattr(cfg, "preapproval_window_allowed_actions", "") or "")
    return {normalize_action(part) for part in raw.split(",") if part.strip()}


def _validated_preapproval_window_request(req: CreatePreapprovalWindowRequest) -> tuple[str, int, int, dict[str, Any]]:
    cfg = get_settings().agentic_runtime
    if not cfg.preapproval_windows_enabled:
        raise HTTPException(status_code=409, detail="Preapproval windows are disabled by configuration")
    decision = check_policy(req.action, req.scope)
    allowed_actions = _preapproval_allowed_actions(cfg)
    if decision.action not in allowed_actions:
        raise HTTPException(status_code=403, detail=f"Action {decision.action} is not safe-listed for preapproval windows")
    if decision.risk_level in {"high", "deny"} or decision.requires_approval:
        raise HTTPException(status_code=403, detail="High-risk or denied actions cannot use preapproval windows")
    ttl = min(
        int(req.ttl_seconds or cfg.preapproval_window_default_ttl_seconds),
        int(cfg.preapproval_window_max_ttl_seconds),
    )
    uses = min(int(req.max_uses or 1), int(cfg.preapproval_window_max_uses))
    metadata = {
        **req.metadata,
        "policy_decision": decision.to_dict(),
        "phase": "13",
        "safe_listed": True,
    }
    return decision.action, ttl, uses, metadata


def _task_trace_ref(store: Any, task_id: str | None) -> dict[str, Any]:
    if not task_id:
        return {}
    task = store.get_task(str(task_id))
    if task is None:
        return {"task_id": str(task_id)}
    data = task.to_dict()
    return {"task_id": data["id"], "trace_id": data.get("trace_id"), "status": data.get("status")}


def _links(
    *,
    task_id: str | None = None,
    approval_id: str | None = None,
    preapproval_window_id: str | None = None,
    proposal_id: str | None = None,
    actuation_id: str | None = None,
) -> dict[str, str]:
    links: dict[str, str] = {}
    if task_id:
        links["task"] = f"/agentic/tasks/{task_id}"
        links["trace"] = f"/agentic/tasks/{task_id}/trace"
        links["explain"] = f"/agentic/tasks/{task_id}/explain"
        links["evidence"] = f"/agentic/evidence?task_id={task_id}"
    if approval_id:
        links["approval"] = f"/agentic/approvals/{approval_id}"
        links["approval_evidence"] = f"/agentic/evidence?approval_id={approval_id}"
    if preapproval_window_id:
        links["preapproval_window"] = f"/agentic/preapproval-windows/{preapproval_window_id}"
        links["preapproval_evidence"] = f"/agentic/evidence?preapproval_window_id={preapproval_window_id}"
    if proposal_id:
        links["proposal"] = f"/agentic/improvements/{proposal_id}"
        links["proposal_evidence"] = f"/agentic/evidence?proposal_id={proposal_id}"
    if actuation_id:
        links["actuation"] = f"/agentic/actuations/{actuation_id}"
        links["actuation_evidence"] = f"/agentic/evidence?actuation_id={actuation_id}"
    return links


def _query_int(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _query_float(value: Any, *, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _feature_client_for_agentic():
    try:
        from orchestrator.gateway import app as gateway_app

        engine = getattr(gateway_app, "_engine", None)
        client = getattr(engine, "_feature_client", None)
        if client is not None:
            return client
    except Exception:
        pass

    from orchestrator.dispatch.feature_client import FeatureClient
    from orchestrator.factory import _build_service_registry

    return FeatureClient(_build_service_registry())


def _request_storage_restore_test(req: RestoreTestRequest) -> dict[str, Any]:
    client = _feature_client_for_agentic()
    response = client.invoke_endpoint(
        "storage_guardian",
        method="POST",
        path="/internal/storage/restore-tests",
        payload={"volume": req.volume, "requested_by": req.requested_by},
        timeout=60.0,
        policy_action="storage.restore_test",
    )
    if response.success:
        return dict(response.data or {})
    error = str(response.error or "restore test request failed")
    if "404" in error or "unknown volume" in error.lower():
        raise HTTPException(status_code=404, detail="Unknown volume")
    raise HTTPException(status_code=409, detail="Restore test is not allowed")


def _collect_attention_items(
    store: Any,
    *,
    pending_approvals: list[dict[str, Any]],
    waiting_tasks: list[dict[str, Any]],
    proposals: list[dict[str, Any]],
    actuations: list[dict[str, Any]],
    escalation_events: list[dict[str, Any]],
    runtime_flags: list[dict[str, Any]],
    preapproval_windows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for approval in pending_approvals:
        task_ref = _task_trace_ref(store, approval.get("task_id"))
        items.append(
            {
                "type": "approval",
                "severity": approval.get("risk_level") or "unknown",
                "status": approval.get("status"),
                "title": approval.get("action"),
                "approval_id": approval.get("id"),
                "task_id": task_ref.get("task_id"),
                "trace_id": task_ref.get("trace_id"),
                "payload_hash": approval.get("payload_hash"),
                "payload_preview": approval.get("payload_preview"),
                "expires_at": approval.get("expires_at"),
                "links": _links(task_id=task_ref.get("task_id"), approval_id=approval.get("id")),
            }
        )
    for task in waiting_tasks:
        items.append(
            {
                "type": "task_waiting_approval",
                "severity": "medium",
                "status": task.get("status"),
                "title": task.get("goal"),
                "task_id": task.get("id"),
                "trace_id": task.get("trace_id"),
                "links": _links(task_id=task.get("id")),
            }
        )
    for proposal in proposals:
        items.append(
            {
                "type": "improvement_proposal",
                "severity": proposal.get("risk_level") or "unknown",
                "status": proposal.get("status"),
                "title": proposal.get("title"),
                "proposal_id": proposal.get("id"),
                "approval_id": proposal.get("approval_id"),
                "task_id": proposal.get("task_id"),
                "trace_id": _task_trace_ref(store, proposal.get("task_id")).get("trace_id"),
                "links": _links(
                    task_id=proposal.get("task_id"),
                    approval_id=proposal.get("approval_id"),
                    proposal_id=proposal.get("id"),
                ),
            }
        )
    for actuation in actuations:
        items.append(
            {
                "type": "actuation",
                "severity": "medium",
                "status": actuation.get("status"),
                "title": actuation.get("action"),
                "actuation_id": actuation.get("id"),
                "proposal_id": actuation.get("proposal_id"),
                "task_id": actuation.get("task_id"),
                "trace_id": _task_trace_ref(store, actuation.get("task_id")).get("trace_id"),
                "expires_at": actuation.get("expires_at"),
                "links": _links(
                    task_id=actuation.get("task_id"),
                    proposal_id=actuation.get("proposal_id"),
                    actuation_id=actuation.get("id"),
                ),
            }
        )
    for event in escalation_events:
        payload = event.get("payload") or {}
        route = payload.get("route") or {}
        items.append(
            {
                "type": "escalation",
                "severity": route.get("risk_level") or "medium",
                "status": "planned",
                "title": route.get("domain") or event.get("event_type"),
                "event_id": event.get("id"),
                "task_id": event.get("task_id"),
                "proposal_id": payload.get("proposal_id"),
                "actuation_id": payload.get("actuation_id"),
                "timestamp": event.get("timestamp"),
                "links": _links(
                    task_id=event.get("task_id"),
                    proposal_id=payload.get("proposal_id"),
                    actuation_id=payload.get("actuation_id"),
                ),
            }
        )
    for flag in runtime_flags:
        items.append(
            {
                "type": "runtime_flag",
                "severity": "low",
                "status": "active",
                "title": flag.get("key"),
                "key": flag.get("key"),
                "expires_at": flag.get("expires_at"),
                "links": {"runtime_flags": "/agentic/runtime-flags"},
            }
        )
    for window in preapproval_windows:
        task_ref = _task_trace_ref(store, window.get("task_id"))
        items.append(
            {
                "type": "preapproval_window",
                "severity": "medium",
                "status": window.get("status"),
                "title": window.get("action"),
                "preapproval_window_id": window.get("id"),
                "task_id": task_ref.get("task_id"),
                "trace_id": task_ref.get("trace_id"),
                "expires_at": window.get("expires_at"),
                "used_count": window.get("used_count"),
                "max_uses": window.get("max_uses"),
                "links": _links(
                    task_id=task_ref.get("task_id"),
                    preapproval_window_id=window.get("id"),
                ),
            }
        )
    return items


def _related_events(
    store: Any,
    *,
    task_ids: set[str],
    approval_id: str | None,
    preapproval_window_id: str | None,
    proposal_id: str | None,
    actuation_id: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for event in store.list_events(limit=limit):
        payload = event.get("payload") or {}
        if event.get("task_id") in task_ids:
            matches.append(event)
            continue
        if approval_id and payload.get("approval_id") == approval_id:
            matches.append(event)
            continue
        if preapproval_window_id and payload.get("window_id") == preapproval_window_id:
            matches.append(event)
            continue
        if proposal_id and payload.get("proposal_id") == proposal_id:
            matches.append(event)
            continue
        if actuation_id and payload.get("actuation_id") == actuation_id:
            matches.append(event)
    return matches


@router.get("/status")
def agentic_status() -> dict[str, Any]:
    cfg = get_settings().agentic_runtime
    store = get_agentic_store()
    store.expire_resource_leases()
    pending_approvals = len(store.list_approvals(status=ApprovalStatus.PENDING.value, limit=500))
    active_preapprovals = store.list_preapproval_windows(status="active", limit=500)
    recent_tasks = store.list_tasks(limit=10)
    return {
        "enabled": cfg.enabled,
        "shadow_ledger_enabled": cfg.shadow_ledger_enabled,
        "default_mode": cfg.default_mode,
        "autonomous_safe_enabled": cfg.autonomous_safe_enabled,
        "policy_mode": cfg.policy_mode,
        "db_path": str(store.path),
        "runner": get_runner_status(),
        "event_loop": get_event_loop_status(),
        "actuator": get_actuator_status(),
        "queue_depth": store.count_tasks(status=TaskStatus.QUEUED.value) + store.count_tasks(status=TaskStatus.RECOVERING.value),
        "active_task_ids": store.active_task_ids(),
        "status_counts": store.task_status_counts(),
        "runtime_flags": store.list_runtime_flags(),
        "pending_approvals": pending_approvals,
        "preapproval_windows": {
            "enabled": cfg.preapproval_windows_enabled,
            "active": len(active_preapprovals),
            "allowed_actions": sorted(_preapproval_allowed_actions(cfg)),
        },
        "improvement_proposals": {
            "proposed": len(store.list_improvement_proposals(status="proposed", limit=500)),
            "waiting_approval": len(store.list_improvement_proposals(status="waiting_approval", limit=500)),
        },
        "actuations": {
            "active": len(store.list_actuations(status="active", limit=500)),
            "applied": len(store.list_actuations(status="applied", limit=500)),
        },
        "agentic_artifacts": {
            "recent_ai_events": len(store.list_ai_local_events(limit=100)),
        },
        "commands": {
            "status": CommandToolService(store=store).status(),
            "open_sessions": len(store.list_command_sessions(status="open", limit=500)),
        },
        "recent_tasks": recent_tasks,
    }


@router.get("/capabilities/actions")
def list_agentic_action_capabilities() -> dict[str, Any]:
    from orchestrator.agentic.tool_envelope import action_tool_envelopes

    capabilities = [envelope.to_public_dict() for envelope in action_tool_envelopes()]
    return {"capabilities": capabilities, "count": len(capabilities)}


@router.get("/capabilities/services")
def list_agentic_service_capabilities(kind: Literal["all", "agent", "feature"] = "all") -> dict[str, Any]:
    from orchestrator.capabilities.catalog import service_capability_manifests, service_catalog_entry_map

    agent_model_metadata: dict[str, dict[str, Any]] = {}
    model_router = None
    try:
        from orchestrator.routing.model_router import ConfigModelRouter

        model_router = ConfigModelRouter()
    except Exception:
        model_router = None
    try:
        from orchestrator.registry import get_registry

        registry = get_registry()
        for agent_name, cfg in registry.get_all_agent_configs().items():
            agent_model_metadata[agent_name] = {
                "llm_model": cfg.model,
                "llm_backend_type": cfg.backend_type,
                "llm_timeout": cfg.timeout,
            }
    except Exception:
        agent_model_metadata = {}
    catalog_by_service = service_catalog_entry_map()
    services = []
    for manifest in service_capability_manifests():
        if kind != "all" and manifest.kind != kind:
            continue
        entry = catalog_by_service.get(manifest.service_name)
        model_metadata = agent_model_metadata.get(manifest.service_name, {})
        service = {
            "name": manifest.service_name,
            "kind": manifest.kind,
            "owner": manifest.owner,
            "capability_id": manifest.capability_id,
            "capabilities": list(manifest.capabilities),
            "description": manifest.description or (entry.description if entry is not None else ""),
            "timeout_seconds": manifest.timeout_seconds if manifest.timeout_seconds is not None else (entry.timeout if entry is not None else None),
            "health_path": entry.health_path if entry is not None else "/health",
            "enabled": entry.enabled if entry is not None else True,
            "policy_action": manifest.policy_action,
            "transport": manifest.transport,
            "risk_level": manifest.risk_level,
            "supported_action_types": list(manifest.supported_action_types),
            "resource_profile": manifest.resource_profile,
            "input_schema": manifest.input_schema,
            "output_schema": manifest.output_schema,
            "evidence_types": list(manifest.evidence_types),
            "owner_family": manifest.owner_family,
            "lifecycle_status": manifest.lifecycle_status,
            "consolidation_target": manifest.consolidation_target,
            "writes_allowed": manifest.writes_allowed,
            "idempotency_policy": manifest.idempotency_policy,
            "dry_run_supported": manifest.dry_run_supported,
            "rollback_supported": manifest.rollback_supported,
            "events_published": list(manifest.events_published),
            "risk_review_criteria": list(manifest.risk_review_criteria),
            "round_dependencies": list(manifest.round_dependencies),
            "model_profile": manifest.model_profile,
            "model": model_metadata.get("llm_model"),
            "model_backend": model_metadata.get("llm_backend_type"),
        }
        if manifest.kind == "agent" and service["model_profile"] is None:
            service["model_profile"] = service["resource_profile"].get("model_profile")
        if manifest.kind == "agent" and service["model_profile"] and model_router is not None:
            selection = model_router.select_model_profile(str(service["model_profile"]), fallback_profile=None)
            service["model_selection"] = selection.to_event_payload() if selection is not None else None
        else:
            service["model_selection"] = None
        services.append(service)
    return {"services": services, "count": len(services), "kind": kind}


@router.get("/capabilities/tool-envelopes")
def list_agentic_runtime_tool_envelopes(kind: Literal["all", "action", "service"] = "all") -> dict[str, Any]:
    from orchestrator.agentic.tool_envelope import runtime_tool_envelopes

    envelopes = [envelope.to_public_dict() for envelope in runtime_tool_envelopes(kind=kind)]
    return {"tool_envelopes": envelopes, "count": len(envelopes), "kind": kind}


@router.get("/capabilities/search")
def search_agentic_capabilities(
    query: str = Query(..., min_length=1, max_length=1000),
    max_results: int = Query(8, ge=1, le=50),
    kind: Literal["all", "action", "service"] = "all",
) -> dict[str, Any]:
    from orchestrator.agentic.capability_search import search_capabilities

    results = [item.model_dump(mode="json") for item in search_capabilities(query, max_results=max_results, kind=kind)]
    return {"query": query, "results": results, "count": len(results), "kind": kind}


@router.get("/capabilities/select")
def select_agentic_capability(capability_id: str = Query(..., min_length=1, max_length=200)) -> dict[str, Any]:
    from orchestrator.agentic.capability_search import select_capability

    envelope = select_capability(capability_id)
    if envelope is None:
        raise HTTPException(status_code=404, detail=f"Capability {capability_id} not found")
    return {"capability": envelope}


@router.get("/cockpit")
def get_agentic_cockpit(limit: int = Query(50, ge=1, le=200)) -> dict[str, Any]:
    cfg = get_settings().agentic_runtime
    store = get_agentic_store()
    store.expire_resource_leases()
    pending_approvals = store.list_approvals(status=ApprovalStatus.PENDING.value, limit=limit)
    waiting_tasks = store.list_tasks(status=TaskStatus.WAITING_APPROVAL.value, limit=limit)
    queued_tasks = store.list_tasks(status=TaskStatus.QUEUED.value, limit=limit)
    recovering_tasks = store.list_tasks(status=TaskStatus.RECOVERING.value, limit=limit)
    proposed_improvements = store.list_improvement_proposals(status="proposed", limit=limit)
    waiting_improvements = store.list_improvement_proposals(status="waiting_approval", limit=limit)
    active_actuations = store.list_actuations(status="active", limit=limit)
    applied_actuations = store.list_actuations(status="applied", limit=limit)
    recent_actuations = store.list_actuations(limit=limit)
    escalation_events = store.list_events(event_type="escalation.route_planned", limit=limit)
    runtime_flags = store.list_runtime_flags()
    active_preapprovals = store.list_preapproval_windows(status="active", limit=limit)
    resource_leases = store.list_resource_leases(limit=limit)
    consensus = store.list_recent_agent_messages(kind="consensus", limit=limit)
    memories = store.list_agent_memory(include_expired=False, limit=limit)
    readiness = evaluate_autonomous_readiness(store=store, limit=limit)
    reflection = build_agentic_reflection(store, limit=limit)
    proposals = [*waiting_improvements, *proposed_improvements]
    actuations = [*active_actuations, *applied_actuations]
    return {
        "now": time.time(),
        "mode": {
            "enabled": cfg.enabled,
            "default_mode": cfg.default_mode,
            "autonomous_safe_enabled": cfg.autonomous_safe_enabled,
            "event_loop_enabled": cfg.event_loop_enabled,
            "policy_mode": cfg.policy_mode,
            "safe_actions_only": True,
            "preapproval_windows_enabled": cfg.preapproval_windows_enabled,
        },
        "runner": get_runner_status(),
        "event_loop": get_event_loop_status(),
        "actuator": get_actuator_status(),
        "counts": {
            "pending_approvals": len(pending_approvals),
            "waiting_approval_tasks": len(waiting_tasks),
            "queued_tasks": store.count_tasks(status=TaskStatus.QUEUED.value),
            "recovering_tasks": store.count_tasks(status=TaskStatus.RECOVERING.value),
            "proposed_improvements": len(proposed_improvements),
            "waiting_improvements": len(waiting_improvements),
            "active_actuations": len(active_actuations),
            "applied_actuations": len(applied_actuations),
            "runtime_flags": len(runtime_flags),
            "active_preapproval_windows": len(active_preapprovals),
            "escalations": len(escalation_events),
            "resource_leases": len(resource_leases),
            "consensus": len(consensus),
            "memories": len(memories),
            "readiness_gaps": len(readiness["gaps"]),
        },
        "approvals": pending_approvals,
        "tasks": {
            "waiting_approval": waiting_tasks,
            "queued": queued_tasks,
            "recovering": recovering_tasks,
            "active_ids": store.active_task_ids(),
        },
        "improvements": {
            "waiting_approval": waiting_improvements,
            "proposed": proposed_improvements,
        },
        "actuations": {
            "active": active_actuations,
            "applied": applied_actuations,
            "recent": recent_actuations,
        },
        "impact": [
            {
                "actuation_id": item.get("id"),
                "proposal_id": item.get("proposal_id"),
                "status": item.get("status"),
                "impact": item.get("impact") or {},
                "rollback_reason": item.get("rollback_reason"),
                "expires_at": item.get("expires_at"),
            }
            for item in recent_actuations
        ],
        "escalations": escalation_events,
        "leases": resource_leases,
        "consensus": consensus,
        "memory": {"recent": memories},
        "runtime_flags": runtime_flags,
        "preapproval_windows": {
            "active": active_preapprovals,
            "allowed_actions": sorted(_preapproval_allowed_actions(cfg)),
        },
        "readiness": readiness,
        "reflection": reflection,
        "gaps": readiness["gaps"],
        "coverage": {"required_fields": cockpit_coverage_fields()},
        "docker": docker_shield_summary(),
        "resilience": local_resilience_summary(),
        "attention": _collect_attention_items(
            store,
            pending_approvals=pending_approvals,
            waiting_tasks=waiting_tasks,
            proposals=proposals,
            actuations=actuations,
            escalation_events=escalation_events,
            runtime_flags=runtime_flags,
            preapproval_windows=active_preapprovals,
        ),
    }


@router.get("/reflection")
def get_agentic_reflection(limit: int = Query(100, ge=1, le=500)) -> dict[str, Any]:
    return build_agentic_reflection(get_agentic_store(), limit=limit)


@router.get("/readiness")
def get_agentic_readiness(limit: int = Query(100, ge=1, le=500)) -> dict[str, Any]:
    return evaluate_autonomous_readiness(store=get_agentic_store(), limit=limit)


@router.get("/evals/autonomous-total")
def get_agentic_autonomous_total_evals(limit: int = Query(100, ge=1, le=500)) -> dict[str, Any]:
    readiness = evaluate_autonomous_readiness(store=get_agentic_store(), limit=limit)
    return {
        "suite": "autonomous_total",
        "read_only": True,
        "generated_at": readiness["generated_at"],
        "passed": readiness["ready_for_opt_in"],
        "checks": readiness["checks"],
        "gaps": readiness["gaps"],
    }


@router.get("/cockpit/docker")
def get_agentic_docker_cockpit() -> dict[str, Any]:
    return docker_shield_summary()


@router.get("/cockpit/resilience")
def get_agentic_resilience_cockpit() -> dict[str, Any]:
    return local_resilience_summary()


@router.get("/evidence")
def get_agentic_evidence(
    task_id: str | None = None,
    approval_id: str | None = None,
    preapproval_window_id: str | None = None,
    proposal_id: str | None = None,
    actuation_id: str | None = None,
    limit: int = Query(200, ge=1, le=1000),
) -> dict[str, Any]:
    if not any([task_id, approval_id, preapproval_window_id, proposal_id, actuation_id]):
        raise HTTPException(status_code=400, detail="Provide task_id, approval_id, preapproval_window_id, proposal_id or actuation_id")
    store = get_agentic_store()
    task_ids: set[str] = set()
    approvals: dict[str, Any] = {}
    preapprovals: dict[str, Any] = {}
    proposals: dict[str, Any] = {}
    actuations: dict[str, Any] = {}

    if task_id:
        task = store.get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        task_ids.add(task_id)

    if approval_id:
        approval = store.get_approval(approval_id)
        if approval is None:
            raise HTTPException(status_code=404, detail="Approval not found")
        approvals[approval_id] = approval
        if approval.get("task_id"):
            task_ids.add(str(approval["task_id"]))

    if preapproval_window_id:
        preapproval = store.get_preapproval_window(preapproval_window_id)
        if preapproval is None:
            raise HTTPException(status_code=404, detail="Preapproval window not found")
        preapprovals[preapproval_window_id] = preapproval
        if preapproval.get("task_id"):
            task_ids.add(str(preapproval["task_id"]))

    if proposal_id:
        proposal = store.get_improvement_proposal(proposal_id)
        if proposal is None:
            raise HTTPException(status_code=404, detail="Improvement proposal not found")
        proposals[proposal_id] = proposal
        if proposal.get("task_id"):
            task_ids.add(str(proposal["task_id"]))
        if proposal.get("approval_id") and proposal.get("approval_id") not in approvals:
            approval = store.get_approval(str(proposal["approval_id"]))
            if approval is not None:
                approvals[str(proposal["approval_id"])] = approval

    if actuation_id:
        actuation = store.get_actuation(actuation_id)
        if actuation is None:
            raise HTTPException(status_code=404, detail="Actuation not found")
        actuations[actuation_id] = actuation
        if actuation.get("task_id"):
            task_ids.add(str(actuation["task_id"]))
        linked_proposal_id = actuation.get("proposal_id")
        if linked_proposal_id and linked_proposal_id not in proposals:
            proposal = store.get_improvement_proposal(str(linked_proposal_id))
            if proposal is not None:
                proposals[str(linked_proposal_id)] = proposal
                if proposal.get("task_id"):
                    task_ids.add(str(proposal["task_id"]))
                if proposal.get("approval_id") and proposal.get("approval_id") not in approvals:
                    approval = store.get_approval(str(proposal["approval_id"]))
                    if approval is not None:
                        approvals[str(proposal["approval_id"])] = approval

    traces = {tid: store.trace(tid) for tid in sorted(task_ids)}
    explanations = {tid: store.explain(tid) for tid in sorted(task_ids)}
    decision_ids = sorted(
        {
            str(decision.get("id"))
            for trace in traces.values()
            if trace
            for decision in trace.get("decisions", [])
            if decision.get("id")
        }
    )
    raw_output_ids = sorted(
        {
            str(raw.get("id"))
            for trace in traces.values()
            if trace
            for raw in trace.get("raw_outputs", [])
            if raw.get("id")
        }
    )
    events = _related_events(
        store,
        task_ids=task_ids,
        approval_id=approval_id,
        preapproval_window_id=preapproval_window_id,
        proposal_id=proposal_id,
        actuation_id=actuation_id,
        limit=limit,
    )
    return {
        "read_only": True,
        "generated_at": time.time(),
        "query": {
            "task_id": task_id,
            "approval_id": approval_id,
            "preapproval_window_id": preapproval_window_id,
            "proposal_id": proposal_id,
            "actuation_id": actuation_id,
        },
        "refs": {
            "task_ids": sorted(task_ids),
            "approval_ids": sorted(approvals),
            "preapproval_window_ids": sorted(preapprovals),
            "proposal_ids": sorted(proposals),
            "actuation_ids": sorted(actuations),
            "decision_ids": decision_ids,
            "raw_output_ids": raw_output_ids,
        },
        "immutability": {
            "approval_payloads": [
                {
                    "approval_id": approval.get("id"),
                    "action": approval.get("action"),
                    "payload_hash": approval.get("payload_hash"),
                    "payload_preview": approval.get("payload_preview"),
                    "dry_run_result": approval.get("dry_run_result"),
                    "status": approval.get("status"),
                    "expires_at": approval.get("expires_at"),
                }
                for approval in approvals.values()
            ],
            "payload_editing_allowed": False,
        },
        "approvals": list(approvals.values()),
        "preapproval_windows": list(preapprovals.values()),
        "improvement_proposals": list(proposals.values()),
        "actuations": list(actuations.values()),
        "traces": traces,
        "explanations": explanations,
        "events": events,
        "links": _links(
            task_id=task_id or (sorted(task_ids)[0] if len(task_ids) == 1 else None),
            approval_id=approval_id,
            preapproval_window_id=preapproval_window_id,
            proposal_id=proposal_id,
            actuation_id=actuation_id,
        ),
    }


@router.get("/evidence/docker-shield")
def get_agentic_docker_shield_evidence() -> dict[str, Any]:
    evidence = docker_shield_evidence()
    if evidence["summary"]["status"] == "missing":
        raise HTTPException(
            status_code=404,
            detail={
                "message": "Docker shield report not found. Run make docker-shield-report.",
                "source": evidence["source"],
            },
        )
    return evidence


@router.get("/evidence/local-resilience/{report_name}")
def get_agentic_local_resilience_evidence(report_name: str) -> dict[str, Any]:
    if report_name not in LOCAL_RESILIENCE_REPORTS:
        raise HTTPException(status_code=404, detail="Unknown local resilience report")
    evidence = local_resilience_evidence(report_name)
    if evidence["summary"]["status"] == "missing":
        refresh_command = LOCAL_RESILIENCE_REPORTS[report_name]["refresh_command"]
        raise HTTPException(
            status_code=404,
            detail={
                "message": f"Local resilience report {report_name!r} not found. Run {refresh_command}.",
                "source": evidence["source"],
            },
        )
    return evidence


@router.post("/resilience/chaos/proposals/import")
def import_agentic_chaos_proposals(include_pass: bool = Query(False)) -> dict[str, Any]:
    try:
        return import_chaos_proposals(include_pass=include_pass, imported_by="agentic.api")
    except FileNotFoundError as exc:
        log.warning("Chaos proposal import failed: report missing: %s", exc)
        raise HTTPException(
            status_code=404,
            detail={"message": "Chaos report not found. Run make chaos-local."},
        ) from exc
    except (json.JSONDecodeError, ValueError) as exc:
        log.warning("Chaos proposal import failed: invalid report: %s", exc)
        raise HTTPException(
            status_code=422,
            detail={"message": "Chaos report is invalid. Regenerate it with make chaos-local."},
        ) from exc


@router.post("/storage/restore-tests")
def create_agentic_restore_test(req: RestoreTestRequest) -> dict[str, Any]:
    try:
        return _request_storage_restore_test(req)
    except HTTPException:
        raise
    except Exception as exc:
        log.warning("Restore test rejected: %s", exc)
        raise HTTPException(status_code=409, detail="Restore test is not allowed") from exc


@router.post("/resilience/drills/{drill_name}/request")
def request_agentic_resilience_drill(drill_name: str, req: DrillRequest | None = None) -> dict[str, Any]:
    request = req or DrillRequest()
    try:
        return request_resilience_drill(
            drill_name,
            requested_by=request.requested_by,
            ttl_seconds=request.ttl_seconds,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Unknown resilience drill") from exc
    except DrillBlockedError as exc:
        log.warning("Resilience drill request blocked: %s", exc)
        raise HTTPException(status_code=409, detail="Resilience drill is blocked") from exc


@router.post("/resilience/drills/{drill_id}/approve")
def approve_agentic_resilience_drill(drill_id: str, req: ApprovalApproveRequest | None = None) -> dict[str, Any]:
    approved_by = req.approved_by if req is not None else "user"
    try:
        return approve_resilience_drill(drill_id, approved_by=approved_by)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Resilience drill not found") from exc
    except (PermissionError, NotImplementedError, DrillBlockedError, ValueError) as exc:
        log.warning("Resilience drill approval rejected: %s", exc)
        raise HTTPException(status_code=409, detail="Resilience drill approval rejected") from exc


@router.post("/resilience/drills/{drill_id}/rollback")
def rollback_agentic_resilience_drill(drill_id: str, req: RollbackActuationRequest | None = None) -> dict[str, Any]:
    reason = req.reason if req is not None else "resilience_drill_rollback"
    try:
        return rollback_resilience_drill(drill_id, reason=reason)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Resilience drill not found") from exc
    except (DrillBlockedError, NotImplementedError, ValueError) as exc:
        log.warning("Resilience drill rollback rejected: %s", exc)
        raise HTTPException(status_code=409, detail="Resilience drill rollback rejected") from exc


@router.post("/tasks")
def create_agentic_task(req: CreateTaskRequest) -> dict[str, Any]:
    cfg = get_settings().agentic_runtime
    store = get_agentic_store()
    trace_id = uuid.uuid4().hex[:16]
    task = store.create_task(
        goal=req.goal,
        mode=req.mode or cfg.default_mode,
        source="agentic.api",
        session_id=req.session_id,
        trace_id=trace_id,
        priority=req.priority,
        budget=req.budget,
        metadata={**req.metadata, "runner": "not_started"},
        status=TaskStatus.QUEUED.value,
    )
    return task.to_dict()


@router.get("/tasks")
def list_agentic_tasks(
    status: str | None = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    return {"tasks": get_agentic_store().list_tasks(status=status, limit=limit, offset=offset)}


@router.get("/live/snapshot")
def get_agentic_live_snapshot(
    status: str = Query("running,recent", max_length=100),
    limit: int = Query(200, ge=1, le=500),
    recent_seconds: int = Query(900, ge=0, le=86400),
) -> dict[str, Any]:
    store = get_agentic_store()
    store.expire_resource_leases()
    return build_live_snapshot(
        store.list_tasks(limit=limit),
        status_filter=status,
        limit=_query_int(limit, default=200),
        recent_seconds=_query_int(recent_seconds, default=900),
    )


@router.get("/tasks/{task_id}")
def get_agentic_task(task_id: str) -> dict[str, Any]:
    task = get_agentic_store().get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return task.to_dict()


@router.get("/tasks/{task_id}/trace")
def get_agentic_trace(task_id: str) -> dict[str, Any]:
    store = get_agentic_store()
    store.expire_resource_leases()
    trace = store.trace(task_id)
    if trace is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return trace


@router.get("/tasks/{task_id}/timeline")
def get_agentic_task_timeline(task_id: str) -> dict[str, Any]:
    store = get_agentic_store()
    store.expire_resource_leases()
    trace = store.trace(task_id)
    if trace is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return build_task_timeline(
        trace,
        command_runs=store.list_command_runs(task_id=task_id, limit=500),
    )


@router.get("/tasks/{task_id}/diffs/{file_path:path}")
def get_agentic_task_diff(task_id: str, file_path: str) -> dict[str, Any]:
    store = get_agentic_store()
    store.expire_resource_leases()
    trace = store.trace(task_id)
    if trace is None:
        raise HTTPException(status_code=404, detail="Task not found")
    timeline = build_task_timeline(
        trace,
        command_runs=store.list_command_runs(task_id=task_id, limit=500),
    )
    for item in timeline.get("file_activity") or []:
        if str(item.get("path") or "") == file_path:
            return {
                "task_id": task_id,
                "path": file_path,
                "status": item.get("status"),
                "additions": item.get("additions"),
                "deletions": item.get("deletions"),
                "patch": item.get("patch") or "",
                "patch_ref": item.get("patch_ref"),
                "binary": bool(item.get("binary")),
            }
    raise HTTPException(status_code=404, detail="Diff not found")


@router.get("/tasks/{task_id}/events")
def get_agentic_task_events(
    task_id: str,
    cursor: int = Query(0, ge=0),
    limit: int = Query(200, ge=1, le=500),
) -> dict[str, Any]:
    store = get_agentic_store()
    store.expire_resource_leases()
    trace = store.trace(task_id)
    if trace is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return build_task_event_feed(
        trace,
        command_runs=store.list_command_runs(task_id=task_id, limit=500),
        cursor=_query_int(cursor, default=0),
        limit=_query_int(limit, default=200),
    )


@router.get("/tasks/{task_id}/events/stream")
def stream_agentic_task_events(
    task_id: str,
    cursor: int = Query(0, ge=0),
    poll_seconds: float = Query(1.0, ge=0.2, le=5.0),
) -> StreamingResponse:
    store = get_agentic_store()
    if store.get_task(task_id) is None:
        raise HTTPException(status_code=404, detail="Task not found")

    cursor_value = _query_int(cursor, default=0)
    poll_value = _query_float(poll_seconds, default=1.0)

    def event_stream():
        current_cursor = max(0, cursor_value)
        while True:
            trace = store.trace(task_id)
            if trace is None:
                yield sse_encode_control("error", {"detail": "Task not found", "task_id": task_id})
                return
            feed = build_task_event_feed(
                trace,
                command_runs=store.list_command_runs(task_id=task_id, limit=500),
                cursor=current_cursor,
                limit=500,
            )
            for event in feed["events"]:
                current_cursor = int(event.get("seq") or current_cursor)
                yield sse_encode_event(event)
            task = feed.get("task") or {}
            if task.get("terminal"):
                yield sse_encode_control("done", {"task_id": task_id, "cursor": current_cursor, "status": task.get("status")})
                return
            yield sse_encode_control("heartbeat", {"task_id": task_id, "cursor": current_cursor})
            time.sleep(poll_value)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.get("/tasks/{task_id}/explain")
def explain_agentic_task(task_id: str) -> dict[str, Any]:
    store = get_agentic_store()
    store.expire_resource_leases()
    explanation = store.explain(task_id)
    if explanation is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return explanation


@router.get("/tasks/{task_id}/state")
def get_agentic_task_state(task_id: str) -> dict[str, Any]:
    store = get_agentic_store()
    if store.get_task(task_id) is None:
        raise HTTPException(status_code=404, detail="Task not found")
    snapshot = store.current_agent_state(task_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="Task state not found")
    return snapshot


@router.get("/tasks/{task_id}/decisions")
def list_agentic_task_decisions(
    task_id: str,
    limit: int = Query(100, ge=1, le=500),
) -> dict[str, Any]:
    store = get_agentic_store()
    if store.get_task(task_id) is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"decisions": store.list_agent_decisions(task_id=task_id, limit=limit)}


@router.get("/tasks/{task_id}/rounds")
def list_agentic_task_rounds(
    task_id: str,
    limit: int = Query(100, ge=1, le=500),
) -> dict[str, Any]:
    store = get_agentic_store()
    if store.get_task(task_id) is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"rounds": store.list_parallel_rounds(task_id=task_id, limit=limit)}


@router.get("/tasks/{task_id}/messages")
def list_agentic_task_messages(
    task_id: str,
    kind: str | None = None,
    limit: int = Query(200, ge=1, le=1000),
) -> dict[str, Any]:
    store = get_agentic_store()
    if store.get_task(task_id) is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"messages": store.list_agent_messages(task_id=task_id, kind=kind, limit=limit)}


@router.get("/tasks/{task_id}/consensus")
def list_agentic_task_consensus(
    task_id: str,
    limit: int = Query(100, ge=1, le=500),
) -> dict[str, Any]:
    store = get_agentic_store()
    if store.get_task(task_id) is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"consensus": store.list_agent_messages(task_id=task_id, kind="consensus", limit=limit)}


@router.get("/tasks/{task_id}/deliberation")
def get_agentic_task_deliberation(
    task_id: str,
    limit: int = Query(200, ge=1, le=1000),
) -> dict[str, Any]:
    store = get_agentic_store()
    if store.get_task(task_id) is None:
        raise HTTPException(status_code=404, detail="Task not found")
    messages = store.list_agent_messages(task_id=task_id, limit=limit)
    trace = store.trace(task_id) or {}
    events = [
        event
        for event in trace.get("events", [])
        if str(event.get("event_type") or "").startswith("agent.deliberation.")
    ]
    return {
        "questions": [message for message in messages if message.get("message_type") == "question"],
        "answers": [message for message in messages if message.get("message_type") == "answer"],
        "critiques": [message for message in messages if message.get("message_type") == "critique"],
        "validations": [message for message in messages if message.get("message_type") == "validation"],
        "consensus": [message for message in messages if message.get("message_type") == "consensus"],
        "events": events,
    }


@router.get("/tasks/{task_id}/memory")
def list_agentic_task_memory(
    task_id: str,
    kind: str | None = None,
    include_expired: bool = False,
    limit: int = Query(100, ge=1, le=500),
) -> dict[str, Any]:
    store = get_agentic_store()
    if store.get_task(task_id) is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"memories": store.list_agent_memory(task_id=task_id, kind=kind, include_expired=include_expired, limit=limit)}


@router.get("/tasks/{task_id}/replay")
def replay_agentic_task_state(task_id: str) -> dict[str, Any]:
    store = get_agentic_store()
    replay = store.replay_agent_state(task_id)
    if replay is None:
        raise HTTPException(status_code=404, detail="Task not found")
    latest = store.latest_agent_state_snapshot(task_id)
    return {
        **replay,
        "latest_snapshot_hash": latest.get("state_hash") if latest else None,
        "matches_latest_snapshot": bool(latest and latest.get("state_hash") == replay.get("state_hash")),
    }


@router.post("/tasks/{task_id}/cancel")
def cancel_agentic_task(task_id: str) -> dict[str, Any]:
    if not cancel_task(task_id):
        raise HTTPException(status_code=404, detail="Task not found")
    task = get_agentic_store().get_task(task_id)
    return task.to_dict() if task is not None else {"id": task_id, "status": TaskStatus.CANCELLED.value}


@router.post("/tasks/{task_id}/retry")
def retry_agentic_task(task_id: str) -> dict[str, Any]:
    task = retry_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@router.post("/tasks/{task_id}/resume")
def resume_agentic_task(task_id: str, req: ResumeTaskRequest | None = None) -> dict[str, Any]:
    store = get_agentic_store()
    task = store.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.status not in {TaskStatus.WAITING_APPROVAL.value, TaskStatus.RECOVERING.value}:
        raise HTTPException(status_code=409, detail=f"Task is not resumable from status {task.status}")
    pending = store.approvals_for_task(task_id, status=ApprovalStatus.PENDING.value)
    if pending:
        raise HTTPException(status_code=409, detail="Task still has pending approvals")
    terminal_approvals = [
        approval
        for approval in store.approvals_for_task(task_id)
        if approval["status"] in {ApprovalStatus.REJECTED.value, ApprovalStatus.EXPIRED.value}
    ]
    if terminal_approvals:
        raise HTTPException(status_code=409, detail="Task has rejected or expired approvals")
    if not resume_task(task_id, reason=req.reason if req is not None else "manual_resume"):
        raise HTTPException(status_code=409, detail="Task could not be resumed")
    resumed = store.get_task(task_id)
    return resumed.to_dict() if resumed is not None else {"id": task_id, "status": TaskStatus.QUEUED.value}


@router.get("/policies")
def get_agentic_policies() -> dict[str, Any]:
    return get_policy_engine().list_matrix()


@router.post("/policies/check")
def check_agentic_policy(req: PolicyCheckRequest) -> dict[str, Any]:
    return asdict(check_policy(req.action, req.payload))


@router.get("/preapproval-windows")
def list_agentic_preapproval_windows(
    status: str | None = None,
    include_expired: bool = False,
    limit: int = Query(100, ge=1, le=500),
) -> dict[str, Any]:
    cfg = get_settings().agentic_runtime
    store = get_agentic_store()
    return {
        "enabled": cfg.preapproval_windows_enabled,
        "allowed_actions": sorted(_preapproval_allowed_actions(cfg)),
        "windows": store.list_preapproval_windows(status=status, include_expired=include_expired, limit=limit),
        "now": time.time(),
    }


@router.post("/preapproval-windows")
def create_agentic_preapproval_window(req: CreatePreapprovalWindowRequest) -> dict[str, Any]:
    action, ttl, uses, metadata = _validated_preapproval_window_request(req)
    store = get_agentic_store()
    if req.task_id and store.get_task(req.task_id) is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return store.create_preapproval_window(
        task_id=req.task_id,
        action=action,
        scope=req.scope,
        ttl_seconds=ttl,
        max_uses=uses,
        reason=req.reason,
        created_by=req.created_by,
        metadata=metadata,
    )


@router.get("/preapproval-windows/{window_id}")
def get_agentic_preapproval_window(window_id: str) -> dict[str, Any]:
    window = get_agentic_store().get_preapproval_window(window_id)
    if window is None:
        raise HTTPException(status_code=404, detail="Preapproval window not found")
    return window


@router.post("/preapproval-windows/{window_id}/revoke")
def revoke_agentic_preapproval_window(
    window_id: str,
    req: RevokePreapprovalWindowRequest | None = None,
) -> dict[str, Any]:
    window = get_agentic_store().revoke_preapproval_window(
        window_id,
        reason=req.reason if req is not None else "manual_revoke",
    )
    if window is None:
        raise HTTPException(status_code=404, detail="Preapproval window not found")
    return window


@router.get("/approvals")
def list_agentic_approvals(
    status: str | None = None,
    limit: int = Query(100, ge=1, le=500),
) -> dict[str, Any]:
    return {"approvals": get_agentic_store().list_approvals(status=status, limit=limit)}


@router.get("/approvals/{approval_id}")
def get_agentic_approval(approval_id: str) -> dict[str, Any]:
    approval = get_agentic_store().get_approval(approval_id)
    if approval is None:
        raise HTTPException(status_code=404, detail="Approval not found")
    return approval


@router.post("/approvals/{approval_id}/approve")
def approve_agentic_action(approval_id: str, req: ApprovalApproveRequest | None = None) -> dict[str, Any]:
    approved_by = req.approved_by if req is not None else "user"
    store = get_agentic_store()
    approval = store.approve(approval_id, approved_by=approved_by)
    if approval is None:
        raise HTTPException(status_code=404, detail="Approval not found")
    if approval.get("status") == ApprovalStatus.EXPIRED.value:
        raise HTTPException(status_code=409, detail="Approval expired")
    task_id = approval.get("task_id")
    if task_id:
        task = store.get_task(str(task_id))
        if task is not None and task.status == TaskStatus.WAITING_APPROVAL.value:
            pending = store.approvals_for_task(str(task_id), status=ApprovalStatus.PENDING.value)
            if not pending:
                store.resume_task(str(task_id), reason=f"approval_approved:{approval_id}")
    return approval


@router.post("/approvals/{approval_id}/reject")
def reject_agentic_action(approval_id: str, req: ApprovalRejectRequest | None = None) -> dict[str, Any]:
    store = get_agentic_store()
    approval = store.reject(approval_id, reason=req.reason if req is not None else "")
    if approval is None:
        raise HTTPException(status_code=404, detail="Approval not found")
    task_id = approval.get("task_id")
    if task_id:
        task = store.get_task(str(task_id))
        if task is not None and task.status == TaskStatus.WAITING_APPROVAL.value:
            store.update_task(
                str(task_id),
                status=TaskStatus.FAILED.value,
                error={"type": "ApprovalRejected", "message": req.reason if req is not None else ""},
            )
    return approval


@router.get("/ai-events")
def list_ai_local_events(
    task_id: str | None = None,
    event_type: str | None = None,
    producer: str | None = None,
    since: float | None = None,
    limit: int = Query(500, ge=1, le=1000),
) -> dict[str, Any]:
    return {
        "events": get_agentic_store().list_ai_local_events(
            task_id=task_id,
            event_type=event_type,
            producer=producer,
            since=since,
            limit=limit,
        ),
        "now": time.time(),
    }


@router.post("/ai-events")
def publish_ai_local_event(req: PublishAiLocalEventRequest) -> dict[str, Any]:
    from orchestrator.agentic.contracts import AiLocalEvent

    store = get_agentic_store()
    trace_id = req.trace_id
    if req.task_id:
        task = store.get_task(req.task_id)
        if task is not None and not trace_id:
            trace_id = task.trace_id
    return store.record_ai_local_event(
        AiLocalEvent(
            event_id=f"evt_{uuid.uuid4().hex}",
            producer=req.producer,
            type=req.type,
            severity=req.severity,
            payload=req.payload,
            evidence_ref=req.evidence_ref,
            task_id=req.task_id,
            trace_id=trace_id,
            created_at=time.time(),
            metadata=req.metadata,
        ),
        actor=req.producer,
    )


@router.post("/memory")
def record_agentic_memory(req: RecordAgenticMemoryRequest) -> dict[str, Any]:
    from orchestrator.agentic.contracts import AgenticMemory

    store = get_agentic_store()
    trace_id = req.trace_id
    if req.task_id:
        task = store.get_task(req.task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        trace_id = trace_id or task.trace_id
    return store.record_agent_memory(
        AgenticMemory(
            memory_id=f"mem_{uuid.uuid4().hex}",
            task_id=req.task_id,
            trace_id=trace_id,
            kind=req.kind,
            owner=req.owner,
            source=req.source,
            content=req.content,
            evidence_refs=req.evidence_refs,
            expires_at=req.expires_at,
            sensitivity=req.sensitivity,
            redaction_status=req.redaction_status,
            storage_artifact_ref=req.storage_artifact_ref,
            semantic_ref=req.semantic_ref,
            metadata=req.metadata,
        ),
        actor=req.source,
    )


@router.post("/memory/retrieve")
def retrieve_agentic_memory(req: RetrieveAgenticMemoryRequest) -> dict[str, Any]:
    from orchestrator.agentic.contracts import AgenticMemoryQuery

    store = get_agentic_store()
    trace_id = req.trace_id
    if req.task_id:
        task = store.get_task(req.task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        trace_id = trace_id or task.trace_id
    return store.retrieve_agent_memory(
        AgenticMemoryQuery(
            query_id=f"memq_{uuid.uuid4().hex}",
            task_id=req.task_id,
            trace_id=trace_id,
            query=req.query,
            kinds=req.kinds,
            sources=req.sources,
            evidence_refs=req.evidence_refs,
            metadata_filter=req.metadata_filter,
            include_expired=req.include_expired,
            limit=req.limit,
            min_score=req.min_score,
        ),
        actor="agentic.api",
    )


@router.get("/events")
def list_agentic_events(
    event_type: str | None = None,
    limit: int = Query(100, ge=1, le=1000),
) -> dict[str, Any]:
    return {"events": get_agentic_store().list_events(event_type=event_type, limit=limit), "now": time.time()}


@router.get("/escalation-routes")
def list_agentic_escalation_routes(
    domain: str | None = None,
    limit: int = Query(100, ge=1, le=500),
) -> dict[str, Any]:
    events = get_agentic_store().list_events(event_type="escalation.route_planned", limit=limit)
    routes = []
    for event in events:
        route = (event.get("payload") or {}).get("route") or {}
        if domain and route.get("domain") != domain:
            continue
        routes.append({
            "event_id": event.get("id"),
            "timestamp": event.get("timestamp"),
            "task_id": event.get("task_id"),
            "actuation_id": (event.get("payload") or {}).get("actuation_id"),
            "proposal_id": (event.get("payload") or {}).get("proposal_id"),
            "route": route,
        })
    return {"routes": routes, "now": time.time()}


@router.get("/runtime-flags")
def list_agentic_runtime_flags() -> dict[str, Any]:
    return {"flags": get_agentic_store().list_runtime_flags(), "now": time.time()}


@router.get("/commands/status")
def get_agentic_command_status() -> dict[str, Any]:
    return CommandToolService().status()


@router.get("/commands/registry")
def list_agentic_command_registry() -> dict[str, Any]:
    from orchestrator.capabilities.command_registry import command_registry_entries

    commands = [entry.to_public_dict() for entry in command_registry_entries()]
    return {"commands": commands, "count": len(commands)}


@router.get("/commands/select")
def select_agentic_command(name: str = Query(..., min_length=1, max_length=80)) -> dict[str, Any]:
    from orchestrator.capabilities.command_registry import command_registry_entry

    entry = command_registry_entry(name)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Command {name} not found")
    return {"command": entry.to_public_dict()}


@router.post("/commands/classify")
def classify_agentic_command(req: CommandClassifyRequest) -> dict[str, Any]:
    return CommandToolService().classify(req.command, context_profile=req.context_profile)


@router.post("/commands/sessions")
def create_agentic_command_session(req: CreateCommandSessionRequest) -> dict[str, Any]:
    try:
        return CommandToolService().create_session(
            context_profile=req.context_profile,
            cwd=req.cwd,
            task_id=req.task_id,
            trace_id=req.trace_id,
            metadata=req.metadata,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/commands/sessions")
def list_agentic_command_sessions(
    status: str | None = None,
    limit: int = Query(100, ge=1, le=500),
) -> dict[str, Any]:
    return {"sessions": get_agentic_store().list_command_sessions(status=status, limit=limit), "now": time.time()}


@router.get("/commands/sessions/{session_id}")
def get_agentic_command_session(session_id: str) -> dict[str, Any]:
    store = get_agentic_store()
    session = store.get_command_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Command session not found")
    return {
        **session,
        "runs": store.list_command_runs(session_id=session_id, limit=500),
    }


@router.post("/commands/sessions/{session_id}/run")
def run_agentic_command(session_id: str, req: RunCommandRequest) -> dict[str, Any]:
    try:
        return CommandToolService().run_command(
            session_id,
            command=req.command,
            cwd=req.cwd,
            task_id=req.task_id,
            trace_id=req.trace_id,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.get("/commands/runs")
def list_agentic_command_runs(
    session_id: str | None = None,
    task_id: str | None = None,
    limit: int = Query(100, ge=1, le=500),
) -> dict[str, Any]:
    return {
        "runs": get_agentic_store().list_command_runs(session_id=session_id, task_id=task_id, limit=limit),
        "now": time.time(),
    }


@router.post("/commands/sessions/{session_id}/close")
def close_agentic_command_session(
    session_id: str,
    req: CloseCommandSessionRequest | None = None,
) -> dict[str, Any]:
    session = CommandToolService().close_session(
        session_id,
        reason=req.reason if req is not None else "manual_close",
    )
    if session is None:
        raise HTTPException(status_code=404, detail="Command session not found")
    return session


@router.get("/improvements")
def list_agentic_improvements(
    status: str | None = None,
    limit: int = Query(100, ge=1, le=500),
) -> dict[str, Any]:
    return {"improvements": get_agentic_store().list_improvement_proposals(status=status, limit=limit)}


@router.get("/improvements/{proposal_id}")
def get_agentic_improvement(proposal_id: str) -> dict[str, Any]:
    proposal = get_agentic_store().get_improvement_proposal(proposal_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail="Improvement proposal not found")
    return proposal


@router.post("/improvements/{proposal_id}/request-approval")
def request_agentic_improvement_approval(proposal_id: str) -> dict[str, Any]:
    try:
        approval = request_improvement_approval(proposal_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if approval is None:
        raise HTTPException(status_code=404, detail="Improvement proposal not found")
    return approval


@router.post("/improvements/{proposal_id}/apply")
def apply_agentic_improvement(proposal_id: str) -> dict[str, Any]:
    try:
        proposal = apply_improvement(proposal_id)
    except PermissionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except NotImplementedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if proposal is None:
        raise HTTPException(status_code=404, detail="Improvement proposal not found")
    return proposal


@router.post("/improvements/{proposal_id}/reject")
def reject_agentic_improvement(proposal_id: str, req: ApprovalRejectRequest | None = None) -> dict[str, Any]:
    proposal = reject_improvement(proposal_id, reason=req.reason if req is not None else "")
    if proposal is None:
        raise HTTPException(status_code=404, detail="Improvement proposal not found")
    return proposal


@router.get("/actuations")
def list_agentic_actuations(
    status: str | None = None,
    limit: int = Query(100, ge=1, le=500),
) -> dict[str, Any]:
    return {"actuations": get_agentic_store().list_actuations(status=status, limit=limit), "now": time.time()}


@router.get("/actuations/{actuation_id}")
def get_agentic_actuation(actuation_id: str) -> dict[str, Any]:
    actuation = get_agentic_store().get_actuation(actuation_id)
    if actuation is None:
        raise HTTPException(status_code=404, detail="Actuation not found")
    return actuation


@router.post("/actuations/{actuation_id}/rollback")
def rollback_agentic_actuation(actuation_id: str, req: RollbackActuationRequest | None = None) -> dict[str, Any]:
    try:
        actuation = rollback_actuation(actuation_id, reason=req.reason if req is not None else "manual_rollback")
    except NotImplementedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if actuation is None:
        raise HTTPException(status_code=404, detail="Actuation not found")
    return actuation
