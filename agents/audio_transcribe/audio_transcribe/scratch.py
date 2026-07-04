"""Local scratch path policy for audio_transcribe staging."""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_SCRATCH_ROOT = "/temp"
DEFAULT_MODEL_CACHE_ROOT = "/models"


class ScratchPathError(RuntimeError):
    """Raised when audio_transcribe tries to stage writable artifacts outside scratch."""


def scratch_roots() -> list[Path]:
    raw = (
        os.environ.get("AI_LOCAL_AGENT_TEMP_ROOTS")
        or os.environ.get("AI_LOCAL_AGENT_TEMP_ROOT")
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


def model_cache_roots() -> list[Path]:
    raw = (
        os.environ.get("AI_LOCAL_MODEL_CACHE_ROOTS")
        or os.environ.get("HF_HOME")
        or DEFAULT_MODEL_CACHE_ROOT
    )
    roots = [Path(item.strip()).expanduser().resolve() for item in raw.split(":") if item.strip()]
    return roots or [Path(DEFAULT_MODEL_CACHE_ROOT).resolve()]


def assert_model_cache_path(path: Path | str, *, label: str = "model cache path") -> Path:
    resolved = Path(path).expanduser().resolve()
    roots = model_cache_roots()
    if any(resolved == root or resolved.is_relative_to(root) for root in roots):
        return resolved
    allowed = ", ".join(str(root) for root in roots)
    raise ScratchPathError(f"{label} must stay under configured model cache roots: {allowed}")
