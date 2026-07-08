"""FastAPI application — endpoints + lifespan."""

import json as _json
import logging
import os
import secrets
import socket
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx as _httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

try:
    from slowapi import Limiter
    from slowapi.errors import RateLimitExceeded
    from slowapi.util import get_remote_address
except ModuleNotFoundError:  # pragma: no cover - exercised in lightweight root test envs
    class RateLimitExceeded(Exception):
        detail = "slowapi unavailable"

    def get_remote_address(request: Request) -> str:
        return request.client.host if request.client else "unknown"

    class Limiter:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def limit(self, *_args: Any, **_kwargs: Any):
            def _decorator(func):
                return func

            return _decorator

from api.schemas import (
    AdminJobResponse,
    AdminJobsResponse,
    AdminJobStatusResponse,
    AdminReprocessRequest,
    BatchQueryRequest,
    BatchQueryResponse,
    BatchQueryResultItem,
    CagExplainItem,
    CagExplainRequest,
    CagExplainResponse,
    CagPackDetail,
    CagPackItem,
    CagPacksResponse,
    ChatMessage,
    ChatRequest,
    ChunkResult,
    CitationRef,
    CodeQueryRequest,
    EvidenceFreshness,
    EvidenceProvenance,
    EvidenceTruncation,
    GraphContextItem,
    GraphContextRequest,
    GraphContextResponse,
    GraphNeighborsResponse,
    GraphQueryRequest,
    IndexingStatusResponse,
    QueryRequest,
    QueryResponse,
    RagEvidence,
    RepoInfo,
    ReposResponse,
    RetrievalBudget,
    RetrievalStatusResponse,
    RetrievalTrace,
    StatsResponse,
)
from embeddings import get_embedder
from prompts.templates import SYSTEM_GENERAL
from rag_config import settings
from retrieval.observe import QueryTrace, setup_logging
from retrieval.rag import (
    _get_store,
    build_rag_context_async,
    should_use_rag,
)
from workflows.job_store import default_admin_job_store

try:
    from importlib.metadata import PackageNotFoundError, version

    try:
        __version__ = version("obsidian-rag")
    except PackageNotFoundError:
        __version__ = "0+local"
except Exception:
    __version__ = "0+local"

_SQL_DIR = Path(__file__).resolve().parent / "sql"
_SQL_CACHE = {}


def _sql(name: str) -> str:
    text = _SQL_CACHE.get(name)
    if text is None:
        text = (_SQL_DIR / name).read_text(encoding="utf-8").strip()
        _SQL_CACHE[name] = text
    return text


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None


def _optional_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _source_namespace(metadata: dict[str, Any]) -> str:
    return str(
        metadata.get("source_name")
        or metadata.get("repo_name")
        or metadata.get("source_id")
        or "unknown"
    )


def _freshness_from_metadata(metadata: dict[str, Any]) -> EvidenceFreshness:
    stale_reason = _optional_str(metadata.get("stale_reason"))
    fresh_value = metadata.get("fresh")
    if isinstance(fresh_value, bool):
        status = "fresh" if fresh_value else "stale"
    elif stale_reason:
        status = "stale"
    else:
        status = "unknown"

    return EvidenceFreshness(
        status=status,
        indexed_at=_optional_str(metadata.get("last_indexed_at") or metadata.get("indexed_at")),
        source_mtime=_optional_float(metadata.get("mtime") or metadata.get("source_mtime")),
        source_hash=_optional_str(
            metadata.get("content_hash")
            or metadata.get("sha256")
            or metadata.get("source_hash")
        ),
        stale_reason=stale_reason,
    )


def _truncation_from_metadata(metadata: dict[str, Any]) -> EvidenceTruncation:
    truncated = bool(
        metadata.get("truncated_after_max_chunks")
        or metadata.get("truncated")
    )
    return EvidenceTruncation(
        truncated=truncated,
        reason="max_chunks_per_file" if metadata.get("truncated_after_max_chunks") else None,
        original_chunk_count=_optional_int(metadata.get("original_chunk_count")),
    )


def _query_filters_for_trace(req: QueryRequest, filters: dict[str, Any] | None) -> dict[str, Any]:
    merged = dict(filters) if filters else {}
    if req.source_type:
        merged["source_type"] = req.source_type
    if req.exclude_source_type:
        merged["__exclude_source_type"] = req.exclude_source_type
    return merged


def _result_to_chunk(r) -> ChunkResult:
    display = r.metadata.get("display_text", r.document)
    return ChunkResult(
        text=display,
        score=round(r.score, 4),
        source_path=r.metadata.get("source_path", ""),
        note_title=r.metadata.get("note_title", ""),
        section_header=r.metadata.get("section_header", ""),
        source_type=r.metadata.get("source_type", "markdown"),
        repo_name=r.metadata.get("repo_name"),
        symbol_type=r.metadata.get("symbol_type"),
    )


def _result_to_evidence(
    r,
    *,
    collection_name: str,
    filters: dict[str, Any],
    sparse_used: bool,
) -> RagEvidence:
    metadata = r.metadata
    return RagEvidence(
        citation=CitationRef(
            source_path=metadata.get("source_path", ""),
            source_namespace=_source_namespace(metadata),
            source_type=metadata.get("source_type", "markdown"),
            chunk_id=_optional_str(getattr(r, "id", None)),
            chunk_index=_optional_int(metadata.get("chunk_index")),
            note_title=metadata.get("note_title", ""),
            section_header=metadata.get("section_header", ""),
            repo_name=metadata.get("repo_name"),
            symbol_type=metadata.get("symbol_type"),
        ),
        score=round(r.score, 4),
        provenance=EvidenceProvenance(
            collection=collection_name,
            retrieval_backend="hybrid_vector_sparse" if sparse_used else "vector",
            source_id=_optional_str(metadata.get("source_id")),
            source_name=_optional_str(metadata.get("source_name")),
            filters=filters,
        ),
        freshness=_freshness_from_metadata(metadata),
        truncation=_truncation_from_metadata(metadata),
    )


def _miss_reasons(raw_count: int, kept_count: int, filters: dict[str, Any]) -> list[str]:
    if kept_count:
        return []
    if raw_count == 0:
        return ["no_candidates_after_filters"] if filters else ["no_candidates"]
    return ["below_min_score"]


# === Globals ===
_http_pool: _httpx.AsyncClient | None = None
_admin_jobs: dict[str, dict[str, Any]] = {}
_admin_jobs_lock = threading.Lock()
_admin_job_store = default_admin_job_store(settings)
_admin_job_cancel_events: dict[str, threading.Event] = {}
_admin_active_statuses = {"queued", "running", "submitted", "paused_resource_pressure", "retry_scheduled"}
_admin_blocking_statuses = _admin_active_statuses | {"cancel_requested"}
_admin_terminal_statuses = {
    "completed",
    "failed",
    "canceled",
    "cancelled",
    "failed_resource_pressure",
    "interrupted",
}
_auto_sync_stop: threading.Event | None = None
_auto_sync_thread: threading.Thread | None = None
log = logging.getLogger(__name__)


# === Authentication ===

async def _verify_api_key(request: Request) -> None:
    """Validate Bearer token against configured api_key.

    If api_key is empty in config, authentication is disabled (open access).
    """
    key = settings.api.api_key
    if not key:
        return
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    token = auth.removeprefix("Bearer ").strip()
    if not secrets.compare_digest(token, key):
        raise HTTPException(status_code=401, detail="Invalid API key")


