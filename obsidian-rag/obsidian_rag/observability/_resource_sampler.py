"""Background resource sampler for infrastructure metrics.

Periodically samples CPU, RAM, swap, disk, and GPU metrics,
emitting RESOURCE_SAMPLE events to the observability dispatcher.
"""

from __future__ import annotations

import logging
import threading

log = logging.getLogger(__name__)

_thread: threading.Thread | None = None
_stop_event = threading.Event()


def start_sampler(interval: float = 5.0) -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop_event.clear()
    _thread = threading.Thread(
        target=_sample_loop, args=(interval,), name="obs-resource-sampler", daemon=True
    )
    _thread.start()


def stop_sampler() -> None:
    global _thread
    _stop_event.set()
    if _thread and _thread.is_alive():
        _thread.join(timeout=3.0)
    _thread = None


def _sample_loop(interval: float) -> None:
    while not _stop_event.is_set():
        try:
            _emit_sample()
        except Exception as exc:
            log.debug("Resource sample failed: %s", exc)
        _stop_event.wait(timeout=interval)


def _emit_sample() -> None:
    import shutil

    import psutil

    from . import emit
    from .events import EventName, RAGEvent

    cpu = psutil.cpu_percent(interval=None)
    mem = psutil.virtual_memory()
    swap = psutil.swap_memory()
    disk = shutil.disk_usage("/")

    vram_used = 0.0
    vram_pct = 0.0
    try:
        import subprocess

        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            parts = result.stdout.strip().split(",")
            if len(parts) == 2:
                used_mb = float(parts[0].strip())
                total_mb = float(parts[1].strip())
                vram_used = used_mb / 1024.0
                vram_pct = (used_mb / total_mb * 100.0) if total_mb > 0 else 0.0
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
        pass

    psi_mem = 0.0
    psi_io = 0.0
    try:
        with open("/proc/pressure/memory") as f:
            for line in f:
                if line.startswith("full"):
                    for part in line.split():
                        if part.startswith("avg10="):
                            psi_mem = float(part.split("=")[1])
                            break
                    break
        with open("/proc/pressure/io") as f:
            for line in f:
                if line.startswith("full"):
                    for part in line.split():
                        if part.startswith("avg10="):
                            psi_io = float(part.split("=")[1])
                            break
                    break
    except (FileNotFoundError, ValueError, OSError):
        pass

    active_ingest = False
    try:
        for proc in psutil.process_iter(["cmdline"]):
            cmdline = proc.info.get("cmdline") or []
            if any("rag" in arg and "sync" in arg for arg in cmdline):
                active_ingest = True
                break
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass

    emit(RAGEvent(
        event=EventName.RESOURCE_SAMPLE,
        cpu_percent=cpu,
        ram_percent=mem.percent,
        ram_available_gb=round(mem.available / (1024**3), 2),
        swap_percent=swap.percent,
        disk_free_gb=round(disk.free / (1024**3), 2),
        vram_used_gb=round(vram_used, 2),
        vram_percent=round(vram_pct, 1),
        psi_memory_full_avg10=psi_mem,
        psi_io_full_avg10=psi_io,
        active_ingest=active_ingest,
    ))
