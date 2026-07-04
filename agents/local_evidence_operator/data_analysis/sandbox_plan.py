"""Workspace execution planning for data evidence provider outputs."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def build_data_sandbox_plan(report: dict[str, Any], *, mode: str, workspace: Path) -> dict[str, Any]:
    """Build a non-executing sandbox plan for optional dataset validation."""

    return {
        "kind": "workspace_execution_plan",
        "owner": "local_evidence_operator",
        "provider": "data_analysis",
        "uses": "workspace_execution",
        "capability": "workspace_sandbox_validation_plan",
        "requires_orchestrator_execution": True,
        "recommended": False,
        "required_before_mutation": True,
        "source": {
            "kind": "workspace",
            "workspace": str(workspace),
            "copy_required": True,
        },
        "session": {
            "execution_profile": "inspect",
            "network": "disabled",
            "real_host_writes": False,
        },
        "commands": [
            {
                "purpose": "inspect copied dataset inputs before optional transformations",
                "argv": ["/bin/bash", "-lc", "find . -maxdepth 3 -type f | sort | head -200"],
                "cwd": ".",
                "allow_profile": "inspect",
            }
        ],
        "evidence": {
            "mode": mode,
            "analysis_mode": report.get("analysis_mode"),
            "summary": report.get("summary", {}),
        },
        "publish": {
            "required": False,
            "allowed_via": "workspace_execution.artifacts.publish",
        },
    }
