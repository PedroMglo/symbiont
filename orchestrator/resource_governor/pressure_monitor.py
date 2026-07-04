"""Resource pressure sampling for weak and strong machines."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from orchestrator.resource_governor.schemas import PressureLevel, ResourceSnapshot


def _read_psi_some(path: str) -> float | None:
    try:
        text = Path(path).read_text(encoding="utf-8")
        first = text.splitlines()[0]
        for part in first.split():
            if part.startswith("avg10="):
                return float(part.split("=", 1)[1])
    except Exception:
        return None
    return None


def _read_nvidia_smi() -> tuple[int | None, int | None, int | None, float | None, bool]:
    enabled = os.environ.get("AI_RESOURCE_GOVERNOR_ENABLE_NVIDIA_SMI", "").strip().lower()
    if enabled not in {"1", "true", "yes", "on"}:
        return None, None, None, None, False
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=memory.total,memory.used,memory.free,utilization.gpu",
                "--format=csv,noheader,nounits",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=1.5,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None, None, None, None, False
        first = result.stdout.splitlines()[0]
        total, used, free, util = [part.strip() for part in first.split(",")[:4]]
        return int(total), int(used), int(free), float(util), True
    except Exception:
        return None, None, None, None, False


def _read_battery() -> tuple[float | None, bool | None]:
    try:
        import psutil

        battery = psutil.sensors_battery()
    except Exception:
        return None, None
    if battery is None:
        return None, None
    return float(battery.percent), bool(battery.power_plugged)


def _read_thermal_max_celsius() -> float | None:
    values: list[float] = []
    for path in Path("/sys/class/thermal").glob("thermal_zone*/temp"):
        try:
            raw = path.read_text(encoding="utf-8").strip()
            if not raw:
                continue
            value = float(raw)
            values.append(value / 1000.0 if value > 1000 else value)
        except Exception:
            continue
    if values:
        return round(max(values), 1)
    try:
        import psutil

        for entries in psutil.sensors_temperatures().values():
            for entry in entries:
                if entry.current is not None:
                    values.append(float(entry.current))
    except Exception:
        pass
    return round(max(values), 1) if values else None


def _read_lid_closed() -> bool | None:
    for path in Path("/proc/acpi/button/lid").glob("*/state"):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore").lower()
        except Exception:
            continue
        if "closed" in text:
            return True
        if "open" in text:
            return False
    return None


class PressureMonitor:
    def __init__(
        self,
        *,
        disk_path: str | os.PathLike[str] | None = None,
        thresholds: dict[str, Any] | None = None,
    ) -> None:
        self.disk_path = Path(disk_path or os.environ.get("AI_RESOURCE_GOVERNOR_DISK_PATH", "."))
        self.thresholds = thresholds or {}
        self._last_swap_used_mb: int | None = None

    def snapshot(self, *, active_activities: int = 0, active_leases: int = 0) -> ResourceSnapshot:
        cpu_percent = None
        ram_total_mb = None
        ram_available_mb = None
        ram_percent = None
        swap_used_mb = None
        swap_percent = None
        try:
            import psutil

            cpu_percent = float(psutil.cpu_percent(interval=None))
            mem = psutil.virtual_memory()
            ram_total_mb = int(mem.total / 1024 / 1024)
            ram_available_mb = int(mem.available / 1024 / 1024)
            ram_percent = float(mem.percent)
            swap = psutil.swap_memory()
            swap_used_mb = int(swap.used / 1024 / 1024)
            swap_percent = float(swap.percent)
        except Exception:
            pass

        disk_free_mb = None
        disk_percent = None
        disk_free_ratio = None
        try:
            usage = shutil.disk_usage(self.disk_path)
            disk_free_mb = int(usage.free / 1024 / 1024)
            if usage.total:
                disk_percent = round((usage.used / usage.total) * 100, 2)
                disk_free_ratio = round(usage.free / usage.total, 4)
        except Exception:
            pass

        vram_total_mb, vram_used_mb, vram_free_mb, gpu_util, gpu_available = _read_nvidia_smi()
        psi_cpu = _read_psi_some("/proc/pressure/cpu")
        psi_mem = _read_psi_some("/proc/pressure/memory")
        psi_io = _read_psi_some("/proc/pressure/io")
        battery_percent, battery_power_plugged = _read_battery()
        thermal_max_celsius = _read_thermal_max_celsius()
        lid_closed = _read_lid_closed()

        pressure = PressureLevel.LOW
        swap_growth = 0
        if swap_used_mb is not None:
            if self._last_swap_used_mb is not None:
                swap_growth = max(0, swap_used_mb - self._last_swap_used_mb)
            self._last_swap_used_mb = swap_used_mb

        thresholds = self.thresholds
        swap_hard = int(thresholds.get("swap_used_mb_hard", 512))
        swap_growth_hard = int(thresholds.get("swap_growth_mb_hard", 128))
        swap_percent_hard = float(thresholds.get("swap_percent_hard", 70))
        ram_available_hard = int(thresholds.get("ram_available_mb_hard", 1024))
        mem_hard = float(thresholds.get("memory_pressure_some_10s_hard", 0.35))
        io_hard = float(thresholds.get("io_pressure_some_10s_hard", 0.40))
        mem_soft = float(thresholds.get("memory_pressure_some_10s_soft", 0.20))
        io_soft = float(thresholds.get("io_pressure_some_10s_soft", 0.25))
        thermal_hard = float(thresholds.get("thermal_celsius_hard", 92))
        thermal_soft = float(thresholds.get("thermal_celsius_soft", 85))
        battery_hard = float(thresholds.get("battery_percent_hard", 15))
        battery_soft = float(thresholds.get("battery_percent_soft", 25))

        reasons: list[str] = []
        swap_used_over_hard = swap_used_mb is not None and swap_used_mb > swap_hard
        swap_growth_over_hard = swap_growth > swap_growth_hard
        swap_percent_over_hard = swap_percent is not None and swap_percent >= swap_percent_hard
        ram_available_below_hard = ram_available_mb is not None and ram_available_mb <= ram_available_hard
        if swap_used_over_hard and (swap_percent_over_hard or swap_growth_over_hard or ram_available_below_hard):
            reasons.append(f"swap_used>{swap_hard}MB")
        if swap_growth_over_hard:
            reasons.append(f"swap_growth>{swap_growth_hard}MB")
        if psi_mem is not None and psi_mem > mem_hard:
            reasons.append(f"psi_memory>{mem_hard:.2f}")
        if psi_io is not None and psi_io > io_hard:
            reasons.append(f"psi_io>{io_hard:.2f}")
        if thermal_max_celsius is not None and thermal_max_celsius >= thermal_hard:
            reasons.append(f"thermal>={thermal_hard:.0f}C")
        if battery_percent is not None and battery_power_plugged is False and battery_percent <= battery_hard:
            reasons.append(f"battery<={battery_hard:.0f}%")

        if reasons:
            pressure = PressureLevel.CRITICAL
        else:
            soft_reasons: list[str] = []
            if swap_used_over_hard:
                soft_reasons.append(f"swap_used>{swap_hard}MB")
            elif swap_used_mb and swap_used_mb > 0:
                soft_reasons.append("swap_in_use")
            if psi_mem is not None and psi_mem > mem_soft:
                soft_reasons.append(f"psi_memory>{mem_soft:.2f}")
            if psi_io is not None and psi_io > io_soft:
                soft_reasons.append(f"psi_io>{io_soft:.2f}")
            if thermal_max_celsius is not None and thermal_max_celsius >= thermal_soft:
                soft_reasons.append(f"thermal>={thermal_soft:.0f}C")
            if battery_percent is not None and battery_power_plugged is False and battery_percent <= battery_soft:
                soft_reasons.append(f"battery<={battery_soft:.0f}%")
            reasons.extend(soft_reasons)

        if pressure != PressureLevel.CRITICAL and reasons:
            pressure = PressureLevel.HIGH
        elif (ram_percent and ram_percent > 80) or (cpu_percent and cpu_percent > 85):
            if ram_percent and ram_percent > 80:
                reasons.append("ram_percent>80")
            if cpu_percent and cpu_percent > 85:
                reasons.append("cpu_percent>85")
            pressure = PressureLevel.MODERATE

        return ResourceSnapshot(
            cpu_percent=cpu_percent,
            ram_total_mb=ram_total_mb,
            ram_available_mb=ram_available_mb,
            ram_percent=ram_percent,
            swap_used_mb=swap_used_mb,
            swap_percent=swap_percent,
            swap_growth_mb=swap_growth,
            disk_free_mb=disk_free_mb,
            disk_percent=disk_percent,
            disk_free_ratio=disk_free_ratio,
            psi_cpu_some=psi_cpu,
            psi_memory_some=psi_mem,
            psi_io_some=psi_io,
            gpu_available=gpu_available,
            vram_total_mb=vram_total_mb,
            vram_used_mb=vram_used_mb,
            vram_free_mb=vram_free_mb,
            gpu_utilization_pct=gpu_util,
            battery_percent=battery_percent,
            battery_power_plugged=battery_power_plugged,
            thermal_max_celsius=thermal_max_celsius,
            thermal_throttle=thermal_max_celsius is not None and thermal_max_celsius >= thermal_hard,
            lid_closed=lid_closed,
            pressure_level=pressure,
            pressure_reasons=reasons,
            active_activities=active_activities,
            active_leases=active_leases,
        )
