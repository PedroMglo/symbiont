"""FastAPI application for the workspace_execution feature."""

from __future__ import annotations

from fastapi import Depends, FastAPI
from sharedai.servicekit.auth import service_token_dependency

from workspace_execution import __version__
from workspace_execution.compose_proxy import ComposeRuntimeProxyServer
from workspace_execution.config import get_settings
from workspace_execution.errors import WorkspaceExecutionError, workspace_execution_error_handler
from workspace_execution.lifecycle import SessionStore
from workspace_execution.storage_client import StorageGuardianClient
from workspace_execution.types import (
    ArtifactListResponse,
    ArtifactPackageRequest,
    ArtifactPackageResponse,
    ArtifactPublishRequest,
    ArtifactPublishResponse,
    CapabilitiesResponse,
    CommandRunRequest,
    CommandRunResponse,
    DiffResponse,
    GitRemoteSourceAcquireRequest,
    GitRemoteSourceAcquireResponse,
    WorkspaceFileBatchWriteRequest,
    WorkspaceFileBatchWriteResponse,
    WorkspacePatchApplyRequest,
    WorkspacePatchApplyResponse,
    HealthResponse,
    InputAttachRequest,
    InputAttachResponse,
    LifecycleEvent,
    SessionCloseRequest,
    SessionCloseResponse,
    SessionCreateRequest,
    SessionResponse,
    VmLifecycleEvent,
    VmSessionCloseRequest,
    VmSessionCloseResponse,
    VmSessionCreateRequest,
    VmSessionResponse,
)

app = FastAPI(title="Workspace Execution Feature", version=__version__)
app.add_exception_handler(WorkspaceExecutionError, workspace_execution_error_handler)
require_service_token = service_token_dependency(
    "Workspace Execution",
    lambda: get_settings().security.api_key,
)


def get_store() -> SessionStore:
    settings = get_settings()
    if not hasattr(app.state, "session_store"):
        app.state.session_store = SessionStore(
            scratch_root=settings.scratch_root,
            source_roots=settings.source_roots,
            default_ttl_seconds=settings.session_ttl_seconds,
            command_timeout_seconds=settings.command_timeout_seconds,
            max_output_bytes=settings.max_output_bytes,
            runner_backend=settings.runner_backend,
            runner_image=settings.runner_image,
            runner_cpu_limit=settings.runner_cpu_limit,
            runner_memory_limit=settings.runner_memory_limit,
            runner_pids_limit=settings.runner_pids_limit,
            sandbox_runtime=settings.sandbox_runtime,
            require_runtime=settings.require_runtime,
            vm_backend=settings.vm_backend,
            vm_control_url=settings.vm_control_url,
            vm_image_ref=settings.vm_image_ref,
            vm_profile=settings.vm_profile,
            vm_qemu_binary=settings.vm_qemu_binary,
            vm_kernel_path=settings.vm_kernel_path,
            vm_kvm_device=settings.vm_kvm_device,
            vm_require_kvm=settings.vm_require_kvm,
            vm_cache_root=settings.vm_cache_root,
            vm_boot_timeout_seconds=settings.vm_boot_timeout_seconds,
            vm_prewarm_rootfs=settings.vm_prewarm_rootfs,
            vm_ttl_seconds=settings.vm_ttl_seconds,
            vm_cpu_limit=settings.vm_cpu_limit,
            vm_memory_limit=settings.vm_memory_limit,
            vm_disk_limit=settings.vm_disk_limit,
            compose_runtime_url=settings.compose_runtime_url,
            compose_runtime_token=settings.compose_runtime_token,
            compose_runtime_timeout_seconds=settings.compose_runtime_timeout_seconds,
            compose_runtime_backend=settings.compose_runtime_backend,
            compose_runtime_dind_image=settings.compose_runtime_dind_image,
            compose_runtime_runner_image=settings.compose_runtime_runner_image,
            host_read_host_root=settings.host_read_host_root,
            host_read_container_root=settings.host_read_container_root,
        )
    return app.state.session_store


def get_storage_publisher() -> StorageGuardianClient:
    return StorageGuardianClient(get_settings().storage_guardian)


def get_compose_runtime_proxy() -> ComposeRuntimeProxyServer:
    settings = get_settings()
    return ComposeRuntimeProxyServer(
        backend=settings.compose_runtime_backend,
        dind_image=settings.compose_runtime_dind_image,
        runner_image=settings.compose_runtime_runner_image,
        timeout_seconds=settings.compose_runtime_timeout_seconds,
    )


@app.get("/health")
def health() -> HealthResponse:
    return HealthResponse(version=__version__)


