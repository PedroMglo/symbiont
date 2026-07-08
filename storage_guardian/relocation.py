"""Managed filesystem relocation to external storage."""

from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from storage_guardian.config import StorageGuardianConfig
from storage_guardian.fallback import external_available, is_external_path, pending_relocation_root
from storage_guardian.path_safety import safe_child_path, safe_path_under_root, safe_relative_path


@dataclass(frozen=True)
class PathStats:
    exists: bool
    is_dir: bool
    files_count: int
    size_bytes: int


class RelocationError(RuntimeError):
    """Raised when a relocation request violates safety policy."""


def relocate_paths(
    config: StorageGuardianConfig,
    paths: list[str | Path],
    *,
    requested_by: str = "system",
    target_root: str | Path | None = None,
    replace_with: str = "symlink",
    dry_run: bool = False,
) -> dict[str, Any]:
    if replace_with not in {"symlink", "none"}:
        raise ValueError("replace_with must be 'symlink' or 'none'")
    relocation_cfg = config.root.get("relocation", {})
    if not relocation_cfg.get("enabled", True):
        raise RelocationError("relocation is disabled")

    root = _target_root(config, target_root)
    requests = [_plan_path(config, Path(path), root, replace_with=replace_with) for path in paths]
    total_size = sum(item["source_stats"]["size_bytes"] for item in requests)

    result: dict[str, Any] = {
        "status": "dry_run" if dry_run else "completed",
        "requested_by": requested_by,
        "target_root": str(root),
        "replace_with": replace_with,
        "paths_requested": [str(path) for path in paths],
        "total_size_bytes": total_size,
        "items": requests,
    }
    if dry_run:
        return result

    manifests: list[dict[str, Any]] = []
    for item in requests:
        manifests.append(_execute_item(config, item, requested_by=requested_by))
    result["items"] = manifests
    result["relocations_created"] = len(manifests)
    return result


def _target_root(config: StorageGuardianConfig, override: str | Path | None) -> Path:
    if override:
        return _target_root_override(config, override)
    relocation_cfg = config.root.get("relocation", {})
    configured = relocation_cfg.get("target_root")
    if configured:
        return Path(str(configured)).expanduser().resolve()
    external = config.root.get("placement", {}).get("external_ssd", {})
    mount = Path(str(external.get("mount_path", config.project_root / "relocated"))).expanduser().resolve()
    return mount / "relocated" / config.project_name


def _target_root_override(config: StorageGuardianConfig, override: str | Path) -> Path:
    raw = str(override).strip().replace("\\", "/")
    if not raw or "\x00" in raw or raw.startswith("~"):
        raise RelocationError("target_root is invalid")
    if not os.path.isabs(raw):
        raise RelocationError("target_root must be absolute")
    target_root = Path(os.path.realpath(os.path.abspath(raw)))
    if target_root.is_relative_to(config.project_root.resolve()):
        raise RelocationError("target_root must be outside project root")
    return target_root


def _plan_path(config: StorageGuardianConfig, raw_path: str | Path, target_root: Path, *, replace_with: str) -> dict[str, Any]:
    source = safe_path_under_root(config.project_root, raw_path, field_name="source_path")
    _validate_source(config, source)
    project_root = config.project_root.resolve()
    relative = source.relative_to(project_root)
    target = safe_child_path(target_root, relative.as_posix(), field_name="target_path")
    _validate_target(config, source, target, target_root)
    source_stats = _stats(source)
    target_stats = _stats(target)
    status = "planned"
    if source.is_symlink():
        status = "already_symlink"
    elif target.exists():
        status = "target_exists"
    return {
        "status": status,
        "source_path": str(source),
        "target_path": str(target),
        "target_root": str(target_root),
        "relative_path": relative.as_posix(),
        "replace_with": replace_with,
        "source_stats": source_stats.__dict__,
        "target_stats": target_stats.__dict__,
    }


