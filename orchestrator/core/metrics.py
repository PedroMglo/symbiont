"""In-memory query metrics collector."""

from __future__ import annotations

import statistics
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any


@dataclass
class _QueryRecord:
    timestamp: float
    latency_ms: float
    intent: str
    complexity: str
    model: str
    context_tokens: int
    agentic: bool = False
    iterations: int = 0
    tools_invoked: list[str] = field(default_factory=list)


class MetricsCollector:
    """Ring-buffer metrics collector for query latencies and distributions.

    Thread-safe enough for single-writer (API layer) usage.  Not intended
    for high-concurrency production workloads.
    """

    def __init__(self, maxlen: int = 1000) -> None:
        self._records: deque[_QueryRecord] = deque(maxlen=maxlen)

    def record(self, result: Any) -> None:
        """Record an ``SymbiontResult`` (duck-typed)."""
        self._records.append(_QueryRecord(
            timestamp=time.time(),
            latency_ms=result.latency_ms,
            intent=result.intent.value if hasattr(result.intent, "value") else str(result.intent),
            complexity=result.complexity.value if hasattr(result.complexity, "value") else str(result.complexity),
            model=result.model_used,
            context_tokens=result.context_tokens,
            agentic=getattr(result, "agentic", False),
            iterations=getattr(result, "iterations", 0),
            tools_invoked=list(getattr(result, "tools_invoked", [])),
        ))

    def summary(self, *, window_seconds: int = 300) -> dict[str, Any]:
        """Return aggregated metrics.

        Args:
            window_seconds: Only include records from the last N seconds.
                            Use 0 for all-time stats.
        """
        now = time.time()
        if window_seconds > 0:
            cutoff = now - window_seconds
            records = [r for r in self._records if r.timestamp >= cutoff]
        else:
            records = list(self._records)

        if not records:
            return {
                "total_queries": 0,
                "window_seconds": window_seconds,
                "avg_latency_ms": 0,
                "p95_latency_ms": 0,
                "intent_distribution": {},
                "model_distribution": {},
                "avg_context_tokens": 0,
                "agentic_queries": 0,
                "agentic_ratio": 0.0,
                "avg_iterations": 0.0,
                "tool_usage": {},
            }

        latencies = [r.latency_ms for r in records]
        intent_dist: dict[str, int] = {}
        model_dist: dict[str, int] = {}
        tool_usage: dict[str, int] = {}
        agentic_count = 0
        total_iterations = 0

        for r in records:
            intent_dist[r.intent] = intent_dist.get(r.intent, 0) + 1
            model_dist[r.model] = model_dist.get(r.model, 0) + 1
            if r.agentic:
                agentic_count += 1
                total_iterations += r.iterations
                for tool in r.tools_invoked:
                    tool_usage[tool] = tool_usage.get(tool, 0) + 1

        sorted_lat = sorted(latencies)
        p95_idx = max(0, int(len(sorted_lat) * 0.95) - 1)

        return {
            "total_queries": len(records),
            "window_seconds": window_seconds,
            "avg_latency_ms": round(statistics.mean(latencies), 1),
            "p95_latency_ms": round(sorted_lat[p95_idx], 1),
            "intent_distribution": intent_dist,
            "model_distribution": model_dist,
            "avg_context_tokens": round(statistics.mean(r.context_tokens for r in records)),
            "agentic_queries": agentic_count,
            "agentic_ratio": round(agentic_count / len(records), 3),
            "avg_iterations": round(total_iterations / max(agentic_count, 1), 2),
            "tool_usage": tool_usage,
        }


# Module-level singleton
metrics = MetricsCollector()
