"""Command risk classification adapter backed by execution_policy_operator."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from orchestrator.agentic.tools.command.schemas import CommandClassification


class ExecutionPolicyCommandRiskError(RuntimeError):
    """Raised when execution_policy_operator cannot classify a command."""


class ExecutionPolicyCommandRiskAdapter:
    """Invoke execution_policy_operator over dispatch for single-command risk evidence."""

    def __init__(self, *, timeout_seconds: int = 5) -> None:
        self.timeout_seconds = max(1, int(timeout_seconds))
        self._client: Any | None = None

    def classify(self, command: str, *, context_profile: str = "project_context") -> CommandClassification:
        response = self._feature_client().invoke_endpoint(
            "execution_policy_operator",
            method="POST",
            path="/v1/bash/command-risk",
            payload={
                "command": command,
                "context_profile": context_profile,
                "metadata": {"caller": "orchestrator.command_tool"},
            },
            timeout=float(self.timeout_seconds),
            policy_action="command.risk.classify",
        )
        if not response.success:
            raise ExecutionPolicyCommandRiskError(
                response.error or "execution_policy_command_risk_unavailable"
            )
        return classification_from_payload(response.data)

    def _feature_client(self) -> Any:
        if self._client is not None:
            return self._client
        from orchestrator.dispatch.feature_client import FeatureClient
        from orchestrator.factory import _build_service_registry

        self._client = FeatureClient(_build_service_registry())
        return self._client


def classify_command(command: str, *, context_profile: str = "project_context") -> CommandClassification:
    return ExecutionPolicyCommandRiskAdapter().classify(command, context_profile=context_profile)


def classification_from_payload(payload: dict[str, Any]) -> CommandClassification:
    if payload.get("success") is False:
        raise ExecutionPolicyCommandRiskError(
            str(payload.get("error") or "execution_policy_command_risk_failed")
        )
    return CommandClassification(
        command=str(payload.get("command") or ""),
        action=str(payload.get("action") or "command.run.deny"),
        risk_level=str(payload.get("risk_level") or "deny"),
        decision_hint=str(payload.get("decision_hint") or "deny"),
        reason=str(payload.get("reason") or "classification_missing_reason"),
        tokens=tuple(str(item) for item in payload.get("tokens") or ()),
        denied_markers=tuple(str(item) for item in payload.get("denied_markers") or ()),
        requires_approval=bool(payload.get("requires_approval")),
        dry_run_required=bool(payload.get("dry_run_required")),
        metadata=dict(payload.get("metadata") or {}),
    )


def with_metadata(classification: CommandClassification, **metadata: object) -> CommandClassification:
    merged = dict(classification.metadata)
    merged.update(metadata)
    return replace(classification, metadata=merged)
