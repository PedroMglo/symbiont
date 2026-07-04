"""Explicit policy action registry for agentic governance."""

from __future__ import annotations

import tomllib
from pathlib import Path

from orchestrator.agentic.models import RiskLevel

ROOT = Path(__file__).resolve().parents[2]
POLICY_ACTIONS_PATH = ROOT / "infra" / "security" / "policy-actions.toml"


def _load_policy_actions(path: Path = POLICY_ACTIONS_PATH) -> dict[str, frozenset[str]]:
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    actions = raw.get("policy_actions", {})
    return {
        "low": frozenset(str(action) for action in actions.get("low", ())),
        "medium": frozenset(str(action) for action in actions.get("medium", ())),
        "high": frozenset(str(action) for action in actions.get("high", ())),
        "deny": frozenset(str(action) for action in actions.get("deny", ())),
    }


_ACTION_GROUPS = _load_policy_actions()

LOW_RISK_ACTIONS = _ACTION_GROUPS["low"]
MEDIUM_RISK_ACTIONS = _ACTION_GROUPS["medium"]
HIGH_RISK_ACTIONS = _ACTION_GROUPS["high"]
DENY_ACTIONS = _ACTION_GROUPS["deny"]


def risk_for_action(action: str) -> str:
    if action in DENY_ACTIONS:
        return RiskLevel.DENY.value
    if action in HIGH_RISK_ACTIONS:
        return RiskLevel.HIGH.value
    if action in MEDIUM_RISK_ACTIONS:
        return RiskLevel.MEDIUM.value
    if action in LOW_RISK_ACTIONS:
        return RiskLevel.LOW.value

    if action.startswith("storage."):
        if any(part in action for part in ("delete", "restore", "archive", "promote", "cycle", "mutable")):
            return RiskLevel.HIGH.value
        if any(part in action for part in ("plan", "scan", "status", "read", "search")):
            return RiskLevel.LOW.value
        return RiskLevel.MEDIUM.value
    if action.startswith("extrator."):
        if "delete" in action:
            return RiskLevel.HIGH.value
        if any(part in action for part in ("extract", "conversion", "upload")):
            return RiskLevel.MEDIUM.value
        return RiskLevel.LOW.value
    if action.startswith("docker.") or action.startswith("lifecycle."):
        if any(part in action for part in ("restart", "stop", "remove", "start")):
            return RiskLevel.HIGH.value
        return RiskLevel.LOW.value
    if action.startswith("rag.admin."):
        return RiskLevel.HIGH.value if action.endswith(".all") else RiskLevel.MEDIUM.value
    if action.startswith(("repo.write", "config.write", "git.")):
        return RiskLevel.HIGH.value
    if action.startswith("command."):
        if action.endswith(".deny") or "host" in action:
            return RiskLevel.DENY.value
        if any(part in action for part in ("write", "destructive")):
            return RiskLevel.HIGH.value
        if any(part in action for part in ("medium", "scan", "dry_run")):
            return RiskLevel.MEDIUM.value
        return RiskLevel.LOW.value
    if action.startswith("workspace.sandbox."):
        if action.endswith(".apply_real"):
            return RiskLevel.DENY.value
        if action.endswith(".destructive"):
            return RiskLevel.HIGH.value
        if action.endswith((".create", ".execute", ".publish")):
            return RiskLevel.MEDIUM.value
        if action.endswith(".read"):
            return RiskLevel.LOW.value
        return RiskLevel.MEDIUM.value
    return RiskLevel.MEDIUM.value


def action_matrix() -> dict[str, list[str]]:
    return {
        "low": sorted(LOW_RISK_ACTIONS),
        "medium": sorted(MEDIUM_RISK_ACTIONS),
        "high": sorted(HIGH_RISK_ACTIONS),
        "deny": sorted(DENY_ACTIONS),
    }
