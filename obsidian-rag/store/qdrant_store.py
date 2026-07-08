"""Qdrant implementation of the VectorStore protocol.

Connects to a Qdrant server at ``url`` (e.g. Docker container).
Vector dimensions configured via models.json (embedding role parameters).
"""

from __future__ import annotations

import asyncio
import logging
import time

from store.base import QueryResult

log = logging.getLogger(__name__)


def _get_vector_dim() -> int:
    """Load embedding dimensions from registry."""
    from registry import _load
    data = _load()
    return data.get("rag", {}).get("roles", {}).get("embedding", {}).get("parameters", {}).get("dimensions", 1024)


_VECTOR_DIM = _get_vector_dim()
_MAX_RETRIES = 3
_RETRY_BACKOFF = 0.5  # seconds — doubles each retry


def _import_qdrant():
    """Lazy import — qdrant-client is an optional dependency."""
    try:
        from qdrant_client import QdrantClient, models
        return QdrantClient, models
    except ImportError:
        raise ImportError(
            "qdrant-client is required for the Qdrant backend.  "
            "Install it with:  pip install 'qdrant-client>=1.18,<1.19'"
        ) from None


def _import_async_qdrant():
    """Lazy import of AsyncQdrantClient."""
    try:
        from qdrant_client import AsyncQdrantClient, models
        return AsyncQdrantClient, models
    except ImportError:
        raise ImportError(
            "qdrant-client is required for the Qdrant backend.  "
            "Install it with:  pip install 'qdrant-client>=1.18,<1.19'"
        ) from None


def _retry(fn, *, max_retries: int = _MAX_RETRIES, backoff: float = _RETRY_BACKOFF):
    """Execute *fn* with exponential-backoff retry on transient errors."""
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return fn()
        except Exception as exc:
            exc_name = type(exc).__name__
            is_transient = any(kw in exc_name.lower() for kw in (
                "connection", "timeout", "unavailable", "transport",
            )) or any(kw in str(exc).lower() for kw in (
                "connection", "timed out", "unavailable", "refused",
            ))
            if not is_transient or attempt == max_retries:
                raise
            last_exc = exc
            wait = backoff * (2 ** attempt)
            log.warning("Qdrant: %s (attempt %d/%d) — retry in %.1fs",
                        exc, attempt + 1, max_retries, wait)
            time.sleep(wait)
    raise last_exc  # type: ignore[misc]


async def _retry_async(fn, *, max_retries: int = _MAX_RETRIES, backoff: float = _RETRY_BACKOFF):
    """Execute async *fn* with exponential-backoff retry on transient errors."""
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return await fn()
        except Exception as exc:
            exc_name = type(exc).__name__
            is_transient = any(kw in exc_name.lower() for kw in (
                "connection", "timeout", "unavailable", "transport",
            )) or any(kw in str(exc).lower() for kw in (
                "connection", "timed out", "unavailable", "refused",
            ))
            if not is_transient or attempt == max_retries:
                raise
            last_exc = exc
            wait = backoff * (2 ** attempt)
            log.warning("Qdrant async: %s (attempt %d/%d) — retry in %.1fs",
                        exc, attempt + 1, max_retries, wait)
            await asyncio.sleep(wait)
    raise last_exc  # type: ignore[misc]


