"""OpenTelemetry metrics instruments.

Provides counters, histograms, gauges for LLM performance.
Falls back to noop when OTel SDK is not available.
"""

from __future__ import annotations

import logging
from typing import Any

from orchestrator.observability.config import OTelConfig

log = logging.getLogger(__name__)

_meter_provider = None
_meter_cache: dict[str, Any] = {}
_configured = False

# Pre-defined instrument names
METRIC_REQUEST_COUNT = "orc.requests.count"
METRIC_REQUEST_LATENCY = "orc.requests.latency_ms"
METRIC_LLM_CALL_COUNT = "orc.llm.calls.count"
METRIC_LLM_CALL_LATENCY = "orc.llm.calls.latency_ms"
METRIC_TOKENS_TOTAL = "orc.llm.tokens.total"
METRIC_TOKENS_PER_SECOND = "orc.llm.tokens_per_second"
METRIC_FALLBACK_COUNT = "orc.router.fallbacks.count"
METRIC_ERROR_COUNT = "orc.errors.count"
METRIC_COLD_START_COUNT = "orc.llm.cold_starts.count"
METRIC_CONTEXT_BUILD_LATENCY = "orc.context.build_latency_ms"
METRIC_VRAM_USED = "orc.resources.vram_used_mb"
METRIC_RAM_USED = "orc.resources.ram_used_mb"
METRIC_CPU_PERCENT = "orc.resources.cpu_percent"


def configure_metrics(config: OTelConfig, service_name: str, environment: str) -> None:
    """Configure the OpenTelemetry meter provider."""
    global _meter_provider, _configured

    if not config.enabled:
        _configured = True
        return

    try:
        from opentelemetry import metrics
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource

        resource = Resource.create({
            "service.name": service_name,
            "deployment.environment": environment,
        })

        readers = []

        # Try OTLP exporter
        try:
            from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter

            exporter = OTLPMetricExporter(
                endpoint=f"{config.endpoint}/v1/metrics",
                timeout=int(config.timeout_seconds),
            )
            reader = PeriodicExportingMetricReader(exporter, export_interval_millis=10000)
            readers.append(reader)
            log.info("OTel metrics: OTLP HTTP exporter → %s", config.endpoint)
        except ImportError:
            log.debug("OTel metrics: otlp exporter not installed")

        provider = MeterProvider(resource=resource, metric_readers=readers)
        metrics.set_meter_provider(provider)
        _meter_provider = provider
        _configured = True

    except ImportError:
        log.debug("OTel metrics: SDK not installed, using noop")
        _configured = True
    except Exception as exc:
        if config.fail_silent:
            log.warning("OTel metrics: setup failed (fail_silent=True): %s", exc)
            _configured = True
        else:
            raise


def get_meter(name: str = "ai-symbiont"):
    """Get an OTel meter (real or noop)."""
    if name in _meter_cache:
        return _meter_cache[name]

    try:
        from opentelemetry import metrics

        meter = metrics.get_meter(name)
    except ImportError:
        meter = _NoopMeter()

    _meter_cache[name] = meter
    return meter


def shutdown_metrics() -> None:
    """Shutdown meter provider."""
    global _meter_provider
    if _meter_provider:
        try:
            _meter_provider.shutdown()
        except Exception:
            pass
        _meter_provider = None


class _NoopMeter:
    """Fallback meter when OTel not installed."""

    def create_counter(self, name, **kwargs):
        return _NoopInstrument()

    def create_histogram(self, name, **kwargs):
        return _NoopInstrument()

    def create_up_down_counter(self, name, **kwargs):
        return _NoopInstrument()

    def create_gauge(self, name, **kwargs):
        return _NoopInstrument()


class _NoopInstrument:
    def add(self, value, attributes=None):
        pass

    def record(self, value, attributes=None):
        pass

    def set(self, value, attributes=None):
        pass
