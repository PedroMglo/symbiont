"""Read-only material task evidence context for local workspaces."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from orchestrator.agentic.runtime import _absolute_path_candidates, _map_host_path_to_container_root

DEFAULT_MAX_DEPTH = 2
DEFAULT_MAX_FILES = 120
DEFAULT_MAX_FILE_BYTES = 1_000_000
DEFAULT_MAX_EXCERPT_BYTES = 12_000
DEFAULT_MAX_HASH_BYTES = 10_000_000

EXCLUDED_DIR_NAMES = {
    ".ai-local",
    ".git",
    ".hg",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".svn",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "venv",
}
HIDDEN_BENCHMARK_FILES = {"GROUND_TRUTH.md", "EVALUATION.md"}

DOCUMENT_EXTENSIONS = {
    ".doc",
    ".docx",
    ".htm",
    ".html",
    ".md",
    ".odf",
    ".ods",
    ".odt",
    ".pdf",
    ".ppt",
    ".pptx",
    ".rtf",
    ".txt",
    ".xls",
    ".xlsx",
}
DATA_EXTENSIONS = {
    ".csv",
    ".db",
    ".json",
    ".jsonl",
    ".parquet",
    ".sqlite",
    ".sqlite3",
    ".tsv",
    ".xml",
}
CONFIG_EXTENSIONS = {
    ".cfg",
    ".conf",
    ".env",
    ".ini",
    ".sh",
    ".sql",
    ".toml",
    ".yaml",
    ".yml",
}
MEDIA_EXTENSIONS = {
    ".aac",
    ".flac",
    ".m4a",
    ".mkv",
    ".mov",
    ".mp3",
    ".mp4",
    ".ogg",
    ".opus",
    ".wav",
    ".webm",
}
TEXT_EXCERPT_EXTENSIONS = {
    ".cfg",
    ".conf",
    ".csv",
    ".env",
    ".ini",
    ".json",
    ".jsonl",
    ".md",
    ".py",
    ".r",
    ".rst",
    ".sh",
    ".sql",
    ".toml",
    ".tsv",
    ".txt",
    ".yaml",
    ".yml",
}


def build_material_evidence_context(
    *,
    original_query: str,
    working_query: str = "",
    expected_artifact_root: str = "",
    user_language: str = "",
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_files: int = DEFAULT_MAX_FILES,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    max_excerpt_bytes: int = DEFAULT_MAX_EXCERPT_BYTES,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Build a bounded, read-only evidence context for a material task.

    The helper only inspects explicit paths found in the user/normalized prompt.
    It never searches the whole filesystem and never executes project code.
    """

    candidate_paths = _candidate_paths(original_query, working_query)
    request = {
        "original_user_prompt": original_query,
        "normalized_prompt": working_query,
        "user_language": user_language,
        "explicit_paths": [item["mapped_path"] for item in candidate_paths],
        "request_intents": ["material_output", "local_evidence"],
        "risk_level": "read_only",
        "allowed_operations": ["list_files", "read_text_excerpt", "stat_files", "hash_small_files"],
        "max_commands": 0,
        "max_seconds": 15,
        "max_output_bytes": 200_000,
        "require_read_only": True,
        "reason_for_acquisition": "local path mentioned in material-output request",
    }
    workspace = _select_workspace(candidate_paths, expected_artifact_root=expected_artifact_root)
    if workspace is None:
        request["missing_evidence"] = ["No explicit existing local workspace path was resolved."]
        return None, request
    context = _inspect_workspace(
        workspace,
        expected_artifact_root=expected_artifact_root,
        max_depth=max_depth,
        max_files=max_files,
        max_file_bytes=max_file_bytes,
        max_excerpt_bytes=max_excerpt_bytes,
    )
    if user_language:
        context["user_language"] = user_language
    request["inferred_workspace"] = str(workspace)
    request["workspace_resolution"] = {
        "active_workspace": str(workspace),
        "workspace_exists": workspace.exists(),
        "workspace_type": "directory" if workspace.is_dir() else "file",
        "resolution_source": "explicit_user_path",
        "boundary_root": str(workspace),
        "forbidden_paths": sorted(HIDDEN_BENCHMARK_FILES),
        "warnings": context.get("missing_evidence", []),
    }
    return context, request


