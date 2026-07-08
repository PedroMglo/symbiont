"""Deterministic repair-loop arbitration for material sessions.

The arbiter is policy code owned by the Material Execution Kernel. It does not
generate code, validate success, execute commands, or publish artifacts. Its job
is to make each repair attempt explicit, bounded, and different when previous
evidence shows no progress.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from typing import Any, Literal

from material_execution_kernel.types import MaterialIssue, PatchRejectionEvidence


RepairStrategy = Literal["patch", "replacement", "patch_set", "plan_repair", "regeneration", "failed_closed"]
ProgressClassification = Literal["first_attempt", "changed_strategy", "repeated", "no_progress", "regressed"]
FailureClass = Literal[
    "schema_or_payload_hallucination",
    "proposal_contract_mismatch",
    "sandbox_apply_failure",
    "noop",
    "governed_mode_unavailable",
    "unknown",
]


@dataclass(frozen=True)
class RepairArbiterDecision:
    strategy: RepairStrategy
    issue_fingerprint: str
    target_path: str | None
    target_sha256: str | None
    obligation_ids: list[str] = field(default_factory=list)
    failure_class: FailureClass | None = None
    progress: ProgressClassification = "first_attempt"
    previous_attempt_count: int = 0
    repeated_failure_count: int = 0
    request_critic_advisory: bool = False
    reason: str = ""
    allowed_repair_proposals: list[str] = field(default_factory=list)

    def model_dump(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "issue_fingerprint": self.issue_fingerprint,
            "target_path": self.target_path,
            "target_sha256": self.target_sha256,
            "obligation_ids": self.obligation_ids,
            "failure_class": self.failure_class,
            "progress": self.progress,
            "previous_attempt_count": self.previous_attempt_count,
            "repeated_failure_count": self.repeated_failure_count,
            "request_critic_advisory": self.request_critic_advisory,
            "reason": self.reason,
            "allowed_repair_proposals": self.allowed_repair_proposals,
        }


@dataclass(frozen=True)
class RepairRejectionDecision:
    failure_class: FailureClass
    retry_limit: int
    retryable: bool
    repeated_failure_count: int
    request_critic_advisory: bool
    progress: ProgressClassification
    reason: str

    def model_dump(self) -> dict[str, Any]:
        return {
            "failure_class": self.failure_class,
            "retry_limit": self.retry_limit,
            "retryable": self.retryable,
            "repeated_failure_count": self.repeated_failure_count,
            "request_critic_advisory": self.request_critic_advisory,
            "progress": self.progress,
            "reason": self.reason,
        }


def arbitrate_repair_attempt(
    issue: MaterialIssue,
    *,
    target_sha256: str | None,
    related_target_count: int,
    max_repair_rounds: int,
) -> RepairArbiterDecision:
    previous = list(issue.patch_rejections)
    obligation_ids = _obligation_ids(issue)
    fingerprint = issue_fingerprint(issue, target_sha256=target_sha256, obligation_ids=obligation_ids)
    last_rejection = previous[-1] if previous else None
    failure_class = classify_rejection(last_rejection.reason, last_rejection.diagnostics) if last_rejection else None
    repeated_count = _repeated_failure_count(previous)
    strategy = _strategy_for_issue(
        issue,
        failure_class=failure_class,
        repeated_failure_count=repeated_count,
        related_target_count=related_target_count,
    )
    if len(previous) >= max_repair_rounds:
        strategy = "failed_closed"
    progress = _attempt_progress(previous, strategy=strategy)
    request_critic = _should_request_critic(
        failure_class=failure_class,
        repeated_failure_count=repeated_count,
        strategy=strategy,
    )
    reason = _decision_reason(
        strategy=strategy,
        failure_class=failure_class,
        repeated_failure_count=repeated_count,
        related_target_count=related_target_count,
    )
    return RepairArbiterDecision(
        strategy=strategy,
        issue_fingerprint=fingerprint,
        target_path=issue.target_path,
        target_sha256=target_sha256,
        obligation_ids=obligation_ids,
        failure_class=failure_class,
        progress=progress,
        previous_attempt_count=len(previous),
        repeated_failure_count=repeated_count,
        request_critic_advisory=request_critic,
        reason=reason,
        allowed_repair_proposals=_allowed_proposals(strategy, related_target_count=related_target_count),
    )


def arbitrate_repair_rejection(
    issue: MaterialIssue,
    *,
    reason: str,
    details: dict[str, Any],
    rejection_attempt: int,
    max_repair_rounds: int,
) -> RepairRejectionDecision:
    failure_class = classify_rejection(reason, details)
    repeated_count = _repeated_failure_count(
        [
            *issue.patch_rejections,
            PatchRejectionEvidence(
                rejection_id="patch_rejection:preview:0",
                issue_id=issue.issue_id,
                attempt=rejection_attempt,
                reason=reason,
                retryable=False,
                target_path=issue.target_path,
                diagnostics=details,
                message=str(details.get("message") or reason)[:2048],
            ),
        ]
    )
    retry_limit = retry_limit_for_failure_class(failure_class, max_repair_rounds=max_repair_rounds)
    retryable = rejection_attempt < retry_limit
    return RepairRejectionDecision(
        failure_class=failure_class,
        retry_limit=retry_limit,
        retryable=retryable,
        repeated_failure_count=repeated_count,
        request_critic_advisory=_should_request_critic(
            failure_class=failure_class,
            repeated_failure_count=repeated_count,
            strategy="replacement" if failure_class != "sandbox_apply_failure" else "patch_set",
        ),
        progress="repeated" if repeated_count > 1 else "no_progress",
        reason=_rejection_reason(failure_class, repeated_failure_count=repeated_count),
    )


def apply_attempt_decision_to_issue(issue: MaterialIssue, decision: RepairArbiterDecision) -> None:
    issue.details["repair_arbiter"] = decision.model_dump()
    issue.details["repair_strategy"] = decision.strategy
    issue.details["allowed_repair_proposals"] = decision.allowed_repair_proposals


def issue_fingerprint(
    issue: MaterialIssue,
    *,
    target_sha256: str | None,
    obligation_ids: list[str] | None = None,
) -> str:
    payload = {
        "issue_type": issue.issue_type,
        "target_kind": issue.target_kind,
        "target_path": issue.target_path,
        "target_sha256": target_sha256,
        "obligation_ids": obligation_ids if obligation_ids is not None else _obligation_ids(issue),
        "missing_name": issue.details.get("missing_name") or issue.details.get("name"),
        "profile": issue.details.get("profile"),
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()
    return f"repair_fingerprint:{digest[:32]}"


def classify_rejection(reason: str, details: dict[str, Any] | None = None) -> FailureClass:
    evidence = " ".join(
        str(value)
        for value in (
            reason,
            json.dumps(details or {}, ensure_ascii=False, sort_keys=True, default=str),
        )
        if value
    ).casefold()
    if any(
        marker in evidence
        for marker in (
            "llm_schema_invalid",
            "schema_invalid_after_repair",
            "did not contain a json object",
            "replacement_payload_missing",
            "replacement_code",
            "material_builder_patch_schema_invalid",
        )
    ):
        return "schema_or_payload_hallucination"
    if any(
        marker in evidence
        for marker in (
            "llm_contract_violation",
            "contract_mismatch",
            "does not match the repair target",
            "expected_current_sha256",
            "checksum_mismatch",
            "material_builder_patch_contract_invalid",
        )
    ):
        return "proposal_contract_mismatch"
    if "noop" in evidence:
        return "noop"
    if "unavailable" in evidence and "runner" in evidence:
        return "governed_mode_unavailable"
    if "apply_failed" in evidence or "microvm_patch_apply_failed" in evidence or "patch_apply_failed" in evidence:
        return "sandbox_apply_failure"
    return "unknown"


def retry_limit_for_failure_class(failure_class: FailureClass, *, max_repair_rounds: int) -> int:
    bounded = max(0, max_repair_rounds)
    if failure_class == "schema_or_payload_hallucination":
        return min(bounded, 3)
    if failure_class in {"proposal_contract_mismatch", "noop"}:
        return min(bounded, 4)
    return bounded


def _strategy_for_issue(
    issue: MaterialIssue,
    *,
    failure_class: FailureClass | None,
    repeated_failure_count: int,
    related_target_count: int,
) -> RepairStrategy:
    if failure_class in {"schema_or_payload_hallucination", "proposal_contract_mismatch", "noop"}:
        if repeated_failure_count >= 2 and related_target_count > 1:
            return "patch_set"
        return "replacement"
    if failure_class == "sandbox_apply_failure":
        return "patch_set" if related_target_count > 1 else "replacement"
    if issue.issue_type in {"missing_symbol_provider", "missing_test_contract"}:
        return "replacement"
    if bool(issue.details.get("target_file_missing")):
        return "replacement"
    return "patch"


def _attempt_progress(previous: list[PatchRejectionEvidence], *, strategy: RepairStrategy) -> ProgressClassification:
    if not previous:
        return "first_attempt"
    previous_strategy = _strategy_from_rejection(previous[-1])
    if previous_strategy and previous_strategy != strategy:
        return "changed_strategy"
    if len(previous) >= 2 and previous[-1].reason == previous[-2].reason:
        return "repeated"
    return "no_progress"


def _strategy_from_rejection(rejection: PatchRejectionEvidence) -> RepairStrategy | None:
    arbiter = rejection.diagnostics.get("repair_arbiter")
    if isinstance(arbiter, dict):
        strategy = arbiter.get("strategy")
        if strategy in {"patch", "replacement", "patch_set", "plan_repair", "regeneration", "failed_closed"}:
            return strategy  # type: ignore[return-value]
    return None


def _allowed_proposals(strategy: RepairStrategy, *, related_target_count: int) -> list[str]:
    if strategy == "patch":
        return ["patch", "replacement"]
    if strategy == "replacement":
        return ["replacement"]
    if strategy == "patch_set":
        return ["patch_set"] if related_target_count > 1 else ["replacement"]
    if strategy == "regeneration":
        return ["regeneration"]
    if strategy == "plan_repair":
        return ["regeneration"]
    return []


def _should_request_critic(
    *,
    failure_class: FailureClass | None,
    repeated_failure_count: int,
    strategy: RepairStrategy,
) -> bool:
    if strategy == "failed_closed":
        return False
    if failure_class in {"schema_or_payload_hallucination", "proposal_contract_mismatch"}:
        return repeated_failure_count >= 1
    return repeated_failure_count >= 2


def _decision_reason(
    *,
    strategy: RepairStrategy,
    failure_class: FailureClass | None,
    repeated_failure_count: int,
    related_target_count: int,
) -> str:
    if strategy == "failed_closed":
        return "repair budget exhausted for this issue fingerprint"
    if strategy == "patch_set":
        return "related targets are governed together after repeated repair failure"
    if strategy == "replacement":
        if failure_class:
            return f"{failure_class} requires a complete target replacement"
        return "issue class is best repaired as a complete target replacement"
    if strategy == "regeneration":
        return "target set requires regeneration from frozen material contract"
    if related_target_count > 1:
        return "focused patch is still allowed before broadening to related targets"
    if repeated_failure_count:
        return "retry uses changed context according to repair arbiter policy"
    return "first focused repair attempt"


def _rejection_reason(failure_class: FailureClass, *, repeated_failure_count: int) -> str:
    if failure_class == "schema_or_payload_hallucination":
        return "proposal did not satisfy the repair payload/schema contract"
    if failure_class == "proposal_contract_mismatch":
        return "proposal did not satisfy the deterministic repair contract"
    if failure_class == "noop":
        return "proposal made no material progress on the target"
    if failure_class == "sandbox_apply_failure":
        return "sandbox owner could not apply the proposal"
    if repeated_failure_count > 1:
        return "same failure class repeated for the same issue"
    return "repair proposal rejected by policy"


def _repeated_failure_count(rejections: list[PatchRejectionEvidence]) -> int:
    if not rejections:
        return 0
    latest = rejections[-1]
    latest_class = classify_rejection(latest.reason, latest.diagnostics)
    count = 0
    for rejection in reversed(rejections):
        if classify_rejection(rejection.reason, rejection.diagnostics) != latest_class:
            break
        count += 1
    return count


def _obligation_ids(issue: MaterialIssue) -> list[str]:
    obligations = issue.details.get("repair_obligations")
    if not isinstance(obligations, list):
        return []
    ids: list[str] = []
    for obligation in obligations:
        if not isinstance(obligation, dict):
            continue
        value = obligation.get("obligation_id")
        if isinstance(value, str) and value:
            ids.append(value)
    return _dedupe(ids)


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result
