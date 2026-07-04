"""Typed client boundary for material builder proposals.

The real material builder service owns LLM prompts and proposal generation.
This module defines only the kernel-side transport contract; it intentionally
does not import ``agents/material_builder`` internals.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx


class MaterialBuilderUnavailable(RuntimeError):
    """Raised when no material builder backend is configured for this session."""


@dataclass(frozen=True)
class MaterialRequirementProposal:
    requirement_id: str
    description: str
    source: str = "user"
    capability_refs: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PlannedMaterialFile:
    path: str
    purpose: str
    kind: str = "other"
    requirement_refs: list[str] = field(default_factory=list)
    contract_refs: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class MaterialInterfaceProposal:
    interface_id: str
    kind: str
    name: str
    purpose: str
    requirement_refs: list[str] = field(default_factory=list)
    file_refs: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class MaterialArtifactExpectationProposal:
    artifact_id: str
    root: str
    purpose: str
    requirement_refs: list[str] = field(default_factory=list)
    contract_refs: list[str] = field(default_factory=list)
    file_refs: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class MaterialCompletionCriterionProposal:
    criterion_id: str
    description: str
    requirement_refs: list[str] = field(default_factory=list)
    validation_refs: list[str] = field(default_factory=list)
    artifact_refs: list[str] = field(default_factory=list)
    contract_refs: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class MaterialDependencyStrategyProposal:
    declared_dependency_files: list[str] = field(default_factory=list)
    external_dependencies: list[str] = field(default_factory=list)
    install_profiles: list[str] = field(default_factory=list)
    lockfiles: list[str] = field(default_factory=list)
    native_builds_required: bool = False
    network_required: str = "none"
    requirement_refs: list[str] = field(default_factory=list)
    contract_refs: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class MaterialPlanProposal:
    project_root: str
    files: list[PlannedMaterialFile]
    requirements: list[MaterialRequirementProposal] = field(default_factory=list)
    intended_interfaces: list[MaterialInterfaceProposal] = field(default_factory=list)
    required_validation_profiles: list[str] = field(default_factory=list)
    optional_validation_profiles: list[str] = field(default_factory=list)
    validation_commands: dict[str, "MaterialValidationCommandProposal"] = field(default_factory=dict)
    artifact_expectations: list[MaterialArtifactExpectationProposal] = field(default_factory=list)
    completion_criteria: list[MaterialCompletionCriterionProposal] = field(default_factory=list)
    dependency_strategy: MaterialDependencyStrategyProposal = field(default_factory=MaterialDependencyStrategyProposal)
    architecture_notes: list[str] = field(default_factory=list)
    variation_reason: str | None = None
    model_route: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class MaterialValidationCommandProposal:
    profile: str
    argv: list[str]
    cwd: str = "."
    timeout_seconds: int = 120
    env: dict[str, str] = field(default_factory=dict)
    purpose: str | None = None
    requirement_refs: list[str] = field(default_factory=list)
    contract_refs: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class GeneratedMaterialFile:
    path: str
    content: str
    sha256: str
    kind: str = "other"

    @classmethod
    def from_text(cls, *, path: str, content: str, kind: str = "other") -> "GeneratedMaterialFile":
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        return cls(path=path, content=content, sha256=f"sha256:{digest}", kind=kind)


@dataclass(frozen=True)
class MaterialPatchProposal:
    issue_id: str
    target_path: str
    expected_old_sha256: str
    unified_diff: str
    requirement_refs: list[str] = field(default_factory=list)
    contract_refs: list[str] = field(default_factory=list)
    rationale: str | None = None


@dataclass(frozen=True)
class MaterialReplacementProposal:
    issue_id: str
    target_path: str
    expected_old_sha256: str
    replacement_content: str
    replacement_sha256: str
    requirement_refs: list[str] = field(default_factory=list)
    contract_refs: list[str] = field(default_factory=list)
    rationale: str | None = None


@dataclass(frozen=True)
class MaterialPatchSetProposal:
    issue_id: str
    patches: list[MaterialPatchProposal]
    requirement_refs: list[str] = field(default_factory=list)
    contract_refs: list[str] = field(default_factory=list)
    rationale: str | None = None


@dataclass(frozen=True)
class MaterialRegenerateFromContractProposal:
    issue_id: str
    contract_refs: list[str]
    requirement_refs: list[str]
    target_paths: list[str]
    rationale: str


@dataclass(frozen=True)
class MaterialRepairProposal:
    patch: MaterialPatchProposal | None = None
    patch_set: MaterialPatchSetProposal | None = None
    replacement: MaterialReplacementProposal | None = None
    regeneration: MaterialRegenerateFromContractProposal | None = None

    def primary_patch(self) -> MaterialPatchProposal | None:
        if self.patch is not None:
            return self.patch
        if self.patch_set and self.patch_set.patches:
            return self.patch_set.patches[0]
        return None


@dataclass(frozen=True)
class MaterialRepairCriticAdvisory:
    advisory_only: bool = True
    findings: list[dict[str, object]] = field(default_factory=list)
    likely_root_cause: str | None = None
    recommended_strategy: str = "replacement"
    confidence: float = 0.0
    model_route: dict[str, object] = field(default_factory=dict)
    lane_metrics: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class PlanCoverageIssueProposal:
    issue_type: str
    severity: str
    message: str
    details: dict[str, object] = field(default_factory=dict)
    acceptance: list[str] = field(default_factory=list)


class MaterialBuilderClient(Protocol):
    def create_plan(
        self,
        *,
        session_id: str,
        task_id: str,
        working_query: str,
        required_capabilities: list[str],
        constraints: dict[str, object],
        variation_nonce: str,
        original_query: str | None = None,
        original_language: str | None = None,
        language_context: dict[str, object] | None = None,
    ) -> MaterialPlanProposal:
        """Return a structured material plan proposal."""

    def repair_plan(
        self,
        *,
        session_id: str,
        task_id: str,
        working_query: str,
        required_capabilities: list[str],
        constraints: dict[str, object],
        plan: MaterialPlanProposal,
        coverage_issues: list[PlanCoverageIssueProposal],
        original_query: str | None = None,
        original_language: str | None = None,
        language_context: dict[str, object] | None = None,
    ) -> MaterialPlanProposal:
        """Return a repaired material plan proposal."""

    def generate_files(
        self,
        *,
        session_id: str,
        task_id: str,
        plan: MaterialPlanProposal,
        target_file_paths: list[str] | None = None,
    ) -> list[GeneratedMaterialFile]:
        """Return structured file proposals for a plan."""

    def propose_patch(
        self,
        *,
        session_id: str,
        task_id: str,
        plan: MaterialPlanProposal,
        issue_id: str,
        issue_contract: dict[str, object],
        target_path: str,
        expected_old_sha256: str,
        current_content: str,
        current_context: dict[str, object],
        validation_profile: str | None,
        command_evidence: dict[str, object],
        previous_patch_rejections: list[dict[str, object]],
        patch_blueprints: list[dict[str, object]],
        target_resolution: dict[str, object] | None = None,
        patch_set_blueprints: list[dict[str, object]] | None = None,
        replacement_blueprints: list[dict[str, object]] | None = None,
        regeneration_blueprints: list[dict[str, object]] | None = None,
    ) -> MaterialRepairProposal:
        """Return one structured repair proposal for a validation issue."""

    def critique_repair(
        self,
        *,
        session_id: str,
        task_id: str,
        plan: MaterialPlanProposal,
        issue_id: str,
        issue_contract: dict[str, object],
        target_path: str,
        current_content: str,
        current_context: dict[str, object],
        command_evidence: dict[str, object],
        previous_patch_rejections: list[dict[str, object]],
        repair_arbiter: dict[str, object],
    ) -> MaterialRepairCriticAdvisory:
        """Return optional advisory critique for a repair attempt."""


class UnavailableMaterialBuilderClient:
    def create_plan(
        self,
        *,
        session_id: str,
        task_id: str,
        working_query: str,
        required_capabilities: list[str],
        constraints: dict[str, object],
        variation_nonce: str,
        original_query: str | None = None,
        original_language: str | None = None,
        language_context: dict[str, object] | None = None,
    ) -> MaterialPlanProposal:
        raise MaterialBuilderUnavailable("material builder client is not configured")

    def generate_files(
        self,
        *,
        session_id: str,
        task_id: str,
        plan: MaterialPlanProposal,
        target_file_paths: list[str] | None = None,
    ) -> list[GeneratedMaterialFile]:
        raise MaterialBuilderUnavailable("material builder client is not configured")

    def repair_plan(
        self,
        *,
        session_id: str,
        task_id: str,
        working_query: str,
        required_capabilities: list[str],
        constraints: dict[str, object],
        plan: MaterialPlanProposal,
        coverage_issues: list[PlanCoverageIssueProposal],
        original_query: str | None = None,
        original_language: str | None = None,
        language_context: dict[str, object] | None = None,
    ) -> MaterialPlanProposal:
        raise MaterialBuilderUnavailable("material builder client is not configured")

    def propose_patch(
        self,
        *,
        session_id: str,
        task_id: str,
        plan: MaterialPlanProposal,
        issue_id: str,
        issue_contract: dict[str, object],
        target_path: str,
        expected_old_sha256: str,
        current_content: str,
        current_context: dict[str, object],
        validation_profile: str | None,
        command_evidence: dict[str, object],
        previous_patch_rejections: list[dict[str, object]],
        patch_blueprints: list[dict[str, object]],
        target_resolution: dict[str, object] | None = None,
        patch_set_blueprints: list[dict[str, object]] | None = None,
        replacement_blueprints: list[dict[str, object]] | None = None,
        regeneration_blueprints: list[dict[str, object]] | None = None,
    ) -> MaterialRepairProposal:
        raise MaterialBuilderUnavailable("material builder client is not configured")

    def critique_repair(
        self,
        *,
        session_id: str,
        task_id: str,
        plan: MaterialPlanProposal,
        issue_id: str,
        issue_contract: dict[str, object],
        target_path: str,
        current_content: str,
        current_context: dict[str, object],
        command_evidence: dict[str, object],
        previous_patch_rejections: list[dict[str, object]],
        repair_arbiter: dict[str, object],
    ) -> MaterialRepairCriticAdvisory:
        raise MaterialBuilderUnavailable("material builder client is not configured")


class HTTPMaterialBuilderClient:
    """HTTP transport client for the material_builder agent."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str = "",
        timeout_seconds: float = 120.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds
        self._blueprint_file_contents: dict[tuple[str, str], dict[str, str]] = {}
        self._model_routes: dict[tuple[str, str, str], dict[str, object]] = {}

    def create_plan(
        self,
        *,
        session_id: str,
        task_id: str,
        working_query: str,
        required_capabilities: list[str],
        constraints: dict[str, object],
        variation_nonce: str,
        original_query: str | None = None,
        original_language: str | None = None,
        language_context: dict[str, object] | None = None,
    ) -> MaterialPlanProposal:
        payload = {
            "task_id": task_id,
            "working_query": working_query,
            "original_query": original_query,
            "original_language": original_language,
            "working_language": "en",
            "language_context": language_context or {},
            "required_capabilities": required_capabilities,
            "constraints": constraints,
            "variation_nonce": variation_nonce,
        }
        data = self._post("/v1/material-builder/plan", payload)
        plan = data["plan"]
        model_route = _route_with_metrics(data)
        file_contents = data.get("file_contents") if isinstance(data.get("file_contents"), dict) else {}
        self._blueprint_file_contents[(session_id, task_id)] = {
            str(path): str(content) for path, content in file_contents.items()
        }
        self._model_routes[(session_id, task_id, "plan")] = {str(key): value for key, value in model_route.items()}
        return MaterialPlanProposal(
            project_root=str(plan["project_root"]),
            requirements=_requirements(plan.get("requirements")),
            files=[
                PlannedMaterialFile(
                    path=str(item["path"]),
                    purpose=str(item.get("purpose") or ""),
                    kind=str(item.get("kind") or "other"),
                    requirement_refs=[str(ref) for ref in item.get("requirement_refs", [])],
                    contract_refs=[str(ref) for ref in item.get("contract_refs", [])],
                )
                for item in plan.get("files", [])
            ],
            intended_interfaces=_intended_interfaces(plan.get("intended_interfaces")),
            required_validation_profiles=[str(item) for item in plan.get("required_validation_profiles", [])],
            optional_validation_profiles=[str(item) for item in plan.get("optional_validation_profiles", [])],
            validation_commands=_validation_commands(plan.get("validation_commands")),
            artifact_expectations=_artifact_expectations(plan.get("artifact_expectations")),
            completion_criteria=_completion_criteria(plan.get("completion_criteria")),
            dependency_strategy=_dependency_strategy(plan.get("dependency_strategy")),
            architecture_notes=[str(item) for item in plan.get("architecture_notes", [])],
            variation_reason=plan.get("variation_reason"),
            model_route=self._model_routes[(session_id, task_id, "plan")],
        )

    def repair_plan(
        self,
        *,
        session_id: str,
        task_id: str,
        working_query: str,
        required_capabilities: list[str],
        constraints: dict[str, object],
        plan: MaterialPlanProposal,
        coverage_issues: list[PlanCoverageIssueProposal],
        original_query: str | None = None,
        original_language: str | None = None,
        language_context: dict[str, object] | None = None,
    ) -> MaterialPlanProposal:
        payload = {
            "session_id": session_id,
            "task_id": task_id,
            "working_query": working_query,
            "original_query": original_query,
            "original_language": original_language,
            "working_language": "en",
            "language_context": language_context or {},
            "required_capabilities": required_capabilities,
            "constraints": constraints,
            "plan": _plan_payload(plan),
            "coverage_issues": [_coverage_issue_payload(issue) for issue in coverage_issues],
        }
        data = self._post("/v1/material-builder/plan/repair", payload)
        repaired = data["plan"]
        model_route = _route_with_metrics(data)
        self._model_routes[(session_id, task_id, "plan_repair")] = {
            str(key): value for key, value in model_route.items()
        }
        return MaterialPlanProposal(
            project_root=str(repaired["project_root"]),
            requirements=_requirements(repaired.get("requirements")),
            files=[
                PlannedMaterialFile(
                    path=str(item["path"]),
                    purpose=str(item.get("purpose") or ""),
                    kind=str(item.get("kind") or "other"),
                    requirement_refs=[str(ref) for ref in item.get("requirement_refs", [])],
                    contract_refs=[str(ref) for ref in item.get("contract_refs", [])],
                )
                for item in repaired.get("files", [])
            ],
            intended_interfaces=_intended_interfaces(repaired.get("intended_interfaces")),
            required_validation_profiles=[str(item) for item in repaired.get("required_validation_profiles", [])],
            optional_validation_profiles=[str(item) for item in repaired.get("optional_validation_profiles", [])],
            validation_commands=_validation_commands(repaired.get("validation_commands")),
            artifact_expectations=_artifact_expectations(repaired.get("artifact_expectations")),
            completion_criteria=_completion_criteria(repaired.get("completion_criteria")),
            dependency_strategy=_dependency_strategy(repaired.get("dependency_strategy")),
            architecture_notes=[str(item) for item in repaired.get("architecture_notes", [])],
            variation_reason=repaired.get("variation_reason"),
            model_route=self._model_routes[(session_id, task_id, "plan_repair")],
        )

    def generate_files(
        self,
        *,
        session_id: str,
        task_id: str,
        plan: MaterialPlanProposal,
        target_file_paths: list[str] | None = None,
    ) -> list[GeneratedMaterialFile]:
        file_contents = self._blueprint_file_contents.get((session_id, task_id), {})
        payload = {
            "session_id": session_id,
            "task_id": task_id,
            "plan": _plan_payload(plan),
            "target_file_paths": target_file_paths or [],
            "file_contents": file_contents,
            "source_plan_ref": session_id,
        }
        data = self._post("/v1/material-builder/files", payload)
        model_route = _route_with_metrics(data)
        self._model_routes[(session_id, task_id, "files")] = {str(key): value for key, value in model_route.items()}
        return [
            GeneratedMaterialFile(
                path=str(item["path"]),
                content=str(item["content"]),
                sha256=str(item["sha256"]),
                kind=str(item.get("kind") or "other"),
            )
            for item in data.get("files", [])
        ]

    def propose_patch(
        self,
        *,
        session_id: str,
        task_id: str,
        plan: MaterialPlanProposal,
        issue_id: str,
        issue_contract: dict[str, object],
        target_path: str,
        expected_old_sha256: str,
        current_content: str,
        current_context: dict[str, object],
        validation_profile: str | None,
        command_evidence: dict[str, object],
        previous_patch_rejections: list[dict[str, object]],
        patch_blueprints: list[dict[str, object]],
        target_resolution: dict[str, object] | None = None,
        patch_set_blueprints: list[dict[str, object]] | None = None,
        replacement_blueprints: list[dict[str, object]] | None = None,
        regeneration_blueprints: list[dict[str, object]] | None = None,
    ) -> MaterialRepairProposal:
        payload: dict[str, Any] = {
            "session_id": session_id,
            "task_id": task_id,
            "plan": _plan_payload(plan),
            "issue_id": issue_id,
            "issue": issue_contract,
            "target_path": target_path,
            "expected_old_sha256": expected_old_sha256,
            "current_content": current_content,
            "current_context": current_context,
            "validation_profile": validation_profile,
            "command_evidence": command_evidence,
            "previous_patch_rejections": previous_patch_rejections,
            "patch_blueprints": patch_blueprints,
            "target_resolution": target_resolution,
            "patch_set_blueprints": patch_set_blueprints or [],
            "replacement_blueprints": replacement_blueprints or [],
            "regeneration_blueprints": regeneration_blueprints or [],
        }
        data = self._post("/v1/material-builder/patch", payload)
        model_route = _route_with_metrics(data)
        self._model_routes[(session_id, task_id, "patch")] = {str(key): value for key, value in model_route.items()}
        patch = data.get("patch") if isinstance(data.get("patch"), dict) else None
        patch_set = data.get("patch_set") if isinstance(data.get("patch_set"), dict) else None
        replacement = data.get("replacement") if isinstance(data.get("replacement"), dict) else None
        regeneration = data.get("regeneration") if isinstance(data.get("regeneration"), dict) else None
        return MaterialRepairProposal(
            patch=_patch_proposal(patch) if patch else None,
            patch_set=_patch_set_proposal(patch_set) if patch_set else None,
            replacement=_replacement_proposal(replacement) if replacement else None,
            regeneration=_regeneration_proposal(regeneration) if regeneration else None,
        )

    def critique_repair(
        self,
        *,
        session_id: str,
        task_id: str,
        plan: MaterialPlanProposal,
        issue_id: str,
        issue_contract: dict[str, object],
        target_path: str,
        current_content: str,
        current_context: dict[str, object],
        command_evidence: dict[str, object],
        previous_patch_rejections: list[dict[str, object]],
        repair_arbiter: dict[str, object],
    ) -> MaterialRepairCriticAdvisory:
        payload: dict[str, Any] = {
            "session_id": session_id,
            "task_id": task_id,
            "plan": _plan_payload(plan),
            "issue_id": issue_id,
            "issue": issue_contract,
            "target_path": target_path,
            "current_content": current_content,
            "current_context": current_context,
            "command_evidence": command_evidence,
            "previous_patch_rejections": previous_patch_rejections,
            "repair_arbiter": repair_arbiter,
        }
        data = self._post("/v1/material-builder/repair/critic", payload)
        model_route = _route_with_metrics(data)
        self._model_routes[(session_id, task_id, "critic")] = {str(key): value for key, value in model_route.items()}
        return MaterialRepairCriticAdvisory(
            advisory_only=bool(data.get("advisory_only", True)),
            findings=[dict(item) for item in data.get("findings", []) if isinstance(item, dict)],
            likely_root_cause=str(data["likely_root_cause"]) if data.get("likely_root_cause") is not None else None,
            recommended_strategy=str(data.get("recommended_strategy") or "replacement"),
            confidence=float(data.get("confidence") or 0.0),
            model_route={str(key): value for key, value in dict(data.get("model_route") or {}).items()},
            lane_metrics={str(key): value for key, value in dict(data.get("lane_metrics") or {}).items()},
        )

    def model_route(self, *, session_id: str, task_id: str, phase: str) -> dict[str, object]:
        return dict(self._model_routes.get((session_id, task_id, phase), {}))

    def _post(self, path: str, payload: dict[str, object]) -> dict[str, object]:
        headers = _auth_headers(self._api_key)
        try:
            response = httpx.post(
                f"{self._base_url}{path}",
                json=payload,
                headers=headers,
                timeout=self._timeout_seconds,
            )
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPStatusError as exc:
            detail = _error_detail(exc.response)
            raise MaterialBuilderUnavailable(detail) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise MaterialBuilderUnavailable(str(exc)) from exc
        if not isinstance(data, dict):
            raise MaterialBuilderUnavailable("material builder returned a non-object response")
        return data


