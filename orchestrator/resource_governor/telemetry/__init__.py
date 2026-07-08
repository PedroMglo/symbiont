"""Read-only host telemetry authority for Resource Governor decisions."""

from orchestrator.resource_governor.telemetry.authority import TelemetryAuthority
from orchestrator.resource_governor.telemetry.schemas import TelemetrySnapshot

__all__ = ["TelemetryAuthority", "TelemetrySnapshot"]
