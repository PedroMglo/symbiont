"""Ollama implementation of the EmbeddingProvider protocol."""

from __future__ import annotations

import asyncio
import logging
import time
from functools import lru_cache

import httpx

from rag_config import settings

log = logging.getLogger(__name__)

_MAX_RETRIES = 2
_RETRY_BACKOFF = (1.0, 3.0)  # seconds between retries
_GPU_FIRST_NUM_GPU = 999
_CPU_FALLBACK_NUM_GPU = 0


class OllamaEmbeddingProvider:
    """EmbeddingProvider backed by an Ollama server.

    Reads ``base_url``, ``embedding_model``, and timeouts from config
    when not provided explicitly.
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        model: str | None = None,
    ) -> None:
        self._base_url = base_url or settings.ollama.base_url
        self._model = model or settings.ollama.embedding_model
        self._cache_size = settings.retrieval.embedding_cache_size
        self._async_client: httpx.AsyncClient | None = None

        # Per-instance LRU cache (sync)
        @lru_cache(maxsize=self._cache_size)
        def _cached_embed(text: str) -> tuple[float, ...]:
            return tuple(self.embed_texts([text])[0])

        self._cached_embed = _cached_embed

        # Async embedding cache (simple dict with bounded size)
        self._async_cache: dict[str, list[float]] = {}

        # Persistent on-disk embedding cache (ingestion only — lazy init)
        self._persistent_cache = None
        self._persistent_cache_init = False

    # ------------------------------------------------------------------
    # Async client lifecycle
    # ------------------------------------------------------------------

    async def _get_async_client(self) -> httpx.AsyncClient:
        """Return a shared async HTTP client (created on first use)."""
        if self._async_client is None or self._async_client.is_closed:
            self._async_client = httpx.AsyncClient(
                timeout=float(settings.performance.embedding_timeout),
            )
        return self._async_client

    async def aclose(self) -> None:
        """Close the async HTTP client."""
        if self._async_client is not None and not self._async_client.is_closed:
            await self._async_client.aclose()
            self._async_client = None

    # ------------------------------------------------------------------
    # Sync API (kept for pipeline/batch ingestion)
    # ------------------------------------------------------------------

    def _embed_texts_with_options(
        self,
        texts: list[str],
        *,
        options: dict[str, int] | None = None,
        max_retries: int = _MAX_RETRIES,
    ) -> list[list[float]]:
        """Generate embeddings via Ollama API (batch) with retry."""
        last_exc: Exception | None = None
        t0 = time.time()
        attempts = 0
        payload: dict[str, object] = {"model": self._model, "input": texts}
        if options is not None:
            payload["options"] = options

        for attempt in range(max_retries + 1):
            attempts = attempt + 1
            try:
                response = httpx.post(
                    f"{self._base_url}/api/embed",
                    json=payload,
                    timeout=float(settings.performance.embedding_timeout),
                )
                response.raise_for_status()
                result: list[list[float]] = response.json()["embeddings"]

                from observability import emit, is_enabled
                if is_enabled():
                    from observability import EventName, RAGEvent
                    emit(RAGEvent(
                        event=EventName.EMBEDDING_BATCH,
                        batch_size=len(texts),
                        batch_chars=sum(len(t) for t in texts),
                        latency_ms=(time.time() - t0) * 1000,
                        model_used=self._model,
                        retry_count=attempts - 1,
                        success=True,
                    ))

                return result
            except (httpx.HTTPStatusError, httpx.TimeoutException, httpx.ConnectError) as exc:
                last_exc = exc
                if attempt < max_retries:
                    wait = _RETRY_BACKOFF[min(attempt, len(_RETRY_BACKOFF) - 1)]
                    log.warning(
                        "Embedding retry %d/%d após erro: %s — aguardando %.0fs",
                        attempt + 1, max_retries, exc, wait,
                    )
                    time.sleep(wait)
                else:
                    log.error("Embedding falhou após %d tentativas: %s", max_retries + 1, exc)

                    from observability import emit, is_enabled
                    if is_enabled():
                        from observability import EventName, RAGEvent
                        emit(RAGEvent(
                            event=EventName.EMBEDDING_BATCH,
                            batch_size=len(texts),
                            batch_chars=sum(len(t) for t in texts),
                            latency_ms=(time.time() - t0) * 1000,
                            model_used=self._model,
                            retry_count=attempts - 1,
                            success=False,
                            error_type=type(exc).__name__,
                        ))
        raise last_exc  # type: ignore[misc]

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings via Ollama API (batch) with default runtime policy."""
        return self._embed_texts_with_options(texts)

    def embed_texts_gpu_first(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings preferring GPU, with explicit CPU fallback.

        Intended only for exceptional full rebuild paths. Normal query-time
        API calls must continue using :meth:`embed_texts`.
        """
        try:
            return self._embed_texts_with_options(
                texts,
                options={"num_gpu": _GPU_FIRST_NUM_GPU},
                max_retries=0,
            )
        except Exception as exc:
            log.warning(
                "GPU-first embedding failed; falling back to CPU for this batch: %s",
                exc,
            )

        try:
            return self._embed_texts_with_options(
                texts,
                options={"num_gpu": _CPU_FALLBACK_NUM_GPU},
                max_retries=0,
            )
        except Exception as exc:
            log.warning(
                "Explicit CPU embedding fallback failed; retrying Ollama defaults: %s",
                exc,
            )
            return self.embed_texts(texts)

    def get_query_embedding(self, text: str) -> list[float]:
        """Get embedding for a single query text (cached)."""
        return list(self._cached_embed(text))

    # ------------------------------------------------------------------
    # Persistent cache (ingestion path) — reuse vectors across runs
    # ------------------------------------------------------------------

    def _get_persistent_cache(self):
        """Lazily open the on-disk embedding cache (None if disabled)."""
        if self._persistent_cache_init:
            return self._persistent_cache
        self._persistent_cache_init = True
        try:
            if not getattr(settings.retrieval, "embedding_cache_persistent", True):
                self._persistent_cache = None
                return None
            from pathlib import Path

            from embeddings.cache import EmbeddingCache
            db_path = Path(settings.paths.data_dir) / "embedding_cache.db"
            self._persistent_cache = EmbeddingCache(db_path)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("Persistent embedding cache disabled: %s", exc)
            self._persistent_cache = None
        return self._persistent_cache

    def _embed_texts_cached_with(
        self,
        texts: list[str],
        embed_missing,
    ) -> list[list[float]]:
        """Like :meth:`embed_texts` but reuses a persistent on-disk cache.

        Only texts not already cached (by SHA-256 + model) are sent to Ollama.
        Intended for the ingest pipeline — query embeddings should use
        :meth:`get_query_embedding` to avoid polluting the cache with one-off
        queries.
        """
        cache = self._get_persistent_cache()
        if cache is None:
            return self.embed_texts(texts)

        from embeddings.cache import text_sha256
        hashes = [text_sha256(t) for t in texts]
        cached = cache.get_many(hashes, self._model)

        missing_idx = [i for i, h in enumerate(hashes) if h not in cached]
        if missing_idx:
            missing_texts = [texts[i] for i in missing_idx]
            fresh = embed_missing(missing_texts)
            new_items: list[tuple[str, list[float]]] = []
            for j, i in enumerate(missing_idx):
                cached[hashes[i]] = fresh[j]
                new_items.append((hashes[i], fresh[j]))
            cache.put_many(new_items, self._model)

        return [cached[h] for h in hashes]

    def embed_texts_cached(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings, reusing persistent cache where possible."""
        return self._embed_texts_cached_with(texts, self.embed_texts)

    def embed_texts_cached_gpu_first(self, texts: list[str]) -> list[list[float]]:
        """Persistent-cache embedding path that prefers GPU for cache misses."""
        return self._embed_texts_cached_with(texts, self.embed_texts_gpu_first)

    # ------------------------------------------------------------------
    # Async API (for query-time retrieval — non-blocking)
    # ------------------------------------------------------------------

    async def embed_texts_async(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings via Ollama API (batch) with async retry."""
        last_exc: Exception | None = None
        t0 = time.time()
        attempts = 0
        client = await self._get_async_client()

        for attempt in range(_MAX_RETRIES + 1):
            attempts = attempt + 1
            try:
                response = await client.post(
                    f"{self._base_url}/api/embed",
                    json={"model": self._model, "input": texts},
                )
                response.raise_for_status()
                result: list[list[float]] = response.json()["embeddings"]

                from observability import emit, is_enabled
                if is_enabled():
                    from observability import EventName, RAGEvent
                    emit(RAGEvent(
                        event=EventName.EMBEDDING_BATCH,
                        batch_size=len(texts),
                        batch_chars=sum(len(t) for t in texts),
                        latency_ms=(time.time() - t0) * 1000,
                        model_used=self._model,
                        retry_count=attempts - 1,
                        success=True,
                    ))

                return result
            except (httpx.HTTPStatusError, httpx.TimeoutException, httpx.ConnectError) as exc:
                last_exc = exc
                if attempt < _MAX_RETRIES:
                    wait = _RETRY_BACKOFF[min(attempt, len(_RETRY_BACKOFF) - 1)]
                    log.warning(
                        "Embedding async retry %d/%d após erro: %s — aguardando %.0fs",
                        attempt + 1, _MAX_RETRIES, exc, wait,
                    )
                    await asyncio.sleep(wait)
                else:
                    log.error("Embedding async falhou após %d tentativas: %s", _MAX_RETRIES + 1, exc)

                    from observability import emit, is_enabled
                    if is_enabled():
                        from observability import EventName, RAGEvent
                        emit(RAGEvent(
                            event=EventName.EMBEDDING_BATCH,
                            batch_size=len(texts),
                            batch_chars=sum(len(t) for t in texts),
                            latency_ms=(time.time() - t0) * 1000,
                            model_used=self._model,
                            retry_count=attempts - 1,
                            success=False,
                            error_type=type(exc).__name__,
                        ))
        raise last_exc  # type: ignore[misc]

    async def get_query_embedding_async(self, text: str) -> list[float]:
        """Get embedding for a single query text (async, cached)."""
        if text in self._async_cache:
            return self._async_cache[text]
        result = (await self.embed_texts_async([text]))[0]
        # Bounded cache — evict oldest if over limit
        if len(self._async_cache) >= self._cache_size:
            oldest_key = next(iter(self._async_cache))
            del self._async_cache[oldest_key]
        self._async_cache[text] = result
        return result

    # ------------------------------------------------------------------
    # Common
    # ------------------------------------------------------------------

    def clear_cache(self) -> None:
        """Invalidate the embedding LRU cache."""
        self._cached_embed.cache_clear()
        self._async_cache.clear()

    def health(self) -> bool:
        """Return *True* if the Ollama embedding endpoint is reachable."""
        try:
            resp = httpx.get(f"{self._base_url}/api/tags", timeout=5.0)
            return resp.status_code == 200
        except Exception:
            return False

    async def health_async(self) -> bool:
        """Return *True* if the Ollama embedding endpoint is reachable (async)."""
        try:
            client = await self._get_async_client()
            resp = await client.get(f"{self._base_url}/api/tags")
            return resp.status_code == 200
        except Exception:
            return False


# ---------------------------------------------------------------------------
# Backward-compatible module-level functions
# ---------------------------------------------------------------------------
# These delegate to the singleton in embeddings/__init__.py so that
# existing ``from embeddings.ollama import embed_texts``
# imports continue to work during the transition.
# ---------------------------------------------------------------------------

def embed_texts(texts: list[str]) -> list[list[float]]:
    """Module-level convenience — delegates to the singleton."""
    from embeddings import get_embedder
    return get_embedder().embed_texts(texts)


def get_query_embedding(text: str) -> list[float]:
    """Module-level convenience — delegates to the singleton."""
    from embeddings import get_embedder
    return get_embedder().get_query_embedding(text)


def clear_embed_cache() -> None:
    """Module-level convenience — delegates to the singleton."""
    from embeddings import get_embedder
    get_embedder().clear_cache()
