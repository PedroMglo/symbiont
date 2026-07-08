"""NVIDIA telemetry via read-only nvidia-smi queries."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from orchestrator.resource_governor.telemetry.schemas import GpuProcess, GpuTelemetry


def _parse_int(value: str) -> int | None:
    value = value.strip().replace("MiB", "").replace("W", "")
    if value in {"", "[Not Supported]", "N/A"}:
        return None
    try:
        return int(float(value))
    except ValueError:
        return None


def _parse_float(value: str) -> float | None:
    value = value.strip().replace("%", "").replace("W", "")
    if value in {"", "[Not Supported]", "N/A"}:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _nvidia_smi_binary() -> str | None:
    for candidate in (
        "nvidia-smi",
        "/usr/lib/wsl/lib/nvidia-smi",
        "nvidia-smi.exe",
        "/mnt/c/Windows/System32/nvidia-smi.exe",
    ):
        found = shutil.which(candidate) if "/" not in candidate else candidate
        if found and Path(found).exists():
            return found
    return None


def read_nvidia_smi(*, timeout: float = 1.5) -> GpuTelemetry:
    binary = _nvidia_smi_binary()
    if not binary:
        return GpuTelemetry(source="nvidia-smi")
    query = (
        "--query-gpu=name,utilization.gpu,memory.total,memory.used,"
        "memory.free,temperature.gpu,power.draw"
    )
    try:
        result = subprocess.run(
            [binary, query, "--format=csv,noheader,nounits"],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except Exception:
        return GpuTelemetry(source="nvidia-smi")
    if result.returncode != 0 or not result.stdout.strip():
        return GpuTelemetry(source="nvidia-smi")
    parts = [part.strip() for part in result.stdout.splitlines()[0].split(",")]
    while len(parts) < 7:
        parts.append("")
    processes = _read_processes(binary, timeout=timeout)
    return GpuTelemetry(
        available=True,
        name=parts[0] or "NVIDIA GPU",
        util_pct=_parse_float(parts[1]),
        memory_total_mb=_parse_int(parts[2]),
        memory_used_mb=_parse_int(parts[3]),
        memory_free_mb=_parse_int(parts[4]),
        temperature_c=_parse_float(parts[5]),
        power_w=_parse_float(parts[6]),
        processes=processes,
        source="nvidia-smi",
    )


def _read_processes(binary: str, *, timeout: float) -> list[GpuProcess]:
    try:
        result = subprocess.run(
            [
                binary,
                "--query-compute-apps=pid,process_name,used_memory",
                "--format=csv,noheader,nounits",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except Exception:
        return []
    if result.returncode != 0:
        return []
    processes: list[GpuProcess] = []
    for line in result.stdout.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 3:
            continue
        pid = _parse_int(parts[0])
        if pid is None:
            continue
        processes.append(
            GpuProcess(
                pid=pid,
                name=parts[1] or "unknown",
                used_memory_mb=_parse_int(parts[2]),
            )
        )
    return processes
