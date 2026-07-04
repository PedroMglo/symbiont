"""Embeddings — backend-agnostic embedding generation.

Exports a process-wide singleton via ``get_embedder()`` so that
the retrieval layer, pipeline, and API share the same instance.
"""

import threading

from obsidian_rag.embeddings.base import EmbeddingProvider, create_embedder

__all__ = [
    "EmbeddingProvider",
    "create_embedder",
    "get_embedder",
    "embed_texts",
    "get_query_embedding",
    "clear_embed_cache",
]

_lock = threading.Lock()
_embedder: EmbeddingProvider | None = None


def get_embedder(*, _override: EmbeddingProvider | None = None) -> EmbeddingProvider:
    """Return the process-wide EmbeddingProvider singleton.

    Args:
        _override: inject a provider for testing (bypasses singleton).
    """
    global _embedder
    if _override is not None:
        return _override
    if _embedder is None:
        with _lock:
            if _embedder is None:
                _embedder = create_embedder()
    return _embedder


def _reset_embedder() -> None:
    """Reset singleton — for testing only."""
    global _embedder
    with _lock:
        _embedder = None


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Backward-compatible lazy wrapper for Ollama embedding batches."""
    from obsidian_rag.embeddings.ollama import embed_texts as _embed_texts

    return _embed_texts(texts)


def get_query_embedding(text: str) -> list[float]:
    """Backward-compatible lazy wrapper for query embeddings."""
    from obsidian_rag.embeddings.ollama import get_query_embedding as _get_query_embedding

    return _get_query_embedding(text)


def clear_embed_cache() -> None:
    """Backward-compatible lazy wrapper for clearing embedding caches."""
    from obsidian_rag.embeddings.ollama import clear_embed_cache as _clear_embed_cache

    _clear_embed_cache()
