"""Embeddings — backend-agnostic embedding generation.

Exports a process-wide singleton via ``get_embedder()`` so that
the retrieval layer, pipeline, and API share the same instance.
"""

import threading

from embeddings.base import EmbeddingProvider, create_embedder

__all__ = [
    "EmbeddingProvider",
    "create_embedder",
    "get_embedder",
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
