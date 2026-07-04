"""Counters for the Resource Governor."""

from __future__ import annotations

from threading import RLock

from orchestrator.resource_governor.schemas import DecisionType, GovernorMetrics, LeaseDecisionKind


class ResourceGovernorMetrics:
    def __init__(self) -> None:
        self._lock = RLock()
        self._metrics = GovernorMetrics()

    def record_decision(self, decision: str, decision_type: str) -> None:
        with self._lock:
            self._metrics.decisions_total += 1
            if decision in {LeaseDecisionKind.GRANTED, LeaseDecisionKind.GRANTED_WITH_LIMITS, LeaseDecisionKind.RUN_CPU_ONLY}:
                self._metrics.grants_total += 1
            elif decision == LeaseDecisionKind.DEFER:
                self._metrics.defers_total += 1
            elif decision == LeaseDecisionKind.DENY:
                self._metrics.denies_total += 1
            if decision_type == DecisionType.SOFT_ADVICE:
                self._metrics.soft_advice_total += 1
            elif decision_type == DecisionType.HARD_BLOCK:
                self._metrics.hard_blocks_total += 1

    def record_expired_leases(self, count: int) -> None:
        if count:
            with self._lock:
                self._metrics.expired_leases_total += count

    def record_expired_activities(self, count: int) -> None:
        if count:
            with self._lock:
                self._metrics.expired_activities_total += count

    def snapshot(self, *, active_leases: int, active_activities: int) -> GovernorMetrics:
        with self._lock:
            data = self._metrics.model_dump()
        data["active_leases"] = active_leases
        data["active_activities"] = active_activities
        return GovernorMetrics.model_validate(data)
