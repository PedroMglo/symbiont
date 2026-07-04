"""Resource monitor — async sampling of CPU, RAM, GPU for request correlation.

Non-blocking: uses a background thread that caches latest readings.
Can attach resource readings to events by request_id.
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from orchestrator.observability.config import ResourceMonitorConfig

if TYPE_CHECKING:
    from orchestrator.core.adaptive_config import DegradationMode

log = logging.getLogger(__name__)


@dataclass
class ResourceSnapshot:
    """Point-in-time resource reading."""

    timestamp: float = 0.0
    cpu_percent: float | None = None
    ram_used_mb: int | None = None
    ram_total_mb: int | None = None
    ram_percent: float | None = None
    gpu_util_percent: float | None = None
    vram_used_mb: int | None = None
    vram_total_mb: int | None = None
    vram_free_mb: int | None = None
    gpu_name: str | None = None
    gpu_temperature_c: float | None = None
    gpu_power_w: float | None = None
    resource_source: str = "psutil"  # "psutil" | "nvidia-smi" | "missing"


class ResourceMonitor:
    """Background resource sampler — caches latest snapshot."""

    def __init__(self, config: ResourceMonitorConfig):
        self._cfg = config
        self._latest: ResourceSnapshot = ResourceSnapshot()
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._gpu_available: bool | None = None

    @property
    def latest(self) -> ResourceSnapshot:
        """Get the most recent resource snapshot (non-blocking)."""
        with self._lock:
            return self._latest

    def start(self) -> None:
        """Start background sampling thread."""
        if not self._cfg.enabled:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._sample_loop, daemon=True, name="resource-monitor"
        )
        self._thread.start()
        log.debug("ResourceMonitor: started (interval=%.1fs)", self._cfg.sample_interval_seconds)

    def stop(self) -> None:
        """Stop sampling thread."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)

    def snapshot_now(self) -> ResourceSnapshot:
        """Take a synchronous snapshot (for correlation)."""
        return self._collect()

    def attach_to_event(self, event_dict: dict[str, Any]) -> dict[str, Any]:
        """Attach latest resource readings to an event dict."""
        snap = self.latest
        if snap.cpu_percent is not None:
            event_dict["cpu_percent"] = round(snap.cpu_percent, 1)
        if snap.ram_used_mb is not None:
            event_dict["ram_used_mb"] = snap.ram_used_mb
        if snap.gpu_util_percent is not None:
            event_dict["gpu_util_percent"] = round(snap.gpu_util_percent, 1)
        if snap.vram_used_mb is not None:
            event_dict["vram_used_mb"] = snap.vram_used_mb
        return event_dict

    def _sample_loop(self) -> None:
        """Background loop: collect snapshot at configured interval."""
        # Minimal initial delay to avoid conflicting with other startup tasks
        self._stop_event.wait(timeout=0.5)

        while not self._stop_event.is_set():
            try:
                snap = self._collect()
                with self._lock:
                    self._latest = snap
                # Check resource pressure and update degradation mode
                self._check_pressure(snap)
            except Exception as exc:
                log.debug("ResourceMonitor: sample error: %s", exc)

            self._stop_event.wait(timeout=self._cfg.sample_interval_seconds)

    def _check_pressure(self, snap: ResourceSnapshot) -> None:
        """Evaluate resource pressure and update adaptive degradation mode."""
        try:
            from orchestrator.core.adaptive_config import (
                DegradationMode,
                get_adaptive_overrides,
                update_degradation_mode,
            )
            overrides = get_adaptive_overrides()
            mode = DegradationMode.NORMAL

            # RAM pressure
            if snap.ram_percent is not None:
                if snap.ram_percent >= 95:
                    mode = DegradationMode.MINIMAL
                elif snap.ram_percent >= overrides.ram_pressure_threshold * 100:
                    mode = DegradationMode.CONSTRAINED

            # VRAM pressure
            if snap.vram_total_mb and snap.vram_used_mb:
                vram_pct = snap.vram_used_mb / snap.vram_total_mb
                if vram_pct >= 0.98:
                    mode = DegradationMode.MINIMAL
                elif vram_pct >= overrides.vram_pressure_threshold:
                    if mode == DegradationMode.NORMAL:
                        mode = DegradationMode.CONSTRAINED

            prev_mode = overrides.degradation_mode
            update_degradation_mode(mode)

            # Emit adaptation event on mode transition
            if mode != prev_mode:
                try:
                    from orchestrator.observability import emit_adaptation_event
                    trigger = "vram_pressure" if (snap.vram_total_mb and snap.vram_used_mb and snap.vram_used_mb / snap.vram_total_mb > 0.9) else "ram_pressure"
                    trigger_val = snap.ram_percent or 0
                    if "vram" in trigger:
                        trigger_val = (snap.vram_used_mb / snap.vram_total_mb * 100) if snap.vram_total_mb else 0
                    emit_adaptation_event(
                        "degradation_change",
                        prev_mode=prev_mode.value,
                        new_mode=mode.value,
                        trigger_metric=trigger,
                        trigger_value=trigger_val,
                    )
                except Exception:
                    pass

            # Trigger model eviction on VRAM pressure transition
            if mode != DegradationMode.NORMAL and prev_mode == DegradationMode.NORMAL:
                self._handle_vram_pressure(mode)

            # Trigger cache shrink on RAM pressure
            if mode != DegradationMode.NORMAL and snap.ram_percent and snap.ram_percent >= 85:
                self._handle_ram_pressure()

        except Exception:
            pass  # Non-critical — don't break monitoring

    def _handle_vram_pressure(self, mode: "DegradationMode") -> None:
        """React to VRAM pressure by evicting models."""
        try:
            from orchestrator.config import get_settings
            from orchestrator.core.warmup import get_warmup_manager

            mgr = get_warmup_manager()
            cfg = get_settings()

            if mode.value == "minimal":
                # Emergency: reduce keep_alive aggressively
                mgr.reduce_keep_alive("1m")
                try:
                    from orchestrator.observability import emit_adaptation_event
                    emit_adaptation_event("model_eviction", new_mode=mode.value, detail="reduce_keep_alive=1m")
                except Exception:
                    pass
            else:
                # Constrained: evict least-used model, protect primary
                protect = list(cfg.performance.primary_warm_models[:1])
                evicted = mgr.evict_least_used(protect_models=protect)
                if evicted:
                    log.info("ResourceMonitor: VRAM pressure — evicted %s", evicted)
                    try:
                        from orchestrator.observability import emit_adaptation_event
                        emit_adaptation_event("model_eviction", new_mode=mode.value, detail=f"evicted={evicted}")
                    except Exception:
                        pass
                else:
                    mgr.reduce_keep_alive("5m")
        except Exception as exc:
            log.debug("ResourceMonitor: eviction handler failed: %s", exc)

    def _handle_ram_pressure(self) -> None:
        """React to RAM pressure by shrinking caches."""
        try:
            from orchestrator.core.response_cache import get_response_cache
            cache = get_response_cache()
            # Shrink cache to 50% of current size
            new_max = max(100, cache._max_size // 2)
            evicted = cache.shrink(new_max)
            if evicted:
                log.info("ResourceMonitor: RAM pressure — shrunk cache by %d entries", evicted)
                try:
                    from orchestrator.observability import emit_adaptation_event
                    emit_adaptation_event(
                        "cache_shrink",
                        trigger_metric="ram_pressure",
                        detail=f"evicted={evicted} new_max={new_max}",
                    )
                except Exception:
                    pass
        except Exception:
            pass

    def _collect(self) -> ResourceSnapshot:
        """Collect a full resource snapshot."""
        snap = ResourceSnapshot(timestamp=time.time())

        if self._cfg.collect_cpu or self._cfg.collect_ram:
            self._collect_psutil(snap)

        if self._cfg.collect_gpu:
            self._collect_gpu(snap)

        return snap

    def _collect_psutil(self, snap: ResourceSnapshot) -> None:
        """Collect CPU/RAM via psutil."""
        try:
            import psutil

            if self._cfg.collect_cpu:
                snap.cpu_percent = psutil.cpu_percent(interval=0.1)

            if self._cfg.collect_ram:
                mem = psutil.virtual_memory()
                snap.ram_used_mb = int(mem.used / (1024 * 1024))
                snap.ram_total_mb = int(mem.total / (1024 * 1024))
                snap.ram_percent = mem.percent

            snap.resource_source = "psutil"
        except ImportError:
            snap.resource_source = "missing"
        except Exception as exc:
            log.debug("ResourceMonitor: psutil error: %s", exc)

    def _collect_gpu(self, snap: ResourceSnapshot) -> None:
        """Collect GPU metrics via nvidia-smi."""
        if self._gpu_available is False:
            return

        try:
            result = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=name,memory.total,memory.used,memory.free,utilization.gpu,temperature.gpu,power.draw",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=3.0,
            )
            if result.returncode != 0:
                self._gpu_available = False
                return

            self._gpu_available = True
            line = result.stdout.strip().split("\n")[0]
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 7:
                snap.gpu_name = parts[0]
                snap.vram_total_mb = int(float(parts[1]))
                snap.vram_used_mb = int(float(parts[2]))
                snap.vram_free_mb = int(float(parts[3]))
                snap.gpu_util_percent = float(parts[4])
                snap.gpu_temperature_c = float(parts[5])
                try:
                    snap.gpu_power_w = float(parts[6])
                except (ValueError, IndexError):
                    pass

        except FileNotFoundError:
            self._gpu_available = False
        except (subprocess.TimeoutExpired, Exception) as exc:
            log.debug("ResourceMonitor: nvidia-smi error: %s", exc)

    def health(self) -> dict[str, Any]:
        """Health status."""
        snap = self.latest
        return {
            "enabled": self._cfg.enabled,
            "running": self._thread is not None and self._thread.is_alive() if self._thread else False,
            "gpu_available": self._gpu_available,
            "latest_timestamp": snap.timestamp,
            "cpu_percent": snap.cpu_percent,
            "ram_used_mb": snap.ram_used_mb,
            "vram_used_mb": snap.vram_used_mb,
        }
