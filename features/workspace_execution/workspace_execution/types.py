"""Pydantic contracts for the workspace_execution feature."""

from __future__ import annotations

from datetime import UTC, datetime
import base64
import binascii
import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sharedai.servicekit.contracts import CapabilitiesResponse as ServiceCapabilitiesResponse
from sharedai.servicekit.contracts import HealthResponse as ServiceHealthResponse

from workspace_execution.validation_profiles import validation_profiles_payload


Identifier = Annotated[str, Field(min_length=3, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")]
RelativePath = Annotated[str, Field(min_length=1, max_length=4096)]
NetworkMode = Literal["disabled", "restricted"]
VmNetworkMode = Literal["none", "vm-internal", "dependency-cache"]
ExecutionProfile = Literal["standard", "test", "destructive"]
CommandAllowProfile = Literal["inspect", "test", "destructive"]
ValidationProfile = Literal[
    "python-basic",
    "python-pytest",
    "python-api",
    "docker-compose-static",
    "docker-compose-runtime",
    "stateful-postgres",
    "stateful-redis",
    "worker-queue",
    "cli",
    "artifact",
    "node-basic",
]
SessionStatus = Literal["created", "active", "closing", "closed", "expired", "failed"]
CommandStatus = Literal["queued", "running", "completed", "failed", "timed_out", "blocked"]
GitRemoteAcquireStatus = Literal["completed", "blocked"]
ArtifactOrigin = Literal["command", "input", "diff", "system"]
PublishStatus = Literal["published", "already_published", "blocked", "failed"]
WorkspaceWriteStatus = Literal["completed", "blocked"]
PatchApplyStatus = Literal["completed", "blocked"]
ArtifactPackageStatus = Literal["completed", "blocked"]
VmSessionStatus = Literal[
    "requested",
    "allocating",
    "ready",
    "failed",
    "cleanup_started",
    "cleanup_completed",
    "cleanup_failed",
]
VmIsolationMode = Literal["vm", "microvm", "vm-backed-proxy"]
VmLifecycleEventType = Literal[
    "material.vm.requested",
    "material.vm.ready",
    "material.vm.failed",
    "material.vm.rootfs_prewarm.started",
    "material.vm.rootfs_prewarm.completed",
    "material.vm.rootfs_prewarm.failed",
    "material.vm.rootfs_prewarm.blocked",
    "material.vm.cleanup.started",
    "material.vm.cleanup.completed",
    "material.vm.cleanup.failed",
]


class WorkspaceExecutionModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ErrorDetail(WorkspaceExecutionModel):
    code: Identifier
    message: str = Field(min_length=1, max_length=2048)
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(WorkspaceExecutionModel):
    success: Literal[False] = False
    error: ErrorDetail


class WorkspaceSource(WorkspaceExecutionModel):
    kind: Literal["workspace"] = "workspace"
    root_ref: Identifier
    paths: list[RelativePath] = Field(default_factory=list, max_length=256)
    state_hash: str | None = None


class EmptySource(WorkspaceExecutionModel):
    kind: Literal["empty"] = "empty"
    purpose: str = Field(default="generated_workspace", min_length=1, max_length=128)
    state_hash: str | None = None


class HostPathSource(WorkspaceExecutionModel):
    kind: Literal["host_path"] = "host_path"
    path: str = Field(min_length=1, max_length=4096)
    access_origin: Literal["direct_user_request", "system_inferred"]
    user_approved: bool = False
    approval_id: Identifier | None = None
    read_only: Literal[True] = True

    @model_validator(mode="after")
    def require_approval_for_system_inferred_paths(self) -> "HostPathSource":
        if self.access_origin == "system_inferred" and self.user_approved and not self.approval_id:
            raise ValueError("system-inferred host path grants require approval_id when user_approved is true")
        return self


class UploadSource(WorkspaceExecutionModel):
    kind: Literal["upload"] = "upload"
    upload_ref: Identifier
    filename: str | None = Field(default=None, max_length=512)
    media_type: str | None = Field(default=None, max_length=255)
    sha256: str | None = Field(default=None, min_length=64, max_length=64)


class StorageObjectSource(WorkspaceExecutionModel):
    kind: Literal["storage_object"] = "storage_object"
    object_ref: Identifier
    object_version: str | None = Field(default=None, max_length=128)
    sha256: str | None = Field(default=None, min_length=64, max_length=64)


MaterializationSource = Annotated[
    EmptySource | WorkspaceSource | HostPathSource | UploadSource | StorageObjectSource,
    Field(discriminator="kind"),
]


class ArtifactDescriptor(WorkspaceExecutionModel):
    artifact_id: Identifier
    path: RelativePath
    media_type: str = Field(min_length=1, max_length=255)
    sha256: str = Field(min_length=64, max_length=64)
    size_bytes: int = Field(ge=0)
    origin: ArtifactOrigin = "command"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, Any] = Field(default_factory=dict)


class SessionCreateRequest(WorkspaceExecutionModel):
    idempotency_key: Identifier | None = None
    source: MaterializationSource
    execution_profile: ExecutionProfile = "standard"
    network: NetworkMode = "disabled"
    ttl_seconds: int = Field(default=3600, ge=60, le=86400)
    metadata: dict[str, Any] = Field(default_factory=dict)
    limits: dict[str, Any] = Field(default_factory=dict)


class SessionResponse(WorkspaceExecutionModel):
    session_id: Identifier
    status: SessionStatus
    idempotency_key: Identifier | None = None
    source_hash: str = Field(min_length=16, max_length=128)
    state_hash: str = Field(min_length=16, max_length=128)
    scratch_ref: str
    workspace_ref: str | None = None
    expires_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)


