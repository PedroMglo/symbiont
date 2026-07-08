"""Explicit user-requested archive planning."""

from __future__ import annotations

import hashlib
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from storage_guardian.classifier import backend_for, classify_path
from storage_guardian.config import StorageGuardianConfig
from storage_guardian.hashing import hash_file
from storage_guardian.lifecycle_math import compression_aggression
from storage_guardian.path_safety import safe_path_under_root
from storage_guardian.placement import PlacementSelector
from storage_guardian.safety import SafetyGate
from storage_guardian.types import ArchivePlan, CyclePlan, FileRecord, SkipDecision, StorageTarget, StoreConfig


def build_explicit_archive_plan(
    config: StorageGuardianConfig,
    paths: list[str | Path],
    *,
    tier: str = "cold",
    requested_by: str = "orcai",
    placement_mode: str = "configured",
    replace_sources: bool = False,
) -> CyclePlan:
    """Build a lifecycle plan for paths explicitly requested by a user."""

    if tier not in {"warm", "cold"}:
        raise ValueError("tier must be 'warm' or 'cold'")

    safety = SafetyGate(config)
    if placement_mode not in {"configured", "source_directory"}:
        raise ValueError("placement_mode must be 'configured' or 'source_directory'")

    placement_selector = PlacementSelector(config)
    files: list[tuple[FileRecord, Path | None]] = []
    skipped: list[SkipDecision] = []
    events: list[dict[str, Any]] = []

    for requested_path in paths:
        root = safe_path_under_root(config.project_root, requested_path, field_name="archive_path")
        if not root.exists():
            skipped.append(_missing_path_skip(config, root))
            continue
        for path, source_archive_root in _iter_files(root):
            store = _store_for_path(config, path)
            if store is None:
                skipped.append(_unregistered_path_skip(config, path))
                continue
            record = _file_record(config, store, path, tier=tier, requested_by=requested_by)
            allowed, reason = safety.evaluate_file(record)
            if allowed:
                files.append((record, source_archive_root if placement_mode == "source_directory" else None))
            else:
                skipped.append(SkipDecision(record, reason, _state_for_reason(reason)))

    grouped: dict[tuple[str, str, str], list[FileRecord]] = defaultdict(list)
    archive_roots: dict[str, Path] = {}
    for record, source_archive_root in files:
        policy = config.policy_for(record.store)
        backend = backend_for(policy, tier, record.detected_type, record.extension)
        root_key = str(source_archive_root or "")
        if source_archive_root is not None:
            archive_roots[root_key] = source_archive_root
        grouped[(record.store.name, backend, root_key)].append(record)

    archive_plans: list[ArchivePlan] = []
    for (_store_name, backend, root_key), group in grouped.items():
        store = group[0].store
        original_size = sum(item.size_bytes for item in group)
        estimated_size = _estimate_archive_size(original_size, tier, backend)
        target = (
            _source_directory_target(config, archive_roots[root_key])
            if root_key
            else placement_selector.select(store, estimated_size)
        )
        policy = config.policy_for(store)
        max_age = max(item.effective_age_days for item in group)
        policy_snapshot = dict(policy.values)
        policy_snapshot.update(
            {
                "compression_aggression": compression_aggression(max_age, config.hot_until_days, config.cold_after_days),
                "explicit_request": True,
                "requested_by": requested_by,
                "archive_layout": "source_directory" if root_key else "managed_tier_store",
                "delete_original_sources_override": bool(replace_sources),
            }
        )
        archive_plans.append(
            ArchivePlan(
                archive_id=_archive_id(store.name, tier, backend, group),
                store=store,
                tier=tier,  # type: ignore[arg-type]
                backend=backend,
                target=target,
                files=tuple(group),
                original_size_bytes=original_size,
                estimated_archive_size_bytes=estimated_size,
                policy_snapshot=policy_snapshot,
            )
        )
        events.append(
            {
                "event_type": "explicit_archive_plan_created",
                "requested_by": requested_by,
                "store": store.name,
                "tier": tier,
                "backend": backend,
                "files_count": len(group),
                "storage_target": target.kind,
            }
        )

    cycle_id = f"manual_{_safe(requested_by)}_{_timestamp_slug(compact=True)}"
    return CyclePlan(cycle_id=cycle_id, files=tuple(record for record, _root in files), archive_plans=tuple(archive_plans), skipped=tuple(skipped), events=tuple(events))


def _iter_files(path: Path) -> tuple[tuple[Path, Path], ...]:
    if path.is_file():
        return ((path, path.parent),)
    files: list[tuple[Path, Path]] = []
    for item in path.rglob("*"):
        if item.is_file() and not _is_internal_skip(item):
            files.append((item, path))
    return tuple(files)


