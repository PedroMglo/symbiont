"""Plan executor."""

from __future__ import annotations

import os
import shutil
import logging
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from storage_guardian.compressors import get_compressor
from storage_guardian.config import StorageGuardianConfig
from storage_guardian.fallback import pending_archive_root
from storage_guardian.hashing import hash_file
from storage_guardian.index import StorageIndex
from storage_guardian.lifecycle_math import sevenzip_level, zstd_level
from storage_guardian.manifest import (
    artifact_paths,
    manifest_payload,
    result_from_plan,
    write_filelist,
    write_manifest,
    write_summary,
    write_verify,
)
from storage_guardian.types import ArchivePlan, ArchiveResult, CyclePlan, StorageTarget
from storage_guardian.verifier import verify_archive

log = logging.getLogger(__name__)


class PlanExecutor:
    def __init__(self, config: StorageGuardianConfig, index: StorageIndex) -> None:
        self.config = config
        self.index = index

    def execute(self, plan: CyclePlan, effective_config: dict[str, Any]) -> tuple[ArchiveResult, ...]:
        results: list[ArchiveResult] = []
        for archive_plan in plan.archive_plans:
            lease = None
            try:
                from storage_guardian.integrations.resource_governor_client import request_storage_lease

                estimated_io_mb = max(1, int(sum(record.size_bytes for record in archive_plan.files) / 1024 / 1024))
                lease = request_storage_lease(
                    component="archive_executor",
                    lease_scope="archive",
                    request_id=effective_config.get("cycle_id"),
                    estimated_duration_seconds=900,
                    estimated_io_mb=estimated_io_mb,
                    suffix=archive_plan.archive_id,
                )
                if not lease.granted:
                    log.info(
                        "storage_guardian archive %s paused by Resource Governor: %s",
                        archive_plan.archive_id,
                        lease.decision.reason,
                    )
                    break
            except Exception as exc:
                log.debug("Resource Governor archive lease skipped: %s", exc)
                if os.environ.get("AI_RESOURCE_GOVERNOR_URL"):
                    break
            try:
                results.append(self.execute_archive_plan(archive_plan, effective_config, skipped_count=len(plan.skipped)))
            finally:
                if lease is not None:
                    lease.release()
        self.index.commit()
        self.index.export_parquet()
        return tuple(results)

    def execute_archive_plan(self, plan: ArchivePlan, effective_config: dict[str, Any], skipped_count: int = 0) -> ArchiveResult:
        paths = artifact_paths(plan, self.config.data_root)
        write_filelist(plan, paths["filelist"])
        preliminary = manifest_payload(plan, archive_path=None, archive_hash=None, verified=False, effective_config=effective_config)
        write_manifest(paths["manifest"], preliminary)

        compressor = get_compressor(plan.backend)
        final_plan = plan
        if plan.policy_snapshot.get("archive_layout") == "source_directory":
            archive_dir = plan.target.archive_root
        else:
            archive_dir = plan.target.archive_root / plan.tier / plan.store.name
        try:
            archive_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            archive_dir = _fallback_archive_dir(self.config, plan)
            final_plan = _fallback_plan(self.config, plan)
            archive_dir.mkdir(parents=True, exist_ok=True)
        staging_dir = self.config.data_root / "staging"
        staging_dir.mkdir(parents=True, exist_ok=True)
        staging_archive = staging_dir / f"{plan.archive_id}{compressor.extension}"
        final_archive = archive_dir / staging_archive.name
        if final_archive.exists():
            raise FileExistsError(f"archive already exists: {final_archive}")

        compression_level = _compression_level(plan)
        compressor.archive(plan.files, staging_archive, level=compression_level)
        verify_payload = verify_archive(plan, staging_archive)
        if not verify_payload["verified"]:
            write_verify(paths["verify"], verify_payload)
            raise RuntimeError(f"archive verification failed for {plan.archive_id}")
        try:
            shutil.move(str(staging_archive), final_archive)
        except OSError:
            archive_dir = _fallback_archive_dir(self.config, plan)
            final_plan = _fallback_plan(self.config, plan)
            archive_dir.mkdir(parents=True, exist_ok=True)
            final_archive = archive_dir / staging_archive.name
            if final_archive.exists():
                final_archive = final_archive.with_name(f"{final_archive.stem}_{time.time_ns()}{final_archive.suffix}")
            shutil.move(str(staging_archive), final_archive)

        archive_hash = hash_file(final_archive)
        verify_payload["archive_path"] = str(final_archive)
        verify_payload["archive_hash"] = archive_hash
        write_verify(paths["verify"], verify_payload)

        removed_sources_count = self._remove_sources_after_verify(plan) if self._should_replace_sources(plan) else 0
        final_payload = manifest_payload(
            final_plan,
            final_archive,
            archive_hash,
            True,
            effective_config,
            sources_removed=removed_sources_count > 0,
            removed_sources_count=removed_sources_count,
        )
        write_manifest(paths["manifest"], final_payload)
        write_summary(paths["summary"], final_payload, skipped_count=skipped_count)
        self._mirror_artifacts_if_needed(final_plan, paths)

        result = result_from_plan(final_plan, paths, final_archive, plan.backend, verified=True)
        self.index.insert_archive(result, effective_config.get("effective_config_hash"), plan.tier, plan.store.name)
        self.index.insert_archive_members(plan.archive_id, plan.files)
        self.index.insert_event(
            cycle_id=effective_config.get("cycle_id", plan.archive_id),
            event_type="archive_created",
            message=f"archive {plan.archive_id} created",
            metadata={"archive_id": plan.archive_id, "store": plan.store.name, "files_count": len(plan.files)},
        )
        return result

    def _should_replace_sources(self, plan: ArchivePlan) -> bool:
        override = plan.policy_snapshot.get("delete_original_sources_override")
        if override is not None:
            return bool(override)
        safety = self.config.root.get("safety", {})
        return bool(safety.get("destructive_actions_enabled", False) and safety.get("delete_original_sources", False))

    def _remove_sources_after_verify(self, plan: ArchivePlan) -> int:
        removed = 0
        store_root = plan.store.path.resolve()
        for record in plan.files:
            source = record.absolute_path.resolve()
            if not source.is_relative_to(store_root):
                raise RuntimeError(f"refusing to remove source outside registered store: {source}")
            if not source.exists():
                continue
            if record.content_hash and hash_file(source) != record.content_hash:
                raise RuntimeError(f"refusing to remove changed source after archive verify: {source}")
            source.unlink()
            removed += 1
        self._remove_empty_dirs(plan.store.path)
        return removed

    def _remove_empty_dirs(self, store_root: Path) -> None:
        root = store_root.resolve()
        for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
            if not path.is_dir():
                continue
            try:
                path.rmdir()
            except OSError:
                continue

    def _mirror_artifacts_if_needed(self, plan: ArchivePlan, paths: dict[str, Path]) -> None:
        selection = self.config.root.get("placement", {}).get("selection", {})
        if plan.target.kind != "external_ssd" or not selection.get("mirror_manifests_to_external_when_used", True):
            return
        mirror_root = plan.target.data_root
        for key, source in paths.items():
            target = mirror_root / key / source.name
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
            except OSError as exc:
                log.warning("storage_guardian manifest mirror skipped for %s: %s", source, exc)


def _compression_level(plan: ArchivePlan) -> int | None:
    aggression = float(plan.policy_snapshot.get("compression_aggression", 0.5))
    if plan.backend == "sevenzip":
        return sevenzip_level(aggression)
    if plan.backend == "zstd":
        return zstd_level(aggression)
    return None


def _fallback_archive_dir(config: StorageGuardianConfig, plan: ArchivePlan) -> Path:
    return pending_archive_root(config) / plan.tier / plan.store.name


def _fallback_plan(config: StorageGuardianConfig, plan: ArchivePlan) -> ArchivePlan:
    target = StorageTarget(
        kind="local",
        archive_root=pending_archive_root(config),
        data_root=config.data_root,
        fallback_used=True,
        selection_reason="external_storage_failed_pending_sync",
    )
    return replace(plan, target=target)
