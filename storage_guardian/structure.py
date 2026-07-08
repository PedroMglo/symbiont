"""Authoritative storage-root reconciliation."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from storage_guardian.config import StorageGuardianConfig
from storage_guardian.fallback import (
    external_available,
    external_mount_path,
    local_fallback_root,
    pending_archive_root,
    pending_relocation_root,
    pending_store_root,
)
from storage_guardian.registry_ids import directory_id, parent_directory_id, path_hash
from storage_guardian.storage_schema import schema_services, service_schema_roots

ReconcileScope = Literal["external", "local", "all"]


@dataclass(frozen=True)
class StructurePlan:
    scope: str
    root: Path
    available: bool
    expected_dirs: tuple[Path, ...]
    protected_roots: tuple[Path, ...]
    orphan_dirs: tuple[Path, ...]
    reason: str = ""


def reconcile_structure(
    config: StorageGuardianConfig,
    *,
    index: Any | None = None,
    scope: ReconcileScope = "all",
    apply: bool = False,
) -> dict[str, Any]:
    """Create canonical roots and record structure drift without deleting paths."""

    requested = ("external", "local") if scope == "all" else (scope,)
    plans = [_plan_scope(config, item) for item in requested]
    created: list[str] = []
    directories_registered = 0
    if index is not None:
        _delete_stale_store_directories(config, index)
    for plan in plans:
        if not plan.available:
            continue
        _assert_safe_root(plan.root, config)
        for directory in plan.expected_dirs:
            _assert_within(directory, plan.root)
            existed = directory.exists()
            if apply:
                directory.mkdir(parents=True, exist_ok=True)
            if apply and not existed:
                created.append(str(directory))
            elif not apply:
                created.append(str(directory))
        if index is not None:
            directories_registered += _register_plan(config, index, plan, apply=apply)
    report = {
        "applied": apply,
        "scope": scope,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "plans": [_public_plan(plan) for plan in plans],
        "created_dirs": sorted(set(created)),
        "created_count": len(set(created)) if apply else 0,
        "orphan_dirs": sorted({str(path) for plan in plans for path in plan.orphan_dirs}),
        "orphan_count": sum(len(plan.orphan_dirs) for plan in plans),
        "directories_registered": directories_registered,
    }
    if index is not None:
        _record_reconcile_operation(index, report)
        index.commit()
    if apply:
        _write_report(config, report)
    return report


def _plan_scope(config: StorageGuardianConfig, scope: str) -> StructurePlan:
    if scope == "external":
        root = external_mount_path(config)
        if root is None:
            return StructurePlan(scope, Path("/"), False, (), (), (), "external root is not configured")
        if not external_available(config):
            return StructurePlan(scope, root, False, (), (), (), "external root is not available")
        expected, protected = _external_expected_roots(config, root)
        return StructurePlan(scope, root, True, tuple(sorted(expected)), tuple(sorted(protected)), _orphan_dirs(root, protected))
    root = config.data_root
    expected, protected = _local_expected_roots(config, root)
    return StructurePlan(scope, root, True, tuple(sorted(expected)), tuple(sorted(protected)), _orphan_dirs(root, protected))


def _external_expected_roots(config: StorageGuardianConfig, root: Path) -> tuple[set[Path], set[Path]]:
    external = config.root.get("placement", {}).get("external_ssd", {})
    relocation = config.root.get("relocation", {})
    protected: set[Path] = {
        root / ".storage_guardian" / "schemas" / f"v{config.root['storage_schema']['version']}",
        Path(str(external["archive_root"])).expanduser().resolve(),
        Path(str(external.get("data_mirror_root", root / "data" / "storage_guardian"))).expanduser().resolve(),
        Path(str(relocation.get("target_root", root / "relocated" / config.project_name))).expanduser().resolve(),
    }
    for item in service_schema_roots(config.root):
        env_key = item.get("env_key")
        if not env_key:
            continue
        resolved = _find_env_path(config, env_key)
        if resolved is not None and _is_relative_to(resolved, root):
            protected.add(resolved)
    for store in config.stores:
        resolved = store.path.expanduser().resolve()
        if _is_relative_to(resolved, root):
            protected.add(resolved)
    expected = set(protected) | _ancestors(root, protected)
    return expected, protected


def _local_expected_roots(config: StorageGuardianConfig, root: Path) -> tuple[set[Path], set[Path]]:
    version_root = root / "schemas" / f"v{config.root['storage_schema']['version']}"
    expected: set[Path] = {
        root,
        root / "objects",
        root / "versions",
        root / "directories",
        root / "manifests",
        root / "archives",
        root / "materialized",
        root / "uploads",
        root / "quarantine",
        root / "restore",
        root / "scratch",
        root / "service_stores",
        root / "indexes",
        root / "state",
        root / "state" / "metrics",
        root / "state" / "logs",
        root / "cache",
        root / "cache" / "temp",
        root / "cache" / "staging",
        root / "cache" / "extraction_cache",
        root / "cache" / "upload_chunks",
        local_fallback_root(config),
        pending_archive_root(config),
        pending_relocation_root(config),
    }
    protected: set[Path] = set(expected)
    for service in schema_services(config.root):
        path = version_root / "services" / service
        expected.add(path)
        protected.add(path)
    for store in config.stores:
        path = pending_store_root(config, store)
        expected.add(path)
        protected.add(path)
        resolved_store = store.path.expanduser().resolve()
        if _is_relative_to(resolved_store, root):
            expected.add(resolved_store)
            protected.add(resolved_store)
    protected.update(_ancestors(root, protected))
    protected.discard(root)
    return expected, protected


def _find_env_path(config: StorageGuardianConfig, env_key: str) -> Path | None:
    for store in config.stores:
        if store.name == env_key:
            return store.path.expanduser().resolve()
    value = _env_lookup(config, env_key)
    return Path(value).expanduser().resolve() if value else None


def _env_lookup(config: StorageGuardianConfig, env_key: str) -> str | None:
    storage_schema = config.root.get("storage_schema", {})
    for service_cfg in storage_schema.get("services", {}).values():
        if not isinstance(service_cfg, dict):
            continue
        for root_cfg in (service_cfg.get("roots") or {}).values():
            if isinstance(root_cfg, dict) and root_cfg.get("env_key") == env_key:
                relative = root_cfg.get("relative_path")
                if relative:
                    external = external_mount_path(config)
                    if external is not None:
                        return str(external / str(relative))
    return None


def _ancestors(root: Path, paths: set[Path]) -> set[Path]:
    ancestors: set[Path] = set()
    for path in paths:
        current = path
        while current != root and _is_relative_to(current, root):
            ancestors.add(current)
            current = current.parent
    ancestors.add(root)
    return ancestors


def _orphan_dirs(root: Path, protected_roots: set[Path]) -> tuple[Path, ...]:
    if not root.exists():
        return ()
    candidates: list[Path] = []
    for path in sorted(root.iterdir()):
        candidates.extend(_candidate_paths(root, path, protected_roots, blocked_by_candidate=()))
    return tuple(candidates)


def _candidate_paths(root: Path, path: Path, protected_roots: set[Path], *, blocked_by_candidate: tuple[Path, ...]) -> list[Path]:
    resolved = path.resolve()
    if any(_is_relative_to(resolved, candidate) for candidate in blocked_by_candidate):
        return []
    if _is_protected_or_parent(resolved, protected_roots):
        if path.is_dir() and not path.is_symlink():
            items: list[Path] = []
            for child in sorted(path.iterdir()):
                items.extend(_candidate_paths(root, child, protected_roots, blocked_by_candidate=blocked_by_candidate))
            return items
        return []
    _assert_within(resolved, root)
    return [resolved]


def _is_protected_or_parent(path: Path, protected_roots: set[Path]) -> bool:
    return any(_is_relative_to(path, protected) or _is_relative_to(protected, path) for protected in protected_roots)


def _path_size(path: Path) -> int:
    if path.is_symlink():
        return 0
    if path.is_file():
        return path.stat().st_size
    if not path.is_dir():
        return 0
    total = 0
    for item in path.rglob("*"):
        if item.is_symlink() or not item.is_file():
            continue
        try:
            total += item.stat().st_size
        except OSError:
            continue
    return total


def _public_plan(plan: StructurePlan) -> dict[str, Any]:
    return {
        "scope": plan.scope,
        "root": str(plan.root),
        "available": plan.available,
        "reason": plan.reason,
        "expected_dirs": [str(path) for path in plan.expected_dirs],
        "orphan_dirs": [str(path) for path in plan.orphan_dirs],
        "orphan_count": len(plan.orphan_dirs),
        "orphan_bytes": sum(_path_size(path) for path in plan.orphan_dirs if path.exists() or path.is_symlink()),
    }


def _register_plan(config: StorageGuardianConfig, index: Any, plan: StructurePlan, *, apply: bool) -> int:
    count = 0
    now = time.time()
    for path in plan.expected_dirs:
        record = _directory_record(config, plan.root, path, expected=True, apply=apply, now=now)
        index.upsert_directory(record)
        count += 1
    for path in plan.orphan_dirs:
        record = _directory_record(config, plan.root, path, expected=False, apply=apply, now=now)
        record["status"] = "orphan"
        record["protected"] = False
        index.upsert_directory(record)
        count += 1
    return count


def _delete_stale_store_directories(config: StorageGuardianConfig, index: Any) -> None:
    for store in config.stores:
        store_root = store.path.expanduser().resolve()
        for row in index.list_directories(store=store.name):
            metadata = row.get("metadata") or {}
            raw_path = metadata.get("path")
            if not raw_path:
                continue
            try:
                registered = Path(str(raw_path)).expanduser().resolve()
            except OSError:
                continue
            if _is_relative_to(registered, store_root):
                continue
            directory_id_value = row.get("directory_id")
            if directory_id_value:
                index.delete_directory(str(directory_id_value))


def _directory_record(
    config: StorageGuardianConfig,
    root: Path,
    path: Path,
    *,
    expected: bool,
    apply: bool,
    now: float,
) -> dict[str, Any]:
    service, store_name, owner, mode, policy = _directory_owner(config, path)
    relative = _relative_directory(root, path)
    exists = path.exists()
    status = "active" if exists else "created" if apply else "missing"
    if not expected:
        status = "orphan"
    rel_path = relative.as_posix()
    return {
        "directory_id": directory_id(service, store_name, rel_path),
        "service": service,
        "store_id": store_name,
        "relative_path": rel_path,
        "parent_directory_id": parent_directory_id(service, store_name, relative),
        "owner": owner,
        "zone": "system",
        "mode": mode,
        "policy": policy,
        "expected_by_schema": expected,
        "status": status,
        "created_by": "schema" if expected else "scanner",
        "created_at": now,
        "last_seen_at": now,
        "protected": expected,
        "writable_by_storage_guardian": expected,
        "readable_by_callers": expected,
        "absolute_path_hash": path_hash(path),
        "metadata": {
            "scope_root": str(root),
            "path": str(path),
            "exists": exists,
            "reconciler": "structure_v2",
        },
    }


def _directory_owner(config: StorageGuardianConfig, path: Path) -> tuple[str, str | None, str, str, str]:
    resolved = path.expanduser().resolve()
    for store in config.stores:
        store_path = store.path.expanduser().resolve()
        if _is_relative_to(resolved, store_path):
            return store.service, store.name, store.owner, store.mode, store.policy
    if _is_relative_to(resolved, config.data_root.expanduser().resolve()):
        return "storage_guardian", None, "storage", "managed", "storage_guardian_policy"
    for root_cfg in service_schema_roots(config.root):
        rel = str(root_cfg.get("relative_path") or "")
        if rel and rel in str(resolved):
            service = str(root_cfg.get("service") or "unknown")
            return service, None, service, str(root_cfg.get("role") or "managed"), "schema_root_policy"
    return "unknown", None, "unknown", "external", "unmanaged"


def _relative_directory(root: Path, path: Path) -> Any:
    try:
        rel = path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        rel = path.name
    if rel in {"", "."}:
        rel = "."
    from pathlib import PurePosixPath

    return PurePosixPath(rel)


def _record_reconcile_operation(index: Any, report: dict[str, Any]) -> None:
    operation_id = f"op_reconcile_{time.time_ns()}"
    custody_event_id = index.insert_custody_event(
        {
            "custody_event_id": f"custody_reconcile_{time.time_ns()}",
            "event_type": "schema_reconciled",
            "actor": "storage_guardian",
            "requesting_service": "storage_guardian",
            "operation_id": operation_id,
            "source_ref": "storage_schema",
            "target_ref": "directory_registry",
            "metadata": report,
        }
    )
    index.insert_operation(
        {
            "operation_id": operation_id,
            "operation_type": "reconcile_structure",
            "actor": "storage_guardian",
            "requesting_service": "storage_guardian",
            "source_ref": "storage_schema",
            "target_ref": "directory_registry",
            "policy_decision": "allowed",
            "dry_run_result": report,
            "preconditions": {"delete_unexpected_paths": False},
            "status": "completed",
            "started_at": time.time(),
            "finished_at": time.time(),
            "custody_event_id": custody_event_id,
            "rollback_plan": {"strategy": "no_delete_reconcile"},
            "metadata": report,
        }
    )


def _write_report(config: StorageGuardianConfig, report: dict[str, Any]) -> None:
    root = config.data_root / "state" / "structure_reconcile"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"reconcile_{time.strftime('%Y%m%d_%H%M%S')}_{time.time_ns()}.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    (root / "last.json").write_text(json.dumps(report, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def _assert_safe_root(root: Path, config: StorageGuardianConfig) -> None:
    resolved = root.expanduser().resolve()
    forbidden = {Path("/").resolve(), Path("/mnt").resolve(), Path.home().resolve(), config.project_root.resolve()}
    if resolved in forbidden or len(resolved.parts) < 4:
        raise RuntimeError(f"refusing to reconcile unsafe root: {resolved}")


def _assert_within(path: Path, root: Path) -> None:
    if not _is_relative_to(path, root):
        raise RuntimeError(f"path escaped reconcile root: {path} not under {root}")


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False
