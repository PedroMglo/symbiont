"""Telemetry helpers — convenience functions for instrumenting Engine/Router/LLM.

These are the ONLY functions that application code should call.
They handle trace context propagation, event emission, and resource correlation.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Any, Generator

from orchestrator.observability.events import (
    EventLevel,
    EventName,
    ObservabilityEvent,
    new_request_id,
)
from orchestrator.observability.semantic_attributes import (
    ATTR_ENTRYPOINT,
    ATTR_MODEL_BACKEND,
    ATTR_MODEL_NAME,
    OWNER_ORCHESTRATOR,
    base_attributes,
    event_attributes,
)
from orchestrator.observability.traces import add_event_to_current_span, current_trace_context, span


def emit_event(
    event_name: EventName,
    *,
    level: EventLevel = EventLevel.INFO,
    request_id: str | None = None,
    **kwargs,
) -> None:
    """Emit a structured observability event (non-blocking)."""
    from orchestrator.observability import emit

    trace_id, span_id = current_trace_context()

    event = ObservabilityEvent(
        event=event_name,
        level=level,
        request_id=request_id or new_request_id(),
        trace_id=trace_id,
        span_id=span_id,
        **kwargs,
    )
    add_event_to_current_span(event_name.value, event_attributes(event, owner=OWNER_ORCHESTRATOR))
    emit(event)


@contextmanager
def trace_request(
    query: str,
    *,
    session_id: str | None = None,
    entrypoint: str = "api",
    request_id: str | None = None,
) -> Generator[dict[str, Any], None, None]:
    """Context manager that traces a full request lifecycle.

    Usage:
        with trace_request(query, session_id=sid, entrypoint="api") as ctx:
            ctx["model"] = selected_model
            ctx["backend"] = selected_backend
            # ... do work ...
            ctx["response_length"] = len(response)

    Emits request_started on enter, request_completed/request_error on exit.
    """
    rid = request_id or new_request_id()
    t0 = time.perf_counter()

    # Emit request_started
    emit_event(
        EventName.REQUEST_STARTED,
        request_id=rid,
        session_id=session_id,
        entrypoint=entrypoint,
        query_length=len(query),
    )

    ctx: dict[str, Any] = {
        "request_id": rid,
        "session_id": session_id,
        "entrypoint": entrypoint,
        "t0": t0,
    }

    with span(
        "request",
        attributes=base_attributes(
            request_id=rid,
            session_id=session_id,
            trace_kind="request",
            **{ATTR_ENTRYPOINT: entrypoint},
        ),
    ):
        try:
            yield ctx

            # Success — emit request_completed
            latency_ms = (time.perf_counter() - t0) * 1000
            emit_event(
                EventName.REQUEST_COMPLETED,
                request_id=rid,
                session_id=session_id,
                entrypoint=entrypoint,
                total_latency_ms=latency_ms,
                model=ctx.get("model"),
                backend=ctx.get("backend"),
                intent=ctx.get("intent"),
                complexity=ctx.get("complexity"),
                prompt_tokens=ctx.get("prompt_tokens"),
                completion_tokens=ctx.get("completion_tokens"),
                total_tokens=ctx.get("total_tokens"),
                context_tokens=ctx.get("context_tokens"),
                usage_source=ctx.get("usage_source"),
                context_build_latency_ms=ctx.get("context_build_latency_ms"),
                llm_latency_ms=ctx.get("llm_latency_ms"),
                router_latency_ms=ctx.get("router_latency_ms"),
                model_load_latency_ms=ctx.get("model_load_latency_ms"),
                prompt_eval_latency_ms=ctx.get("prompt_eval_latency_ms"),
                generation_latency_ms=ctx.get("generation_latency_ms"),
                rag_latency_ms=ctx.get("rag_latency_ms"),
                graph_latency_ms=ctx.get("graph_latency_ms"),
                first_token_latency_ms=ctx.get("first_token_latency_ms"),
                tokens_per_second=ctx.get("tokens_per_second"),
                prompt_tokens_per_second=ctx.get("prompt_tokens_per_second"),
                generation_tokens_per_second=ctx.get("generation_tokens_per_second"),
                rag_used=ctx.get("rag_used", False),
                graph_used=ctx.get("graph_used", False),
                tools_used=ctx.get("tools_used", []),
                agentic=ctx.get("agentic", False),
                iterations=ctx.get("iterations", 0),
                cold_start=ctx.get("cold_start", False),
                profile=ctx.get("profile"),
                selected_model=ctx.get("selected_model"),
                selected_backend=ctx.get("selected_backend"),
                requested_model=ctx.get("requested_model"),
                fallback_used=ctx.get("fallback_used", False),
                fallback_reason=ctx.get("fallback_reason"),
                query_length=len(query),
                response_length=ctx.get("response_length"),
                query_hash=ObservabilityEvent.hash_query(query),
                stream=ctx.get("stream", False),
                chunks_count=ctx.get("chunks_count", 0),
                success=True,
            )

        except Exception as exc:
            # Error — emit request_error
            latency_ms = (time.perf_counter() - t0) * 1000
            emit_event(
                EventName.REQUEST_ERROR,
                level=EventLevel.ERROR,
                request_id=rid,
                session_id=session_id,
                entrypoint=entrypoint,
                total_latency_ms=latency_ms,
                model=ctx.get("model"),
                backend=ctx.get("backend"),
                success=False,
                error_type=type(exc).__name__,
                error_message_safe=str(exc)[:500],
            )
            raise


@contextmanager
def trace_llm_call(
    model: str,
    backend: str,
    *,
    request_id: str | None = None,
    stream: bool = False,
) -> Generator[dict[str, Any], None, None]:
    """Context manager for tracing an LLM call.

    Usage:
        with trace_llm_call(model, backend, request_id=rid) as ctx:
            result = client.chat_instrumented(...)
            ctx["total_tokens"] = result.usage.total_tokens
            ctx["latency_ms"] = result.latency_ms
    """
    rid = request_id or new_request_id()
    t0 = time.perf_counter()

    event_started = EventName.LLM_STREAM_STARTED if stream else EventName.LLM_CALL_STARTED
    event_completed = EventName.LLM_STREAM_COMPLETED if stream else EventName.LLM_CALL_COMPLETED

    emit_event(
        event_started,
        request_id=rid,
        model=model,
        backend=backend,
        stream=stream,
    )

    ctx: dict[str, Any] = {"request_id": rid, "model": model, "backend": backend}

    with span(
        "llm_call",
        attributes=base_attributes(
            request_id=rid,
            trace_kind="llm_call",
            **{ATTR_MODEL_NAME: model, ATTR_MODEL_BACKEND: backend},
        ),
    ):
        try:
            yield ctx

            latency_ms = ctx.get("latency_ms") or (time.perf_counter() - t0) * 1000
            emit_event(
                event_completed,
                request_id=rid,
                model=model,
                backend=backend,
                total_latency_ms=latency_ms,
                prompt_tokens=ctx.get("prompt_tokens"),
                completion_tokens=ctx.get("completion_tokens"),
                total_tokens=ctx.get("total_tokens"),
                usage_source=ctx.get("usage_source"),
                model_load_latency_ms=ctx.get("model_load_latency_ms"),
                prompt_eval_latency_ms=ctx.get("prompt_eval_latency_ms"),
                generation_latency_ms=ctx.get("generation_latency_ms"),
                first_token_latency_ms=ctx.get("first_token_latency_ms"),
                tokens_per_second=ctx.get("tokens_per_second"),
                cold_start=ctx.get("cold_start", False),
                stream=stream,
                chunks_count=ctx.get("chunks_count", 0),
                ollama_total_duration=ctx.get("ollama_total_duration"),
                ollama_load_duration=ctx.get("ollama_load_duration"),
                ollama_prompt_eval_count=ctx.get("ollama_prompt_eval_count"),
                ollama_prompt_eval_duration=ctx.get("ollama_prompt_eval_duration"),
                ollama_eval_count=ctx.get("ollama_eval_count"),
                ollama_eval_duration=ctx.get("ollama_eval_duration"),
                success=True,
            )

        except Exception as exc:
            latency_ms = (time.perf_counter() - t0) * 1000
            emit_event(
                EventName.LLM_CALL_ERROR,
                level=EventLevel.ERROR,
                request_id=rid,
                model=model,
                backend=backend,
                total_latency_ms=latency_ms,
                success=False,
                error_type=type(exc).__name__,
                error_message_safe=str(exc)[:500],
            )
            raise


def emit_router_decision(
    *,
    request_id: str,
    requested_model: str,
    selected_model: str,
    selected_backend: str,
    intent: str | None = None,
    complexity: str | None = None,
    profile: str | None = None,
    fallback_used: bool = False,
    fallback_reason: str | None = None,
) -> None:
    """Emit a router_decision event."""
    emit_event(
        EventName.ROUTER_DECISION,
        request_id=request_id,
        requested_model=requested_model,
        selected_model=selected_model,
        selected_backend=selected_backend,
        intent=intent,
        complexity=complexity,
        profile=profile,
        fallback_used=fallback_used,
        fallback_reason=fallback_reason,
    )
    if fallback_used:
        emit_event(
            EventName.FALLBACK_USED,
            level=EventLevel.WARNING,
            request_id=request_id,
            requested_model=requested_model,
            selected_model=selected_model,
            selected_backend=selected_backend,
            fallback_used=True,
            fallback_reason=fallback_reason,
        )


def emit_context_build(
    *,
    request_id: str,
    latency_ms: float,
    context_tokens: int,
    rag_used: bool = False,
    graph_used: bool = False,
    rag_latency_ms: float | None = None,
    graph_latency_ms: float | None = None,
) -> None:
    """Emit context_build_completed event."""
    emit_event(
        EventName.CONTEXT_BUILD_COMPLETED,
        request_id=request_id,
        context_build_latency_ms=latency_ms,
        context_tokens=context_tokens,
        rag_used=rag_used,
        graph_used=graph_used,
        rag_latency_ms=rag_latency_ms,
        graph_latency_ms=graph_latency_ms,
    )


def emit_tool_call(
    *,
    request_id: str,
    tool_name: str,
    latency_ms: float,
    success: bool = True,
    error_type: str | None = None,
) -> None:
    """Emit tool_call_completed event."""
    emit_event(
        EventName.TOOL_CALL_COMPLETED,
        request_id=request_id,
        tools_used=[tool_name],
        total_latency_ms=latency_ms,
        success=success,
        error_type=error_type,
    )


def emit_cold_start(
    *,
    request_id: str,
    model: str,
    load_latency_ms: float,
) -> None:
    """Emit cold start detection event."""
    emit_event(
        EventName.MODEL_COLD_START_DETECTED,
        level=EventLevel.WARNING,
        request_id=request_id,
        model=model,
        model_load_latency_ms=load_latency_ms,
        cold_start=True,
    )
