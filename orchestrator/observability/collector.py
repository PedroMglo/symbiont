"""Event collector — async queue + background flush to MetricsStore.

The collector ensures that emitting events from the Engine/Router never blocks
the request path. Events are queued and flushed to SQLite periodically in a
background thread.

Public API:
    init_observability(metrics_cfg, dashboard_cfg)  → start collector
    emit(event)                                      → enqueue (non-blocking)
    shutdown()                                       → flush & stop
"""

from __future__ import annotations

import logging
import queue
import threading
import time

from orchestrator.observability.metrics_config import DashboardConfig, MetricsConfig
from orchestrator.observability.models import MetricsEvent
from orchestrator.observability.store import MetricsStore, init_store

log = logging.getLogger(__name__)


def _sleep_interruptible(collector: "_Collector", seconds: float) -> None:
    """Sleep in small increments so the thread can stop promptly."""
    end = time.monotonic() + seconds
    while time.monotonic() < end and collector._running:
        time.sleep(min(1.0, end - time.monotonic()))


class _Collector:
    """Background collector that flushes events to the store."""

    def __init__(self, store: MetricsStore, cfg: MetricsConfig) -> None:
        self._store = store
        self._cfg = cfg
        self._queue: queue.Queue[MetricsEvent | None] = queue.Queue(maxsize=cfg.max_queue_size)
        self._sse_subscribers: list[queue.Queue[MetricsEvent]] = []
        self._lock = threading.Lock()
        self._running = False
        self._thread: threading.Thread | None = None
        self._resource_thread: threading.Thread | None = None

    def start(self) -> None:
        """Start the background flush thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._flush_loop, daemon=True, name="metrics-collector"
        )
        self._thread.start()
        if self._cfg.resource_monitor_enabled:
            self._resource_thread = threading.Thread(
                target=self._resource_loop, daemon=True, name="resource-collector"
            )
            self._resource_thread.start()
        log.debug(
            "Collector: background flush thread started (resource_monitor=%s)",
            self._cfg.resource_monitor_enabled,
        )

    def stop(self) -> None:
        """Signal stop and flush remaining events."""
        if not self._running:
            return
        self._running = False
        self._queue.put(None)  # sentinel
        if self._thread:
            self._thread.join(timeout=5.0)
        if self._resource_thread:
            self._resource_thread.join(timeout=2.0)
        log.debug("Collector: stopped")

    def emit(self, event: MetricsEvent) -> None:
        """Enqueue an event (non-blocking). Drops if queue is full."""
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            log.warning("Collector: queue full, dropping event %s", event.request_id)
            return

        # Broadcast to SSE subscribers
        with self._lock:
            dead: list[queue.Queue] = []
            for sub_q in self._sse_subscribers:
                try:
                    sub_q.put_nowait(event)
                except queue.Full:
                    dead.append(sub_q)
            for d in dead:
                self._sse_subscribers.remove(d)

    def subscribe_sse(self) -> queue.Queue[MetricsEvent]:
        """Create a new SSE subscription queue."""
        q: queue.Queue[MetricsEvent] = queue.Queue(maxsize=100)
        with self._lock:
            self._sse_subscribers.append(q)
        return q

    def unsubscribe_sse(self, q: queue.Queue[MetricsEvent]) -> None:
        """Remove an SSE subscription."""
        with self._lock:
            try:
                self._sse_subscribers.remove(q)
            except ValueError:
                pass

    def _flush_loop(self) -> None:
        """Background loop: drain queue and batch-insert to SQLite."""
        while self._running:
            batch: list[MetricsEvent] = []
            deadline = time.monotonic() + self._cfg.flush_interval_seconds

            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                try:
                    item = self._queue.get(timeout=max(0.1, remaining))
                    if item is None:
                        # Sentinel — stop signal
                        break
                    batch.append(item)
                except queue.Empty:
                    break

            if batch:
                try:
                    self._store.insert_batch(batch)
                except Exception as exc:
                    log.error("Collector: flush failed: %s", exc)

        # Final flush on stop
        remaining_batch: list[MetricsEvent] = []
        while not self._queue.empty():
            try:
                item = self._queue.get_nowait()
                if item is not None:
                    remaining_batch.append(item)
            except queue.Empty:
                break
        if remaining_batch:
            try:
                self._store.insert_batch(remaining_batch)
            except Exception as exc:
                log.error("Collector: final flush failed: %s", exc)

    def _resource_loop(self) -> None:
        """Background loop: collect system resource snapshots and check pressure alerts."""
        interval = max(5.0, float(self._cfg.resource_interval_seconds))

        # Initial delay to let the server start
        _sleep_interruptible(self, 5.0)
        while self._running:
            try:
                from orchestrator.observability.resources import get_resource_collector
                collector = get_resource_collector()
                snapshot = collector.record_snapshot()

                # --- VRAM pressure check ---
                vram_used = snapshot.get("gpu_vram_used_mb", 0)
                if vram_used > self._cfg.vram_critical_mb:
                    log.warning(
                        "VRAM CRITICAL: %dMB used (threshold %dMB) — triggering model eviction",
                        vram_used, self._cfg.vram_critical_mb,
                    )
                    self._trigger_vram_eviction()
                elif vram_used > self._cfg.vram_warning_mb:
                    log.warning("VRAM WARNING: %dMB used (threshold %dMB)", vram_used, self._cfg.vram_warning_mb)

                # --- Swap pressure check ---
                swap_used = snapshot.get("swap_used_mb", 0)
                if swap_used > self._cfg.swap_critical_mb:
                    log.warning(
                        "SWAP CRITICAL: %dMB used — system under severe memory pressure",
                        swap_used,
                    )
                elif swap_used > self._cfg.swap_warning_mb:
                    log.warning("SWAP WARNING: %dMB used", swap_used)

            except Exception as exc:
                log.debug("Collector: resource snapshot failed: %s", exc)
            _sleep_interruptible(self, interval)

    def _trigger_vram_eviction(self) -> None:
        """Attempt to evict a model when VRAM is critically high."""
        try:
            from orchestrator.core.warmup import get_warmup_manager
            wm = get_warmup_manager()
            evicted = wm.evict_least_used()
            if evicted:
                log.info("Auto-evicted model %s due to VRAM pressure", evicted)
        except Exception as exc:
            log.debug("VRAM auto-eviction failed: %s", exc)


# ---------------------------------------------------------------------------
# Module-level singleton + public API
# ---------------------------------------------------------------------------

_collector: _Collector | None = None
_metrics_cfg: MetricsConfig | None = None
_dashboard_cfg: DashboardConfig | None = None


def init_observability(
    metrics_cfg: MetricsConfig | None = None,
    dashboard_cfg: DashboardConfig | None = None,
) -> None:
    """Initialise the observability layer. Call once at startup.

    If metrics_cfg is None or metrics_cfg.enabled is False, the layer is a no-op.
    """
    global _collector, _metrics_cfg, _dashboard_cfg

    _metrics_cfg = metrics_cfg or MetricsConfig()
    _dashboard_cfg = dashboard_cfg or DashboardConfig()

    if not _metrics_cfg.enabled:
        log.info("Observability: disabled by config")
        return

    store = init_store(_metrics_cfg)
    _collector = _Collector(store, _metrics_cfg)
    _collector.start()
    log.info("Observability: initialised (flush_interval=%.1fs)", _metrics_cfg.flush_interval_seconds)


def emit(event: MetricsEvent) -> None:
    """Emit an observability event. No-op if layer is disabled."""
    if _collector is not None:
        _collector.emit(event)


def shutdown() -> None:
    """Gracefully stop the collector. Call at server shutdown."""
    global _collector
    if _collector is not None:
        _collector.stop()
        _collector = None


def get_collector() -> _Collector | None:
    """Access the collector (for SSE subscriptions)."""
    return _collector


def get_metrics_config() -> MetricsConfig | None:
    return _metrics_cfg


def get_dashboard_config() -> DashboardConfig | None:
    return _dashboard_cfg
