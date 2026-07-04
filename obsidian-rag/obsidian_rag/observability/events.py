"""Event definitions for RAG observability.

EventName enum maps to ClickHouse target tables.
RAGEvent is a frozen dataclass carrying all possible fields.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from enum import Enum


class EventName(str, Enum):
    REQUEST_COMPLETED = "request_completed"
    REQUEST_ERROR = "request_error"
    AUTH_FAILURE = "auth_failure"
    RATE_LIMITED = "rate_limited"

    RETRIEVAL_COMPLETED = "retrieval_completed"

    EMBEDDING_BATCH = "embedding_batch"

    INGEST_RUN_STARTED = "ingest_run_started"
    INGEST_RUN_COMPLETED = "ingest_run_completed"
    INGEST_STAGE = "ingest_stage"
    GOVERNOR_ACTION = "governor_action"
    BM25_REBUILD = "bm25_rebuild"
    STALE_CLEANUP = "stale_cleanup"

    CAG_PACK_STORE = "cag_pack_store"
    CAG_PACK_GET = "cag_pack_get"
    CAG_PACK_INVALIDATE = "cag_pack_invalidate"
    CAG_RESPONSE_CACHE = "cag_response_cache"
    GRAPH_CONTEXT_BUILT = "graph_context_built"

    STORE_QUERY = "store_query"
    STORE_UPSERT = "store_upsert"
    STORE_DELETE = "store_delete"

    RESOURCE_SAMPLE = "resource_sample"


EVENT_TABLE_MAP: dict[EventName, str] = {
    EventName.REQUEST_COMPLETED: "rag_requests",
    EventName.REQUEST_ERROR: "rag_requests",
    EventName.AUTH_FAILURE: "rag_requests",
    EventName.RATE_LIMITED: "rag_requests",

    EventName.RETRIEVAL_COMPLETED: "rag_retrieval",

    EventName.EMBEDDING_BATCH: "rag_embedding_batches",

    EventName.INGEST_RUN_STARTED: "rag_ingest_runs",
    EventName.INGEST_RUN_COMPLETED: "rag_ingest_runs",
    EventName.INGEST_STAGE: "rag_ingest_stages",
    EventName.GOVERNOR_ACTION: "rag_ingest_stages",
    EventName.BM25_REBUILD: "rag_ingest_stages",
    EventName.STALE_CLEANUP: "rag_ingest_stages",

    EventName.CAG_PACK_STORE: "rag_cag_operations",
    EventName.CAG_PACK_GET: "rag_cag_operations",
    EventName.CAG_PACK_INVALIDATE: "rag_cag_operations",
    EventName.CAG_RESPONSE_CACHE: "rag_cag_operations",
    EventName.GRAPH_CONTEXT_BUILT: "rag_cag_operations",

    EventName.STORE_QUERY: "rag_store_operations",
    EventName.STORE_UPSERT: "rag_store_operations",
    EventName.STORE_DELETE: "rag_store_operations",

    EventName.RESOURCE_SAMPLE: "rag_resource_samples",
}


@dataclass(frozen=True)
class RAGEvent:
    """Universal event for all RAG observability sinks."""

    event: EventName
    timestamp: float = field(default_factory=time.time)

    # Correlation
    request_id: str = ""
    symbiont_request_id: str = ""

    # Common
    latency_ms: float = 0.0
    success: bool = True
    error_type: str = ""
    error_message: str = ""

    # API
    endpoint: str = ""
    method: str = ""
    status_code: int = 0
    caller_ip: str = ""

    # Router
    route_mode: str = ""
    route_method: str = ""
    route_confidence: float = 0.0
    route_latency_ms: float = 0.0

    # Retrieval
    collection: str = ""
    query_hash: str = ""
    query_length: int = 0
    query_complexity: str = ""
    effective_top_k: int = 0
    results_count: int = 0
    results_after_filter: int = 0
    best_score: float = 0.0
    threshold_used: float = 0.0
    search_latency_ms: float = 0.0

    # Deduplication
    exact_removed: int = 0
    semantic_removed: int = 0

    # Reranker
    reranker_used: bool = False
    reranker_backend: str = ""  # "cross_encoder" or "llm"
    candidates_examined: int = 0
    candidates_retained: int = 0
    reranker_best_score: float = 0.0
    reranker_mean_score: float = 0.0
    llm_calls_made: int = 0
    reranker_model: str = ""
    reranker_latency_ms: float = 0.0

    # HyDE
    hyde_used: bool = False
    hyde_chars: int = 0
    hyde_latency_ms: float = 0.0
    hyde_skipped_reason: str = ""

    # Relevance gate
    gate_passed: bool = False
    gate_reason: str = ""

    # Budget & context
    budget_notes_tokens: int = 0
    budget_code_tokens: int = 0
    budget_graph_tokens: int = 0
    total_context_tokens: int = 0
    sources_used: str = ""

    # Embedding
    batch_size: int = 0
    batch_chars: int = 0
    model_used: str = ""
    cache_hits: int = 0
    cache_misses: int = 0
    retry_count: int = 0

    # Ingest
    run_id: str = ""
    stage_name: str = ""
    items_in: int = 0
    items_out: int = 0
    files_scanned: int = 0
    files_parsed: int = 0
    files_skipped: int = 0
    chunks_produced: int = 0
    chunks_embedded: int = 0
    chunks_stored: int = 0
    stale_deleted: int = 0
    error_count: int = 0

    # Governor
    governor_action: str = ""
    governor_reason: str = ""
    ram_percent: float = 0.0
    cpu_percent: float = 0.0
    swap_percent: float = 0.0

    # BM25
    vocab_size: int = 0
    documents_count: int = 0

    # CAG
    operation: str = ""
    pack_type: str = ""
    pack_scope: str = ""
    cache_hit: bool = False
    ttl_remaining: float = 0.0

    # Graph
    nodes_matched: int = 0
    communities_used: int = 0
    traversal_depth: int = 0
    graph_context_hit: bool = False

    # Store
    batch_count: int = 0

    # Resources
    ram_available_gb: float = 0.0
    disk_free_gb: float = 0.0
    vram_used_gb: float = 0.0
    vram_percent: float = 0.0
    psi_memory_full_avg10: float = 0.0
    psi_io_full_avg10: float = 0.0
    active_ingest: bool = False

    def to_row(self) -> dict:
        """Convert to ClickHouse row dict, excluding default/zero values."""
        from datetime import datetime, timezone

        row: dict = {
            "timestamp": datetime.fromtimestamp(self.timestamp, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            "event": self.event.value,
        }

        for fld in self.__dataclass_fields__:
            if fld in ("event", "timestamp"):
                continue
            val = getattr(self, fld)
            default = self.__dataclass_fields__[fld].default
            if val == default:
                continue
            if isinstance(val, bool):
                row[fld] = 1 if val else 0
            elif isinstance(val, EventName):
                row[fld] = val.value
            else:
                row[fld] = val

        return row


def query_hash(query: str) -> str:
    """Privacy-safe query identifier: SHA256 first 16 hex chars."""
    return hashlib.sha256(query.encode()).hexdigest()[:16]
