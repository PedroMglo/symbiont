"""Pydantic models and domain types for extrator."""

from __future__ import annotations

import enum
from typing import Any

from sharedai.servicekit.contracts import CapabilitiesResponse as ServiceCapabilitiesResponse
from sharedai.servicekit.contracts import HealthResponse as ServiceHealthResponse
from pydantic import BaseModel, Field

DOCUMENT_EVIDENCE_CONTRACT_VERSION = "document_evidence.v1"
DOCUMENT_DIAGNOSTIC_CONTRACT_VERSION = "document_diagnostic.v1"
RAG_BUNDLE_CONTRACT_VERSION = "rag_bundle.v1"
SANDBOX_PREPARATION_PLAN_CONTRACT_VERSION = "sandbox_preparation_plan.v1"


class JobStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobKind(str, enum.Enum):
    EXTRACTION = "extraction"
    CONVERSION = "conversion"
    CLEANUP = "cleanup"


class EmbeddingPolicy(str, enum.Enum):
    EMBED = "embed"
    SKIP = "skip"
    SUMMARIZE_THEN_EMBED = "summarize_then_embed"
    TABLE_SUMMARY_ONLY = "table_summary_only"
    CODE_SYMBOL_SUMMARY = "code_symbol_summary"
    OCR_REVIEW_NEEDED = "ocr_review_needed"


class QueryAction(str, enum.Enum):
    CREATED_JOB = "created_job"
    REUSED_RESULT = "reused_result"
    BLOCKED = "blocked"
    NO_ACTION = "no_action"


class DocumentWorkflowAction(str, enum.Enum):
    EXTRACT = "extract"
    CONVERT = "convert"
    SANDBOX_REQUIRED = "sandbox_required"
    BLOCKED = "blocked"


class EvidenceTruncation(BaseModel):
    truncated: bool = False
    reason: str | None = None
    original_length: int | None = None
    retained_length: int | None = None


class EvidenceSecurityDecision(BaseModel):
    scope: str
    decision: str
    reason: str | None = None
    reference: str | None = None


class ParserAttemptEvidence(BaseModel):
    parser: str
    status: str
    reason: str | None = None
    confidence: float | None = None
    cost: int | None = None
    tables: bool = False
    images: bool = False
    layout: bool = False
    warnings: list[str] = Field(default_factory=list)


class ParserSelectionEvidence(BaseModel):
    source_type: str
    extension: str
    candidate_order: list[str] = Field(default_factory=list)
    selected_parser: str | None = None
    selected_index: int | None = None
    fallback_used: bool = False
    attempts: list[ParserAttemptEvidence] = Field(default_factory=list)


class ParserQualityMetrics(BaseModel):
    extraction_loss: float | None = None
    extraction_loss_reason: str | None = None
    table_fidelity: float | None = None
    table_fidelity_reason: str | None = None
    chunk_stability: float | None = None
    chunk_stability_hash: str | None = None
    chunk_count: int = 0


class ChunkEvidence(BaseModel):
    contract_version: str = DOCUMENT_EVIDENCE_CONTRACT_VERSION
    chunk_id: str
    doc_id: str
    source_hash: str
    content_hash: str
    parser_id: str
    parser_version: str
    warnings: list[str] = Field(default_factory=list)
    truncation: EvidenceTruncation = Field(default_factory=EvidenceTruncation)
    security_decisions: list[EvidenceSecurityDecision] = Field(default_factory=list)
    token_count: int
    language: str
    heading_path: list[str] = Field(default_factory=list)
    page_start: int | None = None
    page_end: int | None = None
    embedding_policy: EmbeddingPolicy


class TableEvidence(BaseModel):
    contract_version: str = DOCUMENT_EVIDENCE_CONTRACT_VERSION
    table_id: str
    doc_id: str
    source_hash: str
    parser_id: str
    parser_version: str
    warnings: list[str] = Field(default_factory=list)
    truncation: EvidenceTruncation = Field(default_factory=EvidenceTruncation)
    security_decisions: list[EvidenceSecurityDecision] = Field(default_factory=list)
    rows: int
    columns: int
    output_path: str
    summary: str


class ConversionEvidence(BaseModel):
    contract_version: str = DOCUMENT_EVIDENCE_CONTRACT_VERSION
    conversion_id: str | None = None
    job_id: str
    input_path: str
    source_hash: str | None = None
    output_format: str
    output_path: str
    status: str
    warnings: list[str] = Field(default_factory=list)
    security_decisions: list[EvidenceSecurityDecision] = Field(default_factory=list)