class LifecycleEvent(WorkspaceExecutionModel):
    event_id: Identifier
    event_type: Identifier
    session_id: Identifier
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    payload: dict[str, Any] = Field(default_factory=dict)


class VmLifecycleEvent(WorkspaceExecutionModel):
    event_id: Identifier
    event_type: VmLifecycleEventType
    session_id: Identifier
    material_session_id: Identifier | None = None
    vm_session_id: Identifier | None = None
    status: VmSessionStatus
    isolation_mode: VmIsolationMode
    image_ref: str | None = Field(default=None, max_length=512)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    retryable: bool | None = None
    host_execution_used: bool = False
    docker_socket_exposed: bool = False
    fallback_to_host_allowed: bool = False
    cleanup_status: str | None = Field(default=None, max_length=128)
    reason: str | None = Field(default=None, max_length=2048)
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("host_execution_used")
    @classmethod
    def vm_event_must_not_report_host_execution(cls, value: bool) -> bool:
        if value:
            raise ValueError("VM lifecycle evidence cannot include host execution")
        return value

    @field_validator("docker_socket_exposed")
    @classmethod
    def vm_event_must_not_expose_docker_socket(cls, value: bool) -> bool:
        if value:
            raise ValueError("generated project VM evidence cannot expose the host Docker socket")
        return value

    @field_validator("fallback_to_host_allowed")
    @classmethod
    def vm_event_must_not_allow_host_fallback(cls, value: bool) -> bool:
        if value:
            raise ValueError("VM lifecycle evidence cannot allow host execution fallback")
        return value


class VmResourceLimits(WorkspaceExecutionModel):
    cpu_limit: float = Field(default=2.0, gt=0, le=64)
    memory_limit: str = Field(default="4g", min_length=1, max_length=32)
    disk_limit: str = Field(default="20g", min_length=1, max_length=32)
    ttl_seconds: int = Field(default=3600, ge=60, le=86400)


class SandboxIsolationProof(WorkspaceExecutionModel):
    vm_required: bool = True
    vm_backed: bool = False
    host_execution_used: bool = False
    host_docker_socket_exposed: bool = False
    fallback_to_host_allowed: bool = False
    network_mode: VmNetworkMode = "none"
    env_scrubbed: bool = True
    writable_roots_enforced: bool = True
    non_root_runner: bool = True
    cleanup_required: bool = True
    proof_status: Literal["unavailable", "requested", "ready", "failed", "cleanup_completed"] = "unavailable"
    evidence_refs: list[str] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def reject_host_escape(self) -> "SandboxIsolationProof":
        if self.host_execution_used:
            raise ValueError("sandbox isolation proof cannot include host execution")
        if self.host_docker_socket_exposed:
            raise ValueError("sandbox isolation proof cannot expose the host Docker socket")
        if self.fallback_to_host_allowed:
            raise ValueError("sandbox isolation proof cannot allow host execution fallback")
        return self


