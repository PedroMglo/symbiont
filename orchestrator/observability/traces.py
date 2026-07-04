"""OpenTelemetry trace setup and span helpers.

Provides a tracer that creates real OTel spans when enabled,
or a noop tracer when OTel is disabled/unavailable.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Generator

from orchestrator.observability.config import OTelConfig

log = logging.getLogger(__name__)

_tracer_provider = None
_tracer_cache: dict[str, Any] = {}
_configured = False


def configure_traces(config: OTelConfig, service_name: str, environment: str) -> None:
    """Configure the OpenTelemetry tracer provider with OTLP exporter."""
    global _tracer_provider, _configured

    if not config.enabled:
        _configured = True
        return

    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        resource = Resource.create({
            "service.name": service_name,
            "deployment.environment": environment,
        })

        provider = TracerProvider(resource=resource)

        # Try OTLP HTTP exporter
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            exporter = OTLPSpanExporter(
                endpoint=f"{config.endpoint}/v1/traces",
                timeout=int(config.timeout_seconds),
            )
            processor = BatchSpanProcessor(exporter)
            provider.add_span_processor(processor)
            log.info("OTel traces: OTLP HTTP exporter → %s", config.endpoint)
        except ImportError:
            log.debug("OTel traces: opentelemetry-exporter-otlp-proto-http not installed")
            if not config.fail_silent:
                raise

        trace.set_tracer_provider(provider)
        _tracer_provider = provider
        _configured = True

    except ImportError:
        log.debug("OTel traces: opentelemetry SDK not installed, using noop")
        _configured = True
    except Exception as exc:
        if config.fail_silent:
            log.warning("OTel traces: setup failed (fail_silent=True): %s", exc)
            _configured = True
        else:
            raise


def get_tracer(name: str = "ai-symbiont"):
    """Get an OTel tracer (real or noop)."""
    if name in _tracer_cache:
        return _tracer_cache[name]

    try:
        from opentelemetry import trace

        tracer = trace.get_tracer(name)
    except ImportError:
        tracer = _NoopTracer()

    _tracer_cache[name] = tracer
    return tracer


def shutdown_traces() -> None:
    """Flush and shutdown the tracer provider."""
    global _tracer_provider
    if _tracer_provider:
        try:
            _tracer_provider.shutdown()
        except Exception:
            pass
        _tracer_provider = None


@contextmanager
def span(name: str, attributes: dict[str, Any] | None = None) -> Generator:
    """Context manager for creating a span (noop-safe)."""
    tracer = get_tracer()
    with tracer.start_as_current_span(name) as s:
        if attributes and hasattr(s, "set_attributes"):
            s.set_attributes(attributes)
        yield s


def add_event_to_current_span(name: str, attributes: dict[str, Any] | None = None) -> None:
    """Attach an event to the active span when OTel is available."""
    try:
        from opentelemetry import trace

        current = trace.get_current_span()
        if hasattr(current, "add_event"):
            current.add_event(name, attributes=attributes or {})
    except Exception:
        pass


def current_trace_context() -> tuple[str | None, str | None]:
    """Get current trace_id and span_id as hex strings (or None)."""
    try:
        from opentelemetry import trace

        ctx = trace.get_current_span().get_span_context()
        if ctx and ctx.trace_id:
            return (
                format(ctx.trace_id, "032x"),
                format(ctx.span_id, "016x"),
            )
    except (ImportError, Exception):
        pass
    return None, None


class _NoopTracer:
    """Fallback tracer when OTel is not installed."""

    def start_as_current_span(self, name, **kwargs):
        return _NoopSpanContext()


class _NoopSpanContext:
    def __enter__(self):
        return _NoopSpan()

    def __exit__(self, *args):
        pass


class _NoopSpan:
    def set_attributes(self, attrs):
        pass

    def set_attribute(self, key, value):
        pass

    def add_event(self, name, attributes=None):
        pass

    def set_status(self, status):
        pass

    def get_span_context(self):
        return None
