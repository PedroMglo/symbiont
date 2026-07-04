"""Central observability layer — structured events, traces, metrics, exporters.

Public API:
    init_observability(config)  — initialise all sinks/exporters
    shutdown_observability()    — flush & close
    emit(event)                — non-blocking event emission
    get_tracer()               — OpenTelemetry tracer (or noop)
    get_meter()                — OpenTelemetry meter (or noop)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from orchestrator.observability._dispatcher import EventDispatcher as _EventDispatcherType
    from orchestrator.observability.config import ObservabilityConfig
    from orchestrator.observability.events import ObservabilityEvent

log = logging.getLogger(__name__)

_dispatcher: "_EventDispatcherType | None" = None


def init_observability(config: "ObservabilityConfig") -> None:
    """Initialise the observability subsystem with given config."""
    global _dispatcher
    from orchestrator.observability._dispatcher import EventDispatcher
    from orchestrator.observability.gemilyni import init_gemilyni

    _dispatcher = EventDispatcher(config)
    _dispatcher.start()
    init_gemilyni(config.gemilyni)
    log.info("Observability: initialised (sinks=%s)", _dispatcher.sink_names)


def shutdown_observability() -> None:
    """Flush pending events and stop background workers."""
    global _dispatcher
    if _dispatcher:
        _dispatcher.stop()
        _dispatcher = None
        log.info("Observability: shut down")


def emit(event: "ObservabilityEvent") -> None:
    """Emit a structured event (non-blocking). Silently drops if not initialised."""
    if _dispatcher:
        _dispatcher.emit(event)


def emit_adaptation_event(
    event_type: str,
    *,
    prev_mode: str = "",
    new_mode: str = "",
    trigger_metric: str = "",
    trigger_value: float = 0,
    detail: str = "",
) -> None:
    """Emit an adaptation/degradation event (non-blocking)."""
    if _dispatcher:
        _dispatcher.emit_adaptation_event(
            event_type,
            prev_mode=prev_mode,
            new_mode=new_mode,
            trigger_metric=trigger_metric,
            trigger_value=trigger_value,
            detail=detail,
        )


def get_tracer(name: str = "ai-symbiont"):
    """Return an OpenTelemetry tracer (or noop if OTel disabled)."""
    from orchestrator.observability.traces import get_tracer as _get

    return _get(name)


def get_meter(name: str = "ai-symbiont"):
    """Return an OpenTelemetry meter (or noop if OTel disabled)."""
    from orchestrator.observability.metrics import get_meter as _get

    return _get(name)