def _source_directory_target(config: StorageGuardianConfig, archive_root: Path) -> StorageTarget:
    return StorageTarget(
        kind="local",
        archive_root=archive_root,
        data_root=config.data_root,
        fallback_used=False,
        selection_reason="user_requested_source_directory",
    )


def _file_record(config: StorageGuardianConfig, store: StoreConfig, path: Path, *, tier: str, requested_by: str) -> FileRecord:
    stat = path.stat()
    policy = config.policy_for(store)
    detected_type, input_kind = classify_path(path, policy)
    relative_path = path.relative_to(store.path).as_posix()
    content_hash = hash_file(path) if config.root.get("manifests", {}).get("include_hashes", True) else None
    forced_age = config.cold_after_days + 1 if tier == "cold" else config.hot_until_days + 1
    actual_age = max(0.0, (time.time() - max(stat.st_atime, stat.st_mtime)) / 86400)
    effective_age = max(actual_age, forced_age)
    return FileRecord(
        file_id=_file_id(store.name, relative_path, content_hash, stat.st_size, stat.st_mtime),
        store=store,
        absolute_path=path,
        relative_path=relative_path,
        extension=path.suffix.lower(),
        size_bytes=stat.st_size,
        modified_at=stat.st_mtime,
        accessed_at=stat.st_atime,
        created_at=stat.st_ctime,
        effective_age_days=effective_age,
        detected_type=detected_type,
        input_kind=input_kind,
        lifecycle_state="cold_candidate" if tier == "cold" else "warm_candidate",
        content_hash=content_hash,
        metadata={"explicit_request": True, "requested_by": requested_by},
    )


def _store_for_path(config: StorageGuardianConfig, path: Path) -> StoreConfig | None:
    matches = [store for store in config.stores if store.enabled and _is_relative_to(path, store.path)]
    if not matches:
        return None
    return max(matches, key=lambda store: len(store.path.parts))


def _missing_path_skip(config: StorageGuardianConfig, path: Path) -> SkipDecision:
    store = StoreConfig(
        name="unregistered",
        enabled=False,
        path=config.project_root,
        owner="unknown",
        type="unknown",
        mode="managed",
        policy=next(iter(config.policies), "mixed_policy"),
    )
    record = FileRecord(
        file_id=_file_id("missing", str(path), None, 0, 0.0),
        store=store,
        absolute_path=path,
        relative_path=str(path),
        extension=path.suffix.lower(),
        size_bytes=0,
        modified_at=0.0,
        accessed_at=0.0,
        created_at=0.0,
        effective_age_days=0.0,
        detected_type="missing",
        input_kind="missing",
        lifecycle_state="missing_store_path",
    )
    return SkipDecision(record, "missing_path", "missing_store_path")


def _unregistered_path_skip(config: StorageGuardianConfig, path: Path) -> SkipDecision:
    skip = _missing_path_skip(config, path)
    return SkipDecision(skip.file, "blocked_unregistered_path", "blocked_unregistered_path")


def _archive_id(store_name: str, tier: str, backend: str, files: list[FileRecord]) -> str:
    payload = "\n".join(f"{item.relative_path}:{item.content_hash}:{item.size_bytes}" for item in files).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()[:12]
    stamp = _timestamp_slug()
    return f"manual_{_safe(store_name)}_{tier}_{_safe(backend)}_{stamp}_{digest}"


def _timestamp_slug(*, compact: bool = False) -> str:
    fmt = "%Y%m%d_%H%M%S" if compact else "%Y_%m_%d_%H%M%S"
    return f"{time.strftime(fmt)}_{time.time_ns()}"


def _file_id(store_name: str, relative_path: str, content_hash: str | None, size: int, mtime: float) -> str:
    payload = f"{store_name}\0{relative_path}\0{content_hash or ''}\0{size}\0{mtime}\0manual".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _safe(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in value)


def _estimate_archive_size(original_size: int, tier: str, backend: str) -> int:
    if backend == "passthrough":
        return original_size
    factor = 0.65 if tier == "warm" else 0.45
    if backend == "sevenzip":
        factor -= 0.08
    return max(1, int(original_size * factor))


def _state_for_reason(reason: str) -> str:
    if reason == "catalog_only":
        return "catalog_only"
    if reason == "blocked_live_storage":
        return "blocked_live_storage"
    if reason == "blocked_unregistered_path":
        return "blocked_unregistered_path"
    return "blocked_safety_policy"


def _is_internal_skip(path: Path) -> bool:
    parts = set(path.parts)
    return "__pycache__" in parts or ".git" in parts or ".pytest_cache" in parts


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False
