"""Shared domain types for storage_guardian."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

LifecycleTier = Literal["hot", "warm", "cold"]
LifecycleState = Literal[
    "seen",
    "hot",
    "warm_candidate",
    "warm_archived",
    "cold_candidate",
    "cold_archived",
    "duplicate_logical_reference",
    "snapshot_required",
    "snapshot_archived",
    "catalog_only",
    "blocked_live_storage",
    "blocked_unregistered_path",
    "blocked_safety_policy",
    "restore_available",
    "missing_store_path",
]
StorageTargetKind = Literal["local", "external_ssd"]


@dataclass(frozen=True)
class StoreConfig:
    name: str
    enabled: bool
    path: Path
    owner: str
    type: str
    mode: str
    policy: str
    placement: str = "inherit"
    service: str = "unknown"


@dataclass(frozen=True)
class PolicyConfig:
    name: str
    values: dict[str, Any]

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)


@dataclass(frozen=True)
class FileRecord:
    file_id: str
    store: StoreConfig
    absolute_path: Path
    relative_path: str
    extension: str
    size_bytes: int
    modified_at: float
    accessed_at: float
    created_at: float
    effective_age_days: float
    detected_type: str
    input_kind: str
    lifecycle_state: LifecycleState = "seen"
    content_hash: str | None = None
    processed_at: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StorageTarget:
    kind: StorageTargetKind
    archive_root: Path
    data_root: Path
    fallback_used: bool
    selection_reason: str


@dataclass(frozen=True)
class ArchivePlan:
    archive_id: str
    store: StoreConfig
    tier: Literal["warm", "cold"]
    backend: str
    target: StorageTarget
    files: tuple[FileRecord, ...]
    original_size_bytes: int
    estimated_archive_size_bytes: int
    policy_snapshot: dict[str, Any]


@dataclass(frozen=True)
class SkipDecision:
    file: FileRecord
    reason: str
    state: LifecycleState


@dataclass(frozen=True)
class CyclePlan:
    cycle_id: str
    files: tuple[FileRecord, ...]
    archive_plans: tuple[ArchivePlan, ...]
    skipped: tuple[SkipDecision, ...]
    events: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class ArchiveResult:
    archive_id: str
    archive_path: Path
    manifest_path: Path
    summary_path: Path
    filelist_path: Path
    verify_path: Path
    original_size_bytes: int
    archive_size_bytes: int
    files_count: int
    archive_hash: str
    verified: bool
    backend: str
    storage_target: StorageTarget
