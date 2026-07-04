"""Command tool service with sessions, policy checks and ledger integration."""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict
from typing import Any

from orchestrator.agentic.models import ApprovalStatus, PolicyDecisionKind
from orchestrator.agentic.policy import check_policy
from orchestrator.agentic.store import AgenticStore, get_agentic_store
from orchestrator.agentic.tools.command.classifier import ExecutionPolicyCommandRiskAdapter, with_metadata
from orchestrator.agentic.tools.command.mounts import DEFAULT_PROFILE, WORKSPACE_GENERATION_PROFILE, build_context
from orchestrator.agentic.tools.command.schemas import CommandClassification
from orchestrator.agentic.tools.command.workspace_execution_adapter import (
    WorkspaceExecutionCommandAdapter,
    workspace_run_idempotency_key,
)

_INTERNAL_METADATA_KEYS = {
    "action",
    "approval_id",
    "artifacts",
    "backend",
    "classification",
    "command_run_id",
    "context_profile",
    "diff_ref",
    "mounts",
    "policy_decision",
    "risk_level",
    "run_id",
    "session_id",
    "stderr_ref",
    "stdout_ref",
    "task_id",
    "trace_id",
}
_INTERNAL_METADATA_PREFIXES = ("_", "internal_", "workspace_execution_")
_OUTPUT_PAYLOAD_KEYS = (
    "stdout_ref",
    "stderr_ref",
    "diff_ref",
    "artifacts",
    "stdout_sha256",
    "stderr_sha256",
    "stdout_size_bytes",
    "stderr_size_bytes",
    "output_truncated",
    "redaction_status",
    "output_preview_policy",
    "raw_output_payload_persisted",
)


def _public_session_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    public: dict[str, Any] = {}
    for raw_key, value in (metadata or {}).items():
        key = str(raw_key)
        normalized = key.lower()
        if normalized in _INTERNAL_METADATA_KEYS:
            continue
        if normalized.startswith(_INTERNAL_METADATA_PREFIXES):
            continue
        public[key] = value
    return public


def _public_command_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    public: dict[str, Any] = {}
    raw_profile = (metadata or {}).get("validation_profile")
    if raw_profile:
        public["validation_profile"] = str(raw_profile)
    return public


def _command_output_payload(run: dict[str, Any], result_status: str, exit_code: int | None) -> dict[str, Any]:
    metadata = dict(run.get("metadata") or {})
    payload: dict[str, Any] = {
        "status": result_status,
        "exit_code": exit_code,
    }
    for key in _OUTPUT_PAYLOAD_KEYS:
        if key in metadata:
            payload[key] = metadata[key]
    return payload


