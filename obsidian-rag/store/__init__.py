"""Store — vector storage backend (Qdrant).

Exports a process-wide singleton via ``get_store()`` so that both
the retrieval layer and the pipeline share the same connection.
"""

import threading

from store.base import QueryResult, VectorStore, create_store

__all__: list[str] = ["VectorStore", "QueryResult", "create_store", "get_store"]

_lock = threading.Lock()
_store: VectorStore | None = None


def get_store(*, _override: VectorStore | None = None) -> VectorStore:
    """Return the process-wide VectorStore singleton.

    Args:
        _override: inject a store for testing (bypasses singleton).
    """
    global _store
    if _override is not None:
        return _override
    if _store is None:
        with _lock:
            if _store is None:
                _store = create_store()
    return _store


def _reset_store() -> None:
    """Reset singleton — for testing only."""
    global _store
    with _lock:
        _store = None
