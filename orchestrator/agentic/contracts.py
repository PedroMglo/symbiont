"""Structured contracts for the agentic control plane.

These models are the typed boundary between probabilistic agents and the
deterministic runtime. Agents propose decisions; the runtime validates,
reduces state, applies policy, and executes tools.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

CONTRACT_VERSION = "agentic-control-v1"

RiskLevel = Literal["low", "medium", "high", "deny"]
ExpectedEffect = Literal["read_only", "write", "destructive", "external"]
DecisionStatus = Literal["needs_action", "waiting_for_user", "blocked", "complete", "failed", "no_action"]
StateStatus = Literal["planning", "executing", "waiting_for_user", "blocked", "complete", "failed"]
MessageKind = Literal["message", "question", "answer", "critique", "validation", "consensus"]
ValidationVoteKind = Literal["confirm", "contest", "needs_evidence", "abstain"]
EventSeverity = Literal["debug", "info", "low", "medium", "high", "critical"]
MemoryKind = Literal["working", "episodic", "semantic_ref", "procedural_ref", "preference_ref"]
MemorySensitivity = Literal["normal", "sensitive", "secret"]
MemoryRedactionStatus = Literal["not_required", "redacted", "redacted_only"]
ActionResultStatus = Literal[
    "pending",
    "running",
    "completed",
    "failed",
    "blocked",
    "waiting_approval",
    "denied",
    "skipped",
]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RawOutputRef(_StrictModel):
    """Reference to redacted raw agent output kept for audit, not control flow."""

    ref_id: str = Field(..., min_length=1, max_length=160)
    sha256: str = Field(..., min_length=64, max_length=64)
    preview: str = Field("", max_length=4000)
    redacted: bool = True
    artifact_ref: str | None = Field(None, max_length=1000)
    size_bytes: int = Field(0, ge=0)


class ShellCommandAction(_StrictModel):
    type: Literal["shell_command"]
    action_id: str = Field(..., min_length=1, max_length=160)
    command: str = Field(..., min_length=1, max_length=32000)
    cwd: str | None = Field(None, max_length=500)
    expected_effect: ExpectedEffect = "read_only"
    reason: str = Field("", max_length=2000)
    requires_confirmation: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentInvokeAction(_StrictModel):
    type: Literal["agent_invoke"]
    action_id: str = Field(..., min_length=1, max_length=160)
    agent_name: str = Field(..., min_length=1, max_length=200)
    query: str = Field(..., min_length=1, max_length=16000)
    context: dict[str, Any] = Field(default_factory=dict)
    reason: str = Field("", max_length=2000)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RagQueryAction(_StrictModel):
    type: Literal["rag_query"]
    action_id: str = Field(..., min_length=1, max_length=160)
    query: str = Field(..., min_length=1, max_length=16000)
    namespace: str | None = Field(None, max_length=200)
    reason: str = Field("", max_length=2000)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ApiCallAction(_StrictModel):
    type: Literal["api_call"]
    action_id: str = Field(..., min_length=1, max_length=160)
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
    endpoint: str = Field(..., min_length=1, max_length=1000)
    payload: dict[str, Any] = Field(default_factory=dict)
    expected_effect: ExpectedEffect = "external"
    reason: str = Field("", max_length=2000)
    metadata: dict[str, Any] = Field(default_factory=dict)


class NoopAction(_StrictModel):
    type: Literal["noop"]
    action_id: str = Field(..., min_length=1, max_length=160)
    reason: str = Field("", max_length=2000)
    metadata: dict[str, Any] = Field(default_factory=dict)


AgentAction = Annotated[
    ShellCommandAction | AgentInvokeAction | RagQueryAction | ApiCallAction | NoopAction,
    Field(discriminator="type"),
]


class AgentObservation(_StrictModel):
    observation_id: str = Field(..., min_length=1, max_length=160)
    source: str = Field(..., min_length=1, max_length=200)
    content: str = Field(..., min_length=1, max_length=8000)
    metadata: dict[str, Any] = Field(default_factory=dict)


class CapabilityActionMetadata(_StrictModel):
    """Minimum capability metadata used by generic action adapters."""

    capability_id: str = Field(..., min_length=1, max_length=200)
    owner: str = Field(..., min_length=1, max_length=200)
    endpoint: str = Field(..., min_length=1, max_length=1000)
    policy_action: str = Field(..., min_length=1, max_length=200)
    transport: dict[str, Any] = Field(default_factory=dict)
    risk_level: RiskLevel = "medium"
    supported_action_types: list[str] = Field(default_factory=list)
    resource_profile: dict[str, Any] = Field(default_factory=dict)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    evidence_types: list[str] = Field(default_factory=list)
    writes_allowed: bool = False
    idempotency_policy: str = Field("required_for_writes", max_length=200)
    dry_run_supported: bool = False
    rollback_supported: bool = False
    events_published: list[str] = Field(default_factory=list)
    risk_review_criteria: list[str] = Field(default_factory=list)
    round_dependencies: list[str] = Field(default_factory=list)
    timeout_seconds: float | None = Field(None, gt=0)


class ParallelAgentSpec(_StrictModel):
    agent_name: str = Field(..., min_length=1, max_length=200)
    role: str = Field(..., min_length=1, max_length=120)
    query: str = Field(..., min_length=1, max_length=16000)
    context: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: float | None = Field(None, gt=0)
    budget_tokens: int | None = Field(None, gt=0)
    required: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgenticParallelPlan(_StrictModel):
    schema_version: Literal["agentic-control-v1"] = CONTRACT_VERSION
    plan_id: str = Field(..., min_length=1, max_length=160)
    task_id: str = Field(..., min_length=1, max_length=160)
    trace_id: str = Field(..., min_length=1, max_length=160)
    goal: str = Field(..., min_length=1, max_length=16000)
    participants: list[ParallelAgentSpec] = Field(default_factory=list)
    max_parallel: int = Field(2, ge=1, le=12)
    resource_profile: dict[str, Any] = Field(default_factory=dict)
    fallback_policy: Literal["sequential", "partial", "fail"] = "sequential"
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgenticParallelRound(_StrictModel):
    schema_version: Literal["agentic-control-v1"] = CONTRACT_VERSION
    round_id: str = Field(..., min_length=1, max_length=160)
    plan_id: str = Field(..., min_length=1, max_length=160)
    task_id: str = Field(..., min_length=1, max_length=160)
    trace_id: str = Field(..., min_length=1, max_length=160)
    status: Literal["planned", "running", "completed", "degraded", "failed"]
    participants: list[ParallelAgentSpec] = Field(default_factory=list)
    observations: list[AgentObservation] = Field(default_factory=list)
    lease_decisions: list[dict[str, Any]] = Field(default_factory=list)
    consensus: dict[str, Any] = Field(default_factory=dict)
    degraded: bool = False
    started_at: float | None = None
    finished_at: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentMessage(_StrictModel):
    schema_version: Literal["agentic-control-v1"] = CONTRACT_VERSION
    message_id: str = Field(..., min_length=1, max_length=160)
    task_id: str = Field(..., min_length=1, max_length=160)
    trace_id: str = Field(..., min_length=1, max_length=160)
    kind: MessageKind
    sender: str = Field(..., min_length=1, max_length=200)
    recipient: str | None = Field(None, max_length=200)
    content: str = Field(..., min_length=1, max_length=8000)
    round_id: str | None = Field(None, max_length=160)
    evidence_refs: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentQuestion(_StrictModel):
    schema_version: Literal["agentic-control-v1"] = CONTRACT_VERSION
    question_id: str = Field(..., min_length=1, max_length=160)
    task_id: str = Field(..., min_length=1, max_length=160)
    trace_id: str = Field(..., min_length=1, max_length=160)
    from_agent: str = Field(..., min_length=1, max_length=200)
    to_agent: str = Field(..., min_length=1, max_length=200)
    question: str = Field(..., min_length=1, max_length=4000)
    round_id: str | None = Field(None, max_length=160)
    evidence_refs: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentAnswer(_StrictModel):
    schema_version: Literal["agentic-control-v1"] = CONTRACT_VERSION
    answer_id: str = Field(..., min_length=1, max_length=160)
    question_id: str = Field(..., min_length=1, max_length=160)
    task_id: str = Field(..., min_length=1, max_length=160)
    trace_id: str = Field(..., min_length=1, max_length=160)
    from_agent: str = Field(..., min_length=1, max_length=200)
    answer: str = Field(..., min_length=1, max_length=8000)
    evidence_refs: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ValidationVote(_StrictModel):
    schema_version: Literal["agentic-control-v1"] = CONTRACT_VERSION
    vote_id: str = Field(..., min_length=1, max_length=160)
    task_id: str = Field(..., min_length=1, max_length=160)
    trace_id: str = Field(..., min_length=1, max_length=160)
    voter: str = Field(..., min_length=1, max_length=200)
    target_ref: str = Field(..., min_length=1, max_length=300)
    vote: ValidationVoteKind
    rationale: str = Field("", max_length=4000)
    evidence_refs: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class CritiqueDecision(_StrictModel):
    schema_version: Literal["agentic-control-v1"] = CONTRACT_VERSION
    critique_id: str = Field(..., min_length=1, max_length=160)
    task_id: str = Field(..., min_length=1, max_length=160)
    trace_id: str = Field(..., min_length=1, max_length=160)
    critic: str = Field(..., min_length=1, max_length=200)
    target_ref: str = Field(..., min_length=1, max_length=300)
    findings: list[str] = Field(default_factory=list)
    required_revisions: list[str] = Field(default_factory=list)
    confidence: float = Field(..., ge=0.0, le=1.0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ConsensusDecision(_StrictModel):
    schema_version: Literal["agentic-control-v1"] = CONTRACT_VERSION
    consensus_id: str = Field(..., min_length=1, max_length=160)
    task_id: str = Field(..., min_length=1, max_length=160)
    trace_id: str = Field(..., min_length=1, max_length=160)
    round_id: str | None = Field(None, max_length=160)
    status: Literal["accepted", "contested", "needs_more_evidence", "failed"]
    summary: str = Field(..., min_length=1, max_length=8000)
    agreed_facts: list[str] = Field(default_factory=list)
    contested_facts: list[str] = Field(default_factory=list)
    validation_votes: list[ValidationVote] = Field(default_factory=list)
    confidence: float = Field(..., ge=0.0, le=1.0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AiLocalEvent(_StrictModel):
    schema_version: Literal["ai-local-event-v1"] = "ai-local-event-v1"
    event_id: str = Field(..., min_length=1, max_length=160)
    producer: str = Field(..., min_length=1, max_length=200)
    type: str = Field(..., min_length=1, max_length=200)
    severity: EventSeverity = "info"
    trace_id: str | None = Field(None, max_length=160)
    task_id: str | None = Field(None, max_length=160)
    payload: dict[str, Any] = Field(default_factory=dict)
    evidence_ref: str | None = Field(None, max_length=1000)
    created_at: float = Field(..., ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgenticMemory(_StrictModel):
    schema_version: Literal["agentic-control-v1"] = CONTRACT_VERSION
    memory_id: str = Field(..., min_length=1, max_length=160)
    task_id: str | None = Field(None, max_length=160)
    trace_id: str | None = Field(None, max_length=160)
    kind: MemoryKind
    owner: str = Field("orchestrator/agentic", min_length=1, max_length=200)
    source: str = Field(..., min_length=1, max_length=200)
    content: str = Field(..., min_length=1, max_length=8000)
    evidence_refs: list[str] = Field(default_factory=list)
    expires_at: float | None = Field(None, ge=0)
    sensitivity: MemorySensitivity = "normal"
    redaction_status: MemoryRedactionStatus = "not_required"
    storage_artifact_ref: str | None = Field(None, max_length=1000)
    semantic_ref: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgenticMemoryQuery(_StrictModel):
    schema_version: Literal["agentic-control-v1"] = CONTRACT_VERSION
    query_id: str = Field(..., min_length=1, max_length=160)
    task_id: str | None = Field(None, max_length=160)
    trace_id: str | None = Field(None, max_length=160)
    query: str = Field("", max_length=4000)
    kinds: list[MemoryKind] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    metadata_filter: dict[str, Any] = Field(default_factory=dict)
    include_expired: bool = False
    limit: int = Field(12, ge=1, le=100)
    min_score: float = Field(0.1, ge=0.0, le=10.0)


class RetrievedAgenticMemory(_StrictModel):
    schema_version: Literal["agentic-control-v1"] = CONTRACT_VERSION
    memory: AgenticMemory
    score: float = Field(..., ge=0.0)
    reasons: list[str] = Field(default_factory=list)
    expired: bool = False


class ActionResult(_StrictModel):
    action_id: str = Field(..., min_length=1, max_length=160)
    action_type: str = Field(..., min_length=1, max_length=80)
    status: ActionResultStatus
    observation: str = Field("", max_length=8000)
    result: dict[str, Any] = Field(default_factory=dict)
    error: dict[str, Any] | None = None
    policy_decision: dict[str, Any] | None = None
    raw_output_ref: RawOutputRef | None = None


class AgentDecision(_StrictModel):
    """Validated output from an LLM agent."""

    schema_version: Literal["agentic-control-v1"] = CONTRACT_VERSION
    task_id: str = Field(..., min_length=1, max_length=160)
    trace_id: str = Field(..., min_length=1, max_length=160)
    input_state_hash: str = Field(..., min_length=64, max_length=64)
    status: DecisionStatus
    confidence: float = Field(..., ge=0.0, le=1.0)
    new_facts: list[str] = Field(default_factory=list)
    proposed_actions: list[AgentAction] = Field(default_factory=list)
    questions_for_user: list[str] = Field(default_factory=list)
    reasoning_summary: str = Field("", max_length=4000)
    raw_output_ref: RawOutputRef | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentState(_StrictModel):
    """Projection of a task's control-plane state.

    The event ledger is authoritative; this model is a deterministic projection
    that can be stored as a snapshot and rebuilt by replay.
    """

    schema_version: Literal["agentic-control-v1"] = CONTRACT_VERSION
    task_id: str = Field(..., min_length=1, max_length=160)
    trace_id: str = Field(..., min_length=1, max_length=160)
    goal: str = Field(..., min_length=1, max_length=16000)
    known_facts: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    constraints: dict[str, Any] = Field(default_factory=dict)
    completed_actions: list[ActionResult] = Field(default_factory=list)
    pending_actions: list[AgentAction] = Field(default_factory=list)
    observations: list[AgentObservation] = Field(default_factory=list)
    risk_level: RiskLevel = "low"
    status: StateStatus = "planning"
    metadata: dict[str, Any] = Field(default_factory=dict)
