"""Scratch path policy for artifacts before storage_guardian publication."""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_SCRATCH_ROOT = "/temp"


class ScratchPathError(RuntimeError):
    """Raised when a service tries to stage writable artifacts outside scratch."""


def scratch_roots() -> list[Path]:
    raw = (
        os.environ.get("AI_LOCAL_AGENT_TEMP_ROOTS")
        or os.environ.get("AI_LOCAL_AGENT_TEMP_ROOT")
        or os.environ.get("AI_LOCAL_PROJECT_SCRATCH_ROOT")
        or DEFAULT_SCRATCH_ROOT
    )
    roots = [Path(item.strip()).expanduser().resolve() for item in raw.split(":") if item.strip()]
    return roots or [Path(DEFAULT_SCRATCH_ROOT).resolve()]


def assert_scratch_path(path: Path | str, *, label: str = "path") -> Path:
    resolved = Path(path).expanduser().resolve()
    roots = scratch_roots()
    if any(resolved == root or resolved.is_relative_to(root) for root in roots):
        return resolved
    allowed = ", ".join(str(root) for root in roots)
    raise ScratchPathError(f"{label} must stay under scratch roots before storage_guardian publication: {allowed}")
