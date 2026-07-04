"""Session lifecycle store for disposable workspace_execution sessions."""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
import tarfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from pathlib import Path
from threading import RLock, Thread
from typing import Any

from workspace_execution.compose_proxy import ComposeRuntimeProxyClient, ComposeRuntimeProxyPreflight
from workspace_execution.errors import WorkspaceExecutionError
from workspace_execution.materialization import (
    artifact_descriptors,
    apply_workspace_patch,
    diff_files,
    file_manifest,
    is_excluded_relative_path,
    manifest_hash,
    materialize_source,
    safe_child,
    write_workspace_file,
)
from workspace_execution.microvm import QemuMicroVmBackend
from workspace_execution.runner import DockerEphemeralRunner, LocalProcessRunner, RunnerLimits, _redact_and_truncate
from workspace_execution.security_policy import command_requires_vm_backed_sandbox, scrub_command_env
from workspace_execution.storage_client import ArtifactPublisher, StorageGuardianPublishError
from workspace_execution.types import (
    ArtifactListResponse,
    ArtifactPackageRequest,
    ArtifactPackageResponse,
    ArtifactPublishRequest,
    ArtifactPublishResponse,
    CommandRunRequest,
    CommandRunResponse,
    DiffResponse,
    ErrorDetail,
    WorkspaceFileBatchWriteRequest,
    WorkspaceFileBatchWriteResponse,
    WorkspaceFileWriteResult,
    WorkspacePatchApplyRequest,
    WorkspacePatchApplyResponse,
    WorkspacePatchApplyResult,
    InputAttachRequest,
    InputAttachResponse,
    LifecycleEvent,
    PublishedArtifact,
    SandboxIsolationProof,
    SessionCloseRequest,
    SessionCloseResponse,
    SessionCreateRequest,
    SessionResponse,
    VmLifecycleEvent,
    VmResourceLimits,
    VmSessionCloseRequest,
    VmSessionCloseResponse,
    VmSessionCreateRequest,
    VmSessionResponse,
)
from workspace_execution.validation_profiles import command_required_tools, validation_profile_spec


def _now() -> datetime:
    return datetime.now(UTC)


def _stable_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _microvm_command_error(error: str, request: CommandRunRequest) -> ErrorDetail:
    code = "command_failed"
    details: dict[str, Any] = {
        "cwd": request.cwd,
        "validation_profile": request.validation_profile,
        "vm_session_id": request.vm_session_id,
    }
    lowered = error.lower()
    if "no such file or directory" in lowered and "/workspace/project" in lowered:
        code = "workspace_root_missing" if str(request.cwd or ".") == "." else "workspace_cwd_missing"
        details["sandbox_path"] = f"/workspace/project/{request.cwd}".rstrip("/")
    elif code == "command_failed":
        code = _profile_failure_code(str(request.validation_profile or ""), default=code)
    return ErrorDetail(code=code, message=error, details=details)


def _profile_failure_code(profile_name: str, *, default: str) -> str:
    return {
        "python-api": "api_health_smoke_failed",
        "stateful-postgres": "stateful_service_unhealthy",
        "stateful-redis": "stateful_service_unhealthy",
        "worker-queue": "worker_queue_smoke_failed",
        "cli": "cli_smoke_failed",
    }.get(profile_name, default)


def _command_env_for_validation(request: CommandRunRequest) -> dict[str, str]:
    env = dict(request.env)
    profile = str(request.validation_profile or "")
    command_name = Path(request.argv[0]).name if request.argv else ""
    is_python_validation = profile.startswith("python-") or command_name in {"python", "python3", "pytest"}
    if not is_python_validation:
        return env
    python_path_entries = [".", "src"]
    existing = env.get("PYTHONPATH", "").strip()
    if existing:
        python_path_entries.append(existing)
    env["PYTHONPATH"] = ":".join(item for item in python_path_entries if item)
    return env


def _bounded_error_value(value: Any, *, redaction_terms: list[str], max_bytes: int = 240) -> str:
    redacted, _truncated = _redact_and_truncate(str(value), max_bytes, redaction_terms)
    return redacted


def _patch_error_details(
    result_metadata: dict[str, Any],
    *,
    request: WorkspacePatchApplyRequest,
    redaction_terms: list[str],
) -> dict[str, Any]:
    raw_patch_error = result_metadata.get("patch_error")
    details: dict[str, Any] = {
        "vm_session_id": request.vm_session_id,
        "patch_error_reason": None,
    }
    if not isinstance(raw_patch_error, dict):
        return details
    reason = str(raw_patch_error.get("reason") or "unknown")
    raw_details = raw_patch_error.get("details")
    safe_details: dict[str, Any] = {}
    if isinstance(raw_details, dict):
        for key, value in raw_details.items():
            safe_details[str(key)] = _bounded_error_value(value, redaction_terms=redaction_terms, max_bytes=240)
    details.update(
        {
            "patch_error_reason": reason,
            "patch_error_details": safe_details,
            "patch_error_message": _bounded_error_value(
                raw_patch_error.get("message") or reason,
                redaction_terms=redaction_terms,
                max_bytes=500,
            ),
        }
    )
    for key in (
        "path",
        "hunk_index",
        "old_line",
        "diff_line",
        "expected",
        "actual",
        "expected_snippet",
        "actual_snippet",
    ):
        if key in safe_details:
            details[key] = safe_details[key]
    return details


@dataclass
class _SessionRecord:
    response: SessionResponse
    fingerprint: str
    scratch_path: Path
    workspace_path: Path
    artifacts_path: Path
    logs_path: Path
    baseline_manifest: dict[str, dict[str, Any]]
    input_responses: dict[str, InputAttachResponse] = field(default_factory=dict)
    command_responses: dict[str, CommandRunResponse] = field(default_factory=dict)
    closed: bool = False


@dataclass
class _VmSessionRecord:
    response: VmSessionResponse
    fingerprint: str
    events: list[VmLifecycleEvent] = field(default_factory=list)
    closed: bool = False


