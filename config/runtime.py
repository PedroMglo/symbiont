"""Runtime probes for CPU, RAM, GPU, storage, filesystem and Docker."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DOCKER_CONTEXT = "default"
GPU_TEST_IMAGE = "nvidia/cuda:12.4.1-runtime-ubuntu22.04"


@dataclass(frozen=True)
class RuntimeInfo:
    cpu_threads: int
    ram_total_gb: float | None
    ram_available_gb: float | None
    gpu_available: bool
    gpu_name: str | None
    vram_total_gb: float | None
    vram_used_gb: float | None
    vram_free_gb: float | None
    storage_root: Path | None
    storage_exists: bool
    storage_mounted: bool
    storage_filesystem: str | None
    storage_writable: bool
    docker_available: bool
    docker_context: str | None = None
    battery_percent: float | None = None
    battery_power_plugged: bool | None = None
    thermal_max_celsius: float | None = None
    thermal_throttle: bool = False
    lid_closed: bool | None = None


def _run(cmd: list[str], timeout: float = 2.0) -> str | None:
    try:
        result = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _docker_context_name() -> str:
    return os.environ.get("AI_LOCAL_DOCKER_CONTEXT") or os.environ.get("DOCKER_CONTEXT") or DEFAULT_DOCKER_CONTEXT


def _docker_cmd(*args: str) -> list[str]:
    return ["docker", "--context", _docker_context_name(), *args]


def _memory_gb() -> tuple[float | None, float | None]:
    meminfo = Path("/proc/meminfo")
    if meminfo.exists():
        values: dict[str, int] = {}
        for line in meminfo.read_text(encoding="utf-8", errors="ignore").splitlines():
            key, _, rest = line.partition(":")
            parts = rest.strip().split()
            if parts and parts[0].isdigit():
                values[key] = int(parts[0])
        total = values.get("MemTotal")
        available = values.get("MemAvailable")
        return (
            round(total / 1024 / 1024, 2) if total else None,
            round(available / 1024 / 1024, 2) if available else None,
        )
    return None, None


def _gpu() -> tuple[bool, str | None, float | None, float | None, float | None]:
    if not shutil.which("nvidia-smi"):
        return False, None, None, None, None
    out = _run([
        "nvidia-smi",
        "--query-gpu=name,memory.total,memory.used,memory.free",
        "--format=csv,noheader,nounits",
    ])
    if not out:
        return False, None, None, None, None
    first = out.splitlines()[0]
    parts = [p.strip() for p in first.split(",")]
    if len(parts) < 4:
        return False, None, None, None, None
    try:
        total = round(float(parts[1]) / 1024, 2)
        used = round(float(parts[2]) / 1024, 2)
        free = round(float(parts[3]) / 1024, 2)
    except ValueError:
        return False, None, None, None, None
    return True, parts[0], total, used, free


def _docker_gpu() -> tuple[bool, str | None, float | None, float | None, float | None]:
    if not shutil.which("docker"):
        return False, None, None, None, None
    image = os.environ.get("AI_LOCAL_GPU_TEST_IMAGE", GPU_TEST_IMAGE)
    out = _run(
        _docker_cmd(
            "run",
            "--rm",
            "--pull",
            "never",
            "--gpus",
            "all",
            "--entrypoint",
            "nvidia-smi",
            image,
            "--query-gpu=name,memory.total,memory.used,memory.free",
            "--format=csv,noheader,nounits",
        ),
        timeout=15.0,
    )
    if not out:
        return False, None, None, None, None
    first = out.splitlines()[0]
    parts = [p.strip() for p in first.split(",")]
    if len(parts) < 4:
        return False, None, None, None, None
    try:
        total = round(float(parts[1]) / 1024, 2)
        used = round(float(parts[2]) / 1024, 2)
        free = round(float(parts[3]) / 1024, 2)
    except ValueError:
        return False, None, None, None, None
    return True, parts[0], total, used, free


def _storage(path: Path | None) -> tuple[bool, bool, str | None, bool]:
    if path is None:
        return False, False, None, False
    exists = path.is_dir()
    writable = os.access(path, os.W_OK) if exists else False
    out = _run(["findmnt", "-T", str(path), "-n", "-o", "SOURCE,FSTYPE"], timeout=1.5) if exists else None
    filesystem = None
    if out:
        candidates: list[tuple[str, str]] = []
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 2:
                candidates.append((parts[0], parts[1]))
        preferred = next(((src, fs) for src, fs in candidates if src.startswith("/dev/") and fs != "autofs"), None)
        if preferred is None:
            preferred = next(((src, fs) for src, fs in candidates if fs != "autofs"), None)
        if preferred is None and candidates:
            preferred = candidates[0]
        filesystem = preferred[1] if preferred else None
    mounted = bool(filesystem)
    return exists, mounted, filesystem, writable


def _docker_available(enabled: bool) -> bool:
    if not enabled or not shutil.which("docker"):
        return False
    out = _run(_docker_cmd("info", "--format", "{{.ServerVersion}}"), timeout=2.0)
    return bool(out)


def _docker_context(enabled: bool) -> str | None:
    if not enabled or not shutil.which("docker"):
        return None
    return _docker_context_name()


def _battery() -> tuple[float | None, bool | None]:
    try:
        import psutil

        battery = psutil.sensors_battery()
    except Exception:
        return None, None
    if battery is None:
        return None, None
    return float(battery.percent), bool(battery.power_plugged)


def _thermal_max_celsius() -> float | None:
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
    return round(max(values), 1) if values else None


def _lid_closed() -> bool | None:
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


def probe_runtime(storage_root: Path | None, *, docker_probe: bool = True, force_gpu: bool | None = None) -> RuntimeInfo:
    total, available = _memory_gb()
    gpu_available, gpu_name, vram_total, vram_used, vram_free = _gpu()
    docker_gpu_probe = os.environ.get("AI_RUNTIME_DOCKER_GPU_PROBE", "true").lower() not in {"0", "false", "no"}
    if not gpu_available and docker_probe and docker_gpu_probe:
        gpu_available, gpu_name, vram_total, vram_used, vram_free = _docker_gpu()
    if force_gpu is False:
        gpu_available, gpu_name, vram_total, vram_used, vram_free = False, None, None, None, None
    elif force_gpu is True and not gpu_available:
        gpu_available = True
    storage_exists, storage_mounted, storage_fs, storage_writable = _storage(storage_root)
    docker_available = _docker_available(docker_probe)
    battery_percent, battery_power_plugged = _battery()
    thermal_max_celsius = _thermal_max_celsius()
    return RuntimeInfo(
        cpu_threads=os.cpu_count() or 1,
        ram_total_gb=total,
        ram_available_gb=available,
        gpu_available=gpu_available,
        gpu_name=gpu_name,
        vram_total_gb=vram_total,
        vram_used_gb=vram_used,
        vram_free_gb=vram_free,
        storage_root=storage_root,
        storage_exists=storage_exists,
        storage_mounted=storage_mounted,
        storage_filesystem=storage_fs,
        storage_writable=storage_writable,
        docker_available=docker_available,
        docker_context=_docker_context(docker_probe) if docker_available else None,
        battery_percent=battery_percent,
        battery_power_plugged=battery_power_plugged,
        thermal_max_celsius=thermal_max_celsius,
        thermal_throttle=thermal_max_celsius is not None and thermal_max_celsius >= 92,
        lid_closed=_lid_closed(),
    )
