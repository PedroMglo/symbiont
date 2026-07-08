"""Container-visible NVIDIA procfs fallback."""

from __future__ import annotations

from pathlib import Path

from orchestrator.resource_governor.telemetry.schemas import GpuTelemetry

NVIDIA_PROCFS_ROOT = Path("/proc/driver/nvidia/gpus")


def read_procfs_gpu(root: Path = NVIDIA_PROCFS_ROOT) -> GpuTelemetry:
    for info_path in root.glob("*/information"):
        try:
            fields: dict[str, str] = {}
            with info_path.open(encoding="utf-8") as fh:
                for line in fh:
                    if ":" not in line:
                        continue
                    key, value = line.split(":", 1)
                    fields[key.strip().lower()] = value.strip()
            return GpuTelemetry(
                available=True,
                name=fields.get("model") or "NVIDIA GPU",
                source="procfs",
            )
        except OSError:
            continue
    for dev_path in (Path("/dev/nvidia0"), Path("/dev/dxg")):
        if dev_path.exists():
            return GpuTelemetry(
                available=True,
                name="NVIDIA GPU" if "nvidia" in str(dev_path) else "GPU device",
                source=str(dev_path),
            )
    return GpuTelemetry(source="procfs")
