"""Simple learning loop — auto-tunes prewarm thresholds based on hit/miss history.

Strategy (no ML, pure heuristics):
- If a feature is consistently hit (high accuracy): lower its confidence threshold slightly
- If a feature is consistently wasted (low accuracy): raise its threshold
- If a feature is cheap and frequently used: increase its TTL
- Adjustments are bounded and conservative (max ±0.05 per cycle)

Runs periodically (e.g. every 50 requests) and adjusts the aggregator/policy weights.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

# Bounds for threshold adjustments
_MIN_THRESHOLD = 0.50
_MAX_THRESHOLD = 0.95
_ADJUSTMENT_STEP = 0.03
_MIN_SAMPLES = 10  # Need at least this many samples before adjusting


@dataclass
class FeatureLearningState:
    """Per-feature learning state."""

    hits: int = 0
    misses: int = 0
    threshold_adjustment: float = 0.0  # Cumulative adjustment from baseline

    @property
    def total(self) -> int:
        return self.hits + self.misses

    @property
    def accuracy(self) -> float:
        if self.total == 0:
            return 0.0
        return self.hits / self.total


class LearningLoop:
    """Auto-tunes prewarm behaviour based on observed hit/miss patterns.

    Does NOT modify the PrewarmConfig directly. Instead, produces per-feature
    threshold adjustments that the PolicyEngine can query.
    """

    def __init__(self, *, cycle_interval: int = 50) -> None:
        self._cycle_interval = cycle_interval
        self._requests_since_cycle = 0
        self._feature_states: dict[str, FeatureLearningState] = {}
        self._feature_threshold_overrides: dict[str, float] = {}

    def record_outcome(self, feature_id: str, was_used: bool) -> None:
        """Record whether a prewarmed container was actually used."""
        state = self._feature_states.setdefault(feature_id, FeatureLearningState())
        if was_used:
            state.hits += 1
        else:
            state.misses += 1

        self._requests_since_cycle += 1
        if self._requests_since_cycle >= self._cycle_interval:
            self._run_cycle()
            self._requests_since_cycle = 0

    def get_threshold_adjustment(self, feature_id: str) -> float:
        """Get the learned threshold adjustment for a feature.

        Returns a value to ADD to the base threshold:
        - Negative = lower threshold (more aggressive prewarming)
        - Positive = raise threshold (more conservative)
        """
        return self._feature_threshold_overrides.get(feature_id, 0.0)

    def get_effective_threshold(self, feature_id: str, base_threshold: float) -> float:
        """Get effective threshold for a feature (base + adjustment)."""
        adj = self.get_threshold_adjustment(feature_id)
        return max(_MIN_THRESHOLD, min(_MAX_THRESHOLD, base_threshold + adj))

    def get_accuracy(self, feature_id: str) -> float | None:
        """Get current accuracy for a feature, or None if no data."""
        state = self._feature_states.get(feature_id)
        if not state or state.total == 0:
            return None
        return state.accuracy

    def _run_cycle(self) -> None:
        """Run one learning cycle — adjust thresholds based on accumulated data."""
        adjusted = 0
        for fid, state in self._feature_states.items():
            if state.total < _MIN_SAMPLES:
                continue

            accuracy = state.accuracy
            old_adj = state.threshold_adjustment

            if accuracy >= 0.80:
                # High accuracy — can be more aggressive (lower threshold)
                state.threshold_adjustment = max(
                    old_adj - _ADJUSTMENT_STEP, -0.15
                )
            elif accuracy <= 0.30:
                # Low accuracy — be more conservative (raise threshold)
                state.threshold_adjustment = min(
                    old_adj + _ADJUSTMENT_STEP, 0.15
                )
            # else: accuracy between 0.30-0.80, keep current adjustment

            if state.threshold_adjustment != old_adj:
                self._feature_threshold_overrides[fid] = state.threshold_adjustment
                adjusted += 1

        if adjusted:
            log.info(
                "Learning cycle: adjusted %d features. Overrides: %s",
                adjusted,
                {k: f"{v:+.3f}" for k, v in self._feature_threshold_overrides.items()},
            )

    def get_status(self) -> dict:
        """Return learning loop status for diagnostics."""
        return {
            "requests_since_cycle": self._requests_since_cycle,
            "cycle_interval": self._cycle_interval,
            "feature_states": {
                fid: {
                    "hits": s.hits,
                    "misses": s.misses,
                    "accuracy": round(s.accuracy, 3),
                    "threshold_adj": round(s.threshold_adjustment, 3),
                }
                for fid, s in self._feature_states.items()
                if s.total > 0
            },
            "active_overrides": dict(self._feature_threshold_overrides),
        }
