"""Command runners for workspace_execution sessions."""

from __future__ import annotations

import contextlib
import io
import importlib.util
import shutil
import subprocess
import tarfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from workspace_execution.errors import WorkspaceExecutionError
from workspace_execution.materialization import safe_child
from workspace_execution.security_policy import scrub_command_env

RUNNER_UID = 10001
RUNNER_GID = 10001
RUNNER_USER = f"{RUNNER_UID}:{RUNNER_GID}"
RUNNER_DIR_MODE = 0o755
RUNNER_FILE_MODE = 0o644
RUNNER_EXECUTABLE_FILE_MODE = 0o755


@dataclass(frozen=True)
class RunnerLimits:
    timeout_seconds: int
    max_output_bytes: int
    memory_limit: str
    pids_limit: int
    cpu_limit: float


@dataclass(frozen=True)
class RunnerResult:
    run_id: str
    status: str
    exit_code: int | None
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = 0
    error: str | None = None
    output_truncated: bool = False
    metadata: dict[str, str] = field(default_factory=dict)


class LocalProcessRunner:
    """Test/development runner constrained to the disposable workspace path."""

    def missing_tools(self, tools: tuple[str, ...], *, limits: RunnerLimits) -> tuple[str, ...]:
        del limits
        missing: list[str] = []
        for tool in tools:
            if tool == "python":
                if shutil.which("python") or shutil.which("python3"):
                    continue
            elif tool == "pytest":
                if shutil.which("pytest") or importlib.util.find_spec("pytest") is not None:
                    continue
            elif shutil.which(tool):
                continue
            missing.append(tool)
        return tuple(missing)

    def run(
        self,
        *,
        argv: list[str],
        cwd: Path,
        workspace_path: Path,
        artifacts_path: Path,
        env: dict[str, str],
        limits: RunnerLimits,
        redaction_terms: list[str],
        network_enabled: bool = False,
    ) -> RunnerResult:
        del network_enabled
        safe_cwd = safe_child(workspace_path, str(cwd), default=".")
        safe_cwd.mkdir(parents=True, exist_ok=True)
        started = time.time()
        run_id = f"run:{uuid.uuid4().hex}"
        try:
            completed = subprocess.run(
                argv,
                cwd=safe_cwd,
                env=_runner_env(env, artifacts_path=artifacts_path),
                capture_output=True,
                text=True,
                timeout=limits.timeout_seconds,
                check=False,
            )
            duration_ms = int((time.time() - started) * 1000)
            stdout, stdout_truncated = _redact_and_truncate(completed.stdout, limits.max_output_bytes, redaction_terms)
            stderr, stderr_truncated = _redact_and_truncate(completed.stderr, limits.max_output_bytes, redaction_terms)
            return RunnerResult(
                run_id=run_id,
                status="completed" if completed.returncode == 0 else "failed",
                exit_code=completed.returncode,
                stdout=stdout,
                stderr=stderr,
                duration_ms=duration_ms,
                error=None if completed.returncode == 0 else f"exit_code:{completed.returncode}",
                output_truncated=stdout_truncated or stderr_truncated,
                metadata={"backend": "local_process"},
            )
        except subprocess.TimeoutExpired as exc:
            duration_ms = int((time.time() - started) * 1000)
            stdout, stdout_truncated = _redact_and_truncate(str(exc.stdout or ""), limits.max_output_bytes, redaction_terms)
            stderr, stderr_truncated = _redact_and_truncate(str(exc.stderr or ""), limits.max_output_bytes, redaction_terms)
            return RunnerResult(
                run_id=run_id,
                status="timed_out",
                exit_code=None,
                stdout=stdout,
                stderr=stderr,
                duration_ms=duration_ms,
                error=f"timeout:{limits.timeout_seconds}s",
                output_truncated=stdout_truncated or stderr_truncated,
                metadata={"backend": "local_process"},
            )


