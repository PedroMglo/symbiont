"""Service orchestration for scan, plan, archive and restore."""

from __future__ import annotations

import json
import math
import os
import threading
from collections import deque
from pathlib import Path
from typing import Any

from storage_guardian.config import StorageGuardianConfig, load_config
from storage_guardian.archive_reader import ArchiveReader
from storage_guardian.derived_config import build_effective_config
from storage_guardian.executor import PlanExecutor
from storage_guardian.explicit_archive import build_explicit_archive_plan
from storage_guardian.fallback import ensure_external_fixed_roots, ensure_local_fallback_roots, sync_pending_external
from storage_guardian.index import StorageIndex
from storage_guardian.planner import LifecyclePlanner
from storage_guardian.relocation import relocate_paths
from storage_guardian.resource_guard import ResourceGuard, current_resource_snapshot
from storage_guardian.restore import RestoreManager
from storage_guardian.scanner import StoreScanner
from storage_guardian.structure import ReconcileScope, reconcile_structure
from storage_guardian.storage_schema import storage_schema_payload
from storage_guardian.storage_control import AgentStorageGateway
from storage_guardian.types import CyclePlan, FileRecord


class StorageGuardianService:
    def __init__(self, config: StorageGuardianConfig | None = None) -> None:
        self.config = config or load_config()
        index_cfg = self.config.root.get("index", {})
        catalog_path = Path(index_cfg.get("catalog_path", self.config.data_root / "catalog.sqlite"))
        self.index = StorageIndex(
            db_path=catalog_path,
            parquet_catalog_path=Path(index_cfg.get("parquet_catalog_path", self.config.data_root / "archive_catalog.parquet")),
            catalog_backend=str(index_cfg.get("catalog_backend") or "sqlite_wal"),
        )
        self._metrics_lock = threading.RLock()
        self._request_latencies: deque[float] = deque(maxlen=4096)
        self._request_total = 0
        self._request_duration_sum = 0.0
        self._request_duration_max = 0.0

    def close(self) -> None:
        self.index.close()

    def ensure_roots(self) -> None:
        ensure_local_fallback_roots(self.config)
        self.config.data_root.mkdir(parents=True, exist_ok=True)
        ensure_external_fixed_roots(self.config)

    def status(self) -> dict[str, Any]:
        self.ensure_roots()
        effective = build_effective_config(self.config)
        external = ensure_external_fixed_roots(self.config)
        return {
            "enabled": self.config.enabled,
            "config_path": str(self.config.path),
            "index_path": str(self.index.db_path),
            "index_backend": self.index.backend,
            "stores_count": len(self.config.stores),
            "archives_count": len(self.index.list_archives()),
            "external_storage_available": bool(external.get("available")),
            "effective_config_hash": effective["effective_config_hash"],
            "storage_schema_version": effective["storage_schema"]["version"],
            "storage_schema_hash": effective["storage_schema"]["computed_schema_hash"],
            "storage_schema_locked": effective["storage_schema"]["locked"],
        }

    def effective_config(self) -> dict[str, Any]:
        return build_effective_config(self.config)

    def scan(self) -> tuple[FileRecord, ...]:
        self.ensure_roots()
        for store in self.config.stores:
            self.index.upsert_store(store)
        files = StoreScanner(self.config).scan()
        for record in files:
            self.index.upsert_file(record)
        self.index.commit()
        return files

    def plan(self, files: tuple[FileRecord, ...] | None = None) -> CyclePlan:
        files = files if files is not None else self.scan()
        return LifecyclePlanner(self.config).plan(files)

    def run_cycle(self) -> dict[str, Any]:
        self.ensure_roots()
        snapshot = current_resource_snapshot(self.config.data_root)
        resource_cfg = self.config.root.get("resources", {})
        pause, reason = ResourceGuard(float(resource_cfg.get("disk_free_safety_ratio", 0.12))).should_pause(snapshot)
        if pause:
            return {"status": "paused", "reason": reason, "snapshot": snapshot.__dict__}

        lease = None
        try:
            from storage_guardian.integrations.resource_governor_client import request_storage_lease

            lease = request_storage_lease(
                component="scheduler",
                lease_scope="background_cycle",
                estimated_duration_seconds=int(resource_cfg.get("max_cycle_seconds", 1800)),
                suffix="cycle",
            )
            if not lease.granted:
                return {
                    "status": "paused",
                    "reason": lease.decision.reason,
                    "retry_after_seconds": lease.decision.retry_after_seconds,
                    "snapshot": snapshot.__dict__,
                    "governor_decision": lease.decision.model_dump(mode="json"),
                }
        except Exception as exc:
            if os.environ.get("AI_RESOURCE_GOVERNOR_URL"):
                return {
                    "status": "paused",
                    "reason": f"Resource Governor lease unavailable: {str(exc)[:160]}",
                    "snapshot": snapshot.__dict__,
                    "governor_decision": {
                        "decision": "defer",
                        "reason": "resource_governor_unavailable",
                    },
                }
            lease = None

        try:
            files = self.scan()
            if lease is not None:
                lease.heartbeat()
            cycle_plan = self.plan(files)
            effective = build_effective_config(self.config, snapshot)
            effective["cycle_id"] = cycle_plan.cycle_id
            results = PlanExecutor(self.config, self.index).execute(cycle_plan, effective)
            self._record_events(cycle_plan)
            return {
                "status": "completed",
                "cycle_id": cycle_plan.cycle_id,
                "files_seen": len(files),
                "archives_created": len(results),
                "skipped": len(cycle_plan.skipped),
                "results": [result.__dict__ | {"archive_path": str(result.archive_path)} for result in results],
            }
        finally:
            if lease is not None:
                lease.release()

    def archive_paths(
        self,
        paths: list[str | Path],
        *,
        tier: str = "cold",
        requested_by: str = "orcai",
        placement_mode: str = "configured",
        replace_sources: bool = False,
    ) -> dict[str, Any]:
        self.ensure_roots()
        cycle_plan = build_explicit_archive_plan(
            self.config,
            paths,
            tier=tier,
            requested_by=requested_by,
            placement_mode=placement_mode,
            replace_sources=replace_sources,
        )
        effective = build_effective_config(self.config)
        effective["cycle_id"] = cycle_plan.cycle_id
        lease = None
        try:
            from storage_guardian.integrations.resource_governor_client import request_storage_lease

            lease = request_storage_lease(
                component="explicit_archive",
                lease_scope="archive",
                estimated_duration_seconds=900,
                suffix=cycle_plan.cycle_id,
            )
            if not lease.granted:
                return {
                    "status": "paused",
                    "reason": lease.decision.reason,
                    "retry_after_seconds": lease.decision.retry_after_seconds,
                    "cycle_id": cycle_plan.cycle_id,
                    "governor_decision": lease.decision.model_dump(mode="json"),
                }
        except Exception as exc:
            if os.environ.get("AI_RESOURCE_GOVERNOR_URL"):
                return {
                    "status": "paused",
                    "reason": f"Resource Governor lease unavailable: {str(exc)[:160]}",
                    "cycle_id": cycle_plan.cycle_id,
                    "governor_decision": {
                        "decision": "defer",
                        "reason": "resource_governor_unavailable",
                    },
                }
            lease = None
        try:
            results = PlanExecutor(self.config, self.index).execute(cycle_plan, effective)
        finally:
            if lease is not None:
                lease.release()
        self._record_events(cycle_plan)
        return {
            "status": "completed" if results else "no_archives_created",
            "cycle_id": cycle_plan.cycle_id,
            "requested_by": requested_by,
            "placement_mode": placement_mode,
            "replace_sources": replace_sources,
            "paths": [str(path) for path in paths],
            "files_seen": len(cycle_plan.files),
            "archives_created": len(results),
            "skipped": [
                {
                    "path": str(skip.file.absolute_path),
                    "relative_path": skip.file.relative_path,
                    "reason": skip.reason,
                    "state": skip.state,
                }
                for skip in cycle_plan.skipped
            ],
            "results": [
                result.__dict__
                | {
                    "archive_path": str(result.archive_path),
                    "manifest_path": str(result.manifest_path),
                    "summary_path": str(result.summary_path),
                    "filelist_path": str(result.filelist_path),
                    "verify_path": str(result.verify_path),
                }
                for result in results
            ],
        }

    def restore(self, manifest_path: str | Path, restore_name: str | None = None) -> dict[str, Any]:
        self.ensure_roots()
        return RestoreManager(self.config, self.index).restore(manifest_path, restore_name)

    def archive_members(self, manifest_path: str | Path) -> list[dict[str, Any]]:
        return ArchiveReader(allowed_manifest_roots=self._manifest_roots()).list_members(manifest_path)

    def read_archive_text(self, manifest_path: str | Path, relative_path: str, max_bytes: int | None = None) -> dict[str, Any]:
        return ArchiveReader(allowed_manifest_roots=self._manifest_roots()).read_text_member(manifest_path, relative_path, max_bytes=max_bytes)

    def archives(self) -> list[dict[str, Any]]:
        return self.index.list_archives()

    def relocate_paths(
        self,
        paths: list[str | Path],
        *,
        requested_by: str = "system",
        target_root: str | Path | None = None,
        replace_with: str = "symlink",
        dry_run: bool = False,
    ) -> dict[str, Any]:
        self.ensure_roots()
        result = relocate_paths(
            self.config,
            paths,
            requested_by=requested_by,
            target_root=target_root,
            replace_with=replace_with,
            dry_run=dry_run,
        )
        if not dry_run:
            self.index.insert_event(
                str(result.get("requested_by", "system")),
                "paths_relocated",
                "managed relocation completed",
                {
                    "paths": result.get("paths_requested", []),
                    "target_root": result.get("target_root"),
                    "relocations_created": result.get("relocations_created", 0),
                    "total_size_bytes": result.get("total_size_bytes", 0),
                },
            )
            self.index.commit()
        return result

    def sync_pending(self, *, max_items: int | None = None, max_bytes: int | None = None) -> dict[str, Any]:
        self.ensure_roots()
        return sync_pending_external(self.config, self.index, max_items=max_items, max_bytes=max_bytes)

    def _manifest_roots(self) -> tuple[Path, ...]:
        return (self.config.data_root / "manifests",)

    def storage_policies(self) -> dict[str, Any]:
        self.ensure_roots()
        return AgentStorageGateway(self.config, self.index).policies()

    def storage_schema(self) -> dict[str, Any]:
        self.ensure_roots()
        schema = storage_schema_payload(self.config.root)
        policies = AgentStorageGateway(self.config, self.index).policies()
        schema["storage_contract_version"] = policies["storage_contract_version"]
        schema["api_schema_hash"] = policies["api_schema_hash"]
        schema["capabilities"] = {
            "stores": policies["stores"],
            "zones": policies["zones"],
            "object_model": "immutable_versioned_objects",
            "path_control": "guardian_internal_only",
            "idempotency_required": True,
        }
        return schema

    def create_storage_object(self, payload: dict[str, Any], *, idempotency_key: str | None) -> dict[str, Any]:
        self.ensure_roots()
        return AgentStorageGateway(self.config, self.index).create_object(payload, idempotency_key=idempotency_key)

    def create_upload_session(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.ensure_roots()
        return AgentStorageGateway(self.config, self.index).create_upload_session(payload)

    def append_upload_bytes(self, upload_id: str, content: bytes) -> dict[str, Any]:
        self.ensure_roots()
        return AgentStorageGateway(self.config, self.index).append_upload_bytes(upload_id, content)

    def commit_upload(self, upload_id: str, payload: dict[str, Any], *, idempotency_key: str | None) -> dict[str, Any]:
        self.ensure_roots()
        return AgentStorageGateway(self.config, self.index).commit_upload(upload_id, payload, idempotency_key=idempotency_key)

    def materialize_storage_artifact(self, payload: dict[str, Any], *, idempotency_key: str | None) -> dict[str, Any]:
        self.ensure_roots()
        return AgentStorageGateway(self.config, self.index).materialize_artifact(payload, idempotency_key=idempotency_key)

    def cleanup_storage_control(self) -> dict[str, Any]:
        self.ensure_roots()
        return AgentStorageGateway(self.config, self.index).cleanup_expired_uploads()

    def reconcile_structure(self, *, scope: ReconcileScope = "all", apply: bool = False) -> dict[str, Any]:
        self.ensure_roots()
        return reconcile_structure(self.config, index=self.index, scope=scope, apply=apply)

    def promote_storage_object(self, object_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.ensure_roots()
        return AgentStorageGateway(self.config, self.index).promote_object(
            object_id,
            agent=str(payload["agent"]),
            target_zone=str(payload["target_zone"]),
        )

    def soft_delete_storage_object(self, object_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.ensure_roots()
        return AgentStorageGateway(self.config, self.index).soft_delete_object(
            object_id,
            agent=str(payload["agent"]),
            reason=payload.get("reason"),
        )

    def storage_objects(self, *, agent: str | None = None, zone: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        self.ensure_roots()
        return AgentStorageGateway(self.config, self.index).list_objects(agent=agent, zone=zone, status=status)

    def storage_directories(
        self,
        *,
        service: str | None = None,
        store: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        self.ensure_roots()
        return self.index.list_directories(service=service, store=store, status=status)

    def storage_operations(
        self,
        *,
        operation_type: str | None = None,
        actor: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        self.ensure_roots()
        return self.index.list_operations(operation_type=operation_type, actor=actor, status=status)

    def storage_object(self, object_id: str) -> dict[str, Any] | None:
        self.ensure_roots()
        return AgentStorageGateway(self.config, self.index).get_object(object_id)

    def read_storage_object_text(self, object_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.ensure_roots()
        return AgentStorageGateway(self.config, self.index).read_object_text(object_id, payload)

    def create_storage_directory(self, payload: dict[str, Any], *, idempotency_key: str | None) -> dict[str, Any]:
        self.ensure_roots()
        return AgentStorageGateway(self.config, self.index).create_directory(payload, idempotency_key=idempotency_key)

    def copy_storage_object(self, object_id: str, payload: dict[str, Any], *, idempotency_key: str | None) -> dict[str, Any]:
        self.ensure_roots()
        return AgentStorageGateway(self.config, self.index).copy_object(object_id, payload, idempotency_key=idempotency_key)

    def move_storage_object(self, object_id: str, payload: dict[str, Any], *, idempotency_key: str | None) -> dict[str, Any]:
        self.ensure_roots()
        return AgentStorageGateway(self.config, self.index).move_object(object_id, payload, idempotency_key=idempotency_key)

    def rename_storage_object(self, object_id: str, payload: dict[str, Any], *, idempotency_key: str | None) -> dict[str, Any]:
        self.ensure_roots()
        return AgentStorageGateway(self.config, self.index).rename_object(object_id, payload, idempotency_key=idempotency_key)

    def hard_purge_storage_object(self, object_id: str, payload: dict[str, Any], *, idempotency_key: str | None) -> dict[str, Any]:
        self.ensure_roots()
        return AgentStorageGateway(self.config, self.index).hard_purge_object(object_id, payload, idempotency_key=idempotency_key)

    def metrics(self) -> dict[str, Any]:
        status_payload = self.status()
        with self._metrics_lock:
            latencies = sorted(self._request_latencies)
            p95_index = max(0, math.ceil(len(latencies) * 0.95) - 1) if latencies else 0
            http_metrics = {
                "storage_guardian_request_total": self._request_total,
                "storage_guardian_request_duration_seconds_sum": self._request_duration_sum,
                "storage_guardian_request_duration_seconds_max": self._request_duration_max,
                "storage_guardian_request_duration_seconds_p95": latencies[p95_index] if latencies else 0.0,
            }
        return {
            "storage_guardian_archives_count": status_payload["archives_count"],
            "storage_guardian_stores_count": status_payload["stores_count"],
            **http_metrics,
            **self.index.storage_control_metrics(),
        }

    def record_http_request(self, *, latency_seconds: float) -> None:
        with self._metrics_lock:
            self._request_total += 1
            self._request_duration_sum += latency_seconds
            self._request_duration_max = max(self._request_duration_max, latency_seconds)
            self._request_latencies.append(latency_seconds)

    def record_storage_control_rejection(
        self,
        *,
        reason: str,
        action: str,
        agent: str,
        object_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.index.insert_control_event(
            event_type="request_rejected",
            agent=agent,
            action=action,
            allowed=False,
            reason=reason,
            object_id=object_id,
            metadata=metadata or {},
        )
        self.index.commit()

    def write_status_json(self) -> Path:
        status_path = self.config.data_root / "state" / "status.json"
        status_path.parent.mkdir(parents=True, exist_ok=True)
        status_path.write_text(json.dumps(self.status(), indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
        return status_path

    def _record_events(self, plan: CyclePlan) -> None:
        for event in plan.events:
            self.index.insert_event(plan.cycle_id, str(event.get("event_type", "event")), "planner event", event)
        for skipped in plan.skipped:
            self.index.insert_event(
                plan.cycle_id,
                "file_skipped",
                skipped.reason,
                {"file": skipped.file.relative_path, "store": skipped.file.store.name, "state": skipped.state},
            )
        self.index.commit()