def _candidate_paths(original_query: str, working_query: str) -> list[dict[str, Any]]:
    merged = f"{original_query or ''}\n{working_query or ''}"
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path, start, end in _absolute_path_candidates(merged):
        mapped = _map_host_path_to_container_root(path)
        if not mapped or mapped in seen:
            continue
        seen.add(mapped)
        candidates.append(
            {
                "original_path": path,
                "mapped_path": mapped,
                "start": start,
                "end": end,
                "exists": Path(mapped).exists(),
            }
        )
    return candidates


def _select_workspace(candidates: list[dict[str, Any]], *, expected_artifact_root: str) -> Path | None:
    scored: list[tuple[int, int, Path]] = []
    artifact_root = str(expected_artifact_root or "").strip().strip("/").casefold()
    for index, item in enumerate(candidates):
        raw_path = Path(str(item.get("mapped_path") or ""))
        if not raw_path.exists():
            continue
        path = raw_path
        score = 0
        if path.is_file():
            path = path.parent
            score += 20
        elif path.is_dir():
            score += 30
        else:
            continue
        if artifact_root and raw_path.name.casefold() == artifact_root and raw_path.parent.exists():
            path = raw_path.parent
            score -= 12
        if path.name in EXCLUDED_DIR_NAMES:
            score -= 20
        entries = _safe_iterdir(path)
        non_excluded = [entry for entry in entries if not _should_skip_path(entry, artifact_root=artifact_root)]
        if any(entry.is_dir() for entry in non_excluded):
            score += 12
        if non_excluded:
            score += min(len(non_excluded), 12)
        scored.append((score, -index, path.resolve()))
    if not scored:
        return None
    return max(scored, key=lambda item: (item[0], item[1]))[2]


def _inspect_workspace(
    workspace: Path,
    *,
    expected_artifact_root: str,
    max_depth: int,
    max_files: int,
    max_file_bytes: int,
    max_excerpt_bytes: int,
) -> dict[str, Any]:
    artifact_root = str(expected_artifact_root or "").strip().strip("/").casefold()
    top_level_entries = [entry.name for entry in _safe_iterdir(workspace)[:150]]
    files, skipped = _bounded_files(workspace, artifact_root=artifact_root, max_depth=max_depth, max_files=max_files)
    detected_docs: list[str] = []
    detected_data: list[str] = []
    detected_config: list[str] = []
    detected_tests: list[str] = []
    detected_media: list[str] = []
    observations: list[dict[str, Any]] = []
    relevant_files: list[str] = []
    for path in files:
        relative = _relative_path(workspace, path)
        category = _file_category(path)
        if category == "document":
            detected_docs.append(relative)
        elif category == "data":
            detected_data.append(relative)
        elif category == "config":
            detected_config.append(relative)
        elif category == "media":
            detected_media.append(relative)
            detected_data.append(relative)
        if _looks_like_test_path(path, workspace):
            detected_tests.append(relative)
        if category in {"document", "data", "config", "media"} or _looks_like_test_path(path, workspace):
            relevant_files.append(relative)
        observation = _file_observation(
            workspace,
            path,
            category=category,
            max_file_bytes=max_file_bytes,
            max_excerpt_bytes=max_excerpt_bytes,
        )
        if observation is not None:
            observations.append(observation)

    workspace_map = {
        "root": str(workspace),
        "top_level_entries": top_level_entries,
        "detected_languages": _detected_languages(files),
        "detected_frameworks": [],
        "detected_services": [],
        "detected_data_files": _dedupe(detected_data),
        "detected_media_files": _dedupe(detected_media),
        "detected_config_files": _dedupe(detected_config),
        "detected_test_files": _dedupe(detected_tests),
        "detected_docs": _dedupe(detected_docs),
        "large_or_skipped_paths": skipped,
        "git_status_summary": "not_collected",
        "risk_notes": [],
    }
    commands = [
        f"python pathlib list top-level entries in {workspace}",
        f"python pathlib bounded walk max_depth={max_depth} max_files={max_files}",
        "python pathlib stat/hash/read short text excerpts for selected files",
    ]
    missing_evidence: list[str] = []
    if skipped:
        missing_evidence.append("Some paths were skipped by safety, depth, or file-count limits.")
    if detected_docs or detected_media:
        missing_evidence.append(
            "Binary/structured documents and media require owner enrichment before claiming extracted content."
        )
    fingerprint = _workspace_fingerprint(workspace, files, skipped)
    context = {
        "request_id": fingerprint[:16],
        "workspace": str(workspace),
        "boundary_root": str(workspace),
        "workspace_exists": workspace.exists(),
        "resolution_source": "explicit_user_path",
        "workspace_map": workspace_map,
        "observations": [],
        "file_observations": observations,
        "relevant_files": _dedupe(relevant_files)[:max_files],
        "relevant_commands": commands,
        "commands": commands,
        "constraints": {
            "read_only": True,
            "max_depth": max_depth,
            "max_files": max_files,
            "max_file_bytes": max_file_bytes,
            "max_excerpt_bytes": max_excerpt_bytes,
        },
        "enrichment_plan": _enrichment_plan(detected_docs, detected_media),
        "evidence_summary": _evidence_summary(workspace_map, observations),
        "missing_evidence": missing_evidence,
        "confidence": 0.72 if relevant_files else 0.45,
        "cache_fingerprint": f"sha256:{fingerprint}",
        "context_digest": {
            "inspected": {
                "workspace": str(workspace),
                "top_level_entries": top_level_entries[:40],
                "files_sampled": len(observations),
            },
            "not_inspected": skipped[:50],
            "key_evidence": _dedupe(relevant_files)[:40],
            "uncertainties": missing_evidence,
            "next_recommended_actions": [
                "Use owner extraction/transcription results for binary documents or media before writing content claims."
            ],
        },
    }
    return context