class DockerEphemeralRunner:
    """Run commands in a short-lived container without host bind mounts."""

    def __init__(
        self,
        *,
        image: str,
        sandbox_runtime: str = "docker",
        require_runtime: bool = False,
    ) -> None:
        self.image = image
        self.sandbox_runtime = sandbox_runtime
        self.require_runtime = require_runtime

    def missing_tools(self, tools: tuple[str, ...], *, limits: RunnerLimits) -> tuple[str, ...]:
        if not tools:
            return ()
        try:
            import docker

            client = docker.from_env(timeout=max(10, min(limits.timeout_seconds, 60)) + 10)
            missing_image_error = self._missing_image_error(client)
            if missing_image_error:
                return tuple(tools)
            script = _tool_preflight_script(tools)
            kwargs: dict[str, object] = {
                "command": ["/bin/sh", "-lc", script],
                "user": RUNNER_USER,
                "read_only": False,
                "network_disabled": True,
                "cap_drop": ["ALL"],
                "security_opt": ["no-new-privileges:true"],
                "mem_limit": limits.memory_limit,
                "pids_limit": limits.pids_limit,
                "nano_cpus": max(100_000_000, int(limits.cpu_limit * 1_000_000_000)),
                "tmpfs": {
                    "/tmp": f"rw,noexec,nosuid,size=16m,uid={RUNNER_UID},gid={RUNNER_GID},mode=1777",
                },
                "environment": {"PATH": "/usr/local/bin:/usr/bin:/bin", "LC_ALL": "C.UTF-8", "LANG": "C.UTF-8"},
                "labels": {
                    "ai.local.managed": "true",
                    "ai.local.component": "workspace-execution-runner-preflight",
                    "ai.local.ephemeral": "true",
                    "ai.local.sandbox-runtime": self.sandbox_runtime,
                },
            }
            if self.sandbox_runtime == "runsc":
                kwargs["runtime"] = "runsc"
            container = None
            try:
                container = client.containers.create(self.image, **kwargs)
            except Exception:
                if self.sandbox_runtime != "runsc" or self.require_runtime:
                    return tuple(tools)
                kwargs.pop("runtime", None)
                container = client.containers.create(self.image, **kwargs)
            try:
                container.start()
                result = container.wait(timeout=max(5, min(limits.timeout_seconds, 30)))
                exit_code = int(result.get("StatusCode", 1)) if isinstance(result, dict) else int(result or 0)
                if exit_code == 0:
                    return ()
                output = _decode(container.logs(stdout=True, stderr=True))
                prefix = "missing_tools:"
                for line in output.splitlines():
                    if line.startswith(prefix):
                        missing = [item for item in line.removeprefix(prefix).split(",") if item]
                        return tuple(missing)
                return tuple(tools)
            finally:
                if container is not None:
                    with contextlib.suppress(Exception):
                        container.remove(force=True)
        except Exception:
            return tuple(tools)

    def run(
        self,
        *,
        argv: list[str],
        cwd: Path,
        workspace_path: Path,
        artifacts_path: Path,
        env: dict[str, str],
        limits: RunnerLimits,
        redaction_terms: list[str],
        network_enabled: bool = False,
    ) -> RunnerResult:
        run_id = f"run:{uuid.uuid4().hex}"
        container = None
        started = time.time()
        try:
            import docker

            client = docker.from_env(timeout=limits.timeout_seconds + 10)
            missing_image_error = self._missing_image_error(client)
            if missing_image_error:
                return RunnerResult(
                    run_id=run_id,
                    status="failed",
                    exit_code=None,
                    duration_ms=int((time.time() - started) * 1000),
                    error=missing_image_error,
                    metadata=self._runtime_metadata(
                        runtime_used="",
                        runtime_fallback_reason=missing_image_error,
                        network_enabled=network_enabled,
                    ),
                )
            create_kwargs = self._create_kwargs(
                argv=argv,
                cwd=cwd,
                workspace_path=workspace_path,
                limits=limits,
                env=env,
                network_enabled=network_enabled,
            )
            try:
                container = client.containers.create(self.image, **create_kwargs)
                runtime_used = self.sandbox_runtime
                runtime_fallback_reason = ""
            except Exception as exc:
                if self.sandbox_runtime != "runsc" or self.require_runtime:
                    raise
                runtime_fallback_reason = str(exc)[:500]
                create_kwargs.pop("runtime", None)
                container = client.containers.create(self.image, **create_kwargs)
                runtime_used = "docker"
            container.put_archive("/", _tar_empty_dir("artifacts", uid=RUNNER_UID, gid=RUNNER_GID, mode=0o1775))
            container.put_archive("/workspace", _tar_directory(workspace_path))
            container.start()
            wait_result = container.wait(timeout=limits.timeout_seconds)
            exit_code = int(wait_result.get("StatusCode", 1)) if isinstance(wait_result, dict) else int(wait_result or 0)
            stdout, stdout_truncated = _redact_and_truncate(
                _decode(container.logs(stdout=True, stderr=False)),
                limits.max_output_bytes,
                redaction_terms,
            )
            stderr, stderr_truncated = _redact_and_truncate(
                _decode(container.logs(stdout=False, stderr=True)),
                limits.max_output_bytes,
                redaction_terms,
            )
            _replace_from_container_archive(container, "/workspace", workspace_path)
            _replace_from_container_archive(container, "/artifacts", artifacts_path)
            return RunnerResult(
                run_id=run_id,
                status="completed" if exit_code == 0 else "failed",
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                duration_ms=int((time.time() - started) * 1000),
                error=None if exit_code == 0 else f"exit_code:{exit_code}",
                output_truncated=stdout_truncated or stderr_truncated,
                metadata=self._runtime_metadata(
                    runtime_used=runtime_used,
                    runtime_fallback_reason=runtime_fallback_reason,
                    network_enabled=network_enabled,
                ),
            )
        except Exception as exc:
            name = exc.__class__.__name__.lower()
            timed_out = "timeout" in name or "readtimeout" in name
            if timed_out and container is not None:
                try:
                    container.kill()
                except Exception:
                    pass
            return RunnerResult(
                run_id=run_id,
                status="timed_out" if timed_out else "failed",
                exit_code=None,
                duration_ms=int((time.time() - started) * 1000),
                error=("timeout" if timed_out else f"runner_error:{str(exc)[:500]}"),
                metadata=self._runtime_metadata(
                    runtime_used="",
                    runtime_fallback_reason=str(exc)[:500],
                    network_enabled=network_enabled,
                ),
            )
        finally:
            if container is not None:
                try:
                    container.remove(force=True)
                except Exception:
                    pass

    def _missing_image_error(self, client: object) -> str:
        images = getattr(client, "images", None)
        if images is None or not hasattr(images, "get"):
            return ""
        try:
            images.get(self.image)
        except Exception as exc:
            if _looks_like_missing_image(exc):
                return f"runner_image_unavailable:{self.image}"
            return ""
        return ""

    def _create_kwargs(
        self,
        *,
        argv: list[str],
        cwd: Path,
        workspace_path: Path,
        limits: RunnerLimits,
        env: dict[str, str],
        network_enabled: bool,
    ) -> dict[str, object]:
        kwargs: dict[str, object] = {
            "command": argv,
            "working_dir": (
                f"/workspace/"
                f"{safe_child(workspace_path, str(cwd), default='.').relative_to(workspace_path).as_posix()}"
            ),
            "user": RUNNER_USER,
            # Docker rejects put_archive into a stopped read-only rootfs.
            # Host isolation is enforced by no bind mounts, disabled
            # network, non-root user, dropped caps and tmpfs for /tmp.
            "read_only": False,
            "network_disabled": not network_enabled,
            "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges:true"],
            "mem_limit": limits.memory_limit,
            "pids_limit": limits.pids_limit,
            "nano_cpus": max(100_000_000, int(limits.cpu_limit * 1_000_000_000)),
            "tmpfs": {
                "/tmp": f"rw,noexec,nosuid,size=64m,uid={RUNNER_UID},gid={RUNNER_GID},mode=1777",
            },
            "environment": _runner_env(env, artifacts_path=Path("/artifacts")),
            "labels": {
                "ai.local.managed": "true",
                "ai.local.component": "workspace-execution-runner",
                "ai.local.ephemeral": "true",
                "ai.local.sandbox-runtime": self.sandbox_runtime,
            },
        }
        if self.sandbox_runtime == "runsc":
            kwargs["runtime"] = "runsc"
        return kwargs

    def _runtime_metadata(
        self,
        *,
        runtime_used: str,
        runtime_fallback_reason: str = "",
        network_enabled: bool = False,
    ) -> dict[str, str]:
        metadata = {
            "backend": "docker_ephemeral",
            "network": "enabled" if network_enabled else "disabled",
            "image": self.image,
            "sandbox_runtime_requested": self.sandbox_runtime,
            "sandbox_runtime_used": runtime_used or self.sandbox_runtime,
            "sandbox_runtime_required": str(self.require_runtime).lower(),
        }
        if runtime_fallback_reason:
            metadata["sandbox_runtime_fallback_reason"] = runtime_fallback_reason
        return metadata


