"""Workspace execution planning for code evidence provider outputs."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def build_git_regression_sandbox_plan(report: dict[str, Any]) -> dict[str, Any]:
    """Build a non-executing workspace_execution plan for regression validation."""

    workspace = Path(str(report.get("workspace") or "."))
    repo_raw = report.get("repo")
    repo = Path(str(repo_raw)) if repo_raw else workspace
    cwd = _relative_cwd(workspace, repo)
    commands = [
        _command(command, cwd=cwd)
        for command in report.get("validation_commands", [])
        if isinstance(command, str) and command.strip() and not command.strip().startswith("cd ")
    ]
    return {
        "kind": "workspace_execution_plan",
        "owner": "local_evidence_operator",
        "provider": "code_analysis",
        "uses": "workspace_execution",
        "capability": "workspace_sandbox_validation_plan",
        "requires_orchestrator_execution": True,
        "recommended": True,
        "source": {
            "kind": "workspace",
            "workspace": str(workspace),
            "repo": str(repo) if repo_raw else None,
            "copy_required": True,
        },
        "session": {
            "execution_profile": "test",
            "network": "disabled",
            "real_host_writes": False,
        },
        "commands": commands,
        "publish": {
            "required": False,
            "allowed_via": "workspace_execution.artifacts.publish",
        },
    }


def _relative_cwd(workspace: Path, repo: Path) -> str:
    try:
        relative = repo.resolve().relative_to(workspace.resolve())
    except (OSError, ValueError):
        return "."
    text = relative.as_posix()
    return text or "."


def _command(command: str, *, cwd: str) -> dict[str, Any]:
    stripped = command.strip()
    profile = "test" if _looks_like_test_command(stripped) else "inspect"
    return {
        "purpose": "validate regression evidence inside disposable copy",
        "argv": ["/bin/bash", "-lc", stripped],
        "cwd": cwd,
        "allow_profile": profile,
    }


def _looks_like_test_command(command: str) -> bool:
    return any(token in command for token in ("pytest", "unittest", " npm test", " pnpm test", " yarn test"))