def _bounded_files(workspace: Path, *, artifact_root: str, max_depth: int, max_files: int) -> tuple[list[Path], list[str]]:
    files: list[Path] = []
    skipped: list[str] = []
    stack: list[tuple[Path, int]] = [(workspace, 0)]
    while stack and len(files) < max_files:
        current, depth = stack.pop()
        if depth > max_depth:
            skipped.append(_relative_path(workspace, current))
            continue
        for entry in _safe_iterdir(current):
            if _should_skip_path(entry, artifact_root=artifact_root):
                skipped.append(_relative_path(workspace, entry))
                continue
            if entry.is_symlink():
                skipped.append(f"{_relative_path(workspace, entry)} (symlink skipped)")
                continue
            if entry.is_dir():
                if depth < max_depth:
                    stack.append((entry, depth + 1))
                else:
                    skipped.append(_relative_path(workspace, entry))
                continue
            if entry.is_file():
                files.append(entry)
                if len(files) >= max_files:
                    break
    if stack:
        skipped.append("file_limit_reached")
    return sorted(files, key=lambda item: _relative_path(workspace, item)), skipped[:100]


def _file_observation(
    workspace: Path,
    path: Path,
    *,
    category: str,
    max_file_bytes: int,
    max_excerpt_bytes: int,
) -> dict[str, Any] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    warnings: list[str] = []
    relative = _relative_path(workspace, path)
    excerpt = ""
    line_count: int | None = None
    was_fully_read = False
    was_sampled = False
    sha256 = ""
    if stat.st_size <= DEFAULT_MAX_HASH_BYTES:
        sha256 = f"sha256:{_hash_file(path)}"
    else:
        warnings.append("sha256_skipped_due_to_size")
    if path.suffix.casefold() in TEXT_EXCERPT_EXTENSIONS and stat.st_size <= max_file_bytes:
        raw = path.read_bytes()[:max_excerpt_bytes]
        text = raw.decode("utf-8", errors="replace")
        excerpt = text.strip()
        line_count = text.count("\n") + (1 if text else 0)
        was_fully_read = stat.st_size <= max_excerpt_bytes
        was_sampled = stat.st_size > max_excerpt_bytes
        if was_sampled:
            warnings.append("excerpt_truncated")
    elif path.suffix.casefold() in TEXT_EXCERPT_EXTENSIONS:
        raw = path.read_bytes()[:max_excerpt_bytes]
        excerpt = raw.decode("utf-8", errors="replace").strip()
        line_count = excerpt.count("\n") + (1 if excerpt else 0)
        was_sampled = True
        warnings.append("file_too_large_sampled")
    else:
        warnings.append("binary_or_unsupported_text_type_not_read")
    return {
        "path": relative,
        "file_type": _file_type(path),
        "size_bytes": stat.st_size,
        "line_count": line_count,
        "sha256": sha256,
        "excerpt": excerpt[:4000],
        "relevance_reason": category,
        "was_fully_read": was_fully_read,
        "was_sampled": was_sampled,
        "warnings": warnings,
    }