class DocumentEvidence(BaseModel):
    contract_version: str = DOCUMENT_EVIDENCE_CONTRACT_VERSION
    doc_id: str
    source_path: str
    source_type: str
    mime_type: str
    source_hash: str
    parser_id: str
    parser_version: str
    parser_confidence: float | None = None
    parser_selection: ParserSelectionEvidence | None = None
    quality_metrics: ParserQualityMetrics | None = None
    warnings: list[str] = Field(default_factory=list)
    truncation: EvidenceTruncation = Field(default_factory=EvidenceTruncation)
    security_decisions: list[EvidenceSecurityDecision] = Field(default_factory=list)
    output_paths: dict[str, str] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    chunks: list[ChunkEvidence] = Field(default_factory=list)
    tables: list[TableEvidence] = Field(default_factory=list)


class RagBundleArtifact(BaseModel):
    key: str
    role: str
    ref: str
    media_type: str
    sha256: str | None = None
    required_for_reprocess: bool = False


class RagBundleReprocessPlan(BaseModel):
    can_reprocess_without_original: bool
    reason: str
    required_artifacts: list[str] = Field(default_factory=list)
    forbidden_actions: list[str] = Field(default_factory=list)


class RagBundleChainOfCustody(BaseModel):
    source_hash: str
    evidence_ref: str | None = None
    original_ref: str | None = None
    custody_owner: str = "storage_guardian"
    verified_by: list[str] = Field(default_factory=list)


class RagBundleManifest(BaseModel):
    contract_version: str = RAG_BUNDLE_CONTRACT_VERSION
    producer: str = "extrator"
    consumer: str = "obsidian-rag"
    embedding_owner: str = "obsidian-rag"
    storage_owner: str = "storage_guardian"
    embeddings_included: bool = False
    doc_id: str
    source_path: str
    source_type: str
    source_hash: str
    parser_id: str
    parser_version: str
    chunk_count: int
    table_count: int
    graph_candidate_count: int
    artifacts: list[RagBundleArtifact] = Field(default_factory=list)
    chain_of_custody: RagBundleChainOfCustody
    reprocess: RagBundleReprocessPlan
    metadata: dict[str, Any] = Field(default_factory=dict)


class DocumentDiagnosticRequest(BaseModel):
    input_path: str
    recursive: bool = False
    force: bool = False
    conversion_format: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class DocumentSensitivityDiagnostic(BaseModel):
    level: str
    signals: list[str] = Field(default_factory=list)
    sample_scanned: bool = False


class DocumentLanguageDiagnostic(BaseModel):
    language: str
    confidence: float | None = None
    reason: str


class DocumentStructureDiagnostic(BaseModel):
    path_kind: str
    extension: str
    source_type: str
    mime_type: str
    size_bytes: int | None = None
    likely_tabular: bool = False
    likely_code: bool = False
    likely_multi_document: bool = False
    requires_parser: bool = True


class DocumentOcrDiagnostic(BaseModel):
    needed: bool | None = None
    enabled: bool
    reason: str


class DocumentCostEstimate(BaseModel):
    cost_tier: str
    estimated_bytes: int | None = None
    estimated_tokens: int | None = None
    estimated_items: int | None = None
    reason: str


class DocumentWorkflowRecommendation(BaseModel):
    action: DocumentWorkflowAction
    job_kind: JobKind | None = None
    policy_action: str = "document.extract"
    capability_id: str = "feature.extrator"
    requires_workspace_execution: bool = False
    reason: str
    targets: list[str] = Field(default_factory=list)
    output_format: str | None = None


class DocumentDiagnostic(BaseModel):
    contract_version: str = DOCUMENT_DIAGNOSTIC_CONTRACT_VERSION
    owner: str = "features/extrator"
    storage_owner: str = "storage_guardian"
    sandbox_owner: str = "workspace_execution"
    input_path: str
    status: str
    sensitivity: DocumentSensitivityDiagnostic
    language: DocumentLanguageDiagnostic
    structure: DocumentStructureDiagnostic
    ocr: DocumentOcrDiagnostic
    cost: DocumentCostEstimate
    workflow: DocumentWorkflowRecommendation
    warnings: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class CapabilityWorkflow(BaseModel):
    action: str
    capability: str
    endpoint: str
    method: str = "POST"
    policy_action: str
    requires_workspace_execution: bool = False
    description: str


