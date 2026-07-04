"""Policy classification and audit hooks for agentic capabilities."""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Any

from orchestrator.agentic.context import get_agentic_context
from orchestrator.agentic.models import ApprovalStatus, PolicyDecision, PolicyDecisionKind, RiskLevel
from orchestrator.agentic.policy_backends import OpaPolicyBackend
from orchestrator.agentic.policy_registry import action_matrix, risk_for_action

log = logging.getLogger(__name__)

def normalize_action(action: str) -> str:
    return " ".join((action or "").strip().lower().split())


class PolicyEngine:
    """Classify actions according to the local agentic risk matrix."""

    def __init__(
        self,
        *,
        mode: str = "supervised",
        policy_mode: str = "audit",
        autonomous_safe_enabled: bool = False,
        opa_shadow_enabled: bool = False,
        opa_enforce_enabled: bool = False,
        opa_backend: OpaPolicyBackend | None = None,
    ) -> None:
        self.mode = mode
        self.policy_mode = policy_mode
        self.autonomous_safe_enabled = autonomous_safe_enabled
        self.opa_shadow_enabled = opa_shadow_enabled
        self.opa_enforce_enabled = opa_enforce_enabled
        self.opa_backend = opa_backend or OpaPolicyBackend()

    def check(self, action: str, payload: dict[str, Any] | None = None) -> PolicyDecision:
        normalized = normalize_action(action)
        risk = self._risk_for(normalized)

        if risk == RiskLevel.DENY.value:
            decision = PolicyDecisionKind.DENY.value
            reason = "Action is denied without explicit override"
            requires_approval = False
            dry_run_required = True
        elif risk == RiskLevel.HIGH.value:
            decision = (
                PolicyDecisionKind.REQUIRE_APPROVAL.value
                if self.policy_mode == "enforce"
                else PolicyDecisionKind.WOULD_REQUIRE_APPROVAL.value
            )
            reason = "High-risk action requires immutable approval and dry-run before execution"
            requires_approval = True
            dry_run_required = True
        elif risk == RiskLevel.MEDIUM.value:
            decision = PolicyDecisionKind.ALLOW.value
            reason = "Medium-risk action allowed in supervised mode with audit trail"
            requires_approval = False
            dry_run_required = False
        else:
            decision = PolicyDecisionKind.ALLOW.value
            reason = "Low-risk read-only or generation action allowed"
            requires_approval = False
            dry_run_required = False

        python_decision = PolicyDecision(
            action=normalized,
            decision=decision,
            risk_level=risk,
            reason=reason,
            requires_approval=requires_approval,
            dry_run_required=dry_run_required,
            evidence_required=risk in {RiskLevel.MEDIUM.value, RiskLevel.HIGH.value, RiskLevel.DENY.value},
            lease_required=risk in {RiskLevel.HIGH.value, RiskLevel.DENY.value},
            max_risk=RiskLevel.HIGH.value,
            mode=self.mode,
            policy_mode=self.policy_mode,
        )
        if not (self.opa_shadow_enabled or self.opa_enforce_enabled):
            return python_decision

        opa = self.opa_backend.decide(
            action=normalized,
            payload=payload,
            risk_level=risk,
            mode=self.mode,
            policy_mode=self.policy_mode,
            autonomous_safe_enabled=self.autonomous_safe_enabled,
        )
        shadow_parity = (
            opa.decision == python_decision.decision
            and opa.requires_approval == python_decision.requires_approval
            and opa.dry_run_required == python_decision.dry_run_required
        )
        if self.opa_enforce_enabled:
            return PolicyDecision(
                action=normalized,
                decision=opa.decision,
                risk_level=risk,
                reason=opa.reason,
                requires_approval=opa.requires_approval,
                dry_run_required=opa.dry_run_required,
                evidence_required=opa.evidence_required,
                lease_required=opa.lease_required,
                max_risk=opa.max_risk,
                mode=self.mode,
                policy_mode=self.policy_mode,
                backend=self.opa_backend.backend_name,
                shadow_backend="python_registry",
                shadow_decision=python_decision.decision,
                shadow_parity=shadow_parity,
                shadow_reason=python_decision.reason,
            )

        return PolicyDecision(
            action=python_decision.action,
            decision=python_decision.decision,
            risk_level=python_decision.risk_level,
            reason=python_decision.reason,
            requires_approval=python_decision.requires_approval,
            dry_run_required=python_decision.dry_run_required,
            evidence_required=python_decision.evidence_required,
            lease_required=python_decision.lease_required,
            max_risk=python_decision.max_risk,
            mode=python_decision.mode,
            policy_mode=python_decision.policy_mode,
            backend=python_decision.backend,
            shadow_backend=self.opa_backend.backend_name,
            shadow_decision=opa.decision,
            shadow_parity=shadow_parity,
            shadow_reason=opa.reason,
        )

    def list_matrix(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "policy_mode": self.policy_mode,
            "autonomous_safe_enabled": self.autonomous_safe_enabled,
            "opa_shadow_enabled": self.opa_shadow_enabled,
            "opa_enforce_enabled": self.opa_enforce_enabled,
            **action_matrix(),
        }

    def _risk_for(self, action: str) -> str:
        return risk_for_action(action)