def _enrichment_plan(detected_docs: list[str], detected_media: list[str]) -> list[dict[str, Any]]:
    plan: list[dict[str, Any]] = []
    structured_docs = [
        path for path in _dedupe(detected_docs)
        if Path(path).suffix.casefold() not in {".md", ".txt", ".html", ".htm"}
    ]
    if structured_docs:
        plan.append(
            {
                "provider": "extrator",
                "capability": "document_extraction",
                "input_paths": structured_docs[:100],
                "reason": "binary_or_structured_documents_detected",
                "execution_mode": "owner_required_or_reuse_existing",
                "expected_evidence_types": ["document_diagnostic", "document_extract", "document_evidence"],
            }
        )
    if detected_media:
        plan.append(
            {
                "provider": "audio_transcribe",
                "capability": "audio_transcription",
                "input_paths": _dedupe(detected_media)[:100],
                "reason": "audio_or_video_media_detected",
                "execution_mode": "owner_required_or_reuse_existing",
                "expected_evidence_types": ["audio_transcript", "audio_query_response"],
            }
        )
    return plan


def _evidence_summary(workspace_map: dict[str, Any], observations: list[dict[str, Any]]) -> str:
    return (
        f"Observed {len(workspace_map.get('top_level_entries') or [])} top-level entries, "
        f"{len(workspace_map.get('detected_docs') or [])} document files, "
        f"{len(workspace_map.get('detected_data_files') or [])} data/media files, "
        f"{len(workspace_map.get('detected_config_files') or [])} config/script files, "
        f"and {len(observations)} sampled file observations."
    )


def _workspace_fingerprint(workspace: Path, files: list[Path], skipped: list[str]) -> str:
    hasher = hashlib.sha256()
    hasher.update(str(workspace).encode("utf-8", errors="replace"))
    for path in files:
        try:
            stat = path.stat()
        except OSError:
            continue
        hasher.update(_relative_path(workspace, path).encode("utf-8", errors="replace"))
        hasher.update(str(stat.st_size).encode())
        hasher.update(str(int(stat.st_mtime_ns)).encode())
    for skipped_path in skipped[:50]:
        hasher.update(str(skipped_path).encode("utf-8", errors="replace"))
    return hasher.hexdigest()


def _hash_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _safe_iterdir(path: Path) -> list[Path]:
    try:
        return sorted(path.iterdir(), key=lambda item: item.name.casefold())
    except OSError:
        return []


def _should_skip_path(path: Path, *, artifact_root: str) -> bool:
    name = path.name
    lower = name.casefold()
    if name in HIDDEN_BENCHMARK_FILES:
        return True
    if lower in EXCLUDED_DIR_NAMES:
        return True
    if artifact_root and lower == artifact_root:
        return True
    return False


def _relative_path(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _file_category(path: Path) -> str:
    suffix = path.suffix.casefold()
    name = path.name.casefold()
    if suffix in DOCUMENT_EXTENSIONS:
        return "document"
    if suffix in MEDIA_EXTENSIONS:
        return "media"
    if suffix in DATA_EXTENSIONS:
        return "data"
    if suffix in CONFIG_EXTENSIONS or name in {"dockerfile", "compose.yml", "docker-compose.yml"}:
        return "config"
    return "other"


def _file_type(path: Path) -> str:
    suffix = path.suffix.casefold().lstrip(".")
    if suffix:
        return suffix
    return path.name.casefold()


def _looks_like_test_path(path: Path, root: Path) -> bool:
    relative = _relative_path(root, path).casefold()
    parts = relative.split("/")
    return any(part in {"test", "tests"} for part in parts) or path.name.casefold().startswith("test_")


def _detected_languages(files: list[Path]) -> list[str]:
    mapping = {
        ".js": "javascript",
        ".jsx": "javascript",
        ".py": "python",
        ".r": "r",
        ".rs": "rust",
        ".sql": "sql",
        ".ts": "typescript",
        ".tsx": "typescript",
    }
    languages = [mapping[path.suffix.casefold()] for path in files if path.suffix.casefold() in mapping]
    return _dedupe(languages)


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result
