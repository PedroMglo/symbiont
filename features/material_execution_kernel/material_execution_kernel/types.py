"""Pydantic contracts for the material execution kernel feature."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


Identifier = Annotated[str, Field(min_length=3, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")]
Sha256 = Annotated[str, Field(pattern=r"^sha256:[a-f0-9]{64}$")]

MaterialSessionStatus = Literal[
    "created",
    "policy_preflight",
    "vm_allocating",
    "vm_ready",
    "planning",
    "generating_files",
    "workspace_materializing",
    "validating",
    "repairing",
    "revalidating",
    "packaging",
    "completed",
    "blocked_by_policy",
    "blocked_by_vm_isolation",
    "blocked_by_sandbox_profile",
    "blocked_by_contract",
    "blocked_by_missing_tool",
    "failed_closed",
    "stalled",
    "cancelled",
]

NetworkPolicy = Literal[
    "disabled_by_default",
    "none",
    "vm-internal",
    "dependency-cache",
    "external_denied",
]
PackageInstallPolicy = Literal["disabled", "dependency-cache-only", "external-allowed"]
DependencyNetworkPolicy = Literal["none", "dependency-cache", "external"]
NativeBuildPolicy = Literal["deny", "allow-pure-python", "allow-with-approval"]

SandboxMode = Literal["vm-material-read", "vm-workspace-write", "supervised"]
DockerMode = Literal["vm-local-or-proxied-isolated"]
GeneratedProjectTrust = Literal["untrusted"]
VmIsolation = Literal["required", "vm", "microvm", "vm-backed-proxy"]
IssueSeverity = Literal["info", "warning", "repairable", "blocking_completion", "security_block"]
EventSource = Literal["kernel", "orchestrator", "material_builder", "sandbox_owner", "policy"]
EventStatus = Literal["started", "progress", "completed", "failed", "blocked", "heartbeat", "cancelled"]
ContractRequirementSource = Literal["user", "derived", "capability", "constraint"]
ContractInterfaceKind = Literal["api", "cli", "worker", "service", "library", "data", "artifact", "other"]
ObservedEcosystem = Literal["python", "node", "generic"]
ObservedParseStatus = Literal["parsed", "skipped", "failed"]
ObservedSymbolKind = Literal["function", "class", "variable", "module", "script", "unknown"]
ObservedEntrypointKind = Literal["api", "cli", "worker", "service", "library", "test", "other"]
ObservedDependencySource = Literal["requirements", "pyproject", "setup_cfg", "package_json", "unknown"]
ObservedIssueSeverity = Literal["info", "warning", "blocking_contract"]
RequirementTraceStatus = Literal["covered", "pending_runtime_evidence", "missing_evidence_path"]
ContractComparisonStatus = Literal["passed", "amendment_required", "failed_closed"]
ContractComparisonIssueSeverity = Literal["info", "warning", "blocking_completion"]
LatencySource = Literal[
    "kernel",
    "llm",
    "material_builder",
    "vm",
    "sandbox",
    "docker",
    "policy",
    "validation",
    "storage",
    "orchestrator",
    "unknown",
]


class MaterialKernelModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LanguageContext(MaterialKernelModel):
    original_language: str = Field(min_length=2, max_length=32)
    working_language: str = Field(default="en", min_length=2, max_length=32)
    translation_available: bool = False
    source_variant: str | None = Field(default=None, min_length=2, max_length=32)
    target_language: str = Field(default="en", min_length=2, max_length=32)
    translation_safe: bool = True
    internal_contract_language: Literal["en"] = "en"
    final_response_language: str | None = Field(default=None, min_length=2, max_length=32)
    contract_version: str | None = Field(default=None, max_length=128)
    quality: dict[str, Any] = Field(default_factory=dict, max_length=64)
    safety_error: dict[str, Any] | None = None


class MaterialDependencyPolicy(MaterialKernelModel):
    package_install: PackageInstallPolicy = "disabled"
    network: DependencyNetworkPolicy = "none"
    lockfile_required: bool = False
    native_builds: NativeBuildPolicy = "deny"
    dependency_cache_profile: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def require_cache_profile_for_cache_network(self) -> "MaterialDependencyPolicy":
        if self.network == "dependency-cache" and not self.dependency_cache_profile:
            raise ValueError("dependency-cache network policy requires dependency_cache_profile")
        if self.package_install == "dependency-cache-only" and not self.dependency_cache_profile:
            raise ValueError("dependency-cache-only install policy requires dependency_cache_profile")
        return self


class MaterialExecutionConstraints(MaterialKernelModel):
    expected_artifact_root: str | None = Field(default=None, min_length=1, max_length=255)
    must_use_vm_backed_sandbox: bool = True
    must_not_execute_on_host: bool = True
    durable_publish: bool = False
    publish_destination_root: str | None = Field(default=None, min_length=1, max_length=4096)
    publish_direct_to_destination_root: bool = False
    publish_store: str = Field(default="agent_outputs", min_length=1, max_length=128)
    publish_zone: str = Field(default="ingest", min_length=1, max_length=128)
    network_policy: NetworkPolicy = "disabled_by_default"
    generated_project_trust: GeneratedProjectTrust = "untrusted"
    dependency_policy: MaterialDependencyPolicy = Field(default_factory=MaterialDependencyPolicy)

    @model_validator(mode="after")
    def require_v3_2_sandbox_invariants(self) -> "MaterialExecutionConstraints":
        if not self.must_use_vm_backed_sandbox:
            raise ValueError("material execution requires a VM-backed sandbox")
        if not self.must_not_execute_on_host:
            raise ValueError("host execution fallback is forbidden")
        return self


class MaterialPolicyContext(MaterialKernelModel):
    sandbox_mode: SandboxMode = "vm-workspace-write"
    approval_mode: str = Field(default="supervised", min_length=1, max_length=64)
    network_mode: NetworkPolicy = "disabled_by_default"
    docker_mode: DockerMode = "vm-local-or-proxied-isolated"


class MaterialSessionRequest(MaterialKernelModel):
    task_id: Identifier
    trace_id: Identifier
    idempotency_key: Identifier
    goal: str = Field(min_length=1, max_length=20000)
    language_context: LanguageContext
    constraints: MaterialExecutionConstraints = Field(default_factory=MaterialExecutionConstraints)
    material_builder_context: dict[str, Any] = Field(default_factory=dict, max_length=64)
    max_repair_rounds: int = Field(default=6, ge=0, le=10)
    required_capabilities: list[str] = Field(default_factory=list, max_length=128)
    context_refs: list[str] = Field(default_factory=list, max_length=256)
    policy_context: MaterialPolicyContext = Field(default_factory=MaterialPolicyContext)


class SandboxEvidence(MaterialKernelModel):
    owner: str = Field(min_length=1, max_length=255)
    vm_session_id: Identifier | None = None
    vm_isolation: VmIsolation = "required"
    host_execution_used: bool = False
    docker_socket_available_to_generated_project: bool = False
    network_policy: NetworkPolicy = "disabled_by_default"
    cleanup_recorded: bool = False

    @model_validator(mode="after")
    def forbid_host_escape_evidence(self) -> "SandboxEvidence":
        if self.host_execution_used:
            raise ValueError("host execution is not valid sandbox evidence")
        if self.docker_socket_available_to_generated_project:
            raise ValueError("generated projects must not receive the host Docker socket")
        return self


class ArtifactEvidence(MaterialKernelModel):
    path: str = Field(min_length=1, max_length=4096)
    sha256: Sha256
    size_bytes: int = Field(ge=0)
    storage_object_ref: str | None = None
    chain_of_custody_ref: str | None = None
    materialized_path: str | None = None
    materialized_sha256: Sha256 | None = None
    extracted_path: str | None = None
    extracted_files_count: int | None = Field(default=None, ge=0)
    extracted_top_level_paths: list[str] = Field(default_factory=list)


class CommandRunEvidence(MaterialKernelModel):
    command_run_id: Identifier
    profile: str = Field(min_length=1, max_length=128)
    vm_session_id: Identifier
    host_execution_used: bool = False
    duration_ms: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def forbid_host_command_evidence(self) -> "CommandRunEvidence":
        if self.host_execution_used:
            raise ValueError("command evidence from host execution is forbidden")
        return self


class ValidationSummary(MaterialKernelModel):
    passed: list[str] = Field(default_factory=list, max_length=256)
    failed: list[str] = Field(default_factory=list, max_length=256)
    skipped: list[str] = Field(default_factory=list, max_length=256)


class RepairTargetResolution(MaterialKernelModel):
    primary_target: str | None = Field(default=None, max_length=4096)
    related_targets: list[str] = Field(default_factory=list, max_length=64)
    candidate_targets: list[str] = Field(default_factory=list, max_length=128)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    rationale: str = Field(default="", max_length=2048)


class IssueBundleFailure(MaterialKernelModel):
    profile: str = Field(min_length=1, max_length=128)
    command_run_id: Identifier
    issue_code: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=2048)
    target_path: str | None = Field(default=None, max_length=4096)
    target_resolution: RepairTargetResolution | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class IssueBundleSkippedProfile(MaterialKernelModel):
    profile: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=2048)
    blocked_by: list[str] = Field(default_factory=list, max_length=64)


class IssueBundle(MaterialKernelModel):
    bundle_id: Identifier
    profiles_attempted: list[str] = Field(default_factory=list, max_length=256)
    profiles_failed: list[str] = Field(default_factory=list, max_length=256)
    profiles_skipped: list[str] = Field(default_factory=list, max_length=256)
    failures: list[IssueBundleFailure] = Field(default_factory=list, max_length=256)
    skipped: list[IssueBundleSkippedProfile] = Field(default_factory=list, max_length=256)
    repair_focus_profile: str | None = Field(default=None, max_length=128)
    repair_focus_target_path: str | None = Field(default=None, max_length=4096)
    repair_focus_reason: str | None = Field(default=None, max_length=2048)
    repairable: bool = False


class MaterialIssue(MaterialKernelModel):
    issue_id: Identifier
    issue_type: str = Field(min_length=1, max_length=128)
    severity: IssueSeverity
    target_kind: str = Field(min_length=1, max_length=128)
    target_path: str | None = Field(default=None, max_length=4096)
    target_resolution: RepairTargetResolution | None = None
    requirement_refs: list[Identifier] = Field(default_factory=list, max_length=64)
    contract_refs: list[Identifier] = Field(default_factory=list, max_length=64)
    patch_rejections: list["PatchRejectionEvidence"] = Field(default_factory=list, max_length=32)
    details: dict[str, Any] = Field(default_factory=dict)


class PatchRejectionEvidence(MaterialKernelModel):
    rejection_id: Identifier
    issue_id: Identifier
    attempt: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=128)
    retryable: bool
    target_path: str | None = Field(default=None, max_length=4096)
    patch_set_id: str | None = Field(default=None, max_length=128)
    message: str | None = Field(default=None, max_length=2048)
    diagnostics: dict[str, Any] = Field(default_factory=dict)


class MaterialManifestFile(MaterialKernelModel):
    path: str = Field(min_length=1, max_length=4096)
    purpose: str = Field(default="", max_length=8192)
    state: str = Field(default="planned", min_length=1, max_length=128)
    kind: str = Field(default="other", min_length=1, max_length=64)
    content_hash: Sha256 | None = None
    producer: str = Field(default="material_builder", min_length=1, max_length=128)
    repair_round: int = Field(default=0, ge=0)


class MaterialManifestValidation(MaterialKernelModel):
    profile: str = Field(min_length=1, max_length=128)
    status: str = Field(min_length=1, max_length=128)
    command_run_id: Identifier | None = None
    vm_session_id: Identifier | None = None
    duration_ms: int = Field(default=0, ge=0)
    details: dict[str, Any] = Field(default_factory=dict)


class MaterialManifestArtifact(MaterialKernelModel):
    status: str = Field(default="pending", min_length=1, max_length=128)
    path: str | None = Field(default=None, max_length=4096)
    sha256: Sha256 | None = None
    size_bytes: int | None = Field(default=None, ge=0)
    storage_object_ref: str | None = None
    chain_of_custody_ref: str | None = None
    materialized_path: str | None = None
    materialized_sha256: Sha256 | None = None
    extracted_path: str | None = None
    extracted_files_count: int | None = Field(default=None, ge=0)
    extracted_top_level_paths: list[str] = Field(default_factory=list)


class RequirementTrace(MaterialKernelModel):
    trace_id: Identifier
    requirement_id: Identifier
    acceptance_criterion: str = Field(min_length=1, max_length=2048)
    intended_interface_ids: list[Identifier] = Field(default_factory=list, max_length=128)
    runtime_surfaces: list[str] = Field(default_factory=list, max_length=128)
    validation_profiles: list[str] = Field(default_factory=list, max_length=256)
    concrete_checks: list[str] = Field(default_factory=list, max_length=256)
    evidence_refs: list[str] = Field(default_factory=list, max_length=512)
    status: RequirementTraceStatus = "pending_runtime_evidence"


class ContractComparisonIssue(MaterialKernelModel):
    issue_id: Identifier
    issue_type: str = Field(min_length=1, max_length=128)
    severity: ContractComparisonIssueSeverity = "warning"
    requirement_id: Identifier | None = None
    path: str | None = Field(default=None, max_length=4096)
    details: dict[str, Any] = Field(default_factory=dict)


class ContractComparison(MaterialKernelModel):
    schema_version: Literal["contract_comparison.v0.1"] = "contract_comparison.v0.1"
    comparison_id: Identifier
    session_id: Identifier
    task_id: Identifier
    material_contract_id: Identifier
    observed_contract_id: Identifier
    status: ContractComparisonStatus
    requirements_trace: list[RequirementTrace] = Field(default_factory=list, max_length=2048)
    issues: list[ContractComparisonIssue] = Field(default_factory=list, max_length=2048)
    blocking_issue_count: int = Field(default=0, ge=0)
    evidence_refs: list[str] = Field(default_factory=list, max_length=1024)


class MaterialManifest(MaterialKernelModel):
    schema_version: Literal["material_manifest.v3.2"] = "material_manifest.v3.2"
    session_id: Identifier
    task_id: Identifier
    trace_id: Identifier
    status: MaterialSessionStatus
    project_root: str | None = Field(default=None, max_length=255)
    language: dict[str, Any] = Field(default_factory=dict)
    sandbox: dict[str, Any] = Field(default_factory=dict)
    files: list[MaterialManifestFile] = Field(default_factory=list, max_length=1024)
    required_validation_profiles: list[str] = Field(default_factory=list, max_length=256)
    optional_validation_profiles: list[str] = Field(default_factory=list, max_length=256)
    material_contract: dict[str, Any] | None = None
    observed_contract: dict[str, Any] | None = None
    interface_ledger: dict[str, Any] | None = None
    repair_obligations: list[dict[str, Any]] = Field(default_factory=list, max_length=2048)
    repair_cases: list[dict[str, Any]] = Field(default_factory=list, max_length=2048)
    repair_arbiter: dict[str, Any] | None = None
    requirements_trace: list[RequirementTrace] = Field(default_factory=list, max_length=2048)
    contract_comparison: dict[str, Any] | None = None
    validations: list[MaterialManifestValidation] = Field(default_factory=list, max_length=1024)
    issue_bundles: list[IssueBundle] = Field(default_factory=list, max_length=256)
    issues: list[MaterialIssue] = Field(default_factory=list, max_length=1024)
    artifact: MaterialManifestArtifact = Field(default_factory=MaterialManifestArtifact)


class MaterialContractLanguage(MaterialKernelModel):
    original_query_language: str = Field(min_length=2, max_length=32)
    working_language: Literal["en"] = "en"
    internal_contract_language: Literal["en"] = "en"
    final_response_language: str | None = Field(default=None, min_length=2, max_length=32)
    translation_available: bool = False


class MaterialContractRequirement(MaterialKernelModel):
    requirement_id: Identifier
    description: str = Field(min_length=1, max_length=2048)
    source: ContractRequirementSource = "user"
    capability_refs: list[str] = Field(default_factory=list, max_length=64)


class MaterialContractPlannedFile(MaterialKernelModel):
    file_id: Identifier
    path: str = Field(min_length=1, max_length=4096)
    purpose: str = Field(min_length=1, max_length=8192)
    kind: str = Field(min_length=1, max_length=64)
    requirement_ids: list[Identifier] = Field(default_factory=list, max_length=64)
    contract_refs: list[Identifier] = Field(default_factory=list, max_length=64)


class MaterialContractInterface(MaterialKernelModel):
    interface_id: Identifier
    kind: ContractInterfaceKind = "other"
    name: str = Field(min_length=1, max_length=255)
    purpose: str = Field(min_length=1, max_length=2048)
    requirement_ids: list[Identifier] = Field(default_factory=list, max_length=64)
    file_ids: list[Identifier] = Field(default_factory=list, max_length=128)


class MaterialContractValidationProfile(MaterialKernelModel):
    validation_id: Identifier
    profile: str = Field(min_length=1, max_length=128)
    requirement_ids: list[Identifier] = Field(default_factory=list, max_length=64)
    contract_refs: list[Identifier] = Field(default_factory=list, max_length=64)
    file_ids: list[Identifier] = Field(default_factory=list, max_length=128)
    command_ref: Identifier | None = None


class MaterialContractArtifactExpectation(MaterialKernelModel):
    artifact_id: Identifier
    root: str = Field(min_length=1, max_length=4096)
    purpose: str = Field(min_length=1, max_length=2048)
    requirement_ids: list[Identifier] = Field(default_factory=list, max_length=64)
    contract_refs: list[Identifier] = Field(default_factory=list, max_length=64)
    file_ids: list[Identifier] = Field(default_factory=list, max_length=128)


class MaterialContractCompletionCriterion(MaterialKernelModel):
    criterion_id: Identifier
    description: str = Field(min_length=1, max_length=2048)
    requirement_ids: list[Identifier] = Field(default_factory=list, max_length=64)
    validation_ids: list[Identifier] = Field(default_factory=list, max_length=128)
    artifact_ids: list[Identifier] = Field(default_factory=list, max_length=64)
    contract_refs: list[Identifier] = Field(default_factory=list, max_length=64)


class MaterialContractDependencyStrategy(MaterialKernelModel):
    strategy_id: Identifier = "dependency_strategy:v0.1"
    declared_dependency_files: list[str] = Field(default_factory=list, max_length=128)
    external_dependencies: list[str] = Field(default_factory=list, max_length=512)
    install_profiles: list[str] = Field(default_factory=list, max_length=64)
    lockfiles: list[str] = Field(default_factory=list, max_length=128)
    native_builds_required: bool = False
    network_required: DependencyNetworkPolicy = "none"
    requirement_ids: list[Identifier] = Field(default_factory=list, max_length=64)
    contract_refs: list[Identifier] = Field(default_factory=list, max_length=64)


class MaterialContract(MaterialKernelModel):
    schema_version: Literal["material_contract.v0.1"] = "material_contract.v0.1"
    contract_id: Identifier
    session_id: Identifier
    task_id: Identifier
    project_root: str = Field(min_length=1, max_length=255)
    language: MaterialContractLanguage
    requirements: list[MaterialContractRequirement] = Field(min_length=1, max_length=512)
    planned_files: list[MaterialContractPlannedFile] = Field(min_length=1, max_length=1024)
    intended_interfaces: list[MaterialContractInterface] = Field(default_factory=list, max_length=256)
    validation_profiles: list[MaterialContractValidationProfile] = Field(min_length=1, max_length=256)
    artifact_expectations: list[MaterialContractArtifactExpectation] = Field(min_length=1, max_length=64)
    completion_criteria: list[MaterialContractCompletionCriterion] = Field(min_length=1, max_length=256)
    dependency_policy: MaterialDependencyPolicy = Field(default_factory=MaterialDependencyPolicy)
    dependency_strategy: MaterialContractDependencyStrategy = Field(default_factory=MaterialContractDependencyStrategy)
    frozen: Literal[True] = True
    neutrality_notes: list[str] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def require_traceable_and_neutral_contract(self) -> "MaterialContract":
        requirement_ids = _unique_contract_ids("requirement_id", [item.requirement_id for item in self.requirements])
        file_ids = _unique_contract_ids("file_id", [item.file_id for item in self.planned_files])
        interface_ids = _unique_contract_ids(
            "interface_id",
            [item.interface_id for item in self.intended_interfaces],
        )
        validation_ids = _unique_contract_ids(
            "validation_id",
            [item.validation_id for item in self.validation_profiles],
        )
        artifact_ids = _unique_contract_ids(
            "artifact_id",
            [item.artifact_id for item in self.artifact_expectations],
        )
        criterion_ids = _unique_contract_ids(
            "criterion_id",
            [item.criterion_id for item in self.completion_criteria],
        )
        del interface_ids, criterion_ids
        allowed_contract_refs = {self.contract_id}
        for item in self.planned_files:
            _require_requirement_or_contract_ref("planned file", item.requirement_ids, item.contract_refs)
            _require_known_contract_refs("planned file requirement_ids", item.requirement_ids, requirement_ids)
            _require_known_contract_refs("planned file contract_refs", item.contract_refs, allowed_contract_refs)
        for item in self.validation_profiles:
            _require_requirement_or_contract_ref("validation profile", item.requirement_ids, item.contract_refs)
            _require_known_contract_refs("validation requirement_ids", item.requirement_ids, requirement_ids)
            _require_known_contract_refs("validation contract_refs", item.contract_refs, allowed_contract_refs)
            _require_known_contract_refs("validation file_ids", item.file_ids, file_ids)
        for item in self.intended_interfaces:
            _require_known_contract_refs("interface requirement_ids", item.requirement_ids, requirement_ids)
            _require_known_contract_refs("interface file_ids", item.file_ids, file_ids)
        for item in self.artifact_expectations:
            _require_requirement_or_contract_ref("artifact expectation", item.requirement_ids, item.contract_refs)
            _require_known_contract_refs("artifact requirement_ids", item.requirement_ids, requirement_ids)
            _require_known_contract_refs("artifact contract_refs", item.contract_refs, allowed_contract_refs)
            _require_known_contract_refs("artifact file_ids", item.file_ids, file_ids)
        for item in self.completion_criteria:
            _require_requirement_or_contract_ref("completion criterion", item.requirement_ids, item.contract_refs)
            _require_known_contract_refs("completion requirement_ids", item.requirement_ids, requirement_ids)
            _require_known_contract_refs("completion contract_refs", item.contract_refs, allowed_contract_refs)
            _require_known_contract_refs("completion validation_ids", item.validation_ids, validation_ids)
            _require_known_contract_refs("completion artifact_ids", item.artifact_ids, artifact_ids)
        _require_known_contract_refs(
            "dependency strategy requirement_ids",
            self.dependency_strategy.requirement_ids,
            requirement_ids,
        )
        _require_known_contract_refs(
            "dependency strategy contract_refs",
            self.dependency_strategy.contract_refs,
            allowed_contract_refs,
        )
        _reject_scenario_specific_runtime_rules(self)
        return self


class ObservedImport(MaterialKernelModel):
    path: str = Field(min_length=1, max_length=4096)
    module: str = Field(min_length=1, max_length=512)
    name: str | None = Field(default=None, max_length=255)
    alias: str | None = Field(default=None, max_length=255)
    line: int = Field(default=0, ge=0)
    relative: bool = False
    local: bool = False
    standard_library: bool = False
    declared_dependency: bool = False


class ObservedExport(MaterialKernelModel):
    path: str = Field(min_length=1, max_length=4096)
    module: str = Field(min_length=1, max_length=512)
    name: str = Field(min_length=1, max_length=255)
    kind: ObservedSymbolKind = "unknown"
    line: int = Field(default=0, ge=0)


class ObservedDependency(MaterialKernelModel):
    name: str = Field(min_length=1, max_length=255)
    normalized_name: str = Field(min_length=1, max_length=255)
    source_path: str = Field(min_length=1, max_length=4096)
    source_kind: ObservedDependencySource = "unknown"
    raw: str = Field(min_length=1, max_length=2048)


class ObservedTestExpectation(MaterialKernelModel):
    path: str = Field(min_length=1, max_length=4096)
    target_module: str = Field(min_length=1, max_length=512)
    target_name: str | None = Field(default=None, max_length=255)
    expectation_kind: str = Field(min_length=1, max_length=128)
    line: int = Field(default=0, ge=0)


class ObservedEntrypoint(MaterialKernelModel):
    entrypoint_id: Identifier
    kind: ObservedEntrypointKind = "other"
    path: str = Field(min_length=1, max_length=4096)
    name: str = Field(min_length=1, max_length=255)
    line: int | None = Field(default=None, ge=0)
    evidence: str = Field(min_length=1, max_length=2048)


class ObservedContractIssue(MaterialKernelModel):
    issue_id: Identifier
    issue_type: str = Field(min_length=1, max_length=128)
    severity: ObservedIssueSeverity = "warning"
    path: str | None = Field(default=None, max_length=4096)
    details: dict[str, Any] = Field(default_factory=dict)


class ObservedFile(MaterialKernelModel):
    path: str = Field(min_length=1, max_length=4096)
    ecosystem: ObservedEcosystem = "generic"
    kind: str = Field(default="other", min_length=1, max_length=64)
    module: str | None = Field(default=None, max_length=512)
    parse_status: ObservedParseStatus = "skipped"
    imports: list[ObservedImport] = Field(default_factory=list, max_length=2048)
    exports: list[ObservedExport] = Field(default_factory=list, max_length=2048)
    issues: list[ObservedContractIssue] = Field(default_factory=list, max_length=256)


class ObservedContract(MaterialKernelModel):
    schema_version: Literal["observed_contract.v0.1"] = "observed_contract.v0.1"
    observed_contract_id: Identifier
    session_id: Identifier
    task_id: Identifier
    project_root: str = Field(min_length=1, max_length=255)
    ecosystems: list[ObservedEcosystem] = Field(default_factory=list, max_length=32)
    files: list[ObservedFile] = Field(default_factory=list, max_length=1024)
    imports: list[ObservedImport] = Field(default_factory=list, max_length=4096)
    exports: list[ObservedExport] = Field(default_factory=list, max_length=4096)
    dependencies: list[ObservedDependency] = Field(default_factory=list, max_length=2048)
    test_expectations: list[ObservedTestExpectation] = Field(default_factory=list, max_length=4096)
    entrypoints: list[ObservedEntrypoint] = Field(default_factory=list, max_length=1024)
    issues: list[ObservedContractIssue] = Field(default_factory=list, max_length=2048)
    extractor_versions: dict[str, str] = Field(default_factory=dict, max_length=64)


class MaterialEvent(MaterialKernelModel):
    event_id: Identifier
    event_type: str = Field(min_length=1, max_length=128)
    session_id: Identifier
    task_id: Identifier
    source: EventSource
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    phase: str | None = Field(default=None, max_length=128)
    status: EventStatus = "progress"
    started_at: datetime | None = None
    finished_at: datetime | None = None
    last_progress_at: datetime | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    latency_source: LatencySource = "unknown"
    payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def require_finished_at_for_terminal_event(self) -> "MaterialEvent":
        if self.status in {"completed", "failed", "blocked", "cancelled"} and self.finished_at is None:
            object.__setattr__(self, "finished_at", self.created_at)
        if self.last_progress_at is None:
            object.__setattr__(self, "last_progress_at", self.created_at)
        return self


class MaterialPhaseTiming(MaterialKernelModel):
    phase: str = Field(min_length=1, max_length=128)
    status: EventStatus
    started_at: datetime | None = None
    finished_at: datetime | None = None
    last_progress_at: datetime | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    latency_source: LatencySource = "unknown"
    event_id: Identifier | None = None


class MaterialSessionResponse(MaterialKernelModel):
    session_id: Identifier
    task_id: Identifier
    status: MaterialSessionStatus
    manifest_ref: str | None = None
    sandbox: SandboxEvidence
    artifact: ArtifactEvidence | None = None
    validation_summary: ValidationSummary = Field(default_factory=ValidationSummary)
    command_runs: list[CommandRunEvidence] = Field(default_factory=list, max_length=1024)
    issues: list[MaterialIssue] = Field(default_factory=list, max_length=1024)
    phase_timings: list[MaterialPhaseTiming] = Field(default_factory=list, max_length=1024)
    last_progress_at: datetime | None = None
    latency_summary: dict[LatencySource, int] = Field(default_factory=dict)
    diagnostics_ref: str | None = None

    @model_validator(mode="after")
    def require_completion_evidence(self) -> "MaterialSessionResponse":
        if self.status != "completed":
            return self
        if self.artifact is None:
            raise ValueError("completed material sessions require artifact evidence")
        if self.validation_summary.failed:
            raise ValueError("completed material sessions cannot have failed validations")
        if any(issue.severity in {"blocking_completion", "security_block"} for issue in self.issues):
            raise ValueError("completed material sessions cannot have blocking issues")
        if self.validation_summary.passed and not self.command_runs:
            raise ValueError("completed material sessions require command run evidence")
        if not self.sandbox.cleanup_recorded:
            raise ValueError("completed material sessions require VM cleanup evidence")
        return self


class CapabilitiesResponse(MaterialKernelModel):
    owner: Literal["features/material_execution_kernel"] = "features/material_execution_kernel"
    active_sandbox_owner: str = "features/workspace_execution"
    runtime_limits: dict[str, int] = Field(default_factory=dict, max_length=16)
    model_lane_policy: dict[str, Any] = Field(default_factory=dict, max_length=32)
    capabilities: dict[str, bool] = Field(
        default_factory=lambda: {
            "material_sessions": True,
            "incremental_manifest": True,
            "event_stream": True,
            "repair_loop": True,
            "patch_first": True,
            "material_contract_v0_1": True,
            "observed_contract_v0_1": True,
            "contract_comparison_v0_1": True,
            "plan_coverage_gate": True,
            "interface_ledger_v0_1": True,
            "repair_obligations": True,
            "repair_arbiter_v0_1": True,
            "critic_advisory_optional": True,
            "requires_vm_backed_sandbox": True,
            "configured_runtime_watchdogs": True,
            "model_lane_metrics": True,
            "prewarm_intent_events": True,
            "diagnostics_bundle": True,
        }
    )
    forbidden: list[str] = Field(
        default_factory=lambda: [
            "direct_shell_execution",
            "host_file_writes",
            "direct_docker_access",
            "durable_storage_write",
            "static_generation_shortcut",
        ]
    )


def _unique_contract_ids(label: str, values: list[str]) -> set[str]:
    duplicates = sorted({value for value in values if values.count(value) > 1})
    if duplicates:
        raise ValueError(f"duplicate {label}: {duplicates}")
    return set(values)


def _require_requirement_or_contract_ref(label: str, requirement_ids: list[str], contract_refs: list[str]) -> None:
    if not requirement_ids and not contract_refs:
        raise ValueError(f"{label} must map to at least one requirement_id or contract_ref")


def _require_known_contract_refs(label: str, refs: list[str], allowed: set[str]) -> None:
    unknown = sorted(set(refs) - allowed)
    if unknown:
        raise ValueError(f"unknown {label}: {unknown}")


def _reject_scenario_specific_runtime_rules(contract: MaterialContract) -> None:
    text = contract.model_dump_json().casefold()
    scenario_rule_terms = {
        "benchmark shortcut",
        "demo-only",
        "fixture-only",
        "hardcoded scenario",
        "hard-coded scenario",
        "prompt-specific shortcut",
        "special-case prompt",
        "typo correction",
    }
    found = sorted(term for term in scenario_rule_terms if term in text)
    if found:
        raise ValueError(f"material contract contains scenario-specific runtime rule terms: {found}")
