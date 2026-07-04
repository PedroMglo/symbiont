"""VectorStore protocol — backend-agnostic interface for vector storage.

Every vector store backend implements ``VectorStore``
so that the rest of the codebase never imports a concrete backend directly.

Usage::

    from store.base import VectorStore, create_store

    store: VectorStore = create_store()          # reads [store] from config
    store.upsert_batch(ids, embeddings, docs, metas)
    results = store.query(embedding, n=10)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Query result
# ---------------------------------------------------------------------------

@dataclass
class QueryResult:
    """A single result from a vector query."""
    id: str
    document: str
    metadata: dict
    score: float          # similarity score (higher = more similar)


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class VectorStore(Protocol):
    """Backend-agnostic vector store interface.

    Implementations must support two collections: ``obsidian_vault`` (notes)
    and ``code_repos`` (code).  The ``collection`` parameter selects which.
    """

    def upsert_batch(
        self,
        ids: list[str],
        embeddings: list[list[float]],
        documents: list[str],
        metadatas: list[dict],
        *,
        collection: str = "obsidian_vault",
        sparse_vectors: list[dict] | None = None,
    ) -> None:
        """Insert or update a batch of vectors."""
        ...

    def delete_ids(
        self,
        ids: list[str],
        *,
        collection: str = "obsidian_vault",
    ) -> int:
        """Delete vectors by ID.  Returns count deleted."""
        ...

    def get_existing_ids(
        self,
        *,
        collection: str = "obsidian_vault",
    ) -> set[str]:
        """Return all IDs currently stored in the collection."""
        ...

    def query(
        self,
        embedding: list[float],
        n: int = 10,
        *,
        collection: str = "obsidian_vault",
        filters: dict | None = None,
        sparse_query: dict | None = None,
    ) -> list[QueryResult]:
        """Return the *n* nearest neighbours for *embedding*.

        Args:
            filters: Optional metadata filter dict.  Regular keys use
                     equality matching (``{"field": "value"}``).  Keys
                     prefixed with ``__exclude_`` are negation filters:
                     ``{"__exclude_source_type": "repo_doc"}`` excludes
                     documents where ``source_type == "repo_doc"``.
                     Translated to Qdrant ``must`` / ``must_not``
                     ``FieldCondition`` syntax.
            sparse_query: Optional BM25 sparse vector ``{indices, values}``
                          for hybrid (dense + sparse) retrieval with RRF.
        """
        ...

    def count(self, *, collection: str = "obsidian_vault") -> int:
        """Return the number of vectors in the collection."""
        ...

    def health(self) -> bool:
        """Return *True* if the backend is reachable and operational."""
        ...


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_store(backend: str | None = None, **kwargs) -> VectorStore:
    """Instantiate the configured vector store backend.

    Args:
        backend: ``"qdrant"``.  If *None*, reads from
                 ``settings.store.backend``.
        **kwargs: forwarded to the backend constructor.  When called
                  without kwargs, reads ``qdrant_url`` / ``qdrant_api_key``
                  from ``settings.store``.
    """
    if backend is None:
        from rag_config import settings
        backend = settings.store.backend
        # Inject config values unless the caller already provided them
        if "url" not in kwargs:
            if not settings.store.qdrant_url:
                raise RuntimeError(
                    "qdrant_url is required in config/rag/internal.toml [store] section. "
                    "Start the Qdrant container with 'make qdrant' and set "
                    "qdrant_url = \"https://localhost:6333\"."
                )
            kwargs["url"] = settings.store.qdrant_url
        if "api_key" not in kwargs and settings.store.qdrant_api_key:
            kwargs["api_key"] = settings.store.qdrant_api_key

    backend = backend.lower().strip()

    if backend == "qdrant":
        from store.qdrant_store import QdrantVectorStore
        return QdrantVectorStore(**kwargs)

    raise ValueError(f"Unknown vector store backend: {backend!r}  (expected 'qdrant')")
