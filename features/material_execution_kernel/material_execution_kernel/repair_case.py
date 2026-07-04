"""Repair Control Plane contracts and compiler.

The control plane turns validation symptoms into a governed repair case before
any code author is asked to patch or replace generated files.  It is owned by
the material execution kernel: this module does not generate code, execute
commands, publish artifacts, or treat critic output as authority.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
from typing import Any, Literal
from uuid import uuid4

from pydantic import Field

from material_execution_kernel.types import (
    MaterialIssue,
    MaterialKernelModel,
    ObservedContract,
)


EvidenceSurface = Literal[
    "static_contract",
    "runtime",
    "test",
    "sandbox_apply",
    "packaging",
    "publication",
    "proposal_schema",
    "proposal_contract",
    "unknown",
]
ObligationKind = Literal[
    "importable_export",
    "package_importable",
    "call_signature",
    "call_return_value",
    "return_contains",
    "stdout_contains",
    "stderr_contains",
    "raises_exception",
    "exit_code",
    "artifact_exists",
    "file_output_exists",
    "file_parseable",
    "runtime_entrypoint",
    "collectible_test",
]
RootCauseKind = Literal[
    "interface_drift",
    "behavior_drift",
    "validation_contract_drift",
    "proposal_schema_failure",
    "proposal_contract_failure",
    "sandbox_apply_failure",
    "dependency_gap",
    "artifact_publication_gap",
    "completion_evidence_gap",
    "repair_case_under_specified",
]
RepairAction = Literal["patch", "replacement", "patchset", "deterministic_repair", "critic_advisory", "fail_closed"]
RepairCaseStatus = Literal[
    "ready_for_deterministic_repair",
    "ready_for_llm_proposal",
    "under_specified",
    "blocked",
    "failed_closed",
]
ProgressClassification = Literal[
    "first_attempt",
    "improved",
    "no_change",
    "regressed",
    "same_failure_same_target",
    "same_root_cause_new_symptom",
    "new_root_cause",
]


class TargetRef(MaterialKernelModel):
    path: str = Field(min_length=1, max_length=4096)
    role: Literal["symptom", "provider", "related", "candidate"] = "candidate"
    kind: str = Field(default="unknown", max_length=128)
    source: str = Field(default="", max_length=256)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class ObservedOperation(MaterialKernelModel):
    kind: Literal["import", "call", "command_exec", "artifact_publish", "file_output", "unknown"] = "unknown"
    callable: str | None = Field(default=None, max_length=512)
    module: str | None = Field(default=None, max_length=512)
    symbol: str | None = Field(default=None, max_length=255)
    inputs: dict[str, Any] = Field(default_factory=dict)
    observed_output_channel: str | None = Field(default=None, max_length=64)


class NormalizedEvidence(MaterialKernelModel):
    evidence_id: str = Field(min_length=1, max_length=128)
    source_profile: str | None = Field(default=None, max_length=128)
    surface: EvidenceSurface = "unknown"
    symptom_targets: list[TargetRef] = Field(default_factory=list, max_length=64)
    observed_operation: ObservedOperation | None = None
    assertion: dict[str, Any] | None = None
    normalized_message: str = Field(default="", max_length=4096)
    raw_excerpt_ref: str | None = Field(default=None, max_length=512)
    severity: Literal["blocking", "warning", "info"] = "blocking"


class RepairObligation(MaterialKernelModel):
    obligation_id: str = Field(min_length=1, max_length=256)
    kind: ObligationKind
    owner_target: TargetRef | None = None
    expected: dict[str, Any] = Field(default_factory=dict)
    observed: dict[str, Any] | None = None
    evidence_ids: list[str] = Field(default_factory=list, max_length=128)
    acceptance_criterion: str = Field(min_length=1, max_length=2048)
    severity: Literal["blocking", "warning"] = "blocking"


class SuccessCriterion(MaterialKernelModel):
    criterion_id: str = Field(min_length=1, max_length=256)
    obligation_id: str = Field(min_length=1, max_length=256)
    description: str = Field(min_length=1, max_length=2048)
    validation_profiles: list[str] = Field(default_factory=list, max_length=64)


class ProviderResolution(MaterialKernelModel):
    provider_target: TargetRef | None = None
    provider_interface: str | None = Field(default=None, max_length=512)
    source: str = Field(default="", max_length=256)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    rationale: str = Field(default="", max_length=2048)


class RepairAttemptSummary(MaterialKernelModel):
    attempt: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=256)
    target_path: str | None = Field(default=None, max_length=4096)
    failure_class: str | None = Field(default=None, max_length=128)


class ProgressState(MaterialKernelModel):
    previous_fingerprint: str | None = Field(default=None, max_length=128)
    current_fingerprint: str = Field(min_length=1, max_length=128)
    classification: ProgressClassification = "first_attempt"
    repeated_failure_count: int = Field(default=0, ge=0)


class RetryBudget(MaterialKernelModel):
    llm_attempts_remaining: int = Field(default=0, ge=0)
    deterministic_attempts_remaining: int = Field(default=1, ge=0)
    max_repair_rounds: int = Field(default=0, ge=0)


class RepairCase(MaterialKernelModel):
    schema_version: Literal["repair_case.v0.1"] = "repair_case.v0.1"
    case_id: str = Field(min_length=1, max_length=128)
    root_cause_kind: RootCauseKind
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    primary_repair_target: TargetRef | None = None
    symptom_targets: list[TargetRef] = Field(default_factory=list, max_length=64)
    related_targets: list[TargetRef] = Field(default_factory=list, max_length=128)
    failed_profiles: list[str] = Field(default_factory=list, max_length=64)
    evidence: list[NormalizedEvidence] = Field(default_factory=list, max_length=256)
    obligations: list[RepairObligation] = Field(default_factory=list, max_length=512)
    success_criteria: list[SuccessCriterion] = Field(default_factory=list, max_length=512)
    provider_resolution: ProviderResolution | None = None
    previous_attempts: list[RepairAttemptSummary] = Field(default_factory=list, max_length=64)
    progress_state: ProgressState
    allowed_actions: list[RepairAction] = Field(default_factory=list, max_length=16)
    forbidden_actions: list[RepairAction] = Field(default_factory=list, max_length=16)
    retry_budget: RetryBudget
    stop_conditions: list[str] = Field(default_factory=list, max_length=64)
    status: RepairCaseStatus
    original_issue_target_path: str | None = Field(default=None, max_length=4096)


def compile_repair_case(
    raw: dict[str, Any],
    issue: MaterialIssue,
    *,
    target_sha256: str | None,
    max_repair_rounds: int,
) -> RepairCase:
    evidence = normalize_evidence(raw, issue)
    provider = resolve_provider(raw, issue, evidence)
    obligations = compile_obligations(issue, evidence=evidence, provider=provider)
    root_cause = classify_root_cause(issue, evidence=evidence, obligations=obligations, provider=provider)
    primary_target = _primary_target_for_case(issue, provider=provider, root_cause=root_cause)
    symptom_targets = _dedupe_targets([target for item in evidence for target in item.symptom_targets])
    related_targets = _related_targets(issue, primary_target=primary_target, symptom_targets=symptom_targets)
    confidence = max(
        [provider.confidence if provider else 0.0, *[target.confidence for target in symptom_targets], 0.0]
    )
    previous_attempts = [
        RepairAttemptSummary(
            attempt=rejection.attempt,
            reason=rejection.reason,
            target_path=rejection.target_path,
            failure_class=str((rejection.diagnostics or {}).get("repair_arbiter", {}).get("failure_class") or ""),
        )
        for rejection in issue.patch_rejections
    ]
    fingerprint = repair_case_fingerprint(
        root_cause_kind=root_cause,
        primary_target_path=primary_target.path if primary_target else None,
        obligations=obligations,
        failed_profiles=_failed_profiles(issue),
        target_sha256=target_sha256,
    )
    repeated = _repeated_failure_count(issue, fingerprint=fingerprint)
    progress = ProgressState(
        previous_fingerprint=_previous_fingerprint(issue),
        current_fingerprint=fingerprint,
        classification="same_failure_same_target" if repeated else ("first_attempt" if not previous_attempts else "new_root_cause"),
        repeated_failure_count=repeated,
    )
    retry_budget = RetryBudget(
        llm_attempts_remaining=max(0, max_repair_rounds - len(issue.patch_rejections)),
        deterministic_attempts_remaining=1,
        max_repair_rounds=max_repair_rounds,
    )
    status = _case_status(
        primary_target=primary_target,
        obligations=obligations,
        root_cause=root_cause,
        progress=progress,
        retry_budget=retry_budget,
    )
    allowed = _allowed_actions(status=status, root_cause=root_cause)
    forbidden = _forbidden_actions(status=status, root_cause=root_cause, issue=issue, primary_target=primary_target)
    return RepairCase(
        case_id=f"repair_case_{uuid4().hex}",
        root_cause_kind=root_cause,
        confidence=confidence,
        primary_repair_target=primary_target,
        symptom_targets=symptom_targets,
        related_targets=related_targets,
        failed_profiles=_failed_profiles(issue),
        evidence=evidence,
        obligations=obligations,
        success_criteria=_success_criteria(obligations, failed_profiles=_failed_profiles(issue)),
        provider_resolution=provider,
        previous_attempts=previous_attempts,
        progress_state=progress,
        allowed_actions=allowed,
        forbidden_actions=forbidden,
        retry_budget=retry_budget,
        stop_conditions=_stop_conditions(status=status, progress=progress),
        status=status,
        original_issue_target_path=issue.target_path,
    )


def normalize_evidence(raw: dict[str, Any], issue: MaterialIssue) -> list[NormalizedEvidence]:
    profile = str(issue.details.get("profile") or "")
    evidence_text = _issue_text(issue)
    surface: EvidenceSurface = "unknown"
    if issue.issue_type in {"missing_symbol_provider", "missing_test_contract"} or "contract comparison failed" in evidence_text:
        surface = "static_contract"
    elif "schema_invalid" in evidence_text or "llm_schema_invalid" in evidence_text:
        surface = "proposal_schema"
    elif "contract_violation" in evidence_text or "contract_mismatch" in evidence_text:
        surface = "proposal_contract"
    elif profile == "python-pytest" or _looks_like_test_path(issue.target_path or ""):
        surface = "test"
    elif profile:
        surface = "runtime"
    symptom_targets = _symptom_targets(raw, issue, evidence_text=evidence_text)
    operation = _observed_operation_from_text(raw, issue, evidence_text=evidence_text, symptom_targets=symptom_targets)
    assertion = _assertion_from_text(evidence_text)
    return [
        NormalizedEvidence(
            evidence_id=f"evidence:{issue.issue_id}:0",
            source_profile=profile or None,
            surface=surface,
            symptom_targets=symptom_targets,
            observed_operation=operation,
            assertion=assertion,
            normalized_message=_compact(evidence_text, 4096),
            raw_excerpt_ref=str(issue.details.get("stdout_ref") or issue.details.get("stderr_ref") or "") or None,
            severity="blocking" if issue.severity in {"repairable", "blocking_completion"} else "warning",
        )
    ]


def compile_obligations(
    issue: MaterialIssue,
    *,
    evidence: list[NormalizedEvidence],
    provider: ProviderResolution | None,
) -> list[RepairObligation]:
    obligations: list[RepairObligation] = []
    if issue.issue_type == "local_import_cycle" or issue.details.get("observed_issue_type") == "local_import_cycle":
        cycle_paths = [
            str(path).strip()
            for path in issue.details.get("cycle_paths", [])
            if str(path).strip()
        ] if isinstance(issue.details.get("cycle_paths"), list) else []
        obligations.append(
            RepairObligation(
                obligation_id=f"obligation:local_import_acyclic:{issue.target_path or issue.issue_id}",
                kind="runtime_entrypoint",
                owner_target=_target_ref(
                    issue.target_path or "",
                    role="provider",
                    source="local_import_cycle",
                    confidence=0.94,
                )
                if issue.target_path
                else None,
                expected={
                    "acyclic_local_imports": True,
                    "cycle_paths": cycle_paths,
                    "cycle_modules": issue.details.get("cycle_modules") or [],
                },
                observed={
                    "message": issue.details.get("message"),
                    "observed_issue_type": issue.details.get("observed_issue_type"),
                },
                evidence_ids=[ev.evidence_id for ev in evidence],
                acceptance_criterion="local Python imports are acyclic at module import time",
                severity="blocking",
            )
        )
    for item in issue.details.get("repair_obligations") or []:
        if not isinstance(item, dict):
            continue
        kind = "importable_export" if item.get("kind") == "importable_export" else "package_importable"
        target_path = str(
            item.get("target_path")
            or (provider.provider_target.path if provider is not None and provider.provider_target is not None else "")
            or issue.target_path
            or ""
        )
        symbol = str(item.get("symbol") or item.get("missing_name") or issue.details.get("missing_name") or "")
        obligations.append(
            RepairObligation(
                obligation_id=str(item.get("obligation_id") or f"obligation:{kind}:{target_path}:{symbol}"),
                kind=kind,
                owner_target=_target_ref(target_path, role="provider", source="repair_obligation", confidence=0.95)
                if target_path
                else None,
                expected={"symbol": symbol, "module": item.get("target_module")},
                observed=None,
                evidence_ids=[ev.evidence_id for ev in evidence],
                acceptance_criterion="symbol is provided by the target interface surface",
                severity="blocking",
            )
        )
    for ev in evidence:
        operation = ev.observed_operation
        assertion = ev.assertion or {}
        owner_target = provider.provider_target if provider else None
        if operation and operation.kind == "call":
            minimum_args = operation.inputs.get("minimum_positional_arguments")
            if isinstance(minimum_args, int) and minimum_args > 0:
                obligations.append(
                    RepairObligation(
                        obligation_id=f"obligation:call_signature:{operation.callable or issue.issue_id}",
                        kind="call_signature",
                        owner_target=owner_target,
                        expected={
                            "callable": operation.callable,
                            "minimum_positional_arguments": minimum_args,
                        },
                        observed=operation.inputs,
                        evidence_ids=[ev.evidence_id],
                        acceptance_criterion="callable accepts the positional arguments shown by validation evidence",
                    )
                )
        if assertion.get("stdout_contains"):
            obligations.append(
                RepairObligation(
                    obligation_id=f"obligation:stdout_contains:{operation.callable if operation else issue.issue_id}",
                    kind="stdout_contains",
                    owner_target=owner_target,
                    expected={"contains": assertion["stdout_contains"], "callable": operation.callable if operation else None},
                    observed={"channel": "stdout"},
                    evidence_ids=[ev.evidence_id],
                    acceptance_criterion="validation-observed call emits expected text to stdout",
                )
            )
        if assertion.get("return_contains"):
            obligations.append(
                RepairObligation(
                    obligation_id=f"obligation:return_contains:{operation.callable if operation else issue.issue_id}",
                    kind="return_contains",
                    owner_target=owner_target,
                    expected={"contains": assertion["return_contains"], "callable": operation.callable if operation else None},
                    observed={"channel": "return"},
                    evidence_ids=[ev.evidence_id],
                    acceptance_criterion="validation-observed call returns expected text",
                )
            )
        if assertion.get("return_value") is not None:
            obligations.append(
                RepairObligation(
                    obligation_id=f"obligation:call_return_value:{operation.callable if operation else issue.issue_id}",
                    kind="call_return_value",
                    owner_target=owner_target,
                    expected={"value": assertion["return_value"], "callable": operation.callable if operation else None},
                    observed={"channel": "return"},
                    evidence_ids=[ev.evidence_id],
                    acceptance_criterion="validation-observed call returns the expected literal value",
                )
            )
        if assertion.get("raises_exception"):
            obligations.append(
                RepairObligation(
                    obligation_id=f"obligation:raises_exception:{operation.callable if operation else issue.issue_id}",
                    kind="raises_exception",
                    owner_target=owner_target,
                    expected={"exception": assertion["raises_exception"], "callable": operation.callable if operation else None},
                    observed=None,
                    evidence_ids=[ev.evidence_id],
                    acceptance_criterion="validation-observed call raises the expected exception",
                )
            )
    if issue.issue_type == "missing_test_contract" and issue.target_path:
        obligations.append(
            RepairObligation(
                obligation_id=f"obligation:collectible_test:{issue.target_path}",
                kind="collectible_test",
                owner_target=_target_ref(issue.target_path, role="provider", source="missing_test_contract", confidence=0.9),
                expected={"path": issue.target_path, "collectible": True},
                observed=None,
                evidence_ids=[ev.evidence_id for ev in evidence],
                acceptance_criterion="validation surface contains at least one collectible test",
            )
        )
    dependency_names = _dependency_names_for_issue(issue)
    if dependency_names and issue.target_path:
        for dependency_name in dependency_names:
            obligations.append(
                RepairObligation(
                    obligation_id=f"obligation:package_importable:{issue.target_path}:{dependency_name}",
                    kind="package_importable",
                    owner_target=_target_ref(issue.target_path, role="provider", source="dependency_strategy", confidence=0.9),
                    expected={"dependency": dependency_name},
                    observed=None,
                    evidence_ids=[ev.evidence_id for ev in evidence],
                    acceptance_criterion="dependency strategy declares the package required by generated runtime imports",
                )
            )
    if _is_parseable_generated_file_issue(issue) and issue.target_path:
        file_format = str(issue.details.get("format") or _path_format_hint(issue.target_path) or "file")
        obligations.append(
            RepairObligation(
                obligation_id=f"obligation:file_parseable:{issue.target_path}:{file_format}",
                kind="file_parseable",
                owner_target=_target_ref(issue.target_path, role="provider", source="parse_error", confidence=0.9),
                expected={"path": issue.target_path, "format": file_format, "parseable": True},
                observed={
                    "message": issue.details.get("message"),
                    "observed_issue_type": issue.details.get("observed_issue_type"),
                },
                evidence_ids=[ev.evidence_id for ev in evidence],
                acceptance_criterion="generated metadata/config file parses successfully for its declared format",
            )
        )
    if not obligations:
        missing = str(issue.details.get("missing_name") or "")
        if missing and issue.target_path:
            obligations.append(
                RepairObligation(
                    obligation_id=f"obligation:importable_export:{issue.target_path}:{missing}",
                    kind="importable_export",
                    owner_target=_target_ref(issue.target_path, role="provider", source="missing_name", confidence=0.7),
                    expected={"symbol": missing},
                    observed=None,
                    evidence_ids=[ev.evidence_id for ev in evidence],
                    acceptance_criterion="missing symbol is provided by the target interface surface",
                )
            )
    if not obligations and provider is not None and provider.provider_target is not None:
        for ev in evidence:
            if ev.severity != "blocking" or ev.surface not in {"runtime", "test", "packaging"}:
                continue
            kind: ObligationKind = "exit_code" if ev.source_profile else "runtime_entrypoint"
            obligations.append(
                RepairObligation(
                    obligation_id=f"obligation:{kind}:{provider.provider_target.path}:{ev.source_profile or ev.evidence_id}",
                    kind=kind,
                    owner_target=provider.provider_target,
                    expected={
                        "validation_profile": ev.source_profile,
                        "validation_passes": True,
                    },
                    observed={"message": ev.normalized_message},
                    evidence_ids=[ev.evidence_id],
                    acceptance_criterion="provider target satisfies the failing validation evidence without introducing a new failure",
                )
            )
    return _dedupe_obligations(obligations)


def resolve_provider(
    raw: dict[str, Any],
    issue: MaterialIssue,
    evidence: list[NormalizedEvidence],
) -> ProviderResolution | None:
    declared_provider = _provider_from_issue_module_details(raw, issue)
    if declared_provider is not None:
        return declared_provider
    for obligation in issue.details.get("repair_obligations") or []:
        if isinstance(obligation, dict) and obligation.get("target_path"):
            target = _target_ref(str(obligation["target_path"]), role="provider", source="repair_obligation", confidence=0.95)
            return ProviderResolution(
                provider_target=target,
                provider_interface=str(obligation.get("target_module") or obligation.get("symbol") or ""),
                source="repair_obligation",
                confidence=0.95,
                rationale="repair obligation declares the provider target",
            )
    observed_contract: ObservedContract | None = raw.get("observed_contract")
    if observed_contract is None:
        return _provider_from_evidence_targets(evidence) or _provider_from_non_test_target(issue)
    generated_by_path = {item.path: item for item in raw.get("generated_files", [])}
    for ev in evidence:
        operation = ev.observed_operation
        if operation is None or operation.kind != "call":
            continue
        resolved = _resolve_call_provider(
            operation,
            evidence=ev,
            observed_contract=observed_contract,
            generated_by_path=generated_by_path,
        )
        if resolved is not None:
            return resolved
    return _provider_from_evidence_targets(evidence) or _provider_from_non_test_target(issue)


def classify_root_cause(
    issue: MaterialIssue,
    *,
    evidence: list[NormalizedEvidence],
    obligations: list[RepairObligation],
    provider: ProviderResolution | None,
) -> RootCauseKind:
    text = _issue_text(issue).casefold()
    if issue.issue_type == "local_import_cycle" or "local import cycle" in text or "circular import" in text:
        return "interface_drift"
    if issue.issue_type == "missing_test_contract":
        return "validation_contract_drift"
    if "schema_invalid" in text or "llm_schema_invalid" in text or "patch_schema_invalid" in text:
        return "proposal_schema_failure"
    if "contract_violation" in text or "contract_mismatch" in text or "patch_contract_invalid" in text:
        return "proposal_contract_failure"
    if any(item.kind == "package_importable" for item in obligations) or "dependency" in text or "modulenotfounderror" in text:
        return "dependency_gap"
    if any(item.kind in {"importable_export", "runtime_entrypoint"} for item in obligations):
        return "interface_drift"
    if any(item.kind == "file_parseable" for item in obligations):
        return "validation_contract_drift"
    if any(
        item.kind
        in {"call_signature", "call_return_value", "return_contains", "stdout_contains", "stderr_contains", "raises_exception", "exit_code"}
        for item in obligations
    ) and provider is not None:
        return "behavior_drift"
    if "artifact" in text and "publish" in text:
        return "artifact_publication_gap"
    return "repair_case_under_specified"


def repair_case_fingerprint(
    *,
    root_cause_kind: RootCauseKind,
    primary_target_path: str | None,
    obligations: list[RepairObligation],
    failed_profiles: list[str],
    target_sha256: str | None,
) -> str:
    payload = {
        "root_cause_kind": root_cause_kind,
        "primary_target": primary_target_path,
        "obligations": sorted((item.kind, item.obligation_id, item.acceptance_criterion) for item in obligations),
        "failed_profiles": sorted(failed_profiles),
        "target_sha256": target_sha256,
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()
    return f"repair_case_fingerprint:{digest[:32]}"


def validation_surface_is_primary_target(issue: MaterialIssue, repair_case: RepairCase) -> bool:
    if not issue.target_path or not _looks_like_validation_surface(issue.target_path):
        return False
    if repair_case.root_cause_kind == "validation_contract_drift":
        return True
    text = _issue_text(issue).casefold()
    return (
        "syntaxerror" in text
        or "error collecting" in text
        or "validator schema" in text
        or issue.issue_type == "missing_test_contract"
    )


def repair_case_allows_llm(repair_case: RepairCase) -> bool:
    return (
        repair_case.status == "ready_for_llm_proposal"
        and repair_case.primary_repair_target is not None
        and repair_case.retry_budget.llm_attempts_remaining > 0
        and repair_case.progress_state.classification != "same_failure_same_target"
    )


def _primary_target_for_case(
    issue: MaterialIssue,
    *,
    provider: ProviderResolution | None,
    root_cause: RootCauseKind,
) -> TargetRef | None:
    if root_cause == "validation_contract_drift" and issue.target_path:
        return _target_ref(issue.target_path, role="provider", source="validation_contract_drift", confidence=0.9)
    if provider and provider.provider_target is not None:
        return provider.provider_target
    if issue.target_path and not _looks_like_validation_surface(issue.target_path):
        return _target_ref(issue.target_path, role="provider", source="issue_non_validation_target", confidence=0.55)
    return None


def _case_status(
    *,
    primary_target: TargetRef | None,
    obligations: list[RepairObligation],
    root_cause: RootCauseKind,
    progress: ProgressState,
    retry_budget: RetryBudget,
) -> RepairCaseStatus:
    if primary_target is None:
        return "under_specified"
    if not any(item.severity == "blocking" for item in obligations):
        return "under_specified"
    if progress.classification == "same_failure_same_target":
        return "blocked"
    if root_cause == "proposal_schema_failure":
        return "ready_for_deterministic_repair"
    if retry_budget.llm_attempts_remaining <= 0:
        return "failed_closed"
    return "ready_for_llm_proposal"


def _allowed_actions(*, status: RepairCaseStatus, root_cause: RootCauseKind) -> list[RepairAction]:
    if status == "ready_for_deterministic_repair":
        return ["deterministic_repair", "fail_closed"]
    if status == "ready_for_llm_proposal":
        if root_cause == "behavior_drift":
            return ["patch", "replacement", "critic_advisory", "fail_closed"]
        return ["patch", "replacement", "patchset", "critic_advisory", "fail_closed"]
    if status in {"under_specified", "blocked", "failed_closed"}:
        return ["fail_closed"]
    return ["fail_closed"]


def _forbidden_actions(
    *,
    status: RepairCaseStatus,
    root_cause: RootCauseKind,
    issue: MaterialIssue,
    primary_target: TargetRef | None,
) -> list[RepairAction]:
    forbidden: list[RepairAction] = []
    if status in {"under_specified", "blocked", "failed_closed"}:
        forbidden.extend(["patch", "replacement", "patchset", "critic_advisory"])
    if issue.target_path and primary_target and issue.target_path != primary_target.path and _looks_like_validation_surface(issue.target_path):
        forbidden.append("patch")
    if root_cause == "proposal_schema_failure":
        forbidden.append("critic_advisory")
    return _dedupe_list(forbidden)


def _success_criteria(obligations: list[RepairObligation], *, failed_profiles: list[str]) -> list[SuccessCriterion]:
    return [
        SuccessCriterion(
            criterion_id=f"criterion:{item.obligation_id}",
            obligation_id=item.obligation_id,
            description=item.acceptance_criterion,
            validation_profiles=failed_profiles,
        )
        for item in obligations
        if item.severity == "blocking"
    ]


def _stop_conditions(*, status: RepairCaseStatus, progress: ProgressState) -> list[str]:
    conditions: list[str] = []
    if status == "under_specified":
        conditions.append("repair_case_under_specified")
    if status == "blocked":
        conditions.append("same_fingerprint_same_target_same_context")
    if progress.repeated_failure_count:
        conditions.append("repeated_failure_fingerprint")
    return conditions


def _symptom_targets(raw: dict[str, Any], issue: MaterialIssue, *, evidence_text: str) -> list[TargetRef]:
    known_paths = _known_paths(raw)
    targets: list[TargetRef] = []
    resolution = issue.target_resolution
    if issue.target_path:
        targets.append(_target_ref(issue.target_path, role="symptom", source="issue_target", confidence=0.7))
    if resolution is not None:
        if resolution.primary_target:
            targets.append(_target_ref(resolution.primary_target, role="symptom", source="issue_target_resolution", confidence=resolution.confidence))
        for path in [*resolution.candidate_targets, *resolution.related_targets]:
            targets.append(_target_ref(path, role="symptom", source="issue_target_resolution_candidate", confidence=min(resolution.confidence, 0.6)))
    for known in sorted(known_paths, key=len, reverse=True):
        basename = known.rsplit("/", 1)[-1]
        basename_pattern = rf"(?<![A-Za-z0-9_.-]){re.escape(basename)}(?![A-Za-z0-9_.-])"
        if known in evidence_text or re.search(basename_pattern, evidence_text):
            targets.append(_target_ref(known, role="symptom", source="evidence_path", confidence=0.55))
    return _dedupe_targets(targets)


def _observed_operation_from_text(
    raw: dict[str, Any],
    issue: MaterialIssue,
    *,
    evidence_text: str,
    symptom_targets: list[TargetRef],
) -> ObservedOperation | None:
    call = _first_call_expression(evidence_text)
    if not call:
        return None
    inputs: dict[str, Any] = {}
    if "--help" in evidence_text:
        inputs["argv_contains"] = "--help"
    positional = re.search(r"takes\s+\d+\s+positional arguments?\s+but\s+(\d+)\s+(?:was|were)\s+given", evidence_text)
    if positional:
        inputs["minimum_positional_arguments"] = int(positional.group(1))
    return ObservedOperation(
        kind="call",
        callable=call,
        module=call.rsplit(".", 1)[0] if "." in call else None,
        symbol=call.rsplit(".", 1)[-1],
        inputs=inputs,
        observed_output_channel=_observed_output_channel(evidence_text),
    )


def _assertion_from_text(evidence_text: str) -> dict[str, Any] | None:
    lowered = evidence_text.casefold()
    assertion: dict[str, Any] = {}
    stdout_match = re.search(r"assert\s+['\"]([^'\"]+)['\"]\s+in\s+.+?(?:readouterr\(\)\.out|stdout)", evidence_text)
    if stdout_match:
        assertion["stdout_contains"] = stdout_match.group(1)
    return_match = re.search(r"assert\s+['\"]([^'\"]+)['\"]\s+in\s+(?:result|response|output)", evidence_text)
    if return_match:
        assertion["return_contains"] = return_match.group(1)
    return_value_match = re.search(
        r"assert\s+((?:[A-Za-z_][A-Za-z0-9_]*\.)*[A-Za-z_][A-Za-z0-9_]*)\s*\([^)]*\)\s*==\s*([^\n#]+)",
        evidence_text,
    )
    if return_value_match:
        expected_value = _safe_literal_from_assertion(return_value_match.group(2))
        if expected_value is not None:
            assertion["return_value"] = expected_value
    if "return_value" not in assertion:
        value_match = re.search(r"assert\s+([A-Za-z_][A-Za-z0-9_]*)\s*==\s*([^\n#]+)", evidence_text)
        if value_match:
            expected_value = _safe_literal_from_assertion(value_match.group(2))
            if expected_value is not None:
                assertion["return_value"] = expected_value
                assertion["value_name"] = value_match.group(1)
    raises_match = re.search(r"pytest\.raises\(\s*([A-Za-z_][A-Za-z0-9_.]*)", evidence_text)
    if raises_match:
        assertion["raises_exception"] = raises_match.group(1)
    if "did not raise systemexit" in lowered:
        assertion["raises_exception"] = "SystemExit"
    return assertion or None


def _safe_literal_from_assertion(raw: str) -> Any | None:
    value = raw.strip()
    value = re.split(r"\s+(?:#|and|or)\s+", value, maxsplit=1)[0].strip()
    value = value.rstrip(",); ")
    if not value:
        return None
    try:
        parsed = ast.literal_eval(value)
    except (SyntaxError, ValueError):
        return None
    if isinstance(parsed, (str, int, float, bool)) or parsed is None:
        return parsed
    return None


def _resolve_call_provider(
    operation: ObservedOperation,
    *,
    evidence: NormalizedEvidence,
    observed_contract: ObservedContract,
    generated_by_path: dict[str, Any],
) -> ProviderResolution | None:
    callable_name = operation.callable or ""
    module_hint = operation.module
    symbol = operation.symbol
    for symptom in evidence.symptom_targets:
        generated = generated_by_path.get(symptom.path)
        if generated is None:
            continue
        target = _resolve_call_from_test_content(
            generated.content,
            callable_name=callable_name,
            symbol=symbol,
            observed_contract=observed_contract,
        )
        if target is not None:
            return target
    if module_hint:
        target = _provider_for_module_symbol(observed_contract, module_hint, symbol)
        if target is not None:
            return target
    return None


def _resolve_call_from_test_content(
    content: str,
    *,
    callable_name: str,
    symbol: str | None,
    observed_contract: ObservedContract,
) -> ProviderResolution | None:
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return None
    aliases: dict[str, tuple[str, str | None]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                aliases[alias.asname or alias.name.split(".", 1)[0]] = (alias.name, None)
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                aliases[alias.asname or alias.name] = (node.module, alias.name)
    parts = [part for part in callable_name.split(".") if part]
    if len(parts) >= 2 and parts[0] in aliases:
        module, imported_symbol = aliases[parts[0]]
        return _provider_for_module_symbol(observed_contract, module, parts[-1] if imported_symbol is None else imported_symbol)
    if symbol and symbol in aliases:
        module, imported_symbol = aliases[symbol]
        return _provider_for_module_symbol(observed_contract, module, imported_symbol or symbol)
    return None


def _provider_for_module_symbol(
    observed_contract: ObservedContract,
    module: str,
    symbol: str | None,
) -> ProviderResolution | None:
    if symbol:
        for export in observed_contract.exports:
            if _module_name_matches(export.module, module) and export.name == symbol:
                return ProviderResolution(
                    provider_target=_target_ref(export.path, role="provider", source="observed_contract_export", confidence=0.9),
                    provider_interface=f"{module}.{symbol}",
                    source="observed_contract_export",
                    confidence=0.9,
                    rationale="observed contract export owns the called interface",
                )
    for file in observed_contract.files:
        if _module_name_matches(file.module, module):
            return ProviderResolution(
                provider_target=_target_ref(file.path, role="provider", source="observed_contract_module", confidence=0.82),
                provider_interface=module,
                source="observed_contract_module",
                confidence=0.82,
                rationale="observed contract module owns the called interface",
            )
    return None


def _module_name_matches(observed: str, requested: str) -> bool:
    observed_name = str(observed or "").strip(".")
    requested_name = str(requested or "").strip(".")
    if not observed_name or not requested_name:
        return False
    if observed_name == requested_name:
        return True
    return _strip_src_module_prefix(observed_name) == _strip_src_module_prefix(requested_name)


def _strip_src_module_prefix(module: str) -> str:
    return module[4:] if module.startswith("src.") else module


def _provider_from_issue_module_details(raw: dict[str, Any], issue: MaterialIssue) -> ProviderResolution | None:
    observed_contract: ObservedContract | None = raw.get("observed_contract")
    if observed_contract is None:
        return None
    module = str(issue.details.get("module") or "").strip()
    symbol = str(issue.details.get("missing_name") or issue.details.get("name") or "").strip() or None
    if not module:
        for obligation in issue.details.get("repair_obligations") or []:
            if not isinstance(obligation, dict):
                continue
            module = str(obligation.get("target_module") or "").strip()
            symbol = str(obligation.get("symbol") or symbol or "").strip() or None
            if module:
                break
    if not module:
        return None
    resolved = _provider_for_module_symbol(observed_contract, module, symbol)
    if resolved is None:
        return None
    return ProviderResolution(
        provider_target=resolved.provider_target,
        provider_interface=resolved.provider_interface or (f"{module}.{symbol}" if symbol else module),
        source="issue_module_details",
        confidence=max(resolved.confidence, 0.86),
        rationale="issue details identify the local module that should provide the missing interface",
    )


def _provider_from_non_test_target(issue: MaterialIssue) -> ProviderResolution | None:
    if issue.target_path and not _looks_like_validation_surface(issue.target_path):
        return ProviderResolution(
            provider_target=_target_ref(issue.target_path, role="provider", source="non_validation_issue_target", confidence=0.55),
            source="non_validation_issue_target",
            confidence=0.55,
            rationale="issue target is not a validation surface",
        )
    return None


def _provider_from_evidence_targets(evidence: list[NormalizedEvidence]) -> ProviderResolution | None:
    for ev in evidence:
        for target in ev.symptom_targets:
            if _looks_like_validation_surface(target.path):
                continue
            if not _path_format_hint(target.path) and not target.path.endswith((".py", ".txt")):
                continue
            provider_target = TargetRef(**{**target.model_dump(mode="json"), "role": "provider"})
            return ProviderResolution(
                provider_target=provider_target,
                provider_interface=None,
                source="evidence_non_validation_target",
                confidence=min(0.75, max(0.5, target.confidence)),
                rationale="validation evidence mentioned a generated non-validation target",
            )
    return None


def _related_targets(
    issue: MaterialIssue,
    *,
    primary_target: TargetRef | None,
    symptom_targets: list[TargetRef],
) -> list[TargetRef]:
    related: list[TargetRef] = []
    primary_path = primary_target.path if primary_target else None
    for target in symptom_targets:
        if target.path != primary_path:
            related.append(TargetRef(**{**target.model_dump(mode="json"), "role": "related"}))
    if issue.target_resolution is not None:
        for path in [*issue.target_resolution.related_targets, *issue.target_resolution.candidate_targets]:
            if path != primary_path:
                related.append(_target_ref(path, role="related", source="issue_related_target", confidence=0.5))
    return _dedupe_targets(related)


def _failed_profiles(issue: MaterialIssue) -> list[str]:
    profile = str(issue.details.get("profile") or "")
    profiles = [profile] if profile else []
    bundle = issue.details.get("issue_bundle")
    if isinstance(bundle, dict):
        raw_profiles = bundle.get("profiles_failed")
        if isinstance(raw_profiles, list):
            profiles.extend(str(item) for item in raw_profiles)
    return _dedupe_list([item for item in profiles if item])


def _dependency_names_for_issue(issue: MaterialIssue) -> list[str]:
    names: list[str] = []
    keys = ("dependency_name",) if issue.issue_type == "missing_symbol_provider" else ("dependency_name", "module")
    for key in keys:
        value = issue.details.get(key)
        if isinstance(value, str) and value.strip():
            names.append(value.strip())
    for key in ("undeclared_external_imports", "external_dependencies"):
        values = issue.details.get(key)
        if isinstance(values, list):
            names.extend(str(item).strip() for item in values if str(item).strip())
    text = _issue_text(issue)
    for match in re.finditer(r"ModuleNotFoundError:\s+No module named ['\"]([^'\"]+)['\"]", text):
        names.append(match.group(1).strip())
    return _dedupe_list([name.split(".", 1)[0] for name in names if name])


def _is_parseable_generated_file_issue(issue: MaterialIssue) -> bool:
    observed_type = str(issue.details.get("observed_issue_type") or issue.details.get("observed_issue_id") or "")
    text = _issue_text(issue).casefold()
    return (
        issue.target_path is not None
        and (
            "parse_error" in observed_type
            or "parse error" in text
            or "invalid initial character" in text
            or "unterminated string literal" in text
        )
        and _path_format_hint(issue.target_path) is not None
    )


def _path_format_hint(path: str) -> str | None:
    normalized = path.replace("\\", "/").rsplit("/", 1)[-1].casefold()
    if normalized == "pyproject.toml" or normalized.endswith(".toml"):
        return "toml"
    if normalized.endswith((".yaml", ".yml")):
        return "yaml"
    if normalized.endswith(".json"):
        return "json"
    if normalized.endswith(".cfg"):
        return "cfg"
    if normalized.endswith(".py"):
        return "python"
    return None


def _previous_fingerprint(issue: MaterialIssue) -> str | None:
    for rejection in reversed(issue.patch_rejections):
        raw = (rejection.diagnostics or {}).get("repair_case")
        if isinstance(raw, dict) and raw.get("fingerprint"):
            return str(raw["fingerprint"])
    return None


def _repeated_failure_count(issue: MaterialIssue, *, fingerprint: str) -> int:
    count = 0
    for rejection in issue.patch_rejections:
        raw = (rejection.diagnostics or {}).get("repair_case")
        if isinstance(raw, dict) and raw.get("fingerprint") == fingerprint:
            count += 1
    return count


def _first_call_expression(evidence_text: str) -> str:
    patterns = (
        r"(?m)^\s*>\s*(?:result\s*=\s*)?((?:[A-Za-z_][A-Za-z0-9_]*\.)*[A-Za-z_][A-Za-z0-9_]*)\s*\(",
        r"(?m)^\s*E\s*\+\s+where\s+.+?=\s*((?:[A-Za-z_][A-Za-z0-9_]*\.)*[A-Za-z_][A-Za-z0-9_]*)\s*\(",
        r"\b((?:[A-Za-z_][A-Za-z0-9_]*\.)+[A-Za-z_][A-Za-z0-9_]*)\s*\(\s*\[?['\"]--help",
    )
    for pattern in patterns:
        match = re.search(pattern, evidence_text)
        if match:
            candidate = match.group(1)
            if candidate not in {"assert", "print"}:
                return candidate
    return ""


def _observed_output_channel(evidence_text: str) -> str | None:
    if "readouterr().out" in evidence_text or "stdout" in evidence_text.casefold():
        return "stdout"
    if "readouterr().err" in evidence_text or "stderr" in evidence_text.casefold():
        return "stderr"
    return None


def _looks_like_validation_surface(path: str) -> bool:
    normalized = path.replace("\\", "/")
    return _looks_like_test_path(path) or "/validators/" in f"/{normalized}"


def _looks_like_test_path(path: str) -> bool:
    normalized = path.replace("\\", "/")
    name = normalized.rsplit("/", 1)[-1]
    return normalized.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py") or "/tests/" in f"/{normalized}")


def _target_ref(
    path: str,
    *,
    role: Literal["symptom", "provider", "related", "candidate"],
    source: str,
    confidence: float,
) -> TargetRef:
    return TargetRef(path=path, role=role, kind=_target_kind(path), source=source, confidence=confidence)


def _target_kind(path: str) -> str:
    if _looks_like_test_path(path):
        return "test_file"
    if path.endswith(".py"):
        return "python_file"
    if path.endswith((".toml", ".cfg", ".ini", ".yaml", ".yml", ".json")):
        return "config_file"
    return "file"


def _known_paths(raw: dict[str, Any]) -> set[str]:
    return {item.path for item in raw.get("manifest").files} | {item.path for item in raw.get("generated_files", [])}


def _issue_text(issue: MaterialIssue) -> str:
    return "\n".join(_nested_strings([issue.issue_type, issue.target_path or "", issue.details]))


def _nested_strings(value: object, *, depth: int = 0) -> list[str]:
    if depth > 6:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        strings: list[str] = []
        for item in value.values():
            strings.extend(_nested_strings(item, depth=depth + 1))
        return strings
    if isinstance(value, list):
        strings: list[str] = []
        for item in value:
            strings.extend(_nested_strings(item, depth=depth + 1))
        return strings
    return []


def _compact(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    suffix = "...[truncated]"
    if limit <= len(suffix):
        return value[:limit]
    return f"{value[: limit - len(suffix)]}{suffix}"


def _dedupe_targets(targets: list[TargetRef]) -> list[TargetRef]:
    seen: set[str] = set()
    result: list[TargetRef] = []
    for target in targets:
        if not target.path or target.path in seen:
            continue
        seen.add(target.path)
        result.append(target)
    return result


def _dedupe_obligations(obligations: list[RepairObligation]) -> list[RepairObligation]:
    seen: set[str] = set()
    result: list[RepairObligation] = []
    for obligation in obligations:
        key = f"{obligation.kind}:{obligation.obligation_id}"
        if key in seen:
            continue
        seen.add(key)
        result.append(obligation)
    return result


def _dedupe_list(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result
