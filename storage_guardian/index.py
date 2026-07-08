"""Operational index for lifecycle storage state."""

from __future__ import annotations

import json
import hashlib
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from storage_guardian.integrations.ai_local_events import publish_storage_event, storage_lifecycle_event_type
from storage_guardian.types import ArchiveResult, FileRecord, StoreConfig

_SQL_DIR = Path(__file__).resolve().parent / "sql"
_SQL_CACHE = {}


def _sql(name: str) -> str:
    text = _SQL_CACHE.get(name)
    if text is None:
        text = (_SQL_DIR / name).read_text(encoding="utf-8").strip()
        _SQL_CACHE[name] = text
    return text



_CONTROL_V2_SCHEMA = """
CREATE TABLE IF NOT EXISTS storage_directories (
  directory_id TEXT PRIMARY KEY,
  service TEXT,
  store_id TEXT,
  relative_path TEXT,
  parent_directory_id TEXT,
  owner TEXT,
  zone TEXT,
  mode TEXT,
  policy TEXT,
  expected_by_schema INTEGER,
  status TEXT,
  created_by TEXT,
  created_at REAL,
  last_seen_at REAL,
  protected INTEGER,
  writable_by_storage_guardian INTEGER,
  readable_by_callers INTEGER,
  absolute_path_hash TEXT,
  metadata_json TEXT
);
CREATE TABLE IF NOT EXISTS storage_operations (
  operation_id TEXT PRIMARY KEY,
  operation_type TEXT,
  actor TEXT,
  requesting_service TEXT,
  source_ref TEXT,
  target_ref TEXT,
  source_directory_id TEXT,
  target_directory_id TEXT,
  policy_decision TEXT,
  dry_run_result_json TEXT,
  idempotency_key TEXT,
  preconditions_json TEXT,
  hash_before TEXT,
  hash_after TEXT,
  status TEXT,
  started_at REAL,
  finished_at REAL,
  custody_event_id TEXT,
  rollback_plan_json TEXT,
  metadata_json TEXT
);
CREATE TABLE IF NOT EXISTS storage_custody_events (
  custody_event_id TEXT PRIMARY KEY,
  timestamp REAL,
  event_type TEXT,
  actor TEXT,
  requesting_service TEXT,
  operation_id TEXT,
  object_id TEXT,
  directory_id TEXT,
  source_ref TEXT,
  target_ref TEXT,
  content_hash TEXT,
  metadata_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_storage_directories_service_store
  ON storage_directories(service, store_id, status);
CREATE INDEX IF NOT EXISTS idx_storage_operations_type_status
  ON storage_operations(operation_type, status);
CREATE INDEX IF NOT EXISTS idx_storage_operations_actor
  ON storage_operations(actor, started_at);
CREATE INDEX IF NOT EXISTS idx_storage_custody_object
  ON storage_custody_events(object_id, timestamp);
"""


