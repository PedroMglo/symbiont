"""Level 1.5 — Semantic Route layer (invoked only when L1 is ambiguous).

Implements the "Semantic Router" pattern: each feature defines a Route with
generic service intent documents. At init, those documents are embedded via FastEmbed.
At query time, the query is embedded and matched against individual utterances
(not centroids) — this gives higher recall for varied phrasings.

This layer runs ONLY when L1 scores are ambiguous (top-2 gap < threshold),
adding ~2-5ms to resolve the ambiguity before falling through to the
expensive L2 micro-classifier.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any

from orchestrator.prewarming.feature_catalog import FeatureCatalog
from orchestrator.prewarming.state import PrewarmPrediction

log = logging.getLogger(__name__)

_DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"


@dataclass
class SemanticRoute:
    """A single route — maps a feature to its utterance embeddings."""

    feature_id: str
    utterances: list[str] = field(default_factory=list)
    embeddings: list[list[float]] = field(default_factory=list)


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


class SemanticRouterAdapter:
    """Semantic routing layer that disambiguates when L1 centroids are too close.

    Unlike FastEmbedRouter (which uses centroids), this compares the query
    against EVERY individual utterance — more expensive but better at
    distinguishing between similar features.

    Only invoked when L1 ambiguity gap is below threshold.
    """

    def __init__(
        self,
        catalog: FeatureCatalog,
        *,
        model_name: str = _DEFAULT_MODEL,
        similarity_threshold: float = 0.55,
    ) -> None:
        self._catalog = catalog
        self._model_name = model_name
        self._similarity_threshold = similarity_threshold
        self._embedder: Any = None
        self._routes: list[SemanticRoute] = []
        self._initialized = False

    def initialize(self) -> None:
        """Load model and embed all utterances per route."""
        if self._initialized:
            return

        try:
            from fastembed import TextEmbedding
        except ImportError:
            log.warning("fastembed not installed — SemanticRouterAdapter disabled")
            self._initialized = True
            return

        try:
            self._embedder = TextEmbedding(model_name=self._model_name)
        except Exception as e:
            log.warning("SemanticRouter model load failed: %s", e)
            self._initialized = True
            return

        # Build routes from generic catalog intent documents.
        all_texts: list[str] = []
        text_to_route: list[int] = []  # index in all_texts → route index

        for fid, feat in self._catalog.get_prewarm_targets().items():
            route = SemanticRoute(feature_id=fid)
            utterances = list(feat.intent_documents())
            route.utterances = utterances

            route_idx = len(self._routes)
            self._routes.append(route)
            for utt in utterances:
                all_texts.append(utt)
                text_to_route.append(route_idx)

        if not all_texts:
            log.warning("No utterances for SemanticRouter — disabled")
            self._initialized = True
            return

        # Batch embed all utterances
        try:
            all_embeddings = list(self._embedder.embed(all_texts))
        except Exception as e:
            log.warning("SemanticRouter embedding failed: %s", e)
            self._initialized = True
            return

        # Assign embeddings to routes
        for idx, emb in enumerate(all_embeddings):
            route_idx = text_to_route[idx]
            self._routes[route_idx].embeddings.append(list(emb))

        self._initialized = True
        log.info(
            "SemanticRouterAdapter initialized: %d routes, %d utterances",
            len(self._routes), len(all_texts),
        )

    def route(
        self,
        query: str,
        *,
        candidate_features: list[str] | None = None,
        top_k: int = 3,
    ) -> list[PrewarmPrediction]:
        """Route a query against utterance embeddings.

        Args:
            query: The user query to route.
            candidate_features: If provided, only consider these features
                (used to narrow search when L1 already has candidates).
            top_k: Max predictions to return.
        """
        if not self._initialized or not self._routes or not self._embedder:
            return []

        # Embed query
        try:
            query_emb = list(next(iter(self._embedder.embed([query]))))
        except Exception as e:
            log.debug("SemanticRouter query embedding failed: %s", e)
            return []

        # Score each route by max similarity to any utterance
        route_scores: list[tuple[str, float]] = []

        for route in self._routes:
            if candidate_features and route.feature_id not in candidate_features:
                continue
            if not route.embeddings:
                continue

            # Max similarity (not mean) — picks the closest matching utterance
            max_sim = max(
                _cosine_similarity(query_emb, emb)
                for emb in route.embeddings
            )
            if max_sim >= self._similarity_threshold:
                route_scores.append((route.feature_id, max_sim))

        # Sort descending
        route_scores.sort(key=lambda x: x[1], reverse=True)
        top = route_scores[:top_k]

        predictions: list[PrewarmPrediction] = []
        for fid, sim in top:
            # Map similarity to confidence
            confidence = max(0.0, min(0.95, (sim - 0.40) / 0.40))
            predictions.append(PrewarmPrediction(
                feature_id=fid,
                confidence=confidence,
                source="embedding",
                reason=f"semantic_route_sim={sim:.3f}",
            ))

        return predictions