class CommandToolService:
    """Governed command capability for agentic investigations."""

    def __init__(self, *, store: AgenticStore | None = None) -> None:
        from orchestrator.config import get_settings

        cfg = get_settings().agentic_runtime
        self.cfg = cfg
        self.store = store or get_agentic_store()

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.cfg.command_tool_enabled,
            "backend": self.cfg.command_tool_backend,
            "workspace_execution_adapter": self.cfg.command_tool_backend == "workspace_execution",
            "sandbox_image": self.cfg.command_tool_sandbox_image,
            "default_context_profile": self.cfg.command_tool_default_context_profile,
            "timeout_seconds": self.cfg.command_tool_timeout_seconds,
            "max_output_bytes": self.cfg.command_tool_max_output_bytes,
            "session_ttl_seconds": self.cfg.command_tool_session_ttl_seconds,
            "max_commands_per_session": self.cfg.command_tool_max_commands_per_session,
            "docker_memory_limit_mb": self.cfg.command_tool_docker_memory_limit_mb,
            "docker_pids_limit": self.cfg.command_tool_docker_pids_limit,
            "allow_user_context_ro": self.cfg.command_tool_allow_user_context_ro,
            "allow_host_context_ro": self.cfg.command_tool_allow_host_context_ro,
            "safe_actions_only": True,
            "command_risk_owner": "execution_policy_operator",
        }

    def classify(self, command: str, *, context_profile: str | None = None) -> dict[str, Any]:
        profile = context_profile or self.cfg.command_tool_default_context_profile
        classification = self._classify_command(command, context_profile=profile)
        return asdict(classification)

    def create_session(
        self,
        *,
        context_profile: str | None = None,
        cwd: str | None = None,
        task_id: str | None = None,
        trace_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._ensure_enabled()
        profile = context_profile or self.cfg.command_tool_default_context_profile or DEFAULT_PROFILE
        if profile == WORKSPACE_GENERATION_PROFILE and self.cfg.command_tool_backend != "workspace_execution":
            raise PermissionError("workspace_generation_requires_workspace_execution")
        context = build_context(
            profile,
            allow_user_context_ro=self.cfg.command_tool_allow_user_context_ro,
            allow_host_context_ro=self.cfg.command_tool_allow_host_context_ro,
        )
        session_metadata = {
            **_public_session_metadata(metadata),
            "backend": self.cfg.command_tool_backend,
            "context_profile": profile,
            "mounts": [
                {
                    "sandbox_path": mount.sandbox_path,
                    "source_path": str(mount.source_path),
                    "read_only": mount.read_only,
                    "label": mount.label,
                    "docker_source_path": str(mount.docker_source_path or mount.source_path),
                }
                for mount in context.mounts
            ],
        }
        workspace_session_id = ""
        workspace_session = self._workspace_execution_adapter().create_session(
            context=context,
            idempotency_key=f"cmdsess:{uuid.uuid4().hex}",
            ttl_seconds=self.cfg.command_tool_session_ttl_seconds,
            task_id=task_id,
            trace_id=trace_id,
            metadata=session_metadata,
        )
        workspace_session_id = str(workspace_session.get("session_id") or "")
        session_metadata.update(
            {
                "workspace_execution_session_id": workspace_session_id,
                "workspace_execution_state_hash": workspace_session.get("state_hash"),
                "workspace_execution_scratch_ref": workspace_session.get("scratch_ref"),
            }
        )
        try:
            session = self.store.create_command_session(
                context_profile=profile,
                cwd=cwd or context.default_cwd,
                task_id=task_id,
                trace_id=trace_id,
                ttl_seconds=self.cfg.command_tool_session_ttl_seconds,
                metadata=session_metadata,
            )
        except Exception:
            if workspace_session_id:
                try:
                    self._workspace_execution_adapter().close_session(
                        workspace_session_id,
                        reason="command_local_session_create_failed",
                    )
                except Exception:
                    pass
            raise
        return {**session, "runs": []}

    def close_session(
        self,
        session_id: str,
        *,
        reason: str = "manual_close",
        cleanup: bool | None = None,
    ) -> dict[str, Any] | None:
        session = self.store.get_command_session(session_id)
        closed = self.store.close_command_session(session_id, reason=reason)
        metadata = dict((session or {}).get("metadata") or {})
        workspace_session_id = metadata.get("workspace_execution_session_id")
        if workspace_session_id:
            cleanup_workspace = cleanup
            if cleanup_workspace is None:
                cleanup_workspace = metadata.get("context_profile") != WORKSPACE_GENERATION_PROFILE
            try:
                self._workspace_execution_adapter().close_session(
                    str(workspace_session_id),
                    reason=reason,
                    cleanup=bool(cleanup_workspace),
                )
            except Exception as exc:
                self.store.record_event(
                    task_id=(session or {}).get("task_id"),
                    trace_id=(session or {}).get("trace_id"),
                    event_type="command.workspace_execution_close_failed",
                    actor="agentic.command",
                    payload={"session_id": session_id, "workspace_execution_session_id": workspace_session_id, "error": str(exc)[:500]},
                )
        return closed

    def run_command(
        self,
        session_id: str,
        *,
        command: str,
        cwd: str | None = None,
        task_id: str | None = None,
        trace_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._ensure_enabled()
        session = self.store.get_command_session(session_id)
        if session is None:
            raise KeyError("command_session_not_found")
        self._ensure_session_open(session)
        profile = str(session.get("context_profile") or DEFAULT_PROFILE)
        if profile == WORKSPACE_GENERATION_PROFILE and self.cfg.command_tool_backend != "workspace_execution":
            raise PermissionError("workspace_generation_requires_workspace_execution")
        effective_cwd = cwd or str(session.get("cwd") or "/workspace/project")
        effective_task_id = task_id or session.get("task_id")
        effective_trace_id = trace_id or session.get("trace_id")
        context = build_context(
            profile,
            allow_user_context_ro=self.cfg.command_tool_allow_user_context_ro,
            allow_host_context_ro=self.cfg.command_tool_allow_host_context_ro,
        )
        classification = with_metadata(
            self._classify_command(command, context_profile=profile),
            context_profile=profile,
            cwd=effective_cwd,
            session_id=session_id,
            **_public_command_metadata(metadata),
        )
        payload = {
            "command": classification.command,
            "cwd": effective_cwd,
            "context_profile": profile,
            "classification": asdict(classification),
        }
        policy = check_policy(classification.action, payload)
        approval_id = None

        if classification.risk_level == "deny" or policy.decision == PolicyDecisionKind.DENY.value:
            return self._record_blocked(
                session_id=session_id,
                classification=classification,
                policy_decision=policy.decision,
                status="denied",
                cwd=effective_cwd,
                task_id=effective_task_id,
                trace_id=effective_trace_id,
                error=classification.reason,
            )

        if classification.risk_level == "high" or policy.decision == PolicyDecisionKind.REQUIRE_APPROVAL.value:
            approval = self._ensure_approval(
                action=classification.action,
                payload=payload,
                risk_level=classification.risk_level,
                task_id=effective_task_id,
            )
            approval_id = str(approval.get("id"))
            return self._record_blocked(
                session_id=session_id,
                classification=classification,
                policy_decision=PolicyDecisionKind.REQUIRE_APPROVAL.value,
                status="waiting_approval",
                cwd=effective_cwd,
                task_id=effective_task_id,
                trace_id=effective_trace_id,
                approval_id=approval_id,
                error="command_requires_approval",
            )

        started = time.time()
        workspace_session_id = str((session.get("metadata") or {}).get("workspace_execution_session_id") or "")
        if not workspace_session_id:
            raise PermissionError("workspace_execution_session_missing")
        workspace_run = self._workspace_execution_adapter().run_command(
            workspace_session_id=workspace_session_id,
            command=classification.command,
            cwd=effective_cwd,
            context=context,
            classification=classification,
            idempotency_key=workspace_run_idempotency_key(session_id, classification.command, effective_cwd),
            metadata=_public_command_metadata(metadata),
        )
        result = workspace_run.result
        backend_metadata = workspace_run.metadata
        finished = started + (result.duration_ms / 1000.0)
        run = self.store.record_command_run(
            session_id=session_id,
            task_id=effective_task_id,
            trace_id=effective_trace_id,
            command=classification.command,
            cwd=effective_cwd,
            context_profile=profile,
            action=classification.action,
            risk_level=classification.risk_level,
            policy_decision=policy.decision,
            status=result.status,
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
            output_truncated=result.output_truncated,
            started_at=started,
            finished_at=finished,
            error=result.error,
            metadata={
                "classification": asdict(classification),
                "backend": self.cfg.command_tool_backend,
                **backend_metadata,
            },
        )
        self.store.record_tool_call(
            tool_name="command.run",
            risk_level=classification.risk_level,
            status=result.status,
            task_id=effective_task_id,
            input_payload=payload,
            output_payload=_command_output_payload(run, result.status, result.exit_code),
            requires_approval=False,
            error=result.error,
            metadata={
                "command_run_id": run.get("id"),
                "session_id": session_id,
                "output": _command_output_payload(run, result.status, result.exit_code),
            },
        )
        return run

    def _record_blocked(
        self,
        *,
        session_id: str,
        classification: CommandClassification,
        policy_decision: str,
        status: str,
        cwd: str,
        task_id: str | None,
        trace_id: str | None,
        approval_id: str | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        run = self.store.record_command_run(
            session_id=session_id,
            task_id=task_id,
            trace_id=trace_id,
            command=classification.command,
            cwd=cwd,
            context_profile=str(classification.metadata.get("context_profile") or DEFAULT_PROFILE),
            action=classification.action,
            risk_level=classification.risk_level,
            policy_decision=policy_decision,
            status=status,
            approval_id=approval_id,
            error=error,
            metadata={"classification": asdict(classification), "backend": self.cfg.command_tool_backend},
        )
        self.store.record_tool_call(
            tool_name="command.run",
            risk_level=classification.risk_level,
            status=status,
            task_id=task_id,
            input_payload={"command": classification.command, "cwd": cwd},
            output_payload={"status": status, "reason": error},
            requires_approval=status == "waiting_approval",
            approval_id=approval_id,
            error=error,
            metadata={"command_run_id": run.get("id"), "session_id": session_id},
        )
        return run

    def _ensure_approval(
        self,
        *,
        action: str,
        payload: dict[str, Any],
        risk_level: str,
        task_id: str | None,
    ) -> dict[str, Any]:
        existing = self.store.find_approval_for_payload(
            action=action,
            payload=payload,
            statuses=(ApprovalStatus.PENDING.value, ApprovalStatus.APPROVED.value),
            task_id=task_id,
        )
        if existing is not None:
            return existing
        return self.store.create_approval(
            action=action,
            risk_level=risk_level,
            payload=payload,
            ttl_seconds=self.cfg.approval_ttl_seconds,
            task_id=task_id,
            dry_run_result={
                "would_execute": False,
                "reason": "command approval required before execution",
            },
            metadata={"component": "agentic.command", "command_session": payload.get("session_id")},
        )

    def _ensure_enabled(self) -> None:
        if not self.cfg.command_tool_enabled:
            raise PermissionError("command_tool_disabled")
        if self.cfg.command_tool_backend != "workspace_execution":
            raise PermissionError(f"unsupported_command_tool_backend:{self.cfg.command_tool_backend}")

    def _workspace_execution_adapter(self) -> WorkspaceExecutionCommandAdapter:
        existing = getattr(self, "_workspace_execution_adapter_instance", None)
        if existing is not None:
            return existing
        adapter = WorkspaceExecutionCommandAdapter(
            timeout_seconds=self.cfg.command_tool_timeout_seconds,
            max_output_bytes=self.cfg.command_tool_max_output_bytes,
        )
        self._workspace_execution_adapter_instance = adapter
        return adapter

    def _command_risk_adapter(self) -> ExecutionPolicyCommandRiskAdapter:
        existing = getattr(self, "_command_risk_adapter_instance", None)
        if existing is not None:
            return existing
        adapter = ExecutionPolicyCommandRiskAdapter(timeout_seconds=self.cfg.command_tool_timeout_seconds)
        self._command_risk_adapter_instance = adapter
        return adapter

    def _classify_command(self, command: str, *, context_profile: str) -> CommandClassification:
        return self._command_risk_adapter().classify(command, context_profile=context_profile)

    def _ensure_session_open(self, session: dict[str, Any]) -> None:
        if session.get("status") != "open":
            raise PermissionError("command_session_closed")
        expires_at = session.get("expires_at")
        if expires_at is not None and float(expires_at) < time.time():
            self.store.close_command_session(str(session["id"]), reason="expired")
            raise PermissionError("command_session_expired")
        count = len(self.store.list_command_runs(session_id=str(session["id"]), limit=500))
        if count >= self.cfg.command_tool_max_commands_per_session:
            self.store.close_command_session(str(session["id"]), reason="max_commands_reached")
            raise PermissionError("command_session_max_commands_reached")


def get_command_tool_status() -> dict[str, Any]:
    return CommandToolService().status()
