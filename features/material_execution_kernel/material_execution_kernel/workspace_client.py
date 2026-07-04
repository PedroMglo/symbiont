"""Typed client boundary for the active sandbox owner.

The active sandbox owner applies writes, patches, commands and artifacts.  The
kernel only depends on this transport-shaped contract and never imports
``workspace_execution`` internals.
"""

from __future__ import annotations

import base64
import os
import time
from dataclasses import dataclass, field
from typing import Protocol

import httpx

from material_execution_kernel.material_builder_client import (
    GeneratedMaterialFile,
    MaterialPatchProposal,
    MaterialPatchSetProposal,
    MaterialValidationCommandProposal,
)

_TRANSIENT_HTTP_ERRORS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
    httpx.ReadTimeout,
    httpx.RemoteProtocolError,
    httpx.WriteError,
    httpx.WriteTimeout,
)


@dataclass(frozen=True)
class WorkspaceIssue:
    code: str
    message: str
    details: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class VmSessionResult:
    status: str
    owner: str = "features/workspace_execution"
    vm_session_id: str | None = None
    vm_backed: bool = False
    isolation_mode: str = "required"
    network_policy: str = "disabled_by_default"
    failure_code: str | None = None
    failure_reason: str | None = None


@dataclass(frozen=True)
class BatchWriteResult:
    status: str
    state_hash: str
    written_paths: list[str] = field(default_factory=list)
    issue: WorkspaceIssue | None = None


@dataclass(frozen=True)
class PatchApplyResult:
    status: str
    patch_set_id: str
    target_path: str
    before_sha256: str | None = None
    after_sha256: str | None = None
    issue: WorkspaceIssue | None = None


@dataclass(frozen=True)
class PatchSetApplyResult:
    status: str
    patch_set_id: str
    patches: list[PatchApplyResult] = field(default_factory=list)
    issue: WorkspaceIssue | None = None


@dataclass(frozen=True)
class CommandValidationResult:
    status: str
    command_run_id: str
    profile: str
    vm_session_id: str
    duration_ms: int = 0
    exit_code: int | None = None
    stdout_ref: str | None = None
    stderr_ref: str | None = None
    stdout_preview: str | None = None
    stderr_preview: str | None = None
    issue: WorkspaceIssue | None = None


@dataclass(frozen=True)
class ArtifactPackageResult:
    status: str
    path: str
    sha256: str
    size_bytes: int
    artifact_id: str | None = None
    issue: WorkspaceIssue | None = None


@dataclass(frozen=True)
class ArtifactPublishResult:
    status: str
    artifact_id: str
    storage_object_ref: str | None = None
    chain_of_custody_ref: str | None = None
    materialized_path: str | None = None
    materialized_sha256: str | None = None
    extracted_path: str | None = None
    extracted_files_count: int | None = None
    extracted_top_level_paths: list[str] = field(default_factory=list)
    issue: WorkspaceIssue | None = None


@dataclass(frozen=True)
class VmCleanupResult:
    cleanup_recorded: bool
    issue: WorkspaceIssue | None = None


