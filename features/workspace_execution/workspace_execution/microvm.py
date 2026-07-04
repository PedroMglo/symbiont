"""QEMU microVM backend for VM-backed workspace execution."""

from __future__ import annotations

import base64
import contextlib
import gzip
import hashlib
import inspect
import io
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import textwrap
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from workspace_execution.errors import WorkspaceExecutionError
from workspace_execution.runner import RunnerLimits, _redact_and_truncate
from workspace_execution.security_policy import scrub_command_env


RESULT_BEGIN = "__AI_LOCAL_VM_RESULT_BEGIN__"
RESULT_END = "__AI_LOCAL_VM_RESULT_END__"
ROOTFS_MARKER = ".ai-local-rootfs-image.json"
KVM_MAJOR = 10
KVM_MINOR = 232
HUNK_RE = re.compile(r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? \+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@")
_ROOTFS_LOCKS: dict[str, threading.Lock] = {}
_ROOTFS_LOCKS_GUARD = threading.Lock()


@dataclass(frozen=True)
class MicroVmConfig:
    qemu_binary: str
    kernel_path: str
    rootfs_image: str
    cache_root: Path
    require_kvm: bool
    kvm_device: str
    memory_mb: int
    cpu_count: int
    boot_timeout_seconds: int


@dataclass(frozen=True)
class MicroVmPreflight:
    ready: bool
    status: str
    failure_code: str | None = None
    failure_reason: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MicroVmOperationResult:
    run_id: str
    status: str
    exit_code: int | None
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = 0
    error: str | None = None
    output_truncated: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    operation_metadata: dict[str, Any] = field(default_factory=dict)


class PatchApplyDiagnosticError(ValueError):
    def __init__(self, reason: str, **details: object) -> None:
        self.reason = reason
        self.details = {key: short_patch_text(value) for key, value in details.items()}
        super().__init__(_format_patch_apply_error(reason, self.details))


def short_patch_text(value: object, limit: int = 80) -> str:
    text = str(value).replace("\n", "\\n")
    return text if len(text) <= limit else f"{text[:limit]}..."


def _format_patch_apply_error(reason: str, details: dict[str, object]) -> str:
    suffix = ""
    if details:
        suffix = ":" + ",".join(f"{key}={short_patch_text(value)!r}" for key, value in sorted(details.items()))
    return f"patch_apply_failed:{reason}{suffix}"


def patch_error(reason: str, **details: object) -> None:
    raise PatchApplyDiagnosticError(reason, **details)


def patch_failure_payload(exc: Exception) -> dict[str, object]:
    if isinstance(exc, PatchApplyDiagnosticError):
        return {
            "reason": exc.reason,
            "details": exc.details,
            "message": str(exc),
        }
    raw = str(exc)
    if raw in {
        "checksum_mismatch",
        "path_not_allowed",
        "symlink_escape_attempt",
    }:
        return {
            "reason": raw,
            "details": {},
            "message": raw,
        }
    if raw.startswith("patch_apply_failed:"):
        parts = raw.split(":", 2)
        return {
            "reason": parts[1] if len(parts) > 1 and parts[1] else "unknown",
            "details": {},
            "message": raw[:500],
        }
    return {
        "reason": "unknown",
        "details": {},
        "message": raw[:500],
    }


def apply_diff(original: str, diff: str, path: str) -> str:
    diff_lines = diff.splitlines()
    if not any(line.startswith("--- ") for line in diff_lines) or not any(line.startswith("+++ ") for line in diff_lines):
        patch_error("missing_file_headers", path=path)
    original_lines = original.splitlines()
    output = []
    old_index = 0
    index = 0
    saw_hunk = False
    while index < len(diff_lines):
        line = diff_lines[index]
        match = HUNK_RE.match(line)
        if match is None:
            index += 1
            continue
        saw_hunk = True
        hunk_index = sum(1 for item in diff_lines[:index] if item.startswith("@@ ")) + 1
        hunk_old_index = max(int(match.group("old_start")) - 1, 0)
        if hunk_old_index < old_index:
            patch_error("overlapping_hunk", path=path, hunk_index=hunk_index, old_line=old_index + 1)
        output.extend(original_lines[old_index:hunk_old_index])
        old_index = hunk_old_index
        index += 1
        while index < len(diff_lines) and not diff_lines[index].startswith("@@ "):
            hunk_line = diff_lines[index]
            if hunk_line.startswith("\\"):
                index += 1
                continue
            if not hunk_line:
                patch_error("empty_diff_line", path=path, diff_line=index + 1)
            prefix = hunk_line[0]
            content = hunk_line[1:]
            if prefix == " ":
                if old_index >= len(original_lines) or original_lines[old_index] != content:
                    actual = original_lines[old_index] if old_index < len(original_lines) else "<eof>"
                    patch_error(
                        "context_mismatch",
                        path=path,
                        hunk_index=hunk_index,
                        old_line=old_index + 1,
                        expected=content,
                        expected_snippet=content,
                        actual=actual,
                        actual_snippet=actual,
                    )
                output.append(original_lines[old_index])
                old_index += 1
            elif prefix == "-":
                if old_index >= len(original_lines) or original_lines[old_index] != content:
                    actual = original_lines[old_index] if old_index < len(original_lines) else "<eof>"
                    patch_error(
                        "removal_mismatch",
                        path=path,
                        hunk_index=hunk_index,
                        old_line=old_index + 1,
                        expected=content,
                        expected_snippet=content,
                        actual=actual,
                        actual_snippet=actual,
                    )
                old_index += 1
            elif prefix == "+":
                output.append(content)
            else:
                patch_error("invalid_diff_line_prefix", path=path, diff_line=index + 1, prefix=prefix)
            index += 1
    if not saw_hunk:
        patch_error("missing_hunk", path=path)
    output.extend(original_lines[old_index:])
    text = "\n".join(output)
    return f"{text}\n" if original.endswith("\n") or diff.endswith("\n") else text


def _runner_patch_helpers_source() -> str:
    helpers = [
        inspect.getsource(PatchApplyDiagnosticError),
        f"HUNK_RE = re.compile({HUNK_RE.pattern!r})",
        inspect.getsource(short_patch_text),
        inspect.getsource(_format_patch_apply_error),
        inspect.getsource(patch_error),
        inspect.getsource(patch_failure_payload),
        inspect.getsource(apply_diff),
    ]
    return "\n\n".join(textwrap.dedent(source).strip() for source in helpers) + "\n\n"


class QemuMicroVmBackend:
    """Run workspace operations inside a short-lived QEMU microVM.

    The backend uses a trusted rootfs image as an initramfs base. The generated
    project is serialized into the VM as data, the VM runs with network
    disabled, and results are returned through the serial console as a tarball.
    """

    def __init__(self, config: MicroVmConfig) -> None:
        self.config = config

    @classmethod
    def from_settings(
        cls,
        *,
        qemu_binary: str,
        kernel_path: str,
        rootfs_image: str,
        cache_root: Path,
        require_kvm: bool,
        kvm_device: str,
        memory_limit: str,
        cpu_limit: float,
        boot_timeout_seconds: int,
    ) -> "QemuMicroVmBackend":
        return cls(
            MicroVmConfig(
                qemu_binary=qemu_binary or "qemu-system-x86_64",
                kernel_path=kernel_path,
                rootfs_image=rootfs_image,
                cache_root=cache_root,
                require_kvm=require_kvm,
                kvm_device=kvm_device or "/dev/kvm",
                memory_mb=_memory_to_mb(memory_limit),
                cpu_count=max(1, int(cpu_limit)),
                boot_timeout_seconds=boot_timeout_seconds,
            )
        )

    def preflight(self, *, verify_rootfs_image: bool = True) -> MicroVmPreflight:
        qemu = shutil.which(self.config.qemu_binary) if "/" not in self.config.qemu_binary else self.config.qemu_binary
        if not qemu or not Path(qemu).exists():
            return MicroVmPreflight(
                ready=False,
                status="unavailable",
                failure_code="vm_qemu_unavailable",
                failure_reason="qemu-system-x86_64 is not available",
                evidence={"qemu_binary": self.config.qemu_binary},
            )
        kernel_path = self.kernel_path()
        if kernel_path is None:
            return MicroVmPreflight(
                ready=False,
                status="unavailable",
                failure_code="vm_kernel_unavailable",
                failure_reason="no readable VM kernel path was found",
                evidence={"configured_kernel_path": self.config.kernel_path},
            )
        kvm_ready = self.kvm_ready()
        if self.config.require_kvm and not kvm_ready:
            return MicroVmPreflight(
                ready=False,
                status="unavailable",
                failure_code="vm_kvm_unavailable",
                failure_reason="KVM is required but the configured KVM device is not available",
                evidence={"kvm_device": self.config.kvm_device},
            )
        if verify_rootfs_image:
            image_status = self._docker_image_status()
            if image_status is not None:
                return image_status
        return MicroVmPreflight(
            ready=True,
            status="ready",
            evidence={
                "qemu_binary": str(qemu),
                "kernel_path": str(kernel_path),
                "rootfs_image": self.config.rootfs_image,
                "kvm_device": self.config.kvm_device,
                "kvm_ready": kvm_ready,
                "network_mode": "none",
                "serial_result_transport": True,
            },
        )

    def kernel_path(self) -> Path | None:
        if self.config.kernel_path:
            path = Path(self.config.kernel_path)
            return path if path.exists() and path.is_file() else None
        release = platform.uname().release
        candidates = [
            Path("/host-boot") / f"vmlinuz-{release}",
            Path("/boot") / f"vmlinuz-{release}",
        ]
        for candidate in candidates:
            if candidate.exists() and candidate.is_file():
                return candidate
        for boot_dir in (Path("/host-boot"), Path("/boot")):
            if not boot_dir.exists():
                continue
            matches = sorted(boot_dir.glob("vmlinuz-*"), key=lambda item: item.stat().st_mtime, reverse=True)
            if matches:
                return matches[0]
        return None

    def kvm_ready(self) -> bool:
        try:
            info = os.stat(self.config.kvm_device)
        except OSError:
            return False
        return stat.S_ISCHR(info.st_mode) and os.major(info.st_rdev) == KVM_MAJOR and os.minor(info.st_rdev) == KVM_MINOR

    def missing_tools(self, tools: tuple[str, ...], *, limits: RunnerLimits) -> tuple[str, ...]:
        if not tools:
            return ()
        script = (
            "import importlib.util, shutil, sys\n"
            f"tools = {list(tools)!r}\n"
            "missing = []\n"
            "for tool in tools:\n"
            "    if tool == 'python':\n"
            "        ok = shutil.which('python') or shutil.which('python3')\n"
            "    elif tool == 'pytest':\n"
            "        ok = shutil.which('pytest') or importlib.util.find_spec('pytest') is not None\n"
            "    else:\n"
            "        ok = shutil.which(tool)\n"
            "    if not ok:\n"
            "        missing.append(tool)\n"
            "print('missing_tools:' + ','.join(missing))\n"
            "sys.exit(0)\n"
        )
        result = self.run_command(
            argv=["python", "-c", script],
            cwd=Path("."),
            workspace_path=_empty_workspace(),
            artifacts_path=_empty_workspace(),
            env={},
            limits=limits,
            redaction_terms=[],
        )
        if result.status not in {"completed", "failed"}:
            return tools
        for line in result.stdout.splitlines():
            if line.startswith("missing_tools:"):
                return tuple(item for item in line.removeprefix("missing_tools:").split(",") if item)
        return tools

    def tool_cache_key(self, tools: tuple[str, ...]) -> str:
        image_id = ""
        with contextlib.suppress(Exception):
            image_id = self._docker_image_id()
        kernel = self.kernel_path()
        payload = {
            "backend": "microvm",
            "rootfs_image": self.config.rootfs_image,
            "image_id": image_id,
            "kernel_path": str(kernel or ""),
            "tools": sorted(set(tools)),
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def warm_rootfs_cache(self) -> dict[str, Any]:
        started = time.time()
        preflight = self.preflight(verify_rootfs_image=True)
        if not preflight.ready:
            return {
                "status": "blocked",
                "duration_ms": int((time.time() - started) * 1000),
                "error_code": preflight.failure_code,
                "error": preflight.failure_reason,
                **preflight.evidence,
            }
        try:
            rootfs = self._ensure_rootfs()
            return {
                "status": "completed",
                "duration_ms": int((time.time() - started) * 1000),
                "rootfs_path": str(rootfs),
                **self._metadata(preflight.evidence),
            }
        except Exception as exc:
            return {
                "status": "failed",
                "duration_ms": int((time.time() - started) * 1000),
                "error_code": "microvm_rootfs_prewarm_failed",
                "error": str(exc)[:500],
                **self._metadata(preflight.evidence),
            }

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
    ) -> MicroVmOperationResult:
        return self._run_operation(
            workspace_path=workspace_path,
            artifacts_path=artifacts_path,
            config={
                "operation": "command",
                "argv": argv,
                "cwd": str(cwd),
                "env": _vm_env(env),
                "timeout_seconds": limits.timeout_seconds,
                "run_as_uid": 10001,
                "run_as_gid": 10001,
            },
            timeout_seconds=limits.timeout_seconds + self.config.boot_timeout_seconds,
            max_output_bytes=limits.max_output_bytes,
            redaction_terms=redaction_terms,
        )

    def file_batch(
        self,
        *,
        workspace_path: Path,
        artifacts_path: Path,
        root: str,
        files: list[dict[str, str]],
        verify_hashes: bool,
        forbid_symlink_escape: bool,
        timeout_seconds: int,
        max_output_bytes: int,
        redaction_terms: list[str],
    ) -> MicroVmOperationResult:
        return self._run_operation(
            workspace_path=workspace_path,
            artifacts_path=artifacts_path,
            config={
                "operation": "file_batch",
                "root": root,
                "files": files,
                "verify_hashes": verify_hashes,
                "forbid_symlink_escape": forbid_symlink_escape,
            },
            timeout_seconds=timeout_seconds + self.config.boot_timeout_seconds,
            max_output_bytes=max_output_bytes,
            redaction_terms=redaction_terms,
        )

    def patch_apply(
        self,
        *,
        workspace_path: Path,
        artifacts_path: Path,
        patches: list[dict[str, str | None]],
        verify: bool,
        forbid_symlink_escape: bool,
        timeout_seconds: int,
        max_output_bytes: int,
        redaction_terms: list[str],
    ) -> MicroVmOperationResult:
        return self._run_operation(
            workspace_path=workspace_path,
            artifacts_path=artifacts_path,
            config={
                "operation": "patch_apply",
                "patches": patches,
                "verify": verify,
                "forbid_symlink_escape": forbid_symlink_escape,
            },
            timeout_seconds=timeout_seconds + self.config.boot_timeout_seconds,
            max_output_bytes=max_output_bytes,
            redaction_terms=redaction_terms,
        )

    def package_artifact(
        self,
        *,
        workspace_path: Path,
        artifacts_path: Path,
        root: str,
        forbid_symlink_escape: bool,
        timeout_seconds: int,
        max_output_bytes: int,
        redaction_terms: list[str],
    ) -> MicroVmOperationResult:
        return self._run_operation(
            workspace_path=workspace_path,
            artifacts_path=artifacts_path,
            config={
                "operation": "package_artifact",
                "root": root,
                "forbid_symlink_escape": forbid_symlink_escape,
            },
            timeout_seconds=timeout_seconds + self.config.boot_timeout_seconds,
            max_output_bytes=max_output_bytes,
            redaction_terms=redaction_terms,
        )

    def _run_operation(
        self,
        *,
        workspace_path: Path,
        artifacts_path: Path,
        config: dict[str, Any],
        timeout_seconds: int,
        max_output_bytes: int,
        redaction_terms: list[str],
    ) -> MicroVmOperationResult:
        run_id = f"run:{uuid.uuid4().hex}"
        started = time.time()
        preflight = self.preflight(verify_rootfs_image=True)
        if not preflight.ready:
            return MicroVmOperationResult(
                run_id=run_id,
                status="blocked",
                exit_code=None,
                duration_ms=int((time.time() - started) * 1000),
                error=preflight.failure_code,
                metadata={"backend": "microvm", **preflight.evidence},
            )
        try:
            rootfs = self._ensure_rootfs()
            with tempfile.TemporaryDirectory(prefix="workspace-microvm-", dir=str(self.config.cache_root)) as raw_tmp:
                tmp = Path(raw_tmp)
                initramfs = tmp / "initramfs.cpio.gz"
                _build_initramfs(
                    rootfs=rootfs,
                    workspace_path=workspace_path,
                    config=config,
                    initramfs_path=initramfs,
                )
                completed = self._boot(initramfs=initramfs, timeout_seconds=timeout_seconds)
            duration_ms = int((time.time() - started) * 1000)
            if completed.timed_out:
                return MicroVmOperationResult(
                    run_id=run_id,
                    status="timed_out",
                    exit_code=None,
                    stdout=completed.stdout,
                    stderr=completed.stderr,
                    duration_ms=duration_ms,
                    error=f"timeout:{timeout_seconds}s",
                    metadata=self._metadata(preflight.evidence),
                )
            result_tar = _extract_result_tar(completed.stdout)
            meta = _restore_result(
                result_tar,
                workspace_path=workspace_path,
                artifacts_path=artifacts_path,
            )
            stdout, stdout_truncated = _redact_and_truncate(
                str(meta.get("stdout") or ""),
                max_output_bytes,
                redaction_terms,
            )
            stderr, stderr_truncated = _redact_and_truncate(
                str(meta.get("stderr") or ""),
                max_output_bytes,
                redaction_terms,
            )
            exit_code = meta.get("exit_code")
            status = str(meta.get("status") or ("completed" if exit_code == 0 else "failed"))
            return MicroVmOperationResult(
                run_id=run_id,
                status=status,
                exit_code=int(exit_code) if isinstance(exit_code, int) else None,
                stdout=stdout,
                stderr=stderr,
                duration_ms=duration_ms,
                error=None if status == "completed" else str(meta.get("error") or f"exit_code:{exit_code}"),
                output_truncated=stdout_truncated or stderr_truncated,
                metadata=self._metadata(preflight.evidence),
                operation_metadata=dict(meta.get("operation_metadata") or {}),
            )
        except Exception as exc:
            duration_ms = int((time.time() - started) * 1000)
            return MicroVmOperationResult(
                run_id=run_id,
                status="failed",
                exit_code=None,
                duration_ms=duration_ms,
                error=str(exc)[:500],
                metadata={**self._metadata({}), "error_code": "microvm_operation_failed"},
            )

    def _boot(self, *, initramfs: Path, timeout_seconds: int) -> _BootResult:
        qemu = shutil.which(self.config.qemu_binary) or self.config.qemu_binary
        kernel = self.kernel_path()
        if kernel is None:
            raise WorkspaceExecutionError("vm_kernel_unavailable", "no VM kernel path is available")
        command = [
            qemu,
            "-m",
            f"{self.config.memory_mb}M",
            "-smp",
            str(self.config.cpu_count),
            "-nographic",
            "-nodefaults",
            "-no-reboot",
            "-serial",
            "stdio",
            "-net",
            "none",
            "-kernel",
            str(kernel),
            "-initrd",
            str(initramfs),
            "-append",
            "console=ttyS0,115200 rdinit=/init panic=-1 quiet",
        ]
        if self.kvm_ready():
            command[1:1] = ["-enable-kvm", "-cpu", "host"]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
            return _BootResult(stdout=completed.stdout, stderr=completed.stderr, timed_out=False)
        except subprocess.TimeoutExpired as exc:
            return _BootResult(
                stdout=str(exc.stdout or ""),
                stderr=str(exc.stderr or "timeout"),
                timed_out=True,
            )

    def _ensure_rootfs(self) -> Path:
        self.config.cache_root.mkdir(parents=True, exist_ok=True)
        rootfs_key = hashlib.sha256(self.config.rootfs_image.encode("utf-8")).hexdigest()[:16]
        rootfs = self.config.cache_root / f"rootfs-{rootfs_key}"
        lock = _rootfs_lock(rootfs)
        with lock:
            marker = rootfs / ROOTFS_MARKER
            image_id = self._docker_image_id()
            marker_payload = {"image": self.config.rootfs_image, "image_id": image_id}
            if marker.exists():
                try:
                    if json.loads(marker.read_text(encoding="utf-8")) == marker_payload:
                        return rootfs
                except (json.JSONDecodeError, OSError):
                    pass
            shutil.rmtree(rootfs, ignore_errors=True)
            rootfs.mkdir(parents=True, exist_ok=True)
            self._export_rootfs(rootfs)
            marker.write_text(json.dumps(marker_payload, sort_keys=True), encoding="utf-8")
            return rootfs

    def _docker_image_id(self) -> str:
        import docker

        client = docker.from_env(timeout=30)
        image = client.images.get(self.config.rootfs_image)
        return str(image.attrs.get("Id") or "")

    def _docker_image_status(self) -> MicroVmPreflight | None:
        try:
            image_id = self._docker_image_id()
        except Exception as exc:
            return MicroVmPreflight(
                ready=False,
                status="unavailable",
                failure_code="vm_rootfs_image_unavailable",
                failure_reason=f"trusted VM rootfs image is unavailable: {str(exc)[:500]}",
                evidence={"rootfs_image": self.config.rootfs_image},
            )
        if not image_id:
            return MicroVmPreflight(
                ready=False,
                status="unavailable",
                failure_code="vm_rootfs_image_unavailable",
                failure_reason="trusted VM rootfs image has no image id",
                evidence={"rootfs_image": self.config.rootfs_image},
            )
        return None

    def _export_rootfs(self, rootfs: Path) -> None:
        import docker

        client = docker.from_env(timeout=120)
        container = client.containers.create(self.config.rootfs_image, command=["/bin/true"])
        try:
            chunks = container.export()
            data = b"".join(chunks)
        finally:
            with contextlib.suppress(Exception):
                container.remove(force=True)
        _safe_extract_rootfs(data, rootfs)

    def _metadata(self, evidence: dict[str, Any]) -> dict[str, Any]:
        return {
            "backend": "microvm",
            "vm_backed": True,
            "host_execution_used": False,
            "host_docker_socket_exposed": False,
            "network": "disabled",
            "rootfs_image": self.config.rootfs_image,
            **evidence,
        }


@dataclass(frozen=True)
class _BootResult:
    stdout: str
    stderr: str
    timed_out: bool = False


def _rootfs_lock(rootfs: Path) -> threading.Lock:
    key = str(rootfs)
    with _ROOTFS_LOCKS_GUARD:
        lock = _ROOTFS_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _ROOTFS_LOCKS[key] = lock
        return lock


def _empty_workspace() -> Path:
    path = Path(tempfile.mkdtemp(prefix="workspace-microvm-empty-"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def _vm_env(env: dict[str, str]) -> dict[str, str]:
    scrubbed, _removed = scrub_command_env(env, generated_project=True)
    allowed = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LC_ALL": "C.UTF-8",
        "LANG": "C.UTF-8",
        "HOME": "/tmp",
        "WORKSPACE_ARTIFACTS_DIR": "/artifacts",
    }
    for key, value in scrubbed.items():
        if key not in {"PATH", "LC_ALL", "LANG", "HOME", "WORKSPACE_ARTIFACTS_DIR"}:
            allowed[key] = value
    return allowed


def _memory_to_mb(value: str) -> int:
    raw = str(value or "2048m").strip().lower()
    try:
        if raw.endswith("g"):
            return max(512, int(float(raw[:-1]) * 1024))
        if raw.endswith("m"):
            return max(512, int(float(raw[:-1])))
        return max(512, int(raw))
    except ValueError:
        return 2048


def _build_initramfs(
    *,
    rootfs: Path,
    workspace_path: Path,
    config: dict[str, Any],
    initramfs_path: Path,
) -> None:
    overlay = {
        "init": _init_script().encode("utf-8"),
        "ai_local_vm_runner.py": _runner_script().encode("utf-8"),
        "ai_local_vm_config.json": json.dumps(config, sort_keys=True).encode("utf-8"),
    }
    stream = io.BytesIO()
    with gzip.GzipFile(fileobj=stream, mode="wb", compresslevel=1) as gz:
        writer = _CpioWriter(gz)
        writer.add_tree(rootfs)
        writer.add_bytes("init", overlay["init"], mode=0o100755)
        writer.add_bytes("ai_local_vm_runner.py", overlay["ai_local_vm_runner.py"], mode=0o100644)
        writer.add_bytes("ai_local_vm_config.json", overlay["ai_local_vm_config.json"], mode=0o100600)
        writer.add_directory("workspace")
        writer.add_directory("workspace/project")
        writer.add_tree(workspace_path, prefix="workspace/project")
        writer.add_directory("artifacts", mode=0o40755)
        writer.close()
    initramfs_path.write_bytes(stream.getvalue())


def _init_script() -> str:
    return textwrap.dedent(
        """\
        #!/bin/sh
        mount -t devtmpfs devtmpfs /dev 2>/dev/null || true
        mount -t proc proc /proc 2>/dev/null || true
        mount -t sysfs sysfs /sys 2>/dev/null || true
        exec >/dev/console 2>&1
        /usr/sbin/ip link set lo up 2>/dev/null || /sbin/ip link set lo up 2>/dev/null || true
        mkdir -p /workspace/project /artifacts /run/ws /tmp
        /usr/local/bin/python -u /ai_local_vm_runner.py
        sleep 1
        poweroff -f 2>/dev/null || exit 0
        """
    )


def _runner_script() -> str:
    return r'''
import base64
import hashlib
import io
import json
import os
import pathlib
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import time

RESULT_BEGIN = "__AI_LOCAL_VM_RESULT_BEGIN__"
RESULT_END = "__AI_LOCAL_VM_RESULT_END__"
WORKSPACE = pathlib.Path("/workspace/project")
ARTIFACTS = pathlib.Path("/artifacts")
EXCLUDED_ARTIFACT_DIR_NAMES = {
    ".cache",
    ".git",
    ".hg",
    ".local",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "venv",
}
EXCLUDED_ARTIFACT_FILE_SUFFIXES = {".pyc", ".pyo"}


def safe_relative(raw, default="."):
    value = raw if raw and str(raw).strip() else default
    normalized = str(value).replace("\\", "/")
    if "\x00" in normalized or normalized.startswith(("/", "~")):
        raise ValueError("path_not_allowed")
    path = pathlib.PurePosixPath(normalized)
    if ".." in path.parts or (len(normalized) >= 2 and normalized[1] == ":"):
        raise ValueError("path_not_allowed")
    return "." if str(path) in {"", "."} else str(path)


def safe_child(base, raw, default="."):
    rel = safe_relative(raw, default=default)
    candidate = (base / rel).resolve()
    root = base.resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError("path_not_allowed")
    return candidate


def sha256(path):
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def excluded_artifact_member(relative):
    return (
        any(part in EXCLUDED_ARTIFACT_DIR_NAMES for part in relative.parts)
        or relative.suffix in EXCLUDED_ARTIFACT_FILE_SUFFIXES
    )


def reject_symlink(base, target):
    root = base.resolve()
    current = target
    while current != root and root in current.parents:
        if current.is_symlink():
            raise ValueError("symlink_escape_attempt")
        current = current.parent


def write_file(root, item, verify_hashes, forbid_symlink_escape):
    target = safe_child(root, item["path"])
    if target.exists() and not target.is_file():
        raise ValueError("path_not_allowed")
    if forbid_symlink_escape:
        reject_symlink(root, target)
    content = base64.b64decode(item["content_b64"].encode("ascii"), validate=True)
    actual = hashlib.sha256(content).hexdigest()
    if verify_hashes and actual != str(item["sha256"]).removeprefix("sha256:"):
        raise ValueError("checksum_mismatch")
    before = sha256(target) if target.exists() else None
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    if forbid_symlink_escape:
        reject_symlink(root, target)
    return {"path": item["path"], "before_sha256": before, "sha256": sha256(target), "size_bytes": target.stat().st_size}


''' + _runner_patch_helpers_source() + r'''
def apply_patch(item, verify, forbid_symlink_escape):
    target = safe_child(WORKSPACE, item["path"])
    if forbid_symlink_escape:
        reject_symlink(WORKSPACE, target)
    before = sha256(target) if target.exists() else None
    expected = item.get("expected_old_sha256")
    if verify and expected is not None and before != str(expected).removeprefix("sha256:"):
        raise ValueError("checksum_mismatch")
    original = target.read_text(encoding="utf-8") if target.exists() else ""
    after = apply_diff(original, item["unified_diff"], item["path"])
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(after, encoding="utf-8")
    if forbid_symlink_escape:
        reject_symlink(WORKSPACE, target)
    return {"path": item["path"], "before_sha256": before, "after_sha256": sha256(target), "applied": True}


def package_artifact(root, forbid_symlink_escape):
    package_root = safe_child(WORKSPACE, root)
    if not package_root.exists() or not package_root.is_dir():
        raise ValueError("artifact_root_missing")
    if forbid_symlink_escape:
        for path in package_root.rglob("*"):
            if path.is_symlink():
                raise ValueError("symlink_escape_attempt")
    artifact = ARTIFACTS / f"{package_root.name}.tar.gz"
    with tarfile.open(artifact, "w:gz") as archive:
        archive.add(package_root, arcname=package_root.name, recursive=False)
        for path in sorted(package_root.rglob("*")):
            relative = path.relative_to(package_root)
            if excluded_artifact_member(relative):
                continue
            archive.add(path, arcname=str(pathlib.Path(package_root.name) / relative), recursive=False)
    return {"artifact_path": artifact.name, "sha256": sha256(artifact), "size_bytes": artifact.stat().st_size}


def prepare_runner_writable(uid, gid):
    for base in (WORKSPACE, ARTIFACTS):
        paths = [base]
        paths.extend(sorted(base.rglob("*")))
        for path in paths:
            if path.is_symlink():
                continue
            try:
                os.chown(path, uid, gid)
                current_mode = path.stat().st_mode
                if path.is_dir():
                    path.chmod(current_mode | stat.S_IRWXU)
                elif path.is_file():
                    path.chmod(current_mode | stat.S_IRUSR | stat.S_IWUSR)
            except FileNotFoundError:
                continue


def run_operation(config):
    op = config["operation"]
    if op == "command":
        env = dict(config.get("env") or {})
        env["WORKSPACE_ARTIFACTS_DIR"] = "/artifacts"
        cwd = safe_child(WORKSPACE, config.get("cwd") or ".")
        run_uid = int(config.get("run_as_uid") or 10001)
        run_gid = int(config.get("run_as_gid") or 10001)
        prepare_runner_writable(run_uid, run_gid)
        started = time.time()
        try:
            completed = subprocess.run(
                config["argv"],
                cwd=cwd,
                env=env,
                capture_output=True,
                text=True,
                timeout=int(config.get("timeout_seconds") or 120),
                check=False,
                preexec_fn=lambda: (os.setgid(run_gid), os.setuid(run_uid)),
            )
            return {
                "status": "completed" if completed.returncode == 0 else "failed",
                "exit_code": completed.returncode,
                "stdout": completed.stdout,
                "stderr": completed.stderr,
                "duration_ms": int((time.time() - started) * 1000),
                "operation_metadata": {},
            }
        except subprocess.TimeoutExpired as exc:
            return {
                "status": "timed_out",
                "exit_code": None,
                "stdout": str(exc.stdout or ""),
                "stderr": str(exc.stderr or ""),
                "error": "timeout",
                "duration_ms": int((time.time() - started) * 1000),
                "operation_metadata": {},
            }
    if op == "file_batch":
        root = safe_child(WORKSPACE, config.get("root") or ".")
        root.mkdir(parents=True, exist_ok=True)
        files = [write_file(root, item, bool(config.get("verify_hashes", True)), bool(config.get("forbid_symlink_escape", True))) for item in config.get("files", [])]
        return {"status": "completed", "exit_code": 0, "stdout": "", "stderr": "", "operation_metadata": {"files": files}}
    if op == "patch_apply":
        try:
            patches = [apply_patch(item, bool(config.get("verify", True)), bool(config.get("forbid_symlink_escape", True))) for item in config.get("patches", [])]
            return {"status": "completed", "exit_code": 0, "stdout": "", "stderr": "", "operation_metadata": {"patches": patches}}
        except Exception as exc:
            return {
                "status": "failed",
                "exit_code": None,
                "stdout": "",
                "stderr": "",
                "error": str(exc),
                "operation_metadata": {"patch_error": patch_failure_payload(exc)},
            }
    if op == "package_artifact":
        artifact = package_artifact(config["root"], bool(config.get("forbid_symlink_escape", True)))
        return {"status": "completed", "exit_code": 0, "stdout": "", "stderr": "", "operation_metadata": {"artifact": artifact}}
    raise ValueError(f"unsupported_operation:{op}")


def emit_result(meta):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as archive:
        for root in (WORKSPACE, ARTIFACTS):
            for path in sorted(root.rglob("*")):
                if path.is_symlink():
                    continue
                if path.is_file() or path.is_dir():
                    archive.add(path, arcname=str(path.relative_to("/")), recursive=False)
        payload = json.dumps(meta).encode("utf-8")
        info = tarfile.TarInfo("run/meta.json")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    print(RESULT_BEGIN)
    sys.stdout.write(base64.b64encode(buf.getvalue()).decode("ascii"))
    print()
    print(RESULT_END)
    sys.stdout.flush()


def main():
    try:
        config = json.loads(pathlib.Path("/ai_local_vm_config.json").read_text(encoding="utf-8"))
        meta = run_operation(config)
    except Exception as exc:
        meta = {"status": "failed", "exit_code": None, "stdout": "", "stderr": "", "error": str(exc), "operation_metadata": {}}
    emit_result(meta)


main()
'''


class _CpioWriter:
    def __init__(self, output: gzip.GzipFile) -> None:
        self.output = output
        self.inodes = 1
        self.added: set[str] = set()

    def add_tree(self, root: Path, *, prefix: str = "") -> None:
        root = root.resolve()
        for path in sorted(root.rglob("*")):
            rel = path.relative_to(root).as_posix()
            if rel == ROOTFS_MARKER:
                continue
            name = f"{prefix.rstrip('/')}/{rel}" if prefix else rel
            if path.is_symlink():
                self.add_symlink(name, os.readlink(path), mode=0o120777)
            elif path.is_dir():
                self.add_directory(name, mode=0o40755)
            elif path.is_file():
                mode = 0o100755 if path.stat().st_mode & 0o111 else 0o100644
                self.add_bytes(name, path.read_bytes(), mode=mode)

    def add_directory(self, name: str, *, mode: int = 0o40755) -> None:
        self._add(name.rstrip("/"), b"", mode=mode, file_type=stat.S_IFDIR)

    def add_symlink(self, name: str, target: str, *, mode: int = 0o120777) -> None:
        self._add(name, target.encode("utf-8"), mode=mode, file_type=stat.S_IFLNK)

    def add_bytes(self, name: str, data: bytes, *, mode: int) -> None:
        self._add(name, data, mode=mode, file_type=stat.S_IFREG)

    def close(self) -> None:
        self._write_header("TRAILER!!!", 0, 0)
        self._pad(0)

    def _add(self, name: str, data: bytes, *, mode: int, file_type: int) -> None:
        clean = name.strip("/")
        if not clean or clean in self.added:
            return
        self.added.add(clean)
        final_mode = mode if mode & 0o170000 else file_type | mode
        self._write_header(clean, final_mode, len(data))
        self.output.write(data)
        self._pad(len(data))

    def _write_header(self, name: str, mode: int, size: int) -> None:
        encoded_name = name.encode("utf-8") + b"\0"
        fields = [
            "070701",
            f"{self.inodes:08x}",
            f"{mode:08x}",
            "00000000",
            "00000000",
            "00000001",
            "00000000",
            f"{size:08x}",
            "00000000",
            "00000000",
            "00000000",
            "00000000",
            f"{len(encoded_name):08x}",
            "00000000",
        ]
        self.inodes += 1
        payload = "".join(fields).encode("ascii") + encoded_name
        self.output.write(payload)
        self._pad(len(payload))

    def _pad(self, size: int) -> None:
        remainder = size % 4
        if remainder:
            self.output.write(b"\0" * (4 - remainder))


def _extract_result_tar(output: str) -> bytes:
    start = output.find(RESULT_BEGIN)
    end = output.find(RESULT_END)
    if start < 0 or end < 0 or end <= start:
        raise WorkspaceExecutionError("vm_result_missing", "microVM did not emit a result envelope")
    payload = "".join(output[start + len(RESULT_BEGIN) : end].split())
    return base64.b64decode(payload.encode("ascii"), validate=True)


def _restore_result(data: bytes, *, workspace_path: Path, artifacts_path: Path) -> dict[str, Any]:
    meta: dict[str, Any] | None = None
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as archive:
        workspace_tmp = workspace_path.parent / f"{workspace_path.name}.vm-next"
        artifacts_tmp = artifacts_path.parent / f"{artifacts_path.name}.vm-next"
        shutil.rmtree(workspace_tmp, ignore_errors=True)
        shutil.rmtree(artifacts_tmp, ignore_errors=True)
        workspace_tmp.mkdir(parents=True, exist_ok=True)
        artifacts_tmp.mkdir(parents=True, exist_ok=True)
        for member in archive.getmembers():
            if member.issym() or member.islnk():
                continue
            if member.name == "run/meta.json":
                source = archive.extractfile(member)
                if source is not None:
                    meta = json.loads(source.read().decode("utf-8"))
                continue
            if member.name.startswith("workspace/project/"):
                _extract_member(archive, member, workspace_tmp, strip="workspace/project")
            elif member.name.startswith("artifacts/"):
                _extract_member(archive, member, artifacts_tmp, strip="artifacts")
        if meta is None:
            raise WorkspaceExecutionError("vm_result_missing", "microVM result metadata is missing")
        shutil.rmtree(workspace_path, ignore_errors=True)
        shutil.move(str(workspace_tmp), str(workspace_path))
        shutil.rmtree(artifacts_path, ignore_errors=True)
        shutil.move(str(artifacts_tmp), str(artifacts_path))
    return meta


def _extract_member(archive: tarfile.TarFile, member: tarfile.TarInfo, target: Path, *, strip: str) -> None:
    rel = Path(member.name).relative_to(strip)
    if str(rel) == ".":
        return
    destination = (target / rel).resolve()
    root = target.resolve()
    if destination != root and root not in destination.parents:
        raise WorkspaceExecutionError("archive_path_not_allowed", "microVM archive path escapes target")
    if member.isdir():
        destination.mkdir(parents=True, exist_ok=True)
        return
    if not member.isfile():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    source = archive.extractfile(member)
    if source is None:
        return
    with source, destination.open("wb") as handle:
        shutil.copyfileobj(source, handle)


def _safe_extract_rootfs(data: bytes, target: Path) -> None:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as archive:
        for member in archive.getmembers():
            if member.isdev() or member.isfifo():
                continue
            destination = (target / member.name.lstrip("/")).resolve()
            root = target.resolve()
            if destination != root and root not in destination.parents:
                continue
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True)
            elif member.issym():
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists() or destination.is_symlink():
                    destination.unlink()
                destination.symlink_to(member.linkname)
            elif member.isfile():
                destination.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    continue
                with source, destination.open("wb") as handle:
                    shutil.copyfileobj(source, handle)
                destination.chmod(member.mode & 0o7777)
