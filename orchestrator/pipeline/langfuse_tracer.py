"""Langfuse observability integration for the symbiont pipeline.

Provides LLM-specific tracing on top of the existing ClickHouse/OTel tracer:
- Per-request traces with full prompt/response content
- Per-node spans with timing and metadata
- Prewarming decision logging (hit/miss/false positive)
- Prompt versioning (fetches prompts from Langfuse if configured)

Configuration via environment:
    LANGFUSE_SECRET_KEY   — Langfuse secret key
    LANGFUSE_PUBLIC_KEY   — Langfuse public key
    LANGFUSE_HOST         — Langfuse server URL (default: https://localhost:3000)
    LANGFUSE_ENABLED      — Enable/disable (default: true if keys are set)

Usage:
    from orchestrator.pipeline.langfuse_tracer import LangfuseTracer

    tracer = LangfuseTracer(request_id="abc123", session_id="sess1")
    tracer.trace_node_start("classify", input={"query": "..."})
    tracer.trace_node_end("classify", output={"intent": "SYSTEM"}, duration_ms=5.2)
    tracer.trace_prewarm_decision(feature="local_evidence", hit=True, confidence=0.92)
    tracer.finalize(model="qwen3:8b", total_tokens=150)
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

log = logging.getLogger(__name__)

_langfuse_client = None
_langfuse_available: bool | None = None


def _is_enabled() -> bool:
    """Check if Langfuse is configured and available."""
    global _langfuse_available
    if _langfuse_available is not None:
        return _langfuse_available

    secret_key = os.environ.get("LANGFUSE_SECRET_KEY", "")
    public_key = os.environ.get("LANGFUSE_PUBLIC_KEY", "")

    if not secret_key or not public_key:
        _langfuse_available = False
        return False

    try:
        import langfuse  # noqa: F401
        _langfuse_available = True
    except ImportError:
        log.debug("langfuse package not installed — tracing disabled")
        _langfuse_available = False

    return _langfuse_available


def _get_client():
    """Lazy-init Langfuse client singleton."""
    global _langfuse_client
    if _langfuse_client is not None:
        return _langfuse_client

    if not _is_enabled():
        return None

    try:
        from langfuse import Langfuse

        from orchestrator.config import get_settings

        langfuse_host = get_settings().services.langfuse_url
        if not langfuse_host:
            langfuse_host = os.environ.get("LANGFUSE_HOST", "")
        if not langfuse_host:
            log.warning("Langfuse host not configured in [services] langfuse_url or LANGFUSE_HOST env var")
            return None

        _langfuse_client = Langfuse(
            secret_key=os.environ["LANGFUSE_SECRET_KEY"],
            public_key=os.environ["LANGFUSE_PUBLIC_KEY"],
            host=langfuse_host,
            enabled=True,
        )
        log.info("Langfuse client initialized (host=%s)", langfuse_host)
    except Exception as exc:
        log.warning("Failed to initialize Langfuse: %s", exc)
        _langfuse_client = None

    return _langfuse_client


class LangfuseTracer:
    """Per-request Langfuse tracer — wraps a single Langfuse trace."""

    def __init__(
        self,
        request_id: str,
        session_id: str = "",
        query: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.request_id = request_id
        self.session_id = session_id
        self._trace = None
        self._spans: dict[str, Any] = {}
        self._start_time = time.perf_counter()

        client = _get_client()
        if client is None:
            return

        try:
            self._trace = client.trace(
                id=request_id,
                session_id=session_id or None,
                name="symbiont-pipeline",
                input={"query": query},
                metadata=metadata or {},
            )
        except Exception as exc:
            log.debug("Langfuse trace creation failed: %s", exc)

    @property
    def enabled(self) -> bool:
        return self._trace is not None

    def trace_node_start(self, node_name: str, *, input: dict[str, Any] | None = None) -> None:
        """Record the start of a pipeline node."""
        if not self._trace:
            return
        try:
            span = self._trace.span(
                name=node_name,
                input=input or {},
                metadata={"node_type": _infer_type(node_name)},
            )
            self._spans[node_name] = (span, time.perf_counter())
        except Exception as exc:
            log.debug("Langfuse span start failed for %s: %s", node_name, exc)

    def trace_node_end(
        self,
        node_name: str,
        *,
        output: dict[str, Any] | None = None,
        duration_ms: float | None = None,
        error: str | None = None,
    ) -> None:
        """Record the end of a pipeline node."""
        if not self._trace:
            return

        span_data = self._spans.pop(node_name, None)
        if span_data is None:
            return

        span, start = span_data
        try:
            if error:
                span.end(
                    output={"error": error},
                    level="ERROR",
                    status_message=error,
                )
            else:
                span.end(output=output or {})
        except Exception as exc:
            log.debug("Langfuse span end failed for %s: %s", node_name, exc)

    def trace_generation(
        self,
        name: str,
        *,
        model: str,
        input_messages: list[dict[str, str]] | None = None,
        output: str = "",
        usage: dict[str, int] | None = None,
        duration_ms: float = 0,
    ) -> None:
        """Record an LLM generation (token-level detail)."""
        if not self._trace:
            return
        try:
            self._trace.generation(
                name=name,
                model=model,
                input=input_messages or [],
                output=output,
                usage=usage or {},
                metadata={"duration_ms": round(duration_ms, 1)},
            )
        except Exception as exc:
            log.debug("Langfuse generation failed for %s: %s", name, exc)

    def trace_prewarm_decision(
        self,
        feature: str,
        *,
        hit: bool,
        confidence: float,
        source: str = "",
        reason: str = "",
    ) -> None:
        """Record a prewarming decision for analysis."""
        if not self._trace:
            return
        try:
            self._trace.event(
                name="prewarm_decision",
                input={
                    "feature": feature,
                    "confidence": round(confidence, 3),
                    "source": source,
                },
                output={
                    "hit": hit,
                    "reason": reason,
                },
                metadata={"type": "prewarm"},
            )
        except Exception as exc:
            log.debug("Langfuse prewarm event failed: %s", exc)

    def finalize(
        self,
        *,
        model: str = "",
        total_tokens: int = 0,
        response: str = "",
        intent: str = "",
        complexity: str = "",
        success: bool = True,
    ) -> None:
        """Finalize the trace with output and metadata."""
        if not self._trace:
            return
        try:
            total_ms = (time.perf_counter() - self._start_time) * 1000
            self._trace.update(
                output={"response": response[:2000]} if response else {},
                metadata={
                    "model": model,
                    "total_tokens": total_tokens,
                    "total_duration_ms": round(total_ms, 1),
                    "intent": intent,
                    "complexity": complexity,
                    "success": success,
                },
            )
        except Exception as exc:
            log.debug("Langfuse trace finalize failed: %s", exc)

        # Flush in background
        try:
            client = _get_client()
            if client:
                client.flush()
        except Exception:
            pass

    def score(self, name: str, value: float, comment: str = "") -> None:
        """Attach a score to this trace (for eval integration)."""
        if not self._trace:
            return
        try:
            self._trace.score(name=name, value=value, comment=comment)
        except Exception as exc:
            log.debug("Langfuse score failed: %s", exc)


def _infer_type(node_name: str) -> str:
    """Map node name to type for Langfuse metadata."""
    if node_name in ("classify", "route", "direct_respond", "synthesize", "critic"):
        return node_name
    if node_name.startswith("context_"):
        return "context"
    if node_name.startswith("agent_"):
        return "agent"
    return "other"
