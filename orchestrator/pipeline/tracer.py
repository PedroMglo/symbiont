"""LangGraph observability tracer — captures node-level execution events.

Implements a LangChain BaseCallbackHandler that:
- Records start/end/error for each graph node
- Creates OTel spans (nested under the parent graph.invoke span)
- Emits structured events to the EventDispatcher → ClickHouse graph_node_events
- Produces a graph_runs summary row at the end of execution
- Optionally emits Langfuse traces (if LANGFUSE_SECRET_KEY is set)

Thread-safe: parallel nodes (via Send()) are handled with a lock around _active_runs.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler

from orchestrator.observability.semantic_attributes import (
    ATTR_GRAPH_NODE_NAME,
    ATTR_GRAPH_NODE_TYPE,
    ATTR_GRAPH_RUN_ID,
    base_attributes,
)
from orchestrator.pipeline.langfuse_tracer import LangfuseTracer

log = logging.getLogger(__name__)

# Node types inferred from node name prefixes
_NODE_TYPE_MAP: dict[str, str] = {
    "classify": "classify",
    "route": "route",
    "llm_fallback": "llm_fallback",
    "direct_respond": "direct",
    "collect_context": "collect",
    "collect_agents": "collect",
    "critic": "critic",
    "synthesize": "synthesize",
    "learn": "learn",
}


def _infer_node_type(node_name: str) -> str:
    """Infer node type from node name."""
    if node_name in _NODE_TYPE_MAP:
        return _NODE_TYPE_MAP[node_name]
    if node_name.startswith("context_"):
        return "context"
    if node_name.startswith("agent_"):
        return "agent"
    return "other"


def _now_ts() -> str:
    """Current UTC timestamp as ClickHouse DateTime64(3) string."""
    dt = datetime.now(tz=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M:%S.") + f"{int(dt.microsecond / 1000):03d}"


class GraphObservabilityTracer(BaseCallbackHandler):
    """LangChain callback handler for tracing LangGraph node execution.

    Created per-request in the /query endpoint. Records each node's
    start/end/error and writes to ClickHouse via the EventDispatcher.
    """

    raise_error = False  # Never crash the graph due to tracing

    def __init__(
        self,
        request_id: str,
        session_id: str = "",
        graph_run_id: str | None = None,
        query: str = "",
    ) -> None:
        super().__init__()
        self.request_id = request_id
        self.session_id = session_id
        self.graph_run_id = graph_run_id or uuid.uuid4().hex[:16]
        self._lock = threading.Lock()
        self._active_runs: dict[str, dict[str, Any]] = {}
        self._completed_nodes: list[dict[str, Any]] = []
        self._graph_start_time: float = time.perf_counter()
        self._graph_start_ts: str = _now_ts()
        self._otel_spans: dict[str, Any] = {}

        # Langfuse trace (no-op if not configured)
        self._langfuse = LangfuseTracer(
            request_id=request_id,
            session_id=session_id,
            query=query,
            metadata={"graph_run_id": self.graph_run_id},
        )

    # ------------------------------------------------------------------
    # LangChain Callback Interface
    # ------------------------------------------------------------------

    def on_chain_start(
        self,
        serialized: dict[str, Any] | None,
        inputs: dict[str, Any] | Any,
        *,
        run_id: uuid.UUID,
        parent_run_id: uuid.UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        """Called when a graph node starts execution."""
        # LangGraph v0.2+ only: filter to top-level node steps
        # (skip graph-level wrapper and internal seq:step sub-calls)
        if not metadata or "langgraph_node" not in metadata:
            return
        if tags and not any(t.startswith("graph:step") for t in tags):
            return

        node_name = self._extract_node_name(serialized, tags, metadata)
        if not node_name or node_name in ("LangGraph", "RunnableSequence", "__start__"):
            return

        run_info = {
            "node_name": node_name,
            "node_type": _infer_node_type(node_name),
            "start_time": time.perf_counter(),
            "start_ts": _now_ts(),
            "parent_run_id": str(parent_run_id) if parent_run_id else "",
            "input_keys": sorted(inputs.keys()) if isinstance(inputs, dict) else [],
            "langgraph_step": metadata.get("langgraph_step", 0) if metadata else 0,
        }

        # Start OTel span
        try:
            from orchestrator.observability.traces import get_tracer
            tracer = get_tracer("ai-symbiont.graph")
            span = tracer.start_as_current_span(
                f"graph.{node_name}",
                attributes=base_attributes(
                    component="pipeline.graph",
                    trace_kind="graph_node",
                    run_id=self.graph_run_id,
                    request_id=self.request_id,
                    session_id=self.session_id,
                    **{
                        ATTR_GRAPH_RUN_ID: self.graph_run_id,
                        ATTR_GRAPH_NODE_NAME: node_name,
                        ATTR_GRAPH_NODE_TYPE: run_info["node_type"],
                    },
                ),
            )
            otel_ctx = span.__enter__()
            self._otel_spans[str(run_id)] = (span, otel_ctx)

            # Capture trace/span IDs
            from orchestrator.observability.traces import current_trace_context
            trace_id, span_id = current_trace_context()
            run_info["trace_id"] = trace_id or ""
            run_info["span_id"] = span_id or ""
        except Exception:
            run_info["trace_id"] = ""
            run_info["span_id"] = ""

        with self._lock:
            self._active_runs[str(run_id)] = run_info

        # Langfuse span
        self._langfuse.trace_node_start(
            node_name,
            input={"keys": run_info["input_keys"], "step": run_info["langgraph_step"]},
        )

    def on_chain_end(
        self,
        outputs: dict[str, Any],
        *,
        run_id: uuid.UUID,
        parent_run_id: uuid.UUID | None = None,
        **kwargs: Any,
    ) -> None:
        """Called when a graph node completes successfully."""
        run_key = str(run_id)

        with self._lock:
            run_info = self._active_runs.pop(run_key, None)

        if run_info is None:
            return

        duration_ms = (time.perf_counter() - run_info["start_time"]) * 1000
        output_keys = sorted(outputs.keys()) if isinstance(outputs, dict) else []

        # End OTel span
        self._end_otel_span(run_key, success=True)

        node_record = {
            "timestamp": run_info["start_ts"],
            "graph_run_id": self.graph_run_id,
            "request_id": self.request_id,
            "session_id": self.session_id,
            "trace_id": run_info.get("trace_id", ""),
            "span_id": run_info.get("span_id", ""),
            "parent_span_id": "",
            "node_name": run_info["node_name"],
            "node_type": run_info["node_type"],
            "duration_ms": round(duration_ms, 2),
            "success": 1,
            "error_type": "",
            "error_message": "",
            "tokens_used": self._extract_tokens(outputs),
            "iteration": 0,
            "parallel_group": run_info["parent_run_id"],
            "input_keys": run_info["input_keys"],
            "output_keys": output_keys,
        }

        with self._lock:
            self._completed_nodes.append(node_record)

        # Langfuse span end
        self._langfuse.trace_node_end(
            run_info["node_name"],
            output={"keys": output_keys, "tokens": node_record["tokens_used"]},
            duration_ms=duration_ms,
        )

        # If node performed an LLM call, record it as a Langfuse generation
        if isinstance(outputs, dict):
            model_used = outputs.get("model_used", "")
            tokens_used = node_record["tokens_used"]
            if model_used or tokens_used:
                self._langfuse.trace_generation(
                    name=f"{run_info['node_name']}_llm",
                    model=model_used or "unknown",
                    output=str(outputs.get("response", ""))[:2000],
                    usage={"total_tokens": tokens_used} if tokens_used else {},
                    duration_ms=duration_ms,
                )

        # Write to ClickHouse immediately
        self._write_node_event(node_record)

    def on_chain_error(
        self,
        error: BaseException,
        *,
        run_id: uuid.UUID,
        parent_run_id: uuid.UUID | None = None,
        **kwargs: Any,
    ) -> None:
        """Called when a graph node raises an error."""
        run_key = str(run_id)

        with self._lock:
            run_info = self._active_runs.pop(run_key, None)

        if run_info is None:
            return

        duration_ms = (time.perf_counter() - run_info["start_time"]) * 1000

        # End OTel span with error
        self._end_otel_span(run_key, success=False, error=error)

        node_record = {
            "timestamp": run_info["start_ts"],
            "graph_run_id": self.graph_run_id,
            "request_id": self.request_id,
            "session_id": self.session_id,
            "trace_id": run_info.get("trace_id", ""),
            "span_id": run_info.get("span_id", ""),
            "parent_span_id": "",
            "node_name": run_info["node_name"],
            "node_type": run_info["node_type"],
            "duration_ms": round(duration_ms, 2),
            "success": 0,
            "error_type": type(error).__name__,
            "error_message": str(error)[:500],
            "tokens_used": 0,
            "iteration": 0,
            "parallel_group": run_info["parent_run_id"],
            "input_keys": run_info["input_keys"],
            "output_keys": [],
        }

        with self._lock:
            self._completed_nodes.append(node_record)

        # Langfuse error span
        self._langfuse.trace_node_end(
            run_info["node_name"],
            error=f"{type(error).__name__}: {str(error)[:200]}",
            duration_ms=duration_ms,
        )

        self._write_node_event(node_record)

    # ------------------------------------------------------------------
    # Summary — called after graph.invoke() completes
    # ------------------------------------------------------------------

    def finalize(self, final_state: dict[str, Any]) -> None:
        """Write the graph_runs summary row after graph.invoke() returns.

        Should be called by the /query endpoint after graph.invoke() completes.
        """
        total_duration_ms = (time.perf_counter() - self._graph_start_time) * 1000

        with self._lock:
            nodes = list(self._completed_nodes)

        path = [n["node_name"] for n in nodes]
        agents_invoked = [
            n["node_name"].removeprefix("agent_")
            for n in nodes if n["node_type"] == "agent"
        ]
        context_sources = [
            n["node_name"].removeprefix("context_")
            for n in nodes if n["node_type"] == "context"
        ]

        intent = final_state.get("intent")
        complexity = final_state.get("complexity")

        run_record = {
            "timestamp": self._graph_start_ts,
            "graph_run_id": self.graph_run_id,
            "request_id": self.request_id,
            "session_id": self.session_id,
            "trace_id": "",
            "total_duration_ms": round(total_duration_ms, 2),
            "node_count": len(nodes),
            "success": 1 if all(n["success"] for n in nodes) else 0,
            "error_type": "",
            "path": path,
            "agents_invoked": agents_invoked,
            "context_sources": context_sources,
            "intent": intent.value if intent else "",
            "complexity": complexity.value if complexity else "",
            "confidence": round(final_state.get("confidence", 0), 3),
            "fallback_used": 1 if final_state.get("fallback_used") else 0,
            "critic_invoked": 1 if any(n["node_name"] == "critic" for n in nodes) else 0,
            "critic_loops": max(0, sum(1 for n in nodes if n["node_name"] == "critic") - 1),
            "iterations": final_state.get("iterations", 0),
            "total_tokens": final_state.get("tokens_used", 0),
            "model_used": final_state.get("model_used", ""),
        }

        self._write_graph_run(run_record)

        # Langfuse trace finalization
        self._langfuse.finalize(
            model=final_state.get("model_used", ""),
            total_tokens=final_state.get("tokens_used", 0),
            response=final_state.get("response", "")[:2000],
            intent=intent.value if intent else "",
            complexity=complexity.value if complexity else "",
            success=run_record["success"] == 1,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _extract_node_name(
        self,
        serialized: dict[str, Any] | None,
        tags: list[str] | None,
        metadata: dict[str, Any] | None,
    ) -> str:
        """Extract the meaningful node name from callback metadata."""
        # LangGraph v0.2+ passes node name in metadata["langgraph_node"]
        if metadata and "langgraph_node" in metadata:
            return metadata["langgraph_node"]

        # Fallback: serialized name (LangGraph v0.2+ passes serialized=None)
        if serialized:
            name = serialized.get("name", "")
            if name:
                return name
            id_list = serialized.get("id", [])
            if id_list:
                return id_list[-1]

        return ""

    def _extract_tokens(self, outputs: dict[str, Any]) -> int:
        """Extract tokens_used from node outputs if present."""
        if isinstance(outputs, dict):
            return int(outputs.get("tokens_used", 0) or 0)
        return 0

    def _end_otel_span(self, run_key: str, success: bool, error: BaseException | None = None) -> None:
        """End an OTel span for a given run."""
        span_data = self._otel_spans.pop(run_key, None)
        if span_data is None:
            return
        try:
            span_ctx_mgr, otel_span = span_data
            if not success and otel_span and hasattr(otel_span, "set_status"):
                try:
                    from opentelemetry.trace import StatusCode
                    otel_span.set_status(StatusCode.ERROR, str(error) if error else "error")
                except ImportError:
                    pass
            span_ctx_mgr.__exit__(None, None, None)
        except Exception:
            pass

    def _write_node_event(self, record: dict[str, Any]) -> None:
        """Write a single node event to ClickHouse via the dispatcher."""
        try:
            from orchestrator.observability import _dispatcher
            if _dispatcher and _dispatcher._clickhouse_sink and _dispatcher._clickhouse_sink.available:
                # Convert input_keys/output_keys to ClickHouse Array format
                row = dict(record)
                row["input_keys"] = record.get("input_keys", [])
                row["output_keys"] = record.get("output_keys", [])
                _dispatcher._clickhouse_sink.write_to_table("graph_node_events", row)
        except Exception as exc:
            log.debug("GraphTracer: failed to write node event: %s", exc)

    def _write_graph_run(self, record: dict[str, Any]) -> None:
        """Write the graph_runs summary row to ClickHouse."""
        try:
            from orchestrator.observability import _dispatcher
            if _dispatcher and _dispatcher._clickhouse_sink and _dispatcher._clickhouse_sink.available:
                _dispatcher._clickhouse_sink.write_to_table("graph_runs", record)
        except Exception as exc:
            log.debug("GraphTracer: failed to write graph run: %s", exc)

    # ------------------------------------------------------------------
    # Properties for inspection
    # ------------------------------------------------------------------

    def trace_llm_generation(
        self,
        name: str,
        *,
        model: str,
        input_messages: list[dict[str, str]] | None = None,
        output: str = "",
        usage: dict[str, int] | None = None,
        duration_ms: float = 0,
    ) -> None:
        """Record an LLM generation to Langfuse (called by pipeline nodes after LLM call)."""
        self._langfuse.trace_generation(
            name=name,
            model=model,
            input_messages=input_messages,
            output=output,
            usage=usage or {},
            duration_ms=duration_ms,
        )

    @property
    def completed_nodes(self) -> list[dict[str, Any]]:
        """Get list of completed node records (for testing/debugging)."""
        with self._lock:
            return list(self._completed_nodes)

    @property
    def total_duration_ms(self) -> float:
        """Elapsed time since graph started."""
        return (time.perf_counter() - self._graph_start_time) * 1000
