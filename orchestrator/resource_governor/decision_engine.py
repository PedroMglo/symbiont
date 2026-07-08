"""Resource Governor decision engine."""

from __future__ import annotations

from orchestrator.resource_governor.constants import (
    DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
    DEFAULT_LEASE_TTL_SECONDS,
)
from orchestrator.resource_governor.schemas import (
    ActivityRecord,
    Capability,
    DecisionType,
    EffectivePolicy,
    GovernorMode,
    Lane,
    LeaseDecision,
    LeaseDecisionKind,
    LeaseRecord,
    LeaseRequest,
    PressureLevel,
    QualityPolicy,
    ResourceSnapshot,
    UserImpact,
)


def _is_chat_activity(activity: ActivityRecord) -> bool:
    return activity.request.capability == Capability.CHAT_STREAM or str(activity.request.activity_type).startswith("interactive")


def _capability(record: LeaseRecord | ActivityRecord) -> str:
    if isinstance(record, LeaseRecord):
        return str(record.request.capability)
    return str(record.request.capability)


HEAVY_BACKGROUND_CAPABILITIES = {
    Capability.EMBEDDING_GPU_BATCH,
    Capability.GRAPH_LLM,
    Capability.DEEP_REASONING_BATCH,
    Capability.MATERIAL_GENERATION,
    Capability.MODEL_WARMUP,
    Capability.AUDIO_TRANSCRIBE_GPU,
}

INTERACTIVE_MODEL_CAPABILITIES = {
    Capability.CHAT_STREAM,
    Capability.MATERIAL_GENERATION,
}


def _enum_value(value: object) -> str:
    return getattr(value, "value", str(value))


def _swap_pressure_is_hard(snapshot: ResourceSnapshot, thresholds: dict) -> bool:
    swap_hard = int(thresholds.get("swap_used_mb_hard", 512))
    if snapshot.swap_used_mb is None or snapshot.swap_used_mb < swap_hard:
        return False
    swap_growth_hard = int(thresholds.get("swap_growth_mb_hard", 128))
    swap_percent_hard = float(thresholds.get("swap_percent_hard", 70))
    ram_available_hard = int(thresholds.get("ram_available_mb_hard", 1024))
    mem_hard = float(thresholds.get("memory_pressure_some_10s_hard", 35.0))
    active_pressure = bool(
        (snapshot.swap_growth_mb is not None and snapshot.swap_growth_mb >= swap_growth_hard)
        or (snapshot.ram_available_mb is not None and snapshot.ram_available_mb <= ram_available_hard)
        or (snapshot.psi_memory_some is not None and snapshot.psi_memory_some >= mem_hard)
    )
    if active_pressure:
        return True
    requires_active = bool(thresholds.get("swap_requires_active_pressure", True))
    if requires_active:
        return False
    return bool(snapshot.swap_percent is not None and snapshot.swap_percent >= swap_percent_hard)


def _critical_pressure_is_hard(snapshot: ResourceSnapshot, thresholds: dict) -> bool:
    try:
        critical = snapshot.pressure_level == PressureLevel.CRITICAL
    except Exception:
        critical = str(snapshot.pressure_level) == PressureLevel.CRITICAL.value
    if not critical:
        return False
    reasons = [str(reason) for reason in snapshot.pressure_reasons]
    if reasons and all(reason.startswith(("swap_used>", "swap_in_use")) for reason in reasons):
        return _swap_pressure_is_hard(snapshot, thresholds)
    return True


