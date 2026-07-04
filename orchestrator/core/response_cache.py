"""Response cache — LRU cache with TTL for symbiont responses.

Avoids redundant LLM calls for repeated/similar queries. Cache size is
derived from adaptive config (RAM-aware).
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CacheEntry:
    """A cached response with metadata."""

    response: str
    model_used: str
    intent: str
    context_tokens: int
    created_at: float
    hit_count: int = 0


class ResponseCache:
    """Thread-safe LRU response cache with TTL eviction.

    Keys are computed from (query, intent, model, context_sources).
    Skips caching for agentic queries, tool-using queries, and system state queries.
    """

    # Intents that should never be cached (state-dependent)
    _SKIP_INTENTS = frozenset({"SYSTEM", "SYSTEM_AND_LOCAL"})

    def __init__(self, max_size: int = 5000, ttl_seconds: float = 3600.0):
        self._max_size = max_size
        self._ttl = ttl_seconds
        self._cache: OrderedDict[str, CacheEntry] = OrderedDict()
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._cache)

    @property
    def hit_rate(self) -> float:
        total = self._hits + self._misses
        return self._hits / total if total > 0 else 0.0

    @property
    def stats(self) -> dict:
        with self._lock:
            return {
                "size": len(self._cache),
                "max_size": self._max_size,
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": round(self.hit_rate, 3),
                "ttl_seconds": self._ttl,
            }

    def get(
        self,
        query: str,
        intent: str,
        model: str,
        sources: list[str] | None = None,
        *,
        workspace_fingerprint: str | None = None,
        evidence_fingerprint: str | None = None,
        tool_result_hashes: list[str] | None = None,
        language: str | None = None,
        session_id: str | None = None,
    ) -> CacheEntry | None:
        """Look up a cached response. Returns None on miss."""
        if intent in self._SKIP_INTENTS:
            return None
        if self._uses_local_evidence(sources, evidence_fingerprint):
            self._misses += 1
            log.debug(
                "Response cache bypassed for local evidence task: workspace=%s evidence=%s",
                bool(workspace_fingerprint),
                bool(evidence_fingerprint),
            )
            return None

        key = self._make_key(
            query,
            intent,
            model,
            sources,
            workspace_fingerprint=workspace_fingerprint,
            evidence_fingerprint=evidence_fingerprint,
            tool_result_hashes=tool_result_hashes,
            language=language,
            session_id=session_id,
        )
        now = time.time()

        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                self._misses += 1
                return None

            # TTL check
            if now - entry.created_at > self._ttl:
                del self._cache[key]
                self._misses += 1
                return None

            # Move to end (most recently used)
            self._cache.move_to_end(key)
            self._hits += 1
            return entry

    def put(
        self,
        query: str,
        intent: str,
        model: str,
        response: str,
        context_tokens: int = 0,
        sources: list[str] | None = None,
        *,
        agentic: bool = False,
        tools_used: bool = False,
        workspace_fingerprint: str | None = None,
        evidence_fingerprint: str | None = None,
        tool_result_hashes: list[str] | None = None,
        language: str | None = None,
        session_id: str | None = None,
    ) -> None:
        """Store a response in the cache.

        Skips storage for agentic queries, tool-using queries, and system intents.
        """
        if agentic or tools_used:
            return
        if self._uses_local_evidence(sources, evidence_fingerprint):
            log.debug("Response cache storage skipped for local evidence task")
            return
        if intent in self._SKIP_INTENTS:
            return
        if not response or len(response) < 10:
            return

        key = self._make_key(
            query,
            intent,
            model,
            sources,
            workspace_fingerprint=workspace_fingerprint,
            evidence_fingerprint=evidence_fingerprint,
            tool_result_hashes=tool_result_hashes,
            language=language,
            session_id=session_id,
        )
        entry = CacheEntry(
            response=response,
            model_used=model,
            intent=intent,
            context_tokens=context_tokens,
            created_at=time.time(),
        )

        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                self._cache[key] = entry
            else:
                self._cache[key] = entry
                # Evict oldest if over capacity
                while len(self._cache) > self._max_size:
                    self._cache.popitem(last=False)

    def invalidate(self, query: str | None = None) -> int:
        """Invalidate cache entries. If query is None, clear all."""
        with self._lock:
            if query is None:
                count = len(self._cache)
                self._cache.clear()
                return count
            # Remove entries matching the query hash prefix
            prefix = hashlib.sha256(query.lower().strip().encode()).hexdigest()[:16]
            to_remove = [k for k in self._cache if k.startswith(prefix)]
            for k in to_remove:
                del self._cache[k]
            return len(to_remove)

    def evict_expired(self) -> int:
        """Remove all expired entries. Returns count evicted."""
        now = time.time()
        with self._lock:
            expired = [k for k, v in self._cache.items() if now - v.created_at > self._ttl]
            for k in expired:
                del self._cache[k]
            return len(expired)

    def shrink(self, new_max_size: int) -> int:
        """Shrink cache to new_max_size (for pressure response). Returns evicted count."""
        with self._lock:
            evicted = 0
            while len(self._cache) > new_max_size:
                self._cache.popitem(last=False)
                evicted += 1
            self._max_size = new_max_size
            return evicted

    @staticmethod
    def _make_key(
        query: str,
        intent: str,
        model: str,
        sources: list[str] | None,
        *,
        workspace_fingerprint: str | None = None,
        evidence_fingerprint: str | None = None,
        tool_result_hashes: list[str] | None = None,
        language: str | None = None,
        session_id: str | None = None,
    ) -> str:
        """Create a deterministic cache key from query parameters."""
        normalized = query.lower().strip()
        sources_str = ",".join(sorted(sources)) if sources else ""
        tools_str = ",".join(sorted(tool_result_hashes or []))
        raw = (
            f"{normalized}|{intent}|{model}|{sources_str}|"
            f"workspace={workspace_fingerprint or ''}|"
            f"evidence={evidence_fingerprint or ''}|"
            f"tools={tools_str}|"
            f"language={language or ''}|"
            f"session={session_id or ''}"
        )
        return hashlib.sha256(raw.encode()).hexdigest()

    @staticmethod
    def _uses_local_evidence(sources: list[str] | None, evidence_fingerprint: str | None) -> bool:
        return bool(evidence_fingerprint) or "evidence" in {str(source) for source in (sources or [])}


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_cache: ResponseCache | None = None


def get_response_cache() -> ResponseCache:
    """Get or create the singleton response cache (size from adaptive config)."""
    global _cache
    if _cache is None:
        try:
            from orchestrator.core.adaptive_config import get_adaptive_overrides
            overrides = get_adaptive_overrides()
            max_size = overrides.response_cache_max_size
        except Exception:
            max_size = 5000
        _cache = ResponseCache(max_size=max_size)
        log.info("Response cache initialized: max_size=%d", max_size)
    return _cache


def _reset_cache() -> None:
    """Reset singleton — for testing."""
    global _cache
    _cache = None
