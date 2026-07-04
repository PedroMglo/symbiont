"""Score aggregation — combines results from all routing levels."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from orchestrator.config import PrewarmConfig
from orchestrator.prewarming.feature_catalog import FeatureCatalog
from orchestrator.prewarming.state import PrewarmPrediction

log = logging.getLogger(__name__)


@dataclass
class PrewarmCandidate:
    """Aggregated candidate for prewarming with final score."""

    feature_id: str
    final_score: float
    raw_confidence: float
    sources: list[str]
    reason: str
    is_running: bool = False
    startup_cost: str = "low"
    uses_gpu: bool = False


class Aggregator:
    """Combines predictions from all routing levels with system state.

    Score formula:
        final_score = raw_confidence
                    + rule_boost (if from rules)
                    + recent_usage_boost (if recently used)
                    - startup_cost_penalty (if expensive to start)
                    - gpu_pressure_penalty (if GPU under pressure)
                    + already_running_bonus (if container is already up)
    """

    def __init__(self, cfg: PrewarmConfig, catalog: FeatureCatalog) -> None:
        self._cfg = cfg
        self._catalog = catalog
        self._recent_usage: dict[str, float] = {}  # feature_id → last_used_timestamp

    def record_usage(self, feature_id: str) -> None:
        """Record that a feature was recently used (for boosting)."""
        self._recent_usage[feature_id] = time.time()

    def aggregate(
        self,
        rule_results: list[PrewarmPrediction],
        embedding_results: list[PrewarmPrediction],
        classifier_results: list[PrewarmPrediction],
        *,
        running_containers: set[str] | None = None,
        gpu_pressure: float = 0.0,  # 0.0 = no pressure, 1.0 = full
    ) -> list[PrewarmCandidate]:
        """Aggregate all router outputs into final scored candidates."""
        running = running_containers or set()

        # Merge all predictions by feature_id
        merged: dict[str, dict] = {}

        for pred in rule_results:
            entry = merged.setdefault(pred.feature_id, {
                "confidences": [], "sources": [], "reasons": []
            })
            entry["confidences"].append(pred.confidence)
            entry["sources"].append(pred.source)
            entry["reasons"].append(pred.reason)

        for pred in embedding_results:
            entry = merged.setdefault(pred.feature_id, {
                "confidences": [], "sources": [], "reasons": []
            })
            entry["confidences"].append(pred.confidence)
            entry["sources"].append(pred.source)
            entry["reasons"].append(pred.reason)

        for pred in classifier_results:
            entry = merged.setdefault(pred.feature_id, {
                "confidences": [], "sources": [], "reasons": []
            })
            entry["confidences"].append(pred.confidence)
            entry["sources"].append(pred.source)
            entry["reasons"].append(pred.reason)

        # Score each candidate
        candidates: list[PrewarmCandidate] = []
        now = time.time()

        for fid, entry in merged.items():
            feat = self._catalog.get(fid)
            if not feat:
                continue
            if not self._catalog.is_prewarm_target(fid):
                continue

            # Base: max confidence across all sources
            raw_confidence = max(entry["confidences"])
            score = raw_confidence

            # Rule boost: reward deterministic matches
            if "rules" in entry["sources"]:
                score += self._cfg.rule_boost

            # Recent usage boost (decays over 5 minutes)
            last_used = self._recent_usage.get(fid, 0.0)
            if last_used > 0:
                age = now - last_used
                if age < 300:  # Within 5 minutes
                    decay = 1.0 - (age / 300)
                    score += self._cfg.recent_usage_boost * decay

            # Startup cost penalty
            cost_map = {"low": 0.0, "medium": 0.5, "high": 1.0}
            cost_factor = cost_map.get(feat.startup_cost, 0.0)
            score -= self._cfg.startup_cost_penalty * cost_factor

            # GPU pressure penalty
            if feat.uses_gpu and gpu_pressure > 0:
                score -= self._cfg.gpu_pressure_penalty * gpu_pressure

            # Already running bonus
            is_running = fid in running or feat.container_name in running
            if is_running:
                score += self._cfg.already_running_bonus

            candidates.append(PrewarmCandidate(
                feature_id=fid,
                final_score=min(1.0, max(0.0, score)),
                raw_confidence=raw_confidence,
                sources=entry["sources"],
                reason="; ".join(entry["reasons"]),
                is_running=is_running,
                startup_cost=feat.startup_cost,
                uses_gpu=feat.uses_gpu,
            ))

        # Sort by final score descending
        candidates.sort(key=lambda c: c.final_score, reverse=True)
        return candidates
