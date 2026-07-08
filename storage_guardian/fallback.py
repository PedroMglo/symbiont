"""External-storage fallback and pending-sync helpers."""

from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from storage_guardian.config import StorageGuardianConfig
from storage_guardian.index import StorageIndex
from storage_guardian.storage_schema import schema_pending_store_root
from storage_guardian.types import StoreConfig

LOCAL_FALLBACK_ROOT = Path(".local") / "storage" / "storage_guardian"


@dataclass(frozen=True)
class StoreLocation:
    root: Path
    fallback_used: bool
    reason: str
    intended_root: Path


def local_fallback_root(config: StorageGuardianConfig) -> Path:
    cfg = config.root.get("local_fallback", {})
    configured = cfg.get("root")
    return Path(str(configured)).expanduser().resolve() if configured else (config.project_root / LOCAL_FALLBACK_ROOT).resolve()


def pending_external_root(config: StorageGuardianConfig) -> Path:
    cfg = config.root.get("local_fallback", {})
    configured = cfg.get("pending_external_root")
    if configured:
        return Path(str(configured)).expanduser().resolve()
    return local_fallback_root(config) / "pending_external"


def pending_store_root(config: StorageGuardianConfig, store: StoreConfig) -> Path:
    return schema_pending_store_root(config.root, pending_external_root(config), store)


def pending_store_root_candidates(config: StorageGuardianConfig, store: StoreConfig) -> tuple[Path, ...]:
    """Return managed pending roots that may contain already-registered objects.

    The canonical pending root comes from ``local_fallback.pending_external_root``.
    Existing durable objects can survive config/root migrations, so the gateway
    also accepts pending roots derived from the configured storage bind root. The
    service/store layout remains enforced; this is not a generic filesystem
    escape hatch.
    """

    roots: list[Path] = [pending_store_root(config, store)]
    for env_key in ("AI_STORAGE_GUARDIAN_ROOT", "AI_STORAGE_CONTAINER_BIND_ROOT"):
        raw = os.environ.get(env_key, "").strip()
        if not raw:
            continue
        candidate_root = Path(raw).expanduser().resolve() / "storage_guardian" / "pending_external"
        roots.append(schema_pending_store_root(config.root, candidate_root, store))

    unique: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        resolved = root.expanduser().resolve()
        key = str(resolved)
        if key in seen:
            continue
        seen.add(key)
        unique.append(resolved)
    return tuple(unique)


def pending_archive_root(config: StorageGuardianConfig) -> Path:
    return pending_external_root(config) / "archives"


def pending_relocation_root(config: StorageGuardianConfig) -> Path:
    return pending_external_root(config) / "relocations"


def external_mount_path(config: StorageGuardianConfig) -> Path | None:
    external = config.root.get("placement", {}).get("external_ssd", {})
    mount = external.get("mount_path")
    return Path(str(mount)).expanduser().resolve() if mount else None


def external_available(config: StorageGuardianConfig) -> bool:
    external = config.root.get("placement", {}).get("external_ssd", {})
    mount = external_mount_path(config)
    if mount is None or not mount.exists():
        return False
    if external.get("require_mount", False) and not mount.is_mount():
        return False
    if external.get("require_writable", True) and not os.access(mount, os.W_OK):
        return False
    return True


def is_external_path(config: StorageGuardianConfig, path: Path) -> bool:
    mount = external_mount_path(config)
    if mount is None:
        return False
    return _is_relative_to(path.expanduser().resolve(), mount)


def ensure_local_fallback_roots(config: StorageGuardianConfig) -> None:
    root = local_fallback_root(config)
    root.mkdir(parents=True, exist_ok=True)