def _auth_headers(api_key: str) -> dict[str, str]:
    if not api_key:
        return {}
    return {"Authorization": f"Bearer {api_key}", "X-API-Key": api_key}


def _route_with_metrics(data: dict[str, object]) -> dict[str, object]:
    route = data.get("model_route") if isinstance(data.get("model_route"), dict) else {}
    metrics = data.get("lane_metrics") if isinstance(data.get("lane_metrics"), dict) else {}
    result = {str(key): value for key, value in dict(route).items()}
    if metrics:
        result["lane_metrics"] = {str(key): value for key, value in metrics.items()}
    return result


def _plan_payload(plan: MaterialPlanProposal) -> dict[str, object]:
    return {
        "project_root": plan.project_root,
        "requirements": [
            {
                "requirement_id": item.requirement_id,
                "description": item.description,
                "source": item.source,
                "capability_refs": item.capability_refs,
            }
            for item in plan.requirements
        ],
        "files": [
            {
                "path": item.path,
                "purpose": item.purpose,
                "kind": item.kind,
                "requirement_refs": item.requirement_refs,
                "contract_refs": item.contract_refs,
            }
            for item in plan.files
        ],
        "intended_interfaces": [
            {
                "interface_id": item.interface_id,
                "kind": item.kind,
                "name": item.name,
                "purpose": item.purpose,
                "requirement_refs": item.requirement_refs,
                "file_refs": item.file_refs,
            }
            for item in plan.intended_interfaces
        ],
        "required_validation_profiles": plan.required_validation_profiles,
        "optional_validation_profiles": plan.optional_validation_profiles,
        "validation_commands": {
            profile: {
                "profile": command.profile,
                "argv": command.argv,
                "cwd": command.cwd,
                "timeout_seconds": command.timeout_seconds,
                "env": command.env,
                "purpose": command.purpose,
                "requirement_refs": command.requirement_refs,
                "contract_refs": command.contract_refs,
            }
            for profile, command in plan.validation_commands.items()
        },
        "artifact_expectations": [
            {
                "artifact_id": item.artifact_id,
                "root": item.root,
                "purpose": item.purpose,
                "requirement_refs": item.requirement_refs,
                "contract_refs": item.contract_refs,
                "file_refs": item.file_refs,
            }
            for item in plan.artifact_expectations
        ],
        "completion_criteria": [
            {
                "criterion_id": item.criterion_id,
                "description": item.description,
                "requirement_refs": item.requirement_refs,
                "validation_refs": item.validation_refs,
                "artifact_refs": item.artifact_refs,
                "contract_refs": item.contract_refs,
            }
            for item in plan.completion_criteria
        ],
        "dependency_strategy": {
            "declared_dependency_files": plan.dependency_strategy.declared_dependency_files,
            "external_dependencies": plan.dependency_strategy.external_dependencies,
            "install_profiles": plan.dependency_strategy.install_profiles,
            "lockfiles": plan.dependency_strategy.lockfiles,
            "native_builds_required": plan.dependency_strategy.native_builds_required,
            "network_required": plan.dependency_strategy.network_required,
            "requirement_refs": plan.dependency_strategy.requirement_refs,
            "contract_refs": plan.dependency_strategy.contract_refs,
        },
        "architecture_notes": plan.architecture_notes,
        "variation_reason": plan.variation_reason,
    }


