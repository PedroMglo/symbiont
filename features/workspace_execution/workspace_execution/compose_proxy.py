"""VM-backed Docker Compose runtime proxy client.

The proxy is an optional workspace_execution backend for validation profiles
that need a real isolated container runtime. It must prove VM-backed isolation
before any generated compose command is delegated to it.
"""

from __future__ import annotations

import base64
import contextlib
import gzip
import io
import json
import tarfile
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from workspace_execution.errors import WorkspaceExecutionError
from workspace_execution.materialization import safe_child, safe_relative_path
from workspace_execution.runner import RunnerLimits, _redact_and_truncate


@dataclass(frozen=True)
class ComposeRuntimeProxyPreflight:
    ready: bool
    status: str
    failure_code: str | None = None
    failure_reason: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ComposeRuntimeProxyResult:
    run_id: str
    status: str
    exit_code: int | None
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = 0
    error: str | None = None
    output_truncated: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


class ComposeRuntimeProxyClient:
    """Delegate compose runtime commands to an explicitly VM-backed proxy."""

    def __init__(self, *, base_url: str, token: str = "", timeout_seconds: int = 30) -> None:
        self.base_url = base_url.strip().rstrip("/")
        self.token = token.strip()
        self.timeout_seconds = timeout_seconds

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    def preflight(self) -> ComposeRuntimeProxyPreflight:
        if not self.configured:
            return ComposeRuntimeProxyPreflight(
                ready=False,
                status="not_configured",
                failure_code="docker_runtime_unavailable",
                failure_reason="no VM-backed compose runtime proxy is configured for workspace_execution",
                evidence=_default_isolation_evidence(compose_runtime=False, vm_backed=False),
            )
        try:
            payload = self._request_json("GET", "/v1/compose-runtime/capabilities")
        except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            return ComposeRuntimeProxyPreflight(
                ready=False,
                status="unavailable",
                failure_code="docker_runtime_unavailable",
                failure_reason=f"VM-backed compose runtime proxy is unreachable: {exc}",
                evidence={
                    **_default_isolation_evidence(compose_runtime=False, vm_backed=False),
                    "base_url_configured": True,
                },
            )
        evidence = dict(payload)
        vm_backed = _nested_bool(payload, "vm_backed", "vm_backed_runtime", "vm_isolation")
        compose_runtime = _nested_bool(payload, "compose_runtime", "docker_compose_runtime")
        host_execution_used = _nested_bool(payload, "host_execution_used")
        host_docker_socket_exposed = _nested_bool(payload, "host_docker_socket_exposed")
        fallback_to_host_allowed = _nested_bool(payload, "fallback_to_host_allowed", "host_execution_fallback")
        raw_status = str(payload.get("status") or payload.get("health") or "").lower()
        status_ready = raw_status in {"", "ok", "ready", "healthy"} or (vm_backed and compose_runtime)
        if not compose_runtime:
            return ComposeRuntimeProxyPreflight(
                ready=False,
                status="unsafe",
                failure_code="docker_runtime_unavailable",
                failure_reason="configured compose runtime proxy does not advertise compose runtime capability",
                evidence=evidence,
            )
        if not vm_backed or host_execution_used or host_docker_socket_exposed or fallback_to_host_allowed:
            return ComposeRuntimeProxyPreflight(
                ready=False,
                status="unsafe",
                failure_code="compose_runtime_isolation_failed",
                failure_reason="configured compose runtime proxy failed VM isolation invariants",
                evidence=evidence,
            )
        if not status_ready:
            return ComposeRuntimeProxyPreflight(
                ready=False,
                status=raw_status or "unavailable",
                failure_code="docker_runtime_unavailable",
                failure_reason="configured compose runtime proxy is not ready",
                evidence=evidence,
            )
        return ComposeRuntimeProxyPreflight(
            ready=True,
            status="ready",
            evidence={
                **evidence,
                "vm_backed": True,
                "compose_runtime": True,
                "host_execution_used": False,
                "host_docker_socket_exposed": False,
                "fallback_to_host_allowed": False,
            },
        )

    def run_command(
        self,
        *,
        argv: list[str],
        cwd: Path,
        workspace_path: Path,
        artifacts_path: Path,
        env: dict[str, str],
        limits: RunnerLimits,
        redaction_terms: list[str],
        material_session_id: str | None,
        vm_session_id: str | None,
    ) -> ComposeRuntimeProxyResult:
        if not self.configured:
            return ComposeRuntimeProxyResult(
                run_id=f"run:{uuid.uuid4().hex}",
                status="blocked",
                exit_code=None,
                error="docker_runtime_unavailable",
                metadata={
                    "backend": "compose_runtime_proxy",
                    "preflight_status": "not_configured",
                    "host_execution_used": False,
                    "vm_backed": False,
                },
            )
        started = time.time()
        request = {
            "schema_version": "workspace_compose_runtime.v1",
            "argv": argv,
            "cwd": str(cwd),
            "env": env,
            "timeout_seconds": limits.timeout_seconds,
            "workspace_tar_gz_b64": _tar_gz_b64(workspace_path),
            "material_session_id": material_session_id,
            "vm_session_id": vm_session_id,
            "security_requirements": {
                "vm_backed": True,
                "host_execution_used": False,
                "host_docker_socket_exposed": False,
                "fallback_to_host_allowed": False,
            },
        }
        try:
            payload = self._request_json("POST", "/v1/compose-runtime/run", payload=request)
        except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            duration_ms = int((time.time() - started) * 1000)
            message = f"VM-backed compose runtime proxy request failed: {exc}"
            return ComposeRuntimeProxyResult(
                run_id=f"run:{uuid.uuid4().hex}",
                status="blocked",
                exit_code=None,
                duration_ms=duration_ms,
                error=message,
                metadata={
                    "backend": "compose_runtime_proxy",
                    "error_code": "docker_runtime_unavailable",
                    "host_execution_used": False,
                    "vm_backed": True,
                },
            )
        metadata = dict(payload.get("metadata") or {})
        _assert_proxy_runtime_evidence(metadata)
        workspace_archive = str(payload.get("workspace_tar_gz_b64") or "")
        artifacts_archive = str(payload.get("artifacts_tar_gz_b64") or "")
        if workspace_archive:
            _extract_tar_gz_b64(workspace_archive, workspace_path)
        if artifacts_archive:
            _extract_tar_gz_b64(artifacts_archive, artifacts_path)
        duration_ms = int(payload.get("duration_ms") or int((time.time() - started) * 1000))
        stdout, stdout_truncated = _redact_and_truncate(str(payload.get("stdout") or ""), limits.max_output_bytes, redaction_terms)
        stderr, stderr_truncated = _redact_and_truncate(str(payload.get("stderr") or ""), limits.max_output_bytes, redaction_terms)
        exit_code = payload.get("exit_code")
        status = str(payload.get("status") or ("completed" if exit_code == 0 else "failed"))
        return ComposeRuntimeProxyResult(
            run_id=str(payload.get("run_id") or f"run:{uuid.uuid4().hex}"),
            status=status,
            exit_code=int(exit_code) if exit_code is not None else None,
            stdout=stdout,
            stderr=stderr,
            duration_ms=duration_ms,
            error=str(payload.get("error") or "") or None,
            output_truncated=stdout_truncated or stderr_truncated or bool(payload.get("output_truncated")),
            metadata={
                **metadata,
                "backend": "compose_runtime_proxy",
                "vm_backed": True,
                "host_execution_used": False,
                "host_docker_socket_exposed": False,
                "fallback_to_host_allowed": False,
            },
        )

    def _request_json(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            method=method,
            headers=self._headers(payload is not None),
        )
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            raw = response.read().decode("utf-8")
        parsed = json.loads(raw or "{}")
        if not isinstance(parsed, dict):
            raise ValueError("compose runtime proxy response must be a JSON object")
        return parsed

    def _headers(self, json_body: bool) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if json_body:
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
            headers["X-API-Key"] = self.token
        return headers


