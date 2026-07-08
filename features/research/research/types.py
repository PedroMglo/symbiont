"""Data types for the Research feature."""

from __future__ import annotations

import enum
from typing import Any

from pydantic import BaseModel, Field
from sharedai.servicekit.contracts import CapabilitiesResponse as ServiceCapabilitiesResponse
from sharedai.servicekit.contracts import HealthResponse as ServiceHealthResponse

JsonScalar = str | int | float | bool | None


class SearchStatus(str, enum.Enum):
    OK = "ok"
    DEGRADED = "degraded"
    NO_RESULTS = "no_results"
    SKIPPED = "skipped"
    SERVICE_UNAVAILABLE = "unavailable"
    TIMEOUT = "timeout"
    CIRCUIT_OPEN = "circuit_open"
    AUTH_ERROR = "auth_error"


class ResearchEvidenceBundle(BaseModel):
    source: str
    source_type: str
    content: str
    citation_ref: str
    score: float = 0.0
    retrieval_mode: str = "unknown"
    token_cost: int = 0
    freshness: str = "unknown"
    limits: dict[str, JsonScalar] = Field(default_factory=dict)
    source_id: str = ""
    path: str = ""
    timestamp: str = ""
    metadata: dict[str, JsonScalar] = Field(default_factory=dict)


class SourceStatus(BaseModel):
    source: str
    source_type: str
    status: SearchStatus = SearchStatus.OK
    result_count: int = 0
    token_cost: int = 0
    limits: dict[str, JsonScalar] = Field(default_factory=dict)
    reason: str = ""


class ResearchQueryPlan(BaseModel):
    requested_intent: str = "general"
    normalized_intent: str = "general"
    source_namespace: str = ""
    source_scoped: bool = False
    include_code: bool = True
    include_code_reason: str = ""
    include_cag: bool = True
    include_cag_reason: str = ""
    budget_tokens: int = 2000
    budget_reason: str = ""
    pack_selection: str = "general"
    pack_selection_reason: str = ""
    notes_top_k: int = 5
    code_top_k: int = 5
    cag_budget_tokens: int = 2000
    notes_payload: dict[str, JsonScalar] = Field(default_factory=dict)
    code_payload: dict[str, JsonScalar] = Field(default_factory=dict)
    cag_payload: dict[str, JsonScalar] = Field(default_factory=dict)
    retrieval_modes: list[str] = Field(default_factory=list)
    namespace: str = ""
    warnings: list[str] = Field(default_factory=list)


class ResearchCitation(BaseModel):
    ref: str
    citation_ref: str
    source: str
    source_type: str
    retrieval_mode: str = "unknown"
    score: float = 0.0
    freshness: str = "unknown"
    path: str = ""
    source_id: str = ""
    metadata: dict[str, JsonScalar] = Field(default_factory=dict)


class ReasoningEvidenceContext(BaseModel):
    content: str = ""
    answerability: str = "insufficient"
    answerability_reason: str = ""
    citations: list[ResearchCitation] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)
    bounded_tokens: int = 0
    max_tokens: int = 0
    sources_used: list[str] = Field(default_factory=list)


class ResearchRetrievalTrace(BaseModel):
    trace_ref: str
    source: str
    source_type: str
    retrieval_mode: str = "unknown"
    status: SearchStatus = SearchStatus.OK
    result_count: int = 0
    token_cost: int = 0
    searched: bool = True
    request: dict[str, JsonScalar] = Field(default_factory=dict)
    limits: dict[str, JsonScalar] = Field(default_factory=dict)
    miss_reasons: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)


class ResearchMissReview(BaseModel):
    should_record: bool = False
    event_type: str = "rag.miss"
    producer: str = "research"
    severity: str = "low"
    reason: str = ""
    evidence_refs: list[str] = Field(default_factory=list)
    payload: dict[str, Any] = Field(default_factory=dict)


class SearchResult(BaseModel):
    source: str
    content: str
    score: float = 0.0
    pack_type: str = ""
    source_type: str = "unknown"
    citation_ref: str = ""
    retrieval_mode: str = "unknown"
    token_cost: int = 0
    freshness: str = "unknown"
    limits: dict[str, JsonScalar] = Field(default_factory=dict)
    source_id: str = ""
    path: str = ""
    timestamp: str = ""
    metadata: dict[str, JsonScalar] = Field(default_factory=dict)

    def to_evidence(self) -> ResearchEvidenceBundle:
        return ResearchEvidenceBundle(
            source=self.source,
            source_type=self.source_type,
            content=self.content,
            citation_ref=self.citation_ref or self.source,
            score=self.score,
            retrieval_mode=self.retrieval_mode,
            token_cost=self.token_cost,
            freshness=self.freshness,
            limits=self.limits,
            source_id=self.source_id,
            path=self.path,
            timestamp=self.timestamp,
            metadata=self.metadata,
        )


class SearchRequest(BaseModel):
    query: str
    budget_tokens: int = 2000
    include_code: bool = True
    intent: str = "general"
    namespace: str = ""
    source_paths: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SearchResponse(BaseModel):
    content: str = ""
    results: list[SearchResult] = Field(default_factory=list)
    total_tokens: int = 0
    status: SearchStatus = SearchStatus.OK
    source_statuses: list[SourceStatus] = Field(default_factory=list)
    degraded: bool = False
    evidence_bundle: list[ResearchEvidenceBundle] = Field(default_factory=list)
    limits: dict[str, JsonScalar] = Field(default_factory=dict)
    query_plan: ResearchQueryPlan | None = None
    reasoning_context: ReasoningEvidenceContext | None = None
    retrieval_traces: list[ResearchRetrievalTrace] = Field(default_factory=list)
    miss_review: ResearchMissReview | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ResearchSourcePrepareItem(BaseModel):
    path: str
    name: str | None = None
    source_type: str = "auto"
    exclude_patterns: list[str] = Field(default_factory=list)


class ResearchSourcePrepareRequest(BaseModel):
    sources: list[ResearchSourcePrepareItem] = Field(default_factory=list)
    target: str = "sources"
    force: bool = False
    wait_seconds: float = 0.0
    poll_interval_seconds: float = 2.0


class ResearchSourcePrepareResponse(BaseModel):
    status: str = "unknown"
    job_id: str = ""
    status_url: str = ""
    target: str = "sources"
    force: bool = False
    sources: list[dict[str, Any]] = Field(default_factory=list)
    result: dict[str, Any] = Field(default_factory=dict)
    error: str = ""


class CAGRequest(BaseModel):
    intent: str = "general"
    budget_tokens: int = 2000


class HealthResponse(ServiceHealthResponse):
    rag_reachable: bool = False


class CapabilitiesResponse(ServiceCapabilitiesResponse):
    name: str = "research"
    capabilities: list[str] = Field(
        default_factory=lambda: ["semantic_search", "knowledge_retrieval", "rag", "cag", "source_preparation"]
    )
    description: str = (
        "Retrieves relevant information from personal notes, documents, "
        "and pre-computed knowledge packs using semantic vector search."
    )
