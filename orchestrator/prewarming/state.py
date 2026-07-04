"""Per-request prewarm state tracking."""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class PrewarmPrediction:
    """A single prediction for a feature."""

    feature_id: str
    confidence: float
    source: str  # "rules" | "embedding" | "classifier"
    reason: str = ""


@dataclass
class PrewarmAction:
    """A decided prewarm action."""

    feature_id: str
    container_name: str
    action: str  # "prewarm_now" | "skip" | "already_running"
    score: float = 0.0
    priority: int = 0


@dataclass
class PrewarmTimestamps:
    """Per-phase timestamps for latency decomposition."""

    request_received: float = 0.0
    l0_done: float = 0.0
    l1_done: float = 0.0
    l2_started: float = 0.0
    l2_done: float = 0.0
    prewarm_requested: float = 0.0
    container_start_called: float = 0.0
    pipeline_done: float = 0.0

    def to_dict(self) -> dict[str, float]:
        """Return non-zero timestamps as dict with ms deltas from request_received."""
        base = self.request_received
        if not base:
            return {}
        result = {}
        for field_name in (
            "l0_done", "l1_done", "l2_started", "l2_done",
            "prewarm_requested", "container_start_called", "pipeline_done",
        ):
            val = getattr(self, field_name)
            if val:
                result[field_name + "_ms"] = round((val - base) * 1000, 2)
        return result


@dataclass
class PrewarmState:
    """Tracks prewarm decisions for a single request lifecycle."""

    request_id: str
    predictions: list[PrewarmPrediction] = field(default_factory=list)
    actions: list[PrewarmAction] = field(default_factory=list)
    containers_started: set[str] = field(default_factory=set)
    containers_used: set[str] = field(default_factory=set)
    latency_ms: float = 0.0
    created_at: float = field(default_factory=time.time)
    timestamps: PrewarmTimestamps = field(default_factory=PrewarmTimestamps)

    def mark_used(self, feature_id: str) -> None:
        """Mark a feature as actually used by the pipeline."""
        self.containers_used.add(feature_id)

    @property
    def unused_containers(self) -> set[str]:
        """Containers that were prewarmed but not used."""
        return self.containers_started - self.containers_used

    @property
    def hit_rate(self) -> float:
        """Fraction of prewarmed containers that were actually used."""
        if not self.containers_started:
            return 0.0
        return len(self.containers_used & self.containers_started) / len(self.containers_started)


@dataclass
class PrewarmMetrics:
    """Cumulative metrics since engine boot — never resets with request cleanup."""

    total_requests: int = 0
    total_containers_started: int = 0
    total_containers_used: int = 0
    total_containers_wasted: int = 0
    total_l0_hits: int = 0
    total_l1_hits: int = 0
    total_l2_invocations: int = 0
    total_cold_starts_saved_ms: float = 0.0
    _guard_blocks: int = 0  # Requests blocked by DirectAnswerGuard
    # Per-feature hit/miss tracking
    feature_hits: dict[str, int] = field(default_factory=dict)
    feature_misses: dict[str, int] = field(default_factory=dict)
    feature_starts: dict[str, int] = field(default_factory=dict)
    # Latency percentiles (last N samples)
    _latency_samples: list[float] = field(default_factory=list)
    _max_samples: int = 200

    def record_request(self, state: PrewarmState, *, count_request: bool = True) -> None:
        """Record metrics from a completed request."""
        if count_request:
            self.total_requests += 1
        started = len(state.containers_started)
        used = len(state.containers_used & state.containers_started)
        wasted = started - used

        self.total_containers_started += started
        self.total_containers_used += used
        self.total_containers_wasted += wasted

        # Per-feature tracking
        for fid in state.containers_started:
            self.feature_starts[fid] = self.feature_starts.get(fid, 0) + 1
            if fid in state.containers_used:
                self.feature_hits[fid] = self.feature_hits.get(fid, 0) + 1
            else:
                self.feature_misses[fid] = self.feature_misses.get(fid, 0) + 1

        # L0/L1/L2 hit tracking
        for pred in state.predictions:
            if pred.source == "rules":
                self.total_l0_hits += 1
            elif pred.source == "embedding":
                self.total_l1_hits += 1
            elif pred.source == "classifier":
                self.total_l2_invocations += 1

        # Latency tracking
        if state.latency_ms > 0:
            self._latency_samples.append(state.latency_ms)
            if len(self._latency_samples) > self._max_samples:
                self._latency_samples = self._latency_samples[-self._max_samples:]

    @property
    def hit_rate(self) -> float:
        if self.total_containers_started == 0:
            return 0.0
        return self.total_containers_used / self.total_containers_started

    @property
    def false_positive_rate(self) -> float:
        if self.total_containers_started == 0:
            return 0.0
        return self.total_containers_wasted / self.total_containers_started

    @property
    def p95_latency_ms(self) -> float:
        if not self._latency_samples:
            return 0.0
        sorted_samples = sorted(self._latency_samples)
        idx = int(len(sorted_samples) * 0.95)
        return sorted_samples[min(idx, len(sorted_samples) - 1)]

    def to_dict(self) -> dict:
        return {
            "total_requests": self.total_requests,
            "guard_blocks": self._guard_blocks,
            "total_containers_started": self.total_containers_started,
            "total_containers_used": self.total_containers_used,
            "total_containers_wasted": self.total_containers_wasted,
            "hit_rate": round(self.hit_rate, 3),
            "false_positive_rate": round(self.false_positive_rate, 3),
            "total_l0_hits": self.total_l0_hits,
            "total_l1_hits": self.total_l1_hits,
            "total_l2_invocations": self.total_l2_invocations,
            "p95_latency_ms": round(self.p95_latency_ms, 2),
            "feature_starts": dict(self.feature_starts),
            "feature_hits": dict(self.feature_hits),
            "feature_misses": dict(self.feature_misses),
        }