def _runner_env(env: dict[str, str], *, artifacts_path: Path) -> dict[str, str]:
    allowed = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LC_ALL": "C.UTF-8",
        "LANG": "C.UTF-8",
        "WORKSPACE_ARTIFACTS_DIR": str(artifacts_path),
    }
    scrubbed, _removed = scrub_command_env(env, generated_project=False)
    for key, value in scrubbed.items():
        if key not in {"PATH", "LC_ALL", "LANG"}:
            allowed[key] = value
    return allowed


def _tool_preflight_script(tools: tuple[str, ...]) -> str:
    quoted_tools = " ".join(shlex_quote(tool) for tool in tools)
    return (
        "missing=''; "
        f"for tool in {quoted_tools}; do "
        "check_tool=\"$tool\"; "
        "if [ \"$tool\" = python ]; then command -v python >/dev/null 2>&1 || command -v python3 >/dev/null 2>&1 || missing=\"$missing${missing:+,}$tool\"; "
        "elif [ \"$tool\" = pytest ]; then command -v pytest >/dev/null 2>&1 || python -c 'import importlib.util, sys; sys.exit(0 if importlib.util.find_spec(\"pytest\") else 1)' >/dev/null 2>&1 || python3 -c 'import importlib.util, sys; sys.exit(0 if importlib.util.find_spec(\"pytest\") else 1)' >/dev/null 2>&1 || missing=\"$missing${missing:+,}$tool\"; "
        "else command -v \"$check_tool\" >/dev/null 2>&1 || missing=\"$missing${missing:+,}$tool\"; fi; "
        "done; "
        "if [ -n \"$missing\" ]; then echo \"missing_tools:$missing\"; exit 127; fi"
    )


