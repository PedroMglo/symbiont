"""Pydantic schemas shared by Resource Governor clients and services."""

from __future__ import annotations

import time
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from orchestrator.resource_governor.constants import (
    DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
    DEFAULT_LEASE_TTL_SECONDS,
)


def utc_now() -> float:
    return time.time()


class _ValueEnum(StrEnum):
    def __str__(self) -> str:
        return self.value


class Lane(_ValueEnum):
    INTERACTIVE = "interactive"
    INTERACTIVE_ENRICHMENT = "interactive_enrichment"
    BACKGROUND = "background"
    STORAGE = "storage"
    HEAVY_GPU = "heavy_gpu"
    SYSTEM_STATUS_FAST = "system_status_fast"


class LeaseScope(_ValueEnum):
    REQUEST = "request"
    SESSION = "session"
    BATCH = "batch"
    ARCHIVE = "archive"
    BACKGROUND_CYCLE = "background_cycle"
    MODEL_LOAD = "model_load"


class ResourceClass(_ValueEnum):
    CPU = "cpu"
    RAM = "ram"
    VRAM = "vram"
    IO_WRITE = "io_write"
    QDRANT_WRITE = "qdrant_write"
    MODEL_RUNTIME = "model_runtime"


class Capability(_ValueEnum):
    BM25_REBUILD = "bm25_rebuild"
    CHAT_STREAM = "chat_stream"
    STORAGE_ARCHIVE = "storage_archive"
    DOCUMENT_ETL = "document_etl"
    EMBEDDING_GPU_BATCH = "embedding_gpu_batch"
    EMBEDDING_CPU_BATCH = "embedding_cpu_batch"
    AUDIO_TRANSCRIBE_GPU = "audio_transcribe_gpu"
    AUDIO_TRANSCRIBE_CPU = "audio_transcribe_cpu"
    GRAPH_LLM = "graph_llm"
    DEEP_REASONING_BATCH = "deep_reasoning_batch"
    MATERIAL_ORCHESTRATION = "material_orchestration"
    MATERIAL_GENERATION = "material_generation"
    MODEL_WARMUP = "model_warmup"
    RAG_QUERY = "rag_query"
    RERANK = "rerank"


class QualityPolicy(_ValueEnum):
    PRESERVE = "preserve"
    DEGRADE_ALLOWED = "degrade_allowed"
    SKIP_ALLOWED = "skip_allowed"


class QualityImpact(_ValueEnum):
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class GovernorMode(_ValueEnum):
    OBSERVE_ONLY = "observe_only"
    ADVISORY = "advisory"
    ENFORCED = "enforced"


class PressureLevel(_ValueEnum):
    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"
    CRITICAL = "critical"


class LeaseDecisionKind(_ValueEnum):
    GRANTED = "granted"
    GRANTED_WITH_LIMITS = "granted_with_limits"
    RUN_CPU_ONLY = "run_cpu_only"
    SKIP_OPTIONAL = "skip_optional"
    DEFER = "defer"
    DENY = "deny"
    QUEUE_BACKGROUND = "queue_background"


class DecisionType(_ValueEnum):
    NORMAL = "normal"
    SOFT_ADVICE = "soft_advice"
    HARD_BLOCK = "hard_block"


class UserImpact(_ValueEnum):
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ActivityType(_ValueEnum):
    INTERACTIVE_CHAT_STREAM = "interactive_chat_stream"
    INTERACTIVE_QUERY = "interactive_query"
    BACKGROUND_TASK = "background_task"


class _Model(BaseModel):
    model_config = ConfigDict(use_enum_values=False, extra="allow")


class LeaseRequest(_Model):
    idempotency_key: str
    requester: str
    component: str
    lane: Lane
    lease_scope: LeaseScope
    resource_class: ResourceClass
    capability: Capability
    estimated_duration_seconds: int | None = None
    requested_ttl_seconds: int | None = None
    estimated_ram_mb: int | None = None
    estimated_vram_mb: int | None = None
    estimated_io_mb: int | None = None
    preemptible: bool = True
    quality_policy: QualityPolicy = QualityPolicy.PRESERVE
    estimated_quality_impact: QualityImpact = QualityImpact.LOW
    request_id: str = Field(default_factory=lambda: f"req_{uuid4().hex}")
    session_id: str | None = None


class LeaseHeartbeat(_Model):
    lease_id: str
    owner: str | None = None
    request_id: str | None = None


