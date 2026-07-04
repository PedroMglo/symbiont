"""Governed self-improvement helpers for the agentic runtime."""

from __future__ import annotations

import time
from typing import Any

from orchestrator.agentic.actuator import AUTO_APPLY_ACTION, validate_runtime_flag_operation
from orchestrator.agentic.models import ApprovalStatus, PolicyDecisionKind
from orchestrator.agentic.policy import check_policy
from orchestrator.agentic.store import AgenticStore, get_agentic_store

APPLY_ACTION = "agentic.improvement.apply"


def request_improvement_approval(
    proposal_id: str,
    *,
    requested_by: str = "user",
    store: AgenticStore | None = None,
) -> dict[str, Any] | None:
    """Create an immutable approval for a proposed improvement payload."""

    store = store or get_agentic_store()
    proposal = store.get_improvement_proposal(proposal_id)
    if proposal is None:
        return None
    if proposal.get("status") == "applied":
        raise ValueError("Improvement proposal is already applied")

    payload = _approval_payload(proposal)
    decision = check_policy(APPLY_ACTION, payload)
    if decision.decision == PolicyDecisionKind.DENY.value:
        raise PermissionError(decision.reason)

    existing_id = proposal.get("approval_id")
    if existing_id:
        existing = store.get_approval(str(existing_id))
        if existing and existing.get("status") in {ApprovalStatus.PENDING.value, ApprovalStatus.APPROVED.value}:
            return existing

    try:
        from orchestrator.config import get_settings

        approval_ttl_seconds = get_settings().agentic_runtime.approval_ttl_seconds
    except Exception:
        approval_ttl_seconds = 3600

    approval = store.create_approval(
        task_id=proposal.get("task_id"),
        action=decision.action,
        risk_level=decision.risk_level,
        payload=payload,
        dry_run_result={
            "would_apply": payload,
            "side_effects": "ttl_runtime_flag_only" if _is_runtime_flag_payload(payload) else "not_supported_in_this_phase",
            "phase_guard": "no destructive or irreversible action is executed",
        },
        ttl_seconds=approval_ttl_seconds,
        metadata={
            "component": "agentic.improvement",
            "proposal_id": proposal_id,
            "requested_by": requested_by,
            "policy_mode": decision.policy_mode,
        },
    )
    store.set_improvement_approval(proposal_id, approval_id=str(approval["id"]))
    return approval


def apply_improvement(
    proposal_id: str,
    *,
    applied_by: str = "user",
    store: AgenticStore | None = None,
) -> dict[str, Any] | None:
    """Apply an approved, reversible improvement.

    This v1 intentionally supports only TTL runtime flags. Anything that would
    write config, restart services, reprocess RAG, mutate storage, commit code,
    or run shell remains blocked even after an approval exists.
    """

    store = store or get_agentic_store()
    proposal = store.get_improvement_proposal(proposal_id)
    if proposal is None:
        return None
    if proposal.get("status") == "applied":
        return proposal

    payload = _approval_payload(proposal)
    approval = _approved_payload_approval(store, proposal=proposal, payload=payload)
    if approval is None:
        raise PermissionError("Approved matching payload is required before applying an improvement")
    if not _is_runtime_flag_payload(payload):
        raise NotImplementedError("Only reversible TTL runtime-flag improvements can be applied in this phase")

    operation = payload["operation"]
    max_ttl_seconds, min_confidence, min_score = _actuator_manual_limits()
    if float(proposal.get("confidence") or 0.0) < min_confidence:
        raise PermissionError("confidence_below_threshold")
    if float(proposal.get("score") or 0.0) < min_score:
        raise PermissionError("score_below_threshold")
    eligible, reason = validate_runtime_flag_operation(
        operation,
        max_ttl_seconds=max_ttl_seconds,
        policy_action=AUTO_APPLY_ACTION,
    )
    if not eligible:
        raise PermissionError(reason)

    ttl_seconds = float(operation.get("ttl_seconds") or 300)
    before = store.get_runtime_flag(str(operation["key"]))
    actuation = store.create_actuation(
        proposal_id=proposal_id,
        task_id=proposal.get("task_id"),
        action="set_runtime_flag",
        mode="manual_approval",
        before={"runtime_flag": before},
        operation=operation,
        expires_at=time.time() + ttl_seconds,
        metadata={
            "approval_id": approval.get("id"),
            "applied_by": applied_by,
            "policy_action": APPLY_ACTION,
        },
    )
    result = store.set_runtime_flag(
        str(operation["key"]),
        dict(operation.get("value") or {}),
        ttl_seconds=ttl_seconds,
    )
    store.finish_actuation(
        str(actuation["id"]),
        status="applied",
        after={"runtime_flag": result},
        impact={
            "expected_effect": (operation.get("value") or {}).get("safe_action"),
            "reversible": True,
            "rollback": "clear_runtime_flag",
            "ttl_seconds": ttl_seconds,
        },
    )
    applied = store.mark_improvement_applied(
        proposal_id,
        result={
            "operation": "set_runtime_flag",
            "flag": result,
            "approval_id": approval.get("id"),
            "actuation_id": actuation.get("id"),
            "applied_by": applied_by,
        },
    )
    return applied


def reject_improvement(
    proposal_id: str,
    *,
    reason: str = "",
    store: AgenticStore | None = None,
) -> dict[str, Any] | None:
    store = store or get_agentic_store()
    proposal = store.reject_improvement_proposal(proposal_id, reason=reason)
    if proposal and proposal.get("approval_id"):
        approval = store.get_approval(str(proposal["approval_id"]))
        if approval and approval.get("status") == ApprovalStatus.PENDING.value:
            store.reject(str(approval["id"]), reason=reason or "improvement_rejected")
    return proposal


def _approval_payload(proposal: dict[str, Any]) -> dict[str, Any]:
    return {
        "proposal_id": proposal["id"],
        "kind": proposal["kind"],
        "title": proposal["title"],
        "risk_level": proposal["risk_level"],
        "confidence": proposal["confidence"],
        "score": proposal["score"],
        "fingerprint": proposal["fingerprint"],
        "operation": proposal.get("payload") or {},
        "evidence": proposal.get("evidence") or {},
        "version": 1,
    }


def _is_runtime_flag_payload(payload: dict[str, Any]) -> bool:
    operation = payload.get("operation") or {}
    return (
        isinstance(operation, dict)
        and operation.get("operation") == "set_runtime_flag"
        and bool(operation.get("key"))
        and isinstance(operation.get("value") or {}, dict)
    )


def _actuator_manual_limits() -> tuple[int, float, float]:
    try:
        from orchestrator.config import get_settings

        cfg = get_settings().agentic_runtime
        return (
            int(cfg.actuator_max_auto_ttl_seconds),
            float(cfg.actuator_min_confidence),
            float(cfg.actuator_min_score),
        )
    except Exception:
        return 900, 0.75, 3.0


def _approved_payload_approval(
    store: AgenticStore,
    *,
    proposal: dict[str, Any],
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    approval_id = proposal.get("approval_id")
    if approval_id:
        approval = store.get_approval(str(approval_id))
        if approval and approval.get("status") == ApprovalStatus.APPROVED.value:
            matching = store.find_approval_for_payload(
                action=APPLY_ACTION,
                payload=payload,
                statuses=(ApprovalStatus.APPROVED.value,),
                task_id=proposal.get("task_id"),
            )
            if matching and matching.get("id") == approval.get("id"):
                return approval
    return store.find_approval_for_payload(
        action=APPLY_ACTION,
        payload=payload,
        statuses=(ApprovalStatus.APPROVED.value,),
        task_id=proposal.get("task_id"),
    )
