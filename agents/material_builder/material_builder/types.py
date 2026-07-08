"""Pydantic contracts for material builder proposals."""

from __future__ import annotations

import hashlib
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


Identifier = Annotated[str, Field(min_length=3, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")]
RelativePath = Annotated[str, Field(min_length=1, max_length=4096)]
Sha256 = Annotated[str, Field(pattern=r"^sha256:[a-f0-9]{64}$")]

FileKind = Literal["python", "test", "dockerfile", "compose", "markdown", "config", "text", "other"]
ChunkRecordType = Literal["file_start", "file_chunk", "file_end"]
IssueSeverity = Literal["info", "warning", "repairable", "blocking_completion", "security_block"]
RequirementSource = Literal["user", "derived", "capability", "constraint"]
InterfaceKind = Literal["api", "cli", "worker", "service", "library", "data", "artifact", "other"]
DependencyNetworkPolicy = Literal["none", "dependency-cache", "external"]
KNOWN_VALIDATION_PROFILES = frozenset(
    {
        "artifact",
        "cli",
        "docker-compose-runtime",
        "docker-compose-static",
        "node-basic",
        "python-api",
        "python-basic",
        "python-pytest",
        "stateful-postgres",
        "stateful-redis",
        "worker-queue",
    }
)


class MaterialBuilderModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class MaterialPlanRequest(MaterialBuilderModel):
    task_id: Identifier
    working_query: str = Field(min_length=1, max_length=20000)
    original_query: str | None = Field(default=None, max_length=20000)
    original_language: str | None = Field(default=None, min_length=2, max_length=32)
    working_language: Literal["en"] = "en"
    language_context: dict[str, Any] = Field(default_factory=dict, max_length=64)
    required_capabilities: list[str] = Field(default_factory=list, max_length=128)
    constraints: dict[str, object] = Field(default_factory=dict)
    variation_nonce: str | None = Field(default=None, max_length=128)


class MaterialFileSpec(MaterialBuilderModel):
    path: RelativePath
    purpose: str = Field(min_length=1, max_length=8192)
    kind: FileKind = "other"
    max_tokens: int = Field(default=1200, ge=1, le=20000)
    prefer_chunked: bool = False
    depends_on: list[RelativePath] = Field(default_factory=list, max_length=128)
    requirement_refs: list[Identifier] = Field(default_factory=list, max_length=64)
    contract_refs: list[Identifier] = Field(default_factory=list, max_length=64)

    @field_validator("path")
    @classmethod
    def path_must_be_relative(cls, value: str) -> str:
        return _validate_relative_path(value)


class MaterialValidationCommand(MaterialBuilderModel):
    profile: str = Field(min_length=1, max_length=128)
    argv: list[str] = Field(min_length=1, max_length=256)
    cwd: RelativePath = "."
    timeout_seconds: int = Field(default=120, ge=1, le=7200)
    env: dict[str, str] = Field(default_factory=dict, max_length=64)
    purpose: str | None = Field(default=None, max_length=2048)
    requirement_refs: list[Identifier] = Field(default_factory=list, max_length=64)
    contract_refs: list[Identifier] = Field(default_factory=list, max_length=64)

    @field_validator("profile")
    @classmethod
    def validation_profile_must_be_known(cls, value: str) -> str:
        if value not in KNOWN_VALIDATION_PROFILES:
            allowed = ", ".join(sorted(KNOWN_VALIDATION_PROFILES))
            raise ValueError(f"unknown validation profile: {value}; allowed profiles: {allowed}")
        return value

    @field_validator("argv")
    @classmethod
    def argv_tokens_must_be_non_empty(cls, value: list[str]) -> list[str]:
        for token in value:
            if not str(token).strip():
                raise ValueError("validation command argv tokens must be non-empty")
        return value

    @field_validator("cwd")
    @classmethod
    def cwd_must_be_relative(cls, value: str) -> str:
        return _validate_relative_path(value)


class MaterialRequirementSpec(MaterialBuilderModel):
    requirement_id: Identifier
    description: str = Field(min_length=1, max_length=2048)
    source: RequirementSource = "user"
    capability_refs: list[str] = Field(default_factory=list, max_length=64)


class MaterialInterfaceSpec(MaterialBuilderModel):
    interface_id: Identifier
    kind: InterfaceKind = "other"
    name: str = Field(min_length=1, max_length=255)
    purpose: str = Field(min_length=1, max_length=2048)
    requirement_refs: list[Identifier] = Field(default_factory=list, max_length=64)
    file_refs: list[RelativePath] = Field(default_factory=list, max_length=128)

    @field_validator("file_refs")
    @classmethod
    def file_refs_must_be_relative(cls, value: list[str]) -> list[str]:
        return [_validate_relative_path(item) for item in value]


class MaterialArtifactExpectationSpec(MaterialBuilderModel):
    artifact_id: Identifier
    root: RelativePath
    purpose: str = Field(min_length=1, max_length=2048)
    requirement_refs: list[Identifier] = Field(default_factory=list, max_length=64)
    contract_refs: list[Identifier] = Field(default_factory=list, max_length=64)
    file_refs: list[RelativePath] = Field(default_factory=list, max_length=128)

    @field_validator("root")
    @classmethod
    def root_must_be_relative(cls, value: str) -> str:
        return _validate_relative_path(value)

    @field_validator("file_refs")
    @classmethod
    def artifact_file_refs_must_be_relative(cls, value: list[str]) -> list[str]:
        return [_validate_relative_path(item) for item in value]


class MaterialCompletionCriterionSpec(MaterialBuilderModel):
    criterion_id: Identifier
    description: str = Field(min_length=1, max_length=2048)
    requirement_refs: list[Identifier] = Field(default_factory=list, max_length=64)
    validation_refs: list[Identifier] = Field(default_factory=list, max_length=64)
    artifact_refs: list[Identifier] = Field(default_factory=list, max_length=64)
    contract_refs: list[Identifier] = Field(default_factory=list, max_length=64)


class MaterialDependencyStrategySpec(MaterialBuilderModel):
    declared_dependency_files: list[RelativePath] = Field(default_factory=list, max_length=128)
    external_dependencies: list[str] = Field(default_factory=list, max_length=512)
    install_profiles: list[str] = Field(default_factory=list, max_length=64)
    lockfiles: list[RelativePath] = Field(default_factory=list, max_length=128)
    native_builds_required: bool = False
    network_required: DependencyNetworkPolicy = "none"
    requirement_refs: list[Identifier] = Field(default_factory=list, max_length=64)
    contract_refs: list[Identifier] = Field(default_factory=list, max_length=64)

    @field_validator("declared_dependency_files", "lockfiles")
    @classmethod
    def dependency_paths_must_be_relative(cls, value: list[str]) -> list[str]:
        return [_validate_relative_path(item) for item in value]


class MaterialPlan(MaterialBuilderModel):
    schema_version: Literal["material_plan.v3.2"] = "material_plan.v3.2"
    project_root: str = Field(min_length=1, max_length=255)
    requirements: list[MaterialRequirementSpec] = Field(default_factory=list, max_length=256)
    files: list[MaterialFileSpec] = Field(min_length=1, max_length=256)
    intended_interfaces: list[MaterialInterfaceSpec] = Field(default_factory=list, max_length=128)
    required_validation_profiles: list[str] = Field(default_factory=list, max_length=64)
    optional_validation_profiles: list[str] = Field(default_factory=list, max_length=64)
    validation_commands: dict[str, MaterialValidationCommand] = Field(default_factory=dict, max_length=64)
    artifact_expectations: list[MaterialArtifactExpectationSpec] = Field(default_factory=list, max_length=64)
    completion_criteria: list[MaterialCompletionCriterionSpec] = Field(default_factory=list, max_length=128)
    dependency_strategy: MaterialDependencyStrategySpec = Field(default_factory=MaterialDependencyStrategySpec)
    architecture_notes: list[str] = Field(default_factory=list, max_length=64)
    variation_reason: str | None = Field(default=None, max_length=2048)

    @field_validator("project_root")
    @classmethod
    def project_root_must_be_relative_name(cls, value: str) -> str:
        normalized = value.strip().strip("/").replace("\\", "/")
        if not normalized or normalized == "." or normalized.startswith("/") or ".." in normalized.split("/"):
            raise ValueError("project_root must be a relative project directory")
        return normalized

    @field_validator("required_validation_profiles", "optional_validation_profiles")
    @classmethod
    def validation_profiles_must_be_known(cls, value: list[str]) -> list[str]:
        unknown = sorted(set(value) - KNOWN_VALIDATION_PROFILES)
        if unknown:
            allowed = ", ".join(sorted(KNOWN_VALIDATION_PROFILES))
            raise ValueError(f"unknown validation profiles: {unknown}; allowed profiles: {allowed}")
        return value

    @model_validator(mode="after")
    def material_plan_contract_refs_must_be_consistent(self) -> "MaterialPlan":
        declared = set(self.required_validation_profiles) | set(self.optional_validation_profiles)
        for key, command in self.validation_commands.items():
            if key not in KNOWN_VALIDATION_PROFILES:
                allowed = ", ".join(sorted(KNOWN_VALIDATION_PROFILES))
                raise ValueError(f"unknown validation command profile: {key}; allowed profiles: {allowed}")
            if command.profile != key:
                raise ValueError("validation command profile must match its map key")
            if declared and key not in declared:
                raise ValueError("validation command profile must be declared as required or optional")
        _require_unique("requirement_id", [item.requirement_id for item in self.requirements])
        _require_unique("interface_id", [item.interface_id for item in self.intended_interfaces])
        _require_unique("artifact_id", [item.artifact_id for item in self.artifact_expectations])
        _require_unique("criterion_id", [item.criterion_id for item in self.completion_criteria])
        requirement_ids = {item.requirement_id for item in self.requirements}
        file_paths = {item.path for item in self.files}
        validation_ids = set(self.required_validation_profiles) | set(self.optional_validation_profiles)
        artifact_ids = {item.artifact_id for item in self.artifact_expectations}
        if requirement_ids:
            for file in self.files:
                _require_known_refs("file requirement_refs", file.requirement_refs, requirement_ids)
            for command in self.validation_commands.values():
                _require_known_refs("validation requirement_refs", command.requirement_refs, requirement_ids)
            for interface in self.intended_interfaces:
                _require_known_refs("interface requirement_refs", interface.requirement_refs, requirement_ids)
            for artifact in self.artifact_expectations:
                _require_known_refs("artifact requirement_refs", artifact.requirement_refs, requirement_ids)
            for criterion in self.completion_criteria:
                _require_known_refs("completion requirement_refs", criterion.requirement_refs, requirement_ids)
            _require_known_refs(
                "dependency strategy requirement_refs",
                self.dependency_strategy.requirement_refs,
                requirement_ids,
            )
        for interface in self.intended_interfaces:
            _require_known_refs("interface file_refs", interface.file_refs, file_paths)
        for artifact in self.artifact_expectations:
            _require_known_refs("artifact file_refs", artifact.file_refs, file_paths)
        for criterion in self.completion_criteria:
            _require_known_refs("completion validation_refs", criterion.validation_refs, validation_ids)
            _require_known_refs("completion artifact_refs", criterion.artifact_refs, artifact_ids)
        return self


class MaterialPlanResponse(MaterialBuilderModel):
    schema_version: Literal["material_plan_response.v3.2"] = "material_plan_response.v3.2"
    plan: MaterialPlan
    generation_backend: Literal["llm", "contract_blueprint"] = "llm"
    static_generation_shortcut_used: Literal[False] = False
    file_contents: dict[RelativePath, str] = Field(default_factory=dict, max_length=512)
    notes: list[str] = Field(default_factory=list, max_length=32)
    model_route: dict[str, Any] = Field(default_factory=dict, max_length=32)
    lane_metrics: dict[str, Any] = Field(default_factory=dict, max_length=64)


class PlanCoverageIssue(MaterialBuilderModel):
    issue_type: str = Field(min_length=1, max_length=128)
    severity: IssueSeverity = "blocking_completion"
    message: str = Field(min_length=1, max_length=2048)
    details: dict[str, Any] = Field(default_factory=dict, max_length=64)
    acceptance: list[str] = Field(default_factory=list, max_length=32)


class MaterialPlanRepairRequest(MaterialBuilderModel):
    session_id: Identifier
    task_id: Identifier
    working_query: str = Field(min_length=1, max_length=20000)
    original_query: str | None = Field(default=None, max_length=20000)
    original_language: str | None = Field(default=None, min_length=2, max_length=32)
    working_language: Literal["en"] = "en"
    language_context: dict[str, Any] = Field(default_factory=dict, max_length=64)
    required_capabilities: list[str] = Field(default_factory=list, max_length=128)
    constraints: dict[str, object] = Field(default_factory=dict)
    plan: MaterialPlan
    coverage_issues: list[PlanCoverageIssue] = Field(min_length=1, max_length=64)


class MaterialPlanRepairResponse(MaterialBuilderModel):
    schema_version: Literal["material_plan_repair_response.v3.2"] = (
        "material_plan_repair_response.v3.2"
    )
    plan: MaterialPlan
    generation_backend: Literal["llm", "contract_blueprint"] = "llm"
    static_generation_shortcut_used: Literal[False] = False
    notes: list[str] = Field(default_factory=list, max_length=32)
    model_route: dict[str, Any] = Field(default_factory=dict, max_length=32)
    lane_metrics: dict[str, Any] = Field(default_factory=dict, max_length=64)


class FileChunkRecord(MaterialBuilderModel):
    record_type: ChunkRecordType = Field(alias="type")
    path: RelativePath
    index: int | None = Field(default=None, ge=0)
    content: str | None = None
    chunk_count: int | None = Field(default=None, ge=1, le=10000)
    sha256: Sha256 | None = None

    @model_validator(mode="after")
    def validate_record_shape(self) -> "FileChunkRecord":
        if self.record_type == "file_start" and self.chunk_count is None:
            raise ValueError("file_start requires chunk_count")
        if self.record_type == "file_chunk" and (self.index is None or self.content is None):
            raise ValueError("file_chunk requires index and content")
        if self.record_type == "file_end" and self.sha256 is None:
            raise ValueError("file_end requires sha256")
        return self


class GeneratedFileProposal(MaterialBuilderModel):
    schema_version: Literal["material_file.v3.2"] = "material_file.v3.2"
    path: RelativePath
    content: str
    sha256: Sha256
    kind: FileKind = "other"
    source_plan_ref: Identifier | None = None

    @classmethod
    def from_content(
        cls,
        *,
        path: str,
        content: str,
        kind: FileKind = "other",
        source_plan_ref: str | None = None,
    ) -> "GeneratedFileProposal":
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        return cls(
            path=path,
            content=content,
            sha256=f"sha256:{digest}",
            kind=kind,
            source_plan_ref=source_plan_ref,
        )


class MaterialFileGenerationRequest(MaterialBuilderModel):
    session_id: Identifier
    task_id: Identifier
    plan: MaterialPlan
    target_file_paths: list[RelativePath] = Field(default_factory=list, max_length=256)
    file_contents: dict[RelativePath, str] = Field(default_factory=dict, max_length=512)
    source_plan_ref: Identifier | None = None


class MaterialFileGenerationResponse(MaterialBuilderModel):
    schema_version: Literal["material_file_generation_response.v3.2"] = (
        "material_file_generation_response.v3.2"
    )
    files: list[GeneratedFileProposal] = Field(default_factory=list, max_length=512)
    generation_backend: Literal["llm", "contract_blueprint"] = "llm"
    static_generation_shortcut_used: Literal[False] = False
    model_route: dict[str, Any] = Field(default_factory=dict, max_length=32)
    lane_metrics: dict[str, Any] = Field(default_factory=dict, max_length=64)


class HealthResponse(MaterialBuilderModel):
    status: Literal["healthy"] = "healthy"
    version: str


class CapabilitiesResponse(MaterialBuilderModel):
    owner: Literal["agents/material_builder"] = "agents/material_builder"
    capabilities: dict[str, bool] = Field(
        default_factory=lambda: {
            "material_planning": True,
            "material_plan_repair": True,
            "material_contract_proposals": True,
            "file_generation_contract": True,
            "patch_proposal_contract": True,
            "patch_set_proposal_contract": True,
            "replacement_proposal_contract": True,
            "regeneration_proposal_contract": True,
            "repair_critic_advisory_contract": True,
            "target_resolution_input": True,
            "chunk_protocol": True,
            "llm_generation_backend": False,
            "llm_lane_metrics": True,
            "llm_no_progress_watchdog": True,
            "contract_blueprint_mode": True,
            "static_generation_shortcut": False,
            "side_effects": False,
        }
    )
    lane_routes: dict[str, dict[str, Any]] = Field(default_factory=dict, max_length=16)
    prewarm_lanes: list[str] = Field(default_factory=list, max_length=16)
    forbidden: list[str] = Field(
        default_factory=lambda: [
            "workspace_writes",
            "command_execution",
            "docker_access",
            "artifact_publish",
            "task_completion",
            "static_generation_shortcut",
            "scenario_hardcoding",
        ]
    )


class IssueContract(MaterialBuilderModel):
    schema_version: Literal["material_issue.v3.2"] = "material_issue.v3.2"
    issue_id: Identifier
    issue_type: str = Field(min_length=1, max_length=128)
    severity: IssueSeverity
    target_kind: str = Field(min_length=1, max_length=128)
    requirement_refs: list[Identifier] = Field(default_factory=list, max_length=64)
    contract_refs: list[Identifier] = Field(default_factory=list, max_length=64)
    repair_intent: list[str] = Field(default_factory=list, max_length=32)
    acceptance: list[str] = Field(default_factory=list, max_length=32)
    related_context: list[str] = Field(default_factory=list, max_length=32)
    repair_obligations: list[dict[str, Any]] = Field(default_factory=list, max_length=64)
    patch_rejections: list["PatchRejectionEvidence"] = Field(default_factory=list, max_length=32)


class PatchRejectionEvidence(MaterialBuilderModel):
    rejection_id: Identifier
    issue_id: Identifier
    attempt: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=128)
    retryable: bool
    target_path: RelativePath | None = None
    patch_set_id: str | None = Field(default=None, max_length=128)
    message: str | None = Field(default=None, max_length=2048)
    diagnostics: dict[str, Any] = Field(default_factory=dict, max_length=64)


class PatchProposal(MaterialBuilderModel):
    schema_version: Literal["material_patch.v3.2"] = "material_patch.v3.2"
    issue_id: Identifier
    target_path: RelativePath
    expected_current_sha256: Sha256
    unified_diff: str = Field(min_length=1, max_length=200000)
    requirement_refs: list[Identifier] = Field(default_factory=list, min_length=1, max_length=64)
    contract_refs: list[Identifier] = Field(default_factory=list, min_length=1, max_length=64)
    rationale: str | None = Field(default=None, max_length=2048)

    @field_validator("unified_diff")
    @classmethod
    def patch_must_look_like_unified_diff(cls, value: str) -> str:
        if "--- " not in value or "+++ " not in value or "@@" not in value:
            raise ValueError("patch proposals must use unified diff format")
        return value

    @field_validator("target_path")
    @classmethod
    def target_path_must_be_relative(cls, value: str) -> str:
        return _validate_relative_path(value)

    @model_validator(mode="after")
    def patch_must_reference_requirement_and_contract(self) -> "PatchProposal":
        if not self.requirement_refs:
            raise ValueError("patch proposals must reference at least one requirement_id")
        if not self.contract_refs:
            raise ValueError("patch proposals must reference at least one contract_id")
        return self


class ReplacementProposal(MaterialBuilderModel):
    schema_version: Literal["material_replacement.v3.2"] = "material_replacement.v3.2"
    issue_id: Identifier
    target_path: RelativePath
    expected_current_sha256: Sha256
    replacement_content: str = Field(min_length=1, max_length=500000)
    replacement_sha256: Sha256
    requirement_refs: list[Identifier] = Field(default_factory=list, min_length=1, max_length=64)
    contract_refs: list[Identifier] = Field(default_factory=list, min_length=1, max_length=64)
    rationale: str | None = Field(default=None, max_length=2048)

    @field_validator("target_path")
    @classmethod
    def replacement_path_must_be_relative(cls, value: str) -> str:
        return _validate_relative_path(value)

    @model_validator(mode="after")
    def replacement_hash_must_match_content(self) -> "ReplacementProposal":
        digest = hashlib.sha256(self.replacement_content.encode("utf-8")).hexdigest()
        if self.replacement_sha256 != f"sha256:{digest}":
            raise ValueError("replacement_sha256 must match replacement_content")
        return self


class PatchSetProposal(MaterialBuilderModel):
    schema_version: Literal["material_patch_set.v3.2"] = "material_patch_set.v3.2"
    issue_id: Identifier
    patches: list[PatchProposal] = Field(min_length=1, max_length=64)
    requirement_refs: list[Identifier] = Field(default_factory=list, min_length=1, max_length=64)
    contract_refs: list[Identifier] = Field(default_factory=list, min_length=1, max_length=64)
    rationale: str | None = Field(default=None, max_length=2048)

    @model_validator(mode="after")
    def patch_set_must_be_coherent(self) -> "PatchSetProposal":
        issue_ids = {patch.issue_id for patch in self.patches}
        if issue_ids - {self.issue_id}:
            raise ValueError("patch set patches must reference the patch set issue_id")
        paths = [patch.target_path for patch in self.patches]
        if len(set(paths)) != len(paths):
            raise ValueError("patch set must target each file at most once")
        for patch in self.patches:
            if not set(patch.requirement_refs).issubset(set(self.requirement_refs)):
                raise ValueError("patch set requirement_refs must include every patch requirement_ref")
            if not set(patch.contract_refs).issubset(set(self.contract_refs)):
                raise ValueError("patch set contract_refs must include every patch contract_ref")
        return self


class RegenerateFromContractProposal(MaterialBuilderModel):
    schema_version: Literal["material_regenerate_from_contract.v3.2"] = (
        "material_regenerate_from_contract.v3.2"
    )
    issue_id: Identifier
    contract_refs: list[Identifier] = Field(min_length=1, max_length=64)
    requirement_refs: list[Identifier] = Field(default_factory=list, min_length=1, max_length=64)
    target_paths: list[RelativePath] = Field(min_length=1, max_length=128)
    rationale: str = Field(min_length=1, max_length=2048)

    @field_validator("target_paths")
    @classmethod
    def regeneration_paths_must_be_relative(cls, value: list[str]) -> list[str]:
        return [_validate_relative_path(item) for item in value]


class RepairTargetResolution(MaterialBuilderModel):
    primary_target: RelativePath | None = None
    related_targets: list[RelativePath] = Field(default_factory=list, max_length=64)
    candidate_targets: list[RelativePath] = Field(default_factory=list, max_length=128)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    rationale: str = Field(default="", max_length=2048)

    @field_validator("primary_target")
    @classmethod
    def primary_target_must_be_relative(cls, value: str | None) -> str | None:
        return _validate_relative_path(value) if value else None

    @field_validator("related_targets", "candidate_targets")
    @classmethod
    def resolution_paths_must_be_relative(cls, value: list[str]) -> list[str]:
        return [_validate_relative_path(item) for item in value]


class MaterialPatchGenerationRequest(MaterialBuilderModel):
    session_id: Identifier
    task_id: Identifier
    plan: MaterialPlan
    issue_id: Identifier
    issue: IssueContract
    target_path: RelativePath
    expected_current_sha256: Sha256
    current_content: str = Field(max_length=500000)
    current_context: dict[str, Any] = Field(default_factory=dict, max_length=64)
    target_resolution: RepairTargetResolution | None = None
    validation_profile: str | None = Field(default=None, max_length=128)
    command_evidence: dict[str, Any] = Field(default_factory=dict)
    prior_patch_rejections: list[PatchRejectionEvidence] = Field(default_factory=list, max_length=32)
    patch_blueprints: list[PatchProposal] = Field(default_factory=list, max_length=32)
    patch_set_blueprints: list[PatchSetProposal] = Field(default_factory=list, max_length=16)
    replacement_blueprints: list[ReplacementProposal] = Field(default_factory=list, max_length=16)
    regeneration_blueprints: list[RegenerateFromContractProposal] = Field(default_factory=list, max_length=16)

    @field_validator("target_path")
    @classmethod
    def target_path_must_be_relative(cls, value: str) -> str:
        return _validate_relative_path(value)


class MaterialPatchGenerationResponse(MaterialBuilderModel):
    schema_version: Literal["material_patch_generation_response.v3.2"] = (
        "material_patch_generation_response.v3.2"
    )
    patch: PatchProposal | None = None
    patch_set: PatchSetProposal | None = None
    replacement: ReplacementProposal | None = None
    regeneration: RegenerateFromContractProposal | None = None
    generation_backend: Literal["llm", "contract_blueprint"] = "llm"
    static_generation_shortcut_used: Literal[False] = False
    model_route: dict[str, Any] = Field(default_factory=dict, max_length=32)
    lane_metrics: dict[str, Any] = Field(default_factory=dict, max_length=64)

    @model_validator(mode="after")
    def require_exactly_one_repair_proposal(self) -> "MaterialPatchGenerationResponse":
        proposals = [
            self.patch is not None,
            self.patch_set is not None,
            self.replacement is not None,
            self.regeneration is not None,
        ]
        if sum(proposals) != 1:
            raise ValueError("material repair response must contain exactly one repair proposal")
        return self


class MaterialRepairCriticRequest(MaterialBuilderModel):
    session_id: Identifier
    task_id: Identifier
    plan: MaterialPlan
    issue_id: Identifier
    issue: IssueContract
    target_path: RelativePath
    current_content: str = Field(max_length=500000)
    current_context: dict[str, Any] = Field(default_factory=dict, max_length=64)
    command_evidence: dict[str, Any] = Field(default_factory=dict)
    prior_patch_rejections: list[PatchRejectionEvidence] = Field(default_factory=list, max_length=32)
    repair_arbiter: dict[str, Any] = Field(default_factory=dict, max_length=64)


class MaterialRepairCriticFinding(MaterialBuilderModel):
    finding_type: str = Field(min_length=1, max_length=128)
    severity: Literal["info", "warning", "blocking_advisory"] = "warning"
    message: str = Field(min_length=1, max_length=2048)
    evidence_refs: list[str] = Field(default_factory=list, max_length=64)


class MaterialRepairCriticResponse(MaterialBuilderModel):
    schema_version: Literal["material_repair_critic.v3.2"] = "material_repair_critic.v3.2"
    advisory_only: Literal[True] = True
    findings: list[MaterialRepairCriticFinding] = Field(default_factory=list, max_length=32)
    likely_root_cause: str | None = Field(default=None, max_length=2048)
    recommended_strategy: Literal[
        "patch",
        "replacement",
        "patch_set",
        "plan_repair",
        "regeneration",
        "failed_closed",
        "continue_validation",
    ] = "replacement"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    model_route: dict[str, Any] = Field(default_factory=dict, max_length=32)
    lane_metrics: dict[str, Any] = Field(default_factory=dict, max_length=64)


def _validate_relative_path(value: str) -> str:
    if value.startswith("/") or ".." in value.split("/"):
        raise ValueError("paths must be relative and stay inside the project")
    return value


def _require_unique(label: str, values: list[str]) -> None:
    duplicates = sorted({value for value in values if values.count(value) > 1})
    if duplicates:
        raise ValueError(f"duplicate {label}: {duplicates}")


def _require_known_refs(label: str, refs: list[str], allowed: set[str]) -> None:
    unknown = sorted(set(refs) - allowed)
    if unknown:
        raise ValueError(f"unknown {label}: {unknown}")
