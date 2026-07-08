"""Read-only telemetry authority with short-lived cache."""

from __future__ import annotations

import os
import time
from typing import Protocol

from orchestrator.resource_governor.telemetry.nvidia_smi_provider import read_nvidia_smi
from orchestrator.resource_governor.telemetry.procfs_provider import read_procfs_gpu
from orchestrator.resource_governor.telemetry.schemas import TelemetrySnapshot

from . import docker_provider, providers


class _TelemetrySnapshotClient(Protocol):
    def snapshot(self) -> TelemetrySnapshot: ...


class TelemetryAuthority:
    """Collect host-grade telemetry without mutating host state."""

    def __init__(
        self,
        *,
        cache_ttl_seconds: float = 1.5,
        external_client: _TelemetrySnapshotClient | None = None,
    ) -> None:
        self.cache_ttl_seconds = max(0.1, float(cache_ttl_seconds))
        self.external_client = external_client
        self._cached: TelemetrySnapshot | None = None
        self._cached_at = 0.0

    @classmethod
    def from_env(cls) -> "TelemetryAuthority":
        raw = os.environ.get("AI_TELEMETRY_AUTHORITY_CACHE_TTL_SECONDS", "1.5")
        try:
            ttl = float(raw)
        except ValueError:
            ttl = 1.5
        client: _TelemetrySnapshotClient | None = None
        base_url = os.environ.get("AI_TELEMETRY_AUTHORITY_URL", "").strip()
        if base_url:
            timeout = _env_float("AI_TELEMETRY_AUTHORITY_TIMEOUT_SECONDS", 2.0)
            client = _build_external_client(base_url=base_url, timeout=timeout)
        return cls(cache_ttl_seconds=ttl, external_client=client)

    def snapshot(self) -> TelemetrySnapshot:
        now = time.monotonic()
        if self._cached is not None and now - self._cached_at <= self.cache_ttl_seconds:
            return self._cached

        errors: list[str] = []
        if self.external_client is not None:
            try:
                snapshot = self.external_client.snapshot()
                self._cached = snapshot
                self._cached_at = now
                return snapshot
            except Exception as exc:
                errors.append(f"external:{type(exc).__name__}")

        try:
            gpu = read_nvidia_smi()
            if not gpu.available:
                gpu = read_procfs_gpu()
        except Exception as exc:
            errors.append(f"gpu:{type(exc).__name__}")
            gpu = read_procfs_gpu()

        try:
            snapshot = TelemetrySnapshot(
                host=providers.read_host(),
                cpu=providers.read_cpu(),
                ram=providers.read_memory(),
                swap=providers.read_swap(),
                gpu=gpu,
                docker=docker_provider.read_docker(),
                errors=errors,
                telemetry_incomplete=gpu.incomplete,
            )
        except Exception as exc:
            snapshot = TelemetrySnapshot(
                errors=[*errors, f"snapshot:{type(exc).__name__}"],
                telemetry_incomplete=True,
            )
        self._cached = snapshot
        self._cached_at = now
        return snapshot


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _build_external_client(*, base_url: str, timeout: float) -> _TelemetrySnapshotClient:
    from orchestrator.resource_governor.telemetry.client import TelemetryAuthorityClient

    return TelemetryAuthorityClient(base_url=base_url, timeout=timeout)
