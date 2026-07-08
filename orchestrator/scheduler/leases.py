"""Typed scheduler lease/admission payloads."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SchedulerDecision = Literal["admit", "admit_degraded", "defer", "reject_policy", "queue_background"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="allow")


class RoutePlan(_Model):
    route: str
    owner: str
    lane: str
    requires_gpu: bool = False
    requires_rag: bool = False
    max_latency_s: int = 60
    max_tokens: int = 512
    can_degrade: bool = True
    evidence_required: bool = False
    session_id: str | None = None


class SchedulerResources(_Model):
    gpu: bool = False
    estimated_vram_mb: int | None = None
    estimated_duration_s: int | None = None
    estimated_ram_mb: int | None = None
    estimated_io_mb: int | None = None


class SchedulerLeaseRequest(_Model):
    owner: str
    lane: str
    resources: SchedulerResources = Field(default_factory=SchedulerResources)
    priority: int | None = None
    preemptible: bool = True
    session_id: str | None = None
    route_plan: RoutePlan | None = None
    idempotency_key: str | None = None


class SchedulerLeaseResponse(_Model):
    decision: SchedulerDecision
    lease_id: str | None = None
    ttl_s: int | None = None
    retry_after_s: int | None = None
    reason: str | None = None
    pressure_level: str | None = None
    pressure_reasons: list[str] = Field(default_factory=list)
    limits: dict[str, Any] = Field(default_factory=dict)