class WorkspaceClient(Protocol):
    def request_vm_session(
        self,
        *,
        session_id: str,
        task_id: str,
        trace_id: str,
        idempotency_key: str,
        network_policy: str,
    ) -> VmSessionResult:
        """Request VM-backed sandbox evidence from the active sandbox owner."""

    def write_files_batch(
        self,
        *,
        session_id: str,
        material_session_id: str,
        vm_session_id: str,
        project_root: str,
        files: list[GeneratedMaterialFile],
        idempotency_key: str,
    ) -> BatchWriteResult:
        """Materialize generated files through the sandbox owner."""

    def run_validation(
        self,
        *,
        session_id: str,
        material_session_id: str,
        vm_session_id: str,
        project_root: str,
        profile: str,
        command: MaterialValidationCommandProposal | None = None,
        idempotency_key: str,
    ) -> CommandValidationResult:
        """Run one validation profile through the sandbox owner."""

    def apply_patch(
        self,
        *,
        session_id: str,
        material_session_id: str,
        vm_session_id: str,
        patch: MaterialPatchProposal,
        idempotency_key: str,
    ) -> PatchApplyResult:
        """Apply one repair patch through the active sandbox owner."""

    def apply_patch_set(
        self,
        *,
        session_id: str,
        material_session_id: str,
        vm_session_id: str,
        patch_set: MaterialPatchSetProposal,
        idempotency_key: str,
    ) -> PatchSetApplyResult:
        """Apply one atomic patch set through the active sandbox owner."""

    def package_artifact(
        self,
        *,
        session_id: str,
        material_session_id: str,
        vm_session_id: str,
        project_root: str,
        idempotency_key: str,
    ) -> ArtifactPackageResult:
        """Package the generated project as a transient sandbox artifact."""

    def publish_artifact(
        self,
        *,
        session_id: str,
        material_session_id: str,
        artifact_id: str,
        target: dict[str, object],
        idempotency_key: str,
    ) -> ArtifactPublishResult:
        """Publish a transient artifact through storage_guardian-owned boundaries."""

    def cleanup_vm(
        self,
        *,
        session_id: str,
        vm_session_id: str,
        idempotency_key: str,
    ) -> VmCleanupResult:
        """Record cleanup evidence for the VM-backed sandbox."""


class UnavailableWorkspaceClient:
    def request_vm_session(
        self,
        *,
        session_id: str,
        task_id: str,
        trace_id: str,
        idempotency_key: str,
        network_policy: str,
    ) -> VmSessionResult:
        return VmSessionResult(
            status="failed",
            failure_code="vm_runtime_unavailable",
            failure_reason="workspace client is not configured",
            network_policy=network_policy,
        )

    def write_files_batch(
        self,
        *,
        session_id: str,
        material_session_id: str,
        vm_session_id: str,
        project_root: str,
        files: list[GeneratedMaterialFile],
        idempotency_key: str,
    ) -> BatchWriteResult:
        return BatchWriteResult(
            status="blocked",
            state_hash="unavailable",
            issue=WorkspaceIssue(code="sandbox_client_unavailable", message="workspace client is not configured"),
        )

    def run_validation(
        self,
        *,
        session_id: str,
        material_session_id: str,
        vm_session_id: str,
        project_root: str,
        profile: str,
        command: MaterialValidationCommandProposal | None = None,
        idempotency_key: str,
    ) -> CommandValidationResult:
        return CommandValidationResult(
            status="blocked",
            command_run_id=f"run:{profile}:unavailable",
            profile=profile,
            vm_session_id=vm_session_id,
            issue=WorkspaceIssue(code="sandbox_client_unavailable", message="workspace client is not configured"),
        )

    def apply_patch(
        self,
        *,
        session_id: str,
        material_session_id: str,
        vm_session_id: str,
        patch: MaterialPatchProposal,
        idempotency_key: str,
    ) -> PatchApplyResult:
        return PatchApplyResult(
            status="blocked",
            patch_set_id="patch:unavailable",
            target_path=patch.target_path,
            issue=WorkspaceIssue(code="sandbox_client_unavailable", message="workspace client is not configured"),
        )

    def apply_patch_set(
        self,
        *,
        session_id: str,
        material_session_id: str,
        vm_session_id: str,
        patch_set: MaterialPatchSetProposal,
        idempotency_key: str,
    ) -> PatchSetApplyResult:
        return PatchSetApplyResult(
            status="blocked",
            patch_set_id="patch:unavailable",
            issue=WorkspaceIssue(code="sandbox_client_unavailable", message="workspace client is not configured"),
        )

    def package_artifact(
        self,
        *,
        session_id: str,
        material_session_id: str,
        vm_session_id: str,
        project_root: str,
        idempotency_key: str,
    ) -> ArtifactPackageResult:
        return ArtifactPackageResult(
            status="blocked",
            path="",
            sha256="sha256:" + "0" * 64,
            size_bytes=0,
            issue=WorkspaceIssue(code="sandbox_client_unavailable", message="workspace client is not configured"),
        )

    def publish_artifact(
        self,
        *,
        session_id: str,
        material_session_id: str,
        artifact_id: str,
        target: dict[str, object],
        idempotency_key: str,
    ) -> ArtifactPublishResult:
        return ArtifactPublishResult(
            status="blocked",
            artifact_id=artifact_id,
            issue=WorkspaceIssue(code="sandbox_client_unavailable", message="workspace client is not configured"),
        )

    def cleanup_vm(
        self,
        *,
        session_id: str,
        vm_session_id: str,
        idempotency_key: str,
    ) -> VmCleanupResult:
        return VmCleanupResult(
            cleanup_recorded=False,
            issue=WorkspaceIssue(code="sandbox_client_unavailable", message="workspace client is not configured"),
        )


