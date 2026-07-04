"""GPTCache-backed semantic cache for LLM responses and classification results.

Uses ONNX embeddings (via GPTCache's built-in Onnx embedding) for similarity
matching. Queries that are semantically similar to previous ones get cached
results, avoiding redundant LLM calls.

Two cache instances:
- classify_cache: caches intent/complexity classification (high hit rate expected)
- response_cache: caches direct_respond LLM outputs (moderate hit rate)
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Cache directory
_CACHE_DIR = Path(os.environ.get(
    "ORC_CACHE_DIR",
    "/tmp/orc-gptcache",
))

# Similarity threshold: 0.0-1.0 (higher = stricter matching)
_CLASSIFY_THRESHOLD = 0.90   # Classification is deterministic, high threshold OK
_RESPONSE_THRESHOLD = 0.95   # Response must be very similar query


@dataclass
class CacheStats:
    """Runtime cache statistics."""
    hits: int = 0
    misses: int = 0
    errors: int = 0
    avg_hit_latency_ms: float = 0.0

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total > 0 else 0.0


class SemanticCache:
    """Wrapper around GPTCache with ONNX embeddings and in-memory map store.

    Provides get/put semantics with semantic similarity matching:
    - get(query) → cached value if similar query exists, else None
    - put(query, value) → stores the result for future lookups
    """

    def __init__(
        self,
        name: str,
        *,
        similarity_threshold: float = 0.90,
        max_entries: int = 1000,
    ) -> None:
        self._name = name
        self._threshold = similarity_threshold
        self._max_entries = max_entries
        self._initialized = False
        self._cache: Any = None
        self._embedding: Any = None
        self._stats = CacheStats()

        # Fallback: simple dict cache keyed by normalized query hash
        self._fallback_store: dict[str, tuple[float, Any]] = {}  # hash → (timestamp, value)

    def initialize(self) -> None:
        """Initialize GPTCache with ONNX embedding."""
        if self._initialized:
            return

        try:
            from gptcache import Cache
            from gptcache.embedding import Onnx
            from gptcache.manager import CacheBase, VectorBase, get_data_manager
            from gptcache.similarity_evaluation.distance import SearchDistanceEvaluation

            self._embedding = Onnx()

            cache_dir = _CACHE_DIR / self._name
            cache_dir.mkdir(parents=True, exist_ok=True)

            cache_base = CacheBase("sqlite", sql_url=f"sqlite:///{cache_dir}/cache.db")
            vector_base = VectorBase(
                "faiss",
                dimension=self._embedding.dimension,
                top_k=1,
            )
            data_manager = get_data_manager(cache_base, vector_base)

            self._cache = Cache()
            self._cache.init(
                embedding_func=self._embedding.to_embeddings,
                data_manager=data_manager,
                similarity_evaluation=SearchDistanceEvaluation(),
            )

            self._initialized = True
            log.info("SemanticCache '%s' initialized (ONNX + FAISS)", self._name)

        except Exception as e:
            log.warning(
                "SemanticCache '%s' init failed (falling back to hash cache): %s",
                self._name, e,
            )
            self._initialized = True  # Mark as init so we use fallback

    def get(self, query: str) -> Any | None:
        """Look up a cached result for a semantically similar query."""
        if not self._initialized:
            self.initialize()

        start = time.time()

        # Try GPTCache first
        if self._cache is not None:
            try:
                from gptcache.adapter.api import get as cache_get
                result = cache_get(self._cache, query)
                if result is not None:
                    self._stats.hits += 1
                    elapsed = (time.time() - start) * 1000
                    self._update_avg_latency(elapsed)
                    return result
            except Exception as e:
                log.debug("GPTCache get error for '%s': %s", self._name, e)
                self._stats.errors += 1

        # Fallback: exact hash match
        key = self._hash_query(query)
        entry = self._fallback_store.get(key)
        if entry is not None:
            self._stats.hits += 1
            elapsed = (time.time() - start) * 1000
            self._update_avg_latency(elapsed)
            return entry[1]

        self._stats.misses += 1
        return None

    def put(self, query: str, value: Any) -> None:
        """Store a result for future semantic lookups."""
        if not self._initialized:
            self.initialize()

        # Store in GPTCache
        if self._cache is not None:
            try:
                from gptcache.adapter.api import put as cache_put
                cache_put(self._cache, query, value)
            except Exception as e:
                log.debug("GPTCache put error for '%s': %s", self._name, e)
                self._stats.errors += 1

        # Also store in fallback
        key = self._hash_query(query)
        self._fallback_store[key] = (time.time(), value)

        # Evict old entries if over capacity
        if len(self._fallback_store) > self._max_entries:
            oldest_key = min(self._fallback_store, key=lambda k: self._fallback_store[k][0])
            del self._fallback_store[oldest_key]

    @property
    def stats(self) -> CacheStats:
        return self._stats

    def _hash_query(self, query: str) -> str:
        """Normalize and hash a query for exact-match fallback."""
        normalized = " ".join(query.lower().split())
        return hashlib.sha256(normalized.encode()).hexdigest()[:16]

    def _update_avg_latency(self, elapsed_ms: float) -> None:
        """Update rolling average hit latency."""
        n = self._stats.hits
        if n == 1:
            self._stats.avg_hit_latency_ms = elapsed_ms
        else:
            self._stats.avg_hit_latency_ms = (
                self._stats.avg_hit_latency_ms * (n - 1) + elapsed_ms
            ) / n


# Singleton instances
_classify_cache: SemanticCache | None = None
_response_cache: SemanticCache | None = None


def get_classify_cache() -> SemanticCache:
    """Get or create the classification cache singleton."""
    global _classify_cache
    if _classify_cache is None:
        _classify_cache = SemanticCache(
            "classify",
            similarity_threshold=_CLASSIFY_THRESHOLD,
            max_entries=2000,
        )
    return _classify_cache


def get_response_cache() -> SemanticCache:
    """Get or create the response cache singleton."""
    global _response_cache
    if _response_cache is None:
        _response_cache = SemanticCache(
            "response",
            similarity_threshold=_RESPONSE_THRESHOLD,
            max_entries=500,
        )
    return _response_cache
