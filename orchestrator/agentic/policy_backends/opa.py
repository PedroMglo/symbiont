"""OPA-compatible policy reducer for agentic policy decisions.

The Rego bundle in ``infra/security/opa/ai_local`` is the policy source artifact.
This module mirrors that reducer locally so shadow/parity tests do not require
the host to have the ``opa`` CLI installed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from orchestrator.agentic.models import PolicyDecisionKind, RiskLevel


@dataclass(frozen=True)
class OpaPolicyResult:
    decision: str
    reason: str
    requires_approval: bool
    dry_run_required: bool
    evidence_required: bool
    lease_required: bool
    max_risk: str
    input: dict[str, Any]


class OpaPolicyBackend:
    """Evaluate the OPA policy contract for a normalized action payload."""

    backend_name = "opa_shadow"

    def decide(
        self,
        *,
        action: str,
        payload: dict[str, Any] | None,
        risk_level: str,
        mode: str,
        policy_mode: str,
        autonomous_safe_enabled: bool,
    ) -> OpaPolicyResult:
        input_payload = build_policy_input(
            action=action,
            payload=payload,
            risk_level=risk_level,
            mode=mode,
            policy_mode=policy_mode,
            autonomous_safe_enabled=autonomous_safe_enabled,
        )
        return evaluate_policy_input(input_payload)


def build_policy_input(
    *,
    action: str,
    payload: dict[str, Any] | None,
    risk_level: str,
    mode: str,
    policy_mode: str,
    autonomous_safe_enabled: bool,
) -> dict[str, Any]:
    payload = payload or {}
    return {
        "action": {
            "name": action,
            "risk_level": risk_level,
            "payload": payload,
        },
        "context": {
            "mode": mode,
            "policy_mode": policy_mode,
            "autonomous_safe_enabled": autonomous_safe_enabled,
        },
    }


def evaluate_policy_input(input_payload: dict[str, Any]) -> OpaPolicyResult:
    action = input_payload.get("action") if isinstance(input_payload.get("action"), dict) else {}
    context = input_payload.get("context") if isinstance(input_payload.get("context"), dict) else {}
    risk_level = str(action.get("risk_level") or RiskLevel.MEDIUM.value)
    policy_mode = str(context.get("policy_mode") or "audit")

    if risk_level == RiskLevel.DENY.value:
        return OpaPolicyResult(
            decision=PolicyDecisionKind.DENY.value,
            reason="OPA policy denies the action without explicit override",
            requires_approval=False,
            dry_run_required=True,
            evidence_required=True,
            lease_required=True,
            max_risk=RiskLevel.HIGH.value,
            input=input_payload,
        )

    if risk_level == RiskLevel.HIGH.value:
        decision = (
            PolicyDecisionKind.REQUIRE_APPROVAL.value
            if policy_mode == "enforce"
            else PolicyDecisionKind.WOULD_REQUIRE_APPROVAL.value
        )
        return OpaPolicyResult(
            decision=decision,
            reason="OPA policy requires approval and dry-run evidence for high-risk action",
            requires_approval=True,
            dry_run_required=True,
            evidence_required=True,
            lease_required=True,
            max_risk=RiskLevel.HIGH.value,
            input=input_payload,
        )

    if risk_level == RiskLevel.MEDIUM.value:
        return OpaPolicyResult(
            decision=PolicyDecisionKind.ALLOW.value,
            reason="OPA policy allows medium-risk action with audit evidence",
            requires_approval=False,
            dry_run_required=False,
            evidence_required=True,
            lease_required=False,
            max_risk=RiskLevel.HIGH.value,
            input=input_payload,
        )

    return OpaPolicyResult(
        decision=PolicyDecisionKind.ALLOW.value,
        reason="OPA policy allows low-risk read-only or generation action",
        requires_approval=False,
        dry_run_required=False,
        evidence_required=False,
        lease_required=False,
        max_risk=RiskLevel.HIGH.value,
        input=input_payload,
    )
