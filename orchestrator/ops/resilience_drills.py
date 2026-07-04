"""Controlled local resilience drills."""

from __future__ import annotations

from typing import Any

from orchestrator.agentic.actuator import rollback_actuation
from orchestrator.agentic.improvements import apply_improvement, request_improvement_approval
from orchestrator.agentic.store import AgenticStore, get_agentic_store


class DrillBlockedError(ValueError):
    """Raised when a drill exists but is intentionally blocked in this phase."""


RUNTIME_DRILLS: dict[str, dict[str, str]] = {
    "qdrant_down": {
        "key": "service_degraded:qdrant",
        "safe_action": "deprioritize_vector_store_and_surface_rag_degraded_state",
        "title": "Runtime drill: Qdrant degraded",
    },
    "rag_down": {
        "key": "service_degraded:rag",
        "safe_action": "skip_rag_dispatch_and_use_non_rag_fallbacks_for_ttl",
        "title": "Runtime drill: RAG degraded",
    },
    "vllm_down": {
        "key": "service_degraded:vllm",
        "safe_action": "route_away_from_vllm_for_ttl",
        "title": "Runtime drill: vLLM degraded",
    },
    "storage_external_missing": {
        "key": "block_heavy_tasks",
        "safe_action": "defer_heavy_tasks_until_storage_recovers",
        "title": "Runtime drill: external storage missing",
    },
    "command_sandbox_crash": {
        "key": "service_degraded:command_sandbox",
        "safe_action": "disable_agentic_command_sessions_for_ttl",
        "title": "Runtime drill: command sandbox degraded",
    },
    "critical_volume_full": {
        "key": "block_heavy_tasks",
        "safe_action": "defer_heavy_tasks_until_volume_pressure_is_reviewed",
        "title": "Runtime drill: critical volume pressure",
    },
    "debug_overlay_active": {
        "key": "route_fallback:debug_overlay",
        "safe_action": "surface_operator_attention_without_mutating_networking",
        "title": "Runtime drill: debug overlay attention",
    },
    "required_secret_missing": {
        "key": "block_heavy_tasks",
        "safe_action": "defer_secret_dependent_work_until_secret_is_restored",
        "title": "Runtime drill: required secret missing",
    },
}

BLOCKED_DOCKER_DRILLS = {"docker_lifecycle", "docker_lifecycle_stop", "container_restart"}


def request_resilience_drill(
    drill_name: str,
    *,
    store: AgenticStore | None = None,
    requested_by: str = "user",
    ttl_seconds: int = 300,
) -> dict[str, Any]:
    if drill_name in BLOCKED_DOCKER_DRILLS:
        raise DrillBlockedError("Docker lifecycle drills require a dedicated high-risk approval phase")
    spec = RUNTIME_DRILLS.get(drill_name)
    if spec is None:
        raise KeyError(drill_name)
    store = store or get_agentic_store()
    payload = {
        "operation": "set_runtime_flag",
        "key": spec["key"],
        "ttl_seconds": ttl_seconds,
        "value": {
            "reason": f"runtime resilience drill: {drill_name}",
            "safe_action": spec["safe_action"],
            "source": "agentic.resilience.drill",
        },
    }
    proposal = store.create_improvement_proposal(
        kind="resilience_drill_runtime_flag",
        title=spec["title"],
        risk_level="medium",
        confidence=0.9,
        score=3,
        payload=payload,
        evidence={
            "drill_name": drill_name,
            "runtime_only": True,
            "no_docker_mutation": True,
            "approval_required": True,
        },
        ttl_seconds=ttl_seconds,
        metadata={
            "source": "agentic.resilience.drill",
            "drill_name": drill_name,
            "runtime_only": True,
            "requested_by": requested_by,
        },
        fingerprint=f"resilience-drill:{drill_name}:{spec['key']}:{spec['safe_action']}",
    )
    approval = request_improvement_approval(str(proposal["id"]), requested_by=requested_by, store=store)
    proposal = store.get_improvement_proposal(str(proposal["id"])) or proposal
    return {
        "drill_id": proposal["id"],
        "status": "waiting_approval",
        "drill_name": drill_name,
        "proposal": proposal,
        "approval": approval,
        "links": {
            "proposal": f"/agentic/improvements/{proposal['id']}",
            "approval": f"/agentic/approvals/{approval['id']}" if approval else "",
            "evidence": f"/agentic/evidence?proposal_id={proposal['id']}",
        },
    }


def approve_resilience_drill(
    drill_id: str,
    *,
    store: AgenticStore | None = None,
    approved_by: str = "user",
) -> dict[str, Any]:
    store = store or get_agentic_store()
    proposal = store.get_improvement_proposal(drill_id)
    if proposal is None:
        raise KeyError(drill_id)
    approval_id = proposal.get("approval_id")
    if not approval_id:
        approval = request_improvement_approval(drill_id, requested_by=approved_by, store=store)
        approval_id = approval.get("id") if approval else None
    if not approval_id:
        raise DrillBlockedError("drill approval could not be created")
    approval = store.approve(str(approval_id), approved_by=approved_by)
    if approval is None:
        raise KeyError(str(approval_id))
    applied = apply_improvement(drill_id, applied_by=approved_by, store=store)
    return {
        "drill_id": drill_id,
        "status": "applied",
        "proposal": applied,
        "approval": approval,
        "runtime_flags": store.list_runtime_flags(),
    }


def rollback_resilience_drill(
    drill_id: str,
    *,
    store: AgenticStore | None = None,
    reason: str = "resilience_drill_rollback",
) -> dict[str, Any]:
    store = store or get_agentic_store()
    proposal = store.get_improvement_proposal(drill_id)
    if proposal is None:
        raise KeyError(drill_id)
    actuation_id = (proposal.get("metadata") or {}).get("apply_result", {}).get("actuation_id")
    if not actuation_id:
        for actuation in store.list_actuations(limit=500):
            if actuation.get("proposal_id") == drill_id:
                actuation_id = actuation.get("id")
                break
    if not actuation_id:
        raise DrillBlockedError("no applied actuation found for drill")
    rolled_back = rollback_actuation(str(actuation_id), reason=reason, store=store)
    return {
        "drill_id": drill_id,
        "status": "rolled_back",
        "actuation": rolled_back,
        "runtime_flags": store.list_runtime_flags(),
    }