class ComposeRuntimeProxyServer:
    """Owned implementation of the compose runtime proxy contract.

    The implementation uses a dedicated Docker-in-Docker daemon per run. The
    host Docker control plane launches only the trusted dind and runner
    containers; generated project containers are created inside the disposable
    dind daemon and never receive the host Docker socket.
    """

    def __init__(
        self,
        *,
        backend: str,
        dind_image: str,
        runner_image: str,
        timeout_seconds: int,
    ) -> None:
        self.backend = backend.strip() or "dedicated-dind"
        self.dind_image = dind_image.strip() or "docker:27-dind"
        self.runner_image = runner_image.strip() or "ai-local-command-sandbox:latest"
        self.timeout_seconds = timeout_seconds

    def capabilities(self) -> dict[str, Any]:
        docker_status, reason = self._docker_control_status()
        ready = self.backend == "dedicated-dind" and docker_status == "ready"
        return {
            "status": "ready" if ready else docker_status,
            "compose_runtime": ready,
            "isolated_container_runtime": ready,
            "vm_backed": ready,
            "vm_backed_runtime": ready,
            "host_execution_used": False,
            "host_docker_socket_exposed": False,
            "fallback_to_host_allowed": False,
            "backend": self.backend,
            "isolation_mode": "dedicated-dind",
            "dind_image": self.dind_image,
            "runner_image": self.runner_image,
            "failure_code": None if ready else "docker_runtime_unavailable",
            "failure_reason": None if ready else reason,
        }

    def run(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.backend != "dedicated-dind":
            raise WorkspaceExecutionError(
                "docker_runtime_unavailable",
                "workspace_execution compose runtime backend is not supported",
                details={"backend": self.backend},
            )
        started = time.time()
        run_id = f"run:{uuid.uuid4().hex}"
        timeout_seconds = int(payload.get("timeout_seconds") or self.timeout_seconds)
        argv = [str(item) for item in payload.get("argv") or []]
        cwd = safe_relative_path(str(payload.get("cwd") or "."))
        with _temporary_directory() as temp_root:
            workspace_path = temp_root / "workspace"
            artifacts_path = temp_root / "artifacts"
            workspace_path.mkdir()
            artifacts_path.mkdir()
            _extract_tar_gz_b64(str(payload.get("workspace_tar_gz_b64") or ""), workspace_path)
            _validate_compose_workspace(workspace_path)
            result = self._run_in_dind(
                run_id=run_id,
                argv=argv,
                cwd=cwd,
                workspace_path=workspace_path,
                timeout_seconds=timeout_seconds,
            )
            duration_ms = int((time.time() - started) * 1000)
            (artifacts_path / "compose-runtime.log").write_text(
                result["stdout"] + ("\n" if result["stdout"] and result["stderr"] else "") + result["stderr"],
                encoding="utf-8",
            )
            return {
                "run_id": run_id,
                "status": result["status"],
                "exit_code": result["exit_code"],
                "stdout": result["stdout"],
                "stderr": result["stderr"],
                "duration_ms": duration_ms,
                "error": result.get("error"),
                "workspace_tar_gz_b64": _tar_gz_b64(workspace_path),
                "artifacts_tar_gz_b64": _tar_gz_b64(artifacts_path),
                "metadata": {
                    "backend": "compose_runtime_proxy",
                    "compose_runtime_backend": self.backend,
                    "isolation_mode": "dedicated-dind",
                    "vm_backed": True,
                    "vm_backed_runtime": True,
                    "isolated_container_runtime": True,
                    "host_execution_used": False,
                    "host_docker_socket_exposed": False,
                    "fallback_to_host_allowed": False,
                    "auto_cleanup": result["auto_cleanup"],
                    "compose_project": result["compose_project"],
                    "services": result["services"],
                    "service_container_ids": result["service_container_ids"],
                    "trusted_runtime_containers": result["trusted_runtime_containers"],
                    "health_checks": result["health_checks"],
                    "logs_collected": result["logs_collected"],
                    "cleanup": result["cleanup"],
                    "cleanup_required_for_completion": result["auto_cleanup"],
                },
            }

    def _docker_control_status(self) -> tuple[str, str]:
        try:
            import docker

            client = docker.from_env(timeout=max(5, min(self.timeout_seconds, 30)))
            client.ping()
            return "ready", ""
        except Exception as exc:
            return "unavailable", str(exc)[:500]

    def _run_in_dind(
        self,
        *,
        run_id: str,
        argv: list[str],
        cwd: Path,
        workspace_path: Path,
        timeout_seconds: int,
    ) -> dict[str, Any]:
        import docker

        client = docker.from_env(timeout=timeout_seconds + 30)
        project_name = f"ailocal-{uuid.uuid4().hex[:12]}"
        network_name = f"{project_name}-net"
        dind_name = f"{project_name}-dind"
        runner_name = f"{project_name}-runner"
        network = None
        dind = None
        runner = None
        stdout = ""
        stderr = ""
        services: list[str] = []
        service_container_ids: list[str] = []
        health_checks: list[dict[str, Any]] = []
        trusted_runtime_containers: list[dict[str, str]] = []
        auto_cleanup = False
        cleanup: dict[str, Any] = {"required": False, "attempted": False, "completed": True, "exit_code": 0}
        logs_collected = False
        try:
            self._ensure_image(client, self.dind_image)
            self._ensure_image(client, self.runner_image)
            network = client.networks.create(
                network_name,
                labels=_compose_runtime_labels(run_id, "network"),
            )
            dind = client.containers.create(
                self.dind_image,
                name=dind_name,
                hostname="dind",
                privileged=True,
                detach=True,
                environment={"DOCKER_TLS_CERTDIR": ""},
                command=["dockerd-entrypoint.sh", "--host=tcp://0.0.0.0:2375", "--tls=false"],
                labels=_compose_runtime_labels(run_id, "dind"),
                network=network.name,
                mem_limit="2g",
                pids_limit=2048,
            )
            dind.start()
            trusted_runtime_containers.append({"role": "dind", "name": dind_name, "container_id": str(dind.id)})
            self._wait_for_dind(client, network.name, timeout_seconds=min(45, timeout_seconds))
            command = _compose_runner_command(argv, cwd)
            runner = client.containers.create(
                self.runner_image,
                name=runner_name,
                command=["/bin/sh", "-lc", command],
                user="10001:10001",
                working_dir="/workspace/project",
                detach=True,
                environment={
                    "DOCKER_HOST": "tcp://dind:2375",
                    "COMPOSE_PROJECT_NAME": project_name,
                    "PATH": "/usr/local/bin:/usr/bin:/bin",
                    "LC_ALL": "C.UTF-8",
                    "LANG": "C.UTF-8",
                },
                labels=_compose_runtime_labels(run_id, "runner"),
                network=network.name,
                mem_limit="1g",
                pids_limit=1024,
                cap_drop=["ALL"],
                security_opt=["no-new-privileges:true"],
            )
            runner.put_archive("/workspace/project", _tar_directory(workspace_path))
            runner.start()
            trusted_runtime_containers.append({"role": "runner", "name": runner_name, "container_id": str(runner.id)})
            wait_result = runner.wait(timeout=timeout_seconds)
            exit_code = int(wait_result.get("StatusCode", 1)) if isinstance(wait_result, dict) else int(wait_result or 0)
            stdout = _decode(runner.logs(stdout=True, stderr=False))
            stderr = _decode(runner.logs(stdout=False, stderr=True))
            if _is_compose_up_detached(argv):
                auto_cleanup = True
                cleanup = {"required": True, "attempted": True, "completed": False, "exit_code": None}
                logs_result = self._run_control_container(
                    client,
                    network_name=network.name,
                    project_name=project_name,
                    command=(
                        "printf '__AI_LOCAL_COMPOSE_PS_START__\\n'; "
                        "docker-compose ps || true; "
                        "printf '__AI_LOCAL_COMPOSE_IDS_START__\\n'; "
                        "docker-compose ps -q || true; "
                        "printf '__AI_LOCAL_COMPOSE_LOGS_START__\\n'; "
                        "docker-compose logs --no-color --tail=200 || true; "
                        "printf '__AI_LOCAL_COMPOSE_DOWN_START__\\n'; "
                        "docker-compose down -v --remove-orphans"
                    ),
                    workspace_path=workspace_path,
                    timeout_seconds=max(30, min(timeout_seconds, 180)),
                    run_id=run_id,
                )
                stdout = "\n".join(part for part in (stdout, logs_result["stdout"]) if part)
                stderr = "\n".join(part for part in (stderr, logs_result["stderr"]) if part)
                ps_output = _marker_section(
                    logs_result["stdout"],
                    "__AI_LOCAL_COMPOSE_PS_START__",
                    "__AI_LOCAL_COMPOSE_IDS_START__",
                )
                ids_output = _marker_section(
                    logs_result["stdout"],
                    "__AI_LOCAL_COMPOSE_IDS_START__",
                    "__AI_LOCAL_COMPOSE_LOGS_START__",
                )
                services = _extract_services_from_ps(ps_output)
                service_container_ids = _extract_container_ids(ids_output)
                health_checks = _health_checks_from_ps(ps_output)
                logs_collected = "__AI_LOCAL_COMPOSE_LOGS_START__" in logs_result["stdout"]
                cleanup = {
                    "required": True,
                    "attempted": True,
                    "completed": int(logs_result["exit_code"]) == 0,
                    "exit_code": int(logs_result["exit_code"]),
                    "down_command": "docker-compose down -v --remove-orphans",
                }
            return {
                "status": "completed" if exit_code == 0 else "failed",
                "exit_code": exit_code,
                "stdout": stdout,
                "stderr": stderr,
                "error": None if exit_code == 0 else f"exit_code:{exit_code}",
                "auto_cleanup": auto_cleanup,
                "compose_project": project_name,
                "services": services,
                "service_container_ids": service_container_ids,
                "trusted_runtime_containers": trusted_runtime_containers,
                "health_checks": health_checks,
                "logs_collected": logs_collected,
                "cleanup": cleanup,
            }
        finally:
            for container in (runner, dind):
                if container is not None:
                    with contextlib.suppress(Exception):
                        container.remove(force=True)
            if network is not None:
                with contextlib.suppress(Exception):
                    network.remove()

    def _run_control_container(
        self,
        client: Any,
        *,
        network_name: str,
        project_name: str,
        command: str,
        workspace_path: Path,
        timeout_seconds: int,
        run_id: str,
    ) -> dict[str, Any]:
        name = f"{project_name}-control"
        container = client.containers.create(
            self.runner_image,
            name=name,
            command=["/bin/sh", "-lc", command],
            user="10001:10001",
            working_dir="/workspace/project",
            detach=True,
            environment={
                "DOCKER_HOST": "tcp://dind:2375",
                "COMPOSE_PROJECT_NAME": project_name,
                "PATH": "/usr/local/bin:/usr/bin:/bin",
            },
            labels=_compose_runtime_labels(run_id, "control"),
            network=network_name,
            mem_limit="512m",
            pids_limit=512,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
        )
        try:
            container.put_archive("/workspace/project", _tar_directory(workspace_path))
            container.start()
            wait_result = container.wait(timeout=timeout_seconds)
            exit_code = int(wait_result.get("StatusCode", 1)) if isinstance(wait_result, dict) else int(wait_result or 0)
            return {
                "exit_code": exit_code,
                "stdout": _decode(container.logs(stdout=True, stderr=False)),
                "stderr": _decode(container.logs(stdout=False, stderr=True)),
            }
        finally:
            with contextlib.suppress(Exception):
                container.remove(force=True)

    def _wait_for_dind(self, client: Any, network_name: str, *, timeout_seconds: int) -> None:
        deadline = time.time() + timeout_seconds
        last_output = ""
        while time.time() < deadline:
            result = self._run_control_container(
                client,
                network_name=network_name,
                project_name=f"probe-{uuid.uuid4().hex[:8]}",
                command="docker version",
                workspace_path=_empty_workspace(),
                timeout_seconds=10,
                run_id=f"probe:{uuid.uuid4().hex}",
            )
            last_output = result["stderr"] or result["stdout"]
            if result["exit_code"] == 0:
                return
            time.sleep(1)
        raise WorkspaceExecutionError(
            "docker_runtime_unavailable",
            "dedicated Docker-in-Docker runtime did not become ready",
            details={"last_output": last_output[-1000:]},
        )

    def _ensure_image(self, client: Any, image: str) -> None:
        try:
            client.images.get(image)
        except Exception:
            client.images.pull(image)


def _nested_bool(payload: dict[str, Any], *keys: str) -> bool:
    candidates: list[Any] = []
    for key in keys:
        candidates.append(payload.get(key))
    for section in ("capabilities", "security", "policy", "runtime"):
        nested = payload.get(section)
        if isinstance(nested, dict):
            for key in keys:
                candidates.append(nested.get(key))
    return any(value is True or str(value).lower() == "true" for value in candidates)


def _default_isolation_evidence(*, compose_runtime: bool, vm_backed: bool) -> dict[str, Any]:
    return {
        "compose_runtime": compose_runtime,
        "vm_backed": vm_backed,
        "host_execution_used": False,
        "host_docker_socket_exposed": False,
        "fallback_to_host_allowed": False,
    }


def _assert_proxy_runtime_evidence(metadata: dict[str, Any]) -> None:
    evidence = {
        "vm_backed": _nested_bool(metadata, "vm_backed", "vm_backed_runtime", "vm_isolation"),
        "host_execution_used": _nested_bool(metadata, "host_execution_used"),
        "host_docker_socket_exposed": _nested_bool(metadata, "host_docker_socket_exposed"),
        "fallback_to_host_allowed": _nested_bool(metadata, "fallback_to_host_allowed", "host_execution_fallback"),
    }
    if (
        not evidence["vm_backed"]
        or evidence["host_execution_used"]
        or evidence["host_docker_socket_exposed"]
        or evidence["fallback_to_host_allowed"]
    ):
        raise WorkspaceExecutionError(
            "compose_runtime_isolation_failed",
            "compose runtime proxy response failed VM isolation invariants",
            details={**metadata, **evidence},
        )


def _tar_gz_b64(root: Path) -> str:
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb") as gzip_file:
        with tarfile.open(fileobj=gzip_file, mode="w") as archive:
            for item in sorted(root.rglob("*")):
                relative = item.relative_to(root)
                if _unsafe_archive_member(relative):
                    raise WorkspaceExecutionError(
                        "path_not_allowed",
                        "compose runtime archive member escapes the workspace",
                        details={"path": str(relative)},
                    )
                if item.is_symlink() or not (item.is_file() or item.is_dir()):
                    raise WorkspaceExecutionError(
                        "path_not_allowed",
                        "compose runtime archive rejects symlinks and special files",
                        details={"path": str(relative)},
                    )
                archive.add(item, arcname=str(relative), recursive=False)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _extract_tar_gz_b64(encoded: str, destination: Path) -> None:
    if not encoded:
        return
    raw = base64.b64decode(encoded.encode("ascii"), validate=True)
    with gzip.GzipFile(fileobj=io.BytesIO(raw), mode="rb") as gzip_file:
        with tarfile.open(fileobj=gzip_file, mode="r:") as archive:
            for member in archive.getmembers():
                relative = Path(member.name)
                if _unsafe_archive_member(relative) or member.issym() or member.islnk() or member.isdev():
                    raise WorkspaceExecutionError(
                        "path_not_allowed",
                        "compose runtime archive contains an unsafe member",
                        details={"path": member.name},
                    )
                target = safe_child(destination, member.name)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if not member.isfile():
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    continue
                target.write_bytes(source.read())


def _unsafe_archive_member(relative: Path) -> bool:
    value = str(relative).replace("\\", "/")
    return (
        "\x00" in value
        or value.startswith(("/", "~"))
        or (len(value) >= 2 and value[1] == ":")
        or any(part in {"", ".", ".."} for part in Path(value).parts)
    )


def _validate_compose_workspace(workspace_path: Path) -> None:
    files = [
        item
        for name in ("compose.yaml", "compose.yml", "docker-compose.yml", "docker-compose.yaml")
        for item in workspace_path.rglob(name)
    ]
    if not files:
        raise WorkspaceExecutionError(
            "compose_file_missing",
            "docker-compose-runtime requires a compose file in the workspace",
        )
    for compose_file in files:
        _validate_compose_file(compose_file, workspace_path=workspace_path)


def _validate_compose_file(compose_file: Path, *, workspace_path: Path) -> None:
    try:
        import yaml
    except Exception as exc:
        raise WorkspaceExecutionError(
            "validation_tool_unavailable",
            "PyYAML is required for compose runtime policy validation",
            details={"error": str(exc)},
        ) from exc
    try:
        payload = yaml.safe_load(compose_file.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        raise WorkspaceExecutionError(
            "compose_policy_violation",
            "compose file could not be parsed before runtime validation",
            details={"path": str(compose_file.relative_to(workspace_path)), "error": str(exc)},
        ) from exc
    services = payload.get("services")
    if not isinstance(services, dict) or not services:
        raise WorkspaceExecutionError(
            "compose_policy_violation",
            "compose runtime validation requires at least one service",
            details={"path": str(compose_file.relative_to(workspace_path))},
        )
    for service_name, service in services.items():
        if not isinstance(service, dict):
            continue
        _validate_compose_service(str(service_name), service, compose_file=compose_file, workspace_path=workspace_path)


def _validate_compose_service(
    service_name: str,
    service: dict[str, Any],
    *,
    compose_file: Path,
    workspace_path: Path,
) -> None:
    forbidden_equals = {
        "network_mode": {"host"},
        "pid": {"host"},
        "ipc": {"host"},
        "cgroup": {"host"},
        "cgroup_parent": {"host"},
        "uts": {"host"},
    }
    for key, forbidden in forbidden_equals.items():
        value = str(service.get(key) or "").strip().lower()
        if value in forbidden:
            _compose_policy_error(service_name, key, value, compose_file, workspace_path)
    for key in ("privileged", "devices", "device_cgroup_rules", "cap_add"):
        value = service.get(key)
        if value is True or (isinstance(value, list | tuple | dict) and bool(value)):
            _compose_policy_error(service_name, key, value, compose_file, workspace_path)
    for option in service.get("security_opt") or []:
        text = str(option).lower()
        if "unconfined" in text or "no-new-privileges:false" in text:
            _compose_policy_error(service_name, "security_opt", option, compose_file, workspace_path)
    for host in service.get("extra_hosts") or []:
        if "host.docker.internal" in str(host).lower():
            _compose_policy_error(service_name, "extra_hosts", host, compose_file, workspace_path)
    for volume in service.get("volumes") or []:
        _validate_compose_volume(service_name, volume, compose_file=compose_file, workspace_path=workspace_path)


def _validate_compose_volume(
    service_name: str,
    volume: Any,
    *,
    compose_file: Path,
    workspace_path: Path,
) -> None:
    source = ""
    if isinstance(volume, str):
        source = volume.split(":", 1)[0]
    elif isinstance(volume, dict):
        source = str(volume.get("source") or volume.get("src") or "")
        volume_type = str(volume.get("type") or "").lower()
        if volume_type == "bind":
            _compose_policy_error(service_name, "volumes", volume, compose_file, workspace_path)
    if not source:
        return
    normalized = source.replace("\\", "/")
    forbidden_prefixes = ("/", "~", "../")
    forbidden_contains = ("/var/run/docker.sock", "/proc", "/sys", "/dev", "/home", "/mnt")
    if normalized.startswith(forbidden_prefixes) or any(item in normalized for item in forbidden_contains):
        _compose_policy_error(service_name, "volumes", volume, compose_file, workspace_path)


def _compose_policy_error(
    service_name: str,
    key: str,
    value: Any,
    compose_file: Path,
    workspace_path: Path,
) -> None:
    raise WorkspaceExecutionError(
        "compose_policy_violation",
        "compose runtime validation blocked an unsafe generated compose option",
        details={
            "service": service_name,
            "field": key,
            "value": value,
            "path": str(compose_file.relative_to(workspace_path)),
            "host_execution_used": False,
            "host_docker_socket_exposed": False,
        },
    )


def _compose_runner_command(argv: list[str], cwd: Path) -> str:
    import shlex

    if not argv:
        raise WorkspaceExecutionError("command_invalid", "compose runtime command argv cannot be empty")
    first = Path(argv[0]).name
    if first not in {"docker-compose", "docker"}:
        raise WorkspaceExecutionError(
            "command_invalid",
            "compose runtime accepts only docker compose or docker-compose commands",
            details={"argv": argv},
        )
    if first == "docker" and (len(argv) < 2 or argv[1] != "compose"):
        raise WorkspaceExecutionError(
            "command_invalid",
            "compose runtime docker commands must use the compose subcommand",
            details={"argv": argv},
        )
    quoted = " ".join(shlex.quote(item) for item in argv)
    return f"cd {shlex.quote(str(cwd))} && {quoted}"


def _is_compose_up_detached(argv: list[str]) -> bool:
    tokens = [str(item) for item in argv]
    if not tokens:
        return False
    if Path(tokens[0]).name == "docker" and len(tokens) >= 3 and tokens[1] == "compose":
        action_tokens = tokens[2:]
    elif Path(tokens[0]).name == "docker-compose":
        action_tokens = tokens[1:]
    else:
        return False
    return "up" in action_tokens and any(token in {"-d", "--detach"} for token in action_tokens)


def _extract_services_from_ps(output: str) -> list[str]:
    services: list[str] = []
    for line in output.splitlines()[1:]:
        columns = line.split()
        if columns:
            services.append(columns[0])
    return services[:64]


def _extract_container_ids(output: str) -> list[str]:
    ids: list[str] = []
    for line in output.splitlines():
        token = line.strip()
        if token and all(char in "0123456789abcdef" for char in token.lower()):
            ids.append(token)
    return ids[:128]


def _health_checks_from_ps(output: str) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for line in output.splitlines()[1:]:
        columns = line.split()
        if not columns:
            continue
        text = f" {line.casefold()} "
        checks.append(
            {
                "service": columns[0],
                "healthy": " healthy " in text or " up " in text,
                "raw": line[:500],
            }
        )
    return checks[:128]


def _marker_section(output: str, start_marker: str, end_marker: str) -> str:
    if start_marker not in output:
        return ""
    after = output.split(start_marker, 1)[1]
    if end_marker in after:
        after = after.split(end_marker, 1)[0]
    return after.strip()


def _compose_runtime_labels(run_id: str, role: str) -> dict[str, str]:
    return {
        "ai.local.managed": "true",
        "ai.local.component": "workspace-execution-compose-runtime",
        "ai.local.compose-runtime.role": role,
        "ai.local.compose-runtime.run-id": run_id,
        "ai.local.ephemeral": "true",
    }


def _tar_directory(root: Path) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for item in sorted(root.rglob("*")):
            relative = item.relative_to(root)
            if _unsafe_archive_member(relative) or item.is_symlink() or not (item.is_file() or item.is_dir()):
                continue
            info = archive.gettarinfo(str(item), arcname=str(relative))
            info.uid = 10001
            info.gid = 10001
            info.uname = "cmdsandbox"
            info.gname = "cmdsandbox"
            if item.is_file():
                with item.open("rb") as handle:
                    archive.addfile(info, handle)
            else:
                archive.addfile(info)
    buffer.seek(0)
    return buffer.getvalue()


def _empty_workspace() -> Path:
    root = Path("/tmp/workspace_execution_empty")
    root.mkdir(parents=True, exist_ok=True)
    return root


def _decode(value: bytes | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return value.decode("utf-8", errors="replace")


@contextlib.contextmanager
def _temporary_directory():
    import tempfile

    path = Path(tempfile.mkdtemp(prefix="workspace-compose-runtime-"))
    try:
        yield path
    finally:
        import shutil

        shutil.rmtree(path, ignore_errors=True)