class DecisionEngine:
    def __init__(self, policy: EffectivePolicy) -> None:
        self.policy = policy

    def _mode(self) -> GovernorMode:
        try:
            return GovernorMode(self.policy.mode)
        except Exception:
            return GovernorMode.OBSERVE_ONLY

    def _observe(self, request: LeaseRequest, reason: str, *, limits: dict | None = None) -> LeaseDecision:
        return LeaseDecision(
            decision=LeaseDecisionKind.GRANTED_WITH_LIMITS,
            decision_type=DecisionType.SOFT_ADVICE,
            ttl_seconds=request.requested_ttl_seconds or DEFAULT_LEASE_TTL_SECONDS,
            heartbeat_interval_seconds=DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
            limits=limits or {},
            reason=f"observe_only: would block/adapt: {reason}",
            effective_quality_policy=request.quality_policy,
            expected_user_impact=UserImpact.NONE,
        )

    def _hard(self, request: LeaseRequest, reason: str, *, retry_after: int = 10) -> LeaseDecision:
        mode = self._mode()
        if mode == GovernorMode.OBSERVE_ONLY:
            return self._observe(request, reason)
        if mode == GovernorMode.ADVISORY and request.lane in {Lane.INTERACTIVE, Lane.INTERACTIVE_ENRICHMENT}:
            return self._observe(request, reason, limits={"advice": "avoid heavy concurrent work"})
        return LeaseDecision(
            decision=LeaseDecisionKind.DEFER,
            decision_type=DecisionType.HARD_BLOCK,
            reason=reason,
            retry_after_seconds=retry_after,
            effective_quality_policy=QualityPolicy.PRESERVE,
            expected_user_impact=UserImpact.NONE,
        )

    def _soft(self, request: LeaseRequest, reason: str, *, limits: dict | None = None) -> LeaseDecision:
        mode = self._mode()
        if request.quality_policy == QualityPolicy.SKIP_ALLOWED and request.lane == Lane.INTERACTIVE_ENRICHMENT:
            return LeaseDecision(
                decision=LeaseDecisionKind.SKIP_OPTIONAL,
                decision_type=DecisionType.SOFT_ADVICE,
                limits=limits or {},
                reason=reason,
                retry_after_seconds=5,
                effective_quality_policy=QualityPolicy.PRESERVE,
                expected_user_impact=UserImpact.LOW,
            )
        if mode in {GovernorMode.OBSERVE_ONLY, GovernorMode.ADVISORY}:
            return LeaseDecision(
                decision=LeaseDecisionKind.GRANTED_WITH_LIMITS,
                decision_type=DecisionType.SOFT_ADVICE,
                ttl_seconds=request.requested_ttl_seconds or DEFAULT_LEASE_TTL_SECONDS,
                heartbeat_interval_seconds=DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
                limits=limits or {},
                reason=reason,
                effective_quality_policy=request.quality_policy,
                expected_user_impact=UserImpact.NONE,
            )
        return LeaseDecision(
            decision=LeaseDecisionKind.GRANTED_WITH_LIMITS,
            decision_type=DecisionType.SOFT_ADVICE,
            ttl_seconds=request.requested_ttl_seconds or DEFAULT_LEASE_TTL_SECONDS,
            heartbeat_interval_seconds=DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
            limits=limits or {},
            reason=reason,
            effective_quality_policy=request.quality_policy,
            expected_user_impact=UserImpact.NONE,
        )

    def _normal_grant(self, request: LeaseRequest) -> LeaseDecision:
        ttl = request.requested_ttl_seconds or min(
            DEFAULT_LEASE_TTL_SECONDS,
            max(10, int(request.estimated_duration_seconds or DEFAULT_LEASE_TTL_SECONDS)),
        )
        if request.lease_scope in {"session", "model_load"}:
            ttl = max(ttl, 120)
        return LeaseDecision(
            decision=LeaseDecisionKind.GRANTED,
            decision_type=DecisionType.NORMAL,
            ttl_seconds=ttl,
            heartbeat_interval_seconds=min(DEFAULT_HEARTBEAT_INTERVAL_SECONDS, max(1, ttl // 3)),
            reason="granted by resource-governor.v1",
            effective_quality_policy=request.quality_policy,
            expected_user_impact=UserImpact.NONE,
        )

    def _conflicts_with_active(self, request: LeaseRequest, active: list[LeaseRecord], activities: list[ActivityRecord]) -> str | None:
        matrix = self.policy.gpu_conflict_matrix or {}
        requested = str(request.capability)
        active_caps = {_capability(record) for record in active}
        active_caps.update(_capability(activity) for activity in activities)

        requested_blocks = set(matrix.get(requested, {}).get("blocks", []))
        for cap in active_caps:
            if cap == requested:
                continue
            cap_blocks = set(matrix.get(cap, {}).get("blocks", []))
            if cap in requested_blocks or requested in cap_blocks:
                return f"{requested} conflicts with active {cap}"
        return None

    def _interactive_workers(self) -> int:
        raw = (self.policy.lanes or {}).get("interactive", {})
        try:
            return max(1, int(raw.get("workers", 1)))
        except (AttributeError, TypeError, ValueError):
            return 1

    def _active_interactive_model_runtime(
        self,
        request: LeaseRequest,
        active_leases: list[LeaseRecord],
        active_activities: list[ActivityRecord],  # noqa: ARG002
    ) -> int:
        active_model_leases = 0
        for record in active_leases:
            if record.request.idempotency_key == request.idempotency_key:
                continue
            if (
                _enum_value(record.request.lane) == Lane.INTERACTIVE.value
                and _enum_value(record.request.capability) in {cap.value for cap in INTERACTIVE_MODEL_CAPABILITIES}
            ):
                active_model_leases += 1
        return active_model_leases

    def decide(
        self,
        request: LeaseRequest,
        *,
        snapshot: ResourceSnapshot,
        active_leases: list[LeaseRecord],
        active_activities: list[ActivityRecord],
    ) -> LeaseDecision:
        active_chat = any(_is_chat_activity(activity) for activity in active_activities)
        pressure_reasons = {str(reason) for reason in snapshot.pressure_reasons}

        if request.lane == Lane.SYSTEM_STATUS_FAST:
            return self._normal_grant(request)

        if request.lane == Lane.INTERACTIVE:
            if (
                request.capability in INTERACTIVE_MODEL_CAPABILITIES
                and self._active_interactive_model_runtime(request, active_leases, active_activities) >= self._interactive_workers()
            ):
                return self._hard(
                    request,
                    "interactive_model_runtime_busy",
                    retry_after=5,
                )
            return self._normal_grant(request)

        if active_chat and request.lane == Lane.STORAGE:
            return self._hard(request, "storage pauses while interactive chat/query is active", retry_after=15)

        if active_chat and request.lane == Lane.HEAVY_GPU:
            return self._hard(request, "heavy GPU work is blocked during active chat stream", retry_after=10)

        if request.capability == Capability.MODEL_WARMUP and active_chat:
            return self._hard(request, "large model warmup is blocked during response generation", retry_after=15)

        if active_chat and request.lane == Lane.BACKGROUND and request.capability in HEAVY_BACKGROUND_CAPABILITIES:
            return self._hard(request, "heavy background work is blocked during active interaction", retry_after=15)

        if "gpu_saturated" in pressure_reasons and request.lane in {Lane.BACKGROUND, Lane.HEAVY_GPU}:
            return self._hard(request, "gpu_saturated", retry_after=15)

        if "thermal_high" in pressure_reasons and request.lane in {Lane.BACKGROUND, Lane.HEAVY_GPU}:
            return self._hard(request, "thermal_high", retry_after=30)

        if (
            "vram_low" in pressure_reasons
            and request.capability in {Capability.MODEL_WARMUP, Capability.EMBEDDING_GPU_BATCH, Capability.RERANK}
        ):
            return self._hard(request, "vram_low", retry_after=20)

        conflict = self._conflicts_with_active(request, active_leases, active_activities)
        if conflict and request.lane in {Lane.HEAVY_GPU, Lane.BACKGROUND, Lane.INTERACTIVE_ENRICHMENT}:
            return self._hard(request, conflict, retry_after=10)

        thresholds = self.policy.thresholds or {}
        if _swap_pressure_is_hard(snapshot, thresholds) and request.lane != Lane.INTERACTIVE:
            return self._hard(request, f"swap usage is high ({snapshot.swap_used_mb}MB)", retry_after=30)

        mem_hard = float(thresholds.get("memory_pressure_some_10s_hard", 35.0))
        io_hard = float(thresholds.get("io_pressure_some_10s_hard", 40.0))
        thermal_soft = float(thresholds.get("thermal_celsius_soft", 85))
        thermal_soft_active = (
            snapshot.thermal_max_celsius is not None
            and snapshot.thermal_max_celsius >= thermal_soft
        )
        if request.lane in {Lane.BACKGROUND, Lane.STORAGE, Lane.HEAVY_GPU}:
            if snapshot.battery_power_plugged is False and snapshot.battery_percent is not None:
                battery_hard = float(thresholds.get("battery_percent_hard", 15))
                if snapshot.battery_percent <= battery_hard:
                    return self._hard(
                        request,
                        f"battery is low and unplugged ({snapshot.battery_percent:.0f}%)",
                        retry_after=60,
                    )
            thermal_hard = float(thresholds.get("thermal_celsius_hard", 92))
            if snapshot.thermal_throttle or (
                snapshot.thermal_max_celsius is not None and snapshot.thermal_max_celsius >= thermal_hard
            ):
                return self._hard(
                    request,
                    f"thermal pressure is high ({snapshot.thermal_max_celsius or thermal_hard:.0f}C)",
                    retry_after=45,
                )
            if snapshot.psi_memory_some is not None and snapshot.psi_memory_some >= mem_hard:
                return self._hard(request, f"PSI memory pressure is critical ({snapshot.psi_memory_some:.2f})", retry_after=30)
            if snapshot.psi_io_some is not None and snapshot.psi_io_some >= io_hard:
                return self._hard(request, f"PSI IO pressure is critical ({snapshot.psi_io_some:.2f})", retry_after=30)
            if _critical_pressure_is_hard(snapshot, thresholds):
                return self._hard(request, "system pressure is critical", retry_after=30)

        if request.resource_class in {"io_write", "qdrant_write"} and snapshot.disk_percent is not None:
            disk_free_ratio_hard = float(thresholds.get("disk_free_ratio_hard", 0.12))
            used_ratio = snapshot.disk_percent / 100.0
            if (1.0 - used_ratio) <= disk_free_ratio_hard:
                return self._hard(request, f"disk free ratio is below {disk_free_ratio_hard:.2f}", retry_after=60)

        if request.capability in {Capability.RERANK, Capability.GRAPH_LLM} and request.lane == Lane.INTERACTIVE_ENRICHMENT:
            limits = {}
            if (self.policy.limits or {}).get("reranker") == "only_if_uncertain":
                limits["run_only_if_uncertain"] = True
            if (self.policy.limits or {}).get("graph_deferred"):
                limits["defer_graph_unless_intent_requires"] = True
            if limits:
                return self._soft(request, "interactive enrichment is opportunistic on this profile", limits=limits)

        if request.lane in {Lane.BACKGROUND, Lane.STORAGE}:
            limits = {
                "workers": (self.policy.lanes or {}).get(str(request.lane), {}).get("workers", 1),
                "checkpoint_required": True,
            }
            if request.capability == Capability.EMBEDDING_GPU_BATCH:
                try:
                    embedding_batch = int((self.policy.limits or {}).get("embedding_batch", 1))
                except (TypeError, ValueError):
                    embedding_batch = 1
                if thermal_soft_active:
                    embedding_batch = max(1, embedding_batch // 2)
                limits["batch_size"] = embedding_batch
            if thermal_soft_active:
                limits["thermal_backoff"] = True
                limits["thermal_soft_celsius"] = thermal_soft
                return self._soft(
                    request,
                    f"thermal soft pressure ({snapshot.thermal_max_celsius:.0f}C)",
                    limits=limits,
                )
            return self._soft(request, "preemptible lane granted with adaptive limits", limits=limits)

        return self._normal_grant(request)
