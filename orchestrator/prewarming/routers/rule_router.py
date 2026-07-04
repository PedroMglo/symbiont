"""Level 0 — Deterministic rule-based routing (sub-millisecond)."""

from __future__ import annotations

from orchestrator.prewarming.feature_catalog import FeatureCatalog
from orchestrator.prewarming.signals import RequestSignals
from orchestrator.prewarming.state import PrewarmPrediction


class RuleRouter:
    """Deterministic router using keyword and pattern matches from the catalog.

    This is the fastest router (no ML, no network). It returns high-confidence
    predictions when signals clearly indicate a specific service.
    """

    def __init__(self, catalog: FeatureCatalog) -> None:
        self._catalog = catalog

    def route(self, signals: RequestSignals, *, query: str = "") -> list[PrewarmPrediction]:
        """Match signals against catalog rules. Returns predictions sorted by confidence."""
        predictions: list[PrewarmPrediction] = []
        feature_scores: dict[str, tuple[float, list[str]]] = {}

        # --- Pattern matches (highest confidence) ---
        for fid, reasons in signals.pattern_matches.items():
            feat = self._catalog.get(fid)
            if not feat or not self._catalog.is_prewarm_target(fid):
                continue
            # Patterns are strong signals
            score = min(0.95, 0.80 + 0.05 * len(reasons))
            feature_scores[fid] = (score, reasons)

        # --- Keyword matches ---
        keyword_hits: dict[str, int] = {}  # feature_id → count
        for kw in signals.keywords_found:
            for fid in self._catalog.get_by_keyword(kw):
                if not self._catalog.is_prewarm_target(fid):
                    continue
                keyword_hits[fid] = keyword_hits.get(fid, 0) + 1

        for fid, count in keyword_hits.items():
            if fid in feature_scores:
                # Already matched by pattern — boost slightly
                old_score, reasons = feature_scores[fid]
                feature_scores[fid] = (min(0.98, old_score + 0.03 * count), reasons + [f"keywords:{count}"])
            else:
                # Keyword-only match: confidence based on absolute hits + ratio
                feat = self._catalog.get(fid)
                if not feat or not self._catalog.is_prewarm_target(fid):
                    continue
                total_keywords = len(feat.keywords)
                ratio = count / max(total_keywords, 1)
                # Use whichever gives higher confidence:
                # - Ratio-based: good when feature has few keywords
                # - Absolute-based: good when feature has many keywords (3+ hits = strong signal)
                ratio_score = 0.40 + ratio * 0.50
                abs_score = min(0.95, 0.50 + 0.15 * count)  # 1→0.65, 2→0.80, 3→0.95
                score = min(0.95, max(ratio_score, abs_score))
                feature_scores[fid] = (score, [f"keywords:{count}/{total_keywords}"])

        # --- File extension direct matches (very strong) ---
        if signals.file_extensions:
            for ext in signals.file_extensions:
                for fid in self._catalog.get_by_extension(ext):
                    if not self._catalog.is_prewarm_target(fid):
                        continue
                    if fid in feature_scores:
                        old_score, reasons = feature_scores[fid]
                        feature_scores[fid] = (min(0.98, old_score + 0.10), reasons + [f"ext:{ext}"])
                    else:
                        feature_scores[fid] = (0.90, [f"ext:{ext}"])

        # --- Negative gates: penalize scores when negative signals match ---
        q_lower = query.lower() if query else ""
        if q_lower:
            for fid in list(feature_scores.keys()):
                feat = self._catalog.get(fid)
                if not feat or not self._catalog.is_prewarm_target(fid):
                    continue
                neg_hits = 0
                # Check negative keywords
                for neg_kw in feat.negative_keywords:
                    if neg_kw.lower() in q_lower:
                        neg_hits += 1
                # Check negative patterns
                for neg_pat in self._catalog.get_negative_patterns(fid):
                    if neg_pat.search(q_lower):
                        neg_hits += 1
                if neg_hits > 0:
                    old_score, reasons = feature_scores[fid]
                    penalty = min(0.60, 0.25 * neg_hits)  # Cap at -0.60
                    new_score = max(0.0, old_score - penalty)
                    feature_scores[fid] = (new_score, reasons + [f"neg_gate:-{penalty:.2f}"])

        # Build predictions
        for fid, (score, reasons) in feature_scores.items():
            predictions.append(PrewarmPrediction(
                feature_id=fid,
                confidence=score,
                source="rules",
                reason="; ".join(reasons),
            ))

        # Sort by confidence descending
        predictions.sort(key=lambda p: p.confidence, reverse=True)
        return predictions