def _coverage_issue_payload(issue: PlanCoverageIssueProposal) -> dict[str, object]:
    return {
        "issue_type": issue.issue_type,
        "severity": issue.severity,
        "message": issue.message,
        "details": issue.details,
        "acceptance": issue.acceptance,
    }


def _patch_proposal(raw: dict[str, object]) -> MaterialPatchProposal:
    return MaterialPatchProposal(
        issue_id=str(raw["issue_id"]),
        target_path=str(raw["target_path"]),
        expected_old_sha256=str(raw["expected_old_sha256"]),
        unified_diff=str(raw["unified_diff"]),
        requirement_refs=[str(ref) for ref in raw.get("requirement_refs", [])],
        contract_refs=[str(ref) for ref in raw.get("contract_refs", [])],
        rationale=str(raw["rationale"]) if raw.get("rationale") is not None else None,
    )


def _patch_set_proposal(raw: dict[str, object]) -> MaterialPatchSetProposal:
    patches = raw.get("patches") if isinstance(raw.get("patches"), list) else []
    return MaterialPatchSetProposal(
        issue_id=str(raw["issue_id"]),
        patches=[_patch_proposal(item) for item in patches if isinstance(item, dict)],
        requirement_refs=[str(ref) for ref in raw.get("requirement_refs", [])],
        contract_refs=[str(ref) for ref in raw.get("contract_refs", [])],
        rationale=str(raw["rationale"]) if raw.get("rationale") is not None else None,
    )