class HTTPWorkspaceClient:
    """HTTP transport client for the active workspace sandbox owner."""

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
        self._workspace_sessions: dict[str, str] = {}

    def request_vm_session(
        self,
        *,
        session_id: str,
        task_id: str,
        trace_id: str,
        idempotency_key: str,
        network_policy: str,
    ) -> VmSessionResult:
        network_mode = "none" if network_policy in {"disabled_by_default", "none"} else "vm-internal"
        try:
            response = self._post(
                "/v1/workspace-execution/vm-sessions",
                {
                    "idempotency_key": idempotency_key,
                    "material_session_id": session_id,
                    "task_id": task_id,
                    "trace_id": trace_id,
                    "network_mode": network_mode,
                    "metadata": {"caller": "features/material_execution_kernel"},
                },
            )
        except RuntimeError as exc:
            return VmSessionResult(
                status="failed",
                owner="features/workspace_execution",
                vm_backed=False,
                network_policy=network_policy,
                failure_code="workspace_transport_failed",
                failure_reason=str(exc),
            )
        proof = response.get("isolation_proof") if isinstance(response.get("isolation_proof"), dict) else {}
        status = str(response.get("status") or "failed")
        vm_session_id = str(response.get("vm_session_id") or "")
        vm_backed = bool(proof.get("vm_backed"))
        if status == "ready" and vm_backed and vm_session_id:
            workspace_session = self._create_workspace_session(
                material_session_id=session_id,
                task_id=task_id,
                idempotency_key=f"{idempotency_key}:workspace",
            )
            self._workspace_sessions[session_id] = workspace_session
        return VmSessionResult(
            status=status,
            owner="features/workspace_execution",
            vm_session_id=vm_session_id or None,
            vm_backed=vm_backed,
            isolation_mode=str(response.get("isolation_mode") or "required"),
            network_policy=network_policy,
            failure_code=response.get("failure_code"),
            failure_reason=response.get("failure_reason"),
        )

    def write_files_batch(
        self,
        *,
        session_id: str,
        material_session_id: str,
        vm_session_id: str,
        project_root: str,
        files: list[GeneratedMaterialFile],
        idempotency_key: str,
    ) -> BatchWriteResult:
        workspace_session = self._workspace_sessions.get(session_id)
        if not workspace_session:
            return BatchWriteResult(
                status="blocked",
                state_hash="workspace_session_unavailable",
                issue=WorkspaceIssue(
                    code="workspace_session_unavailable",
                    message="workspace session was not created because VM-backed sandbox is not ready",
                ),
            )
        response = self._post(
            f"/v1/workspace-execution/sessions/{workspace_session}/files/batch",
            {
                "idempotency_key": idempotency_key,
                "root": project_root,
                "vm_session_id": vm_session_id,
                "material_session_id": material_session_id,
                "mode": "replace",
                "verify_hashes": True,
                "forbid_symlink_escape": True,
                "forbid_absolute_paths": True,
                "requires_vm_backed_sandbox": True,
                "files": [
                    {
                        "path": _workspace_file_path(project_root, file.path),
                        "content_b64": base64.b64encode(file.content.encode("utf-8")).decode("ascii"),
                        "sha256": file.sha256,
                    }
                    for file in files
                ],
            },
        )
        if response.get("status") != "completed":
            error = _error_to_issue(response.get("error"), default_code="workspace_materialization_failed")
            return BatchWriteResult(status="blocked", state_hash=str(response.get("state_hash") or ""), issue=error)
        return BatchWriteResult(
            status="completed",
            state_hash=str(response.get("state_hash") or ""),
            written_paths=[
                _manifest_file_path(project_root, str(item.get("path") or ""))
                for item in response.get("files", [])
                if isinstance(item, dict)
            ],
        )

    def run_validation(
        self,
        *,
        session_id: str,
        material_session_id: str,
        vm_session_id: str,
        project_root: str,
        profile: str,
        command: MaterialValidationCommandProposal | None = None,
        idempotency_key: str,
    ) -> CommandValidationResult:
        workspace_session = self._workspace_sessions.get(session_id)
        if not workspace_session:
            return CommandValidationResult(
                status="blocked",
                command_run_id=f"run:{profile}:workspace-unavailable",
                profile=profile,
                vm_session_id=vm_session_id,
                issue=WorkspaceIssue(
                    code="workspace_session_unavailable",
                    message="workspace session was not created because VM-backed sandbox is not ready",
                ),
            )
        command_plan = _validation_command(profile, project_root=project_root, command=command)
        if command_plan is None:
            return CommandValidationResult(
                status="blocked",
                command_run_id=f"run:{profile}:command-unavailable",
                profile=profile,
                vm_session_id=vm_session_id,
                issue=WorkspaceIssue(
                    code="validation_command_unavailable",
                    message="validation profile requires a project-specific validation command",
                    details={"profile": profile},
                ),
            )
        try:
            response = self._post(
                f"/v1/workspace-execution/sessions/{workspace_session}/commands",
                {
                    "idempotency_key": idempotency_key,
                    "cwd": command_plan.cwd,
                    "argv": command_plan.argv,
                    "allow_profile": "test",
                    "validation_profile": profile,
                    "material_session_id": material_session_id,
                    "vm_session_id": vm_session_id,
                    "requires_vm_backed_sandbox": True,
                    "timeout_seconds": command_plan.timeout_seconds,
                    "env": command_plan.env,
                    "metadata": {
                        "material_session_id": material_session_id,
                        "vm_session_id": vm_session_id,
                        "validation_command_purpose": command_plan.purpose,
                    },
                },
            )
        except RuntimeError as exc:
            return CommandValidationResult(
                status="blocked",
                command_run_id=f"run:{profile}:workspace-transport-failed",
                profile=profile,
                vm_session_id=vm_session_id,
                issue=WorkspaceIssue(
                    code="workspace_transport_failed",
                    message="workspace_execution command transport failed",
                    details={"error": str(exc), "profile": profile},
                ),
            )
        exit_code = response.get("exit_code")
        metadata = response.get("metadata") if isinstance(response.get("metadata"), dict) else {}
        status = "completed" if response.get("status") == "completed" and exit_code == 0 else "failed"
        issue = None
        if status != "completed":
            issue = _error_to_issue(response.get("error"), default_code="validation_failed")
            issue.details.update(
                {
                    "exit_code": exit_code,
                    "stdout_ref": response.get("stdout_ref"),
                    "stderr_ref": response.get("stderr_ref"),
                    "stdout_preview": metadata.get("stdout_preview"),
                    "stderr_preview": metadata.get("stderr_preview"),
                    "runtime_metadata": {
                        key: metadata.get(key)
                        for key in (
                            "backend",
                            "services",
                            "service_container_ids",
                            "health_checks",
                            "cleanup",
                            "logs_collected",
                            "host_execution_used",
                            "host_docker_socket_exposed",
                            "fallback_to_host_allowed",
                        )
                        if key in metadata
                    },
                }
            )
        return CommandValidationResult(
            status=status,
            command_run_id=str(response.get("run_id") or f"run:{profile}:missing"),
            profile=profile,
            vm_session_id=vm_session_id,
            duration_ms=int(response.get("duration_ms") or 0),
            exit_code=int(exit_code) if isinstance(exit_code, int) else None,
            stdout_ref=str(response.get("stdout_ref")) if response.get("stdout_ref") else None,
            stderr_ref=str(response.get("stderr_ref")) if response.get("stderr_ref") else None,
            stdout_preview=str(metadata.get("stdout_preview")) if metadata.get("stdout_preview") is not None else None,
            stderr_preview=str(metadata.get("stderr_preview")) if metadata.get("stderr_preview") is not None else None,
            issue=issue,
        )

    def apply_patch(
        self,
        *,
        session_id: str,
        material_session_id: str,
        vm_session_id: str,
        patch: MaterialPatchProposal,
        idempotency_key: str,
    ) -> PatchApplyResult:
        workspace_session = self._workspace_sessions.get(session_id)
        if not workspace_session:
            return PatchApplyResult(
                status="blocked",
                patch_set_id="patch:workspace-unavailable",
                target_path=patch.target_path,
                issue=WorkspaceIssue(
                    code="workspace_session_unavailable",
                    message="workspace session was not created because VM-backed sandbox is not ready",
                ),
            )
        response = self._post(
            f"/v1/workspace-execution/sessions/{workspace_session}/patches",
            {
                "idempotency_key": idempotency_key,
                "vm_session_id": vm_session_id,
                "material_session_id": material_session_id,
                "verify": True,
                "forbid_symlink_escape": True,
                "requires_vm_backed_sandbox": True,
                "metadata": {"material_session_id": material_session_id, "vm_session_id": vm_session_id},
                "patches": [
                    {
                        "path": patch.target_path,
                        "expected_old_sha256": patch.expected_old_sha256,
                        "unified_diff": patch.unified_diff,
                    }
                ],
            },
        )
        if response.get("status") != "completed":
            return PatchApplyResult(
                status="blocked",
                patch_set_id=str(response.get("patch_set_id") or "patch:failed"),
                target_path=patch.target_path,
                issue=_error_to_issue(response.get("error"), default_code="patch_apply_failed"),
            )
        patches = response.get("patches") if isinstance(response.get("patches"), list) else []
        first = patches[0] if patches and isinstance(patches[0], dict) else {}
        return PatchApplyResult(
            status="completed",
            patch_set_id=str(response.get("patch_set_id") or "patch:missing"),
            target_path=str(first.get("path") or patch.target_path),
            before_sha256=_sha256_prefixed(first.get("before_sha256")),
            after_sha256=_sha256_prefixed(first.get("after_sha256")),
        )

    def apply_patch_set(
        self,
        *,
        session_id: str,
        material_session_id: str,
        vm_session_id: str,
        patch_set: MaterialPatchSetProposal,
        idempotency_key: str,
    ) -> PatchSetApplyResult:
        workspace_session = self._workspace_sessions.get(session_id)
        if not workspace_session:
            return PatchSetApplyResult(
                status="blocked",
                patch_set_id="patch:workspace-unavailable",
                issue=WorkspaceIssue(
                    code="workspace_session_unavailable",
                    message="workspace session was not created because VM-backed sandbox is not ready",
                ),
            )
        response = self._post(
            f"/v1/workspace-execution/sessions/{workspace_session}/patches",
            {
                "idempotency_key": idempotency_key,
                "vm_session_id": vm_session_id,
                "material_session_id": material_session_id,
                "verify": True,
                "forbid_symlink_escape": True,
                "requires_vm_backed_sandbox": True,
                "metadata": {
                    "material_session_id": material_session_id,
                    "vm_session_id": vm_session_id,
                    "issue_id": patch_set.issue_id,
                    "patch_count": len(patch_set.patches),
                },
                "patches": [
                    {
                        "path": patch.target_path,
                        "expected_old_sha256": patch.expected_old_sha256,
                        "unified_diff": patch.unified_diff,
                    }
                    for patch in patch_set.patches
                ],
            },
        )
        if response.get("status") != "completed":
            return PatchSetApplyResult(
                status="blocked",
                patch_set_id=str(response.get("patch_set_id") or "patch:failed"),
                issue=_error_to_issue(response.get("error"), default_code="patch_set_apply_failed"),
            )
        raw_patches = response.get("patches") if isinstance(response.get("patches"), list) else []
        applied = [
            PatchApplyResult(
                status="completed" if item.get("applied", True) else "blocked",
                patch_set_id=str(response.get("patch_set_id") or "patch:missing"),
                target_path=str(item.get("path") or ""),
                before_sha256=_sha256_prefixed(item.get("before_sha256")),
                after_sha256=_sha256_prefixed(item.get("after_sha256")),
                issue=None if item.get("applied", True) else WorkspaceIssue(code="patch_apply_failed", message="patch failed"),
            )
            for item in raw_patches
            if isinstance(item, dict)
        ]
        if len(applied) != len(patch_set.patches) or any(item.status != "completed" for item in applied):
            return PatchSetApplyResult(
                status="blocked",
                patch_set_id=str(response.get("patch_set_id") or "patch:partial"),
                patches=applied,
                issue=WorkspaceIssue(
                    code="patch_set_partial_apply",
                    message="sandbox did not prove that every patch in the set was applied",
                    details={"expected_count": len(patch_set.patches), "applied_count": len(applied)},
                ),
            )
        return PatchSetApplyResult(
            status="completed",
            patch_set_id=str(response.get("patch_set_id") or "patch:missing"),
            patches=applied,
        )

    def package_artifact(
        self,
        *,
        session_id: str,
        material_session_id: str,
        vm_session_id: str,
        project_root: str,
        idempotency_key: str,
    ) -> ArtifactPackageResult:
        workspace_session = self._workspace_sessions.get(session_id)
        if not workspace_session:
            return ArtifactPackageResult(
                status="blocked",
                path="",
                sha256="sha256:" + "0" * 64,
                size_bytes=0,
                issue=WorkspaceIssue(
                    code="workspace_session_unavailable",
                    message="workspace session was not created because VM-backed sandbox is not ready",
                ),
            )
        response = self._post(
            f"/v1/workspace-execution/sessions/{workspace_session}/artifacts/package",
            {
                "idempotency_key": idempotency_key,
                "root": project_root,
                "vm_session_id": vm_session_id,
                "material_session_id": material_session_id,
                "requires_vm_backed_sandbox": True,
                "forbid_symlink_escape": True,
            },
        )
        if response.get("status") != "completed":
            return ArtifactPackageResult(
                status="blocked",
                path="",
                sha256="sha256:" + "0" * 64,
                size_bytes=0,
                issue=_error_to_issue(response.get("error"), default_code="artifact_packaging_failed"),
            )
        artifact = response.get("artifact") if isinstance(response.get("artifact"), dict) else {}
        return ArtifactPackageResult(
            status="completed",
            path=str(artifact.get("path") or ""),
            sha256="sha256:" + str(artifact.get("sha256") or ""),
            size_bytes=int(artifact.get("size_bytes") or 0),
            artifact_id=str(artifact.get("artifact_id") or "") or None,
        )

    def publish_artifact(
        self,
        *,
        session_id: str,
        material_session_id: str,
        artifact_id: str,
        target: dict[str, object],
        idempotency_key: str,
    ) -> ArtifactPublishResult:
        workspace_session = self._workspace_sessions.get(session_id)
        if not workspace_session:
            return ArtifactPublishResult(
                status="blocked",
                artifact_id=artifact_id,
                issue=WorkspaceIssue(
                    code="workspace_session_unavailable",
                    message="workspace session was not created because VM-backed sandbox is not ready",
                ),
            )
        response = self._post(
            f"/v1/workspace-execution/sessions/{workspace_session}/artifacts/publish",
            {
                "idempotency_key": idempotency_key,
                "artifact_ids": [artifact_id],
                "target": target,
                "metadata": {
                    "material_session_id": material_session_id,
                    "publication_owner": "storage_guardian",
                },
            },
        )
        published = response.get("published") if isinstance(response.get("published"), list) else []
        first = next((item for item in published if isinstance(item, dict)), {})
        if not first or first.get("status") not in {"published", "already_published"}:
            return ArtifactPublishResult(
                status="blocked",
                artifact_id=artifact_id,
                issue=_error_to_issue(first.get("error") if isinstance(first, dict) else None, default_code="artifact_publish_failed"),
            )
        return ArtifactPublishResult(
            status=str(first.get("status") or "published"),
            artifact_id=str(first.get("artifact_id") or artifact_id),
            storage_object_ref=str(first.get("storage_object_ref") or "") or None,
            chain_of_custody_ref=str(first.get("chain_of_custody_ref") or "") or None,
            materialized_path=str(first.get("materialized_path") or "") or None,
            materialized_sha256=str(first.get("materialized_sha256") or "") or None,
            extracted_path=str(first.get("extracted_path") or "") or None,
            extracted_files_count=(
                int(first["extracted_files_count"])
                if isinstance(first.get("extracted_files_count"), int)
                else None
            ),
            extracted_top_level_paths=[
                str(item)
                for item in (
                    first.get("extracted_top_level_paths")
                    if isinstance(first.get("extracted_top_level_paths"), list)
                    else []
                )
            ],
        )

    def cleanup_vm(
        self,
        *,
        session_id: str,
        vm_session_id: str,
        idempotency_key: str,
    ) -> VmCleanupResult:
        try:
            response = self._post(
                f"/v1/workspace-execution/vm-sessions/{vm_session_id}/close",
                {"idempotency_key": idempotency_key, "reason": "material_session_finished", "cleanup": True},
            )
        except RuntimeError as exc:
            return VmCleanupResult(
                cleanup_recorded=False,
                issue=WorkspaceIssue(code="vm_cleanup_failed", message=str(exc), details={"session_id": session_id}),
            )
        return VmCleanupResult(cleanup_recorded=str(response.get("status") or "") in {"cleanup_completed", "already_closed"})

    def _create_workspace_session(
        self,
        *,
        material_session_id: str,
        task_id: str,
        idempotency_key: str,
    ) -> str:
        response = self._post(
            "/v1/workspace-execution/sessions",
            {
                "idempotency_key": idempotency_key,
                "source": {
                    "kind": "upload",
                    "upload_ref": material_session_id,
                    "filename": "material_session.json",
                    "media_type": "application/json",
                },
                "execution_profile": "standard",
                "network": "disabled",
                "metadata": {
                    "material_session_id": material_session_id,
                    "task_id": task_id,
                    "created_by": "features/material_execution_kernel",
                },
            },
        )
        return str(response["session_id"])

    def _post(self, path: str, payload: dict[str, object]) -> dict[str, object]:
        headers = _auth_headers(self._api_key)
        url = f"{self._base_url}{path}"
        attempts = max(1, _int_env("MATERIAL_EXECUTION_KERNEL_WORKSPACE_HTTP_RETRIES", 5))
        delay = max(0.05, _float_env("MATERIAL_EXECUTION_KERNEL_WORKSPACE_HTTP_RETRY_DELAY_SECONDS", 0.25))
        last_exc: Exception | None = None
        for attempt in range(attempts):
            try:
                response = httpx.post(
                    url,
                    json=payload,
                    headers=headers,
                    timeout=self._timeout_seconds,
                )
                response.raise_for_status()
                data = response.json()
                break
            except httpx.HTTPStatusError as exc:
                raise RuntimeError(_error_detail(exc.response)) from exc
            except _TRANSIENT_HTTP_ERRORS as exc:
                last_exc = exc
                if attempt + 1 >= attempts:
                    raise RuntimeError(str(exc)) from exc
                time.sleep(min(3.0, delay * (2**attempt)))
            except ValueError as exc:
                raise RuntimeError(str(exc)) from exc
        else:
            raise RuntimeError(str(last_exc) if last_exc is not None else "workspace_execution request failed")
        if not isinstance(data, dict):
            raise RuntimeError("workspace_execution returned a non-object response")
        return data