class VmSessionCreateRequest(WorkspaceExecutionModel):
    idempotency_key: Identifier | None = None
    material_session_id: Identifier | None = None
    task_id: Identifier | None = None
    trace_id: Identifier | None = None
    profile: str = Field(default="material-default", min_length=1, max_length=128)
    image_ref: str | None = Field(default=None, max_length=512)
    network_mode: VmNetworkMode = "none"
    resource_limits: VmResourceLimits = Field(default_factory=VmResourceLimits)
    metadata: dict[str, Any] = Field(default_factory=dict)


class VmSessionResponse(WorkspaceExecutionModel):
    vm_session_id: Identifier
    status: VmSessionStatus
    isolation_mode: VmIsolationMode = "vm"
    image_ref: str
    material_session_id: Identifier | None = None
    task_id: Identifier | None = None
    trace_id: Identifier | None = None
    network_mode: VmNetworkMode = "none"
    resource_limits: VmResourceLimits
    isolation_proof: SandboxIsolationProof
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime
    failure_code: str | None = Field(default=None, max_length=128)
    failure_reason: str | None = Field(default=None, max_length=2048)
    metadata: dict[str, Any] = Field(default_factory=dict)


class VmSessionCloseRequest(WorkspaceExecutionModel):
    idempotency_key: Identifier | None = None
    reason: str = Field(default="requested", min_length=1, max_length=255)
    cleanup: bool = True


class VmSessionCloseResponse(WorkspaceExecutionModel):
    vm_session_id: Identifier
    status: Literal["cleanup_completed", "cleanup_failed", "already_closed"]
    cleanup: bool
    isolation_proof: SandboxIsolationProof
    closed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class SessionReadRequest(WorkspaceExecutionModel):
    session_id: Identifier


class InputAttachRequest(WorkspaceExecutionModel):
    idempotency_key: Identifier | None = None
    sources: list[MaterializationSource] = Field(min_length=1, max_length=256)
    destination: RelativePath | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class InputAttachResponse(WorkspaceExecutionModel):
    session_id: Identifier
    input_ids: list[Identifier]
    state_hash: str = Field(min_length=16, max_length=128)
    attached_count: int = Field(default=0, ge=0)
    success: bool = True


class GitRemoteSourceAcquireRequest(WorkspaceExecutionModel):
    idempotency_key: Identifier
    url: str = Field(min_length=1, max_length=4096)
    destination: RelativePath | None = None
    depth: int = Field(default=1, ge=1, le=1000)
    timeout_seconds: int = Field(default=300, ge=5, le=7200)
    vm_session_id: Identifier | None = None
    material_session_id: Identifier | None = None
    requires_vm_backed_sandbox: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("url")
    @classmethod
    def url_must_be_git_remote(cls, value: str) -> str:
        cleaned = str(value or "").strip().strip(".,;:)】]}>'\"`")
        if not cleaned or "\x00" in cleaned or any(char.isspace() for char in cleaned):
            raise ValueError("git remote URL cannot be empty or contain whitespace/control characters")
        patterns = (
            r"^git@[A-Za-z0-9.-]+:[^\s`'\"<>]+?\.git$",
            r"^ssh://[^\s`'\"<>]+?\.git$",
            r"^https?://[A-Za-z0-9.-]+/[^\s`'\"<>]+/[^\s`'\"<>]+?(?:\.git)?$",
        )
        if not any(re.fullmatch(pattern, cleaned, flags=re.IGNORECASE) for pattern in patterns):
            raise ValueError("source URL must be a supported Git remote URL")
        return cleaned


class GitRemoteCloneAttempt(WorkspaceExecutionModel):
    url: str
    run_id: Identifier | None = None
    status: CommandStatus
    exit_code: int | None = None
    stdout_preview: str = ""
    stderr_preview: str = ""
    error: ErrorDetail | None = None