def _replacement_proposal(raw: dict[str, object]) -> MaterialReplacementProposal:
    return MaterialReplacementProposal(
        issue_id=str(raw["issue_id"]),
        target_path=str(raw["target_path"]),
        expected_old_sha256=str(raw["expected_old_sha256"]),
        replacement_content=str(raw["replacement_content"]),
        replacement_sha256=str(raw["replacement_sha256"]),
        requirement_refs=[str(ref) for ref in raw.get("requirement_refs", [])],
        contract_refs=[str(ref) for ref in raw.get("contract_refs", [])],
        rationale=str(raw["rationale"]) if raw.get("rationale") is not None else None,
    )


def _regeneration_proposal(raw: dict[str, object]) -> MaterialRegenerateFromContractProposal:
    return MaterialRegenerateFromContractProposal(
        issue_id=str(raw["issue_id"]),
        contract_refs=[str(ref) for ref in raw.get("contract_refs", [])],
        requirement_refs=[str(ref) for ref in raw.get("requirement_refs", [])],
        target_paths=[str(path) for path in raw.get("target_paths", [])],
        rationale=str(raw.get("rationale") or ""),
    )


def _validation_commands(raw: object) -> dict[str, MaterialValidationCommandProposal]:
    if not isinstance(raw, dict):
        return {}
    commands: dict[str, MaterialValidationCommandProposal] = {}
    for profile, value in raw.items():
        if not isinstance(value, dict):
            continue
        argv = value.get("argv")
        if not isinstance(argv, list):
            continue
        commands[str(profile)] = MaterialValidationCommandProposal(
            profile=str(value.get("profile") or profile),
            argv=[str(item) for item in argv],
            cwd=str(value.get("cwd") or "."),
            timeout_seconds=int(value.get("timeout_seconds") or 120),
            env={str(key): str(env_value) for key, env_value in dict(value.get("env") or {}).items()},
            purpose=str(value["purpose"]) if value.get("purpose") is not None else None,
            requirement_refs=[str(ref) for ref in value.get("requirement_refs", [])],
            contract_refs=[str(ref) for ref in value.get("contract_refs", [])],
        )
    return commands