def _validation_command(
    profile: str,
    *,
    project_root: str,
    command: MaterialValidationCommandProposal | None,
) -> MaterialValidationCommandProposal | None:
    deterministic_commands = {
        "python-basic": ["python", "-m", "compileall", "."],
        "python-pytest": ["python", "-m", "pytest"],
        "docker-compose-static": ["docker", "compose", "config"],
    }
    if profile in deterministic_commands:
        return MaterialValidationCommandProposal(
            profile=profile,
            argv=deterministic_commands[profile],
            cwd=project_root,
        )
    if command is not None:
        if command.profile != profile:
            return None
        return _normalize_validation_command(project_root=project_root, command=command)
    argv = {
        "docker-compose-runtime": ["docker-compose", "up", "-d"],
    }.get(profile)
    if argv is None:
        return None
    return MaterialValidationCommandProposal(profile=profile, argv=argv, cwd=project_root)


def _normalize_validation_command(
    *,
    project_root: str,
    command: MaterialValidationCommandProposal,
) -> MaterialValidationCommandProposal:
    cwd = str(command.cwd or ".").strip("/").replace("\\", "/") or "."
    argv = [_normalize_command_arg(project_root=project_root, cwd=cwd, arg=arg) for arg in command.argv]
    return MaterialValidationCommandProposal(
        profile=command.profile,
        argv=argv,
        cwd=cwd,
        timeout_seconds=command.timeout_seconds,
        env=dict(command.env),
        purpose=command.purpose,
    )