class GitRemoteSourceAcquireResponse(WorkspaceExecutionModel):
    session_id: Identifier
    status: GitRemoteAcquireStatus
    source_url: str
    effective_url: str | None = None
    destination: RelativePath
    state_hash: str = Field(min_length=16, max_length=128)
    clone_attempts: list[GitRemoteCloneAttempt] = Field(default_factory=list)
    evidence_context: dict[str, Any] = Field(default_factory=dict)
    error: ErrorDetail | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


def _normalize_sha256(value: str) -> str:
    normalized = value.removeprefix("sha256:")
    if not re.fullmatch(r"[A-Fa-f0-9]{64}", normalized):
        raise ValueError("sha256 values must be 64 hex characters, optionally prefixed with sha256:")
    return normalized.lower()


class WorkspaceFileWrite(WorkspaceExecutionModel):
    path: RelativePath
    content_b64: str = Field(min_length=0, max_length=4_000_000)
    sha256: str = Field(min_length=64, max_length=71)

    @field_validator("sha256")
    @classmethod
    def sha256_must_be_valid(cls, value: str) -> str:
        return _normalize_sha256(value)

    @field_validator("content_b64")
    @classmethod
    def content_must_be_base64(cls, value: str) -> str:
        try:
            base64.b64decode(value.encode("ascii"), validate=True)
        except (binascii.Error, UnicodeEncodeError) as exc:
            raise ValueError("content_b64 must be strict base64") from exc
        return value


class WorkspaceFileWriteResult(WorkspaceExecutionModel):
    path: RelativePath
    sha256: str = Field(min_length=64, max_length=64)
    size_bytes: int = Field(ge=0)
    before_sha256: str | None = Field(default=None, min_length=64, max_length=64)


class WorkspaceFileBatchWriteRequest(WorkspaceExecutionModel):
    idempotency_key: Identifier
    root: RelativePath = "."
    vm_session_id: Identifier | None = None
    material_session_id: Identifier | None = None
    files: list[WorkspaceFileWrite] = Field(min_length=1, max_length=512)
    mode: Literal["replace"] = "replace"
    verify_hashes: bool = True
    forbid_symlink_escape: bool = True
    forbid_absolute_paths: bool = True
    requires_vm_backed_sandbox: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class WorkspaceFileBatchWriteResponse(WorkspaceExecutionModel):
    write_id: Identifier
    session_id: Identifier
    status: WorkspaceWriteStatus
    state_hash: str = Field(min_length=16, max_length=128)
    file_count: int = Field(default=0, ge=0)
    files: list[WorkspaceFileWriteResult] = Field(default_factory=list)
    error: ErrorDetail | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class WorkspacePatch(WorkspaceExecutionModel):
    path: RelativePath
    expected_old_sha256: str | None = Field(default=None, min_length=64, max_length=71)
    unified_diff: str = Field(min_length=1, max_length=500_000)

    @field_validator("expected_old_sha256")
    @classmethod
    def expected_sha256_must_be_valid(cls, value: str | None) -> str | None:
        return _normalize_sha256(value) if value is not None else None

    @field_validator("unified_diff")
    @classmethod
    def patch_must_look_like_unified_diff(cls, value: str) -> str:
        if "--- " not in value or "+++ " not in value or "@@" not in value:
            raise ValueError("unified_diff must include file headers and at least one hunk")
        return value


class WorkspacePatchApplyRequest(WorkspaceExecutionModel):
    idempotency_key: Identifier
    vm_session_id: Identifier | None = None
    material_session_id: Identifier | None = None
    patches: list[WorkspacePatch] = Field(min_length=1, max_length=128)
    verify: bool = True
    forbid_symlink_escape: bool = True
    requires_vm_backed_sandbox: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class WorkspacePatchApplyResult(WorkspaceExecutionModel):
    path: RelativePath
    before_sha256: str | None = Field(default=None, min_length=64, max_length=64)
    after_sha256: str | None = Field(default=None, min_length=64, max_length=64)
    applied: bool = True