def get_policy_engine() -> PolicyEngine:
    try:
        from orchestrator.config import get_settings

        cfg = get_settings().agentic_runtime
        security = get_settings().security
        return PolicyEngine(
            mode=cfg.default_mode,
            policy_mode=cfg.policy_mode,
            autonomous_safe_enabled=cfg.autonomous_safe_enabled,
            opa_shadow_enabled=security.opa_shadow_enabled,
            opa_enforce_enabled=security.opa_enforce_enabled,
        )
    except Exception:
        return PolicyEngine()


def check_policy(action: str, payload: dict[str, Any] | None = None) -> PolicyDecision:
    return get_policy_engine().check(action, payload)


def audit_policy_check(
    action: str,
    *,
    payload: dict[str, Any] | None = None,
    component: str = "",
) -> PolicyDecision:
    """Classify and persist a policy decision when a task context exists."""

    decision = check_policy(action, payload)
    ctx = get_agentic_context()
    if ctx is None:
        return decision

    try:
        from orchestrator.agentic.store import get_agentic_store

        store = get_agentic_store()
        data = asdict(decision)
        data["component"] = component
        approval_id = None
        if decision.decision == PolicyDecisionKind.REQUIRE_APPROVAL.value:
            try:
                from orchestrator.config import get_settings

                approved = store.find_approval_for_payload(
                    task_id=ctx.task_id,
                    action=decision.action,
                    payload=payload or {},
                    statuses=(ApprovalStatus.APPROVED.value,),
                )
                if approved is not None:
                    approval_id = str(approved.get("id"))
                    decision = PolicyDecision(
                        action=decision.action,
                        decision=PolicyDecisionKind.ALLOW.value,
                        risk_level=decision.risk_level,
                        reason=f"High-risk action allowed by approved approval {approval_id}",
                        requires_approval=False,
                        dry_run_required=False,
                        mode=decision.mode,
                        policy_mode=decision.policy_mode,
                    )
                    data = asdict(decision)
                    data["component"] = component
                    data["approval_id"] = approval_id
                else:
                    approval = store.find_approval_for_payload(
                        task_id=ctx.task_id,
                        action=decision.action,
                        payload=payload or {},
                        statuses=(ApprovalStatus.PENDING.value,),
                    )
                    if approval is None:
                        approval = store.create_approval(
                            task_id=ctx.task_id,
                            action=decision.action,
                            risk_level=decision.risk_level,
                            payload=payload or {},
                            ttl_seconds=get_settings().agentic_runtime.approval_ttl_seconds,
                            dry_run_result=(payload or {}).get("dry_run_result"),
                            metadata={"component": component, "policy_mode": decision.policy_mode},
                        )
                    approval_id = approval.get("id")
                    data["approval_id"] = approval_id
            except Exception as exc:
                log.debug("Approval handling skipped for %s: %s", action, exc)
        elif decision.decision == PolicyDecisionKind.DENY.value:
            data["blocked"] = True
        store.record_event(
            task_id=ctx.task_id,
            event_type="policy.checked",
            actor="policy",
            payload=data,
            trace_id=ctx.trace_id,
        )
        store.record_tool_call(
            task_id=ctx.task_id,
            tool_name=decision.action,
            risk_level=decision.risk_level,
            status=decision.decision,
            input_payload=payload or {},
            requires_approval=decision.requires_approval,
            approval_id=approval_id,
            metadata={"component": component, "policy_mode": decision.policy_mode},
        )
    except Exception as exc:
        log.debug("Policy audit skipped for %s: %s", action, exc)

    return decision


def headers_for_current_context() -> dict[str, str]:
    ctx = get_agentic_context()
    if ctx is None:
        return {}
    headers = {
        "X-Request-ID": ctx.request_id,
        "X-Trace-ID": ctx.trace_id,
        "X-Task-ID": ctx.task_id,
        "X-Idempotency-Key": f"agentic:{ctx.task_id}",
    }
    if ctx.session_id:
        headers["X-Session-ID"] = ctx.session_id
    return headers