def _normalize_command_arg(*, project_root: str, cwd: str, arg: str) -> str:
    root = str(project_root or "").strip("/").replace("\\", "/")
    token = str(arg or "").strip("/").replace("\\", "/")
    if not root or not token:
        return arg
    if token != root and not token.startswith(f"{root}/"):
        return arg
    if cwd == root:
        return "." if token == root else token[len(root) + 1 :]
    if cwd.startswith(f"{root}/"):
        cwd_inside_root = cwd[len(root) + 1 :]
        relative_to_root = "." if token == root else token[len(root) + 1 :]
        if relative_to_root == cwd_inside_root:
            return "."
        if relative_to_root.startswith(f"{cwd_inside_root}/"):
            return relative_to_root[len(cwd_inside_root) + 1 :]
        return relative_to_root
    return arg


def _workspace_file_path(project_root: str, file_path: str) -> str:
    root = str(project_root or "").strip("/").replace("\\", "/")
    path = str(file_path or "").strip("/").replace("\\", "/")
    if root and path == root:
        return "."
    if root and path.startswith(f"{root}/"):
        return path[len(root) + 1 :]
    return path


def _manifest_file_path(project_root: str, workspace_file_path: str) -> str:
    root = str(project_root or "").strip("/").replace("\\", "/")
    path = str(workspace_file_path or "").strip("/").replace("\\", "/")
    if not root or path == ".":
        return path
    if path == root or path.startswith(f"{root}/"):
        return path
    return f"{root}/{path}"