class WorkspacePatchApplyResponse(WorkspaceExecutionModel):
    patch_set_id: Identifier
    session_id: Identifier
    status: PatchApplyStatus
    state_hash: str = Field(min_length=16, max_length=128)
    applied_count: int = Field(default=0, ge=0)
    patches: list[WorkspacePatchApplyResult] = Field(default_factory=list)
    error: ErrorDetail | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class CommandRunRequest(WorkspaceExecutionModel):
    idempotency_key: Identifier | None = None
    cwd: RelativePath = "."
    argv: list[str] = Field(min_length=1, max_length=256)
    stdin_ref: str | None = None
    timeout_seconds: int = Field(default=120, ge=1, le=7200)
    risk_evidence_ref: str | None = None
    allow_profile: CommandAllowProfile = "test"
    validation_profile: ValidationProfile | None = None
    material_session_id: Identifier | None = None
    vm_session_id: Identifier | None = None
    requires_vm_backed_sandbox: bool = False
    env: dict[str, str] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("argv")
    @classmethod
    def argv_tokens_must_be_non_empty(cls, value: list[str]) -> list[str]:
        for token in value:
            if not token or not token.strip():
                raise ValueError("argv tokens must be non-empty strings")
        return value

    @field_validator("cwd")
    @classmethod
    def cwd_must_not_escape_workspace(cls, value: str) -> str:
        normalized = str(value or ".").replace("\\", "/")
        if "\x00" in normalized:
            raise ValueError("cwd cannot contain NUL bytes")
        if normalized.startswith(("/", "~")):
            raise ValueError("cwd must be relative to the workspace")
        if len(normalized) >= 2 and normalized[1] == ":":
            raise ValueError("cwd must not use drive-qualified paths")
        if any(part == ".." for part in normalized.split("/")):
            raise ValueError("cwd must not contain parent directory segments")
        return value


class CommandRunResponse(WorkspaceExecutionModel):
    run_id: Identifier
    status: CommandStatus
    exit_code: int | None = None
    stdout_ref: str | None = None
    stderr_ref: str | None = None
    duration_ms: int = Field(default=0, ge=0)
    changed: bool = False
    diff_ref: str | None = None
    artifacts: list[ArtifactDescriptor] = Field(default_factory=list)
    error: ErrorDetail | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class DiffFile(WorkspaceExecutionModel):
    path: RelativePath
    status: Literal["added", "modified", "deleted", "renamed", "unchanged"]
    additions: int = Field(default=0, ge=0)
    deletions: int = Field(default=0, ge=0)
    binary: bool = False
    patch: str | None = None
    patch_ref: str | None = None
    old_sha256: str | None = Field(default=None, min_length=64, max_length=64)
    new_sha256: str | None = Field(default=None, min_length=64, max_length=64)


class DiffResponse(WorkspaceExecutionModel):
    session_id: Identifier
    baseline_hash: str = Field(min_length=16, max_length=128)
    state_hash: str = Field(min_length=16, max_length=128)
    changed: bool
    files: list[DiffFile] = Field(default_factory=list)


class ArtifactListResponse(WorkspaceExecutionModel):
    session_id: Identifier
    artifacts: list[ArtifactDescriptor] = Field(default_factory=list)


class ArtifactPackageRequest(WorkspaceExecutionModel):
    idempotency_key: Identifier
    root: RelativePath
    vm_session_id: Identifier | None = None
    material_session_id: Identifier | None = None
    requires_vm_backed_sandbox: bool = False
    forbid_symlink_escape: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)


class ArtifactPackageResponse(WorkspaceExecutionModel):
    package_id: Identifier
    session_id: Identifier
    status: ArtifactPackageStatus
    state_hash: str = Field(min_length=16, max_length=128)
    artifact: ArtifactDescriptor | None = None
    error: ErrorDetail | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ArtifactPublishRequest(WorkspaceExecutionModel):
    idempotency_key: Identifier | None = None
    artifact_ids: list[Identifier] = Field(min_length=1, max_length=256)
    target: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class PublishedArtifact(WorkspaceExecutionModel):
    artifact_id: Identifier
    status: PublishStatus
    storage_object_ref: str | None = None
    chain_of_custody_ref: str | None = None
    materialized_path: str | None = None
    materialized_sha256: str | None = None
    extracted_path: str | None = None
    extracted_files_count: int | None = Field(default=None, ge=0)
    extracted_top_level_paths: list[str] = Field(default_factory=list)
    error: ErrorDetail | None = None