class SandboxSourceOption(BaseModel):
    kind: str
    path: str | None = None
    requires: str | None = None
    copy_required: bool


class SandboxSessionPlan(BaseModel):
    execution_profile: str
    network: str
    real_host_writes: bool


class SandboxInputPlan(BaseModel):
    original_path: str
    container_path: str
    recursive: bool
    force: bool
    conversion_format: str | None = None


class SandboxPublishPlan(BaseModel):
    required: bool
    allowed_via: str


class SandboxPreparationPlan(BaseModel):
    contract_version: str = SANDBOX_PREPARATION_PLAN_CONTRACT_VERSION
    kind: str
    owner: str
    uses: str
    capability: str
    requires_orchestrator_execution: bool
    recommended: bool
    source_options: list[SandboxSourceOption]
    session: SandboxSessionPlan
    inputs: list[SandboxInputPlan]
    checks: list[str]
    publish: SandboxPublishPlan


class ExtractionPathRequest(BaseModel):
    input_path: str
    recursive: bool
    force: bool
    targets: list[str]
    metadata: dict[str, Any]


class ConversionPathRequest(BaseModel):
    input_path: str
    output_format: str
    force: bool
    preserve_layout: bool
    recursive: bool = False
    output_path: str | None = None
    output_paths: dict[str, str] = Field(default_factory=dict)
    metadata: dict[str, Any]


class ExtratorQueryRequest(BaseModel):
    query: str
    budget_tokens: int | None = None
    timeout_seconds: float | None = None
    wait_seconds: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ExtratorQueryResponse(BaseModel):
    content: str = ""
    source: str = "extrator"
    action: QueryAction = QueryAction.NO_ACTION
    token_estimate: int = 0
    success: bool = True
    latency_ms: float = 0.0
    metadata: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class ReprocessRequest(BaseModel):
    force: bool
    targets: list[str]
    metadata: dict[str, Any]


class CleanupRequest(BaseModel):
    older_than_hours: int
    include_failed: bool
    dry_run: bool


class JobCreateResponse(BaseModel):
    job_id: str
    status: JobStatus
    status_url: str


class JobStatusResponse(BaseModel):
    job_id: str
    kind: JobKind
    status: JobStatus
    created_at: str
    started_at: str | None
    completed_at: str | None
    error: str | None
    outputs: dict[str, str]
    summary: dict[str, Any]


class HealthResponse(ServiceHealthResponse):
    status: str
    service: str
    version: str
    manifest_reachable: bool


class CapabilitiesResponse(ServiceCapabilitiesResponse):
    name: str
    capabilities: list[str]
    description: str
    workflow_actions: list[CapabilityWorkflow] = Field(default_factory=list)
    contracts: dict[str, str] = Field(default_factory=dict)


class FormatsResponse(BaseModel):
    extract_input_extensions: list[str]
    conversion_pairs: list[str]
    output_formats: list[str]


class StatsResponse(BaseModel):
    jobs_total: int
    documents_total: int
    chunks_total: int
    tables_total: int
    conversions_total: int


class DocumentInfo(BaseModel):
    doc_id: str
    source_path: str
    source_type: str
    file_hash: str
    status: str
    output_paths: dict[str, str]
    metadata: dict[str, Any]


class ChunkPayload(BaseModel):
    chunk_id: str
    doc_id: str
    source_path: str
    source_type: str
    title: str
    section: str
    heading_path: list[str]
    page_start: int | None
    page_end: int | None
    text: str
    token_count: int
    language: str
    content_hash: str
    parser: str
    parser_version: str
    embedding_policy: EmbeddingPolicy
    text_ref: str


class TableInfo(BaseModel):
    table_id: str
    doc_id: str
    name: str
    rows: int
    columns: int
    output_path: str
    summary: str


class NormalizedDocument(BaseModel):
    doc_id: str
    source_path: str
    source_type: str
    mime_type: str
    file_hash: str
    title: str
    markdown: str
    metadata: dict[str, Any]
    tables: list[TableInfo] = Field(default_factory=list)
    parser: str
    parser_version: str


class GraphCandidate(BaseModel):
    doc_id: str
    chunk_id: str
    candidate_entities: list[str]
    candidate_relations: list[dict[str, Any]]
    source_path: str
    evidence_text: str
    confidence_hint: float
    extraction_method: str
