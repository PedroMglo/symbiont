"""Pydantic schemas for API request/response models."""

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from rag_config import settings

RAG_EVIDENCE_CONTRACT_VERSION = "rag-evidence.v1"
RETRIEVAL_TRACE_CONTRACT_VERSION = "retrieval-trace.v1"


class QueryRequest(BaseModel):
    query: str = Field(..., description="Texto da pergunta/busca", min_length=1, max_length=10000)
    top_k: int = Field(default=settings.api.query_top_k, ge=1, le=50)
    min_score: float = Field(default=0.0, ge=0.0, le=1.0)
    vault: str | None = Field(default=None, description="Filtrar por vault (nome do directório)")
    source_type: str | None = Field(default=None, description="Filtrar por source_type (ex: markdown, code, repo_doc)")
    exclude_source_type: str | None = Field(default=None, description="Excluir source_type (ex: repo_doc)")
    debug: bool = Field(default=False, description="Devolver trace de diagnóstico (dense/sparse, reranker, scores)")


class CodeQueryRequest(QueryRequest):
    """Query apenas na coleção de código (code_repos)."""
    repo: str | None = Field(default=None, description="Filtrar por repo_name específico (opcional)")
    symbol_type: str | None = Field(default=None, description="Filtrar por tipo de símbolo: function, class, method, module")


class ChunkResult(BaseModel):
    text: str
    content: str | None = None
    score: float
    source_path: str
    note_title: str
    section_header: str
    # Campos opcionais presentes em chunks de código
    source_type: str = "markdown"
    repo_name: str | None = None
    symbol_type: str | None = None

    @model_validator(mode="after")
    def _fill_content_alias(self) -> "ChunkResult":
        if self.content is None:
            self.content = self.text
        return self


class CitationRef(BaseModel):
    """Stable reference to a retrieved chunk."""

    source_path: str
    source_namespace: str = "unknown"
    source_type: str = "markdown"
    chunk_id: str | None = None
    chunk_index: int | None = None
    note_title: str = ""
    section_header: str = ""
    repo_name: str | None = None
    symbol_type: str | None = None


class EvidenceFreshness(BaseModel):
    """Freshness metadata when the index exposes it."""

    status: Literal["fresh", "stale", "unknown"] = "unknown"
    indexed_at: str | None = None
    source_mtime: float | None = None
    source_hash: str | None = None
    stale_reason: str | None = None


class EvidenceProvenance(BaseModel):
    """How a piece of evidence was retrieved."""

    collection: str
    retrieval_backend: Literal["hybrid_vector_sparse", "vector"] = "vector"
    source_id: str | None = None
    source_name: str | None = None
    filters: dict[str, Any] = Field(default_factory=dict)


class EvidenceTruncation(BaseModel):
    """Whether the source was truncated before becoming evidence."""

    truncated: bool = False
    reason: str | None = None
    original_chunk_count: int | None = None


class RagEvidence(BaseModel):
    """Versioned evidence item returned by direct RAG queries."""

    contract_version: str = RAG_EVIDENCE_CONTRACT_VERSION
    citation: CitationRef
    score: float
    provenance: EvidenceProvenance
    freshness: EvidenceFreshness = Field(default_factory=EvidenceFreshness)
    truncation: EvidenceTruncation = Field(default_factory=EvidenceTruncation)


class RetrievalBudget(BaseModel):
    """Budget inputs that shaped direct retrieval."""

    candidate_top_k: int
    token_budget: int | None = None


class RetrievalTrace(BaseModel):
    """Versioned direct retrieval trace for API consumers."""

    contract_version: str = RETRIEVAL_TRACE_CONTRACT_VERSION
    collection: str
    top_k: int
    min_score: float
    results_count: int
    results_after_filter: int
    miss_reasons: list[str] = Field(default_factory=list)
    source_namespaces: list[str] = Field(default_factory=list)
    budget: RetrievalBudget
    truncated: bool = False


class QueryResponse(BaseModel):
    results: list[ChunkResult]
    query: str
    elapsed_ms: float
    evidence_contract_version: str = RAG_EVIDENCE_CONTRACT_VERSION
    evidence: list[RagEvidence] = Field(default_factory=list)
    retrieval_trace: RetrievalTrace | None = None
    trace: dict[str, Any] | None = None


class StatsResponse(BaseModel):
    total_chunks: int
    collection_name: str
    data_path: str
    # Coleções adicionais
    code_chunks: int = 0
    code_collection_name: str = ""


class RepoInfo(BaseModel):
    name: str
    path: str
    exists: bool
    graph_built: bool
    graph_path: str | None = None
    report_path: str | None = None
    node_count: int | None = None
    edge_count: int | None = None
    code_chunks: int = 0


class ReposResponse(BaseModel):
    repos: list[RepoInfo]
    graphify_enabled: bool
    graphify_backend: str


class GraphQueryRequest(BaseModel):
    query: str = Field(..., description="Query em linguagem natural para o grafo", min_length=1, max_length=10000)


class GraphNeighborsResponse(BaseModel):
    node: str
    repo: str
    neighbors: list[dict[str, Any]]


class CagPackItem(BaseModel):
    pack_type: str
    scope: str
    content: str
    tokens: int = 0
    fresh: bool = True


