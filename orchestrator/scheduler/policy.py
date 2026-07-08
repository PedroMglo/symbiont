"""Scheduler policy helpers that keep lane decisions explicit."""

from __future__ import annotations

from orchestrator.resource_governor.schemas import LeaseDecision, LeaseDecisionKind


def scheduler_decision_from_lease(decision: LeaseDecision, *, background_lane: bool) -> str:
    if decision.decision in {LeaseDecisionKind.GRANTED}:
        return "admit"
    if decision.decision in {LeaseDecisionKind.GRANTED_WITH_LIMITS, LeaseDecisionKind.RUN_CPU_ONLY}:
        return "admit_degraded"
    if decision.decision == LeaseDecisionKind.SKIP_OPTIONAL:
        return "admit_degraded"
    if decision.decision == LeaseDecisionKind.QUEUE_BACKGROUND:
        return "queue_background"
    if decision.decision == LeaseDecisionKind.DENY:
        return "reject_policy"
    if decision.decision == LeaseDecisionKind.DEFER and background_lane:
        return "queue_background"
    return "defer"
