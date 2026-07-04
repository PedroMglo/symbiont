"""Lightweight resource checks for conservative lifecycle work."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ResourceSnapshot:
    cpu_cores: int
    available_memory_bytes: int
    disk_free_bytes: int
    disk_total_bytes: int

    @property
    def disk_free_ratio(self) -> float:
        if self.disk_total_bytes <= 0:
            return 0.0
        return self.disk_free_bytes / self.disk_total_bytes


def current_resource_snapshot(path: Path) -> ResourceSnapshot:
    usage_path = path if path.exists() else path.parent
    while not usage_path.exists() and usage_path != usage_path.parent:
        usage_path = usage_path.parent
    disk = shutil.disk_usage(usage_path)
    return ResourceSnapshot(
        cpu_cores=os.cpu_count() or 1,
        available_memory_bytes=_available_memory_bytes(),
        disk_free_bytes=disk.free,
        disk_total_bytes=disk.total,
    )


def _available_memory_bytes() -> int:
    meminfo = Path("/proc/meminfo")
    if meminfo.exists():
        for line in meminfo.read_text(encoding="utf-8").splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    return 2 * 1024 * 1024 * 1024


class ResourceGuard:
    def __init__(self, disk_free_safety_ratio: float) -> None:
        self.disk_free_safety_ratio = disk_free_safety_ratio

    def should_pause(self, snapshot: ResourceSnapshot) -> tuple[bool, str]:
        if snapshot.disk_free_ratio < self.disk_free_safety_ratio:
            return True, "disk_free_below_safety_ratio"
        return False, "resources_ok"