class CagPacksResponse(BaseModel):
    packs: list[CagPackItem]
    total_tokens: int
    total_packs: int


class CagPackDetail(BaseModel):
    pack_type: str
    scope: str
    content: str
    tokens: int = 0
    fresh: bool = True
    age_seconds: float | None = None
    expires_at: str | None = None
    created_at: str | None = None
    source_hash: str | None = None
    config_version: str | None = None
    stale_reason: str | None = None


class CagExplainRequest(BaseModel):
    intent: str | None = Field(default=None, description="Intent: local | code | system | graph")
    query: str | None = Field(default=None, description="Query livre (heurística de intent quando intent vazio)")
    budget: int = Field(default=2000, ge=0, le=20000, description="Token budget para selecção de packs")


class CagExplainItem(BaseModel):
    pack_type: str
    selected: bool
    reason: str
    available: bool = False
    fresh: bool = False
    tokens: int = 0
    age_seconds: float | None = None


class CagExplainResponse(BaseModel):
    intent: str
    budget: int
    selected_packs: list[str]
    total_tokens: int
    items: list[CagExplainItem]


class GraphContextRequest(BaseModel):
    query: str = Field(..., description="Query em linguagem natural", min_length=1, max_length=10000)
    repos: list[str] | None = Field(default=None, description="Repos a consultar (None = todos)")
    max_nodes: int = Field(default=20, ge=1, le=100)
    include_summaries: bool = Field(default=True, description="Incluir community summaries")


class GraphContextItem(BaseModel):
    repo: str
    title: str | None = None
    summary: str = ""
    nodes: list[dict[str, Any]]
    edges: list[dict[str, Any]] = []
    summaries: list[str] = []
    god_nodes: list[str] = []


class GraphContextResponse(BaseModel):
    results: list[GraphContextItem]
    total_nodes: int
    elapsed_ms: float


class IndexingStatusResponse(BaseModel):
    status: str = Field(description="idle | running | error")
    last_run_at: str | None = None
    last_run_status: str | None = None
    files_indexed: int = 0
    chunks_tracked: int = 0
    chunks_embedded: int = 0
    config_version: str = ""


class RetrievalStatusResponse(BaseModel):
    summary: dict[str, Any]
    recent: list[dict[str, Any]] = []
    bm25: list[dict[str, Any]] = []


class AdminReprocessRequest(BaseModel):
    target: str = Field(
        default="all",
        pattern="^(local|graph|cag|all)$",
        description="Reprocess target: local, graph, cag, or all",
    )
    force: bool = Field(default=False, description="Bypass incremental caches where supported")
    vault: str | None = Field(default=None, description="Optional vault name for local reindex")


class AdminJobResponse(BaseModel):
    job_id: str
    status: str
    target: str
    force: bool = False
    status_url: str
    message: str = ""


class AdminJobStatusResponse(BaseModel):
    job_id: str
    status: str
    target: str
    force: bool = False
    started_at: float | None = None
    finished_at: float | None = None
    error: str | None = None
    result: dict[str, Any] = Field(default_factory=dict)


class BatchQueryItem(BaseModel):
    query: str = Field(..., min_length=1, max_length=10000)
    collection: str = Field(default="obsidian_vault", description="Collection to query")
    top_k: int = Field(default=5, ge=1, le=20)
    min_score: float = Field(default=0.0, ge=0.0, le=1.0)


class BatchQueryRequest(BaseModel):
    queries: list[BatchQueryItem] = Field(..., min_length=1, max_length=10)


class BatchQueryResultItem(BaseModel):
    query: str
    results: list[ChunkResult]


class BatchQueryResponse(BaseModel):
    results: list[BatchQueryResultItem]
    elapsed_ms: float


class ChatMessage(BaseModel):
    role: str = Field(..., max_length=20)
    content: str = Field(..., max_length=50000)


class ChatContextPackage(BaseModel):
    contract_version: str = Field(default="context-package-v1", max_length=64)
    phase: str | None = Field(default=None, max_length=80)
    mode: str | None = Field(default=None, max_length=40)
    decision: str | None = Field(default=None, max_length=40)
    context_pressure: float | None = Field(default=None, ge=0.0)
    prompt_tokens_estimate: int | None = Field(default=None, ge=0)
    reserved_response_tokens: int | None = Field(default=None, ge=0)
    sources: list[str] = Field(default_factory=list, max_length=50)
    metadata: dict[str, Any] = Field(default_factory=dict)


def _default_chat_model() -> str:
    from registry import get_default_chat_model
    return get_default_chat_model()


class ChatRequest(BaseModel):
    model: str = Field(default_factory=_default_chat_model, max_length=100)
    messages: list[ChatMessage] = Field(..., max_length=200)
    stream: bool = True
    context_mode: str | None = Field(default=None, description="Override do context_mode: auto|rag_only|graph_only|both|none", max_length=20)
    agentic: bool = Field(default=False, description="True only for orchestrated agentic flows.")
    context_package: ChatContextPackage | None = Field(
        default=None,
        description="Required for agentic flows; manual/debug chat must keep agentic=false.",
    )

    @model_validator(mode="after")
    def _agentic_requires_context_package(self) -> "ChatRequest":
        if self.agentic and self.context_package is None:
            raise ValueError("agentic RAG /chat requires context_package")
        return self
