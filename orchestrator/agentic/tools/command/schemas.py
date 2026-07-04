"""Schemas for the command capability."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class CommandMount:
    sandbox_path: str
    source_path: Path
    read_only: bool = True
    label: str = ""
    docker_source_path: Path | None = None


@dataclass(frozen=True)
class CommandContext:
    profile: str
    default_cwd: str
    mounts: tuple[CommandMount, ...]


@dataclass(frozen=True)
class CommandClassification:
    command: str
    action: str
    risk_level: str
    decision_hint: str
    reason: str
    tokens: tuple[str, ...] = ()
    denied_markers: tuple[str, ...] = ()
    requires_approval: bool = False
    dry_run_required: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def should_block(self) -> bool:
        return self.risk_level == "deny"


@dataclass(frozen=True)
class CommandResult:
    command: str
    cwd: str
    exit_code: int | None
    stdout: str
    stderr: str
    output_truncated: bool
    duration_ms: float
    status: str
    error: str | None = None
