"""Resource Governor — background monitor with graduated throttle levels.

Provides a single ``ResourceGovernor`` that runs a lightweight psutil
probe every *interval* seconds and exposes ``check()`` which returns the
current ``GovernorAction``.  The pipeline queries this instead of
re-sampling system resources on every batch.

Five thresholds drive the decision (ordered least → most severe):

  max_memory_percent   → THROTTLE  (reduce batch sizes)
  pause_memory_percent → REDUCE    (lower concurrency + batches)
  abort_memory_percent → ABORT     (fatal, stop pipeline)

Swap monitoring adds another layer:

  max_swap_percent     → REDUCE
  pause_swap_percent   → PAUSE
  abort_swap_percent   → ABORT
  swap delta > 500 MB  → THROTTLE (swap storm early warning)

CPU-only pressure triggers THROTTLE; disk-full always triggers ABORT.

An optional JSONL metrics file records every sample for post-mortem
analysis.
"""

from __future__ import annotations

import json
import logging
import shutil
import threading
import time
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import IO, TYPE_CHECKING

import psutil

if TYPE_CHECKING:
    from rag_config import PerformanceConfig

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# PSI helpers (Linux 4.20+)
# ---------------------------------------------------------------------------

def _parse_psi_line(line: str) -> dict[str, float]:
    """Parse a single PSI line like 'some avg10=0.50 avg60=0.20 avg300=0.05 total=12345'."""
    parts = line.strip().split()
    result: dict[str, float] = {}
    for part in parts[1:]:  # skip 'some'/'full'
        if "=" in part:
            k, v = part.split("=", 1)
            try:
                result[k] = float(v)
            except ValueError:
                pass
    return result


def _read_psi(resource: str) -> dict[str, dict[str, float]]:
    """Read /proc/pressure/<resource> and return {some: {...}, full: {...}}.

    Returns empty dicts on non-Linux or kernels < 4.20.
    """
    result: dict[str, dict[str, float]] = {}
    try:
        with open(f"/proc/pressure/{resource}") as f:
            for line in f:
                line = line.strip()
                if line.startswith("some "):
                    result["some"] = _parse_psi_line(line)
                elif line.startswith("full "):
                    result["full"] = _parse_psi_line(line)
    except (FileNotFoundError, PermissionError, OSError):
        pass
    return result


# ---------------------------------------------------------------------------
# VRAM helpers (nvidia-ml-py, optional)
# ---------------------------------------------------------------------------

def _read_vram() -> tuple[float, float, float]:
    """Return (used_gb, total_gb, percent) for GPU 0, or (0, 0, 0) if unavailable."""
    try:
        import pynvml  # nvidia-ml-py
        pynvml.nvmlInit()
        handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        info = pynvml.nvmlDeviceGetMemoryInfo(handle)
        used = info.used / (1024 ** 3)
        total = info.total / (1024 ** 3)
        pct = (info.used / info.total * 100) if info.total > 0 else 0.0
        return round(used, 2), round(total, 2), round(pct, 1)
    except Exception:
        return 0.0, 0.0, 0.0


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

class GovernorAction(Enum):
    """What the pipeline should do right now."""
    CONTINUE = auto()   # all good
    THROTTLE = auto()   # reduce batch sizes only
    REDUCE = auto()     # lower concurrency / smaller batches
    PAUSE = auto()      # wait until resources free up
    ABORT = auto()      # fatal — stop immediately


@dataclass(frozen=True)
class ResourceSnapshot:
    """Point-in-time system resource readings."""
    ram_percent: float
    ram_available_gb: float
    cpu_percent: float
    disk_free_gb: float
    swap_percent: float
    swap_used_gb: float
    timestamp: float        # time.monotonic()
    # PSI — Pressure Stall Information (Linux 4.20+)
    psi_memory_full_avg10: float = 0.0   # % time ALL tasks stalled on memory (10s window)
    psi_io_full_avg10: float = 0.0       # % time ALL tasks stalled on I/O (10s window)
    psi_cpu_some_avg10: float = 0.0      # % time SOME tasks stalled on CPU (10s window)
    # VRAM (nvidia-ml-py, optional)
    vram_used_gb: float = 0.0
    vram_total_gb: float = 0.0
    vram_percent: float = 0.0


# ---------------------------------------------------------------------------
# Governor
# ---------------------------------------------------------------------------

