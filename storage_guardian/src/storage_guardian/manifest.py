"""Manifest, filelist and summary writers."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from storage_guardian.hashing import hash_file
from storage_guardian.types import ArchivePlan, ArchiveResult


def artifact_paths(plan: ArchivePlan, data_root: Path) -> dict[str, Path]:
    return {
        "manifest": data_root / "manifests" / f"{plan.archive_id}.manifest.json",
        "summary": data_root / "summaries" / f"{plan.archive_id}.summary.md",
        "filelist": data_root / "filelists" / f"{plan.archive_id}.filelist.txt",
        "verify": data_root / "verify" / f"{plan.archive_id}.verify.json",
    }


def write_filelist(plan: ArchivePlan, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [record.relative_path for record in plan.files]
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def manifest_payload(
    plan: ArchivePlan,
    archive_path: Path | None,
    archive_hash: str | None,
    verified: bool,
    effective_config: dict[str, Any],
    sources_removed: bool = False,
    removed_sources_count: int = 0,
) -> dict[str, Any]:
    archive_size = archive_path.stat().st_size if archive_path and archive_path.exists() else 0
    reduction = 0.0
    if plan.original_size_bytes:
        reduction = 1 - (archive_size / plan.original_size_bytes)
    return {
        "archive_id": plan.archive_id,
        "project": effective_config.get("identity", {}).get("project_name", "ai-local"),
        "store": plan.store.name,
        "owner": plan.store.owner,
        "tier": plan.tier,
        "policy": plan.store.policy,
        "storage_target": {
            "kind": plan.target.kind,
            "archive_root": str(plan.target.archive_root),
            "fallback_used": plan.target.fallback_used,
            "selection_reason": plan.target.selection_reason,
        },
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "original_size_bytes": plan.original_size_bytes,
        "archive_size_bytes": archive_size,
        "reduction_ratio": reduction,
        "files_count": len(plan.files),
        "compression_backend": plan.backend,
        "effective_config_hash": effective_config.get("effective_config_hash"),
        "archive_path": str(archive_path) if archive_path else None,
        "archive_hash": archive_hash,
        "verified": verified,
        "restore_supported": True,
        "destructive_cleanup_performed": sources_removed,
        "source_lifecycle": {
            "mode": "replaced_by_archive" if sources_removed else "source_present",
            "sources_removed_after_verify": sources_removed,
            "removed_sources_count": removed_sources_count,
            "backup_created": False,
        },
        "policy_snapshot": plan.policy_snapshot,
        "files": [
            {
                "relative_path": record.relative_path,
                "size_bytes": record.size_bytes,
                "content_hash": record.content_hash,
                "detected_type": record.detected_type,
                "effective_age_days": record.effective_age_days,
            }
            for record in plan.files
        ],
    }


def write_manifest(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def write_summary(path: Path, payload: dict[str, Any], skipped_count: int = 0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    top_files = sorted(payload["files"], key=lambda item: item["size_bytes"], reverse=True)[:10]
    lines = [
        f"# {payload['archive_id']}",
        "",
        f"- store: {payload['store']}",
        f"- owner: {payload['owner']}",
        f"- tier: {payload['tier']}",
        f"- target: {payload['storage_target']['kind']}",
        f"- original_size_bytes: {payload['original_size_bytes']}",
        f"- archive_size_bytes: {payload['archive_size_bytes']}",
        f"- reduction_ratio: {payload['reduction_ratio']:.4f}",
        f"- files_count: {payload['files_count']}",
        f"- compression_backend: {payload['compression_backend']}",
        f"- skipped_files_in_cycle: {skipped_count}",
        "",
        "## Largest files",
        "",
    ]
    lines.extend(f"- {item['relative_path']} ({item['size_bytes']} bytes)" for item in top_files)
    lines.append("")
    lines.append("## Restore")
    lines.append("")
    lines.append("Restore is supported only into the configured safe restore root.")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_verify(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def result_from_plan(plan: ArchivePlan, paths: dict[str, Path], archive_path: Path, backend: str, verified: bool) -> ArchiveResult:
    return ArchiveResult(
        archive_id=plan.archive_id,
        archive_path=archive_path,
        manifest_path=paths["manifest"],
        summary_path=paths["summary"],
        filelist_path=paths["filelist"],
        verify_path=paths["verify"],
        original_size_bytes=plan.original_size_bytes,
        archive_size_bytes=archive_path.stat().st_size,
        files_count=len(plan.files),
        archive_hash=hash_file(archive_path),
        verified=verified,
        backend=backend,
        storage_target=plan.target,
    )
