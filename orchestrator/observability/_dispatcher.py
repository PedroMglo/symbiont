"""Event dispatcher — central async hub that routes events to all sinks.

Architecture:
    emit(event) → queue.put_nowait()  [<1μs, never blocks]
    Background thread drains queue → redact → attach resources → fan-out to sinks

Sinks are independent and fail-silent.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any

from orchestrator.observability.clickhouse import ClickHouseSink
from orchestrator.observability.config import ObservabilityConfig
from orchestrator.observability.events import EventName, ObservabilityEvent
from orchestrator.observability.logger import JSONLSink
from orchestrator.observability.metrics import (
    METRIC_COLD_START_COUNT,
    METRIC_ERROR_COUNT,
    METRIC_FALLBACK_COUNT,
    METRIC_LLM_CALL_COUNT,
    METRIC_LLM_CALL_LATENCY,
    METRIC_REQUEST_COUNT,
    METRIC_REQUEST_LATENCY,
    METRIC_TOKENS_TOTAL,
    configure_metrics,
    get_meter,
    shutdown_metrics,
)
from orchestrator.observability.redaction import Redactor, set_redactor
from orchestrator.observability.resource_monitor import ResourceMonitor
from orchestrator.observability.semantic_attributes import (
    ATTR_MODEL_BACKEND,
    ATTR_MODEL_NAME,
    ATTR_ROUTE_INTENT,
    compact_attributes,
)
from orchestrator.observability.traces import (
    configure_traces,
    shutdown_traces,
)

log = logging.getLogger(__name__)


class EventDispatcher:
    """Central event dispatcher with async queue and multiple sinks."""

    def __init__(self, config: ObservabilityConfig):
        self._cfg = config
        self._queue: queue.Queue[ObservabilityEvent] = queue.Queue(maxsize=10000)
        self._stop_event = threading.Event()
        self._flush_thread: threading.Thread | None = None

        # Privacy
        self._redactor = Redactor(config.privacy)
        set_redactor(config.privacy)

        # Sinks
        self._jsonl_sink: JSONLSink | None = None
        self._clickhouse_sink: ClickHouseSink | None = None
        self._resource_monitor: ResourceMonitor | None = None

        # OTel instruments (lazy)
        self._otel_instruments: dict[str, Any] = {}

        self._setup_sinks()

    @property
    def sink_names(self) -> list[str]:
        """List of active sink names."""
        names = []
        if self._jsonl_sink and self._jsonl_sink.available:
            names.append("jsonl")
        if self._clickhouse_sink and self._clickhouse_sink.available:
            names.append("clickhouse")
        if self._cfg.otel.enabled:
            names.append("otel")
        return names

    def _setup_sinks(self) -> None:
        """Initialise all configured sinks."""
        # JSONL (always try)
        if self._cfg.local_logs.enabled:
            self._jsonl_sink = JSONLSink(self._cfg.local_logs)

        # ClickHouse
        if self._cfg.clickhouse.enabled:
            self._clickhouse_sink = ClickHouseSink(self._cfg.clickhouse)

        # OTel
        if self._cfg.otel.enabled:
            configure_traces(self._cfg.otel, self._cfg.service_name, self._cfg.environment)
            configure_metrics(self._cfg.otel, self._cfg.service_name, self._cfg.environment)

        # Resource monitor
        if self._cfg.resources.enabled:
            self._resource_monitor = ResourceMonitor(self._cfg.resources)

    def start(self) -> None:
        """Start background workers."""
        self._stop_event.clear()

        # Flush thread
        self._flush_thread = threading.Thread(
            target=self._flush_loop, daemon=True, name="obs-dispatcher"
        )
        self._flush_thread.start()

        # ClickHouse flush thread
        if self._clickhouse_sink:
            self._clickhouse_sink.start()

        # Resource monitor
        if self._resource_monitor:
            self._resource_monitor.start()

        # Resource sampler → ClickHouse resource_samples table
        if self._resource_monitor and self._clickhouse_sink:
            self._resource_sample_thread = threading.Thread(
                target=self._resource_sample_loop, daemon=True, name="resource-ch-sampler"
            )
            self._resource_sample_thread.start()

        log.debug("EventDispatcher: started (sinks=%s)", self.sink_names)

    def stop(self) -> None:
        """Stop all background workers and flush."""
        self._stop_event.set()

        # Wait for flush thread
        if self._flush_thread and self._flush_thread.is_alive():
            self._flush_thread.join(timeout=5.0)

        # Final drain
        self._drain_queue()

        # Stop sinks
        if self._clickhouse_sink:
            self._clickhouse_sink.stop()
        if self._jsonl_sink:
            self._jsonl_sink.flush()
            self._jsonl_sink.close()
        if self._resource_monitor:
            self._resource_monitor.stop()

        # OTel shutdown
        shutdown_traces()
        shutdown_metrics()

    def emit(self, event: ObservabilityEvent) -> None:
        """Non-blocking event emission. Drops if queue is full."""
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            log.debug("EventDispatcher: queue full, dropping event %s", event.event.value)

    def _flush_loop(self) -> None:
        """Background: drain queue and dispatch events."""
        while not self._stop_event.is_set():
            self._drain_queue()
            self._stop_event.wait(timeout=1.0)

    def _resource_sample_loop(self) -> None:
        """Periodically write resource snapshots to ClickHouse resource_samples table."""
        from datetime import datetime, timezone

        # Brief wait for resource monitor to collect first sample
        self._stop_event.wait(timeout=1.0)

        interval = self._cfg.resources.sample_interval_seconds if self._cfg.resources else 10.0
        last_ts: float = 0.0
        while not self._stop_event.is_set():
            try:
                snap = self._resource_monitor.latest
                # Skip if snapshot hasn't been updated since last write
                if snap.cpu_percent is not None and snap.timestamp != last_ts:
                    last_ts = snap.timestamp
                    dt = datetime.fromtimestamp(snap.timestamp or time.time(), tz=timezone.utc)
                    ts_str = dt.strftime("%Y-%m-%d %H:%M:%S.") + f"{int(dt.microsecond / 1000):03d}"
                    row = {
                        "timestamp": ts_str,
                        "cpu_percent": round(snap.cpu_percent, 1) if snap.cpu_percent else 0,
                        "ram_used_mb": snap.ram_used_mb or 0,
                        "ram_total_mb": snap.ram_total_mb or 0,
                        "ram_percent": round(snap.ram_percent, 1) if snap.ram_percent else 0,
                        "gpu_util_percent": round(snap.gpu_util_percent, 1) if snap.gpu_util_percent else 0,
                        "vram_used_mb": snap.vram_used_mb or 0,
                        "vram_total_mb": snap.vram_total_mb or 0,
                        "vram_free_mb": snap.vram_free_mb or 0,
                        "gpu_name": snap.gpu_name or "",
                        "gpu_temperature_c": round(snap.gpu_temperature_c, 1) if snap.gpu_temperature_c else 0,
                        "gpu_power_w": round(snap.gpu_power_w, 1) if snap.gpu_power_w else 0,
                    }
                    self._clickhouse_sink.write_to_table("resource_samples", row)
            except Exception as exc:
                log.debug("Resource sampler: %s", exc)

            self._stop_event.wait(timeout=interval)


    def _drain_queue(self) -> None:
        """Drain all pending events from queue."""
        batch: list[ObservabilityEvent] = []
        while True:
            try:
                event = self._queue.get_nowait()
                batch.append(event)
            except queue.Empty:
                break

        if not batch:
            return

        for event in batch:
            self._dispatch_event(event)

    def _dispatch_event(self, event: ObservabilityEvent) -> None:
        """Process a single event: redact, enrich, fan-out to sinks."""
        # Serialise
        event_dict = event.to_dict(exclude_none=True)

        # Attach resource readings
        if self._resource_monitor:
            event_dict = self._resource_monitor.attach_to_event(event_dict)

        # Redact
        event_dict = self._redactor.redact_event_dict(event_dict)

        # Fan-out to sinks
        if self._jsonl_sink and self._jsonl_sink.available:
            try:
                self._jsonl_sink.write(event_dict)
            except Exception:
                pass

        if self._clickhouse_sink and self._clickhouse_sink.available:
            try:
                ch_dict = self._to_clickhouse_row(event_dict)
                self._clickhouse_sink.write(ch_dict)
            except Exception:
                pass

        # Update OTel metrics
        self._update_otel_metrics(event)

    def _to_clickhouse_row(self, event_dict: dict[str, Any]) -> dict[str, Any]:
        """Transform event dict to ClickHouse row format."""
        from datetime import datetime, timezone

        row = {}
        for key, value in event_dict.items():
            if key == "tools_used" and isinstance(value, list):
                row[key] = 1 if len(value) > 0 else 0  # UInt8 flag
                continue
            if key == "timestamp":
                # Convert Unix epoch float to DateTime64(3) string
                if isinstance(value, (int, float)):
                    dt = datetime.fromtimestamp(value, tz=timezone.utc)
                    row[key] = dt.strftime("%Y-%m-%d %H:%M:%S.") + f"{int(dt.microsecond / 1000):03d}"
                else:
                    row[key] = str(value)
                continue
            row[key] = value

        # Ensure boolean fields are 0/1
        for bool_field in ("fallback_used", "rag_used", "graph_used", "tools_used",
                           "agentic", "success", "cold_start", "stream"):
            if bool_field in row:
                row[bool_field] = 1 if row[bool_field] else 0

        return row

    def _update_otel_metrics(self, event: ObservabilityEvent) -> None:
        """Update OTel metric instruments based on event type."""
        if not self._cfg.otel.enabled:
            return

        try:
            meter = get_meter()
            attrs = compact_attributes({
                ATTR_MODEL_NAME: event.model,
                ATTR_MODEL_BACKEND: event.backend,
                ATTR_ROUTE_INTENT: event.intent,
            })

            if event.event == EventName.REQUEST_COMPLETED:
                meter.create_counter(METRIC_REQUEST_COUNT).add(1, attrs)
                if event.total_latency_ms:
                    meter.create_histogram(METRIC_REQUEST_LATENCY).record(
                        event.total_latency_ms, attrs
                    )

            elif event.event == EventName.LLM_CALL_COMPLETED:
                meter.create_counter(METRIC_LLM_CALL_COUNT).add(1, attrs)
                if event.total_latency_ms:
                    meter.create_histogram(METRIC_LLM_CALL_LATENCY).record(
                        event.total_latency_ms, attrs
                    )
                if event.total_tokens:
                    meter.create_counter(METRIC_TOKENS_TOTAL).add(event.total_tokens, attrs)
                if event.cold_start:
                    meter.create_counter(METRIC_COLD_START_COUNT).add(1, attrs)

            elif event.event == EventName.FALLBACK_USED:
                meter.create_counter(METRIC_FALLBACK_COUNT).add(1, attrs)

            elif event.event in (EventName.REQUEST_ERROR, EventName.LLM_CALL_ERROR):
                meter.create_counter(METRIC_ERROR_COUNT).add(1, attrs)

        except Exception:
            pass  # Never let metrics crash the dispatcher

    def emit_adaptation_event(
        self,
        event_type: str,
        *,
        prev_mode: str = "",
        new_mode: str = "",
        trigger_metric: str = "",
        trigger_value: float = 0,
        detail: str = "",
    ) -> None:
        """Write an adaptation event to ClickHouse (non-blocking)."""
        if not self._clickhouse_sink:
            return
        try:
            from datetime import datetime, timezone
            dt = datetime.now(tz=timezone.utc)
            ts_str = dt.strftime("%Y-%m-%d %H:%M:%S.") + f"{int(dt.microsecond / 1000):03d}"
            row = {
                "timestamp": ts_str,
                "event_type": event_type,
                "prev_mode": prev_mode,
                "new_mode": new_mode,
                "trigger_metric": trigger_metric,
                "trigger_value": round(trigger_value, 2),
                "detail": detail,
            }
            self._clickhouse_sink.write_to_table("adaptation_events", row)
        except Exception as exc:
            log.debug("Adaptation event write failed: %s", exc)

    def health(self) -> dict[str, Any]:
        """Health report for diagnostics."""
        return {
            "enabled": self._cfg.enabled,
            "queue_size": self._queue.qsize(),
            "sinks": self.sink_names,
            "jsonl": {"available": self._jsonl_sink.available if self._jsonl_sink else False},
            "clickhouse": self._clickhouse_sink.health() if self._clickhouse_sink else {"enabled": False},
            "otel": {"enabled": self._cfg.otel.enabled},
            "resource_monitor": self._resource_monitor.health() if self._resource_monitor else {"enabled": False},
        }