class LeaseDecision(_Model):
    decision: LeaseDecisionKind
    decision_type: DecisionType = DecisionType.NORMAL
    lease_id: str | None = None
    ttl_seconds: int | None = None
    heartbeat_interval_seconds: int | None = None
    limits: dict[str, Any] = Field(default_factory=dict)
    reason: str = ""
    retry_after_seconds: int | None = None
    effective_quality_policy: QualityPolicy | str = QualityPolicy.PRESERVE
    expected_user_impact: UserImpact | str = UserImpact.NONE

    @property
    def granted(self) -> bool:
        return self.decision in {
            LeaseDecisionKind.GRANTED,
            LeaseDecisionKind.GRANTED_WITH_LIMITS,
            LeaseDecisionKind.RUN_CPU_ONLY,
        }


class ActivityRequest(_Model):
    idempotency_key: str
    activity_type: ActivityType
    requester: str
    capability: Capability
    request_id: str
    component: str | None = None
    session_id: str | None = None
    ttl_seconds: int = 30


class LeaseRecord(_Model):
    lease_id: str
    request: LeaseRequest
    decision: LeaseDecision
    created_at: float
    updated_at: float
    expires_at: float
    released_at: float | None = None

    @classmethod
    def from_request(cls, request: LeaseRequest, decision: LeaseDecision) -> "LeaseRecord":
        now = utc_now()
        lease_id = decision.lease_id or f"lease_{uuid4().hex}"
        ttl = decision.ttl_seconds or request.requested_ttl_seconds or DEFAULT_LEASE_TTL_SECONDS
        normalized_decision = decision.model_copy(
            update={
                "lease_id": lease_id,
                "ttl_seconds": ttl,
                "heartbeat_interval_seconds": decision.heartbeat_interval_seconds
                or min(DEFAULT_HEARTBEAT_INTERVAL_SECONDS, max(1, ttl // 3)),
            }
        )
        return cls(
            lease_id=lease_id,
            request=request,
            decision=normalized_decision,
            created_at=now,
            updated_at=now,
            expires_at=now + ttl,
        )

    def heartbeat(self) -> None:
        now = utc_now()
        ttl = self.decision.ttl_seconds or DEFAULT_LEASE_TTL_SECONDS
        self.updated_at = now
        self.expires_at = now + ttl


class ActivityRecord(_Model):
    activity_id: str
    request: ActivityRequest
    created_at: float
    updated_at: float
    expires_at: float
    released_at: float | None = None

    @classmethod
    def from_request(cls, request: ActivityRequest) -> "ActivityRecord":
        now = utc_now()
        return cls(
            activity_id=f"activity_{uuid4().hex}",
            request=request,
            created_at=now,
            updated_at=now,
            expires_at=now + request.ttl_seconds,
        )

    def heartbeat(self) -> None:
        now = utc_now()
        self.updated_at = now
        self.expires_at = now + self.request.ttl_seconds


class ResourceSnapshot(_Model):
    cpu_percent: float | None = None
    ram_total_mb: int | None = None
    ram_available_mb: int | None = None
    ram_percent: float | None = None
    swap_used_mb: int | None = None
    swap_percent: float | None = None
    swap_growth_mb: int | None = None
    disk_free_mb: int | None = None
    disk_percent: float | None = None
    disk_free_ratio: float | None = None
    psi_cpu_some: float | None = None
    psi_memory_some: float | None = None
    psi_io_some: float | None = None
    gpu_available: bool = False
    gpu_name: str | None = None
    vram_total_mb: int | None = None
    vram_used_mb: int | None = None
    vram_free_mb: int | None = None
    gpu_utilization_pct: float | None = None
    gpu_temperature_c: float | None = None
    gpu_power_w: float | None = None
    gpu_processes: list[dict[str, Any]] = Field(default_factory=list)
    telemetry_incomplete: bool = False
    battery_percent: float | None = None
    battery_power_plugged: bool | None = None
    thermal_max_celsius: float | None = None
    thermal_throttle: bool = False
    lid_closed: bool | None = None
    pressure_level: PressureLevel = PressureLevel.LOW
    pressure_reasons: list[str] = Field(default_factory=list)
    active_activities: int = 0
    active_leases: int = 0


class EffectivePolicy(_Model):
    mode: GovernorMode | str = GovernorMode.OBSERVE_ONLY
    machine_profile: str = "local"
    limits: dict[str, Any] = Field(default_factory=dict)
    lanes: dict[str, Any] = Field(default_factory=dict)
    thresholds: dict[str, Any] = Field(default_factory=dict)
    pressure_policy: dict[str, Any] = Field(default_factory=dict)
    gpu_conflict_matrix: dict[str, Any] = Field(default_factory=dict)


class GovernorMetrics(_Model):
    decisions_total: int = 0
    grants_total: int = 0
    defers_total: int = 0
    denies_total: int = 0
    soft_advice_total: int = 0
    hard_blocks_total: int = 0
    expired_leases_total: int = 0
    expired_activities_total: int = 0
    active_leases: int = 0
    active_activities: int = 0