def _dependency_strategy(raw: object) -> MaterialDependencyStrategyProposal:
    if not isinstance(raw, dict):
        return MaterialDependencyStrategyProposal()
    return MaterialDependencyStrategyProposal(
        declared_dependency_files=[str(item) for item in raw.get("declared_dependency_files", [])],
        external_dependencies=[str(item) for item in raw.get("external_dependencies", [])],
        install_profiles=[str(item) for item in raw.get("install_profiles", [])],
        lockfiles=[str(item) for item in raw.get("lockfiles", [])],
        native_builds_required=bool(raw.get("native_builds_required", False)),
        network_required=str(raw.get("network_required") or "none"),
        requirement_refs=[str(item) for item in raw.get("requirement_refs", [])],
        contract_refs=[str(item) for item in raw.get("contract_refs", [])],
    )


def _requirements(raw: object) -> list[MaterialRequirementProposal]:
    if not isinstance(raw, list):
        return []
    items: list[MaterialRequirementProposal] = []
    for value in raw:
        if not isinstance(value, dict):
            continue
        requirement_id = value.get("requirement_id")
        description = value.get("description")
        if requirement_id is None or description is None:
            continue
        items.append(
            MaterialRequirementProposal(
                requirement_id=str(requirement_id),
                description=str(description),
                source=str(value.get("source") or "user"),
                capability_refs=[str(ref) for ref in value.get("capability_refs", [])],
            )
        )
    return items


