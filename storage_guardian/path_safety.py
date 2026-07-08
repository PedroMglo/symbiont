"""Helpers for constraining untrusted filesystem path inputs."""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
from typing import Iterable


class UnsafePathError(ValueError):
    """Raised when a user-controlled path would escape an allowed root."""


def safe_relative_path(value: str | Path, *, field_name: str = "path") -> Path:
    raw = _raw_path(value, field_name)
    normalized = raw.replace("\\", "/")
    candidate = PurePosixPath(normalized)
    if candidate.is_absolute():
        raise UnsafePathError(f"{field_name} must be relative")
    return _relative_from_parts(candidate.parts, field_name)


def safe_child_path(root: Path, value: str | Path, *, field_name: str = "path") -> Path:
    base = _canonical_root(root)
    relative = safe_relative_path(value, field_name=field_name)
    child_text = os.path.realpath(os.path.join(os.fspath(base), *relative.parts))
    if os.path.commonpath([os.fspath(base), child_text]) != os.fspath(base):
        raise UnsafePathError(f"{field_name} escaped allowed root")
    return Path(child_text)


def safe_path_under_root(
    root: Path,
    value: str | Path,
    *,
    field_name: str = "path",
    allow_absolute: bool = True,
) -> Path:
    base = _canonical_root(root)
    raw = _raw_path(value, field_name)
    normalized = raw.replace("\\", "/")
    candidate = PurePosixPath(normalized)
    if candidate.is_absolute():
        if not allow_absolute:
            raise UnsafePathError(f"{field_name} must be relative")
        relative = _relative_parts_from_absolute(candidate.parts, base, field_name)
    else:
        relative = _relative_from_parts(candidate.parts, field_name)
    child_text = os.path.realpath(os.path.join(os.fspath(base), *relative.parts))
    if os.path.commonpath([os.fspath(base), child_text]) != os.fspath(base):
        raise UnsafePathError(f"{field_name} escaped allowed root")
    return Path(child_text)


def safe_existing_file_under_roots(
    value: str | Path,
    roots: Iterable[Path],
    *,
    field_name: str = "path",
) -> Path:
    allowed_roots = tuple(_canonical_root(root) for root in roots)
    if not allowed_roots:
        raise UnsafePathError("no allowed roots configured")

    raw = _raw_path(value, field_name)
    normalized = raw.replace("\\", "/")
    candidate = PurePosixPath(normalized)
    for root in allowed_roots:
        try:
            if candidate.is_absolute():
                relative = _relative_parts_from_absolute(candidate.parts, root, field_name)
                resolved = safe_child_path(root, relative.as_posix(), field_name=field_name)
            else:
                resolved = safe_child_path(root, normalized, field_name=field_name)
        except UnsafePathError:
            continue
        if resolved.is_relative_to(root) and resolved.is_file():
            return resolved
    raise UnsafePathError(f"{field_name} is outside allowed roots or is not a file")


def safe_path_name(value: str | Path, *, field_name: str = "path") -> str:
    relative = safe_relative_path(value, field_name=field_name)
    if len(relative.parts) != 1:
        raise UnsafePathError(f"{field_name} must be a single path segment")
    return relative.parts[0]


def sanitized_path_name(value: object, *, fallback: str = "item") -> str:
    raw = str(value or "").strip()
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in raw)
    cleaned = cleaned.strip("._")
    return cleaned or fallback


def _raw_path(value: str | Path, field_name: str) -> str:
    raw = str(value).strip()
    if not raw:
        raise UnsafePathError(f"{field_name} must not be empty")
    if "\x00" in raw:
        raise UnsafePathError(f"{field_name} contains a NUL byte")
    if raw.startswith("~"):
        raise UnsafePathError(f"{field_name} must not use home expansion")
    return raw


def _canonical_root(root: Path) -> Path:
    root_text = os.path.realpath(os.path.abspath(os.path.expanduser(os.fspath(root))))
    return Path(root_text)


def _relative_parts_from_absolute(parts: tuple[str, ...], root: Path, field_name: str) -> Path:
    root_parts = root.parts
    if parts[: len(root_parts)] != root_parts:
        raise UnsafePathError(f"{field_name} is outside allowed root")
    return _relative_from_parts(parts[len(root_parts) :], field_name)


def _relative_from_parts(parts: tuple[str, ...], field_name: str) -> Path:
    if not parts:
        raise UnsafePathError(f"{field_name} must not point to the root")
    blocked = {"", ".", ".."}
    if any(part in blocked for part in parts):
        raise UnsafePathError(f"{field_name} contains an unsafe path segment")
    return Path(*parts)
