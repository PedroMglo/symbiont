"""Schemas for host-grade read-only telemetry snapshots."""

from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _Model(BaseModel):
    model_config = ConfigDict(extra="allow")


class GpuProcess(_Model):
    pid: int
    name: str
    used_memory_mb: int | None = None


class GpuTelemetry(_Model):
    available: bool = False
    name: str | None = None
    util_pct: float | None = None
    memory_total_mb: int | None = None
    memory_used_mb: int | None = None
    memory_free_mb: int | None = None
    temperature_c: float | None = None
    power_w: float | None = None
    processes: list[GpuProcess] = Field(default_factory=list)
    source: str | None = None

    @property
    def incomplete(self) -> bool:
        return bool(
            self.available
            and (
                self.util_pct is None
                or self.memory_total_mb is None
                or self.memory_used_mb is None
                or self.memory_free_mb is None
            )
        )


class CpuTelemetry(_Model):
    percent: float | None = None
    psi_some_avg10: float | None = None


class MemoryTelemetry(_Model):
    total_mb: int | None = None
    available_mb: int | None = None
    used_mb: int | None = None
    percent: float | None = None
    psi_some_avg10: float | None = None


class SwapTelemetry(_Model):
    total_mb: int | None = None
    used_mb: int | None = None
    percent: float | None = None


class HostTelemetry(_Model):
    loadavg: list[float] = Field(default_factory=list)
    battery: dict[str, Any] = Field(default_factory=dict)
    thermal: dict[str, Any] = Field(default_factory=dict)


class DockerTelemetry(_Model):
    containers: list[dict[str, Any]] = Field(default_factory=list)


class TelemetrySnapshot(_Model):
    timestamp: float = Field(default_factory=time.time)
    host: HostTelemetry = Field(default_factory=HostTelemetry)
    cpu: CpuTelemetry = Field(default_factory=CpuTelemetry)
    ram: MemoryTelemetry = Field(default_factory=MemoryTelemetry)
    swap: SwapTelemetry = Field(default_factory=SwapTelemetry)
    gpu: GpuTelemetry = Field(default_factory=GpuTelemetry)
    docker: DockerTelemetry = Field(default_factory=DockerTelemetry)
    errors: list[str] = Field(default_factory=list)
    telemetry_incomplete: bool = False