class QdrantVectorStore:
    """VectorStore backed by a Qdrant server.

    Args:
        url: Qdrant server URL (e.g. ``https://localhost:6333``). Required.
        api_key: Optional API key for Qdrant Cloud.
    """

    def __init__(
        self,
        *,
        url: str,
        api_key: str | None = None,
    ) -> None:
        if not url:
            raise ValueError(
                "QdrantVectorStore requires a server URL. "
                "Start the Qdrant container with 'make qdrant' and set "
                "qdrant_url in config/rag/internal.toml."
            )
        QdrantClient, self._models = _import_qdrant()
        self._client = QdrantClient(url=url, api_key=api_key)
        self._url = url
        self._api_key = api_key
        log.info("Qdrant: server mode → %s", url)

        self._ensured: set[str] = set()

    # -- internal helpers --

    def _ensure_collection(self, name: str) -> None:
        """Create collection if it doesn't exist yet (dense + sparse).

        On creation we apply the configured ``on_disk`` flag and HNSW tuning.
        When ``defer_hnsw_on_bulk`` is set, the collection is created with
        ``m=0`` so HNSW construction is deferred until :meth:`finalize_collection_index`
        is called after a bulk load — this keeps ingestion throughput high.
        """
        if name in self._ensured:
            return

        models = self._models
        store_cfg = self._store_cfg()
        collections = [c.name for c in self._client.get_collections().collections]
        if name not in collections:
            initial_m = 0 if store_cfg.defer_hnsw_on_bulk else store_cfg.hnsw_m
            self._client.create_collection(
                collection_name=name,
                vectors_config=models.VectorParams(
                    size=_VECTOR_DIM,
                    distance=models.Distance.COSINE,
                    on_disk=store_cfg.on_disk,
                ),
                hnsw_config=models.HnswConfigDiff(
                    m=initial_m,
                    ef_construct=store_cfg.hnsw_ef_construct,
                ),
                sparse_vectors_config={
                    "bm25": models.SparseVectorParams(),
                },
            )
            log.info(
                "Qdrant: created collection %r (%dd cosine + bm25, on_disk=%s, hnsw_m=%d)",
                name, _VECTOR_DIM, store_cfg.on_disk, initial_m,
            )
        else:
            # Existing collection — ensure sparse index exists
            try:
                info = self._client.get_collection(name)
                has_sparse = (
                    info.config
                    and info.config.params
                    and getattr(info.config.params, "sparse_vectors", None)
                    and "bm25" in info.config.params.sparse_vectors
                )
                if not has_sparse:
                    self._client.update_collection(
                        collection_name=name,
                        sparse_vectors_config={
                            "bm25": models.SparseVectorParams(),
                        },
                    )
                    log.info("Qdrant: added bm25 sparse index to %r", name)
            except Exception as exc:
                log.debug("Qdrant: could not check/add sparse index to %r: %s", name, exc)

        # Ensure payload indexes on the fields actually used for filtering.
        # Creating these up-front lets the filterable HNSW make use of them.
        index_fields = {
            "source_type": models.PayloadSchemaType.KEYWORD,
            "source_id": models.PayloadSchemaType.KEYWORD,
            "source_name": models.PayloadSchemaType.KEYWORD,
            "source_path": models.PayloadSchemaType.KEYWORD,
            "repo_name": models.PayloadSchemaType.KEYWORD,
            "note_title": models.PayloadSchemaType.KEYWORD,
            "content_hash": models.PayloadSchemaType.KEYWORD,
            "_id": models.PayloadSchemaType.KEYWORD,
        }
        for field_name, schema in index_fields.items():
            try:
                self._client.create_payload_index(
                    collection_name=name,
                    field_name=field_name,
                    field_schema=schema,
                )
            except Exception:
                # Index may already exist — ignore
                pass

        self._ensured.add(name)

    @staticmethod
    def _store_cfg():
        """Return the configured vector-store settings."""
        from rag_config import settings

        return settings.store

    def finalize_collection_index(self, collection: str) -> None:
        """Restore HNSW graph degree after a deferred bulk load.

        Call once ingestion of a large batch is complete. Safe to call even when
        ``defer_hnsw_on_bulk`` is disabled (it simply re-asserts the configured
        ``m``/``ef_construct``).
        """
        models = self._models
        store_cfg = self._store_cfg()
        try:
            _retry(lambda: self._client.update_collection(
                collection_name=collection,
                hnsw_config=models.HnswConfigDiff(
                    m=store_cfg.hnsw_m,
                    ef_construct=store_cfg.hnsw_ef_construct,
                ),
            ))
            log.info(
                "Qdrant: finalized HNSW for %r (m=%d, ef_construct=%d)",
                collection, store_cfg.hnsw_m, store_cfg.hnsw_ef_construct,
            )
        except Exception as exc:
            log.warning("Qdrant: could not finalize HNSW for %r: %s", collection, exc)


    # -- VectorStore protocol --

    def reset_collection(self, *, collection: str = "obsidian_vault") -> int:
        collections = [c.name for c in self._client.get_collections().collections]
        if collection not in collections:
            self._ensured.discard(collection)
            self._ensure_collection(collection)
            return 0

        previous_count = self.count(collection=collection)
        _retry(lambda: self._client.delete_collection(collection_name=collection))
        self._ensured.discard(collection)
        self._ensure_collection(collection)
        log.info("Qdrant: reset collection %r (%d vectors removed)", collection, previous_count)
        return previous_count

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
        import time as _time
        _t0 = _time.time()
        models = self._models
        self._ensure_collection(collection)
        externalize = self._store_cfg().externalize_text

        points = []
        for i, (rid, emb, doc, meta) in enumerate(zip(ids, embeddings, documents, metadatas)):
            vector: dict | list = emb
            if sparse_vectors and i < len(sparse_vectors):
                sv = sparse_vectors[i]
                if sv.get("indices"):
                    vector = {
                        "": emb,  # default (dense) vector
                        "bm25": models.SparseVector(
                            indices=sv["indices"],
                            values=sv["values"],
                        ),
                    }
            payload: dict = {"_id": rid, **meta}
            # Always expose a content hash for cheap change-detection / dedup filters.
            payload.setdefault("content_hash", _content_hash(doc))
            if externalize:
                # Lightweight payload — full text lives in the staging layer,
                # referenced for lookup at query time.
                payload["text_ref"] = meta.get("text_ref", "")
            else:
                payload["_document"] = doc
            points.append(
                models.PointStruct(
                    id=_str_to_uint(rid),
                    vector=vector,
                    payload=payload,
                )
            )

        # Qdrant recommends batches ≤ 100
        for i in range(0, len(points), 100):
            batch = points[i : i + 100]
            _retry(lambda b=batch: self._client.upsert(
                collection_name=collection,
                points=b,
            ))

        from observability import emit, is_enabled
        if is_enabled():
            from observability import EventName, RAGEvent
            emit(RAGEvent(
                event=EventName.STORE_UPSERT,
                collection=collection,
                latency_ms=(_time.time() - _t0) * 1000,
                batch_count=len(ids),
                success=True,
            ))

    def delete_ids(
        self,
        ids: list[str],
        *,
        collection: str = "obsidian_vault",
    ) -> int:
        if not ids:
            return 0
        models = self._models
        self._ensure_collection(collection)

        # Delete by matching the stored _id field
        selector = models.FilterSelector(
            filter=models.Filter(
                should=[
                    models.FieldCondition(
                        key="_id",
                        match=models.MatchValue(value=rid),
                    )
                    for rid in ids
                ],
            ),
        )
        _retry(lambda: self._client.delete(
            collection_name=collection,
            points_selector=selector,
        ))
        return len(ids)

    def get_existing_ids(
        self,
        *,
        collection: str = "obsidian_vault",
    ) -> set[str]:
        self._ensure_collection(collection)
        result: set[str] = set()
        offset = None

        while True:
            scroll_kwargs: dict = {
                "collection_name": collection,
                "limit": 1000,
                "with_payload": ["_id"],
                "with_vectors": False,
            }
            if offset is not None:
                scroll_kwargs["offset"] = offset

            points, next_offset = _retry(lambda kw=dict(scroll_kwargs): self._client.scroll(**kw))
            for p in points:
                _id = p.payload.get("_id") if p.payload else None
                if _id:
                    result.add(_id)

            if next_offset is None:
                break
            offset = next_offset

        return result

    def query(
        self,
        embedding: list[float],
        n: int = 10,
        *,
        collection: str = "obsidian_vault",
        filters: dict | None = None,
        sparse_query: dict | None = None,
    ) -> list[QueryResult]:
        import time as _time
        _t0 = _time.time()
        self._ensure_collection(collection)

        models = self._models
        query_filter = None
        if filters:
            must_conditions = []
            must_not_conditions = []
            for k, v in filters.items():
                if k.startswith("__exclude_"):
                    field = k[len("__exclude_"):]
                    must_not_conditions.append(
                        models.FieldCondition(key=field, match=models.MatchValue(value=v))
                    )
                else:
                    must_conditions.append(
                        models.FieldCondition(key=k, match=models.MatchValue(value=v))
                    )
            query_filter = models.Filter(
                must=must_conditions or None,
                must_not=must_not_conditions or None,
            )

        # Hybrid search: dense prefetch + sparse, fused with RRF
        if sparse_query and sparse_query.get("indices"):
            response = _retry(lambda: self._client.query_points(
                collection_name=collection,
                prefetch=[
                    models.Prefetch(
                        query=embedding,
                        using="",  # default dense vector
                        limit=n * 2,
                        filter=query_filter,
                    ),
                    models.Prefetch(
                        query=models.SparseVector(
                            indices=sparse_query["indices"],
                            values=sparse_query["values"],
                        ),
                        using="bm25",
                        limit=n * 2,
                        filter=query_filter,
                    ),
                ],
                query=models.FusionQuery(fusion=models.Fusion.RRF),
                limit=n,
                with_payload=True,
            ))
        else:
            response = _retry(lambda: self._client.query_points(
                collection_name=collection,
                query=embedding,
                limit=n,
                with_payload=True,
                query_filter=query_filter,
            ))
        results = []
        for hit in response.points:
            payload = dict(hit.payload) if hit.payload else {}
            rid = payload.pop("_id", str(hit.id))
            doc = payload.pop("_document", "")
            results.append(QueryResult(
                id=rid,
                document=doc,
                metadata=payload,
                score=hit.score,
            ))

        from observability import emit, is_enabled
        if is_enabled():
            from observability import EventName, RAGEvent
            emit(RAGEvent(
                event=EventName.STORE_QUERY,
                collection=collection,
                latency_ms=(_time.time() - _t0) * 1000,
                results_count=len(results),
                success=True,
            ))

        return results

    def count(self, *, collection: str = "obsidian_vault") -> int:
        self._ensure_collection(collection)
        info = _retry(lambda: self._client.get_collection(collection))
        return info.points_count or 0

    def health(self) -> bool:
        """Return *True* if Qdrant backend is reachable."""
        try:
            self._client.get_collections()
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Async API (for query-time retrieval — non-blocking)
    # ------------------------------------------------------------------

    async def _get_async_client(self):
        """Return an AsyncQdrantClient (lazy initialized)."""
        if not hasattr(self, "_async_client") or self._async_client is None:
            AsyncQdrantClient, _ = _import_async_qdrant()
            self._async_client = AsyncQdrantClient(
                url=self._url,
                api_key=self._api_key,
            )
        return self._async_client

    async def aclose(self) -> None:
        """Close the async client."""
        if hasattr(self, "_async_client") and self._async_client is not None:
            await self._async_client.close()
            self._async_client = None

    async def query_async(
        self,
        embedding: list[float],
        n: int = 10,
        *,
        collection: str = "obsidian_vault",
        filters: dict | None = None,
        sparse_query: dict | None = None,
    ) -> list[QueryResult]:
        """Async version of query — non-blocking vector search."""
        import time as _time
        _t0 = _time.time()
        self._ensure_collection(collection)

        models = self._models
        client = await self._get_async_client()

        query_filter = None
        if filters:
            must_conditions = []
            must_not_conditions = []
            for k, v in filters.items():
                if k.startswith("__exclude_"):
                    field = k[len("__exclude_"):]
                    must_not_conditions.append(
                        models.FieldCondition(key=field, match=models.MatchValue(value=v))
                    )
                else:
                    must_conditions.append(
                        models.FieldCondition(key=k, match=models.MatchValue(value=v))
                    )
            query_filter = models.Filter(
                must=must_conditions or None,
                must_not=must_not_conditions or None,
            )

        # Hybrid search: dense prefetch + sparse, fused with RRF
        if sparse_query and sparse_query.get("indices"):
            response = await _retry_async(lambda: client.query_points(
                collection_name=collection,
                prefetch=[
                    models.Prefetch(
                        query=embedding,
                        using="",
                        limit=n * 2,
                        filter=query_filter,
                    ),
                    models.Prefetch(
                        query=models.SparseVector(
                            indices=sparse_query["indices"],
                            values=sparse_query["values"],
                        ),
                        using="bm25",
                        limit=n * 2,
                        filter=query_filter,
                    ),
                ],
                query=models.FusionQuery(fusion=models.Fusion.RRF),
                limit=n,
                with_payload=True,
            ))
        else:
            response = await _retry_async(lambda: client.query_points(
                collection_name=collection,
                query=embedding,
                limit=n,
                with_payload=True,
                query_filter=query_filter,
            ))

        results = []
        for hit in response.points:
            payload = dict(hit.payload) if hit.payload else {}
            rid = payload.pop("_id", str(hit.id))
            doc = payload.pop("_document", "")
            results.append(QueryResult(
                id=rid,
                document=doc,
                metadata=payload,
                score=hit.score,
            ))

        from observability import emit, is_enabled
        if is_enabled():
            from observability import EventName, RAGEvent
            emit(RAGEvent(
                event=EventName.STORE_QUERY,
                collection=collection,
                latency_ms=(_time.time() - _t0) * 1000,
                results_count=len(results),
                success=True,
            ))

        return results

    async def count_async(self, *, collection: str = "obsidian_vault") -> int:
        """Async version of count."""
        self._ensure_collection(collection)
        client = await self._get_async_client()
        info = await _retry_async(lambda: client.get_collection(collection))
        return info.points_count or 0

    async def health_async(self) -> bool:
        """Async health check."""
        try:
            client = await self._get_async_client()
            await client.get_collections()
            return True
        except Exception:
            return False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _str_to_uint(s: str) -> int:
    """Deterministic string → unsigned 64-bit int for Qdrant point IDs.

    Qdrant requires numeric or UUID point IDs.  We hash the string ID
    to a stable uint64.
    """
    import hashlib
    h = hashlib.sha256(s.encode("utf-8")).digest()
    return int.from_bytes(h[:8], "big") & 0x7FFFFFFFFFFFFFFF


def _content_hash(text: str) -> str:
    """Stable content hash of a chunk's text (payload field for dedup/filtering)."""
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
