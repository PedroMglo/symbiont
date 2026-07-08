"""Registry for user-requested local sources.

The registry lets the RAG owner index bounded local paths requested at runtime
without mutating user configuration files. Paths are resolved to the read-only
host-source mount when the service runs in Docker.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

from rag_config import settings

SOURCE_TYPES = frozenset({"auto", "document", "code", "vault"})
_REGISTRY_FILENAME = "requested_sources.json"


@dataclass(frozen=True)
class RegisteredIngestSource:
    name: str
    path: Path
    source_type: str
    requested_path: str
    registered_at: str
    exclude_patterns: tuple[str, ...] = ()

    def as_record(self) -> dict[str, str]:
        return {
            "name": self.name,
            "path": str(self.path),
            "source_type": self.source_type,
            "requested_path": self.requested_path,
            "registered_at": self.registered_at,
            "exclude_patterns": json.dumps(list(self.exclude_patterns), ensure_ascii=False),
        }


def register_requested_sources(raw_sources: Iterable[Mapping[str, Any]]) -> list[dict[str, str]]:
    """Resolve, validate and persist runtime-requested ingest sources."""

    records = {record["path"]: record for record in _load_registry()}
    registered: list[RegisteredIngestSource] = []
    for raw in raw_sources:
        source = resolve_requested_source(raw)
        records[str(source.path)] = source.as_record()
        registered.append(source)
    _save_registry(records.values())
    return [source.as_record() for source in registered]


def registered_sources(*, source_types: set[str] | None = None) -> tuple[RegisteredIngestSource, ...]:
    """Return valid registered sources, optionally filtered by source type."""

    selected: list[RegisteredIngestSource] = []
    for record in _load_registry():
        source_type = str(record.get("source_type") or "").strip()
        if source_types is not None and source_type not in source_types:
            continue
        path = Path(str(record.get("path") or ""))
        if not path.exists():
            continue
        selected.append(
            RegisteredIngestSource(
                name=str(record.get("name") or path.name),
                path=path,
                source_type=source_type,
                requested_path=str(record.get("requested_path") or path),
                registered_at=str(record.get("registered_at") or ""),
                exclude_patterns=_decode_exclude_patterns(record.get("exclude_patterns")),
            )
        )
    return tuple(selected)


def registered_sources_from_records(
    records: Iterable[Mapping[str, Any]],
    *,
    source_types: set[str] | None = None,
) -> tuple[RegisteredIngestSource, ...]:
    """Rehydrate registered source records returned by ``register_requested_sources``."""

    selected: list[RegisteredIngestSource] = []
    for record in records:
        source_type = str(record.get("source_type") or "").strip()
        if source_types is not None and source_type not in source_types:
            continue
        path = Path(str(record.get("path") or ""))
        if not path.exists():
            continue
        selected.append(
            RegisteredIngestSource(
                name=str(record.get("name") or path.name),
                path=path,
                source_type=source_type,
                requested_path=str(record.get("requested_path") or path),
                registered_at=str(record.get("registered_at") or ""),
                exclude_patterns=_decode_exclude_patterns(record.get("exclude_patterns")),
            )
        )
    return tuple(selected)


def registered_source_paths(*, source_types: set[str] | None = None) -> tuple[Path, ...]:
    return tuple(source.path for source in registered_sources(source_types=source_types))


def resolve_requested_source(raw: Mapping[str, Any]) -> RegisteredIngestSource:
    requested_path = str(raw.get("path") or "").strip()
    if not requested_path:
        raise ValueError("Requested source path is required")

    requested_type = str(raw.get("source_type") or "auto").strip().lower()
    if requested_type not in SOURCE_TYPES:
        raise ValueError(f"Unsupported requested source type: {requested_type}")

    path = _resolve_to_accessible_path(requested_path)
    if not path.exists():
        raise FileNotFoundError(f"Requested source path does not exist: {requested_path}")

    source_type = _classify_source(path) if requested_type == "auto" else requested_type
    raw_name = str(raw.get("name") or "").strip()
    name = _source_name(raw_name, path)
    exclude_patterns = _normalize_exclude_patterns(raw.get("exclude_patterns"))
    return RegisteredIngestSource(
        name=name,
        path=path,
        source_type=source_type,
        requested_path=requested_path,
        registered_at=datetime.now(UTC).isoformat(),
        exclude_patterns=exclude_patterns,
    )


def _registry_path() -> Path:
    return Path(settings.paths.data_dir) / _REGISTRY_FILENAME


def _load_registry() -> list[dict[str, str]]:
    path = _registry_path()
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(payload, list):
        return []
    return [
        {str(key): str(value) for key, value in item.items()}
        for item in payload
        if isinstance(item, dict)
    ]


def _save_registry(records: Iterable[Mapping[str, str]]) -> None:
    path = _registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = sorted((dict(record) for record in records), key=lambda item: item.get("path", ""))
    tmp = path.with_suffix(f"{path.suffix}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _normalize_exclude_patterns(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    patterns: list[str] = []
    seen: set[str] = set()
    for item in value[:100]:
        text = str(item or "").strip().strip("/")
        if not text or text in seen:
            continue
        patterns.append(text)
        seen.add(text)
    return tuple(patterns)


def _decode_exclude_patterns(value: Any) -> tuple[str, ...]:
    if isinstance(value, list):
        return _normalize_exclude_patterns(value)
    if not isinstance(value, str) or not value.strip():
        return ()
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return ()
    return _normalize_exclude_patterns(decoded)


def _resolve_to_accessible_path(requested_path: str) -> Path:
    candidates = _candidate_paths(requested_path)
    mount_root = _host_mount_root()
    mount_exists = mount_root.exists()
    for candidate in candidates:
        expanded = candidate.expanduser()
        if not expanded.exists():
            continue
        resolved = expanded.resolve()
        if mount_exists and not _is_under(resolved, mount_root):
            continue
        return resolved
    raise FileNotFoundError(f"Requested source path is not accessible to RAG: {requested_path}")


def _candidate_paths(requested_path: str) -> list[Path]:
    text = requested_path.strip()
    mount_root = _host_mount_root()
    candidates: list[Path] = []

    if text.startswith("~/"):
        candidates.append(mount_root / text[2:])
    if text == "~":
        candidates.append(mount_root)
    if text.startswith("/host_home/"):
        candidates.append(mount_root / text.removeprefix("/host_home/"))
    if text == "/host_home":
        candidates.append(mount_root)

    raw = Path(text)
    host_root = _host_source_root()
    if raw.is_absolute() and host_root is not None:
        try:
            relative = raw.relative_to(host_root)
        except ValueError:
            pass
        else:
            candidates.append(mount_root / relative)

    if raw.is_absolute():
        candidates.append(raw)
    else:
        candidates.append(mount_root / raw)

    deduped: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate)
        if key not in seen:
            deduped.append(candidate)
            seen.add(key)
    return deduped


def _host_mount_root() -> Path:
    return Path(os.environ.get("AI_RAG_HOST_HOME", "/app/sources/host_home")).expanduser()


def _host_source_root() -> Path | None:
    raw = os.environ.get("AI_RAG_HOST_SOURCE_ROOT", "").strip()
    if not raw:
        return None
    return Path(raw).expanduser()


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _classify_source(path: Path) -> str:
    if path.is_dir() and (path / ".obsidian").is_dir():
        return "vault"
    if path.is_dir() and ((path / ".git").is_dir() or (path / ".git").is_file()):
        return "code"
    return "document"


def _source_name(raw_name: str, path: Path) -> str:
    name = raw_name or path.name or "source"
    return " ".join(name.split())[:120]