class ArtifactPublishResponse(WorkspaceExecutionModel):
    session_id: Identifier
    published: list[PublishedArtifact]


class SessionCloseRequest(WorkspaceExecutionModel):
    idempotency_key: Identifier | None = None
    reason: str = Field(default="requested", min_length=1, max_length=255)
    cleanup: bool = True


class SessionCloseResponse(WorkspaceExecutionModel):
    session_id: Identifier
    status: Literal["closed", "cleanup_scheduled", "already_closed"]
    cleanup: bool
    closed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class HealthResponse(ServiceHealthResponse):
    pass


class CapabilitiesResponse(ServiceCapabilitiesResponse):
    name: str = "workspace_execution"
    capabilities: list[str] = Field(
        default_factory=lambda: [
            "disposable_sessions",
            "workspace_snapshot_contracts",
            "sandbox_command_contracts",
            "structured_diffs",
            "transient_artifacts",
            "storage_guardian_publish_contract",
            "vm_lifecycle_event_contracts",
            "workspace_lifecycle_event_contracts",
            "vm_session_contracts",
            "batch_file_write_contracts",
            "patch_apply_contracts",
            "env_scrub_policy",
            "vm_backed_compose_runtime_proxy_contract",
            "host_path_source_contracts",
        ]
    )
    description: str = (
        "Disposable workspace execution substrate for policy-approved commands, "
        "diffs and transient artifacts inside isolated session copies."
    )
    policy: dict[str, Any] = Field(
        default_factory=lambda: {
            "executes_commands": True,
            "mutates_host": False,
            "writes_managed_storage": False,
            "runner_isolation_required": True,
            "vm_backed_sessions_required_for_generated_code": True,
            "host_execution_fallback": False,
            "host_execution_used": False,
            "host_docker_socket_exposed": False,
            "fallback_to_host_allowed": False,
            "host_docker_socket_exposed_to_generated_project": False,
            "vm_backed_sessions": False,
            "vm_runtime_status": "unavailable",
            "default_vm_network": "none",
            "generated_project_env_inherit": "none",
            "storage_publish_owner": "storage_guardian",
            "host_path_sources": True,
            "host_path_read_mode": "read_only_snapshot",
            "host_path_direct_user_request_grants": "task_scoped_read",
            "host_path_system_inferred": "approval_required",
            "host_path_writes": False,
            "validation_profiles": validation_profiles_payload(),
            "compose_runtime_proxy_configured": False,
            "compose_runtime_status": "not_configured",
            "compose_runtime_vm_backed": False,
            "compose_runtime_host_execution_used": False,
            "compose_runtime_host_docker_socket_exposed": False,
            "compose_runtime_fallback_to_host_allowed": False,
            "compose_runtime_failure_code": "docker_runtime_unavailable",
        }
    )
    endpoints: dict[str, str] = Field(
        default_factory=lambda: {
            "health": "/health",
            "capabilities": "/v1/workspace-execution/capabilities",
            "sessions": "/v1/workspace-execution/sessions",
            "session_events": "/v1/workspace-execution/sessions/{session_id}/events",
            "vm_sessions": "/v1/workspace-execution/vm-sessions",
            "vm_session_events": "/v1/workspace-execution/vm-sessions/{vm_session_id}/events",
            "vm_close": "/v1/workspace-execution/vm-sessions/{vm_session_id}/close",
            "inputs": "/v1/workspace-execution/sessions/{session_id}/inputs",
            "files_batch": "/v1/workspace-execution/sessions/{session_id}/files/batch",
            "patches": "/v1/workspace-execution/sessions/{session_id}/patches",
            "commands": "/v1/workspace-execution/sessions/{session_id}/commands",
            "diff": "/v1/workspace-execution/sessions/{session_id}/diff",
            "artifacts": "/v1/workspace-execution/sessions/{session_id}/artifacts",
            "artifact_package": "/v1/workspace-execution/sessions/{session_id}/artifacts/package",
            "publish": "/v1/workspace-execution/sessions/{session_id}/artifacts/publish",
            "close": "/v1/workspace-execution/sessions/{session_id}/close",
            "compose_runtime_capabilities": "/v1/compose-runtime/capabilities",
            "compose_runtime_run": "/v1/compose-runtime/run",
        }
    )