def _intended_interfaces(raw: object) -> list[MaterialInterfaceProposal]:
    if not isinstance(raw, list):
        return []
    items: list[MaterialInterfaceProposal] = []
    for value in raw:
        if not isinstance(value, dict):
            continue
        interface_id = value.get("interface_id")
        name = value.get("name")
        purpose = value.get("purpose")
        if interface_id is None or name is None or purpose is None:
            continue
        items.append(
            MaterialInterfaceProposal(
                interface_id=str(interface_id),
                kind=str(value.get("kind") or "other"),
                name=str(name),
                purpose=str(purpose),
                requirement_refs=[str(ref) for ref in value.get("requirement_refs", [])],
                file_refs=[str(ref) for ref in value.get("file_refs", [])],
            )
        )
    return items


def _artifact_expectations(raw: object) -> list[MaterialArtifactExpectationProposal]:
    if not isinstance(raw, list):
        return []
    items: list[MaterialArtifactExpectationProposal] = []
    for value in raw:
        if not isinstance(value, dict):
            continue
        artifact_id = value.get("artifact_id")
        root = value.get("root")
        purpose = value.get("purpose")
        if artifact_id is None or root is None or purpose is None:
            continue
        items.append(
            MaterialArtifactExpectationProposal(
                artifact_id=str(artifact_id),
                root=str(root),
                purpose=str(purpose),
                requirement_refs=[str(ref) for ref in value.get("requirement_refs", [])],
                contract_refs=[str(ref) for ref in value.get("contract_refs", [])],
                file_refs=[str(ref) for ref in value.get("file_refs", [])],
            )
        )
    return items


