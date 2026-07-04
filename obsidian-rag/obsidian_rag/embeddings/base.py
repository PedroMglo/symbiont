"""EmbeddingProvider protocol — backend-agnostic interface for embeddings.

Every embedding backend implements ``EmbeddingProvider``
so that the rest of the codebase never imports a concrete backend directly.

Usage::

    from obsidian_rag.embeddings import get_embedder

    embedder = get_embedder()
    vectors = embedder.embed_texts(["hello", "world"])
    query_vec = embedder.get_query_embedding("search term")
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

log = logging.getLogger(__name__)


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Backend-agnostic embedding interface."""

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for a batch of texts."""
        ...

    def get_query_embedding(self, text: str) -> list[float]:
        """Get embedding for a single query text (may be cached)."""
        ...

    def clear_cache(self) -> None:
        """Invalidate any internal embedding cache."""
        ...

    def embed_texts_cached(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings, reusing a persistent on-disk cache (ingestion)."""
        ...

    def health(self) -> bool:
        """Return *True* if the embedding backend is reachable."""
        ...


def create_embedder(backend: str | None = None, **kwargs) -> EmbeddingProvider:
    """Instantiate the configured embedding backend.

    Args:
        backend: ``"ollama"``.  If *None*, defaults to ``"ollama"``.
        **kwargs: forwarded to the backend constructor.
    """
    if backend is None:
        backend = "ollama"

    backend = backend.lower().strip()

    if backend == "ollama":
        from obsidian_rag.embeddings.ollama import OllamaEmbeddingProvider
        return OllamaEmbeddingProvider(**kwargs)

    raise ValueError(f"Unknown embedding backend: {backend!r}  (expected 'ollama')")