def _execute_item(config: StorageGuardianConfig, item: dict[str, Any], *, requested_by: str) -> dict[str, Any]:
    relative = safe_relative_path(str(item["relative_path"]), field_name="relative_path")
    source = safe_child_path(config.project_root, relative.as_posix(), field_name="source_path")
    target_root = _target_root_override(config, str(item["target_root"]))
    intended_target = safe_child_path(target_root, relative.as_posix(), field_name="target_path")
    target = intended_target
    if item["status"] == "already_symlink":
        return item | {"manifest_path": None, "executed": False}
    if item["status"] == "target_exists":
        raise RelocationError(f"target already exists: {target}")

    before = _stats(source)
    fallback_used = False
    fallback_reason = None
    if is_external_path(config, target) and not external_available(config):
        target = _fallback_target(config, item)
        fallback_used = True
        fallback_reason = "external_storage_unavailable"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))
    except OSError as exc:
        if not source.exists():
            raise RelocationError(f"relocation failed after source moved for {source}: {exc}") from exc
        target = _fallback_target(config, item)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))
        fallback_used = True
        fallback_reason = f"external_storage_error: {exc}"
    if item["replace_with"] == "symlink":
        os.symlink(target, source)
    after = _stats(target)
    if before.files_count != after.files_count or before.size_bytes != after.size_bytes:
        raise RelocationError(f"relocation verification failed for {source}")

    manifest = {
        "relocation_id": f"relocate_{time.strftime('%Y%m%d_%H%M%S')}_{time.time_ns()}",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "requested_by": requested_by,
        "source_path": str(source),
        "target_path": str(target),
        "intended_target_path": str(intended_target),
        "relative_path": item["relative_path"],
        "replace_with": item["replace_with"],
        "fallback_used": fallback_used,
        "fallback_reason": fallback_reason,
        "pending_external_transfer": fallback_used,
        "source_is_symlink": source.is_symlink(),
        "verified": True,
        "before": before.__dict__,
        "after": after.__dict__,
    }
    manifest_path = _write_manifest(config, manifest)
    manifest["manifest_path"] = str(manifest_path)
    return manifest


def _write_manifest(config: StorageGuardianConfig, manifest: dict[str, Any]) -> Path:
    root = config.data_root / "relocations"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{manifest['relocation_id']}.json"
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    return path


def _fallback_target(config: StorageGuardianConfig, item: dict[str, Any]) -> Path:
    relative = safe_relative_path(str(item["relative_path"]), field_name="relative_path")
    return safe_child_path(pending_relocation_root(config), relative.as_posix(), field_name="relative_path")


def _validate_source(config: StorageGuardianConfig, source: Path) -> None:
    if not source.exists() and not source.is_symlink():
        raise RelocationError(f"source does not exist: {source}")
    comparable = source.absolute() if source.is_symlink() else source.resolve()
    project_root = config.project_root.resolve()
    if not comparable.is_relative_to(project_root):
        raise RelocationError(f"source is outside project root: {source}")
    if comparable == project_root:
        raise RelocationError("refusing to relocate project root")
    protected = _protected_paths(config)
    for protected_path in protected:
        if comparable == protected_path or comparable.is_relative_to(protected_path):
            raise RelocationError(f"refusing to relocate protected path: {source}")


def _validate_target(config: StorageGuardianConfig, source: Path, target: Path, target_root: Path) -> None:
    if target == source:
        raise RelocationError("target equals source")
    if target.is_relative_to(source):
        raise RelocationError("target cannot be inside source")
    if not target.is_relative_to(target_root):
        raise RelocationError("target escaped target root")
    local_root = config.project_root.resolve()
    if target.is_relative_to(local_root):
        raise RelocationError("target must be outside project root")


def _protected_paths(config: StorageGuardianConfig) -> tuple[Path, ...]:
    relocation_cfg = config.root.get("relocation", {})
    configured = relocation_cfg.get("protected_paths") or []
    defaults = [".git", ".agents", ".codex", "storage_guardian", "config"]
    paths = [Path(str(item)) for item in [*defaults, *configured]]
    resolved: list[Path] = []
    for path in paths:
        resolved.append((config.project_root / path).resolve() if not path.is_absolute() else path.resolve())
    resolved.append(config.data_root.resolve())
    service_root = Path(str(config.root.get("identity", {}).get("service_root", config.project_root / "storage_guardian")))
    resolved.append(service_root.resolve())
    return tuple(resolved)


def _stats(path: Path) -> PathStats:
    if path.is_symlink():
        return PathStats(exists=True, is_dir=False, files_count=1, size_bytes=0)
    if not path.exists():
        return PathStats(exists=False, is_dir=False, files_count=0, size_bytes=0)
    if path.is_file():
        return PathStats(exists=True, is_dir=False, files_count=1, size_bytes=path.stat().st_size)
    files_count = 0
    size = 0
    for item in path.rglob("*"):
        if item.is_symlink():
            files_count += 1
            continue
        if item.is_file():
            files_count += 1
            size += item.stat().st_size
    return PathStats(exists=True, is_dir=True, files_count=files_count, size_bytes=size)
