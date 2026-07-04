"""Workspace copy, diff and artifact helpers for session-local execution."""

from __future__ import annotations

import difflib
import hashlib
import mimetypes
import os
import re
import shutil
from pathlib import Path
from typing import Any

from workspace_execution.errors import WorkspaceExecutionError
from workspace_execution.types import ArtifactDescriptor, DiffFile, HostPathSource, MaterializationSource, WorkspaceSource


DEFAULT_EXCLUDED_DIR_NAMES = frozenset(
    {
        ".cache",
        ".git",
        ".hg",
        ".local",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "venv",
    }
)
DEFAULT_EXCLUDED_FILE_SUFFIXES = frozenset({".pyc", ".pyo"})

MAX_INLINE_DIFF_PATCH_BYTES = 12_000
MAX_INLINE_DIFF_PATCH_LINES = 200
HUNK_HEADER_RE = re.compile(r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? \+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_relative_path(raw: str | None, *, default: str = ".") -> Path:
    value = raw if raw is not None and raw.strip() else default
    normalized = str(value).replace("\\", "/")
    if "\x00" in normalized:
        raise WorkspaceExecutionError(
            "path_not_allowed",
            "workspace execution paths cannot contain NUL bytes",
            details={"path": value},
        )
    if normalized.startswith(("~", "/")) or (len(normalized) >= 2 and normalized[1] == ":"):
        raise WorkspaceExecutionError(
            "path_not_allowed",
            "workspace execution paths must be relative and stay inside the session",
            details={"path": value},
        )
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise WorkspaceExecutionError(
            "path_not_allowed",
            "workspace execution paths must be relative and stay inside the session",
            details={"path": value},
        )
    return Path(".") if str(path) in {"", "."} else path


def safe_child(base: Path, raw: str | None, *, default: str = ".") -> Path:
    rel = safe_relative_path(raw, default=default)
    candidate = (base / rel).resolve()
    root = base.resolve()
    if candidate != root and root not in candidate.parents:
        raise WorkspaceExecutionError(
            "path_not_allowed",
            "workspace execution path escapes the session",
            details={"path": str(raw or default)},
        )
    return candidate


def safe_workspace_file_path(base: Path, raw: str, *, forbid_symlink_escape: bool = True) -> Path:
    rel = safe_relative_path(raw)
    if str(rel) == ".":
        raise WorkspaceExecutionError(
            "path_not_allowed",
            "workspace file operation requires a file path, not the workspace root",
            details={"path": raw},
        )
    candidate = safe_child(base, raw)
    if forbid_symlink_escape:
        _reject_symlink_escape(base, candidate, raw)
    if candidate.exists() and not candidate.is_file():
        raise WorkspaceExecutionError(
            "path_not_allowed",
            "workspace file operation target must be a regular file",
            details={"path": raw},
        )
    return candidate


def write_workspace_file(
    *,
    workspace_path: Path,
    relative_path: str,
    content: bytes,
    forbid_symlink_escape: bool = True,
) -> tuple[str | None, str, int]:
    target = safe_workspace_file_path(workspace_path, relative_path, forbid_symlink_escape=forbid_symlink_escape)
    before_sha = sha256_file(target) if target.exists() else None
    target.parent.mkdir(parents=True, exist_ok=True)
    if forbid_symlink_escape:
        _reject_symlink_escape(workspace_path, target, relative_path)
    target.write_bytes(content)
    if forbid_symlink_escape:
        _reject_symlink_escape(workspace_path, target, relative_path)
    return before_sha, sha256_file(target), target.stat().st_size


def apply_workspace_patch(
    *,
    workspace_path: Path,
    relative_path: str,
    unified_diff: str,
    expected_old_sha256: str | None = None,
    forbid_symlink_escape: bool = True,
) -> tuple[str | None, str | None]:
    target = safe_workspace_file_path(workspace_path, relative_path, forbid_symlink_escape=forbid_symlink_escape)
    before_sha = sha256_file(target) if target.exists() else None
    if expected_old_sha256 is not None and before_sha != expected_old_sha256:
        raise WorkspaceExecutionError(
            "checksum_mismatch",
            "patch expected_old_sha256 does not match the current workspace file",
            details={
                "path": relative_path,
                "expected_old_sha256": expected_old_sha256,
                "actual_old_sha256": before_sha,
            },
        )
    before_text = target.read_text(encoding="utf-8") if target.exists() else ""
    after_text = apply_unified_diff_to_text(before_text, unified_diff, path=relative_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if after_text is None:
        if target.exists():
            target.unlink()
        return before_sha, None
    target.write_text(after_text, encoding="utf-8")
    if forbid_symlink_escape:
        _reject_symlink_escape(workspace_path, target, relative_path)
    return before_sha, sha256_file(target)


def apply_unified_diff_to_text(original: str, unified_diff: str, *, path: str) -> str:
    diff_lines = unified_diff.splitlines()
    if not any(line.startswith("--- ") for line in diff_lines) or not any(line.startswith("+++ ") for line in diff_lines):
        raise WorkspaceExecutionError(
            "patch_apply_failed",
            "patch must include unified diff file headers",
            details={"path": path},
        )
    original_lines = original.splitlines()
    output: list[str] = []
    old_index = 0
    index = 0
    saw_hunk = False
    while index < len(diff_lines):
        line = diff_lines[index]
        match = HUNK_HEADER_RE.match(line)
        if match is None:
            index += 1
            continue
        saw_hunk = True
        old_start = int(match.group("old_start"))
        hunk_old_index = max(old_start - 1, 0)
        if hunk_old_index < old_index:
            raise WorkspaceExecutionError(
                "patch_apply_failed",
                "patch hunks overlap or move backwards",
                details={"path": path, "hunk": line},
            )
        output.extend(original_lines[old_index:hunk_old_index])
        old_index = hunk_old_index
        index += 1
        while index < len(diff_lines) and not diff_lines[index].startswith("@@ "):
            hunk_line = diff_lines[index]
            if hunk_line.startswith("\\"):
                index += 1
                continue
            if not hunk_line:
                raise WorkspaceExecutionError(
                    "patch_apply_failed",
                    "patch hunk line is missing a unified diff prefix",
                    details={"path": path},
                )
            prefix = hunk_line[0]
            content = hunk_line[1:]
            if prefix == " ":
                _assert_patch_context(original_lines, old_index, content, path)
                output.append(original_lines[old_index])
                old_index += 1
            elif prefix == "-":
                _assert_patch_context(original_lines, old_index, content, path)
                old_index += 1
            elif prefix == "+":
                output.append(content)
            else:
                raise WorkspaceExecutionError(
                    "patch_apply_failed",
                    "patch hunk line has an unsupported unified diff prefix",
                    details={"path": path, "prefix": prefix},
                )
            index += 1
    if not saw_hunk:
        raise WorkspaceExecutionError(
            "patch_apply_failed",
            "patch must include at least one unified diff hunk",
            details={"path": path},
        )
    output.extend(original_lines[old_index:])
    trailing_newline = original.endswith("\n") or unified_diff.endswith("\n")
    text = "\n".join(output)
    return f"{text}\n" if trailing_newline else text


def _assert_patch_context(original_lines: list[str], index: int, expected: str, path: str) -> None:
    if index >= len(original_lines) or original_lines[index] != expected:
        actual = original_lines[index] if index < len(original_lines) else None
        raise WorkspaceExecutionError(
            "patch_apply_failed",
            "patch context does not match the current workspace file",
            details={"path": path, "expected": expected, "actual": actual, "line_index": index},
        )


def _reject_symlink_escape(base: Path, candidate: Path, raw: str) -> None:
    root = base.resolve()
    current = root
    rel = safe_relative_path(raw)
    for part in rel.parts:
        current = current / part
        if current.is_symlink():
            resolved = current.resolve()
            if resolved != root and root not in resolved.parents:
                raise WorkspaceExecutionError(
                    "symlink_escape_attempt",
                    "workspace file operation cannot traverse a symlink outside the session",
                    details={"path": raw, "resolved": str(resolved)},
                )
            raise WorkspaceExecutionError(
                "symlink_escape_attempt",
                "workspace file operation cannot target symlinks",
                details={"path": raw, "resolved": str(resolved)},
            )
    resolved_candidate = candidate.resolve()
    if resolved_candidate != root and root not in resolved_candidate.parents:
        raise WorkspaceExecutionError(
            "path_not_allowed",
            "workspace file operation target escapes the session",
            details={"path": raw},
        )


def materialize_source(
    source: MaterializationSource,
    *,
    source_roots: dict[str, Path],
    workspace_path: Path,
    host_read_host_root: Path | None = None,
    host_read_container_root: Path | None = None,
) -> int:
    if isinstance(source, WorkspaceSource):
        return _materialize_workspace_source(source, source_roots=source_roots, workspace_path=workspace_path)
    if isinstance(source, HostPathSource):
        return _materialize_host_path_source(
            source,
            workspace_path=workspace_path,
            host_read_host_root=host_read_host_root,
            host_read_container_root=host_read_container_root,
        )
    marker_dir = workspace_path / ".workspace_execution_inputs"
    marker_dir.mkdir(parents=True, exist_ok=True)
    marker = marker_dir / f"{source.kind}-{hashlib.sha256(source.model_dump_json().encode('utf-8')).hexdigest()[:16]}.json"
    marker.write_text(source.model_dump_json(indent=2), encoding="utf-8")
    return 1


def _materialize_workspace_source(source: WorkspaceSource, *, source_roots: dict[str, Path], workspace_path: Path) -> int:
    root = source_roots.get(source.root_ref)
    if root is None:
        raise WorkspaceExecutionError(
            "source_root_not_allowed",
            "workspace source root is not declared in workspace execution config",
            details={"root_ref": source.root_ref},
        )
    root = root.expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise WorkspaceExecutionError(
            "source_root_unavailable",
            "workspace source root is not available",
            details={"root_ref": source.root_ref},
        )
    copied = 0
    for raw_path in source.paths or ["."]:
        rel = safe_relative_path(raw_path)
        src = (root / rel).resolve()
        if src != root and root not in src.parents:
            raise WorkspaceExecutionError(
                "source_path_not_allowed",
                "workspace source path escapes its declared root",
                details={"root_ref": source.root_ref, "path": raw_path},
            )
        if not src.exists():
            raise WorkspaceExecutionError(
                "source_path_missing",
                "workspace source path does not exist",
                details={"root_ref": source.root_ref, "path": raw_path},
            )
        dest = workspace_path if rel == Path(".") else workspace_path / rel
        copied += _copy_path(src, dest)
    return copied


def _materialize_host_path_source(
    source: HostPathSource,
    *,
    workspace_path: Path,
    host_read_host_root: Path | None,
    host_read_container_root: Path | None,
) -> int:
    if source.access_origin == "system_inferred" and not source.user_approved:
        raise WorkspaceExecutionError(
            "host_read_approval_required",
            "system-inferred host path reads require explicit user approval before materialization",
            details={
                "path": source.path,
                "access_origin": source.access_origin,
                "standby_required": True,
                "requested_mode": "read_only",
            },
        )
    requested = Path(source.path).expanduser()
    if not requested.is_absolute():
        raise WorkspaceExecutionError(
            "host_path_not_absolute",
            "host path sources must be absolute paths resolved from explicit user intent",
            details={"path": source.path},
        )
    src = _resolve_host_read_path(
        requested,
        host_read_host_root=host_read_host_root,
        host_read_container_root=host_read_container_root,
    )
    if not src.exists():
        raise WorkspaceExecutionError(
            "host_path_missing",
            "host path source does not exist or is not visible through the read-only host mount",
            details={"path": source.path},
        )
    dest = workspace_path / src.name
    return _copy_path(src, dest)


def _resolve_host_read_path(
    requested: Path,
    *,
    host_read_host_root: Path | None,
    host_read_container_root: Path | None,
) -> Path:
    direct = requested.resolve(strict=False)
    if direct.exists():
        return direct
    if host_read_host_root is None or host_read_container_root is None:
        return direct
    requested = requested.resolve(strict=False)
    host_root = host_read_host_root.expanduser().resolve(strict=False)
    container_root = host_read_container_root.expanduser().resolve(strict=False)
    try:
        rel = requested.relative_to(host_root)
    except ValueError:
        raise WorkspaceExecutionError(
            "host_path_not_mounted",
            "host path is outside the configured read-only host mount",
            details={"path": str(requested), "host_read_root": str(host_root)},
        ) from None
    candidate = (container_root / rel).resolve(strict=False)
    root = container_root.resolve(strict=False)
    if candidate != root and root not in candidate.parents:
        raise WorkspaceExecutionError(
            "host_path_not_allowed",
            "host path mapping escapes the configured read-only host mount",
            details={"path": str(requested), "host_read_root": str(host_root)},
        )
    return candidate


def _copy_path(src: Path, dest: Path) -> int:
    if src.is_symlink():
        return 0
    if src.is_file():
        dest.parent.mkdir(parents=True, exist_ok=True)
        _copy_file(src, dest)
        return 1
    copied = 0
    for root, dirnames, filenames in os.walk(src, followlinks=False):
        root_path = Path(root)
        dirnames[:] = [
            name
            for name in dirnames
            if name not in DEFAULT_EXCLUDED_DIR_NAMES and not (root_path / name).is_symlink()
        ]
        for filename in filenames:
            file_path = root_path / filename
            if file_path.is_symlink():
                continue
            relative = file_path.relative_to(src)
            target = dest / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            _copy_file(file_path, target)
            copied += 1
    return copied


def _copy_file(src: Path, dest: Path) -> None:
    try:
        shutil.copy2(src, dest)
    except OSError as exc:
        raise WorkspaceExecutionError(
            "source_copy_failed",
            "workspace source file could not be copied into disposable session",
            details={"source": str(src), "destination": str(dest), "reason": str(exc)},
        ) from exc


def _is_excluded_relative_path(path: Path) -> bool:
    return any(part in DEFAULT_EXCLUDED_DIR_NAMES for part in path.parts) or path.suffix in DEFAULT_EXCLUDED_FILE_SUFFIXES


def is_excluded_relative_path(path: Path) -> bool:
    """Return whether a relative workspace output should be omitted from manifests/artifacts."""
    return _is_excluded_relative_path(path)


def file_manifest(root: Path) -> dict[str, dict[str, Any]]:
    manifest: dict[str, dict[str, Any]] = {}
    if not root.exists():
        return manifest
    for path in sorted(item for item in root.rglob("*") if item.is_file() and not item.is_symlink()):
        rel_path = path.relative_to(root)
        if _is_excluded_relative_path(rel_path):
            continue
        rel = rel_path.as_posix()
        manifest[rel] = {"sha256": sha256_file(path), "size_bytes": path.stat().st_size}
    return manifest


def manifest_hash(manifest: dict[str, dict[str, Any]]) -> str:
    encoded = repr(sorted((path, data["sha256"], data["size_bytes"]) for path, data in manifest.items())).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def diff_files(*, baseline: dict[str, dict[str, Any]], current: dict[str, dict[str, Any]], workspace_path: Path) -> list[DiffFile]:
    files: list[DiffFile] = []
    for rel in sorted(set(baseline) | set(current)):
        old = baseline.get(rel)
        new = current.get(rel)
        if old and new and old["sha256"] == new["sha256"]:
            continue
        status = "modified" if old and new else "added" if new else "deleted"
        additions, deletions, binary = _line_delta(workspace_path / rel, status=status)
        patch = _inline_patch(workspace_path / rel, rel, status=status, binary=binary)
        files.append(
            DiffFile(
                path=rel,
                status=status,
                additions=additions,
                deletions=deletions,
                binary=binary,
                patch=patch,
                old_sha256=str(old["sha256"]) if old else None,
                new_sha256=str(new["sha256"]) if new else None,
            )
        )
    return files


def artifact_descriptors(artifacts_path: Path) -> list[ArtifactDescriptor]:
    descriptors: list[ArtifactDescriptor] = []
    if not artifacts_path.exists():
        return descriptors
    for path in sorted(item for item in artifacts_path.rglob("*") if item.is_file() and not item.is_symlink()):
        rel_path = path.relative_to(artifacts_path)
        if _is_excluded_relative_path(rel_path):
            continue
        rel = rel_path.as_posix()
        digest = sha256_file(path)
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        descriptors.append(
            ArtifactDescriptor(
                artifact_id=f"artifact:{digest[:16]}",
                path=rel,
                media_type=media_type,
                sha256=digest,
                size_bytes=path.stat().st_size,
                origin="command",
            )
        )
    return descriptors


def _line_delta(path: Path, *, status: str) -> tuple[int, int, bool]:
    if status == "deleted" or not path.exists():
        return (0, 0, False)
    try:
        data = path.read_bytes()
    except OSError:
        return (0, 0, True)
    if b"\x00" in data:
        return (0, 0, True)
    text = data.decode("utf-8", errors="replace")
    lines = len(text.splitlines())
    if status == "added":
        return (lines, 0, False)
    return (lines, lines, False)


def _inline_patch(path: Path, rel: str, *, status: str, binary: bool) -> str | None:
    """Return a bounded unified diff preview when the session can prove it."""
    if binary or status != "added" or not path.exists():
        return None
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    patch_lines = list(difflib.unified_diff([], lines, fromfile="/dev/null", tofile=f"b/{rel}", lineterm=""))
    truncated = False
    if len(patch_lines) > MAX_INLINE_DIFF_PATCH_LINES:
        patch_lines = patch_lines[:MAX_INLINE_DIFF_PATCH_LINES]
        truncated = True
    patch = "\n".join(patch_lines)
    encoded = patch.encode("utf-8")
    if len(encoded) > MAX_INLINE_DIFF_PATCH_BYTES:
        patch = encoded[:MAX_INLINE_DIFF_PATCH_BYTES].decode("utf-8", errors="ignore")
        truncated = True
    if truncated:
        patch = f"{patch}\n... diff truncated ..."
    return patch