def _error_to_issue(raw: object, *, default_code: str) -> WorkspaceIssue:
    if isinstance(raw, dict):
        return WorkspaceIssue(
            code=str(raw.get("code") or default_code),
            message=str(raw.get("message") or default_code),
            details=raw.get("details") if isinstance(raw.get("details"), dict) else {},
        )
    return WorkspaceIssue(code=default_code, message=default_code)


def _sha256_prefixed(raw: object) -> str | None:
    if not isinstance(raw, str) or not raw:
        return None
    value = raw.removeprefix("sha256:")
    if len(value) != 64:
        return str(raw)
    return f"sha256:{value}"


def _auth_headers(api_key: str) -> dict[str, str]:
    if not api_key:
        return {}
    return {"Authorization": f"Bearer {api_key}", "X-API-Key": api_key}


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _error_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:1000]
    if isinstance(body, dict):
        return str(body.get("detail", body))
    return str(body)


def workspace_client_from_env() -> WorkspaceClient:
    url = os.environ.get(
        "MATERIAL_EXECUTION_KERNEL_WORKSPACE_EXECUTION_URL",
        os.environ.get("ORC_SERVICES_WORKSPACE_EXECUTION_URL", "https://workspace-execution:8000"),
    ).strip()
    if not url:
        return UnavailableWorkspaceClient()
    return HTTPWorkspaceClient(
        base_url=url,
        api_key=_internal_api_key(),
        timeout_seconds=float(os.environ.get("MATERIAL_EXECUTION_KERNEL_WORKSPACE_TIMEOUT_SECONDS", "180")),
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
