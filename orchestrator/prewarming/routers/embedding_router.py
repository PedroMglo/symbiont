"""Level 1 — Embedding-based service-intent routing (~10ms)."""

from __future__ import annotations

import logging
import math
import time

import httpx

from orchestrator.prewarming.feature_catalog import FeatureCatalog
from orchestrator.prewarming.state import PrewarmPrediction

log = logging.getLogger(__name__)


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors (pure Python)."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _mean_vectors(vectors: list[list[float]]) -> list[float]:
    """Compute element-wise mean of multiple vectors."""
    if not vectors:
        return []
    dim = len(vectors[0])
    n = len(vectors)
    return [sum(v[i] for v in vectors) / n for i in range(dim)]


class EmbeddingRouter:
    """Semantic similarity router using pre-computed feature embeddings.

    Computes embeddings for generic feature intent documents on initialization,
    then compares incoming queries via cosine similarity.
    """

    def __init__(
        self,
        catalog: FeatureCatalog,
        *,
        ollama_base_url: str,
        model: str = "nomic-embed-text",
    ) -> None:
        self._catalog = catalog
        self._ollama_url = ollama_base_url.rstrip("/")
        self._model = model
        self._feature_embeddings: dict[str, list[float]] = {}  # feature_id → mean embedding
        self._initialized = False

    async def initialize(self) -> None:
        """Pre-compute embeddings for all feature intent documents."""
        if self._initialized:
            return

        start = time.time()
        all_texts: list[tuple[str, str]] = []  # (feature_id, text)

        for fid, feat in self._catalog.get_prewarm_targets().items():
            for document in feat.intent_documents():
                all_texts.append((fid, document))

        if not all_texts:
            log.warning("No service intent documents in catalog — embedding router disabled")
            self._initialized = True
            return

        # Batch embed all intent documents
        texts = [t[1] for t in all_texts]
        embeddings = await self._batch_embed(texts)

        if embeddings is None:
            log.warning("Failed to compute embeddings — embedding router disabled")
            self._initialized = True
            return

        # Compute mean embedding per feature
        feature_vectors: dict[str, list[list[float]]] = {}
        for (fid, _), emb in zip(all_texts, embeddings):
            feature_vectors.setdefault(fid, []).append(emb)

        for fid, vectors in feature_vectors.items():
            self._feature_embeddings[fid] = _mean_vectors(vectors)

        elapsed = (time.time() - start) * 1000
        log.info(
            "Embedding router initialized: %d features, %d intent documents in %.0fms",
            len(self._feature_embeddings), len(all_texts), elapsed,
        )
        self._initialized = True

    async def route(self, query: str, *, top_k: int = 3) -> list[PrewarmPrediction]:
        """Route a query using semantic similarity against feature embeddings."""
        if not self._initialized or not self._feature_embeddings:
            return []

        query_embedding = await self._embed_single(query)
        if query_embedding is None:
            return []

        # Compute similarities
        similarities: list[tuple[str, float]] = []
        for fid, feat_emb in self._feature_embeddings.items():
            sim = _cosine_similarity(query_embedding, feat_emb)
            similarities.append((fid, sim))

        # Sort by similarity descending, take top-k
        similarities.sort(key=lambda x: x[1], reverse=True)
        top = similarities[:top_k]

        # Convert to predictions (normalize similarity to confidence)
        predictions: list[PrewarmPrediction] = []
        for fid, sim in top:
            # Similarity is typically 0.3-0.9 range; normalize to confidence
            confidence = max(0.0, min(1.0, (sim - 0.3) / 0.6))
            if confidence > 0.1:  # Only include meaningful matches
                predictions.append(PrewarmPrediction(
                    feature_id=fid,
                    confidence=confidence,
                    source="embedding",
                    reason=f"cosine_sim={sim:.3f}",
                ))

        return predictions

    async def _embed_single(self, text: str) -> list[float] | None:
        """Embed a single text string."""
        result = await self._batch_embed([text])
        if result is not None and len(result) > 0:
            return result[0]
        return None

    async def _batch_embed(self, texts: list[str]) -> list[list[float]] | None:
        """Batch embed multiple texts via Ollama API.

        Supports both old (/api/embeddings, single) and new (/api/embed, batch) endpoints.
        """
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                # Try new batch endpoint first (/api/embed)
                resp = await client.post(
                    f"{self._ollama_url}/api/embed",
                    json={"model": self._model, "input": texts},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    embeddings_raw = data.get("embeddings", [])
                    if embeddings_raw:
                        return [list(e) for e in embeddings_raw]

                # Fallback to old single endpoint (/api/embeddings)
                results = []
                for text in texts:
                    resp = await client.post(
                        f"{self._ollama_url}/api/embeddings",
                        json={"model": self._model, "prompt": text},
                    )
                    if resp.status_code != 200:
                        log.warning("Ollama embeddings failed: %d %s", resp.status_code, resp.text[:200])
                        return None
                    data = resp.json()
                    emb = data.get("embedding", [])
                    if not emb:
                        return None
                    results.append(emb)
                return results

        except (httpx.HTTPError, Exception) as e:
            log.warning("Embedding request failed: %s", e)
            return None