@app.get("/v1/workspace-execution/capabilities")
def capabilities() -> CapabilitiesResponse:
    settings = get_settings()
    store = SessionStore(
        scratch_root=settings.scratch_root,
        source_roots=settings.source_roots,
        host_read_host_root=settings.host_read_host_root,
        host_read_container_root=settings.host_read_container_root,
        vm_backend=settings.vm_backend,
        vm_control_url=settings.vm_control_url,
        vm_image_ref=settings.vm_image_ref,
        vm_qemu_binary=settings.vm_qemu_binary,
        vm_kernel_path=settings.vm_kernel_path,
        vm_kvm_device=settings.vm_kvm_device,
        vm_require_kvm=settings.vm_require_kvm,
        vm_cache_root=settings.vm_cache_root,
        vm_boot_timeout_seconds=settings.vm_boot_timeout_seconds,
        vm_prewarm_rootfs=settings.vm_prewarm_rootfs,
        vm_memory_limit=settings.vm_memory_limit,
        vm_cpu_limit=settings.vm_cpu_limit,
        compose_runtime_url=settings.compose_runtime_url,
        compose_runtime_token=settings.compose_runtime_token,
        compose_runtime_timeout_seconds=settings.compose_runtime_timeout_seconds,
        compose_runtime_backend=settings.compose_runtime_backend,
        compose_runtime_dind_image=settings.compose_runtime_dind_image,
        compose_runtime_runner_image=settings.compose_runtime_runner_image,
    )
    vm_runtime_status, _, _ = store.vm_backend_preflight()
    compose_preflight = store.compose_runtime_preflight()
    response = CapabilitiesResponse()
    policy = dict(response.policy)
    policy["vm_backend"] = settings.vm_backend
    policy["vm_runtime_status"] = vm_runtime_status
    policy["vm_rootfs_prewarm_enabled"] = settings.vm_prewarm_rootfs
    policy["vm_backed_sessions"] = vm_runtime_status == "ready"
    policy["vm_backend_configured"] = (
        vm_runtime_status == "ready"
        or (settings.vm_backend in {"external", "vm-proxy"} and bool(settings.vm_control_url))
    )
    policy["vm_control_endpoint_configured"] = bool(settings.vm_control_url)
    policy["compose_runtime_proxy_configured"] = bool(settings.compose_runtime_url)
    policy["compose_runtime_status"] = compose_preflight.status
    policy["compose_runtime_vm_backed"] = compose_preflight.ready
    policy["compose_runtime_failure_code"] = compose_preflight.failure_code
    policy["compose_runtime_failure_reason"] = compose_preflight.failure_reason
    policy["compose_runtime_backend"] = settings.compose_runtime_backend
    policy["compose_runtime_host_execution_used"] = False
    policy["compose_runtime_host_docker_socket_exposed"] = False
    policy["compose_runtime_fallback_to_host_allowed"] = False
    policy["host_execution_used"] = False
    policy["host_docker_socket_exposed"] = False
    policy["fallback_to_host_allowed"] = False
    if compose_preflight.evidence:
        policy["compose_runtime_evidence"] = {
            "vm_backed": bool(compose_preflight.evidence.get("vm_backed")),
            "compose_runtime": bool(compose_preflight.evidence.get("compose_runtime")),
            "host_execution_used": bool(compose_preflight.evidence.get("host_execution_used")),
            "host_docker_socket_exposed": bool(compose_preflight.evidence.get("host_docker_socket_exposed")),
            "fallback_to_host_allowed": bool(compose_preflight.evidence.get("fallback_to_host_allowed")),
            "isolation_mode": compose_preflight.evidence.get("isolation_mode"),
            "backend": compose_preflight.evidence.get("backend"),
        }
    return response.model_copy(update={"policy": policy})


@app.get("/v1/compose-runtime/capabilities", dependencies=[Depends(require_service_token)])
def compose_runtime_capabilities(
    proxy: ComposeRuntimeProxyServer = Depends(get_compose_runtime_proxy),
) -> dict[str, object]:
    return proxy.capabilities()


@app.post("/v1/compose-runtime/run", dependencies=[Depends(require_service_token)])
def compose_runtime_run(
    payload: dict[str, object],
    proxy: ComposeRuntimeProxyServer = Depends(get_compose_runtime_proxy),
) -> dict[str, object]:
    return proxy.run(payload)


@app.post("/v1/workspace-execution/sessions", dependencies=[Depends(require_service_token)])
def create_session(request: SessionCreateRequest, store: SessionStore = Depends(get_store)) -> SessionResponse:
    return store.create_session(request)


@app.get("/v1/workspace-execution/sessions/{session_id}/events", dependencies=[Depends(require_service_token)])
def get_session_events(session_id: str, store: SessionStore = Depends(get_store)) -> list[LifecycleEvent]:
    return store.list_events(session_id)


