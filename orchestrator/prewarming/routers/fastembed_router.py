"""Level 1 — FastEmbed CPU-based semantic router (ONNX, no GPU, no network).

Replaces TF-IDF char n-gram approach with proper sentence embeddings using
FastEmbed (ONNX Runtime). Runs entirely on CPU with ~1-3ms latency per query.

Advantages over TF-IDF:
- True semantic understanding (not just character overlap)
- Better handling of paraphrases and multilingual queries
- Pre-trained model, no vocabulary limited to specific example prompts
"""

from __future__ import annotations

import logging
import math
from typing import Any

from orchestrator.prewarming.feature_catalog import FeatureCatalog
from orchestrator.prewarming.state import PrewarmPrediction

log = logging.getLogger(__name__)

# Default model — small, fast, good multilingual performance
_DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


class FastEmbedRouter:
    """CPU-only semantic router using FastEmbed ONNX embeddings.

    Pre-computes feature centroid embeddings from generic service intent documents.
    At query time, embeds the query and does cosine similarity against all
    feature centroids. Total latency: ~1-3ms (after model warm-up).
    """

    def __init__(self, catalog: FeatureCatalog, *, model_name: str = _DEFAULT_MODEL) -> None:
        self._catalog = catalog
        self._model_name = model_name
        self._embedder: Any = None
        self._feature_centroids: dict[str, list[float]] = {}
        self._initialized = False

    def initialize(self) -> None:
        """Load the embedding model and pre-compute feature centroids."""
        if self._initialized:
            return

        try:
            from fastembed import TextEmbedding
        except ImportError:
            log.warning("fastembed not installed — FastEmbedRouter disabled")
            self._initialized = True
            return

        try:
            self._embedder = TextEmbedding(model_name=self._model_name)
        except Exception as e:
            log.warning("FastEmbed model load failed: %s — router disabled", e)
            self._initialized = True
            return

        feature_docs: dict[str, list[str]] = {}
        for fid, feat in self._catalog.get_prewarm_targets().items():
            docs = list(feat.intent_documents())
            if docs:
                feature_docs[fid] = docs

        if not feature_docs:
            log.warning("No service intent documents in catalog — FastEmbedRouter has no data")
            self._initialized = True
            return

        # Embed all documents and compute centroids per feature
        all_docs: list[str] = []
        doc_feature_map: list[str] = []  # doc_index → feature_id

        for fid, docs in feature_docs.items():
            for doc in docs:
                all_docs.append(doc)
                doc_feature_map.append(fid)

        # Batch embed all docs at once (efficient)
        try:
            all_embeddings = list(self._embedder.embed(all_docs))
        except Exception as e:
            log.warning("FastEmbed batch embedding failed: %s", e)
            self._initialized = True
            return

        # Compute centroid per feature (mean of all intent document embeddings)
        feature_vecs: dict[str, list[list[float]]] = {}
        for idx, emb in enumerate(all_embeddings):
            fid = doc_feature_map[idx]
            feature_vecs.setdefault(fid, []).append(list(emb))

        for fid, vecs in feature_vecs.items():
            dim = len(vecs[0])
            centroid = [
                sum(v[i] for v in vecs) / len(vecs)
                for i in range(dim)
            ]
            self._feature_centroids[fid] = centroid

        self._initialized = True
        log.info(
            "FastEmbedRouter initialized: model=%s, %d features, %d docs",
            self._model_name, len(self._feature_centroids), len(all_docs),
        )

    def route(self, query: str, *, top_k: int = 3) -> list[PrewarmPrediction]:
        """Route a query using FastEmbed cosine similarity against centroids."""
        if not self._initialized or not self._feature_centroids or not self._embedder:
            return []

        # Embed the query
        try:
            query_embedding = list(next(iter(self._embedder.embed([query]))))
        except Exception as e:
            log.debug("FastEmbed query embedding failed: %s", e)
            return []

        # Compute similarities against all centroids
        similarities: list[tuple[str, float]] = []
        for fid, centroid in self._feature_centroids.items():
            sim = _cosine_similarity(query_embedding, centroid)
            similarities.append((fid, sim))

        # Sort by similarity descending
        similarities.sort(key=lambda x: x[1], reverse=True)
        top = similarities[:top_k]

        # Convert to predictions with confidence mapping
        predictions: list[PrewarmPrediction] = []
        for fid, sim in top:
            # FastEmbed similarities tend to be higher than TF-IDF
            # Map: sim < 0.3 → noise, 0.3-0.5 → low, 0.5-0.7 → medium, 0.7+ → high
            confidence = max(0.0, min(0.95, (sim - 0.25) / 0.50))
            if confidence > 0.15:
                predictions.append(PrewarmPrediction(
                    feature_id=fid,
                    confidence=confidence,
                    source="embedding",
                    reason=f"fastembed_sim={sim:.3f}",
                ))

        return predictions
