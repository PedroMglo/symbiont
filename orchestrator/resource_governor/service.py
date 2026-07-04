"""Authoritative Resource Governor service."""

from __future__ import annotations

from collections import deque
from typing import Any

from orchestrator.resource_governor.activity_registry import ActivityRegistry
from orchestrator.resource_governor.decision_engine import DecisionEngine
from orchestrator.resource_governor.effective_policy import build_effective_policy
from orchestrator.resource_governor.lease_registry import LeaseRegistry
from orchestrator.resource_governor.metrics import ResourceGovernorMetrics
from orchestrator.resource_governor.model_lifecycle import ModelLifecycleManager
from orchestrator.resource_governor.pressure_monitor import PressureMonitor
from orchestrator.resource_governor.reapers import ReaperThread
from orchestrator.resource_governor.schemas import (
    ActivityRecord,
    ActivityRequest,
    EffectivePolicy,
    GovernorMetrics,
    LeaseDecision,
    LeaseRequest,
    ResourceSnapshot,
)


class ResourceGovernorService:
    def __init__(self, *, policy: EffectivePolicy | None = None) -> None:
        self.policy = policy or build_effective_policy()
        self.leases = LeaseRegistry()
        self.activities = ActivityRegistry()
        self.monitor = PressureMonitor(thresholds=self.policy.thresholds)
        self.decision_engine = DecisionEngine(self.policy)
        self.metrics = ResourceGovernorMetrics()
        self.model_lifecycle = ModelLifecycleManager()
        self._recent_decisions: deque[dict[str, Any]] = deque(maxlen=200)
        self._lease_reaper = ReaperThread(interval_seconds=5.0, reap=self._reap_leases, name="resource-lease-reaper")
        self._activity_reaper = ReaperThread(interval_seconds=5.0, reap=self._reap_activities, name="resource-activity-reaper")
        self._model_reaper = ReaperThread(interval_seconds=30.0, reap=self.model_lifecycle.cleanup_idle_models, name="resource-model-reaper")

    def start(self) -> None:
        self._lease_reaper.start()
        self._activity_reaper.start()
        self._model_reaper.start()

    def stop(self) -> None:
        self._lease_reaper.stop()
        self._activity_reaper.stop()
        self._model_reaper.stop()

    def _reap_leases(self) -> None:
        self.metrics.record_expired_leases(self.leases.expire())

    def _reap_activities(self) -> None:
        self.metrics.record_expired_activities(self.activities.expire())

    def snapshot(self) -> ResourceSnapshot:
        return self.monitor.snapshot(
            active_activities=len(self.activities.active_records()),
            active_leases=len(self.leases.active_records()),
        )

    def request_lease(self, request: LeaseRequest) -> LeaseDecision:
        snapshot = self.snapshot()
        decision = self.decision_engine.decide(
            request,
            snapshot=snapshot,
            active_leases=self.leases.active_records(),
            active_activities=self.activities.active_records(),
        )
        decision = self.leases.create_or_get(request, decision)
        self.metrics.record_decision(str(decision.decision), str(decision.decision_type))
        self._record_decision(request, decision, snapshot)
        return decision

    def heartbeat_lease(self, lease_id: str) -> bool:
        return self.leases.heartbeat(lease_id) is not None

    def release_lease(self, lease_id: str) -> bool:
        return self.leases.release(lease_id)

    def register_activity(self, request: ActivityRequest) -> ActivityRecord:
        return self.activities.create_or_refresh(request)

    def heartbeat_activity(self, activity_id: str) -> bool:
        return self.activities.heartbeat(activity_id) is not None

    def release_activity(self, activity_id: str) -> bool:
        return self.activities.release(activity_id)

    def effective_policy(self) -> EffectivePolicy:
        return self.policy

    def metrics_snapshot(self) -> GovernorMetrics:
        return self.metrics.snapshot(
            active_leases=len(self.leases.active_records()),
            active_activities=len(self.activities.active_records()),
        )

    def _record_decision(self, request: LeaseRequest, decision: LeaseDecision, snapshot: ResourceSnapshot) -> None:
        self._recent_decisions.append(
            {
                "request_id": request.request_id,
                "requester": request.requester,
                "component": request.component,
                "lane": str(request.lane),
                "capability": str(request.capability),
                "decision": str(decision.decision),
                "decision_type": str(decision.decision_type),
                "reason": decision.reason,
                "pressure_level": str(snapshot.pressure_level),
                "pressure_reasons": list(snapshot.pressure_reasons),
                "active_activities": snapshot.active_activities,
                "active_leases": snapshot.active_leases,
            }
        )

    def recent_decisions(self, *, limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 200))
        return list(self._recent_decisions)[-limit:]


_SERVICE: ResourceGovernorService | None = None


def get_resource_governor_service() -> ResourceGovernorService:
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = ResourceGovernorService()
    return _SERVICE