def ensure_external_fixed_roots(config: StorageGuardianConfig) -> dict[str, Any]:
    ensure_local_fallback_roots(config)
    if not external_available(config):
        return {"available": False, "created": []}

    created: list[str] = []
    placement = config.root.get("placement", {})
    external = placement.get("external_ssd", {})
    relocation = config.root.get("relocation", {})
    roots = [
        Path(str(external["mount_path"])).expanduser().resolve(),
        Path(str(external["archive_root"])).expanduser().resolve(),
        Path(str(external.get("data_mirror_root", Path(str(external["archive_root"])).parent / "data"))).expanduser().resolve(),
        Path(str(relocation.get("target_root", Path(str(external["mount_path"])) / "relocated" / config.project_name))).expanduser().resolve(),
        Path(str(external["mount_path"])).expanduser().resolve()
        / ".storage_guardian"
        / "schemas"
        / f"v{config.root['storage_schema']['version']}",
    ]
    roots.extend(store.path for store in config.stores if is_external_path(config, store.path))
    for root in roots:
        root.mkdir(parents=True, exist_ok=True)
        created.append(str(root))
    return {"available": True, "created": sorted(set(created))}


def location_for_store(config: StorageGuardianConfig, store: StoreConfig) -> StoreLocation:
    intended = store.path.expanduser().resolve()
    if is_external_path(config, intended) and not external_available(config):
        root = pending_store_root(config, store)
        root.mkdir(parents=True, exist_ok=True)
        return StoreLocation(root=root, fallback_used=True, reason="external_storage_unavailable", intended_root=intended)
    try:
        intended.mkdir(parents=True, exist_ok=True)
        probe = intended / ".storage_guardian_probe"
        probe.write_text("ok\n", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return StoreLocation(root=intended, fallback_used=False, reason="primary_store_available", intended_root=intended)
    except OSError:
        root = pending_store_root(config, store)
        root.mkdir(parents=True, exist_ok=True)
        return StoreLocation(root=root, fallback_used=True, reason="primary_store_unwritable", intended_root=intended)


def relative_to_store_location(config: StorageGuardianConfig, store: StoreConfig, path: Path) -> str:
    resolved = path.expanduser().resolve()
    if _is_relative_to(resolved, store.path):
        return resolved.relative_to(store.path.resolve()).as_posix()
    fallback = pending_store_root(config, store)
    if _is_relative_to(resolved, fallback):
        return resolved.relative_to(fallback).as_posix()
    return resolved.relative_to(config.data_root.resolve()).as_posix()


def store_for_fallback_path(config: StorageGuardianConfig, path: Path) -> StoreConfig | None:
    resolved = path.expanduser().resolve()
    matches = [
        store
        for store in config.stores
        if store.enabled
        and _is_relative_to(resolved, pending_store_root(config, store))
    ]
    if not matches:
        return None
    return max(matches, key=lambda store: len(str(pending_store_root(config, store))))


def sync_pending_external(
    config: StorageGuardianConfig,
    index: StorageIndex | None = None,
    *,
    max_items: int | None = None,
    max_bytes: int | None = None,
) -> dict[str, Any]:
    ensure_local_fallback_roots(config)
    if not external_available(config):
        return {
            "status": "external_unavailable",
            "pending_root": str(pending_external_root(config)),
            "moved": 0,
            "moved_bytes": 0,
            "items": [],
            "skipped": [],
        }

    ensure_external_fixed_roots(config)
    items: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    moved = 0
    moved_bytes = 0
    budget_exhausted = False
    for store in config.stores:
        if budget_exhausted:
            break
        for source_root in _pending_source_roots(config, store):
            if not source_root.exists():
                continue
            for source in sorted(source_root.rglob("*")):
                if source.is_dir() or source.is_symlink():
                    continue
                size = source.stat().st_size
                if max_items is not None and moved >= max_items:
                    budget_exhausted = True
                if max_bytes is not None and moved_bytes + size > max_bytes:
                    budget_exhausted = True
                if budget_exhausted:
                    skipped.append(
                        {
                            "store": store.name,
                            "source_path": str(source),
                            "relative_path": source.relative_to(source_root).as_posix(),
                            "size_bytes": size,
                            "reason": "automatic_sync_budget_exhausted",
                        }
                    )
                    break
                relative = source.relative_to(source_root)
                target = _non_conflicting(store.path / relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(target))
                os.symlink(target, source)
                moved += 1
                moved_bytes += size
                item = {
                    "store": store.name,
                    "service": store.service,
                    "source_path": str(source),
                    "target_path": str(target),
                    "relative_path": relative.as_posix(),
                    "size_bytes": size,
                }
                _update_index_object(index, source, target, store, relative)
                items.append(item)
            if budget_exhausted:
                break
    relocation_items = [] if budget_exhausted else _sync_pending_relocations(config)
    items.extend(relocation_items)
    moved += len(relocation_items)
    _write_sync_manifest(config, items)
    if index is not None and moved:
        index.insert_event(
            "storage_guardian_pending_sync",
            "pending_external_synced",
            "pending local external-storage objects synced",
            {"moved": moved, "items": items},
        )
        index.commit()
    _remove_empty_pending_dirs(pending_external_root(config))
    return {
        "status": "partial_budget_exhausted" if budget_exhausted else "completed",
        "pending_root": str(pending_external_root(config)),
        "moved": moved,
        "moved_bytes": moved_bytes,
        "items": items,
        "skipped": skipped,
    }


def _sync_pending_relocations(config: StorageGuardianConfig) -> list[dict[str, Any]]:
    manifest_root = config.data_root / "relocations"
    if not manifest_root.exists():
        return []
    items: list[dict[str, Any]] = []
    for manifest_path in sorted(manifest_root.glob("*.json")):
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if not payload.get("fallback_used") or payload.get("synced_to_external"):
            continue
        source = Path(str(payload.get("target_path", ""))).expanduser().resolve()
        target = Path(str(payload.get("intended_target_path", ""))).expanduser().resolve()
        original = Path(str(payload.get("source_path", ""))).expanduser()
        if not source.exists() or not target:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        final_target = _non_conflicting(target)
        shutil.move(str(source), str(final_target))
        if original.is_symlink():
            original.unlink()
            os.symlink(final_target, original)
        payload.update(
            {
                "target_path": str(final_target),
                "fallback_synced_from": str(source),
                "synced_to_external": True,
                "synced_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "pending_external_transfer": False,
            }
        )
        manifest_path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
        items.append(
            {
                "kind": "relocation",
                "manifest_path": str(manifest_path),
                "source_path": str(source),
                "target_path": str(final_target),
                "original_path": str(original),
            }
        )
    return items


def _pending_source_roots(config: StorageGuardianConfig, store: StoreConfig) -> list[Path]:
    return [pending_store_root(config, store)]


def _update_index_object(index: StorageIndex | None, source: Path, target: Path, store: StoreConfig, relative: Path) -> None:
    if index is None:
        return
    for record in index.list_storage_objects():
        if Path(str(record.get("current_path", ""))).expanduser().resolve() != source.resolve():
            continue
        updated = dict(record)
        updated.update(
            {
                "store_id": store.name,
                "updated_at": time.time(),
                "current_path": str(target),
                "relative_path": relative.as_posix(),
                "metadata": dict(record.get("metadata") or {}) | {"synced_from_local_fallback": str(source)},
            }
        )
        index.upsert_storage_object(updated)


def _write_sync_manifest(config: StorageGuardianConfig, items: list[dict[str, Any]]) -> None:
    if not items:
        return
    root = local_fallback_root(config) / "manifests" / "pending_sync"
    root.mkdir(parents=True, exist_ok=True)
    payload = {
        "sync_id": f"sync_{time.strftime('%Y%m%d_%H%M%S')}_{time.time_ns()}",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "items": items,
    }
    path = root / f"{payload['sync_id']}.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _non_conflicting(path: Path) -> Path:
    if not path.exists() and not path.is_symlink():
        return path
    return path.with_name(f"{path.stem}_{time.time_ns()}{path.suffix}")


def _remove_empty_pending_dirs(root: Path) -> None:
    if not root.exists():
        return
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if not path.is_dir():
            continue
        try:
            path.rmdir()
        except OSError:
            continue


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False
