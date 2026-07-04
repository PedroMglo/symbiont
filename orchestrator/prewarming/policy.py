"""Policy engine — decides which containers to actually prewarm."""

from __future__ import annotations

import logging

from orchestrator.config import PrewarmConfig
from orchestrator.prewarming.aggregator import PrewarmCandidate
from orchestrator.prewarming.feature_catalog import FeatureCatalog
from orchestrator.prewarming.learning import LearningLoop
from orchestrator.prewarming.state import PrewarmAction

log = logging.getLogger(__name__)


class PolicyEngine:
    """Applies resource-aware policies to prewarm candidates.

    Enforces:
    - Maximum total containers prewarmed per request
    - Maximum GPU containers prewarmed per request
    - Per-feature confidence thresholds (from catalog + learning loop adjustment)
    - Already-running containers don't count against limits
    - Features with prewarm_policy == "never" are skipped
    """

    def __init__(self, cfg: PrewarmConfig, catalog: FeatureCatalog, *, learning: LearningLoop | None = None) -> None:
        self._cfg = cfg
        self._catalog = catalog
        self._learning = learning

    def decide(
        self,
        candidates: list[PrewarmCandidate],
        *,
        gpu_pressure: float = 0.0,
    ) -> list[PrewarmAction]:
        """Decide which candidates to actually prewarm.

        Args:
            candidates: Scored candidates from the aggregator (sorted by score).
            gpu_pressure: Current GPU VRAM pressure (0.0-1.0).

        Returns:
            List of prewarm actions to execute.
        """
        actions: list[PrewarmAction] = []
        prewarm_count = 0
        gpu_prewarm_count = 0

        for candidate in candidates:
            feat = self._catalog.get(candidate.feature_id)
            if not feat:
                continue

            # Skip features that should never be prewarmed.
            if not self._catalog.is_prewarm_target(candidate.feature_id):
                continue

            # Already running — no action needed, don't count against limits
            if candidate.is_running:
                actions.append(PrewarmAction(
                    feature_id=candidate.feature_id,
                    container_name=feat.container_name,
                    action="already_running",
                    score=candidate.final_score,
                    priority=feat.priority,
                ))
                continue

            # Check limits
            if prewarm_count >= self._cfg.max_prewarm_per_request:
                actions.append(PrewarmAction(
                    feature_id=candidate.feature_id,
                    container_name=feat.container_name,
                    action="skip",
                    score=candidate.final_score,
                    priority=feat.priority,
                ))
                continue

            # GPU limit check
            if feat.uses_gpu:
                if gpu_prewarm_count >= self._cfg.max_gpu_prewarm_per_request:
                    actions.append(PrewarmAction(
                        feature_id=candidate.feature_id,
                        container_name=feat.container_name,
                        action="skip",
                        score=candidate.final_score,
                        priority=feat.priority,
                    ))
                    continue
                # Additional: skip GPU containers under high VRAM pressure
                if gpu_pressure > 0.8:
                    log.debug(
                        "Skipping GPU prewarm for %s: VRAM pressure %.0f%%",
                        candidate.feature_id, gpu_pressure * 100,
                    )
                    actions.append(PrewarmAction(
                        feature_id=candidate.feature_id,
                        container_name=feat.container_name,
                        action="skip",
                        score=candidate.final_score,
                        priority=feat.priority,
                    ))
                    continue

            # Per-feature confidence threshold (from catalog + learning adjustment)
            feat_threshold = feat.prewarm_threshold
            if self._learning:
                feat_threshold += self._learning.get_threshold_adjustment(candidate.feature_id)
            feat_threshold = max(0.50, min(0.95, feat_threshold))

            if candidate.final_score >= feat_threshold:
                actions.append(PrewarmAction(
                    feature_id=candidate.feature_id,
                    container_name=feat.container_name,
                    action="prewarm_now",
                    score=candidate.final_score,
                    priority=feat.priority,
                ))
                prewarm_count += 1
                if feat.uses_gpu:
                    gpu_prewarm_count += 1
            else:
                # Below threshold — skip
                actions.append(PrewarmAction(
                    feature_id=candidate.feature_id,
                    container_name=feat.container_name,
                    action="skip",
                    score=candidate.final_score,
                    priority=feat.priority,
                ))

        return actions