@app.post("/v1/workspace-execution/vm-sessions", dependencies=[Depends(require_service_token)])
def create_vm_session(
    request: VmSessionCreateRequest,
    store: SessionStore = Depends(get_store),
) -> VmSessionResponse:
    return store.create_vm_session(request)


@app.get("/v1/workspace-execution/vm-sessions/{vm_session_id}", dependencies=[Depends(require_service_token)])
def get_vm_session(vm_session_id: str, store: SessionStore = Depends(get_store)) -> VmSessionResponse:
    return store.get_vm_session(vm_session_id)


@app.get("/v1/workspace-execution/vm-sessions/{vm_session_id}/events", dependencies=[Depends(require_service_token)])
def get_vm_session_events(vm_session_id: str, store: SessionStore = Depends(get_store)) -> list[VmLifecycleEvent]:
    return store.list_vm_events(vm_session_id)


@app.post("/v1/workspace-execution/vm-sessions/{vm_session_id}/close", dependencies=[Depends(require_service_token)])
def close_vm_session(
    vm_session_id: str,
    request: VmSessionCloseRequest,
    store: SessionStore = Depends(get_store),
) -> VmSessionCloseResponse:
    return store.close_vm_session(vm_session_id, request)


@app.post("/v1/workspace-execution/sessions/{session_id}/inputs", dependencies=[Depends(require_service_token)])
def attach_inputs(
    session_id: str,
    request: InputAttachRequest,
    store: SessionStore = Depends(get_store),
) -> InputAttachResponse:
    return store.attach_inputs(session_id, request)


@app.post("/v1/workspace-execution/sessions/{session_id}/remote-sources/git", dependencies=[Depends(require_service_token)])
def acquire_git_remote_source(
    session_id: str,
    request: GitRemoteSourceAcquireRequest,
    store: SessionStore = Depends(get_store),
) -> GitRemoteSourceAcquireResponse:
    return store.acquire_git_remote_source(session_id, request)


@app.post("/v1/workspace-execution/sessions/{session_id}/files/batch", dependencies=[Depends(require_service_token)])
def write_files_batch(
    session_id: str,
    request: WorkspaceFileBatchWriteRequest,
    store: SessionStore = Depends(get_store),
) -> WorkspaceFileBatchWriteResponse:
    return store.write_files_batch(session_id, request)


@app.post("/v1/workspace-execution/sessions/{session_id}/patches", dependencies=[Depends(require_service_token)])
def apply_patches(
    session_id: str,
    request: WorkspacePatchApplyRequest,
    store: SessionStore = Depends(get_store),
) -> WorkspacePatchApplyResponse:
    return store.apply_patches(session_id, request)


@app.post("/v1/workspace-execution/sessions/{session_id}/commands", dependencies=[Depends(require_service_token)])
def run_command(
    session_id: str,
    request: CommandRunRequest,
    store: SessionStore = Depends(get_store),
) -> CommandRunResponse:
    return store.run_command(session_id, request)


@app.get("/v1/workspace-execution/sessions/{session_id}/diff", dependencies=[Depends(require_service_token)])
def get_diff(session_id: str, store: SessionStore = Depends(get_store)) -> DiffResponse:
    return store.diff(session_id)


@app.get("/v1/workspace-execution/sessions/{session_id}/artifacts", dependencies=[Depends(require_service_token)])
def list_artifacts(session_id: str, store: SessionStore = Depends(get_store)) -> ArtifactListResponse:
    return store.artifacts(session_id)


@app.post("/v1/workspace-execution/sessions/{session_id}/artifacts/package", dependencies=[Depends(require_service_token)])
def package_artifact(
    session_id: str,
    request: ArtifactPackageRequest,
    store: SessionStore = Depends(get_store),
) -> ArtifactPackageResponse:
    return store.package_artifact(session_id, request)


@app.post("/v1/workspace-execution/sessions/{session_id}/artifacts/publish", dependencies=[Depends(require_service_token)])
def publish_artifacts(
    session_id: str,
    request: ArtifactPublishRequest,
    store: SessionStore = Depends(get_store),
    publisher: StorageGuardianClient = Depends(get_storage_publisher),
) -> ArtifactPublishResponse:
    return store.publish_artifacts(session_id, request, publisher)


@app.post("/v1/workspace-execution/sessions/{session_id}/close", dependencies=[Depends(require_service_token)])
def close_session(
    session_id: str,
    request: SessionCloseRequest,
    store: SessionStore = Depends(get_store),
) -> SessionCloseResponse:
    return store.close_session(session_id, request)
