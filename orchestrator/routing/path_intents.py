"""Helpers for routing user-provided local paths to feature services."""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from orchestrator.capabilities.source_selection import source_selection_manifest

_ABS_PATH_RE = re.compile("(?<![\\w.-])(?P<path>/(?:[^\\s" + chr(39) + chr(34) + "`<>])+)" )
_QUOTED_PATH_RE = re.compile("[" + chr(39) + chr(34) + "](?P<path>/(?:[^" + chr(39) + chr(34) + "])+)[" + chr(39) + chr(34) + "]")


def extract_absolute_path(query: str) -> str | None:
    """Extract the first absolute path from a user query."""
    text = query or ""
    quoted = _QUOTED_PATH_RE.search(text)
    match = quoted or _ABS_PATH_RE.search(text)
    if not match:
        return None
    path = match.group("path").rstrip(".,;:)])}")
    return path or None


def extract_absolute_paths(query: str) -> tuple[str, ...]:
    """Extract absolute paths while preserving order and removing duplicates."""
    text = query or ""
    found: list[str] = []
    for match in _QUOTED_PATH_RE.finditer(text):
        found.append(match.group("path").rstrip(".,;:)])}"))
    for match in _ABS_PATH_RE.finditer(text):
        found.append(match.group("path").rstrip(".,;:)])}"))
    unique: list[str] = []
    seen: set[str] = set()
    for path in found:
        if path and path not in seen:
            unique.append(path)
            seen.add(path)
    return tuple(unique)


def _path_extension(path: str) -> str:
    lowered = (path or "").lower().rstrip(".,;:)])}")
    for ext in ("jsonl.gz", "ndjson.gz", "csv.gz", "tsv.gz"):
        if lowered.endswith(f".{ext}"):
            return ext
    return PurePosixPath(lowered).suffix.lower().lstrip(".")


def _normalized_query(query: str) -> str:
    return " ".join((query or "").lower().split())


def _query_words(query: str) -> set[str]:
    return {w.strip(".,!?:;" + chr(34) + "()[]{}" + chr(39)) for w in _normalized_query(query).split()}


def _has_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(term in text for term in terms)


def _query_has_explicit_file_path(query: str) -> bool:
    return bool(re.search("(^|[\\s" + chr(34) + chr(39) + "])(?:/|~/|\\./|\\../|[A-Za-z]:[\\\\/])", query or ""))


def is_extrator_path_request(query: str) -> bool:
    """Return True when source-selection signals route a local path to extrator."""
    manifest = source_selection_manifest("extrator_path")
    path = extract_absolute_path(query)
    if manifest.path_required and not path:
        return False

    lower = _normalized_query(query)
    words = _query_words(query)
    has_action = bool(words & set(manifest.action_terms))
    suffix = _path_extension(path or "")
    has_document_ext = suffix in manifest.extensions
    has_folder_hint = _has_any(lower, manifest.folder_terms)
    has_guard_intent = _has_any(lower, manifest.guard_terms)
    if has_guard_intent and not has_action:
        return False
    return has_action or has_document_ext or has_folder_hint


def is_code_path_request(query: str) -> bool:
    """Return True when source-selection signals route a local path to code analysis."""
    manifest = source_selection_manifest("code_path")
    path = extract_absolute_path(query)
    if manifest.path_required and not path:
        return False
    lower = _normalized_query(query)
    suffix = _path_extension(path or "")
    return suffix in manifest.extensions and _has_any(lower, manifest.intent_terms)


def is_explicit_extrator_processing_request(query: str) -> bool:
    """Return True for explicit user requests to run extrator processing."""
    manifest = source_selection_manifest("extrator_path")
    lower = _normalized_query(query)
    if not lower or not _query_has_explicit_file_path(query):
        return False
    return _has_any(lower, manifest.processing_action_terms) and _has_any(
        lower,
        manifest.processing_target_terms,
    )


def is_storage_request(query: str) -> bool:
    """Return True when routing should delegate the query to storage_guardian.

    This is a source-selection hint only. The operation parser and storage
    policy are owned by storage_guardian and run behind its HTTPS API.
    """

    manifest = source_selection_manifest("storage")
    lower = _normalized_query(query)
    has_operation_hint = _has_any(lower, manifest.operation_terms)
    if not has_operation_hint:
        return False
    return (
        _has_any(lower, manifest.source_terms)
        or bool(extract_absolute_path(query))
        or _has_any(lower, manifest.direct_terms)
    )


def needs_storage_context(query: str) -> bool:
    """Return True when storage_guardian should be consulted as context.

    The underlying intent semantics belong to storage_guardian behind HTTPS.
    The orchestrator consumes only this source-selection signal.
    """
    manifest = source_selection_manifest("storage")
    lower = _normalized_query(query)
    return _has_any(lower, manifest.source_terms) or is_storage_request(query) or is_archive_recovery_request(query)


def is_archive_recovery_request(query: str) -> bool:
    """Return True for safe archive/backup recovery investigations.

    This is a workspace capability hint. The archive inspection itself is owned
    by storage_guardian behind HTTPS.
    """

    manifest = source_selection_manifest("storage")
    return _has_any(_normalized_query(query), manifest.recovery_terms)
