"""Adapter from the command tool ledger to the workspace_execution feature API."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from orchestrator.agentic.tools.command.mounts import DEFAULT_PROFILE, WORKSPACE_GENERATION_PROFILE
from orchestrator.agentic.tools.command.schemas import CommandClassification, CommandContext, CommandResult


class WorkspaceExecutionCommandError(RuntimeError):
    """Raised when the workspace_execution feature cannot satisfy a command call."""


@dataclass(frozen=True)
class WorkspaceExecutionCommandRun:
    result: CommandResult
    metadata: dict[str, Any] = field(default_factory=dict)


class WorkspaceExecutionCommandAdapter:
    def __init__(self, *, timeout_seconds: int, max_output_bytes: int) -> None:
        self.timeout_seconds = max(1, int(timeout_seconds))
        self.max_output_bytes = max(1024, int(max_output_bytes))
        self._client: Any | None = None

    def create_session(
        self,
        *,
        context: CommandContext,
        idempotency_key: str,
        ttl_seconds: int,
        task_id: str | None,
        trace_id: str | None,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        if context.profile not in {DEFAULT_PROFILE, WORKSPACE_GENERATION_PROFILE}:
            raise WorkspaceExecutionCommandError("workspace_execution backend currently supports project_context only")
        response = self._feature_client().invoke_endpoint(
            "workspace_execution",
            method="POST",
            path="/v1/workspace-execution/sessions",
            payload={
                "idempotency_key": idempotency_key,
                "source": {"kind": "workspace", "root_ref": "ai-local", "paths": ["."]},
                "execution_profile": "test",
                "network": "disabled",
                "ttl_seconds": ttl_seconds,
                "metadata": {
                    **metadata,
                    "command_tool_task_id": task_id,
                    "command_tool_trace_id": trace_id,
                    "command_context_profile": context.profile,
                },
            },
            timeout=float(self.timeout_seconds),
            policy_action="workspace.sandbox.create",
        )
        if not response.success:
            raise WorkspaceExecutionCommandError(response.error or "workspace_execution_session_create_failed")
        return response.data

    def run_command(
        self,
        *,
        workspace_session_id: str,
        command: str,
        cwd: str,
        context: CommandContext,
        classification: CommandClassification,
        idempotency_key: str,
        metadata: dict[str, Any] | None = None,
    ) -> WorkspaceExecutionCommandRun:
        workspace_cwd = _workspace_relative_cwd(cwd, context)
        command_metadata = dict(metadata or {})
        response = self._feature_client().invoke_endpoint(
            "workspace_execution",
            method="POST",
            path=f"/v1/workspace-execution/sessions/{workspace_session_id}/commands",
            payload={
                "idempotency_key": idempotency_key,
                "cwd": workspace_cwd,
                "argv": ["/bin/bash", "-lc", _rewrite_command_for_workspace(command, context)],
                "timeout_seconds": self.timeout_seconds,
                "allow_profile": "inspect" if classification.risk_level == "low" else "test",
                "risk_evidence_ref": command_risk_evidence_ref(classification),
                "validation_profile": command_metadata.get("validation_profile"),
                "metadata": {
                    **command_metadata,
                    "command_tool_action": classification.action,
                    "command_tool_risk_level": classification.risk_level,
                    "command_tool_cwd": cwd,
                    "command_tool_risk_provider": classification.metadata.get("provider"),
                },
            },
            timeout=float(self.timeout_seconds + 5),
            policy_action="workspace.sandbox.execute",
        )
        if not response.success:
            raise WorkspaceExecutionCommandError(response.error or "workspace_execution_command_failed")
        data = response.data
        diff_files = self._diff_files(workspace_session_id) if data.get("diff_ref") else []
        metadata = dict(data.get("metadata") or {})
        status = _command_status(str(data.get("status") or "failed"))
        error = _error_message(data.get("error"))
        result = CommandResult(
            command=command,
            cwd=cwd,
            exit_code=data.get("exit_code") if isinstance(data.get("exit_code"), int) else None,
            stdout=str(metadata.get("stdout_preview") or ""),
            stderr=str(metadata.get("stderr_preview") or ""),
            output_truncated=bool(metadata.get("output_truncated")),
            duration_ms=float(data.get("duration_ms") or 0.0),
            status=status,
            error=error if error else (None if status == "completed" else status),
        )
        return WorkspaceExecutionCommandRun(
            result=result,
            metadata={
                "workspace_execution_session_id": workspace_session_id,
                "workspace_execution_run_id": data.get("run_id"),
                "workspace_execution_status": data.get("status"),
                "workspace_execution_exit_code": data.get("exit_code"),
                "workspace_execution_duration_ms": data.get("duration_ms"),
                "workspace_execution_error": data.get("error") if isinstance(data.get("error"), dict) else {},
                "workspace_execution_validation_profile": metadata.get("validation_profile"),
                "workspace_execution_validation_profile_policy": metadata.get("validation_profile_policy"),
                "workspace_execution_stdout_ref": data.get("stdout_ref"),
                "workspace_execution_stderr_ref": data.get("stderr_ref"),
                "workspace_execution_diff_ref": data.get("diff_ref"),
                "workspace_execution_diff_files": diff_files,
                "workspace_execution_artifacts": data.get("artifacts") or [],
            },
        )

    def close_session(self, workspace_session_id: str, *, reason: str, cleanup: bool = True) -> dict[str, Any]:
        response = self._feature_client().invoke_endpoint(
            "workspace_execution",
            method="POST",
            path=f"/v1/workspace-execution/sessions/{workspace_session_id}/close",
            payload={
                "idempotency_key": _idempotency_key("close", workspace_session_id, reason),
                "reason": reason,
                "cleanup": cleanup,
            },
            timeout=float(self.timeout_seconds),
            policy_action="command.session.close",
        )
        if not response.success:
            raise WorkspaceExecutionCommandError(response.error or "workspace_execution_session_close_failed")
        return response.data

    def _diff_files(self, workspace_session_id: str) -> list[dict[str, Any]]:
        response = self._feature_client().invoke_endpoint(
            "workspace_execution",
            method="GET",
            path=f"/v1/workspace-execution/sessions/{workspace_session_id}/diff",
            timeout=float(self.timeout_seconds),
            policy_action="workspace.sandbox.diff",
        )
        if not response.success:
            return []
        files = response.data.get("files") if isinstance(response.data, dict) else None
        return files if isinstance(files, list) else []

    def _feature_client(self) -> Any:
        if self._client is not None:
            return self._client
        from orchestrator.dispatch.feature_client import FeatureClient
        from orchestrator.factory import _build_service_registry

        self._client = FeatureClient(_build_service_registry())
        return self._client


def _idempotency_key(*parts: str) -> str:
    digest = hashlib.sha256(":".join(parts).encode("utf-8")).hexdigest()
    return f"cmd:{digest[:32]}"


def workspace_run_idempotency_key(session_id: str, command: str, cwd: str) -> str:
    return _idempotency_key("run", session_id, cwd, command)


def command_risk_evidence_ref(classification: CommandClassification) -> str:
    payload = ":".join((
        classification.command,
        classification.action,
        classification.risk_level,
        classification.reason,
    ))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"execution_policy:command-risk:{digest[:24]}"


def _command_status(value: str) -> str:
    if value == "timed_out":
        return "timeout"
    if value == "blocked":
        return "denied"
    return value if value in {"completed", "failed", "timeout"} else "failed"


def _error_message(value: object) -> str | None:
    if not isinstance(value, dict) or not value:
        return None
    message = value.get("message") or value.get("code")
    return str(message) if message else None


def _workspace_relative_cwd(cwd: str, context: CommandContext) -> str:
    raw = cwd or context.default_cwd
    if not raw.startswith("/"):
        raw = f"{context.default_cwd.rstrip('/')}/{raw}"
    project_mount = next((mount for mount in context.mounts if mount.label == "PROJECT_ROOT"), None)
    if project_mount is None:
        raise WorkspaceExecutionCommandError("project root mount is required")
    for mount in sorted(context.mounts, key=lambda item: len(item.sandbox_path), reverse=True):
        prefix = mount.sandbox_path.rstrip("/")
        if raw == prefix or raw.startswith(prefix + "/"):
            relative = raw.removeprefix(prefix).lstrip("/")
            source_target = (mount.source_path / relative).resolve()
            try:
                rel = source_target.relative_to(project_mount.source_path.resolve())
            except ValueError as exc:
                raise WorkspaceExecutionCommandError(
                    f"cwd is outside the workspace_execution source root: {cwd}"
                ) from exc
            return rel.as_posix() or "."
    raise WorkspaceExecutionCommandError(f"cwd is outside command context mounts: {cwd}")


def _rewrite_command_for_workspace(command: str, context: CommandContext) -> str:
    rewritten = command
    project_mount = next((mount for mount in context.mounts if mount.label == "PROJECT_ROOT"), None)
    if project_mount is None:
        return rewritten
    for mount in sorted(context.mounts, key=lambda item: len(item.sandbox_path), reverse=True):
        try:
            rel = mount.source_path.resolve().relative_to(project_mount.source_path.resolve()).as_posix()
        except ValueError:
            continue
        replacement = "." if rel == "." else f"./{rel}"
        rewritten = rewritten.replace(mount.sandbox_path, replacement)
    return rewritten
