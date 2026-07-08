"""Read-only telemetry provider helpers."""

from __future__ import annotations

import os
from pathlib import Path

from orchestrator.resource_governor.telemetry.schemas import (
    CpuTelemetry,
    HostTelemetry,
    MemoryTelemetry,
    SwapTelemetry,
)


def read_psi_some(path: str) -> float | None:
    try:
        text = Path(path).read_text(encoding="utf-8")
        first = text.splitlines()[0]
        for part in first.split():
            if part.startswith("avg10="):
                return float(part.split("=", 1)[1])
    except Exception:
        return None
    return None


def read_cpu() -> CpuTelemetry:
    percent = None
    try:
        import psutil

        percent = float(psutil.cpu_percent(interval=None))
    except Exception:
        pass
    return CpuTelemetry(percent=percent, psi_some_avg10=read_psi_some("/proc/pressure/cpu"))


def read_memory() -> MemoryTelemetry:
    total_mb = available_mb = used_mb = None
    percent = None
    try:
        import psutil

        mem = psutil.virtual_memory()
        total_mb = int(mem.total / 1024 / 1024)
        available_mb = int(mem.available / 1024 / 1024)
        used_mb = int(mem.used / 1024 / 1024)
        percent = float(mem.percent)
    except Exception:
        pass
    return MemoryTelemetry(
        total_mb=total_mb,
        available_mb=available_mb,
        used_mb=used_mb,
        percent=percent,
        psi_some_avg10=read_psi_some("/proc/pressure/memory"),
    )


def read_swap() -> SwapTelemetry:
    total_mb = used_mb = None
    percent = None
    try:
        import psutil

        swap = psutil.swap_memory()
        total_mb = int(swap.total / 1024 / 1024)
        used_mb = int(swap.used / 1024 / 1024)
        percent = float(swap.percent)
    except Exception:
        pass
    return SwapTelemetry(total_mb=total_mb, used_mb=used_mb, percent=percent)


def read_host() -> HostTelemetry:
    loadavg: list[float] = []
    try:
        loadavg = [float(value) for value in os.getloadavg()]
    except Exception:
        pass
    battery: dict[str, object] = {}
    thermal: dict[str, object] = {}
    try:
        import psutil

        raw_battery = psutil.sensors_battery()
        if raw_battery is not None:
            battery = {
                "percent": float(raw_battery.percent),
                "power_plugged": bool(raw_battery.power_plugged),
            }
        values = [
            float(entry.current)
            for entries in psutil.sensors_temperatures().values()
            for entry in entries
            if entry.current is not None
        ]
        if values:
            thermal = {"max_c": round(max(values), 1)}
    except Exception:
        pass
    return HostTelemetry(loadavg=loadavg, battery=battery, thermal=thermal)
