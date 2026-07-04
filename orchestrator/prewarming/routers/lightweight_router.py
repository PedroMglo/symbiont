"""Level 1 — Lightweight CPU-based semantic router (no GPU, no network).

Uses TF-IDF character n-gram vectorization for fast approximate semantic matching.
Runs entirely in-process with <1ms latency and zero external dependencies beyond stdlib.
"""

from __future__ import annotations

import logging
import math
import re
from collections import Counter

from orchestrator.prewarming.feature_catalog import FeatureCatalog
from orchestrator.prewarming.state import PrewarmPrediction

log = logging.getLogger(__name__)

# N-gram parameters tuned for multilingual short queries (PT/EN)
_NGRAM_MIN = 2
_NGRAM_MAX = 4


def _tokenize(text: str) -> list[str]:
    """Lowercase + split on non-alphanumeric (keeps accented chars)."""
    return re.findall(r"[\w]+", text.lower())


def _char_ngrams(text: str, n_min: int = _NGRAM_MIN, n_max: int = _NGRAM_MAX) -> list[str]:
    """Generate character n-grams from text."""
    text = text.lower().strip()
    ngrams = []
    for n in range(n_min, n_max + 1):
        for i in range(len(text) - n + 1):
            ngrams.append(text[i:i + n])
    return ngrams


def _build_vocabulary(documents: list[str]) -> dict[str, int]:
    """Build vocabulary mapping ngram → index from all documents."""
    vocab: dict[str, int] = {}
    for doc in documents:
        for ngram in _char_ngrams(doc):
            if ngram not in vocab:
                vocab[ngram] = len(vocab)
    return vocab


def _tfidf_vector(
    text: str, vocab: dict[str, int], idf: list[float]
) -> list[float]:
    """Compute TF-IDF vector for a text given vocabulary and IDF weights."""
    ngrams = _char_ngrams(text)
    if not ngrams:
        return [0.0] * len(vocab)

    tf_counts = Counter(ngrams)
    total = len(ngrams)
    vec = [0.0] * len(vocab)

    for ngram, count in tf_counts.items():
        if ngram in vocab:
            idx = vocab[ngram]
            tf = count / total
            vec[idx] = tf * idf[idx]
    return vec


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


class LightweightRouter:
    """CPU-only semantic router using TF-IDF character n-grams.

    Pre-computes feature vectors from generic service intent documents.
    At query time, computes a single vector and does cosine similarity
    against all feature centroids. Total latency: <1ms.
    """

    def __init__(self, catalog: FeatureCatalog) -> None:
        self._catalog = catalog
        self._vocab: dict[str, int] = {}
        self._idf: list[float] = []
        self._feature_vectors: dict[str, list[float]] = {}
        self._initialized = False

    def initialize(self) -> None:
        """Build TF-IDF vectors from catalog service intent documents."""
        if self._initialized:
            return

        all_docs: list[str] = []
        feature_docs: dict[str, list[str]] = {}

        for fid, feat in self._catalog.get_prewarm_targets().items():
            docs = list(feat.intent_documents())
            feature_docs[fid] = docs
            all_docs.extend(docs)

        if not all_docs:
            log.warning("No service intent documents in catalog — lightweight router disabled")
            self._initialized = True
            return

        # Build vocabulary from all documents
        self._vocab = _build_vocabulary(all_docs)
        vocab_size = len(self._vocab)

        if vocab_size == 0:
            self._initialized = True
            return

        # Compute IDF (inverse document frequency)
        n_docs = len(all_docs)
        doc_freq = [0] * vocab_size

        for doc in all_docs:
            seen = set()
            for ngram in _char_ngrams(doc):
                if ngram in self._vocab and ngram not in seen:
                    doc_freq[self._vocab[ngram]] += 1
                    seen.add(ngram)

        self._idf = [
            math.log((n_docs + 1) / (df + 1)) + 1.0  # smoothed IDF
            for df in doc_freq
        ]

        # Compute centroid vector per feature (mean of all example vectors)
        for fid, docs in feature_docs.items():
            vectors = [_tfidf_vector(doc, self._vocab, self._idf) for doc in docs]
            if vectors:
                centroid = [
                    sum(v[i] for v in vectors) / len(vectors)
                    for i in range(vocab_size)
                ]
                self._feature_vectors[fid] = centroid

        self._initialized = True
        log.info(
            "Lightweight router initialized: %d features, %d docs, vocab_size=%d",
            len(self._feature_vectors), len(all_docs), vocab_size,
        )

    def route(self, query: str, *, top_k: int = 3) -> list[PrewarmPrediction]:
        """Route a query using TF-IDF cosine similarity."""
        if not self._initialized or not self._feature_vectors:
            return []

        query_vec = _tfidf_vector(query, self._vocab, self._idf)

        # Compute similarities
        similarities: list[tuple[str, float]] = []
        for fid, feat_vec in self._feature_vectors.items():
            sim = _cosine_similarity(query_vec, feat_vec)
            similarities.append((fid, sim))

        # Sort by similarity descending
        similarities.sort(key=lambda x: x[1], reverse=True)
        top = similarities[:top_k]

        # Convert to predictions
        predictions: list[PrewarmPrediction] = []
        for fid, sim in top:
            # Map similarity to confidence:
            # sim < 0.1 → noise, 0.1-0.3 → low, 0.3-0.5 → medium, 0.5+ → high
            confidence = max(0.0, min(0.95, (sim - 0.05) / 0.55))
            if confidence > 0.15:
                predictions.append(PrewarmPrediction(
                    feature_id=fid,
                    confidence=confidence,
                    source="embedding",
                    reason=f"tfidf_sim={sim:.3f}",
                ))

        return predictions