def _completion_criteria(raw: object) -> list[MaterialCompletionCriterionProposal]:
    if not isinstance(raw, list):
        return []
    items: list[MaterialCompletionCriterionProposal] = []
    for value in raw:
        if not isinstance(value, dict):
            continue
        criterion_id = value.get("criterion_id")
        description = value.get("description")
        if criterion_id is None or description is None:
            continue
        items.append(
            MaterialCompletionCriterionProposal(
                criterion_id=str(criterion_id),
                description=str(description),
                requirement_refs=[str(ref) for ref in value.get("requirement_refs", [])],
                validation_refs=[str(ref) for ref in value.get("validation_refs", [])],
                artifact_refs=[str(ref) for ref in value.get("artifact_refs", [])],
                contract_refs=[str(ref) for ref in value.get("contract_refs", [])],
            )
        )
    return items


def _error_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:1000]
    if isinstance(body, dict):
        detail = body.get("detail", body)
        return str(detail)
    return str(body)


def material_builder_client_from_env() -> MaterialBuilderClient:
    url = os.environ.get(
        "MATERIAL_EXECUTION_KERNEL_MATERIAL_BUILDER_URL",
        os.environ.get("ORC_SERVICES_MATERIAL_BUILDER_URL", "https://material-builder:8000"),
    ).strip()
    if not url:
        return UnavailableMaterialBuilderClient()
    return HTTPMaterialBuilderClient(
        base_url=url,
        api_key=_internal_api_key(),
        timeout_seconds=float(os.environ.get("MATERIAL_EXECUTION_KERNEL_BUILDER_TIMEOUT_SECONDS", "180")),
    )


def _internal_api_key() -> str:
    for name in (
        "MATERIAL_EXECUTION_KERNEL_INTERNAL_API_KEY",
        "INTERNAL_API_KEY",
        "API_KEY",
    ):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    for name in (
        "MATERIAL_EXECUTION_KERNEL_INTERNAL_API_KEY_FILE",
        "INTERNAL_API_KEY_FILE",
        "API_KEY_FILE",
    ):
        path = os.environ.get(name, "").strip()
        if not path:
            continue
        try:
            return open(path, encoding="utf-8").read().strip()
        except OSError:
            continue
    return ""
