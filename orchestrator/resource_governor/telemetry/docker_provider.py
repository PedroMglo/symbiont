"""Read-only Docker telemetry placeholder.

Docker process/container mutation is explicitly out of scope for this authority.
The provider returns an empty container list unless a future read-only stats
collector is wired through a typed infra contract.
"""

from __future__ import annotations

from orchestrator.resource_governor.telemetry.schemas import DockerTelemetry


def read_docker() -> DockerTelemetry:
    return DockerTelemetry(containers=[])
