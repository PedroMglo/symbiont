"""Kernel-owned repair request contract for the material builder.

The material builder is a proposal author, not the owner of repair strategy.
This module compiles the only evidence envelope that may cross from the Repair
Control Plane into the builder repair lane.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, model_validator

from material_execution_kernel.repair_case import RepairCase
from material_execution_kernel.types import Identifier, MaterialIssue, MaterialKernelModel, Sha256


_GENERIC_EVIDENCE_KEYS = {
    "stderr_excerpt",
    "stderr_preview",
    "stdout_excerpt",
    "stdout_preview",
    "missing_name",
    "name",
    "target_file_missing",
    "last_patch_rejection",
}


class BuilderRepairRequest(MaterialKernelModel):
    schema_version: Literal["builder_repair_request.v0.1"] = "builder_repair_request.v0.1"
    issue_id: Identifier
    repair_case_id: Identifier
    target_path: str = Field(min_length=1, max_length=4096)
    expected_old_sha256: Sha256
    validation_profile: str | None = Field(default=None, max_length=128)
    issue_contract: dict[str, Any] = Field(default_factory=dict, max_length=64)
    current_context: dict[str, Any] = Field(default_factory=dict, max_length=64)
    command_evidence: dict[str, Any] = Field(default_factory=dict, max_length=64)
    previous_patch_rejections: list[dict[str, Any]] = Field(default_factory=list, max_length=32)
    target_resolution: dict[str, Any] | None = None
    allowed_actions: list[str] = Field(default_factory=list, max_length=16)
    forbidden_actions: list[str] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def require_repair_case_evidence(self) -> "BuilderRepairRequest":
        if self.command_evidence.get("repair_case_id") != self.repair_case_id:
            raise ValueError("builder repair request evidence must be anchored to the repair case")
        if "raw_issue_bundle" in self.command_evidence:
            raise ValueError("raw issue bundles are forbidden across the builder repair boundary")
        return self


def compile_builder_repair_request(
    *,
    issue: MaterialIssue,
    repair_case: RepairCase,
    target_path: str,
    expected_old_sha256: str,
    current_context: dict[str, Any],
    validation_profile: str | None,
    issue_contract: dict[str, Any],
    previous_patch_rejections: list[dict[str, Any]],
    target_resolution: dict[str, Any] | None,
    repair_arbiter: dict[str, Any],
) -> BuilderRepairRequest:
    """Compile a builder request from RepairCase authority, not raw issue bags."""

    context = dict(current_context)
    context["repair_case"] = repair_case.model_dump(mode="json")
    context["repair_obligations"] = [item.model_dump(mode="json") for item in repair_case.obligations]
    context["success_criteria"] = [item.model_dump(mode="json") for item in repair_case.success_criteria]
    context["repair_authority"] = {
        "schema_version": "repair_authority.v0.1",
        "repair_case_id": repair_case.case_id,
        "root_cause_kind": repair_case.root_cause_kind,
        "allowed_actions": list(repair_case.allowed_actions),
        "forbidden_actions": list(repair_case.forbidden_actions),
        "stop_conditions": list(repair_case.stop_conditions),
    }

    return BuilderRepairRequest(
        issue_id=issue.issue_id,
        repair_case_id=repair_case.case_id,
        target_path=target_path,
        expected_old_sha256=expected_old_sha256,
        validation_profile=validation_profile,
        issue_contract=dict(issue_contract),
        current_context=context,
        command_evidence=_normalized_command_evidence(
            issue=issue,
            repair_case=repair_case,
            validation_profile=validation_profile,
            repair_arbiter=repair_arbiter,
        ),
        previous_patch_rejections=list(previous_patch_rejections),
        target_resolution=target_resolution,
        allowed_actions=list(repair_case.allowed_actions),
        forbidden_actions=list(repair_case.forbidden_actions),
    )


def _normalized_command_evidence(
    *,
    issue: MaterialIssue,
    repair_case: RepairCase,
    validation_profile: str | None,
    repair_arbiter: dict[str, Any],
) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "schema_version": "builder_repair_evidence.v0.1",
        "issue_id": issue.issue_id,
        "issue_type": issue.issue_type,
        "repair_case_id": repair_case.case_id,
        "repair_case_status": repair_case.status,
        "root_cause_kind": repair_case.root_cause_kind,
        "validation_profile": validation_profile,
        "primary_repair_target": (
            repair_case.primary_repair_target.model_dump(mode="json")
            if repair_case.primary_repair_target is not None
            else None
        ),
        "symptom_targets": [target.model_dump(mode="json") for target in repair_case.symptom_targets],
        "normalized_evidence": [item.model_dump(mode="json") for item in repair_case.evidence],
        "obligations": [item.model_dump(mode="json") for item in repair_case.obligations],
        "success_criteria": [item.model_dump(mode="json") for item in repair_case.success_criteria],
        "progress_state": repair_case.progress_state.model_dump(mode="json"),
        "retry_budget": repair_case.retry_budget.model_dump(mode="json"),
        "repair_arbiter": repair_arbiter,
    }
    for key in sorted(_GENERIC_EVIDENCE_KEYS):
        if key in issue.details:
            evidence[key] = _json_safe(issue.details[key])
    return evidence


def _json_safe(value: Any) -> Any:
    if isinstance(value, str):
        return _compact(value, 4096)
    if isinstance(value, bool | int | float) or value is None:
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return _compact(str(value), 4096)


def _compact(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    suffix = "...[truncated]"
    if limit <= len(suffix):
        return value[:limit]
    return f"{value[: limit - len(suffix)]}{suffix}"