def shlex_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _redact_and_truncate(value: str, max_bytes: int, terms: list[str]) -> tuple[str, bool]:
    redacted = value
    for term in sorted((item for item in terms if item), key=len, reverse=True):
        redacted = redacted.replace(term, "<redacted-path>")
    encoded = redacted.encode("utf-8")
    if len(encoded) <= max_bytes:
        return redacted, False
    return encoded[:max_bytes].decode("utf-8", errors="replace") + "\n[truncated]", True


def _tar_directory(path: Path) -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as tar:
        for item in sorted(path.rglob("*")):
            if item.is_symlink():
                continue
            rel = item.relative_to(path).as_posix()
            stat = item.stat()
            info = tarfile.TarInfo(rel + "/" if item.is_dir() else rel)
            info.uid = RUNNER_UID
            info.gid = RUNNER_GID
            info.uname = "cmdsandbox"
            info.gname = "cmdsandbox"
            info.mtime = int(stat.st_mtime)
            if item.is_dir():
                info.type = tarfile.DIRTYPE
                info.mode = RUNNER_DIR_MODE
                tar.addfile(info)
                continue
            if not item.is_file():
                continue
            info.size = stat.st_size
            info.mode = RUNNER_EXECUTABLE_FILE_MODE if stat.st_mode & 0o111 else RUNNER_FILE_MODE
            with item.open("rb") as handle:
                tar.addfile(info, handle)
    stream.seek(0)
    return stream.getvalue()


def _tar_empty_dir(name: str, *, uid: int, gid: int, mode: int) -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as tar:
        info = tarfile.TarInfo(name.rstrip("/") + "/")
        info.type = tarfile.DIRTYPE
        info.mode = mode
        info.uid = uid
        info.gid = gid
        tar.addfile(info)
    stream.seek(0)
    return stream.getvalue()


def _replace_from_container_archive(container, container_path: str, target: Path) -> None:
    stream, _stats = container.get_archive(container_path)
    data = b"".join(stream)
    shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tar:
        _safe_extract(tar, target, strip_top_level=Path(container_path).name)


def _safe_extract(tar: tarfile.TarFile, target: Path, *, strip_top_level: str = "") -> None:
    root = target.resolve()
    for member in tar.getmembers():
        if member.issym() or member.islnk():
            continue
        member_name = _archive_member_name(member.name, strip_top_level=strip_top_level)
        if not member_name:
            continue
        destination = (target / member_name).resolve()
        if destination != root and root not in destination.parents:
            raise WorkspaceExecutionError("archive_path_not_allowed", "runner archive path escapes the session")
        if member.isdir():
            destination.mkdir(parents=True, exist_ok=True)
            destination.chmod(RUNNER_DIR_MODE)
            continue
        if not member.isfile():
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.parent.chmod(RUNNER_DIR_MODE)
        source = tar.extractfile(member)
        if source is None:
            continue
        with source, destination.open("wb") as handle:
            shutil.copyfileobj(source, handle)
        destination.chmod(RUNNER_EXECUTABLE_FILE_MODE if member.mode & 0o111 else RUNNER_FILE_MODE)


def _archive_member_name(name: str, *, strip_top_level: str) -> str:
    parts = Path(name).parts
    if strip_top_level and parts and parts[0] == strip_top_level:
        parts = parts[1:]
    return "/".join(parts)


def _decode(value: bytes | str) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _looks_like_missing_image(exc: Exception) -> bool:
    name = exc.__class__.__name__.lower()
    message = str(exc).lower()
    return (
        "imagenotfound" in name
        or "notfound" in name
        or "no such image" in message
        or ("404" in message and "image" in message)
    )
