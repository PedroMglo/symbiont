"""Hardware auto-detection — probes machine resources at startup.

Provides a frozen HardwareProfile dataclass with CPU, RAM, GPU/VRAM, and disk
type information. Used by adaptive_config to derive optimal runtime parameters.
"""

from __future__ import annotations

import glob
import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

log = logging.getLogger(__name__)


class DiskType(str, Enum):
    NVME = "nvme"
    SSD = "ssd"
    HDD = "hdd"
    UNKNOWN = "unknown"


class GPUVendor(str, Enum):
    NVIDIA = "nvidia"
    AMD = "amd"
    NONE = "none"


@dataclass(frozen=True)
class GPUInfo:
    """GPU hardware information."""

    available: bool = False
    vendor: GPUVendor = GPUVendor.NONE
    name: str = ""
    vram_total_mb: int = 0
    vram_free_mb: int = 0
    vram_used_mb: int = 0
    gpu_count: int = 0
    compute_capability: str = ""


@dataclass(frozen=True)
class CPUInfo:
    """CPU hardware information."""

    physical_cores: int = 1
    logical_cores: int = 1
    architecture: str = ""
    model_name: str = ""


@dataclass(frozen=True)
class RAMInfo:
    """RAM information."""

    total_mb: int = 0
    available_mb: int = 0
    used_mb: int = 0
    swap_total_mb: int = 0
    swap_used_mb: int = 0
    percent_used: float = 0.0


@dataclass(frozen=True)
class DiskInfo:
    """Primary disk information."""

    disk_type: DiskType = DiskType.UNKNOWN
    total_gb: float = 0.0
    free_gb: float = 0.0
    mount_point: str = "/"


@dataclass(frozen=True)
class OllamaInfo:
    """Ollama runtime state (models loaded, available)."""

    available: bool = False
    loaded_models: tuple[str, ...] = ()
    loaded_vram_mb: int = 0
    accelerated: bool = False
    accelerated_models: tuple[str, ...] = ()
    processor_hints: tuple[str, ...] = ()
    available_models: tuple[str, ...] = ()


@dataclass(frozen=True)
class HardwareProfile:
    """Complete machine hardware profile — detected at startup."""

    cpu: CPUInfo = field(default_factory=CPUInfo)
    ram: RAMInfo = field(default_factory=RAMInfo)
    gpu: GPUInfo = field(default_factory=GPUInfo)
    disk: DiskInfo = field(default_factory=DiskInfo)
    ollama: OllamaInfo = field(default_factory=OllamaInfo)
    detected_at: float = 0.0

    @property
    def has_gpu(self) -> bool:
        return self.gpu.available and self.gpu.vram_total_mb > 0

    @property
    def vram_free_mb(self) -> int:
        return self.gpu.vram_free_mb if self.has_gpu else 0

    @property
    def is_ram_constrained(self) -> bool:
        """True if total RAM <= 8GB."""
        return self.ram.total_mb <= 8192

    @property
    def is_vram_constrained(self) -> bool:
        """True if VRAM <= 6GB."""
        return not self.has_gpu or self.gpu.vram_total_mb <= 6144

    def summary(self) -> dict[str, Any]:
        """Human-readable summary for CLI/API."""
        return {
            "cpu": {
                "physical_cores": self.cpu.physical_cores,
                "logical_cores": self.cpu.logical_cores,
                "architecture": self.cpu.architecture,
                "model": self.cpu.model_name,
            },
            "ram": {
                "total_mb": self.ram.total_mb,
                "available_mb": self.ram.available_mb,
                "percent_used": self.ram.percent_used,
                "swap_total_mb": self.ram.swap_total_mb,
                "swap_used_mb": self.ram.swap_used_mb,
            },
            "gpu": {
                "available": self.gpu.available,
                "vendor": self.gpu.vendor.value,
                "name": self.gpu.name,
                "vram_total_mb": self.gpu.vram_total_mb,
                "vram_free_mb": self.gpu.vram_free_mb,
                "vram_used_mb": self.gpu.vram_used_mb,
                "gpu_count": self.gpu.gpu_count,
            },
            "disk": {
                "type": self.disk.disk_type.value,
                "total_gb": round(self.disk.total_gb, 1),
                "free_gb": round(self.disk.free_gb, 1),
                "mount_point": self.disk.mount_point,
            },
            "ollama": {
                "available": self.ollama.available,
                "loaded_models": list(self.ollama.loaded_models),
                "loaded_vram_mb": self.ollama.loaded_vram_mb,
                "accelerated": self.ollama.accelerated,
                "accelerated_models": list(self.ollama.accelerated_models),
                "processor_hints": list(self.ollama.processor_hints),
            },
            "detected_at": self.detected_at,
        }


# ---------------------------------------------------------------------------
# Detection functions
# ---------------------------------------------------------------------------