class StorageIndex:
    def __init__(
        self,
        db_path: Path,
        parquet_catalog_path: Path | None = None,
        *,
        catalog_backend: str = "sqlite_wal",
    ) -> None:
        self.db_path = db_path
        self.parquet_catalog_path = parquet_catalog_path
        self.control_db_path = db_path.with_name(f"{db_path.stem}_control.sqlite3")
        self.backend = "sqlite_wal"
        self._lock = threading.RLock()
        self._control_lock = threading.RLock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        if catalog_backend == "duckdb":
            import duckdb

            self.connection = duckdb.connect(str(self.db_path))
            self.backend = "duckdb"
        else:
            self.db_path = _sqlite_fallback_path(db_path)
            self.connection = sqlite3.connect(self.db_path, check_same_thread=False)
            self.connection.row_factory = sqlite3.Row
            self._configure_sqlite_connection(self.connection)
        self.control_db_path.parent.mkdir(parents=True, exist_ok=True)
        self.control_connection = sqlite3.connect(self.control_db_path, check_same_thread=False, timeout=5.0)
        self.control_connection.row_factory = sqlite3.Row
        self._configure_control_connection()
        self.ensure_schema()

    def close(self) -> None:
        with self._lock:
            self.connection.close()
        with self._control_lock:
            self.control_connection.close()

    def ensure_schema(self) -> None:
        with self._lock:
            self._execute_script(
                _sql("extra_62_1.sql")
            )
            self.connection.commit()
        with self._control_lock:
            self._execute_control_script(
                _sql("extra_190_2.sql")
            )
            self._execute_control_script(_CONTROL_V2_SCHEMA)
            self._ensure_control_column("storage_objects", "latest_version_id", "TEXT")
            self._ensure_control_column("storage_objects", "logical_name", "TEXT")
            self._ensure_control_column("storage_objects", "content_type", "TEXT")
            self._ensure_control_column("storage_objects", "directory_id", "TEXT")
            self._ensure_control_column("storage_objects", "owner_service", "TEXT")
            self._ensure_control_column("storage_objects", "producer_agent", "TEXT")
            self._ensure_control_column("storage_objects", "object_kind", "TEXT")
            self._ensure_control_column("storage_objects", "media_type", "TEXT")
            self._ensure_control_column("storage_objects", "semantic_role", "TEXT")
            self._ensure_control_column("storage_objects", "source_ref", "TEXT")
            self._ensure_control_column("storage_objects", "lineage_root", "TEXT")
            self._ensure_control_column("storage_objects", "storage_ref", "TEXT")
            self._ensure_control_column("storage_objects", "materialized_ref", "TEXT")
            self._ensure_control_column("storage_objects", "retention_policy", "TEXT")
            self.control_connection.commit()

    def upsert_store(self, store: StoreConfig) -> None:
        with self._lock:
            self.connection.execute(_sql("execute_281.sql"), (store.name,))
            self.connection.execute(
                _sql("execute_283.sql"),
                (store.name, store.name, str(store.path), store.owner, store.type, store.mode, store.policy, int(store.enabled), store.placement),
            )

    def upsert_file(self, record: FileRecord) -> None:
        absolute_path_hash = hashlib.sha256(str(record.absolute_path).encode("utf-8")).hexdigest()
        with self._lock:
            self.connection.execute(_sql("execute_294.sql"), (record.file_id,))
            self.connection.execute(
                _sql("execute_296.sql"),
                (
                    record.file_id,
                    record.store.name,
                    record.relative_path,
                    absolute_path_hash,
                    record.extension,
                    record.size_bytes,
                    record.modified_at,
                    record.accessed_at,
                    record.processed_at,
                    record.effective_age_days,
                    "sha256" if record.content_hash else None,
                    record.content_hash,
                    record.detected_type,
                    record.lifecycle_state,
                    time.time(),
                ),
            )

    def insert_archive(self, result: ArchiveResult, effective_config_hash: str | None, tier: str, store_id: str) -> None:
        reduction = 0.0
        if result.original_size_bytes:
            reduction = 1 - (result.archive_size_bytes / result.original_size_bytes)
        with self._lock:
            self.connection.execute(_sql("execute_327.sql"), (result.archive_id,))
            self.connection.execute(
                _sql("execute_329.sql"),
                (
                    result.archive_id,
                    store_id,
                    tier,
                    result.backend,
                    result.storage_target.kind,
                    str(result.archive_path),
                    str(result.manifest_path),
                    str(result.summary_path),
                    str(result.filelist_path),
                    str(result.verify_path),
                    result.original_size_bytes,
                    result.archive_size_bytes,
                    reduction,
                    result.files_count,
                    time.time(),
                    int(result.verified),
                    effective_config_hash,
                ),
            )

    def insert_archive_members(self, archive_id: str, files: tuple[FileRecord, ...]) -> None:
        with self._lock:
            self.connection.execute(_sql("execute_359.sql"), (archive_id,))
            self.connection.executemany(
                _sql("executemany_361.sql"),
                [(archive_id, record.file_id, record.relative_path, record.content_hash, record.size_bytes, "archived") for record in files],
            )

    def insert_event(self, cycle_id: str, event_type: str, message: str, metadata: dict[str, Any] | None = None) -> None:
        event_id = f"{cycle_id}_{time.time_ns()}"
        timestamp = time.time()
        metadata = metadata or {}
        with self._lock:
            self.connection.execute(
                _sql("execute_375.sql"),
                (event_id, timestamp, cycle_id, event_type, "info", message, json.dumps(metadata, default=str)),
            )
        publish_storage_event(
            storage_lifecycle_event_type(event_type),
            payload={
                "source_event_id": event_id,
                "local_event_type": event_type,
                "cycle_id": cycle_id,
                "message": message,
                "metadata": metadata,
            },
            metadata={"source_index": "lifecycle_events"},
        )

    def insert_restore_event(self, restore_id: str, archive_id: str, restore_root: Path, verified: bool) -> None:
        now = time.time()
        with self._lock:
            self.connection.execute(_sql("execute_397.sql"), (restore_id,))
            self.connection.execute(
                _sql("execute_399.sql"),
                (restore_id, archive_id, "internal", str(restore_root), now, now, int(verified), 0),
            )
        publish_storage_event(
            "storage.restore.dry_run.completed",
            payload={
                "source_event_id": restore_id,
                "archive_id": archive_id,
                "restore_root": str(restore_root),
                "verified": verified,
            },
            severity="info" if verified else "medium",
            metadata={"source_index": "restore_events"},
        )

    def upsert_storage_object(self, record: dict[str, Any]) -> None:
        current_path = str(record.get("current_path", ""))
        metadata = record.get("metadata", {})
        absolute_path_hash = hashlib.sha256(current_path.encode("utf-8")).hexdigest() if current_path else None
        with self._control_lock:
            self.control_connection.execute(_sql("execute_423.sql"), (record["object_id"],))
            self.control_connection.execute(
                _sql("execute_425.sql"),
                (
                    record["object_id"],
                    record.get("latest_version_id"),
                    record.get("store_id"),
                    record.get("created_by"),
                    float(record.get("created_at") or time.time()),
                    float(record.get("updated_at") or time.time()),
                    record.get("purpose"),
                    record.get("zone"),
                    record.get("status"),
                    record.get("policy"),
                    current_path,
                    record.get("relative_path"),
                    absolute_path_hash,
                    int(record.get("size_bytes") or 0),
                    record.get("hash_algo"),
                    record.get("content_hash"),
                    record.get("source_file"),
                    record.get("source_content_hash"),
                    record.get("parent_object_id"),
                    record.get("model"),
                    record.get("logical_name"),
                    record.get("content_type"),
                    json.dumps(metadata or {}, default=str),
                ),
            )
            self.control_connection.execute(
                """
                UPDATE storage_objects
                SET directory_id = ?,
                    owner_service = ?,
                    producer_agent = ?,
                    object_kind = ?,
                    media_type = ?,
                    semantic_role = ?,
                    source_ref = ?,
                    lineage_root = ?,
                    storage_ref = ?,
                    materialized_ref = ?,
                    retention_policy = ?
                WHERE object_id = ?
                """,
                (
                    record.get("directory_id"),
                    record.get("owner_service"),
                    record.get("producer_agent") or record.get("created_by"),
                    record.get("object_kind"),
                    record.get("media_type") or record.get("content_type"),
                    record.get("semantic_role"),
                    record.get("source_ref"),
                    record.get("lineage_root"),
                    record.get("storage_ref"),
                    record.get("materialized_ref"),
                    record.get("retention_policy"),
                    record["object_id"],
                ),
            )

    def upsert_storage_version(self, record: dict[str, Any]) -> None:
        current_path = str(record.get("current_path", ""))
        metadata = record.get("metadata", {})
        with self._control_lock:
            self.control_connection.execute(_sql("execute_463.sql"), (record["version_id"],))
            self.control_connection.execute(
                _sql("execute_465.sql"),
                (
                    record["version_id"],
                    record["object_id"],
                    record.get("store_id"),
                    record.get("created_by"),
                    float(record.get("created_at") or time.time()),
                    record.get("zone"),
                    record.get("status"),
                    record.get("policy"),
                    record.get("logical_name"),
                    record.get("content_type"),
                    current_path,
                    record.get("relative_path"),
                    int(record.get("size_bytes") or 0),
                    record.get("hash_algo"),
                    record.get("content_hash"),
                    record.get("parent_object_id"),
                    json.dumps(metadata or {}, default=str),
                ),
            )

    def get_storage_object(self, object_id: str) -> dict[str, Any] | None:
        with self._control_lock:
            cursor = self.control_connection.execute(_sql("execute_495.sql"), (object_id,))
            rows = cursor.fetchall()
            objects = self._rows_to_dicts(cursor, rows)
            return objects[0] if objects else None

    def list_storage_objects(self, *, agent: str | None = None, zone: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if agent:
            clauses.append("created_by = ?")
            params.append(agent)
        if zone:
            clauses.append("zone = ?")
            params.append(zone)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._control_lock:
            cursor = self.control_connection.execute(_sql("fstring_486_3.sql").format(where), tuple(params))
            return self._rows_to_dicts(cursor, cursor.fetchall())

    def storage_usage_bytes(self, agent: str) -> int:
        with self._control_lock:
            cursor = self.control_connection.execute(
                _sql("execute_520.sql"),
                (agent,),
            )
            row = cursor.fetchone()
            return int(row[0] if row else 0)

    def get_idempotency_key(self, scope: str, idempotency_key: str) -> dict[str, Any] | None:
        with self._control_lock:
            cursor = self.control_connection.execute(
                _sql("execute_533.sql"),
                (scope, idempotency_key),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            item = dict(row)
            try:
                item["response"] = json.loads(str(item.pop("response_json") or "{}"))
            except json.JSONDecodeError:
                item["response"] = {}
            return item

    def upsert_idempotency_key(
        self,
        *,
        scope: str,
        idempotency_key: str,
        payload_hash: str,
        status: str,
        object_id: str | None,
        response: dict[str, Any],
        expires_at: float,
    ) -> None:
        with self._control_lock:
            self.control_connection.execute(
                _sql("execute_563.sql"),
                (
                    scope,
                    idempotency_key,
                    payload_hash,
                    status,
                    object_id,
                    json.dumps(response, default=str, sort_keys=True),
                    time.time(),
                    expires_at,
                ),
            )

    def upsert_upload_session(self, record: dict[str, Any]) -> None:
        metadata = record.get("metadata", {})
        with self._control_lock:
            self.control_connection.execute(_sql("execute_583.sql"), (record["upload_id"],))
            self.control_connection.execute(
                _sql("execute_585.sql"),
                (
                    record["upload_id"],
                    record.get("object_id"),
                    record.get("version_id"),
                    record.get("store_id"),
                    record.get("created_by"),
                    float(record.get("created_at") or time.time()),
                    float(record.get("updated_at") or time.time()),
                    float(record.get("expires_at") or 0),
                    record.get("zone"),
                    record.get("status"),
                    record.get("policy"),
                    record.get("logical_name"),
                    record.get("content_type"),
                    record.get("temp_path"),
                    record.get("final_path"),
                    int(record.get("expected_size") or 0),
                    int(record.get("received_size") or 0),
                    record.get("hash_algo"),
                    record.get("expected_hash"),
                    json.dumps(metadata or {}, default=str),
                ),
            )

    def get_upload_session(self, upload_id: str) -> dict[str, Any] | None:
        with self._control_lock:
            cursor = self.control_connection.execute(_sql("execute_618.sql"), (upload_id,))
            rows = cursor.fetchall()
            items = self._rows_to_dicts(cursor, rows)
            return items[0] if items else None

    def expired_upload_sessions(self, now: float) -> list[dict[str, Any]]:
        with self._control_lock:
            cursor = self.control_connection.execute(
                _sql("execute_626.sql"),
                (now,),
            )
            return self._rows_to_dicts(cursor, cursor.fetchall())

    def storage_control_metrics(self) -> dict[str, int]:
        with self._control_lock:
            objects = self._count_by("storage_objects", "status")
            uploads = self._count_by("storage_upload_sessions", "status")
            events = self._count_by("storage_control_events", "action")
            decisions = self._count_by("storage_control_events", "allowed")
            rejections = self._count_rejections_by_reason()
            bytes_row = self.control_connection.execute(
                _sql("execute_643.sql")
            ).fetchone()
            total_bytes = int(bytes_row[0] if bytes_row else 0)
            idempotency_row = self.control_connection.execute(_sql("execute_646.sql")).fetchone()
            idempotency_keys = int(idempotency_row[0] if idempotency_row else 0)
            idempotency_replay_row = self.control_connection.execute(
                _sql("execute_649.sql")
            ).fetchone()
            idempotency_replays = int(idempotency_replay_row[0] if idempotency_replay_row else 0)
            temp_cleanup_row = self.control_connection.execute(
                _sql("execute_653.sql")
            ).fetchone()
            temp_cleanup = int(temp_cleanup_row[0] if temp_cleanup_row else 0)
        metrics: dict[str, int] = {
            "storage_guardian_bytes_written_total": total_bytes,
            "storage_guardian_idempotency_keys_total": idempotency_keys,
            "storage_guardian_idempotency_replay_total": idempotency_replays,
            "storage_guardian_temp_cleanup_total": temp_cleanup,
            "storage_guardian_sqlite_busy_total": 0,
            "storage_guardian_fs_rename_duration_seconds_sum": 0,
            "storage_guardian_fsync_duration_seconds_sum": 0,
        }
        for status, count in objects.items():
            metrics[f"storage_guardian_objects_{status}_total"] = count
        for status, count in uploads.items():
            metrics[f"storage_guardian_upload_sessions_{status}_total"] = count
        for action, count in events.items():
            metrics[f"storage_guardian_policy_decision_{action}_total"] = count
        for allowed, count in decisions.items():
            name = "allowed" if str(allowed) == "1" else "rejected"
            metrics[f"storage_guardian_policy_decision_{name}_total"] = count
        for reason, count in rejections.items():
            metrics[f"storage_guardian_rejections_{_metric_segment(reason)}_total"] = count
        metrics["storage_guardian_quota_rejections_total"] = rejections.get("quota_exceeded", 0)
        metrics["storage_guardian_quarantine_total"] = objects.get("quarantined", 0) + uploads.get("quarantined", 0)
        return metrics

    def insert_control_event(
        self,
        *,
        event_type: str,
        agent: str,
        action: str,
        allowed: bool,
        reason: str,
        object_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        event_id = f"storage_control_{time.time_ns()}"
        timestamp = time.time()
        metadata = metadata or {}
        with self._control_lock:
            self.control_connection.execute(
                _sql("execute_696.sql"),
                (
                    event_id,
                    timestamp,
                    event_type,
                    agent,
                    action,
                    int(allowed),
                    reason,
                    object_id,
                    json.dumps(metadata, default=str),
                ),
            )
        publish_storage_event(
            "storage.lifecycle.changed",
            payload={
                "source_event_id": event_id,
                "local_event_type": event_type,
                "agent": agent,
                "action": action,
                "allowed": allowed,
                "reason": reason,
                "object_id": object_id,
                "metadata": metadata,
            },
            severity="info" if allowed else "medium",
            metadata={"source_index": "storage_control_events"},
        )

    def upsert_directory(self, record: dict[str, Any]) -> None:
        metadata = record.get("metadata", {})
        with self._control_lock:
            self.control_connection.execute(
                """
                INSERT OR REPLACE INTO storage_directories
                (directory_id, service, store_id, relative_path, parent_directory_id, owner, zone, mode, policy,
                 expected_by_schema, status, created_by, created_at, last_seen_at, protected,
                 writable_by_storage_guardian, readable_by_callers, absolute_path_hash, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record["directory_id"],
                    record.get("service"),
                    record.get("store_id"),
                    record.get("relative_path"),
                    record.get("parent_directory_id"),
                    record.get("owner"),
                    record.get("zone"),
                    record.get("mode"),
                    record.get("policy"),
                    int(bool(record.get("expected_by_schema"))),
                    record.get("status"),
                    record.get("created_by"),
                    float(record.get("created_at") or time.time()),
                    float(record.get("last_seen_at") or time.time()),
                    int(bool(record.get("protected"))),
                    int(bool(record.get("writable_by_storage_guardian", True))),
                    int(bool(record.get("readable_by_callers", True))),
                    record.get("absolute_path_hash"),
                    json.dumps(metadata or {}, default=str),
                ),
            )

    def list_directories(
        self,
        *,
        service: str | None = None,
        store: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if service:
            clauses.append("service = ?")
            params.append(service)
        if store:
            clauses.append("store_id = ?")
            params.append(store)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._control_lock:
            cursor = self.control_connection.execute(
                f"SELECT * FROM storage_directories {where} ORDER BY service, store_id, relative_path",  # nosec B608 - WHERE terms are fixed literals; values are bound params.
                tuple(params),
            )
            return self._rows_to_dicts(cursor, cursor.fetchall())

    def delete_directory(self, directory_id: str) -> None:
        with self._control_lock:
            self.control_connection.execute(
                "DELETE FROM storage_directories WHERE directory_id = ?",
                (directory_id,),
            )

    def insert_operation(self, record: dict[str, Any]) -> None:
        metadata = record.get("metadata", {})
        with self._control_lock:
            self.control_connection.execute(
                """
                INSERT OR REPLACE INTO storage_operations
                (operation_id, operation_type, actor, requesting_service, source_ref, target_ref,
                 source_directory_id, target_directory_id, policy_decision, dry_run_result_json,
                 idempotency_key, preconditions_json, hash_before, hash_after, status, started_at,
                 finished_at, custody_event_id, rollback_plan_json, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record["operation_id"],
                    record.get("operation_type"),
                    record.get("actor"),
                    record.get("requesting_service"),
                    record.get("source_ref"),
                    record.get("target_ref"),
                    record.get("source_directory_id"),
                    record.get("target_directory_id"),
                    record.get("policy_decision"),
                    json.dumps(record.get("dry_run_result") or {}, default=str),
                    record.get("idempotency_key"),
                    json.dumps(record.get("preconditions") or {}, default=str),
                    record.get("hash_before"),
                    record.get("hash_after"),
                    record.get("status"),
                    float(record.get("started_at") or time.time()),
                    float(record.get("finished_at") or time.time()),
                    record.get("custody_event_id"),
                    json.dumps(record.get("rollback_plan") or {}, default=str),
                    json.dumps(metadata or {}, default=str),
                ),
            )

    def list_operations(
        self,
        *,
        operation_type: str | None = None,
        actor: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if operation_type:
            clauses.append("operation_type = ?")
            params.append(operation_type)
        if actor:
            clauses.append("actor = ?")
            params.append(actor)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._control_lock:
            cursor = self.control_connection.execute(
                f"SELECT * FROM storage_operations {where} ORDER BY started_at DESC",  # nosec B608 - WHERE terms are fixed literals; values are bound params.
                tuple(params),
            )
            return self._rows_to_dicts(cursor, cursor.fetchall())

    def insert_custody_event(self, record: dict[str, Any]) -> str:
        custody_event_id = str(record.get("custody_event_id") or f"custody_{time.time_ns()}")
        metadata = record.get("metadata", {})
        with self._control_lock:
            self.control_connection.execute(
                """
                INSERT OR REPLACE INTO storage_custody_events
                (custody_event_id, timestamp, event_type, actor, requesting_service, operation_id,
                 object_id, directory_id, source_ref, target_ref, content_hash, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    custody_event_id,
                    float(record.get("timestamp") or time.time()),
                    record.get("event_type"),
                    record.get("actor"),
                    record.get("requesting_service"),
                    record.get("operation_id"),
                    record.get("object_id"),
                    record.get("directory_id"),
                    record.get("source_ref"),
                    record.get("target_ref"),
                    record.get("content_hash"),
                    json.dumps(metadata or {}, default=str),
                ),
            )
        publish_storage_event(
            "storage.lifecycle.changed",
            payload={
                "source_event_id": custody_event_id,
                "local_event_type": record.get("event_type"),
                "actor": record.get("actor"),
                "requesting_service": record.get("requesting_service"),
                "operation_id": record.get("operation_id"),
                "object_id": record.get("object_id"),
                "directory_id": record.get("directory_id"),
                "source_ref": record.get("source_ref"),
                "target_ref": record.get("target_ref"),
                "content_hash": record.get("content_hash"),
                "metadata": metadata,
            },
            severity="info",
            metadata={"source_index": "storage_custody_events"},
        )
        return custody_event_id

    def list_archives(self) -> list[dict[str, Any]]:
        with self._lock:
            cursor = self.connection.execute(_sql("execute_731.sql"))
            return self._rows_to_dicts(cursor, cursor.fetchall(), parse_metadata=False)

    def commit(self) -> None:
        with self._lock:
            self.connection.commit()
        with self._control_lock:
            self.control_connection.commit()

    def _execute_script(self, script: str) -> None:
        with self._lock:
            statements = [statement.strip() for statement in script.split(";") if statement.strip()]
            for statement in statements:
                self.connection.execute(statement)

    def _configure_control_connection(self) -> None:
        with self._control_lock:
            self._configure_sqlite_connection(self.control_connection)
            self.control_connection.execute(_sql("execute_748.sql"))
            self.control_connection.execute(_sql("execute_749.sql"))
            self.control_connection.execute(_sql("execute_750.sql"))
            self.control_connection.execute(_sql("execute_751.sql"))

    @staticmethod
    def _configure_sqlite_connection(connection: sqlite3.Connection) -> None:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA foreign_keys=ON")

    def _execute_control_script(self, script: str) -> None:
        with self._control_lock:
            statements = [statement.strip() for statement in script.split(";") if statement.strip()]
            for statement in statements:
                self.control_connection.execute(statement)

    def _ensure_control_column(self, table: str, column: str, column_type: str) -> None:
        cursor = self.control_connection.execute(_sql("fstring_706.sql").format(table))
        existing = {str(row["name"]) for row in cursor.fetchall()}
        if column not in existing:
            self.control_connection.execute(_sql("fstring_709_4.sql").format(table, column, column_type))

    def _count_by(self, table: str, column: str) -> dict[str, int]:
        cursor = self.control_connection.execute(_sql("fstring_712_2.sql").format(column, table, column))
        return {str(row[0]): int(row[1]) for row in cursor.fetchall()}

    def _count_rejections_by_reason(self) -> dict[str, int]:
        cursor = self.control_connection.execute(
            _sql("execute_771.sql")
        )
        return {str(row[0]): int(row[1]) for row in cursor.fetchall()}

    def _executemany(self, statement: str, values: Iterable[tuple[Any, ...]]) -> None:
        with self._lock:
            self.connection.executemany(statement, list(values))

    def _rows_to_dicts(self, cursor: Any, rows: Iterable[Any], *, parse_metadata: bool = True) -> list[dict[str, Any]]:
        if self.backend == "duckdb":
            columns = [item[0] for item in cursor.description]
            items = [dict(zip(columns, row, strict=False)) for row in rows]
        else:
            items = [dict(row) for row in rows]
        if parse_metadata:
            for item in items:
                metadata_json = item.pop("metadata_json", None)
                if metadata_json:
                    try:
                        item["metadata"] = json.loads(metadata_json)
                    except json.JSONDecodeError:
                        item["metadata"] = {"raw": metadata_json}
                else:
                    item["metadata"] = {}
        return items

    def export_parquet(self) -> Path | None:
        if not self.parquet_catalog_path:
            return None
        with self._lock:
            rows = self.list_archives()
            self.parquet_catalog_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                import pyarrow as pa
                import pyarrow.parquet as pq

                table = pa.Table.from_pylist(rows or [{"archive_id": None}])
                pq.write_table(table, self.parquet_catalog_path)
                return self.parquet_catalog_path
            except ModuleNotFoundError:
                fallback = self.parquet_catalog_path.with_suffix(self.parquet_catalog_path.suffix + ".jsonl")
                fallback.write_text("\n".join(json.dumps(row, default=str) for row in rows) + "\n", encoding="utf-8")
                return fallback


def _metric_segment(value: str) -> str:
    return "".join(char if char.isalnum() else "_" for char in value.lower()).strip("_") or "unknown"


def _sqlite_fallback_path(path: Path) -> Path:
    if not path.exists():
        return path
    try:
        header = path.read_bytes()[:16]
    except OSError:
        return path.with_suffix(path.suffix + ".sqlite3")
    if header == b"SQLite format 3\x00":
        return path
    return path.with_suffix(path.suffix + ".sqlite3")
