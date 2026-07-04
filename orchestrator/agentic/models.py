"""Agentic runtime contracts."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class TaskStatus(str, Enum):
    QUEUED = "queued"
    PLANNING = "planning"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    RECOVERING = "recovering"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    DENY = "deny"


class PolicyDecisionKind(str, Enum):
    ALLOW = "allow"
    WOULD_REQUIRE_APPROVAL = "would_require_approval"
    REQUIRE_APPROVAL = "require_approval"
    DRY_RUN_ONLY = "dry_run_only"
    DENY = "deny"


@dataclass(frozen=True)
class AgenticTask:
    id: str
    goal: str
    mode: str
    status: str
    priority: str
    session_id: str | None
    user_id_hash: str | None
    trace_id: str
    source: str
    created_at: float
    updated_at: float
    budget: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PolicyDecision:
    action: str
    decision: str
    risk_level: str
    reason: str
    requires_approval: bool = False
    dry_run_required: bool = False
    evidence_required: bool = False
    lease_required: bool = False
    max_risk: str = "high"
    mode: str = "supervised"
    policy_mode: str = "audit"
    backend: str = "python_registry"
    shadow_backend: str = ""
    shadow_decision: str = ""
    shadow_parity: bool | None = None
    shadow_reason: str = ""

    @property
    def should_block(self) -> bool:
        if self.policy_mode != "enforce":
            return False
        return self.decision in {
            PolicyDecisionKind.DENY.value,
            PolicyDecisionKind.REQUIRE_APPROVAL.value,
            PolicyDecisionKind.DRY_RUN_ONLY.value,
        }

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