def _detect_cpu() -> CPUInfo:
    """Detect CPU information via os/psutil."""
    logical = os.cpu_count() or 1
    physical = logical
    arch = ""
    model_name = ""

    try:
        import psutil
        physical = psutil.cpu_count(logical=False) or logical
    except ImportError:
        pass

    # Get architecture
    try:
        result = subprocess.run(
            ["uname", "-m"], capture_output=True, text=True, timeout=3
        )
        if result.returncode == 0:
            arch = result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Get CPU model name from /proc/cpuinfo (Linux)
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("model name"):
                    model_name = line.split(":", 1)[1].strip()
                    break
    except (FileNotFoundError, OSError):
        pass

    return CPUInfo(
        physical_cores=physical,
        logical_cores=logical,
        architecture=arch,
        model_name=model_name,
    )


def _detect_ram() -> RAMInfo:
    """Detect RAM via psutil."""
    try:
        import psutil
        mem = psutil.virtual_memory()
        swap = psutil.swap_memory()
        return RAMInfo(
            total_mb=int(mem.total / (1024 * 1024)),
            available_mb=int(mem.available / (1024 * 1024)),
            used_mb=int(mem.used / (1024 * 1024)),
            swap_total_mb=int(swap.total / (1024 * 1024)),
            swap_used_mb=int(swap.used / (1024 * 1024)),
            percent_used=mem.percent,
        )
    except ImportError:
        # Fallback: parse free command
        try:
            result = subprocess.run(
                ["free", "-m"], capture_output=True, text=True, timeout=3
            )
            if result.returncode == 0:
                lines = result.stdout.strip().split("\n")
                # Mem: total used free shared buff/cache available
                if len(lines) >= 2:
                    parts = lines[1].split()
                    total = int(parts[1])
                    used = int(parts[2])
                    available = int(parts[6]) if len(parts) > 6 else total - used
                    swap_total = swap_used = 0
                    if len(lines) >= 3 and "Swap" in lines[2]:
                        swap_parts = lines[2].split()
                        swap_total = int(swap_parts[1])
                        swap_used = int(swap_parts[2])
                    return RAMInfo(
                        total_mb=total,
                        available_mb=available,
                        used_mb=used,
                        swap_total_mb=swap_total,
                        swap_used_mb=swap_used,
                        percent_used=round(used / total * 100, 1) if total > 0 else 0,
                    )
        except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
            pass
    return RAMInfo()


def _detect_gpu() -> GPUInfo:
    """Detect GPU via nvidia-smi."""
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,memory.used,memory.free,count",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return _detect_gpu_from_procfs()

        lines = result.stdout.strip().split("\n")
        if not lines:
            return _detect_gpu_from_procfs()

        # Parse first GPU (primary)
        parts = [p.strip() for p in lines[0].split(",")]
        if len(parts) < 4:
            return _detect_gpu_from_procfs()

        name = parts[0]
        vram_total = int(float(parts[1]))
        vram_used = int(float(parts[2]))
        vram_free = int(float(parts[3]))
        gpu_count = int(parts[4]) if len(parts) > 4 else len(lines)

        return GPUInfo(
            available=True,
            vendor=GPUVendor.NVIDIA,
            name=name,
            vram_total_mb=vram_total,
            vram_free_mb=vram_free,
            vram_used_mb=vram_used,
            gpu_count=gpu_count,
        )
    except FileNotFoundError:
        return _detect_gpu_from_procfs()
    except (subprocess.TimeoutExpired, ValueError, IndexError) as exc:
        log.debug("GPU detection failed: %s", exc)
    return _detect_gpu_from_procfs()


def _detect_gpu_from_procfs() -> GPUInfo:
    """Detect a container-visible NVIDIA GPU when nvidia-smi is unavailable."""
    for info_path in glob.glob("/proc/driver/nvidia/gpus/*/information"):
        try:
            fields: dict[str, str] = {}
            with open(info_path, encoding="utf-8") as fh:
                for line in fh:
                    if ":" not in line:
                        continue
                    key, value = line.split(":", 1)
                    fields[key.strip().lower()] = value.strip()
            name = fields.get("model") or "NVIDIA GPU"
            return GPUInfo(
                available=True,
                vendor=GPUVendor.NVIDIA,
                name=name,
                gpu_count=1,
            )
        except OSError as exc:
            log.debug("GPU procfs detection failed for %s: %s", info_path, exc)
    for dev_path in ("/dev/nvidia0", "/dev/dxg"):
        if os.path.exists(dev_path):
            return GPUInfo(
                available=True,
                vendor=GPUVendor.NVIDIA if "nvidia" in dev_path else GPUVendor.NONE,
                name="NVIDIA GPU" if "nvidia" in dev_path else "GPU device",
                gpu_count=1,
            )
    return GPUInfo()