class ResourceGovernor:
    """Background resource monitor with graduated throttle levels.

    Usage::

        gov = ResourceGovernor(perf, data_dir="/path/to/data")
        gov.start()
        try:
            action = gov.check()
            if action is GovernorAction.PAUSE:
                gov.wait_until_safe(timeout=60)
            ...
        finally:
            gov.stop()

    The monitor thread runs every *interval* seconds (default 1 s) and
    updates an internal snapshot.  ``check()`` reads the latest snapshot
    without blocking on I/O — it is safe to call from hot loops.
    """

    def __init__(
        self,
        perf: "PerformanceConfig",
        *,
        data_dir: str | Path | None = None,
        interval: float = 1.0,
        metrics_path: str | Path | None = None,
    ) -> None:
        self._perf = perf
        self._data_dir = str(data_dir) if data_dir else None
        self._interval = max(0.25, interval)
        self._metrics_path = Path(metrics_path) if metrics_path else None

        # Latest snapshot (written by monitor, read by check())
        self._snapshot: ResourceSnapshot | None = None
        self._prev_swap_used: float | None = None  # for swap delta tracking
        self._swap_delta_gb: float = 0.0            # GB change since last sample
        self._lock = threading.Lock()

        # Monitor lifecycle
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

        # Metrics file handle (opened lazily)
        self._metrics_fh: IO[str] | None = None

    # -- lifecycle --

    def start(self) -> None:
        """Start the background monitor thread."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        # Take an initial sample synchronously so check() works immediately
        self._sample()
        self._thread = threading.Thread(
            target=self._monitor_loop,
            name="resource-governor",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Signal the monitor thread to stop and wait for it."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        if self._metrics_fh is not None:
            try:
                self._metrics_fh.close()
            except Exception:
                pass
            self._metrics_fh = None

    # -- public API --

    def check(self) -> GovernorAction:
        """Return the recommended action based on the latest snapshot.

        This is a non-blocking read — no system calls.
        """
        with self._lock:
            snap = self._snapshot

        if snap is None:
            return GovernorAction.CONTINUE

        return self._evaluate(snap)

    def snapshot(self) -> ResourceSnapshot | None:
        """Return the most recent snapshot (or None if not started)."""
        with self._lock:
            return self._snapshot

    def wait_until_safe(self, timeout: float = 60.0) -> GovernorAction:
        """Block until the action is CONTINUE, THROTTLE, or REDUCE, or *timeout* expires.

        Returns the final action after waiting.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            action = self.check()
            if action in (GovernorAction.CONTINUE, GovernorAction.THROTTLE, GovernorAction.REDUCE):
                return action
            if action is GovernorAction.ABORT:
                return action
            # PAUSE — keep waiting
            time.sleep(self._interval)
        return self.check()

    # -- internals --

    def _monitor_loop(self) -> None:
        """Background loop — samples resources every *interval* seconds."""
        while not self._stop_event.is_set():
            try:
                self._sample()
            except Exception as exc:
                log.debug("Governor sample error: %s", exc)
            self._stop_event.wait(self._interval)

    def _sample(self) -> None:
        """Take a single resource snapshot and store it."""
        mem = psutil.virtual_memory()
        try:
            cpu = psutil.cpu_percent(interval=None)
        except Exception:
            cpu = 0.0

        disk_free = 0.0
        if self._data_dir:
            # Walk up to the nearest existing ancestor (data_dir may not exist on
            # first run — don't treat a missing dir as disk-full).
            target = Path(self._data_dir)
            while not target.exists() and target != target.parent:
                target = target.parent
            try:
                du = shutil.disk_usage(str(target))
                disk_free = du.free / (1024 ** 3)
            except Exception:
                pass

        # Swap monitoring
        try:
            swap = psutil.swap_memory()
            swap_percent = swap.percent
            swap_used_gb = round(swap.used / (1024 ** 3), 2)
        except Exception:
            swap_percent = 0.0
            swap_used_gb = 0.0

        # Swap delta tracking (detect swap storms)
        with self._lock:
            if self._prev_swap_used is not None:
                self._swap_delta_gb = swap_used_gb - self._prev_swap_used
            else:
                self._swap_delta_gb = 0.0
            self._prev_swap_used = swap_used_gb

        # PSI — Pressure Stall Information (best early warning for stalls)
        psi_mem = _read_psi("memory")
        psi_io = _read_psi("io")
        psi_cpu = _read_psi("cpu")

        psi_memory_full_avg10 = psi_mem.get("full", {}).get("avg10", 0.0)
        psi_io_full_avg10 = psi_io.get("full", {}).get("avg10", 0.0)
        psi_cpu_some_avg10 = psi_cpu.get("some", {}).get("avg10", 0.0)

        # VRAM (optional — nvidia-ml-py)
        vram_used, vram_total, vram_pct = _read_vram()

        snap = ResourceSnapshot(
            ram_percent=mem.percent,
            ram_available_gb=round(mem.available / (1024 ** 3), 2),
            cpu_percent=cpu,
            disk_free_gb=round(disk_free, 2),
            swap_percent=swap_percent,
            swap_used_gb=swap_used_gb,
            timestamp=time.monotonic(),
            psi_memory_full_avg10=psi_memory_full_avg10,
            psi_io_full_avg10=psi_io_full_avg10,
            psi_cpu_some_avg10=psi_cpu_some_avg10,
            vram_used_gb=vram_used,
            vram_total_gb=vram_total,
            vram_percent=vram_pct,
        )

        with self._lock:
            self._snapshot = snap

        self._emit_metrics(snap)

    def _evaluate(self, snap: ResourceSnapshot) -> GovernorAction:
        """Decide action from a snapshot and the configured thresholds.

        Evaluation order (most severe first):
          1. Disk full           → ABORT
          2. RAM abort threshold → ABORT
          3. Swap abort (>=80%)  → ABORT
          4. Swap pause (>=60%)  → PAUSE
          5. RAM pause threshold → PAUSE
          6. Swap reduce (>=40%) → REDUCE
          7. RAM reduce threshold→ REDUCE
          8. Swap storm (delta)  → THROTTLE
          9. CPU pressure        → THROTTLE
          10. else               → CONTINUE
        """
        # Disk-full is always fatal
        if self._data_dir and snap.disk_free_gb < 1.0:
            return GovernorAction.ABORT

        # RAM thresholds (ordered most severe → least severe)
        if snap.ram_percent >= self._perf.abort_memory_percent:
            return GovernorAction.ABORT

        # Swap thresholds
        swap_abort = getattr(self._perf, "abort_swap_percent", 80)
        swap_pause = getattr(self._perf, "pause_swap_percent", 60)
        swap_reduce = getattr(self._perf, "max_swap_percent", 40)

        if snap.swap_percent >= swap_abort:
            return GovernorAction.ABORT
        if snap.swap_percent >= swap_pause:
            return GovernorAction.PAUSE

        if snap.ram_percent >= self._perf.pause_memory_percent:
            return GovernorAction.PAUSE

        if snap.swap_percent >= swap_reduce:
            return GovernorAction.REDUCE
        if snap.ram_percent >= self._perf.max_memory_percent:
            return GovernorAction.REDUCE

        # Swap storm detection: >0.5 GB increase between samples
        with self._lock:
            swap_delta = self._swap_delta_gb
        if swap_delta > 0.5:
            return GovernorAction.THROTTLE

        # PSI — Pressure Stall Information (early warning, before RAM% thresholds)
        # memory full avg10 > 10% = tasks are actively stalling on memory
        if snap.psi_memory_full_avg10 > 25.0:
            return GovernorAction.PAUSE
        if snap.psi_memory_full_avg10 > 10.0:
            return GovernorAction.REDUCE
        # I/O full avg10 > 20% = storage bottleneck (heavy swap or slow disk)
        if snap.psi_io_full_avg10 > 40.0:
            return GovernorAction.PAUSE
        if snap.psi_io_full_avg10 > 20.0:
            return GovernorAction.REDUCE
        # memory/io pressure starting = throttle
        if snap.psi_memory_full_avg10 > 5.0 or snap.psi_io_full_avg10 > 10.0:
            return GovernorAction.THROTTLE

        # CPU-only pressure → throttle
        if snap.cpu_percent > self._perf.max_cpu_percent + 10:
            return GovernorAction.THROTTLE
        if snap.psi_cpu_some_avg10 > 50.0:
            return GovernorAction.THROTTLE

        return GovernorAction.CONTINUE

    # -- metrics --

    def _emit_metrics(self, snap: ResourceSnapshot) -> None:
        """Append a JSONL line to the metrics file (if configured)."""
        if self._metrics_path is None:
            return
        try:
            if self._metrics_fh is None:
                self._metrics_path.parent.mkdir(parents=True, exist_ok=True)
                self._metrics_fh = open(self._metrics_path, "a", encoding="utf-8")  # noqa: SIM115

            record = {
                "ts": time.time(),
                "ram_pct": snap.ram_percent,
                "ram_avail_gb": snap.ram_available_gb,
                "cpu_pct": snap.cpu_percent,
                "disk_free_gb": snap.disk_free_gb,
                "swap_pct": snap.swap_percent,
                "swap_used_gb": snap.swap_used_gb,
                "swap_delta_gb": round(self._swap_delta_gb, 3),
                "psi_mem_full10": snap.psi_memory_full_avg10,
                "psi_io_full10": snap.psi_io_full_avg10,
                "psi_cpu_some10": snap.psi_cpu_some_avg10,
                "vram_used_gb": snap.vram_used_gb,
                "vram_pct": snap.vram_percent,
                "action": self._evaluate(snap).name,
            }
            self._metrics_fh.write(json.dumps(record) + "\n")
            self._metrics_fh.flush()
        except Exception as exc:
            log.debug("Metrics write error: %s", exc)