@dataclass
class SessionStore:
    scratch_root: Path
    source_roots: dict[str, Path] = field(default_factory=dict)
    default_ttl_seconds: int = 3600
    command_timeout_seconds: int = 120
    max_output_bytes: int = 20000
    runner_backend: str = "docker_ephemeral"
    runner_image: str = "ai-local-command-sandbox:latest"
    runner_cpu_limit: float = 1.0
    runner_memory_limit: str = "512m"
    runner_pids_limit: int = 256
    sandbox_runtime: str = "docker"
    require_runtime: bool = False
    vm_backend: str = "unavailable"
    vm_control_url: str = ""
    vm_image_ref: str = "ai-local-workspace-vm:latest"
    vm_profile: str = "material-default"
    vm_qemu_binary: str = "qemu-system-x86_64"
    vm_kernel_path: str = ""
    vm_kvm_device: str = "/dev/kvm"
    vm_require_kvm: bool = True
    vm_cache_root: Path | None = None
    vm_boot_timeout_seconds: int = 45
    vm_prewarm_rootfs: bool = False
    vm_ttl_seconds: int = 3600
    vm_cpu_limit: float = 2.0
    vm_memory_limit: str = "4g"
    vm_disk_limit: str = "20g"
    compose_runtime_url: str = ""
    compose_runtime_token: str = ""
    compose_runtime_timeout_seconds: int = 30
    compose_runtime_backend: str = "dedicated-dind"
    compose_runtime_dind_image: str = "docker:27-dind"
    compose_runtime_runner_image: str = "ai-local-command-sandbox:latest"
    host_read_host_root: Path | None = None
    host_read_container_root: Path | None = None
    _sessions: dict[str, _SessionRecord] = field(default_factory=dict)
    _vm_sessions: dict[str, _VmSessionRecord] = field(default_factory=dict)
    _idempotency: dict[str, tuple[str, Any]] = field(default_factory=dict)
    _tool_preflight_cache: dict[str, tuple[str, ...]] = field(default_factory=dict)
    _rootfs_prewarm_keys: set[str] = field(default_factory=set)
    _events: list[LifecycleEvent] = field(default_factory=list)
    _lock: RLock = field(default_factory=RLock)

    def create_session(self, request: SessionCreateRequest) -> SessionResponse:
        with self._lock:
            self.cleanup_expired()
            fingerprint = self._session_fingerprint(request)
            if request.idempotency_key:
                existing = self._idempotency.get(f"session:{request.idempotency_key}")
                if existing is not None:
                    previous_fingerprint, session_id = existing
                    if previous_fingerprint != fingerprint:
                        raise WorkspaceExecutionError(
                            "idempotency_conflict",
                            "session create idempotency key was reused with different state",
                            status_code=HTTPStatus.CONFLICT,
                            details={"idempotency_key": request.idempotency_key},
                        )
                    return self._sessions[session_id].response

            session_id = f"session:{uuid.uuid4().hex}"
            ttl = request.ttl_seconds or self.default_ttl_seconds
            scratch_path = self.scratch_root / session_id.replace(":", "_")
            scratch_path.mkdir(parents=True, exist_ok=False)
            workspace_path = scratch_path / "workspace"
            artifacts_path = scratch_path / "artifacts"
            logs_path = scratch_path / "logs"
            workspace_path.mkdir()
            artifacts_path.mkdir()
            logs_path.mkdir()
            try:
                copied = materialize_source(
                    request.source,
                    source_roots=self.source_roots,
                    workspace_path=workspace_path,
                    host_read_host_root=self.host_read_host_root,
                    host_read_container_root=self.host_read_container_root,
                )
                baseline = file_manifest(workspace_path)
                source_hash = _stable_hash(request.source.model_dump(mode="json"))
                state_hash = manifest_hash(baseline)
                response = SessionResponse(
                    session_id=session_id,
                    status="active",
                    idempotency_key=request.idempotency_key,
                    source_hash=source_hash,
                    state_hash=state_hash,
                    scratch_ref=f"scratch://{session_id}",
                    workspace_ref=f"workspace://{session_id}",
                    expires_at=_now() + timedelta(seconds=ttl),
                    metadata={**request.metadata, "materialized_files": copied},
                )
            except Exception:
                shutil.rmtree(scratch_path, ignore_errors=True)
                raise
            self._sessions[session_id] = _SessionRecord(
                response=response,
                fingerprint=fingerprint,
                scratch_path=scratch_path,
                workspace_path=workspace_path,
                artifacts_path=artifacts_path,
                logs_path=logs_path,
                baseline_manifest=baseline,
            )
            if request.idempotency_key:
                self._idempotency[f"session:{request.idempotency_key}"] = (fingerprint, session_id)
            self._record_event(
                "workspace.session.created",
                session_id=session_id,
                payload={
                    "source_hash": source_hash,
                    "state_hash": state_hash,
                    "execution_profile": request.execution_profile,
                    "network": request.network,
                    "sandbox_runtime": self.sandbox_runtime,
                    "runtime_required": self.require_runtime,
                    "expires_at": response.expires_at.isoformat(),
                    "materialized_files": copied,
                },
            )
            return response

    def create_vm_session(self, request: VmSessionCreateRequest) -> VmSessionResponse:
        with self._lock:
            fingerprint = _stable_hash(request.model_dump(mode="json"))
            if request.idempotency_key:
                existing = self._idempotency.get(f"vm:{request.idempotency_key}")
                if existing is not None:
                    previous_fingerprint, vm_session_id = existing
                    if previous_fingerprint != fingerprint:
                        raise WorkspaceExecutionError(
                            "idempotency_conflict",
                            "VM session idempotency key was reused with different state",
                            status_code=HTTPStatus.CONFLICT,
                            details={"idempotency_key": request.idempotency_key},
                        )
                    return self._vm_sessions[vm_session_id].response

            vm_session_id = f"vm:{uuid.uuid4().hex}"
            image_ref = request.image_ref or self.vm_image_ref
            limits = request.resource_limits or VmResourceLimits(
                cpu_limit=self.vm_cpu_limit,
                memory_limit=self.vm_memory_limit,
                disk_limit=self.vm_disk_limit,
                ttl_seconds=self.vm_ttl_seconds,
            )
            created_at = _now()
            expires_at = created_at + timedelta(seconds=limits.ttl_seconds)
            runtime_status, failure_code, failure_reason = self.vm_backend_preflight()
            ready = runtime_status == "ready" and failure_code is None
            status = "ready" if ready else "failed"
            proof = SandboxIsolationProof(
                vm_backed=ready,
                network_mode=request.network_mode,
                proof_status="ready" if ready else "failed",
                evidence_refs=[f"workspace_execution.vm_session:{vm_session_id}"],
            )
            response = VmSessionResponse(
                vm_session_id=vm_session_id,
                status=status,
                isolation_mode="microvm" if ready and self.vm_backend == "microvm" else "vm",
                image_ref=image_ref,
                material_session_id=request.material_session_id,
                task_id=request.task_id,
                trace_id=request.trace_id,
                network_mode=request.network_mode,
                resource_limits=limits,
                isolation_proof=proof,
                created_at=created_at,
                expires_at=expires_at,
                failure_code=None if ready else failure_code,
                failure_reason=None if ready else failure_reason,
                metadata={
                    **request.metadata,
                    "vm_backend": self.vm_backend,
                    "vm_runtime_status": runtime_status,
                    "vm_control_endpoint_configured": bool(self.vm_control_url),
                    "vm_profile": request.profile or self.vm_profile,
                },
            )
            events = [
                VmLifecycleEvent(
                    event_id=f"evt:{uuid.uuid4().hex}",
                    event_type="material.vm.requested",
                    session_id=vm_session_id,
                    material_session_id=request.material_session_id,
                    vm_session_id=vm_session_id,
                    status="requested",
                    isolation_mode=response.isolation_mode,
                    image_ref=image_ref,
                    retryable=False,
                    payload={
                        "vm_backend": self.vm_backend,
                        "vm_runtime_status": runtime_status,
                        "vm_control_endpoint_configured": bool(self.vm_control_url),
                        "network_mode": request.network_mode,
                    },
                )
            ]
            if ready:
                events.append(
                    VmLifecycleEvent(
                        event_id=f"evt:{uuid.uuid4().hex}",
                        event_type="material.vm.ready",
                        session_id=vm_session_id,
                        material_session_id=request.material_session_id,
                        vm_session_id=vm_session_id,
                        status="ready",
                        isolation_mode=response.isolation_mode,
                        image_ref=image_ref,
                        retryable=False,
                    payload={
                        "vm_backend": self.vm_backend,
                        "vm_runtime_status": runtime_status,
                        "network_mode": request.network_mode,
                        "host_execution_used": False,
                        "host_docker_socket_exposed": False,
                        "fallback_to_host_allowed": False,
                    },
                )
                )
            else:
                events.append(
                    VmLifecycleEvent(
                        event_id=f"evt:{uuid.uuid4().hex}",
                        event_type="material.vm.failed",
                        session_id=vm_session_id,
                        material_session_id=request.material_session_id,
                        vm_session_id=vm_session_id,
                        status="failed",
                        isolation_mode=response.isolation_mode,
                        image_ref=image_ref,
                        retryable=failure_code in {"vm_runtime_unavailable", "vm_backend_not_configured"},
                        reason=failure_reason,
                        payload={
                            "vm_backend": self.vm_backend,
                            "vm_runtime_status": runtime_status,
                            "vm_control_endpoint_configured": bool(self.vm_control_url),
                            "network_mode": request.network_mode,
                        },
                    )
                )
            self._vm_sessions[vm_session_id] = _VmSessionRecord(
                response=response,
                fingerprint=fingerprint,
                events=events,
            )
            if request.idempotency_key:
                self._idempotency[f"vm:{request.idempotency_key}"] = (fingerprint, vm_session_id)
            if ready:
                self._maybe_start_microvm_rootfs_prewarm(
                    vm_session_id=vm_session_id,
                    material_session_id=request.material_session_id,
                    image_ref=image_ref,
                )
            return response

    def vm_backend_preflight(self) -> tuple[str, str | None, str | None]:
        """Return runtime status and typed failure for the configured VM backend."""
        if self.vm_backend == "microvm":
            result = self._microvm_backend().preflight(verify_rootfs_image=True)
            return result.status, result.failure_code, result.failure_reason
        if self.vm_backend == "unavailable":
            return (
                "unavailable",
                "vm_runtime_unavailable",
                "VM backend is not configured for workspace_execution",
            )
        if self.vm_backend in {"external", "vm-proxy"} and not self.vm_control_url:
            return (
                "not_configured",
                "vm_backend_not_configured",
                "VM backend requires WORKSPACE_EXECUTION_VM_CONTROL_URL",
            )
        return (
            "unimplemented",
            "vm_backend_unimplemented",
            "configured VM backend has no active workspace_execution client implementation",
        )

    def _microvm_backend(self) -> QemuMicroVmBackend:
        return QemuMicroVmBackend.from_settings(
            qemu_binary=self.vm_qemu_binary,
            kernel_path=self.vm_kernel_path,
            rootfs_image=self.vm_image_ref,
            cache_root=self.vm_cache_root or (self.scratch_root / "microvm-cache"),
            require_kvm=self.vm_require_kvm,
            kvm_device=self.vm_kvm_device,
            memory_limit=self.vm_memory_limit,
            cpu_limit=self.vm_cpu_limit,
            boot_timeout_seconds=self.vm_boot_timeout_seconds,
        )

    def _maybe_start_microvm_rootfs_prewarm(
        self,
        *,
        vm_session_id: str,
        material_session_id: str | None,
        image_ref: str,
    ) -> None:
        if not self.vm_prewarm_rootfs or self.vm_backend != "microvm":
            return
        cache_key = _stable_hash(
            {
                "vm_backend": self.vm_backend,
                "image_ref": image_ref,
                "kernel_path": self.vm_kernel_path,
                "cache_root": str(self.vm_cache_root or (self.scratch_root / "microvm-cache")),
            }
        )
        if cache_key in self._rootfs_prewarm_keys:
            return
        self._rootfs_prewarm_keys.add(cache_key)
        thread = Thread(
            target=self._prewarm_microvm_rootfs,
            kwargs={
                "vm_session_id": vm_session_id,
                "material_session_id": material_session_id,
                "cache_key": cache_key,
                "image_ref": image_ref,
            },
            name=f"workspace-microvm-prewarm-{cache_key[:8]}",
            daemon=True,
        )
        thread.start()

    def _prewarm_microvm_rootfs(
        self,
        *,
        vm_session_id: str,
        material_session_id: str | None,
        cache_key: str,
        image_ref: str,
    ) -> None:
        started_at = _now()
        self._record_microvm_prewarm_event(
            "material.vm.rootfs_prewarm.started",
            vm_session_id=vm_session_id,
            material_session_id=material_session_id,
            status="allocating",
            image_ref=image_ref,
            started_at=started_at,
            payload={"cache_key": cache_key, "vm_backend": self.vm_backend},
        )
        started = time.time()
        result = self._microvm_backend().warm_rootfs_cache()
        duration_ms = int((time.time() - started) * 1000)
        raw_status = str(result.get("status") or "failed")
        if raw_status == "completed":
            event_type = "material.vm.rootfs_prewarm.completed"
            status = "ready"
        elif raw_status == "blocked":
            event_type = "material.vm.rootfs_prewarm.blocked"
            status = "failed"
        else:
            event_type = "material.vm.rootfs_prewarm.failed"
            status = "failed"
        payload = {
            **result,
            "cache_key": cache_key,
            "vm_backend": self.vm_backend,
            "duration_ms": duration_ms,
        }
        self._record_microvm_prewarm_event(
            event_type,
            vm_session_id=vm_session_id,
            material_session_id=material_session_id,
            status=status,
            image_ref=image_ref,
            started_at=started_at,
            finished_at=_now(),
            duration_ms=duration_ms,
            reason=str(result.get("error") or result.get("error_code") or "") or None,
            payload=payload,
        )

    def _record_microvm_prewarm_event(
        self,
        event_type: str,
        *,
        vm_session_id: str,
        material_session_id: str | None,
        status: str,
        image_ref: str,
        payload: dict[str, Any],
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
        duration_ms: int | None = None,
        reason: str | None = None,
    ) -> None:
        with self._lock:
            vm_record = self._vm_sessions.get(vm_session_id)
            if vm_record is None:
                return
            event = VmLifecycleEvent(
                event_id=f"evt:{uuid.uuid4().hex}",
                event_type=event_type,  # type: ignore[arg-type]
                session_id=vm_session_id,
                material_session_id=material_session_id,
                vm_session_id=vm_session_id,
                status=status,  # type: ignore[arg-type]
                isolation_mode=vm_record.response.isolation_mode,
                image_ref=image_ref,
                started_at=started_at,
                finished_at=finished_at,
                duration_ms=duration_ms,
                retryable=event_type.endswith(".failed") or event_type.endswith(".blocked"),
                reason=reason,
                payload=payload,
            )
            vm_record.events.append(event)
        self._record_event(
            event_type.replace("material.", "workspace."),
            session_id=vm_session_id,
            payload={
                **payload,
                "vm_session_id": vm_session_id,
                "material_session_id": material_session_id,
                "status": status,
                "duration_ms": duration_ms,
                "host_execution_used": False,
            },
        )

    def _compose_runtime_proxy(self) -> ComposeRuntimeProxyClient:
        return ComposeRuntimeProxyClient(
            base_url=self.compose_runtime_url,
            token=self.compose_runtime_token,
            timeout_seconds=self.compose_runtime_timeout_seconds,
        )

    def compose_runtime_preflight(self) -> ComposeRuntimeProxyPreflight:
        return self._compose_runtime_proxy().preflight()

    def _vm_session_backend(self, vm_session_id: str | None) -> str:
        if not vm_session_id:
            return ""
        vm_record = self._vm_sessions.get(vm_session_id)
        if vm_record is None:
            return ""
        return str(vm_record.response.metadata.get("vm_backend") or "")

    def _ready_microvm_backend(self, vm_session_id: str | None) -> QemuMicroVmBackend | None:
        if not self._has_ready_vm_session(vm_session_id):
            return None
        if self._vm_session_backend(vm_session_id) != "microvm":
            return None
        return self._microvm_backend()

    def get_vm_session(self, vm_session_id: str) -> VmSessionResponse:
        with self._lock:
            record = self._require_vm_session(vm_session_id)
            return record.response

    def list_vm_events(self, vm_session_id: str) -> list[VmLifecycleEvent]:
        with self._lock:
            record = self._require_vm_session(vm_session_id)
            return list(record.events)

    def close_vm_session(self, vm_session_id: str, request: VmSessionCloseRequest) -> VmSessionCloseResponse:
        with self._lock:
            record = self._require_vm_session(vm_session_id)
            if record.closed:
                status = "already_closed"
            else:
                record.closed = True
                record.response.isolation_proof.proof_status = "cleanup_completed"
                event = VmLifecycleEvent(
                    event_id=f"evt:{uuid.uuid4().hex}",
                    event_type="material.vm.cleanup.completed",
                    session_id=vm_session_id,
                    material_session_id=record.response.material_session_id,
                    vm_session_id=vm_session_id,
                    status="cleanup_completed",
                    isolation_mode=record.response.isolation_mode,
                    image_ref=record.response.image_ref,
                    cleanup_status="completed",
                    payload={"reason": request.reason, "cleanup": request.cleanup},
                )
                record.events.append(event)
                status = "cleanup_completed"
            return VmSessionCloseResponse(
                vm_session_id=vm_session_id,
                status=status,
                cleanup=request.cleanup,
                isolation_proof=record.response.isolation_proof,
            )

    def attach_inputs(self, session_id: str, request: InputAttachRequest) -> InputAttachResponse:
        with self._lock:
            record = self._require_open_session(session_id)
            fingerprint = _stable_hash(
                {
                    "session_id": session_id,
                    "sources": [source.model_dump(mode="json") for source in request.sources],
                    "destination": request.destination,
                }
            )
            if request.idempotency_key:
                key = f"input:{session_id}:{request.idempotency_key}"
                existing = self._idempotency.get(key)
                if existing is not None:
                    previous_fingerprint, response = existing
                    if previous_fingerprint != fingerprint:
                        raise WorkspaceExecutionError(
                            "idempotency_conflict",
                            "input attach idempotency key was reused with different state",
                            status_code=HTTPStatus.CONFLICT,
                            details={"idempotency_key": request.idempotency_key},
                        )
                    return response
            else:
                key = ""
            destination = record.workspace_path
            if request.destination:
                from workspace_execution.materialization import safe_child

                destination = safe_child(record.workspace_path, request.destination)
                destination.mkdir(parents=True, exist_ok=True)
            attached_count = 0
            input_ids: list[str] = []
            for source in request.sources:
                attached_count += materialize_source(
                    source,
                    source_roots=self.source_roots,
                    workspace_path=destination,
                    host_read_host_root=self.host_read_host_root,
                    host_read_container_root=self.host_read_container_root,
                )
                input_id = f"input:{hashlib.sha256(source.model_dump_json().encode('utf-8')).hexdigest()[:16]}"
                input_ids.append(input_id)
            current = file_manifest(record.workspace_path)
            state_hash = manifest_hash(current)
            record.response.state_hash = state_hash
            response = InputAttachResponse(
                session_id=session_id,
                input_ids=input_ids,
                state_hash=state_hash,
                attached_count=attached_count,
            )
            if key:
                self._idempotency[key] = (fingerprint, response)
            self._record_event(
                "workspace.input.attached",
                session_id=session_id,
                payload={"input_ids": input_ids, "attached_count": attached_count, "state_hash": state_hash},
            )
            return response

    def write_files_batch(
        self,
        session_id: str,
        request: WorkspaceFileBatchWriteRequest,
    ) -> WorkspaceFileBatchWriteResponse:
        with self._lock:
            record = self._require_open_session(session_id)
            fingerprint = _stable_hash(
                {
                    "session_id": session_id,
                    "root": request.root,
                    "files": [file.model_dump(mode="json") for file in request.files],
                    "mode": request.mode,
                    "verify_hashes": request.verify_hashes,
                    "forbid_symlink_escape": request.forbid_symlink_escape,
                    "requires_vm_backed_sandbox": request.requires_vm_backed_sandbox,
                }
            )
            key = f"files_batch:{session_id}:{request.idempotency_key}"
            existing = self._idempotency.get(key)
            if existing is not None:
                previous_fingerprint, response = existing
                if previous_fingerprint != fingerprint:
                    raise WorkspaceExecutionError(
                        "idempotency_conflict",
                        "file batch write idempotency key was reused with different state",
                        status_code=HTTPStatus.CONFLICT,
                        details={"idempotency_key": request.idempotency_key},
                    )
                return response
            blocked = self._vm_required_write_block(record, request, fingerprint=fingerprint, idempotency_key=key)
            if blocked is not None:
                return blocked
            microvm = self._ready_microvm_backend(request.vm_session_id)
            if request.requires_vm_backed_sandbox and microvm is not None:
                return self._write_files_batch_microvm(
                    record,
                    request,
                    microvm=microvm,
                    fingerprint=fingerprint,
                    idempotency_key=key,
                )
            root = safe_child(record.workspace_path, request.root)
            if root.exists() and not root.is_dir():
                raise WorkspaceExecutionError(
                    "path_not_allowed",
                    "file batch write root must be a directory inside the session",
                    status_code=HTTPStatus.BAD_REQUEST,
                    details={"root": request.root},
                )
            written: list[WorkspaceFileWriteResult] = []
            for file in request.files:
                content = base64.b64decode(file.content_b64.encode("ascii"), validate=True)
                actual_sha = hashlib.sha256(content).hexdigest()
                if request.verify_hashes and actual_sha != file.sha256:
                    raise WorkspaceExecutionError(
                        "checksum_mismatch",
                        "file content sha256 does not match the declared hash",
                        status_code=HTTPStatus.BAD_REQUEST,
                        details={"path": file.path, "expected_sha256": file.sha256, "actual_sha256": actual_sha},
                    )
                before_sha, after_sha, size_bytes = write_workspace_file(
                    workspace_path=root,
                    relative_path=file.path,
                    content=content,
                    forbid_symlink_escape=request.forbid_symlink_escape,
                )
                written.append(
                    WorkspaceFileWriteResult(
                        path=str(file.path),
                        before_sha256=before_sha,
                        sha256=after_sha,
                        size_bytes=size_bytes,
                    )
                )
            current = file_manifest(record.workspace_path)
            state_hash = manifest_hash(current)
            record.response.state_hash = state_hash
            response = WorkspaceFileBatchWriteResponse(
                write_id=f"write:{uuid.uuid4().hex}",
                session_id=session_id,
                status="completed",
                state_hash=state_hash,
                file_count=len(written),
                files=written,
                metadata={
                    "root": request.root,
                    "mode": request.mode,
                    "verify_hashes": request.verify_hashes,
                    "host_execution_used": False,
                    "vm_session_id": request.vm_session_id,
                    "material_session_id": request.material_session_id,
                },
            )
            self._idempotency[key] = (fingerprint, response)
            self._record_event(
                "workspace.files.batch_written",
                session_id=session_id,
                payload={
                    "write_id": response.write_id,
                    "file_count": response.file_count,
                    "state_hash": state_hash,
                    "paths": [item.path for item in written],
                    "host_execution_used": False,
                    "vm_session_id": request.vm_session_id,
                    "material_session_id": request.material_session_id,
                },
            )
            return response

    def apply_patches(
        self,
        session_id: str,
        request: WorkspacePatchApplyRequest,
    ) -> WorkspacePatchApplyResponse:
        with self._lock:
            record = self._require_open_session(session_id)
            fingerprint = _stable_hash(
                {
                    "session_id": session_id,
                    "patches": [patch.model_dump(mode="json") for patch in request.patches],
                    "verify": request.verify,
                    "forbid_symlink_escape": request.forbid_symlink_escape,
                    "requires_vm_backed_sandbox": request.requires_vm_backed_sandbox,
                }
            )
            key = f"patches:{session_id}:{request.idempotency_key}"
            existing = self._idempotency.get(key)
            if existing is not None:
                previous_fingerprint, response = existing
                if previous_fingerprint != fingerprint:
                    raise WorkspaceExecutionError(
                        "idempotency_conflict",
                        "patch apply idempotency key was reused with different state",
                        status_code=HTTPStatus.CONFLICT,
                        details={"idempotency_key": request.idempotency_key},
                    )
                return response
            blocked = self._vm_required_patch_block(record, request, fingerprint=fingerprint, idempotency_key=key)
            if blocked is not None:
                return blocked
            microvm = self._ready_microvm_backend(request.vm_session_id)
            if request.requires_vm_backed_sandbox and microvm is not None:
                return self._apply_patches_microvm(
                    record,
                    request,
                    microvm=microvm,
                    fingerprint=fingerprint,
                    idempotency_key=key,
                )
            applied: list[WorkspacePatchApplyResult] = []
            for patch in request.patches:
                before_sha, after_sha = apply_workspace_patch(
                    workspace_path=record.workspace_path,
                    relative_path=patch.path,
                    unified_diff=patch.unified_diff,
                    expected_old_sha256=patch.expected_old_sha256 if request.verify else None,
                    forbid_symlink_escape=request.forbid_symlink_escape,
                )
                applied.append(
                    WorkspacePatchApplyResult(
                        path=str(patch.path),
                        before_sha256=before_sha,
                        after_sha256=after_sha,
                    )
                )
            current = file_manifest(record.workspace_path)
            state_hash = manifest_hash(current)
            record.response.state_hash = state_hash
            response = WorkspacePatchApplyResponse(
                patch_set_id=f"patch:{uuid.uuid4().hex}",
                session_id=session_id,
                status="completed",
                state_hash=state_hash,
                applied_count=len(applied),
                patches=applied,
                metadata={
                    "verify": request.verify,
                    "host_execution_used": False,
                    "vm_session_id": request.vm_session_id,
                    "material_session_id": request.material_session_id,
                },
            )
            self._idempotency[key] = (fingerprint, response)
            self._record_event(
                "workspace.patch.applied",
                session_id=session_id,
                payload={
                    "patch_set_id": response.patch_set_id,
                    "applied_count": response.applied_count,
                    "state_hash": state_hash,
                    "paths": [item.path for item in applied],
                    "host_execution_used": False,
                    "vm_session_id": request.vm_session_id,
                    "material_session_id": request.material_session_id,
                },
            )
            return response

    def run_command(self, session_id: str, request: CommandRunRequest) -> CommandRunResponse:
        with self._lock:
            record = self._require_open_session(session_id)
            fingerprint = _stable_hash(
                {
                    "session_id": session_id,
                    "argv": request.argv,
                    "cwd": request.cwd,
                    "state_hash": record.response.state_hash,
                    "allow_profile": request.allow_profile,
                    "timeout_seconds": request.timeout_seconds,
                }
            )
            if request.idempotency_key:
                key = f"command:{session_id}:{request.idempotency_key}"
                existing = self._idempotency.get(key)
                if existing is not None:
                    previous_fingerprint, response = existing
                    if previous_fingerprint != fingerprint:
                        raise WorkspaceExecutionError(
                            "idempotency_conflict",
                            "command idempotency key was reused with different state",
                            status_code=HTTPStatus.CONFLICT,
                            details={"idempotency_key": request.idempotency_key},
                        )
                    return response
            else:
                key = ""
            if request.allow_profile == "destructive" and not request.risk_evidence_ref:
                raise WorkspaceExecutionError(
                    "risk_evidence_required",
                    "destructive workspace commands require execution_policy evidence",
                    status_code=HTTPStatus.FORBIDDEN,
                )
            profile_name = str(request.validation_profile or request.metadata.get("validation_profile") or "")
            profile = validation_profile_spec(profile_name) if profile_name else None
            profile_policy = profile.public_payload() if profile is not None else None
            requires_vm = command_requires_vm_backed_sandbox(request) or bool(
                profile is not None and profile.requires_isolated_container_runtime
            )
            if requires_vm:
                blocked = self._vm_required_command_block(record, request, fingerprint=fingerprint, idempotency_key=key)
                if blocked is not None:
                    return blocked
            microvm = self._ready_microvm_backend(request.vm_session_id) if requires_vm else None
            limits = RunnerLimits(
                timeout_seconds=min(request.timeout_seconds, self.command_timeout_seconds),
                max_output_bytes=self.max_output_bytes,
                memory_limit=self.runner_memory_limit,
                pids_limit=self.runner_pids_limit,
                cpu_limit=self.runner_cpu_limit,
            )
            preflight_response = self._preflight_command(
                record,
                request,
                profile_name=profile_name,
                limits=limits,
                idempotency_key=key,
                fingerprint=fingerprint,
                runner=microvm,
            )
            if preflight_response is not None:
                return preflight_response
            if (
                profile_name == "docker-compose-runtime"
                and microvm is not None
                and self.compose_runtime_preflight().ready
            ):
                return self._run_command_compose_proxy(
                    record,
                    request,
                    profile_name=profile_name,
                    profile_policy=profile_policy,
                    limits=limits,
                    idempotency_key=key,
                    fingerprint=fingerprint,
                )
            if microvm is not None:
                return self._run_command_microvm(
                    record,
                    request,
                    microvm=microvm,
                    profile_name=profile_name,
                    profile_policy=profile_policy,
                    limits=limits,
                    idempotency_key=key,
                    fingerprint=fingerprint,
                )
            self._record_event(
                "workspace.command.started",
                session_id=session_id,
                payload={
                    "argv": request.argv,
                    "cwd": request.cwd,
                    "allow_profile": request.allow_profile,
                    "validation_profile": profile_name,
                    "validation_profile_policy": profile_policy,
                },
            )
            runner = self._runner()
            generated_project = command_requires_vm_backed_sandbox(request)
            command_env = _command_env_for_validation(request)
            scrubbed_env, removed_env = scrub_command_env(command_env, generated_project=generated_project)
            result = runner.run(
                argv=request.argv,
                cwd=Path(request.cwd),
                workspace_path=record.workspace_path,
                artifacts_path=record.artifacts_path,
                env=scrubbed_env,
                limits=limits,
                redaction_terms=self._redaction_terms(record),
                network_enabled=bool(profile and profile.allows_network),
            )
            stdout_ref, stderr_ref = self._write_logs(record, result.run_id, result.stdout, result.stderr)
            diff = self.diff(session_id, record_event=False)
            artifacts = artifact_descriptors(record.artifacts_path)
            response = CommandRunResponse(
                run_id=result.run_id,
                status=result.status,
                exit_code=result.exit_code,
                stdout_ref=stdout_ref,
                stderr_ref=stderr_ref,
                duration_ms=result.duration_ms,
                changed=diff.changed,
                diff_ref=f"diff://{session_id}" if diff.changed else None,
                artifacts=artifacts,
                error=ErrorDetail(code="command_failed", message=result.error) if result.error else None,
                metadata={
                    **result.metadata,
                    "validation_profile": profile_name or None,
                    "validation_profile_policy": profile_policy,
                    "env_scrubbed": True,
                    "removed_env_keys": removed_env,
                    "host_execution_used": self.runner_backend == "local_process",
                    "host_docker_socket_exposed": False,
                    "vm_backed": False,
                    "fallback_to_host_allowed": False,
                    "stdout_preview": result.stdout,
                    "stderr_preview": result.stderr,
                    "output_truncated": result.output_truncated,
                },
            )
            record.command_responses[result.run_id] = response
            current = file_manifest(record.workspace_path)
            record.response.state_hash = manifest_hash(current)
            if key:
                self._idempotency[key] = (fingerprint, response)
            self._record_event(
                "workspace.command.completed",
                session_id=session_id,
                payload={
                    "run_id": result.run_id,
                    "status": result.status,
                    "exit_code": result.exit_code,
                    "duration_ms": result.duration_ms,
                    "changed": diff.changed,
                    "artifacts": [artifact.artifact_id for artifact in artifacts],
                    "state_hash": record.response.state_hash,
                    "validation_profile": profile_name,
                    "validation_profile_policy": profile_policy,
                },
            )
            return response

    def _write_files_batch_microvm(
        self,
        record: _SessionRecord,
        request: WorkspaceFileBatchWriteRequest,
        *,
        microvm: QemuMicroVmBackend,
        fingerprint: str,
        idempotency_key: str,
    ) -> WorkspaceFileBatchWriteResponse:
        result = microvm.file_batch(
            workspace_path=record.workspace_path,
            artifacts_path=record.artifacts_path,
            root=request.root,
            files=[
                {
                    "path": str(file.path),
                    "content_b64": file.content_b64,
                    "sha256": file.sha256,
                }
                for file in request.files
            ],
            verify_hashes=request.verify_hashes,
            forbid_symlink_escape=request.forbid_symlink_escape,
            timeout_seconds=self.command_timeout_seconds,
            max_output_bytes=self.max_output_bytes,
            redaction_terms=self._redaction_terms(record),
        )
        if result.status != "completed":
            response = WorkspaceFileBatchWriteResponse(
                write_id=f"write:{uuid.uuid4().hex}",
                session_id=record.response.session_id,
                status="blocked",
                state_hash=record.response.state_hash,
                error=ErrorDetail(
                    code="microvm_file_batch_failed",
                    message=str(result.error or "VM-backed file batch write failed"),
                    details={
                        "vm_session_id": request.vm_session_id,
                        "microvm_status": result.status,
                        "microvm_error_code": result.metadata.get("error_code"),
                    },
                ),
                metadata={
                    **result.metadata,
                    "material_session_id": request.material_session_id,
                    "vm_session_id": request.vm_session_id,
                    "requires_vm_backed_sandbox": True,
                    "host_execution_used": False,
                    "vm_backed": True,
                },
            )
            self._idempotency[idempotency_key] = (fingerprint, response)
            return response
        written = [
            WorkspaceFileWriteResult(
                path=str(item["path"]),
                before_sha256=item.get("before_sha256"),
                sha256=str(item["sha256"]),
                size_bytes=int(item["size_bytes"]),
            )
            for item in result.operation_metadata.get("files", [])
        ]
        current = file_manifest(record.workspace_path)
        state_hash = manifest_hash(current)
        record.response.state_hash = state_hash
        response = WorkspaceFileBatchWriteResponse(
            write_id=f"write:{uuid.uuid4().hex}",
            session_id=record.response.session_id,
            status="completed",
            state_hash=state_hash,
            file_count=len(written),
            files=written,
            metadata={
                **result.metadata,
                "root": request.root,
                "mode": request.mode,
                "verify_hashes": request.verify_hashes,
                "host_execution_used": False,
                "vm_backed": True,
                "vm_session_id": request.vm_session_id,
                "material_session_id": request.material_session_id,
            },
        )
        self._idempotency[idempotency_key] = (fingerprint, response)
        self._record_event(
            "workspace.files.batch_written",
            session_id=record.response.session_id,
            payload={
                "write_id": response.write_id,
                "file_count": response.file_count,
                "state_hash": state_hash,
                "paths": [item.path for item in written],
                "root": request.root,
                "host_execution_used": False,
                "vm_backed": True,
                "vm_session_id": request.vm_session_id,
                "material_session_id": request.material_session_id,
            },
        )
        return response

    def _apply_patches_microvm(
        self,
        record: _SessionRecord,
        request: WorkspacePatchApplyRequest,
        *,
        microvm: QemuMicroVmBackend,
        fingerprint: str,
        idempotency_key: str,
    ) -> WorkspacePatchApplyResponse:
        result = microvm.patch_apply(
            workspace_path=record.workspace_path,
            artifacts_path=record.artifacts_path,
            patches=[
                {
                    "path": str(patch.path),
                    "expected_old_sha256": patch.expected_old_sha256,
                    "unified_diff": patch.unified_diff,
                }
                for patch in request.patches
            ],
            verify=request.verify,
            forbid_symlink_escape=request.forbid_symlink_escape,
            timeout_seconds=self.command_timeout_seconds,
            max_output_bytes=self.max_output_bytes,
            redaction_terms=self._redaction_terms(record),
        )
        if result.status != "completed":
            patch_error_details = _patch_error_details(
                result.operation_metadata,
                request=request,
                redaction_terms=self._redaction_terms(record),
            )
            response = WorkspacePatchApplyResponse(
                patch_set_id=f"patch:{uuid.uuid4().hex}",
                session_id=record.response.session_id,
                status="blocked",
                state_hash=record.response.state_hash,
                error=ErrorDetail(
                    code="microvm_patch_apply_failed",
                    message=str(result.error or "VM-backed patch apply failed"),
                    details={
                        "vm_session_id": request.vm_session_id,
                        "microvm_status": result.status,
                        "microvm_error": result.error,
                        "microvm_error_code": result.metadata.get("error_code"),
                        **patch_error_details,
                    },
                ),
                metadata={
                    **result.metadata,
                    "material_session_id": request.material_session_id,
                    "vm_session_id": request.vm_session_id,
                    "requires_vm_backed_sandbox": True,
                    "host_execution_used": False,
                    "vm_backed": True,
                },
            )
            self._idempotency[idempotency_key] = (fingerprint, response)
            return response
        applied = [
            WorkspacePatchApplyResult(
                path=str(item["path"]),
                before_sha256=item.get("before_sha256"),
                after_sha256=item.get("after_sha256"),
                applied=bool(item.get("applied", True)),
            )
            for item in result.operation_metadata.get("patches", [])
        ]
        current = file_manifest(record.workspace_path)
        state_hash = manifest_hash(current)
        record.response.state_hash = state_hash
        response = WorkspacePatchApplyResponse(
            patch_set_id=f"patch:{uuid.uuid4().hex}",
            session_id=record.response.session_id,
            status="completed",
            state_hash=state_hash,
            applied_count=len(applied),
            patches=applied,
            metadata={
                **result.metadata,
                "verify": request.verify,
                "host_execution_used": False,
                "vm_backed": True,
                "vm_session_id": request.vm_session_id,
                "material_session_id": request.material_session_id,
            },
        )
        self._idempotency[idempotency_key] = (fingerprint, response)
        self._record_event(
            "workspace.patch.applied",
            session_id=record.response.session_id,
            payload={
                "patch_set_id": response.patch_set_id,
                "applied_count": response.applied_count,
                "state_hash": state_hash,
                "paths": [item.path for item in applied],
                "host_execution_used": False,
                "vm_backed": True,
                "vm_session_id": request.vm_session_id,
                "material_session_id": request.material_session_id,
            },
        )
        return response

    def _run_command_compose_proxy(
        self,
        record: _SessionRecord,
        request: CommandRunRequest,
        *,
        profile_name: str,
        profile_policy: dict[str, Any] | None,
        limits: RunnerLimits,
        idempotency_key: str,
        fingerprint: str,
    ) -> CommandRunResponse:
        self._record_event(
            "workspace.command.started",
            session_id=record.response.session_id,
            payload={
                "argv": request.argv,
                "cwd": request.cwd,
                "allow_profile": request.allow_profile,
                "validation_profile": profile_name,
                "validation_profile_policy": profile_policy,
                "vm_backed": True,
                "vm_session_id": request.vm_session_id,
                "backend": "compose_runtime_proxy",
            },
        )
        command_env = _command_env_for_validation(request)
        scrubbed_env, removed_env = scrub_command_env(command_env, generated_project=True)
        result = self._compose_runtime_proxy().run_command(
            argv=request.argv,
            cwd=Path(request.cwd),
            workspace_path=record.workspace_path,
            artifacts_path=record.artifacts_path,
            env=scrubbed_env,
            limits=limits,
            redaction_terms=self._redaction_terms(record),
            material_session_id=request.material_session_id or str(request.metadata.get("material_session_id") or ""),
            vm_session_id=request.vm_session_id,
        )
        stdout_ref, stderr_ref = self._write_logs(record, result.run_id, result.stdout, result.stderr)
        diff = self.diff(record.response.session_id, record_event=False)
        artifacts = artifact_descriptors(record.artifacts_path)
        error_message = result.error or (
            f"exit_code:{result.exit_code}" if result.status not in {"completed"} and result.exit_code is not None else None
        )
        error_code = str(
            result.metadata.get("error_code")
            or _profile_failure_code(profile_name, default="command_failed")
        )
        response = CommandRunResponse(
            run_id=result.run_id,
            status=result.status,
            exit_code=result.exit_code,
            stdout_ref=stdout_ref,
            stderr_ref=stderr_ref,
            duration_ms=result.duration_ms,
            changed=diff.changed,
            diff_ref=f"diff://{record.response.session_id}" if diff.changed else None,
            artifacts=artifacts,
            error=ErrorDetail(
                code=error_code,
                message=error_message,
                details={
                    "validation_profile": profile_name or None,
                    "cleanup": result.metadata.get("cleanup"),
                    "services": result.metadata.get("services"),
                    "service_container_ids": result.metadata.get("service_container_ids"),
                    "health_checks": result.metadata.get("health_checks"),
                    "host_execution_used": False,
                    "host_docker_socket_exposed": False,
                    "fallback_to_host_allowed": False,
                },
            )
            if error_message
            else None,
            metadata={
                **result.metadata,
                "backend": "compose_runtime_proxy",
                "validation_profile": profile_name or None,
                "validation_profile_policy": profile_policy,
                "env_scrubbed": True,
                "removed_env_keys": removed_env,
                "host_execution_used": False,
                "host_docker_socket_exposed": False,
                "vm_backed": True,
                "fallback_to_host_allowed": False,
                "vm_session_id": request.vm_session_id,
                "stdout_preview": result.stdout,
                "stderr_preview": result.stderr,
                "output_truncated": result.output_truncated,
            },
        )
        record.command_responses[result.run_id] = response
        current = file_manifest(record.workspace_path)
        record.response.state_hash = manifest_hash(current)
        if idempotency_key:
            self._idempotency[idempotency_key] = (fingerprint, response)
        self._record_event(
            "workspace.command.completed",
            session_id=record.response.session_id,
            payload={
                "run_id": result.run_id,
                "status": result.status,
                "exit_code": result.exit_code,
                "duration_ms": result.duration_ms,
                "changed": diff.changed,
                "artifacts": [artifact.artifact_id for artifact in artifacts],
                "state_hash": record.response.state_hash,
                "validation_profile": profile_name,
                "validation_profile_policy": profile_policy,
                "host_execution_used": False,
                "host_docker_socket_exposed": False,
                "vm_backed": True,
                "fallback_to_host_allowed": False,
                "vm_session_id": request.vm_session_id,
                "backend": "compose_runtime_proxy",
                "services": result.metadata.get("services"),
                "service_container_ids": result.metadata.get("service_container_ids"),
                "health_checks": result.metadata.get("health_checks"),
                "cleanup": result.metadata.get("cleanup"),
                "logs_collected": result.metadata.get("logs_collected"),
            },
        )
        return response

    def _run_command_microvm(
        self,
        record: _SessionRecord,
        request: CommandRunRequest,
        *,
        microvm: QemuMicroVmBackend,
        profile_name: str,
        profile_policy: dict[str, Any] | None,
        limits: RunnerLimits,
        idempotency_key: str,
        fingerprint: str,
    ) -> CommandRunResponse:
        self._record_event(
            "workspace.command.started",
            session_id=record.response.session_id,
            payload={
                "argv": request.argv,
                "cwd": request.cwd,
                "allow_profile": request.allow_profile,
                "validation_profile": profile_name,
                "validation_profile_policy": profile_policy,
                "vm_backed": True,
                "vm_session_id": request.vm_session_id,
            },
        )
        result = microvm.run_command(
            argv=request.argv,
            cwd=Path(request.cwd),
            workspace_path=record.workspace_path,
            artifacts_path=record.artifacts_path,
            env=_command_env_for_validation(request),
            limits=limits,
            redaction_terms=self._redaction_terms(record),
        )
        stdout_ref, stderr_ref = self._write_logs(record, result.run_id, result.stdout, result.stderr)
        diff = self.diff(record.response.session_id, record_event=False)
        artifacts = artifact_descriptors(record.artifacts_path)
        error = _microvm_command_error(result.error, request) if result.error else None
        response = CommandRunResponse(
            run_id=result.run_id,
            status=result.status,
            exit_code=result.exit_code,
            stdout_ref=stdout_ref,
            stderr_ref=stderr_ref,
            duration_ms=result.duration_ms,
            changed=diff.changed,
            diff_ref=f"diff://{record.response.session_id}" if diff.changed else None,
            artifacts=artifacts,
            error=error,
            metadata={
                **result.metadata,
                "validation_profile": profile_name or None,
                "validation_profile_policy": profile_policy,
                "env_scrubbed": True,
                "host_execution_used": False,
                "host_docker_socket_exposed": False,
                "vm_backed": True,
                "fallback_to_host_allowed": False,
                "vm_session_id": request.vm_session_id,
                "stdout_preview": result.stdout,
                "stderr_preview": result.stderr,
                "output_truncated": result.output_truncated,
                "operation_metadata": result.operation_metadata,
            },
        )
        record.command_responses[result.run_id] = response
        current = file_manifest(record.workspace_path)
        record.response.state_hash = manifest_hash(current)
        if idempotency_key:
            self._idempotency[idempotency_key] = (fingerprint, response)
        self._record_event(
            "workspace.command.completed",
            session_id=record.response.session_id,
            payload={
                "run_id": result.run_id,
                "status": result.status,
                "exit_code": result.exit_code,
                "duration_ms": result.duration_ms,
                "changed": diff.changed,
                "artifacts": [artifact.artifact_id for artifact in artifacts],
                "state_hash": record.response.state_hash,
                "validation_profile": profile_name,
                "validation_profile_policy": profile_policy,
                "host_execution_used": False,
                "host_docker_socket_exposed": False,
                "vm_backed": True,
                "fallback_to_host_allowed": False,
                "vm_session_id": request.vm_session_id,
                "operation_metadata": result.operation_metadata,
            },
        )
        return response

    def _vm_required_command_block(
        self,
        record: _SessionRecord,
        request: CommandRunRequest,
        *,
        fingerprint: str,
        idempotency_key: str,
    ) -> CommandRunResponse | None:
        vm_session_id = request.vm_session_id or str(request.metadata.get("vm_session_id") or "")
        vm_record = self._vm_sessions.get(vm_session_id) if vm_session_id else None
        if vm_record is not None and vm_record.response.status == "ready" and vm_record.response.isolation_proof.vm_backed:
            return None
        reason = "vm_runtime_unavailable" if not vm_session_id else "vm_session_not_ready"
        response = CommandRunResponse(
            run_id=f"run:{uuid.uuid4().hex}",
            status="blocked",
            duration_ms=0,
            changed=False,
            error=ErrorDetail(
                code=reason,
                message="generated or untrusted material commands require a ready VM-backed sandbox",
                details={
                    "session_id": record.response.session_id,
                    "material_session_id": request.material_session_id or request.metadata.get("material_session_id"),
                    "vm_session_id": vm_session_id or None,
                    "required": True,
                    "host_execution_used": False,
                    "fallback_to_host_allowed": False,
                },
            ),
            metadata={
                "validation_profile": request.validation_profile,
                "material_session_id": request.material_session_id or request.metadata.get("material_session_id"),
                "vm_session_id": vm_session_id or None,
                "requires_vm_backed_sandbox": True,
                "preflight_status": "blocked",
                "host_execution_used": False,
                "vm_backed": False,
                "fallback_to_host_allowed": False,
            },
        )
        record.command_responses[response.run_id] = response
        if idempotency_key:
            self._idempotency[idempotency_key] = (fingerprint, response)
        self._record_event(
            "workspace.command.blocked",
            session_id=record.response.session_id,
            payload={
                "run_id": response.run_id,
                "reason": reason,
                "requires_vm_backed_sandbox": True,
                "vm_session_id": vm_session_id or None,
                "host_execution_used": False,
            },
        )
        return response

    def _vm_required_write_block(
        self,
        record: _SessionRecord,
        request: WorkspaceFileBatchWriteRequest,
        *,
        fingerprint: str,
        idempotency_key: str,
    ) -> WorkspaceFileBatchWriteResponse | None:
        if not request.requires_vm_backed_sandbox:
            return None
        if self._has_ready_vm_session(request.vm_session_id):
            return None
        reason = "vm_runtime_unavailable" if not request.vm_session_id else "vm_session_not_ready"
        response = WorkspaceFileBatchWriteResponse(
            write_id=f"write:{uuid.uuid4().hex}",
            session_id=record.response.session_id,
            status="blocked",
            state_hash=record.response.state_hash,
            error=ErrorDetail(
                code=reason,
                message="generated or untrusted material file writes require a ready VM-backed sandbox",
                details={
                    "session_id": record.response.session_id,
                    "material_session_id": request.material_session_id or request.metadata.get("material_session_id"),
                    "vm_session_id": request.vm_session_id,
                    "required": True,
                    "host_execution_used": False,
                    "fallback_to_host_allowed": False,
                },
            ),
            metadata={
                "material_session_id": request.material_session_id or request.metadata.get("material_session_id"),
                "vm_session_id": request.vm_session_id,
                "requires_vm_backed_sandbox": True,
                "preflight_status": "blocked",
                "host_execution_used": False,
                "vm_backed": False,
                "fallback_to_host_allowed": False,
            },
        )
        self._idempotency[idempotency_key] = (fingerprint, response)
        self._record_event(
            "workspace.files.batch_blocked",
            session_id=record.response.session_id,
            payload={
                "write_id": response.write_id,
                "reason": reason,
                "requires_vm_backed_sandbox": True,
                "vm_session_id": request.vm_session_id,
                "host_execution_used": False,
            },
        )
        return response

    def _vm_required_patch_block(
        self,
        record: _SessionRecord,
        request: WorkspacePatchApplyRequest,
        *,
        fingerprint: str,
        idempotency_key: str,
    ) -> WorkspacePatchApplyResponse | None:
        if not request.requires_vm_backed_sandbox:
            return None
        if self._has_ready_vm_session(request.vm_session_id):
            return None
        reason = "vm_runtime_unavailable" if not request.vm_session_id else "vm_session_not_ready"
        response = WorkspacePatchApplyResponse(
            patch_set_id=f"patch:{uuid.uuid4().hex}",
            session_id=record.response.session_id,
            status="blocked",
            state_hash=record.response.state_hash,
            error=ErrorDetail(
                code=reason,
                message="generated or untrusted material patches require a ready VM-backed sandbox",
                details={
                    "session_id": record.response.session_id,
                    "material_session_id": request.material_session_id or request.metadata.get("material_session_id"),
                    "vm_session_id": request.vm_session_id,
                    "required": True,
                    "host_execution_used": False,
                    "fallback_to_host_allowed": False,
                },
            ),
            metadata={
                "material_session_id": request.material_session_id or request.metadata.get("material_session_id"),
                "vm_session_id": request.vm_session_id,
                "requires_vm_backed_sandbox": True,
                "preflight_status": "blocked",
                "host_execution_used": False,
                "vm_backed": False,
                "fallback_to_host_allowed": False,
            },
        )
        self._idempotency[idempotency_key] = (fingerprint, response)
        self._record_event(
            "workspace.patch.blocked",
            session_id=record.response.session_id,
            payload={
                "patch_set_id": response.patch_set_id,
                "reason": reason,
                "requires_vm_backed_sandbox": True,
                "vm_session_id": request.vm_session_id,
                "host_execution_used": False,
            },
        )
        return response

    def _has_ready_vm_session(self, vm_session_id: str | None) -> bool:
        if not vm_session_id:
            return False
        vm_record = self._vm_sessions.get(vm_session_id)
        return bool(
            vm_record is not None
            and vm_record.response.status == "ready"
            and vm_record.response.isolation_proof.vm_backed
        )

    def _preflight_command(
        self,
        record: _SessionRecord,
        request: CommandRunRequest,
        *,
        profile_name: str,
        limits: RunnerLimits,
        idempotency_key: str,
        fingerprint: str,
        runner: Any | None = None,
    ) -> CommandRunResponse | None:
        if not profile_name:
            return None
        profile = validation_profile_spec(profile_name)
        if profile is None:
            raise WorkspaceExecutionError(
                "validation_profile_unknown",
                "workspace execution validation profile is not declared",
                status_code=HTTPStatus.BAD_REQUEST,
                details={"validation_profile": profile_name},
            )
        runtime_block = self._isolated_container_runtime_block(
            record,
            request,
            profile=profile,
            idempotency_key=idempotency_key,
            fingerprint=fingerprint,
            vm_backed=runner is not None,
        )
        if runtime_block is not None:
            return runtime_block
        if getattr(profile, "requires_isolated_container_runtime", False) and self.compose_runtime_preflight().ready:
            self._record_event(
                "workspace.command.preflight",
                session_id=record.response.session_id,
                payload={
                    "argv": request.argv,
                    "cwd": request.cwd,
                    "validation_profile": profile.name,
                    "validation_profile_policy": profile.public_payload(),
                    "required_tools": [],
                    "missing_tools": [],
                    "compose_runtime_status": "ready",
                    "backend": "compose_runtime_proxy",
                },
            )
            return None
        required_tools = command_required_tools(profile_name, request.argv)
        if not required_tools:
            return None
        active_runner = runner or self._runner()
        missing_tools, cache_status = self._missing_tools_with_cache(
            active_runner,
            required_tools,
            limits=limits,
        )
        self._record_event(
            "workspace.command.preflight",
            session_id=record.response.session_id,
            payload={
                "argv": request.argv,
                "cwd": request.cwd,
                "validation_profile": profile.name,
                "validation_profile_policy": profile.public_payload(),
                "required_tools": list(required_tools),
                "missing_tools": list(missing_tools),
                **cache_status,
            },
        )
        if not missing_tools:
            return None
        response = CommandRunResponse(
            run_id=f"run:{uuid.uuid4().hex}",
            status="blocked",
            exit_code=None,
            duration_ms=0,
            changed=False,
            error=ErrorDetail(
                code="validation_tool_unavailable",
                message="validation profile requires tools that are not available in the runner",
                details={
                    "validation_profile": profile.name,
                    "validation_profile_policy": profile.public_payload(),
                    "required_tools": list(required_tools),
                    "missing_tools": list(missing_tools),
                },
            ),
            metadata={
                "backend": "microvm" if runner is not None else self.runner_backend,
                "validation_profile": profile.name,
                "validation_profile_policy": profile.public_payload(),
                "required_tools": list(required_tools),
                "missing_tools": list(missing_tools),
                "preflight_status": "failed",
            },
        )
        record.command_responses[response.run_id] = response
        if idempotency_key:
            self._idempotency[idempotency_key] = (fingerprint, response)
        self._record_event(
            "workspace.command.blocked",
            session_id=record.response.session_id,
            payload={
                "run_id": response.run_id,
                "reason": "validation_tool_unavailable",
                "validation_profile": profile.name,
                "validation_profile_policy": profile.public_payload(),
                "missing_tools": list(missing_tools),
            },
        )
        return response

    def _isolated_container_runtime_block(
        self,
        record: _SessionRecord,
        request: CommandRunRequest,
        *,
        profile: Any,
        idempotency_key: str,
        fingerprint: str,
        vm_backed: bool,
    ) -> CommandRunResponse | None:
        if not getattr(profile, "requires_isolated_container_runtime", False):
            return None
        status, reason, message, evidence = self._isolated_container_runtime_preflight()
        if status == "ready":
            return None
        profile_policy = profile.public_payload()
        response = CommandRunResponse(
            run_id=f"run:{uuid.uuid4().hex}",
            status="blocked",
            exit_code=None,
            duration_ms=0,
            changed=False,
            error=ErrorDetail(
                code=reason,
                message=message,
                details={
                    "validation_profile": profile.name,
                    "validation_profile_policy": profile_policy,
                    "container_runtime_status": status,
                    "required": True,
                    "vm_session_id": request.vm_session_id,
                    "host_execution_used": False,
                    "fallback_to_host_allowed": False,
                    "runtime_evidence": evidence,
                },
            ),
            metadata={
                "backend": "microvm" if vm_backed else self.runner_backend,
                "validation_profile": profile.name,
                "validation_profile_policy": profile_policy,
                "container_runtime_status": status,
                "preflight_status": "blocked",
                "host_execution_used": False,
                "vm_backed": vm_backed,
                "fallback_to_host_allowed": False,
                "runtime_evidence": evidence,
            },
        )
        record.command_responses[response.run_id] = response
        if idempotency_key:
            self._idempotency[idempotency_key] = (fingerprint, response)
        self._record_event(
            "workspace.command.blocked",
            session_id=record.response.session_id,
            payload={
                "run_id": response.run_id,
                "reason": reason,
                "validation_profile": profile.name,
                "validation_profile_policy": profile_policy,
                "container_runtime_status": status,
                "vm_session_id": request.vm_session_id,
                "host_execution_used": False,
                "fallback_to_host_allowed": False,
            },
        )
        return response

    def _missing_tools_with_cache(
        self,
        runner: Any,
        required_tools: tuple[str, ...],
        *,
        limits: RunnerLimits,
    ) -> tuple[tuple[str, ...], dict[str, Any]]:
        if not isinstance(runner, QemuMicroVmBackend):
            return runner.missing_tools(required_tools, limits=limits), {
                "tool_preflight_cache": "not_applicable",
                "backend": self.runner_backend,
            }
        cache_key = runner.tool_cache_key(required_tools)
        cached = self._tool_preflight_cache.get(cache_key)
        if cached is not None:
            return cached, {
                "tool_preflight_cache": "hit",
                "tool_preflight_cache_key": cache_key,
                "backend": "microvm",
            }
        started = time.time()
        missing_tools = runner.missing_tools(required_tools, limits=limits)
        duration_ms = int((time.time() - started) * 1000)
        self._tool_preflight_cache[cache_key] = missing_tools
        return missing_tools, {
            "tool_preflight_cache": "miss",
            "tool_preflight_cache_key": cache_key,
            "tool_preflight_duration_ms": duration_ms,
            "backend": "microvm",
        }

    def _isolated_container_runtime_preflight(self) -> tuple[str, str, str, dict[str, Any]]:
        proxy = self.compose_runtime_preflight()
        if proxy.ready:
            return (
                "ready",
                "",
                "VM-backed compose runtime proxy is ready",
                proxy.evidence,
            )
        if proxy.status != "not_configured":
            return (
                proxy.status,
                proxy.failure_code or "docker_runtime_unavailable",
                proxy.failure_reason or "VM-backed compose runtime proxy is not ready",
                proxy.evidence,
            )
        if self.vm_backend == "microvm":
            return (
                "unavailable",
                "docker_runtime_unavailable",
                "the active microVM backend does not provide a VM-local container runtime",
                proxy.evidence,
            )
        if self.vm_backend in {"external", "vm-proxy"} and self.vm_control_url:
            return (
                "unimplemented",
                "docker_runtime_unavailable",
                "configured VM-backed container runtime has no active workspace_execution client implementation",
                proxy.evidence,
            )
        return (
            "unavailable",
            "docker_runtime_unavailable",
            "no VM-backed isolated container runtime is configured for workspace_execution",
            proxy.evidence,
        )

    def diff(self, session_id: str, *, record_event: bool = True) -> DiffResponse:
        with self._lock:
            record = self._require_session(session_id)
            current = file_manifest(record.workspace_path)
            files = diff_files(baseline=record.baseline_manifest, current=current, workspace_path=record.workspace_path)
            response = DiffResponse(
                session_id=session_id,
                baseline_hash=manifest_hash(record.baseline_manifest),
                state_hash=manifest_hash(current),
                changed=bool(files),
                files=files,
            )
            if record_event:
                self._record_event(
                    "workspace.diff.generated",
                    session_id=session_id,
                    payload={"changed": response.changed, "file_count": len(files), "state_hash": response.state_hash},
                )
            return response

    def artifacts(self, session_id: str) -> ArtifactListResponse:
        with self._lock:
            record = self._require_session(session_id)
            artifacts = artifact_descriptors(record.artifacts_path)
            for artifact in artifacts:
                self._record_event(
                    "workspace.artifact.discovered",
                    session_id=session_id,
                    payload={"artifact_id": artifact.artifact_id, "path": artifact.path, "sha256": artifact.sha256},
                )
            return ArtifactListResponse(session_id=session_id, artifacts=artifacts)

    def package_artifact(
        self,
        session_id: str,
        request: ArtifactPackageRequest,
    ) -> ArtifactPackageResponse:
        with self._lock:
            record = self._require_open_session(session_id)
            fingerprint = _stable_hash(
                {
                    "session_id": session_id,
                    "root": request.root,
                    "vm_session_id": request.vm_session_id,
                    "material_session_id": request.material_session_id,
                    "requires_vm_backed_sandbox": request.requires_vm_backed_sandbox,
                    "forbid_symlink_escape": request.forbid_symlink_escape,
                }
            )
            key = f"artifact_package:{session_id}:{request.idempotency_key}"
            existing = self._idempotency.get(key)
            if existing is not None:
                previous_fingerprint, response = existing
                if previous_fingerprint != fingerprint:
                    raise WorkspaceExecutionError(
                        "idempotency_conflict",
                        "artifact package idempotency key was reused with different state",
                        status_code=HTTPStatus.CONFLICT,
                        details={"idempotency_key": request.idempotency_key},
                    )
                return response
            if request.requires_vm_backed_sandbox and not self._has_ready_vm_session(request.vm_session_id):
                reason = "vm_runtime_unavailable" if not request.vm_session_id else "vm_session_not_ready"
                response = ArtifactPackageResponse(
                    package_id=f"package:{uuid.uuid4().hex}",
                    session_id=session_id,
                    status="blocked",
                    state_hash=record.response.state_hash,
                    error=ErrorDetail(
                        code=reason,
                        message="generated or untrusted material packaging requires a ready VM-backed sandbox",
                        details={
                            "session_id": session_id,
                            "material_session_id": request.material_session_id,
                            "vm_session_id": request.vm_session_id,
                            "required": True,
                            "host_execution_used": False,
                            "fallback_to_host_allowed": False,
                        },
                    ),
                    metadata={
                        "material_session_id": request.material_session_id,
                        "vm_session_id": request.vm_session_id,
                        "requires_vm_backed_sandbox": True,
                        "preflight_status": "blocked",
                        "host_execution_used": False,
                        "vm_backed": False,
                        "fallback_to_host_allowed": False,
                    },
                )
                self._idempotency[key] = (fingerprint, response)
                self._record_event(
                    "workspace.artifact.package_blocked",
                    session_id=session_id,
                    payload={
                        "package_id": response.package_id,
                        "reason": reason,
                        "vm_session_id": request.vm_session_id,
                        "host_execution_used": False,
                    },
                )
                return response
            microvm = self._ready_microvm_backend(request.vm_session_id)
            if request.requires_vm_backed_sandbox and microvm is not None:
                result = microvm.package_artifact(
                    workspace_path=record.workspace_path,
                    artifacts_path=record.artifacts_path,
                    root=request.root,
                    forbid_symlink_escape=request.forbid_symlink_escape,
                    timeout_seconds=self.command_timeout_seconds,
                    max_output_bytes=self.max_output_bytes,
                    redaction_terms=self._redaction_terms(record),
                )
                if result.status != "completed":
                    response = ArtifactPackageResponse(
                        package_id=f"package:{uuid.uuid4().hex}",
                        session_id=session_id,
                        status="blocked",
                        state_hash=record.response.state_hash,
                        error=ErrorDetail(
                            code="microvm_artifact_package_failed",
                            message=str(result.error or "VM-backed artifact packaging failed"),
                            details={
                                "vm_session_id": request.vm_session_id,
                                "microvm_status": result.status,
                                "microvm_error_code": result.metadata.get("error_code"),
                            },
                        ),
                        metadata={
                            **result.metadata,
                            "material_session_id": request.material_session_id,
                            "vm_session_id": request.vm_session_id,
                            "requires_vm_backed_sandbox": True,
                            "preflight_status": "blocked",
                            "host_execution_used": False,
                            "vm_backed": True,
                        },
                    )
                    self._idempotency[key] = (fingerprint, response)
                    return response
                artifact_meta = dict(result.operation_metadata.get("artifact") or {})
                artifact_name = str(artifact_meta.get("artifact_path") or "")
                artifact = next(
                    item for item in artifact_descriptors(record.artifacts_path) if item.path == artifact_name
                )
                current = file_manifest(record.workspace_path)
                state_hash = manifest_hash(current)
                record.response.state_hash = state_hash
                response = ArtifactPackageResponse(
                    package_id=f"package:{uuid.uuid4().hex}",
                    session_id=session_id,
                    status="completed",
                    state_hash=state_hash,
                    artifact=artifact,
                    metadata={
                        **result.metadata,
                        "root": request.root,
                        "material_session_id": request.material_session_id,
                        "vm_session_id": request.vm_session_id,
                        "host_execution_used": False,
                        "vm_backed": True,
                    },
                )
                self._idempotency[key] = (fingerprint, response)
                self._record_event(
                    "workspace.artifact.package_created",
                    session_id=session_id,
                    payload={
                        "package_id": response.package_id,
                        "artifact_id": artifact.artifact_id,
                        "path": artifact.path,
                        "sha256": artifact.sha256,
                        "size_bytes": artifact.size_bytes,
                        "host_execution_used": False,
                        "vm_backed": True,
                        "vm_session_id": request.vm_session_id,
                    },
                )
                return response

            root = safe_child(record.workspace_path, request.root)
            if request.forbid_symlink_escape and root.is_symlink():
                raise WorkspaceExecutionError(
                    "symlink_escape_attempt",
                    "artifact package root cannot be a symlink",
                    status_code=HTTPStatus.BAD_REQUEST,
                    details={"root": request.root},
                )
            if not root.exists() or not root.is_dir():
                raise WorkspaceExecutionError(
                    "artifact_root_missing",
                    "artifact package root does not exist inside the workspace",
                    status_code=HTTPStatus.NOT_FOUND,
                    details={"root": request.root},
                )
            artifact_name = f"{root.name}.tar.gz"
            artifact_path = record.artifacts_path / artifact_name
            if request.forbid_symlink_escape:
                for path in root.rglob("*"):
                    if path.is_symlink():
                        raise WorkspaceExecutionError(
                            "symlink_escape_attempt",
                            "artifact package cannot include symlinks when escape protection is enabled",
                            status_code=HTTPStatus.BAD_REQUEST,
                            details={"root": request.root, "path": str(path.relative_to(root))},
                        )
            with tarfile.open(artifact_path, "w:gz") as archive:
                archive.add(root, arcname=root.name, recursive=False)
                for path in sorted(root.rglob("*")):
                    relative = path.relative_to(root)
                    if is_excluded_relative_path(relative):
                        continue
                    arcname = str(Path(root.name) / relative)
                    archive.add(path, arcname=arcname, recursive=False)
            artifact = next(
                item for item in artifact_descriptors(record.artifacts_path) if item.path == artifact_name
            )
            current = file_manifest(record.workspace_path)
            state_hash = manifest_hash(current)
            record.response.state_hash = state_hash
            response = ArtifactPackageResponse(
                package_id=f"package:{uuid.uuid4().hex}",
                session_id=session_id,
                status="completed",
                state_hash=state_hash,
                artifact=artifact,
                metadata={
                    "root": request.root,
                    "material_session_id": request.material_session_id,
                    "vm_session_id": request.vm_session_id,
                    "host_execution_used": False,
                },
            )
            self._idempotency[key] = (fingerprint, response)
            self._record_event(
                "workspace.artifact.package_created",
                session_id=session_id,
                payload={
                    "package_id": response.package_id,
                    "artifact_id": artifact.artifact_id,
                    "path": artifact.path,
                    "sha256": artifact.sha256,
                    "size_bytes": artifact.size_bytes,
                    "host_execution_used": False,
                },
            )
            return response

    def publish_artifacts(
        self,
        session_id: str,
        request: ArtifactPublishRequest,
        publisher: ArtifactPublisher,
    ) -> ArtifactPublishResponse:
        with self._lock:
            record = self._require_open_session(session_id)
            if not request.idempotency_key:
                raise WorkspaceExecutionError(
                    "publish_idempotency_key_required",
                    "artifact publication requires an idempotency key",
                    status_code=HTTPStatus.PRECONDITION_REQUIRED,
                    details={"session_id": session_id},
                )
            artifacts_by_id = {artifact.artifact_id: artifact for artifact in artifact_descriptors(record.artifacts_path)}
            missing = [artifact_id for artifact_id in request.artifact_ids if artifact_id not in artifacts_by_id]
            if missing:
                raise WorkspaceExecutionError(
                    "artifact_not_found",
                    "requested artifacts are not available in the session",
                    status_code=HTTPStatus.NOT_FOUND,
                    details={"session_id": session_id, "artifact_ids": missing},
                )

            fingerprint = _stable_hash(
                {
                    "session_id": session_id,
                    "artifact_ids": request.artifact_ids,
                    "artifact_sha256": {artifact_id: artifacts_by_id[artifact_id].sha256 for artifact_id in request.artifact_ids},
                    "target": request.target,
                    "metadata": request.metadata,
                }
            )
            key = f"publish:{session_id}:{request.idempotency_key}"
            existing = self._idempotency.get(key)
            if existing is not None:
                previous_fingerprint, response = existing
                if previous_fingerprint != fingerprint:
                    raise WorkspaceExecutionError(
                        "idempotency_conflict",
                        "artifact publish idempotency key was reused with different state",
                        status_code=HTTPStatus.CONFLICT,
                        details={"idempotency_key": request.idempotency_key},
                    )
                return response

            published: list[PublishedArtifact] = []
            for index, artifact_id in enumerate(request.artifact_ids):
                artifact = artifacts_by_id[artifact_id]
                artifact_path = safe_child(record.artifacts_path, artifact.path)
                publish_key = f"{request.idempotency_key}:{index}:{artifact.sha256[:16]}"
                try:
                    result = publisher.publish_artifact(
                        artifact_path,
                        artifact,
                        target=request.target,
                        metadata={
                            **request.metadata,
                            "session_id": session_id,
                            "workspace_state_hash": record.response.state_hash,
                        },
                        idempotency_key=publish_key,
                    )
                    item = PublishedArtifact(
                        artifact_id=artifact_id,
                        status="published",
                        storage_object_ref=result.storage_object_ref,
                        chain_of_custody_ref=result.chain_of_custody_ref,
                        materialized_path=result.materialized_path,
                        materialized_sha256=result.materialized_sha256,
                        extracted_path=result.extracted_path,
                        extracted_files_count=result.extracted_files_count,
                        extracted_top_level_paths=result.extracted_top_level_paths,
                    )
                    self._record_event(
                        "workspace.artifact.published",
                        session_id=session_id,
                        payload={
                            "artifact_id": artifact_id,
                            "storage_object_ref": result.storage_object_ref,
                            "chain_of_custody_ref": result.chain_of_custody_ref,
                            "materialized_path": result.materialized_path,
                            "materialized_sha256": result.materialized_sha256,
                            "extracted_path": result.extracted_path,
                            "extracted_files_count": result.extracted_files_count,
                            "extracted_top_level_paths": result.extracted_top_level_paths,
                        },
                    )
                except StorageGuardianPublishError as exc:
                    item = PublishedArtifact(
                        artifact_id=artifact_id,
                        status="failed",
                        error=ErrorDetail(code=exc.code, message=str(exc), details=exc.details),
                    )
                    self._record_event(
                        "workspace.artifact.publish_failed",
                        session_id=session_id,
                        payload={"artifact_id": artifact_id, "error_code": exc.code, "details": exc.details},
                    )
                published.append(item)
            response = ArtifactPublishResponse(session_id=session_id, published=published)
            self._idempotency[key] = (fingerprint, response)
            return response

    def close_session(self, session_id: str, request: SessionCloseRequest) -> SessionCloseResponse:
        with self._lock:
            record = self._require_session(session_id)
            if record.closed:
                return SessionCloseResponse(session_id=session_id, status="already_closed", cleanup=request.cleanup)
            record.closed = True
            record.response.status = "closed"
            self._record_event(
                "workspace.session.closed",
                session_id=session_id,
                payload={"reason": request.reason, "cleanup": request.cleanup},
            )
            status = "closed"
            if request.cleanup:
                status = "cleanup_scheduled"
                self._cleanup_session(record)
            return SessionCloseResponse(session_id=session_id, status=status, cleanup=request.cleanup)

    def cleanup_expired(self) -> list[str]:
        expired: list[str] = []
        now = _now()
        for session_id, record in list(self._sessions.items()):
            if record.closed or record.response.expires_at > now:
                continue
            record.closed = True
            record.response.status = "expired"
            self._record_event("workspace.session.closed", session_id=session_id, payload={"reason": "ttl_expired"})
            self._cleanup_session(record)
            expired.append(session_id)
        return expired

    def list_events(self, session_id: str | None = None) -> list[LifecycleEvent]:
        with self._lock:
            if session_id is None:
                return list(self._events)
            return [event for event in self._events if event.session_id == session_id]

    def get_session(self, session_id: str) -> SessionResponse:
        with self._lock:
            return self._require_session(session_id).response

    def _require_open_session(self, session_id: str) -> _SessionRecord:
        record = self._require_session(session_id)
        if record.closed or record.response.status in {"closed", "expired", "failed"}:
            raise WorkspaceExecutionError(
                "session_closed",
                "workspace execution session is not open",
                status_code=HTTPStatus.CONFLICT,
                details={"session_id": session_id},
            )
        return record

    def _require_session(self, session_id: str) -> _SessionRecord:
        record = self._sessions.get(session_id)
        if record is None:
            raise WorkspaceExecutionError(
                "session_not_found",
                "workspace execution session does not exist",
                status_code=HTTPStatus.NOT_FOUND,
                details={"session_id": session_id},
            )
        return record

    def _require_vm_session(self, vm_session_id: str) -> _VmSessionRecord:
        record = self._vm_sessions.get(vm_session_id)
        if record is None:
            raise WorkspaceExecutionError(
                "vm_session_not_found",
                "workspace execution VM session does not exist",
                status_code=HTTPStatus.NOT_FOUND,
                details={"vm_session_id": vm_session_id},
            )
        return record

    def _cleanup_session(self, record: _SessionRecord) -> None:
        try:
            shutil.rmtree(record.scratch_path, ignore_errors=False)
        except FileNotFoundError:
            pass
        except OSError as exc:
            self._record_event(
                "workspace.cleanup.failed",
                session_id=record.response.session_id,
                payload={"error": str(exc)},
            )
            raise WorkspaceExecutionError(
                "session_cleanup_failed",
                "session scratch cleanup failed",
                status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
                details={"session_id": record.response.session_id, "error": str(exc)},
            ) from exc
        self._record_event(
            "workspace.cleanup.completed",
            session_id=record.response.session_id,
            payload={"scratch_ref": record.response.scratch_ref},
        )

    def _record_event(self, event_type: str, *, session_id: str, payload: dict[str, Any]) -> None:
        self._events.append(
            LifecycleEvent(
                event_id=f"event:{uuid.uuid4().hex}",
                event_type=event_type,
                session_id=session_id,
                created_at=_now(),
                payload=payload,
            )
        )

    def _runner(self) -> LocalProcessRunner | DockerEphemeralRunner:
        if self.runner_backend == "local_process":
            return LocalProcessRunner()
        if self.runner_backend == "docker_ephemeral":
            return DockerEphemeralRunner(
                image=self.runner_image,
                sandbox_runtime=self.sandbox_runtime,
                require_runtime=self.require_runtime,
            )
        raise WorkspaceExecutionError(
            "runner_backend_unsupported",
            "workspace execution runner backend is not supported",
            details={"runner_backend": self.runner_backend},
        )

    def _write_logs(self, record: _SessionRecord, run_id: str, stdout: str, stderr: str) -> tuple[str, str]:
        safe_run_id = run_id.replace(":", "_")
        run_dir = record.logs_path / safe_run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "stdout.log").write_text(stdout, encoding="utf-8")
        (run_dir / "stderr.log").write_text(stderr, encoding="utf-8")
        return (f"log://{record.response.session_id}/{run_id}/stdout", f"log://{record.response.session_id}/{run_id}/stderr")

    def _redaction_terms(self, record: _SessionRecord) -> list[str]:
        terms = [str(record.scratch_path), str(record.workspace_path), str(record.artifacts_path)]
        terms.extend(str(path) for path in self.source_roots.values())
        return terms

    @staticmethod
    def _session_fingerprint(request: SessionCreateRequest) -> str:
        return _stable_hash(
            {
                "source": request.source.model_dump(mode="json"),
                "execution_profile": request.execution_profile,
                "network": request.network,
                "ttl_seconds": request.ttl_seconds,
            }
        )