def _detect_disk() -> DiskInfo:
    """Detect primary disk type (NVMe/SSD/HDD) via lsblk."""
    disk_type = DiskType.UNKNOWN
    total_gb = 0.0
    free_gb = 0.0

    # Get disk usage
    try:
        import shutil
        usage = shutil.disk_usage("/")
        total_gb = usage.total / (1024 ** 3)
        free_gb = usage.free / (1024 ** 3)
    except OSError:
        pass

    # Detect disk type via lsblk
    try:
        result = subprocess.run(
            ["lsblk", "-o", "NAME,ROTA,TYPE", "-J"],
            capture_output=True, text=True, timeout=3,
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            for device in data.get("blockdevices", []):
                if device.get("type") != "disk":
                    continue
                name = device.get("name", "")
                rota = device.get("rota")
                if rota is not None:
                    if rota == "0" or rota is False or rota == 0:
                        # Non-rotational: NVMe or SSD
                        disk_type = DiskType.NVME if "nvme" in name else DiskType.SSD
                    else:
                        disk_type = DiskType.HDD
                    break
    except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError, ValueError):
        pass

    return DiskInfo(
        disk_type=disk_type,
        total_gb=total_gb,
        free_gb=free_gb,
        mount_point="/",
    )


def _detect_ollama(base_url: str) -> OllamaInfo:
    """Detect Ollama runtime state."""
    try:
        import httpx

        # Check loaded models
        loaded_models: list[str] = []
        accelerated_models: list[str] = []
        processor_hints: list[str] = []
        loaded_vram = 0
        try:
            resp = httpx.get(f"{base_url}/api/ps", timeout=3.0)
            if resp.status_code == 200:
                data = resp.json()
                for model in data.get("models", []):
                    name = str(model.get("name", "") or "")
                    loaded_models.append(name)

                    size_vram = int(model.get("size_vram", 0) or 0)
                    loaded_vram += size_vram // (1024 * 1024)

                    raw_hint = model.get("processor") or model.get("processors")
                    if raw_hint is None and isinstance(model.get("details"), dict):
                        raw_hint = model["details"].get("processor")
                    hint = str(raw_hint or "").strip()
                    if hint:
                        processor_hints.append(hint)

                    hint_lower = hint.lower()
                    has_gpu_hint = "gpu" in hint_lower or "cuda" in hint_lower
                    has_cpu_only_hint = "cpu" in hint_lower and not has_gpu_hint
                    if has_gpu_hint or (size_vram > 0 and not has_cpu_only_hint):
                        accelerated_models.append(name)
        except Exception:
            return OllamaInfo()

        # Check available models
        available_models: list[str] = []
        try:
            resp = httpx.get(f"{base_url}/api/tags", timeout=3.0)
            if resp.status_code == 200:
                data = resp.json()
                for model in data.get("models", []):
                    available_models.append(model.get("name", ""))
        except Exception:
            pass

        return OllamaInfo(
            available=True,
            loaded_models=tuple(loaded_models),
            loaded_vram_mb=loaded_vram,
            accelerated=bool(accelerated_models),
            accelerated_models=tuple(accelerated_models),
            processor_hints=tuple(processor_hints),
            available_models=tuple(available_models),
        )
    except ImportError:
        return OllamaInfo()


def detect(*, ollama_url: str) -> HardwareProfile:
    """Run full hardware detection and return a frozen HardwareProfile.

    Safe to call at any time — all probes have timeouts and graceful fallbacks.
    """
    t0 = time.perf_counter()

    cpu = _detect_cpu()
    ram = _detect_ram()
    gpu = _detect_gpu()
    disk = _detect_disk()
    ollama = _detect_ollama(ollama_url)

    elapsed_ms = (time.perf_counter() - t0) * 1000
    log.info(
        "Hardware detected in %.0fms: CPU=%d/%d cores, RAM=%dMB, "
        "GPU=%s VRAM=%dMB/%dMB, Disk=%s",
        elapsed_ms,
        cpu.physical_cores, cpu.logical_cores,
        ram.total_mb,
        gpu.name or "none", gpu.vram_free_mb, gpu.vram_total_mb,
        disk.disk_type.value,
    )

    return HardwareProfile(
        cpu=cpu,
        ram=ram,
        gpu=gpu,
        disk=disk,
        ollama=ollama,
        detected_at=time.time(),
    )


# ---------------------------------------------------------------------------
# Singleton access
# ---------------------------------------------------------------------------

_profile: HardwareProfile | None = None


def get_hardware_profile(*, force_refresh: bool = False, ollama_url: str = "") -> HardwareProfile:
    """Get or create the singleton HardwareProfile.

    If ollama_url is empty, reads from centralized config.
    """
    if not ollama_url:
        from orchestrator.config import get_settings
        ollama_url = get_settings().ollama.base_url
    """Get or create the singleton HardwareProfile."""
    global _profile
    if _profile is None or force_refresh:
        _profile = detect(ollama_url=ollama_url)
    return _profile


def _reset_profile() -> None:
    """Reset singleton — for testing."""
    global _profile
    _profile = None