def _check_api_key(request: Request) -> JSONResponse | None:
    """Check Bearer token; returns a 401 JSONResponse on failure, None on success."""
    key = settings.api.api_key
    if not key:
        return None
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return JSONResponse(status_code=401, content={"detail": "Missing or invalid Authorization header"})
    token = auth.removeprefix("Bearer ").strip()
    if not secrets.compare_digest(token, key):
        return JSONResponse(status_code=401, content={"detail": "Invalid API key"})
    return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Preload Qdrant collections + create connection pool."""
    global _auto_sync_stop, _auto_sync_thread, _http_pool
    setup_logging()
    _recover_interrupted_admin_jobs()
    _get_store()   # warm up the VectorStore singleton
    _http_pool = _httpx.AsyncClient(
        base_url=settings.ollama.base_url,
        timeout=_httpx.Timeout(connect=5.0, read=300.0, write=30.0, pool=10.0),
        limits=_httpx.Limits(max_connections=10, max_keepalive_connections=5),
    )

    # Observability dispatcher
    import os
    if settings.observability.enabled:
        from observability import start as obs_start
        obs_start(
            clickhouse_url=settings.observability.clickhouse_url,
            database=settings.observability.clickhouse_database,
            username=settings.observability.clickhouse_username,
            password=os.environ.get(settings.observability.clickhouse_password_env, ""),
            batch_size=settings.observability.batch_size,
            flush_interval=settings.observability.flush_interval_seconds,
            queue_max_size=settings.observability.queue_max_size,
            resource_sampling=settings.observability.resource_sampling,
            resource_sample_interval=settings.observability.resource_sample_interval,
        )

    if settings.sync.auto_reprocess:
        _auto_sync_stop = threading.Event()
        _auto_sync_thread = threading.Thread(
            target=_auto_reprocess_loop,
            args=(_auto_sync_stop,),
            name="rag-auto-reprocess",
            daemon=True,
        )
        _auto_sync_thread.start()

    yield

    if _auto_sync_stop is not None:
        _auto_sync_stop.set()
    if _auto_sync_thread is not None:
        _auto_sync_thread.join(timeout=5)
    _auto_sync_stop = None
    _auto_sync_thread = None

    if settings.observability.enabled:
        from observability import stop as obs_stop
        obs_stop()

    # Close async clients
    embedder = get_embedder()
    if hasattr(embedder, "aclose"):
        await embedder.aclose()
    store = _get_store()
    if hasattr(store, "aclose"):
        await store.aclose()

    await _http_pool.aclose()
    _http_pool = None


app = FastAPI(
    title="Obsidian RAG API",
    description="API local para queries semânticas ao Vault Obsidian e repositórios de código",
    version=__version__,
    lifespan=lifespan,
)


def _admin_reprocess_running() -> bool:
    _hydrate_admin_jobs()
    with _admin_jobs_lock:
        return any(job.get("status") in _admin_blocking_statuses for job in _admin_jobs.values())


def _auto_reprocess_loop(stop_event: threading.Event) -> None:
    """Background incremental reprocess loop for configured local roots."""
    startup_delay = max(0, int(settings.sync.startup_delay_seconds))
    interval = max(15, int(settings.sync.watch_interval_seconds))
    if stop_event.wait(startup_delay):
        return

    while not stop_event.is_set():
        if _admin_reprocess_running():
            stop_event.wait(interval)
            continue

        try:
            from pipeline.sync import has_configured_sources

            if not has_configured_sources():
                log.debug("Auto reprocess skipped: no configured RAG sources")
                stop_event.wait(interval)
                continue
        except Exception as exc:
            log.debug("Auto reprocess source preflight failed: %s", exc)

        try:
            from tuning import should_throttle

            advice = should_throttle(settings.performance, str(settings.paths.data_dir))
            if advice.low_disk or advice.pause_sync or advice.reduce_workers:
                log.info(
                    "Auto reprocess adiado por recursos%s%s",
                    ": " if advice.reason else "",
                    advice.reason,
                )
                stop_event.wait(interval)
                continue
        except Exception as exc:
            log.debug("Auto reprocess resource preflight failed: %s", exc)

        job_id = f"auto-{uuid.uuid4().hex}"
        origin = {
            "kind": "scheduler",
            "name": "rag-auto-reprocess",
            "service": "obsidian-rag",
            "machine": socket.gethostname(),
            "metadata": {"trigger": "startup-watch"},
        }
        _set_admin_job(
            job_id,
            job_id=job_id,
            parent_job_id=None,
            status="running",
            target="all",
            force=False,
            started_at=time.time(),
            finished_at=None,
            error=None,
            origin=origin,
            result={"auto": True, "origin": origin, "children": []},
        )
        try:
            _execute_or_submit_admin_reprocess(
                job_id,
                target="all",
                force=False,
                vault=None,
                sources=None,
                origin=origin,
                extra_result={"auto": True},
            )
        except Exception as exc:
            _set_admin_job(job_id, status="failed", finished_at=time.time(), error=str(exc)[:1000])

        stop_event.wait(interval)

# === Dashboard router ===
from observability.dashboard import router as dashboard_router  # noqa: E402

app.include_router(dashboard_router)

# === Rate limiting ===
_rate_limit = settings.api.rate_limit
_chat_rate_limit = settings.api.chat_rate_limit

limiter = Limiter(
    key_func=get_remote_address,
    default_limits=[f"{_rate_limit}/minute"] if _rate_limit > 0 else [],
    enabled=_rate_limit > 0,
)
app.state.limiter = limiter


@app.exception_handler(RateLimitExceeded)
async def _rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return JSONResponse(
        status_code=429,
        content={"detail": f"Rate limit exceeded: {exc.detail}"},
    )


# Paths exempt from API key authentication
_AUTH_EXEMPT_PATHS = frozenset({"/health", "/docs", "/openapi.json", "/redoc"})
_AUTH_EXEMPT_PREFIXES: tuple[str, ...] = ()


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """Enforce API key on all endpoints except health and docs."""
    path = request.url.path
    if path not in _AUTH_EXEMPT_PATHS and not path.startswith(_AUTH_EXEMPT_PREFIXES):
        error_response = _check_api_key(request)
        if error_response is not None:
            return error_response
    return await call_next(request)


@app.middleware("http")
async def observability_middleware(request: Request, call_next):
    """Emit request lifecycle events to observability."""
    start_time = time.time()
    request.state.request_id = request.headers.get("X-Request-ID", "")
    request.state.symbiont_request_id = request.headers.get("X-Request-ID", "")

    response = await call_next(request)

    from observability import emit, is_enabled
    if is_enabled() and request.url.path not in _AUTH_EXEMPT_PATHS:
        from observability import EventName, RAGEvent
        latency = (time.time() - start_time) * 1000
        success = 200 <= response.status_code < 400
        event_name = EventName.REQUEST_COMPLETED if success else EventName.REQUEST_ERROR
        emit(RAGEvent(
            event=event_name,
            request_id=request.state.request_id,
            symbiont_request_id=request.state.symbiont_request_id,
            endpoint=request.url.path,
            method=request.method,
            status_code=response.status_code,
            latency_ms=latency,
            success=success,
            caller_ip=request.client.host if request.client else "",
        ))

    return response


# === Endpoints ===


def _component_status(status: str, **details: Any) -> dict[str, Any]:
    return {"status": status, **details}


def _rag_health_status(components: dict[str, dict[str, Any]]) -> str:
    required = ("qdrant",)
    if any(components.get(name, {}).get("status") != "ok" for name in required):
        return "degraded"
    if any(component.get("status") in {"degraded", "unavailable"} for component in components.values()):
        return "degraded"
    return "ok"


def _health_qdrant() -> dict[str, Any]:
    try:
        store = _get_store()
        reachable = store.health()
        if not reachable:
            return _component_status("unavailable", reachable=False)
        return _component_status(
            "ok",
            reachable=True,
            collection="obsidian_vault",
            chunks=store.count(collection="obsidian_vault"),
        )
    except Exception as exc:
        return _component_status("unavailable", reachable=False, error=str(exc)[:200])


def _has_repo_sources() -> bool:
    if settings.repos.paths:
        return True
    try:
        from pipeline.adhoc_sources import registered_source_paths

        return bool(registered_source_paths(source_types={"code", "document"}))
    except Exception:
        return False


def _health_code_index() -> dict[str, Any]:
    if not _has_repo_sources():
        return _component_status("disabled", collection=settings.repos.collection_name, chunks=0)
    try:
        store = _get_store()
        chunks = store.count(collection=settings.repos.collection_name)
        return _component_status("ok" if chunks > 0 else "empty", collection=settings.repos.collection_name, chunks=chunks)
    except Exception as exc:
        return _component_status(
            "unavailable",
            collection=settings.repos.collection_name,
            chunks=0,
            error=str(exc)[:200],
        )


def _health_graph() -> dict[str, Any]:
    if not settings.graphify.enabled:
        return _component_status("disabled", repos=0, built_repos=0)
    try:
        from pipeline.graph.backend import get_graph_backend
        from pipeline.graph.query import list_repos

        backend = get_graph_backend()
        backend_health = backend.health()
        repos = list_repos()
        built = sum(1 for repo in repos if repo.get("graph_built"))
        status = "unavailable" if backend_health.get("ok") is False else ("ok" if built > 0 else "empty")
        return _component_status(
            status,
            repos=len(repos),
            built_repos=built,
            graphify_backend=settings.graphify.backend,
            query_backend=backend_health.get("backend", getattr(backend, "name", "unknown")),
            query_backend_ok=backend_health.get("ok", True),
        )
    except Exception as exc:
        return _component_status("unavailable", repos=0, built_repos=0, error=str(exc)[:200])


def _health_cag() -> dict[str, Any]:
    if not settings.cag.enabled:
        return _component_status("disabled", total_packs=0, fresh_packs=0)
    try:
        from cag import get_pack_store

        store = get_pack_store()
        total = store.count_packs()
        fresh = store.count_fresh()
        return _component_status("ok" if fresh > 0 else "empty", total_packs=total, fresh_packs=fresh)
    except Exception as exc:
        return _component_status("unavailable", total_packs=0, fresh_packs=0, error=str(exc)[:200])


@app.get("/health")
def health():
    components = {
        "qdrant": _health_qdrant(),
        "graph": _health_graph(),
        "cag": _health_cag(),
        "code_index": _health_code_index(),
    }
    return {
        "status": _rag_health_status(components),
        "service": "obsidian-rag",
        "version": __version__,
        "components": components,
    }


@app.get("/stats", response_model=StatsResponse)
def stats():
    store = _get_store()
    code_chunks = 0
    code_name = ""
    if _has_repo_sources():
        try:
            code_chunks = store.count(collection=settings.repos.collection_name)
            code_name = settings.repos.collection_name
        except Exception:
            pass
    return StatsResponse(
        total_chunks=store.count(collection="obsidian_vault"),
        collection_name="obsidian_vault",
        data_path=str(settings.paths.data_dir),
        code_chunks=code_chunks,
        code_collection_name=code_name,
    )


def _query_store(
    store,
    collection_name: str,
    req: QueryRequest,
    *,
    filters: dict | None = None,
    trace=None,
) -> tuple[list[ChunkResult], list[RagEvidence], RetrievalTrace]:
    """Executa query vectorial híbrida e devolve chunks com evidence contract."""
    # Merge source_type filters from request into filters dict
    merged = _query_filters_for_trace(req, filters)

    query_embedding = get_embedder().get_query_embedding(req.query)

    # Hybrid retrieval: add BM25 sparse query when a model exists for this collection
    from retrieval.rag import _get_sparse_query

    sparse_query = _get_sparse_query(req.query, collection_name)
    if trace is not None:
        trace.collection = collection_name
        trace.dense_used = True
        trace.sparse_available = sparse_query is not None
        trace.sparse_used = sparse_query is not None

    results = store.query(
        query_embedding,
        n=req.top_k,
        collection=collection_name,
        filters=merged or None,
        sparse_query=sparse_query,
    )
    chunks = []
    evidence = []
    for r in results:
        if r.score >= req.min_score:
            chunks.append(_result_to_chunk(r))
            evidence.append(_result_to_evidence(
                r,
                collection_name=collection_name,
                filters=merged,
                sparse_used=sparse_query is not None,
            ))
    if trace is not None:
        trace.results_count = len(results)
        trace.results_after_filter = len(chunks)
        if chunks:
            trace.best_score = chunks[0].score
        trace.threshold_used = req.min_score

    retrieval_trace = RetrievalTrace(
        collection=collection_name,
        top_k=req.top_k,
        min_score=req.min_score,
        results_count=len(results),
        results_after_filter=len(chunks),
        miss_reasons=_miss_reasons(len(results), len(chunks), merged),
        source_namespaces=sorted({item.citation.source_namespace for item in evidence}),
        budget=RetrievalBudget(candidate_top_k=req.top_k),
        truncated=any(item.truncation.truncated for item in evidence),
    )
    return chunks, evidence, retrieval_trace


def _count_repo_chunks(store, repo_name: str) -> int:
    """Count code chunks belonging to a specific repo (best-effort)."""
    col_name = settings.repos.collection_name
    try:
        from store.qdrant_store import QdrantVectorStore
        if isinstance(store, QdrantVectorStore):
            qdrant_models = store._models
            result = store._client.count(
                collection_name=col_name,
                count_filter=qdrant_models.Filter(
                    must=[qdrant_models.FieldCondition(
                        key="repo_name",
                        match=qdrant_models.MatchValue(value=repo_name),
                    )]
                ),
            )
            return int(result.count)
    except ImportError:
        pass
    return 0


@app.post("/query", response_model=QueryResponse)
def query(req: QueryRequest):
    """Pesquisa semântica nas notas Obsidian (obsidian_vault)."""
    if not req.query.strip():
        raise HTTPException(status_code=400, detail="Query vazia")
    start = time.time()
    store = _get_store()
    filters: dict | None = None
    if req.vault:
        filters = {"source_name": req.vault}
    trace = QueryTrace(query=req.query) if getattr(req, "debug", False) else None
    chunks, evidence, retrieval_trace = _query_store(store, "obsidian_vault", req, filters=filters, trace=trace)
    elapsed_ms = (time.time() - start) * 1000
    debug_dict = None
    if trace is not None:
        trace.finish()
        debug_dict = trace.to_debug_dict()
    return QueryResponse(
        results=chunks,
        query=req.query,
        elapsed_ms=round(elapsed_ms, 1),
        evidence=evidence,
        retrieval_trace=retrieval_trace,
        trace=debug_dict,
    )


@app.post("/query/code", response_model=QueryResponse)
def query_code(req: CodeQueryRequest):
    """Pesquisa semântica na coleção de código (code_repos).

    Suporta filtro por repo_name e symbol_type.
    """
    if not req.query.strip():
        raise HTTPException(status_code=400, detail="Query vazia")
    if not _has_repo_sources():
        raise HTTPException(status_code=404, detail="Sem fontes de código/documentos preparadas")

    start = time.time()
    store = _get_store()

    # Build query-time filters
    code_filters: dict[str, str] = {}
    if req.repo:
        code_filters["repo_name"] = req.repo
    if req.symbol_type:
        code_filters["symbol_type"] = req.symbol_type

    trace = QueryTrace(query=req.query) if getattr(req, "debug", False) else None
    chunks, evidence, retrieval_trace = _query_store(
        store,
        settings.repos.collection_name,
        req,
        filters=code_filters or None,
        trace=trace,
    )

    elapsed_ms = (time.time() - start) * 1000
    debug_dict = None
    if trace is not None:
        trace.finish()
        debug_dict = trace.to_debug_dict()
    return QueryResponse(
        results=chunks,
        query=req.query,
        elapsed_ms=round(elapsed_ms, 1),
        evidence=evidence,
        retrieval_trace=retrieval_trace,
        trace=debug_dict,
    )


@app.get("/repos", response_model=ReposResponse)
def repos():
    """Lista repos configurados com stats de chunks e grafo."""
    from pipeline.graph.query import list_repos

    repo_infos_raw = list_repos()
    repo_list = []
    for r in repo_infos_raw:
        # Contar chunks de código para este repo
        code_chunks = 0
        if _has_repo_sources():
            try:
                store = _get_store()
                code_chunks = _count_repo_chunks(store, r["name"])
            except Exception:
                pass

        repo_list.append(RepoInfo(
            name=r["name"],
            path=r["path"],
            exists=r["exists"],
            graph_built=r["graph_built"],
            graph_path=r.get("graph_path"),
            report_path=r.get("report_path"),
            node_count=r.get("node_count"),
            edge_count=r.get("edge_count"),
            code_chunks=code_chunks,
        ))

    return ReposResponse(
        repos=repo_list,
        graphify_enabled=settings.graphify.enabled,
        graphify_backend=settings.graphify.backend,
    )


@app.get("/graph/{repo}")
def graph_report(repo: str):
    """Devolve o GRAPH_REPORT.md de um repo como texto."""
    from pipeline.graph.query import get_report
    try:
        report = get_report(repo)
        return {"repo": repo, "report": report}
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post("/graph/{repo}/query")
def graph_query(repo: str, req: GraphQueryRequest):
    """Executa uma query ao knowledge graph de um repo."""
    from pipeline.graph.query import query_graph
    result = query_graph(repo, req.query)
    return {"repo": repo, "query": req.query, "result": result}


@app.get("/graph/{repo}/neighbors/{node}")
def graph_neighbors(repo: str, node: str, max_results: int = 10):
    """Devolve nós vizinhos de um conceito no grafo."""
    from pipeline.graph.query import get_neighbors
    try:
        neighbors = get_neighbors(repo, node, max_results=max_results)
        return GraphNeighborsResponse(node=node, repo=repo, neighbors=neighbors)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ImportError as e:
        raise HTTPException(status_code=503, detail=str(e))


# --- Phase 2 endpoints: CAG, Graph Context, Indexing Status ---


# Maps a coarse intent to the CAG pack types that are most relevant.
# Values MUST match the PackType enum string values in cag/packs.py.
_CAG_INTENT_MAP: dict[str, set[str]] = {
    "local": {"vault_summary", "recurring_errors", "pending_tasks", "knowledge_graph_summary"},
    "code": {"project_architecture", "repo_state", "rag_index_state"},
    "system": {"system_state", "local_services", "local_models", "config_environment", "security_exclusions"},
    "graph": {"knowledge_graph_summary", "project_architecture"},
}


def _infer_cag_intent(query: str | None) -> str:
    """Heuristic mapping of a free-text query to a CAG intent bucket."""
    if not query:
        return "local"
    q = query.lower()
    if any(w in q for w in ("repo", "code", "function", "class", "module", "código", "codigo")):
        return "code"
    if any(w in q for w in ("graph", "grafo", "neighbor", "vizinho", "community", "comunidade")):
        return "graph"
    if any(w in q for w in ("system", "sistema", "service", "serviço", "servico", "model", "modelo", "hardware", "gpu", "config")):
        return "system"
    return "local"


def _set_admin_job(job_key: str, **updates: Any) -> None:
    updates.setdefault("updated_at", time.time())
    with _admin_jobs_lock:
        current = dict(_admin_jobs.get(job_key, {}))
        current.update(updates)
        _admin_jobs[job_key] = current
    _admin_job_store.upsert(job_key, current)


def _get_admin_cancel_event(job_key: str) -> threading.Event | None:
    with _admin_jobs_lock:
        return _admin_job_cancel_events.get(job_key)


def _ensure_admin_cancel_event(job_key: str) -> threading.Event:
    with _admin_jobs_lock:
        event = _admin_job_cancel_events.get(job_key)
        if event is None:
            event = threading.Event()
            _admin_job_cancel_events[job_key] = event
        return event


def _clear_admin_cancel_event(job_key: str) -> None:
    with _admin_jobs_lock:
        _admin_job_cancel_events.pop(job_key, None)


def _get_admin_job(job_key: str) -> dict[str, Any] | None:
    with _admin_jobs_lock:
        job = _admin_jobs.get(job_key)
    stored = _admin_job_store.get(job_key)
    if stored is None:
        return dict(job) if job is not None else None
    if job is not None and float(job.get("updated_at") or 0.0) > float(stored.get("updated_at") or 0.0):
        return dict(job)
    with _admin_jobs_lock:
        _admin_jobs[job_key] = dict(stored)
    return dict(stored)


def _admin_job_response_payload(job: dict[str, Any]) -> dict[str, Any]:
    payload = dict(job)
    if not payload.get("target"):
        result = dict(payload.get("result") or {})
        result.setdefault("response_warnings", []).append("admin job record did not include target; reported as unknown")
        payload["result"] = result
        payload["target"] = str(result.get("target") or "unknown")
    payload.setdefault("force", False)
    payload.setdefault("origin", {})
    payload.setdefault("result", {})
    return payload


def _hydrate_admin_jobs() -> None:
    stored = _admin_job_store.load_all()
    if not stored:
        return
    with _admin_jobs_lock:
        for job_id, job in stored.items():
            current = _admin_jobs.get(job_id)
            if current is None:
                _admin_jobs[job_id] = dict(job)
                continue
            stored_updated_at = float(job.get("updated_at") or 0.0)
            current_updated_at = float(current.get("updated_at") or 0.0)
            if stored_updated_at > current_updated_at:
                _admin_jobs[job_id] = dict(job)


def _recover_interrupted_admin_jobs(*, stale_after_seconds: int = 300) -> int:
    """Close pre-existing active jobs that cannot have a live in-process worker."""
    _hydrate_admin_jobs()
    now = time.time()
    recovered = 0
    stale_statuses = {
        "queued",
        "running",
        "cancel_requested",
        "paused_resource_pressure",
        "deferred_resource_pressure",
        "retry_scheduled",
        "submitted",
    }
    for job_id, job in list(_admin_job_store.load_all().items()):
        status = str(job.get("status") or "")
        if status not in stale_statuses:
            continue
        updated_at = float(job.get("updated_at") or job.get("started_at") or 0.0)
        retry_at = float((job.get("result") or {}).get("retry_at") or job.get("retry_at") or 0.0)
        if status == "submitted" and (job.get("result") or {}).get("backend") == "temporal":
            continue
        if status == "retry_scheduled" and retry_at and retry_at > now:
            continue
        if updated_at and now - updated_at < stale_after_seconds:
            continue
        result = _mark_result_children_interrupted(dict(job.get("result") or {}), now)
        result.update(
            {
                "recovered_interrupted_at": now,
                "previous_status": status,
                "resource_state": "interrupted",
                "reason": "RAG API startup recovered stale active job without live heartbeat",
            }
        )
        _set_admin_job(
            job_id,
            status="interrupted",
            finished_at=now,
            error="interrupted during previous RAG runtime",
            result=result,
        )
        recovered += 1
    return recovered


def _admin_origin_from_request(payload: AdminReprocessRequest, http_request: Request | None = None) -> dict[str, Any]:
    origin = payload.origin.model_dump(mode="json")
    headers = http_request.headers if http_request is not None else {}

    header_kind = headers.get("x-ai-origin-kind") or headers.get("x-rag-origin-kind")
    if header_kind and origin.get("kind") == "unknown":
        normalized_kind = header_kind.strip().lower().replace("-", "_")
        if normalized_kind in {"user_machine", "agent", "feature", "service", "scheduler", "unknown"}:
            origin["kind"] = normalized_kind

    header_map = {
        "agent": ("x-ai-agent", "x-agent-name"),
        "feature": ("x-ai-feature", "x-feature-name"),
        "service": ("x-ai-service", "x-service-name"),
        "machine": ("x-ai-machine", "x-user-machine", "x-host-machine"),
        "user": ("x-ai-user", "x-user"),
        "trace_id": ("x-request-id", "x-correlation-id", "traceparent"),
    }
    for field, candidates in header_map.items():
        if origin.get(field):
            continue
        for header in candidates:
            value = headers.get(header)
            if value:
                origin[field] = value[:255]
                break

    metadata = dict(origin.get("metadata") or {})
    if http_request is not None:
        if http_request.client and "client_host" not in metadata:
            metadata["client_host"] = http_request.client.host
        user_agent = headers.get("user-agent")
        if user_agent and "user_agent" not in metadata:
            metadata["user_agent"] = user_agent[:255]
    origin["metadata"] = {str(k): str(v)[:500] for k, v in list(metadata.items())[:50]}

    if origin.get("kind") == "unknown":
        if origin.get("agent"):
            origin["kind"] = "agent"
        elif origin.get("feature"):
            origin["kind"] = "feature"
        elif origin.get("service"):
            origin["kind"] = "service"
        elif origin.get("machine") or origin.get("user"):
            origin["kind"] = "user_machine"

    if not origin.get("machine"):
        origin["machine"] = socket.gethostname()
    if not origin.get("name"):
        origin["name"] = (
            origin.get("agent")
            or origin.get("feature")
            or origin.get("service")
            or origin.get("user")
            or origin.get("machine")
            or "unknown"
        )
    return origin


def _refresh_temporal_admin_job(job_id: str, job: dict[str, Any]) -> dict[str, Any]:
    result = dict(job.get("result") or {})
    if job.get("status") != "submitted" or result.get("backend") != "temporal":
        return job
    try:
        from workflows.temporal_client import describe_reprocess_workflow

        status = describe_reprocess_workflow(
            str(result.get("workflow_id") or ""),
            str(result.get("run_id") or ""),
        )
        result = {**result, **status}
        updates: dict[str, Any] = {"result": result}
        if status.get("status") in {"completed", "failed", "canceled"}:
            updates["status"] = status["status"]
            updates["finished_at"] = time.time()
        else:
            updates["status"] = status.get("status", "submitted")
        _set_admin_job(job_id, **updates)
        return _get_admin_job(job_id) or {**job, **updates}
    except Exception as exc:
        result["temporal_status_error"] = str(exc)[:500]
        _set_admin_job(job_id, result=result)
        return _get_admin_job(job_id) or {**job, "result": result}


def _execute_or_submit_admin_reprocess(
    job_id: str,
    *,
    target: str,
    force: bool,
    vault: str | None,
    sources: list[dict[str, Any]] | None = None,
    origin: dict[str, Any] | None = None,
    extra_result: dict[str, Any] | None = None,
    cancel_event: threading.Event | None = None,
) -> None:
    payload = {
        "job_id": job_id,
        "target": target,
        "force": force,
        "vault": vault,
        "sources": sources or [],
        "origin": origin or {"kind": "unknown", "name": "unknown"},
    }
    result_prefix = extra_result or {}
    if settings.workflows.backend == "temporal":
        from workflows.temporal_client import start_reprocess_workflow

        result = start_reprocess_workflow(job_id, payload)
        _set_admin_job(
            job_id,
            status="submitted",
            finished_at=None,
            origin=payload["origin"],
            result={**payload, **result_prefix, **result, "submitted_at": time.time()},
        )
        return

    if settings.workflows.backend != "direct":
        raise ValueError(f"Unsupported workflows backend: {settings.workflows.backend}")

    from workflows.reprocess import execute_reprocess_target

    result = execute_reprocess_target(
        target,
        force=force,
        vault=vault,
        sources=sources,
        origin=payload["origin"],
        job_id=job_id,
        cancel_event=cancel_event,
    )
    if cancel_event is not None and cancel_event.is_set():
        from workflows.reprocess import ReprocessCancelled

        raise ReprocessCancelled("Admin reprocess job canceled")
    _set_admin_job(
        job_id,
        status="completed",
        finished_at=time.time(),
        origin=payload["origin"],
        result={**result, **result_prefix},
    )


def _release_admin_job_runtime_resources(job_id: str) -> None:
    if settings.workflows.backend != "direct":
        return
    try:
        import embeddings as embeddings_module
        from pipeline.governor import release_process_memory

        def _clear_embedder_cache_if_loaded() -> None:
            embedder = getattr(embeddings_module, "_embedder", None)
            if embedder is not None:
                embedder.clear_cache()

        cleanup = release_process_memory(
            perf=settings.performance,
            label=f"admin_reprocess:{job_id}",
            clear_cache_callback=_clear_embedder_cache_if_loaded,
        )
    except Exception as exc:
        cleanup = {
            "scope": "owner_process_local",
            "label": f"admin_reprocess:{job_id}",
            "enabled": bool(getattr(settings.performance, "job_end_memory_cleanup", True)),
            "error": str(exc)[:500],
            "global_cleanup_forbidden": ["swapoff", "drop_caches", "kill_unknown_processes", "docker_prune"],
        }

    job = _get_admin_job(job_id) or {}
    result = dict(job.get("result") or {})
    result["runtime_cleanup"] = cleanup
    _set_admin_job(job_id, result=result)


def _mark_result_children_cancelled(result: dict[str, Any], now: float) -> dict[str, Any]:
    children = []
    for child in result.get("children") or []:
        if not isinstance(child, dict):
            continue
        updated = dict(child)
        status = str(updated.get("status") or "")
        if status not in _admin_terminal_statuses:
            updated["status"] = "cancelled"
            updated["resource_state"] = "cancelled"
            updated.setdefault("finished_at", now)
            updated["lease_status"] = "release_requested"
        children.append(updated)
    if children:
        result["children"] = children
        result["children_completed"] = sum(1 for child in children if child.get("status") == "completed")
        result["children_failed"] = sum(
            1 for child in children
            if child.get("status") in {"failed", "failed_resource_pressure", "cancelled", "canceled"}
        )
    return result


def _mark_result_children_interrupted(result: dict[str, Any], now: float) -> dict[str, Any]:
    children = []
    for child in result.get("children") or []:
        if not isinstance(child, dict):
            continue
        updated = dict(child)
        status = str(updated.get("status") or "")
        if status not in _admin_terminal_statuses:
            updated["status"] = "interrupted"
            updated["resource_state"] = "interrupted"
            updated.setdefault("finished_at", now)
            updated["lease_status"] = "release_requested"
        children.append(updated)
    if children:
        result["children"] = children
        result["children_completed"] = sum(1 for child in children if child.get("status") == "completed")
        result["children_failed"] = sum(
            1 for child in children
            if child.get("status") in {"failed", "failed_resource_pressure", "cancelled", "canceled", "interrupted"}
        )
    return result


def _request_admin_job_cancel(job_id: str) -> AdminJobStatusResponse:
    job = _get_admin_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    status = str(job.get("status") or "")
    if status in _admin_terminal_statuses:
        return AdminJobStatusResponse(**_admin_job_response_payload(job))

    now = time.time()
    result = _mark_result_children_cancelled(dict(job.get("result") or {}), now)
    result["cancel_requested_at"] = now
    result["resource_state"] = "cancelled"

    if result.get("backend") == "temporal" and result.get("workflow_id"):
        try:
            from workflows.temporal_client import cancel_reprocess_workflow

            cancel_result = cancel_reprocess_workflow(
                str(result.get("workflow_id") or ""),
                str(result.get("run_id") or ""),
            )
            result.update(cancel_result)
            _set_admin_job(
                job_id,
                status="cancel_requested",
                finished_at=None,
                error=None,
                result=result,
            )
        except Exception as exc:
            result["cancel_error"] = str(exc)[:500]
            _set_admin_job(job_id, result=result)
            raise HTTPException(status_code=502, detail="Failed to cancel Temporal workflow") from exc
    else:
        _ensure_admin_cancel_event(job_id).set()
        try:
            from pipeline.graph.builder import cancel_active_graphify_processes

            result["graphify_subprocess_cancel"] = cancel_active_graphify_processes()
        except Exception as exc:
            result["graphify_subprocess_cancel_error"] = str(exc)[:500]
        _set_admin_job(
            job_id,
            status="cancel_requested",
            finished_at=None,
            error=None,
            result=result,
        )

    updated = _get_admin_job(job_id) or job
    return AdminJobStatusResponse(**_admin_job_response_payload(updated))


def _run_admin_reprocess(job_id: str, request: AdminReprocessRequest, origin: dict[str, Any]) -> None:
    cancel_event = _ensure_admin_cancel_event(job_id)
    if cancel_event.is_set():
        _set_admin_job(job_id, status="canceled", finished_at=time.time(), error=None)
        _clear_admin_cancel_event(job_id)
        return
    _set_admin_job(job_id, status="running", started_at=time.time())
    try:
        _execute_or_submit_admin_reprocess(
            job_id,
            target=request.target,
            force=request.force,
            vault=request.vault,
            sources=[source.model_dump(mode="json") for source in request.sources],
            origin=origin,
            cancel_event=cancel_event,
        )
    except Exception as exc:
        if exc.__class__.__name__ == "ReprocessCancelled":
            result = dict((_get_admin_job(job_id) or {}).get("result") or {})
            result["cancel_confirmed_at"] = time.time()
            _set_admin_job(job_id, status="canceled", finished_at=time.time(), error=None, result=result)
        elif hasattr(exc, "status"):
            status = str(getattr(exc, "status", "failed_resource_pressure"))
            if status == "deferred_resource_pressure":
                status = "failed_resource_pressure"
            result = dict((_get_admin_job(job_id) or {}).get("result") or {})
            payload_fn = getattr(exc, "payload", None)
            if callable(payload_fn):
                result.update(payload_fn())
            result.setdefault("resource_state", status)
            _set_admin_job(job_id, status=status, finished_at=time.time(), error=str(exc)[:1000], result=result)
        else:
            _set_admin_job(job_id, status="failed", finished_at=time.time(), error=str(exc)[:1000])
    finally:
        _release_admin_job_runtime_resources(job_id)
        _clear_admin_cancel_event(job_id)


@app.get("/cag/packs", response_model=CagPacksResponse)
def cag_packs(
    intent: str | None = None,
    budget: int | None = None,
    scope: str | None = None,
):
    """Return CAG packs, optionally filtered by intent and capped by token budget.

    Args:
        intent: Filter pack types relevant to an intent (local, code, system, graph).
        budget: Maximum total tokens to return (packs sorted by relevance).
        scope: Optional exact pack scope. Use this to keep source-specific retrieval isolated.
    """
    from cag import get_pack_store

    if not settings.cag.enabled:
        raise HTTPException(status_code=404, detail="CAG disabled in configuration")

    store = get_pack_store()
    all_packs = store.list_packs()
    now = time.time()

    if intent and intent in _CAG_INTENT_MAP:
        relevant_types = _CAG_INTENT_MAP[intent]
        all_packs = [p for p in all_packs if p.pack_type in relevant_types]
    if scope:
        all_packs = [p for p in all_packs if p.scope == scope]

    items: list[CagPackItem] = []
    total_tokens = 0
    for p in all_packs:
        tokens = len(p.content) // 4  # rough estimate
        if budget and (total_tokens + tokens) > budget:
            break
        fresh = now < p.expires_at
        items.append(CagPackItem(
            pack_type=p.pack_type,
            scope=p.scope,
            content=p.content,
            tokens=tokens,
            fresh=fresh,
        ))
        total_tokens += tokens

    return CagPacksResponse(
        packs=items,
        total_tokens=total_tokens,
        total_packs=len(items),
    )


@app.get("/cag/packs/{pack_type}", response_model=CagPackDetail)
def cag_pack_detail(pack_type: str, scope: str = "global"):
    """Return a single CAG pack with freshness/provenance detail."""
    from cag import get_pack_store

    if not settings.cag.enabled:
        raise HTTPException(status_code=404, detail="CAG disabled in configuration")

    store = get_pack_store()
    pack = store.get_pack(pack_type, scope)
    if pack is None:
        raise HTTPException(status_code=404, detail=f"Pack '{pack_type}' (scope={scope}) not found")

    now = time.time()
    fresh = now < pack.expires_at
    stale_reason = None if fresh else "expired"

    def _iso(ts: float) -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts))

    return CagPackDetail(
        pack_type=pack.pack_type,
        scope=pack.scope,
        content=pack.content,
        tokens=len(pack.content) // 4,
        fresh=fresh,
        age_seconds=round(now - pack.created_at, 1),
        expires_at=_iso(pack.expires_at),
        created_at=_iso(pack.created_at),
        source_hash=pack.source_hash or None,
        config_version=pack.config_version or None,
        stale_reason=stale_reason,
    )


@app.post("/cag/explain", response_model=CagExplainResponse)
def cag_explain(req: CagExplainRequest):
    """Explain which CAG packs would be selected for an intent/query and why.

    Returns the selection decision per pack type (selected/reason/freshness),
    making CAG behaviour observable without consuming the packs.
    """
    from cag import get_pack_store

    if not settings.cag.enabled:
        raise HTTPException(status_code=404, detail="CAG disabled in configuration")

    intent = req.intent if (req.intent in _CAG_INTENT_MAP) else _infer_cag_intent(req.query)
    relevant_types = _CAG_INTENT_MAP.get(intent, set())

    store = get_pack_store()
    available = {p.pack_type: p for p in store.list_packs()}
    now = time.time()

    items: list[CagExplainItem] = []
    selected_packs: list[str] = []
    total_tokens = 0
    for pack_type in sorted(relevant_types):
        pack = available.get(pack_type)
        if pack is None:
            items.append(CagExplainItem(
                pack_type=pack_type,
                selected=False,
                reason="not_generated",
                available=False,
            ))
            continue

        tokens = len(pack.content) // 4
        fresh = now < pack.expires_at
        age = round(now - pack.created_at, 1)
        if not fresh:
            items.append(CagExplainItem(
                pack_type=pack_type, selected=False, reason="stale",
                available=True, fresh=False, tokens=tokens, age_seconds=age,
            ))
            continue
        if req.budget and (total_tokens + tokens) > req.budget:
            items.append(CagExplainItem(
                pack_type=pack_type, selected=False, reason="over_budget",
                available=True, fresh=True, tokens=tokens, age_seconds=age,
            ))
            continue

        selected_packs.append(pack_type)
        total_tokens += tokens
        items.append(CagExplainItem(
            pack_type=pack_type, selected=True, reason="selected",
            available=True, fresh=True, tokens=tokens, age_seconds=age,
        ))

    return CagExplainResponse(
        intent=intent,
        budget=req.budget,
        selected_packs=selected_packs,
        total_tokens=total_tokens,
        items=items,
    )


@app.post("/admin/reprocess", response_model=AdminJobResponse)
def admin_reprocess(
    request: AdminReprocessRequest,
    background_tasks: BackgroundTasks,
    http_request: Request,
):
    """Start a manual RAG/Graphify/CAG reprocess job through the API only."""
    job_id = uuid.uuid4().hex
    origin = _admin_origin_from_request(request, http_request)
    _ensure_admin_cancel_event(job_id)
    _set_admin_job(
        job_id,
        job_id=job_id,
        parent_job_id=None,
        status="queued",
        target=request.target,
        force=request.force,
        started_at=None,
        finished_at=None,
        error=None,
        origin=origin,
        result={"requested_sources": len(request.sources), "origin": origin, "children": []},
    )
    background_tasks.add_task(_run_admin_reprocess, job_id, request, origin)
    return AdminJobResponse(
        job_id=job_id,
        parent_job_id=None,
        status="queued",
        target=request.target,
        force=request.force,
        origin=origin,
        status_url=f"/admin/jobs/{job_id}",
        message="Reprocess job accepted. Poll status_url for completion.",
    )


@app.get("/admin/jobs", response_model=AdminJobsResponse)
def admin_jobs(status: str | None = None, active_only: bool = False, limit: int = 50):
    """List API-triggered admin jobs, newest first."""
    _hydrate_admin_jobs()
    limit = max(1, min(int(limit), 200))
    wanted_statuses = {
        item.strip()
        for item in (status or "").split(",")
        if item.strip()
    }

    with _admin_jobs_lock:
        jobs = [dict(job) for job in _admin_jobs.values()]

    refreshed = [
        _refresh_temporal_admin_job(str(job.get("job_id") or ""), job)
        for job in jobs
        if job.get("job_id")
    ]
    active_count = sum(1 for job in refreshed if job.get("status") in _admin_active_statuses)
    if active_only:
        refreshed = [job for job in refreshed if job.get("status") in _admin_active_statuses]
    if wanted_statuses:
        refreshed = [job for job in refreshed if str(job.get("status") or "") in wanted_statuses]
    refreshed.sort(
        key=lambda job: float(job.get("started_at") or job.get("finished_at") or 0.0),
        reverse=True,
    )
    items = [AdminJobStatusResponse(**_admin_job_response_payload(job)) for job in refreshed[:limit]]
    return AdminJobsResponse(jobs=items, total=len(refreshed), active=active_count)


@app.get("/admin/jobs/{job_id}", response_model=AdminJobStatusResponse)
def admin_job_status(job_id: str):
    """Return status for an API-triggered admin job."""
    job = _get_admin_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    job = _refresh_temporal_admin_job(job_id, job)
    return AdminJobStatusResponse(**_admin_job_response_payload(job))


@app.post("/admin/jobs/{job_id}/cancel", response_model=AdminJobStatusResponse)
def admin_cancel_job_post(job_id: str):
    """Request cancellation for an API-triggered admin job."""
    return _request_admin_job_cancel(job_id)


@app.post("/graph/context", response_model=GraphContextResponse)
def graph_context(req: GraphContextRequest):
    """Aggregate graph context for a query across repos.

    Returns relevant nodes, community summaries, and god nodes
    for use by external consumers (e.g., symbiont).
    """
    from pipeline.graph.backend import get_graph_backend
    from pipeline.graph.query import list_repos

    start = time.time()
    backend = get_graph_backend()
    available_repos = [repo["name"] for repo in list_repos() if repo.get("graph_built")]
    target_repos = req.repos if req.repos else available_repos

    results: list[GraphContextItem] = []
    total_nodes = 0

    for repo_name in target_repos:
        if repo_name not in available_repos:
            continue

        context = backend.context_for_query(
            repo_name,
            req.query,
            max_nodes=req.max_nodes,
            include_summaries=req.include_summaries,
        )
        matched_nodes = context["nodes"]
        matched_edges = context["edges"]
        summaries = context["summaries"]
        god_nodes = context["god_nodes"]

        if matched_nodes or summaries or god_nodes:
            node_labels = [n.get("label", "") for n in matched_nodes if n.get("label")]
            summary_parts: list[str] = []
            if summaries:
                summary_parts.extend(summaries[:3])
            if node_labels:
                summary_parts.append("Matched nodes: " + ", ".join(node_labels[:10]))
            if matched_edges:
                summary_parts.append(f"Matched edges: {len(matched_edges)}")
            if god_nodes:
                summary_parts.append("Central nodes: " + ", ".join(god_nodes[:5]))

            results.append(GraphContextItem(
                repo=repo_name,
                title=f"{repo_name} graph context",
                summary="\n\n".join(summary_parts),
                nodes=matched_nodes,
                edges=matched_edges,
                summaries=summaries,
                god_nodes=god_nodes,
            ))
            total_nodes += len(matched_nodes)

    elapsed_ms = (time.time() - start) * 1000
    return GraphContextResponse(
        results=results,
        total_nodes=total_nodes,
        elapsed_ms=round(elapsed_ms, 1),
    )


@app.get("/status/indexing", response_model=IndexingStatusResponse)
def indexing_status():
    """Return current indexing pipeline status from the manifest DB."""
    from pipeline.manifest import IngestManifest

    manifest_path = settings.paths.data_dir / "manifest.db"
    if not manifest_path.exists():
        return IndexingStatusResponse(status="idle")

    manifest = IngestManifest(manifest_path)
    try:
        stats = manifest.stats()
        incomplete = manifest.get_last_incomplete_run()

        with manifest._lock:
            conn = manifest._get_conn()
            last_run = conn.execute(
                _sql("execute_1028.sql")
            ).fetchone()

        status = "running" if incomplete else "idle"
        last_run_at = last_run[0] if last_run else None
        last_run_status = last_run[1] if last_run else None

        from pipeline.sync import _compute_config_version
        config_ver = _compute_config_version()

        return IndexingStatusResponse(
            status=status,
            last_run_at=last_run_at,
            last_run_status=last_run_status,
            files_indexed=stats["files"],
            chunks_tracked=stats["chunks"],
            chunks_embedded=stats["embedded"],
            config_version=config_ver,
        )
    finally:
        manifest.close()


@app.get("/status/retrieval", response_model=RetrievalStatusResponse)
def retrieval_status(hours: int = 24, recent: int = 20):
    """Return local retrieval audit summary, recent entries, and BM25 health."""
    from retrieval.audit import read_recent, summary_stats
    from retrieval.sparse import bm25_status

    hours = max(1, min(hours, 168))
    recent = max(0, min(recent, 100))

    collections = ["obsidian_vault"]
    if settings.repos.collection_name:
        collections.append(settings.repos.collection_name)
    bm25 = [bm25_status(c) for c in collections]

    return RetrievalStatusResponse(
        summary=summary_stats(hours=hours),
        recent=read_recent(recent) if recent else [],
        bm25=bm25,
    )


@app.get("/status/bm25")
def bm25_status_endpoint():
    """Return BM25 governance/health for every active collection.

    Lightweight standalone view (does not load the full vectorizer) reporting
    availability, vocab size, document count and model age/version hash.
    """
    from retrieval.sparse import bm25_status

    collections = ["obsidian_vault"]
    if settings.repos.collection_name:
        collections.append(settings.repos.collection_name)

    statuses = [bm25_status(c) for c in collections]
    return {
        "collections": statuses,
        "all_available": all(s.get("available") for s in statuses),
    }


@app.post("/query/batch", response_model=BatchQueryResponse)
def query_batch(req: BatchQueryRequest):
    """Execute multiple queries in a single request.

    Useful for symbiont's parallel context retrieval (notes + code)
    without needing separate HTTP roundtrips.
    """
    start = time.time()
    store = _get_store()
    results: list[BatchQueryResultItem] = []

    for item in req.queries:
        query_embedding = get_embedder().get_query_embedding(item.query)
        raw_results = store.query(
            query_embedding, n=item.top_k, collection=item.collection
        )
        chunks = []
        for r in raw_results:
            if r.score >= item.min_score:
                display = r.metadata.get("display_text", r.document)
                chunks.append(ChunkResult(
                    text=display,
                    score=round(r.score, 4),
                    source_path=r.metadata.get("source_path", ""),
                    note_title=r.metadata.get("note_title", ""),
                    section_header=r.metadata.get("section_header", ""),
                    source_type=r.metadata.get("source_type", "markdown"),
                    repo_name=r.metadata.get("repo_name"),
                    symbol_type=r.metadata.get("symbol_type"),
                ))
        results.append(BatchQueryResultItem(query=item.query, results=chunks))

    elapsed_ms = (time.time() - start) * 1000
    return BatchQueryResponse(results=results, elapsed_ms=round(elapsed_ms, 1))


def _inject_rag_into_messages(messages: list[ChatMessage], context: str) -> list[dict]:
    """Inject RAG context as system message prefix."""
    msg_list = [m.model_dump() for m in messages]
    if msg_list and msg_list[0]["role"] == "system":
        msg_list[0]["content"] = context + "\n\n" + msg_list[0]["content"]
    else:
        msg_list.insert(0, {"role": "system", "content": context})
    return msg_list


def _ensure_system_prompt(messages: list[dict]) -> list[dict]:
    """Ensure a domain-neutral system prompt exists when no RAG context is injected."""
    if messages and messages[0]["role"] == "system":
        return messages
    return [{"role": "system", "content": SYSTEM_GENERAL}] + messages


@app.post("/chat")
@limiter.limit(f"{_chat_rate_limit}/minute" if _chat_rate_limit and _chat_rate_limit > 0 else "999999/minute")
async def chat(request: Request, req: ChatRequest):
    """RAG-augmented chat proxy to Ollama.

    Now uses the LLM router to decide if context is needed.
    For general questions, the LLM responds without any RAG context.
    """
    if req.agentic and req.context_package is None:
        raise HTTPException(
            status_code=400,
            detail="RAG /chat agentic flows require context_package; use agentic=false only for manual/debug chat.",
        )

    rag_used = False
    sources_used = "none"
    messages = [m.model_dump() for m in req.messages]
    trace = QueryTrace()
    trace.model = req.model

    if should_use_rag(req.model):
        user_msgs = [m for m in req.messages if m.role == "user"]
        if user_msgs:
            query_text = user_msgs[-1].content
            trace.query = query_text

            # Build history context for multi-turn router awareness
            prev_messages = [
                {"role": m.role, "content": m.content}
                for m in req.messages[:-1]
            ] or None

            context, relevant, sources_used = await build_rag_context_async(
                query_text,
                context_mode=req.context_mode,
                trace=trace,
                history=prev_messages,
            )
            if relevant:
                messages = _inject_rag_into_messages(req.messages, context)
                rag_used = True
            else:
                # No context needed — ensure clean system prompt
                messages = _ensure_system_prompt(messages)
    else:
        messages = _ensure_system_prompt(messages)

    trace.finish()

    ollama_payload = {
        "model": req.model,
        "messages": messages,
        "stream": req.stream,
    }

    if not req.stream:
        assert _http_pool is not None, "HTTP pool not initialized"
        resp = await _http_pool.post("/api/chat", json=ollama_payload)
        data = resp.json()
        data["rag_used"] = rag_used
        data["sources_used"] = sources_used
        data["agentic"] = req.agentic
        if settings.debug.enabled:
            data["debug"] = trace.to_debug_dict()
        return data

    # Streaming proxy
    async def _stream_ollama():
        assert _http_pool is not None, "HTTP pool not initialized"
        async with _http_pool.stream("POST", "/api/chat", json=ollama_payload) as resp:
            async for line in resp.aiter_lines():
                if line:
                    yield line + "\n"
        # Append debug info as final NDJSON line if debug enabled
        if settings.debug.enabled:
            yield _json.dumps({"debug": trace.to_debug_dict()}) + "\n"

    return StreamingResponse(
        _stream_ollama(),
        media_type="application/x-ndjson",
        headers={
            "X-RAG-Used": str(rag_used).lower(),
            "X-Sources-Used": sources_used,
            "X-Route-Mode": trace.route_mode or "unknown",
            "X-Agentic-Flow": str(req.agentic).lower(),
        },
    )


def serve():
    """Start the RAG FastAPI server."""
    import uvicorn

    host = settings.api.host
    port = settings.api.port

    # Security: refuse 0.0.0.0 without API key
    if host == "0.0.0.0" and not settings.api.api_key:  # nosec B104
        import sys
        print(
            "ERRO: API exposta em 0.0.0.0 sem api_key configurada.\n"
            "Define [api] api_key em rag.user.toml ou usa host = \"127.0.0.1\".",
            file=sys.stderr,
        )
        sys.exit(1)

    uvicorn.run(
        "api.app:app",
        host=host,
        port=port,
        ssl_certfile=_required_tls_file("AI_LOCAL_TLS_CERT_FILE"),
        ssl_keyfile=_required_tls_file("AI_LOCAL_TLS_KEY_FILE"),
    )


def _required_tls_file(env_name: str) -> str:
    value = os.environ.get(env_name)
    if not value:
        raise RuntimeError(f"{env_name} is required for HTTPS API serving")
    return value


if __name__ == "__main__":
    serve()
