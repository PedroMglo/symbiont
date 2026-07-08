"""Object-first storage control plane.

Agents create logical objects through this gateway. They never receive final
filesystem paths and never write directly into managed stores.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import os
import shutil
import stat
import tarfile
import time
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any, Iterable
from uuid import uuid4

from storage_guardian.config import StorageGuardianConfig
from storage_guardian.fallback import (
    location_for_store,
    pending_store_root_candidates,
    relative_to_store_location,
)
from storage_guardian.hashing import hash_file
from storage_guardian.index import StorageIndex
from storage_guardian.path_safety import UnsafePathError, safe_path_under_root
from storage_guardian.registry_ids import directory_id, parent_directory_id, path_hash
from storage_guardian.storage_schema import storage_schema_payload
from storage_guardian.types import StoreConfig

from storage_guardian.contracts import (
    DEFAULT_MAX_INLINE_BYTES,
    DEFAULT_UPLOAD_TTL_SECONDS,
    STORAGE_CONTRACT_VERSION,
    storage_contract_schema_hash,
)


OBJECT_DIR = ".storage_guardian_objects"
DEFAULT_ZONES = ("ingest", "validation", "approved", "quarantine")
DEFAULT_ACTIONS = (
    "create",
    "create_directory",
    "upload",
    "commit",
    "read_metadata",
    "read_text",
    "copy_object",
    "move_object",
    "rename_object",
    "delete",
    "hard_purge",
    "promote",
    "materialize",
)
DEFAULT_IDEMPOTENCY_TTL_SECONDS = 24 * 60 * 60
DEFAULT_TEXT_READ_MAX_BYTES = 256 * 1024
_MAX_LOGICAL_NAME_LENGTH = 512


@dataclass(frozen=True)
class ArchiveExtractionPlan:
    files_count: int
    total_bytes: int
    top_level_paths: list[str]


class StorageControlError(ValueError):
    """Raised when the storage gateway rejects a request."""

    def __init__(self, reason: str, message: str | None = None, *, status_code: int = 400) -> None:
        super().__init__(message or reason)
        self.reason = reason
        self.status_code = status_code


class AgentStorageGateway:
    """Policy engine and object registry facade for agent storage operations."""

    def __init__(self, config: StorageGuardianConfig, index: StorageIndex) -> None:
        self.config = config
        self.index = index

    def create_object(self, payload: dict[str, Any], *, idempotency_key: str | None) -> dict[str, Any]:
        """Create an immutable object from a small inline base64 payload."""

        self._require_enabled()
        if not idempotency_key:
            raise StorageControlError("idempotency_key_required", "Idempotency-Key is required", status_code=428)

        clean_agent, owner_cfg, store, zone = self._authorize(
            agent=str(payload["agent"]),
            action="create",
            store_name=payload.get("store"),
            zone=str(payload.get("zone") or ""),
        )
        logical_name = _validate_logical_name(str(payload["logical_name"]))
        policy = self.config.policy_for(store)
        self._validate_extension(logical_name, policy.values)

        try:
            content = base64.b64decode(str(payload["content_base64"]), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise StorageControlError("invalid_content_base64", "content_base64 must be valid base64") from exc

        max_inline = self._max_inline_bytes(store, owner_cfg)
        if len(content) > max_inline:
            raise StorageControlError("inline_payload_too_large", f"inline payload exceeds {max_inline} bytes", status_code=413)
        self._validate_quota(clean_agent, owner_cfg, len(content))

        content_hash = "sha256:" + hashlib.sha256(content).hexdigest()
        expected_hash = payload.get("sha256")
        if expected_hash and _normalize_sha256(str(expected_hash)) != content_hash:
            raise StorageControlError("checksum_mismatch", "sha256 does not match content", status_code=409)

        metadata = dict(payload.get("metadata") or {})
        metadata.update(
            {
                "logical_name": logical_name,
                "content_type": str(payload.get("content_type") or "application/octet-stream"),
                "authority": payload.get("authority"),
            }
        )
        idempotency_scope = f"{clean_agent}:{store.name}:create"
        payload_hash = _payload_hash(
            {
                "agent": clean_agent,
                "store": store.name,
                "zone": zone,
                "logical_name": logical_name,
                "content_type": metadata["content_type"],
                "sha256": content_hash,
                "metadata": payload.get("metadata") or {},
                "parent_object_id": payload.get("parent_object_id"),
            }
        )
        replay = self._idempotency_replay(idempotency_scope, idempotency_key, payload_hash)
        if replay is not None:
            return self._ensure_replayed_projection(
                replay,
                store=store,
                logical_name=logical_name,
                metadata=metadata,
            )

        object_id = _new_object_id()
        version_id = _new_version_id()
        final_path = self._object_path(store, object_id, version_id, content_hash=content_hash)
        if final_path.exists():
            if hash_file(final_path) != content_hash:
                raise StorageControlError("content_address_collision", "content-addressed object path contains different bytes", status_code=409)
        else:
            self._write_bytes_atomic(final_path, content)
        projection_path = self._write_projection_if_requested(store, final_path, logical_name, metadata)
        now = time.time()
        if projection_path is not None:
            metadata["projection_materialized_path"] = str(projection_path)
        record = self._object_record(
            object_id=object_id,
            version_id=version_id,
            store=store,
            agent=clean_agent,
            zone=zone,
            status="active",
            logical_name=logical_name,
            content_type=str(metadata["content_type"]),
            size_bytes=len(content),
            content_hash=content_hash,
            path=final_path,
            parent_object_id=payload.get("parent_object_id"),
            metadata=metadata,
            created_at=now,
            updated_at=now,
        )
        self._persist_object(record, event_type="object_created", action="create")
        response = _public_object(record)
        response["operation_id"] = self._record_operation(
            operation_type="create_object",
            actor=clean_agent,
            requesting_service=store.service,
            target_ref=str(record["storage_ref"]),
            object_id=object_id,
            content_hash=content_hash,
            idempotency_key=idempotency_key,
            hash_after=content_hash,
            metadata={"object": response},
        )
        self.index.upsert_idempotency_key(
            scope=idempotency_scope,
            idempotency_key=idempotency_key,
            payload_hash=payload_hash,
            status="completed",
            object_id=object_id,
            response=response,
            expires_at=now + self._idempotency_ttl_seconds(),
        )
        self.index.commit()
        return response

    def create_upload_session(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Create a controlled temporary upload session."""

        self._require_enabled()
        clean_agent, owner_cfg, store, zone = self._authorize(
            agent=str(payload["agent"]),
            action="upload",
            store_name=payload.get("store"),
            zone=str(payload.get("zone") or ""),
        )
        logical_name = _validate_logical_name(str(payload["logical_name"]))
        policy = self.config.policy_for(store)
        self._validate_extension(logical_name, policy.values)

        expected_size = int(payload["expected_size"])
        if expected_size < 0:
            raise StorageControlError("invalid_expected_size", "expected_size must be >= 0")
        self._validate_quota(clean_agent, owner_cfg, expected_size)
        expected_hash = _normalize_sha256(str(payload["sha256"]))
        upload_id = _new_upload_id()
        object_id = _new_object_id()
        version_id = _new_version_id()
        final_path = self._object_path(store, object_id, version_id, content_hash=expected_hash)
        temp_path = self._upload_temp_path(store, upload_id)
        temp_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path.write_bytes(b"")
        now = time.time()
        ttl_seconds = min(int(payload.get("ttl_seconds") or DEFAULT_UPLOAD_TTL_SECONDS), self._max_upload_ttl_seconds())
        record = {
            "upload_id": upload_id,
            "object_id": object_id,
            "version_id": version_id,
            "store_id": store.name,
            "created_by": clean_agent,
            "created_at": now,
            "updated_at": now,
            "expires_at": now + ttl_seconds,
            "zone": zone,
            "status": "uploading",
            "policy": store.policy,
            "logical_name": logical_name,
            "content_type": str(payload.get("content_type") or "application/octet-stream"),
            "temp_path": str(temp_path),
            "final_path": str(final_path),
            "expected_size": expected_size,
            "received_size": 0,
            "hash_algo": "sha256",
            "expected_hash": expected_hash,
            "metadata": dict(payload.get("metadata") or {}) | {"authority": payload.get("authority")},
        }
        self.index.upsert_upload_session(record)
        self.index.insert_control_event(
            event_type="upload_session_created",
            agent=clean_agent,
            action="upload",
            allowed=True,
            reason="allowed",
            object_id=object_id,
            metadata=_public_upload(record),
        )
        self.index.commit()
        return _public_upload(record)

    def append_upload_bytes(self, upload_id: str, content: bytes) -> dict[str, Any]:
        """Append bytes to an upload session owned by storage_guardian."""

        self._require_enabled()
        record = self._get_upload_or_raise(upload_id)
        self._require_uploading(record)
        temp_path = Path(str(record["temp_path"])).resolve()
        self._ensure_internal_object_path(temp_path, self._store_by_name(str(record["store_id"])))
        received = int(record.get("received_size") or 0) + len(content)
        expected = int(record["expected_size"])
        if received > expected:
            raise StorageControlError("upload_size_exceeded", "received bytes exceed expected_size", status_code=413)
        with temp_path.open("ab") as handle:
            handle.write(content)
        record["received_size"] = received
        record["updated_at"] = time.time()
        self.index.upsert_upload_session(record)
        self.index.commit()
        return _public_upload(record)

    def commit_upload(self, upload_id: str, payload: dict[str, Any], *, idempotency_key: str | None) -> dict[str, Any]:
        """Commit an upload session and make the object visible."""

        self._require_enabled()
        if not idempotency_key:
            raise StorageControlError("idempotency_key_required", "Idempotency-Key is required", status_code=428)
        record = self._get_upload_or_raise(upload_id)
        store = self._store_by_name(str(record["store_id"]))
        if record["status"] == "committed":
            object_record = self.index.get_storage_object(str(record["object_id"]))
            if object_record is not None:
                metadata = dict(record.get("metadata") or {})
                metadata.update(dict(payload.get("metadata") or {}))
                return self._ensure_replayed_projection(
                    _public_object(object_record),
                    store=store,
                    logical_name=str(record["logical_name"]),
                    metadata=metadata,
                )
        self._require_uploading(record)
        agent = str(record["created_by"])
        owner_cfg = self._owner_config(agent)
        self._validate_action("commit", owner_cfg)
        self._validate_store_access(agent, owner_cfg, store)

        expected_hash = _normalize_sha256(str(payload.get("sha256") or record["expected_hash"]))
        if expected_hash != str(record["expected_hash"]):
            raise StorageControlError("checksum_mismatch", "commit sha256 does not match upload session", status_code=409)

        idempotency_scope = f"{agent}:{store.name}:commit"
        payload_hash = _payload_hash({"upload_id": upload_id, "sha256": expected_hash})
        replay = self._idempotency_replay(idempotency_scope, idempotency_key, payload_hash)
        if replay is not None:
            metadata = dict(record.get("metadata") or {})
            metadata.update(dict(payload.get("metadata") or {}))
            return self._ensure_replayed_projection(
                replay,
                store=store,
                logical_name=str(record["logical_name"]),
                metadata=metadata,
            )

        temp_path = Path(str(record["temp_path"])).resolve()
        final_path = Path(str(record["final_path"])).resolve()
        self._ensure_internal_object_path(temp_path, store)
        self._ensure_internal_object_path(final_path, store)
        if not temp_path.is_file():
            raise StorageControlError("upload_temp_missing", "upload temporary file is missing", status_code=404)
        size = temp_path.stat().st_size
        if size != int(record["expected_size"]):
            raise StorageControlError("upload_size_mismatch", "received size does not match expected_size", status_code=409)
        actual_hash = hash_file(temp_path)
        if actual_hash != expected_hash:
            raise StorageControlError("checksum_mismatch", "uploaded bytes do not match sha256", status_code=409)

        final_path.parent.mkdir(parents=True, exist_ok=True)
        if final_path.exists():
            existing_hash = hash_file(final_path)
            if existing_hash != expected_hash:
                raise StorageControlError("object_version_exists", "object version already exists", status_code=409)
            final_size = final_path.stat().st_size
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass
            event_type = "upload_commit_reused_existing_version"
            actual_hash = existing_hash
        else:
            os.replace(temp_path, final_path)
            self._fsync_if_strong(final_path)
            final_size = size
            event_type = "upload_committed"
        self._prune_empty_upload_parents(store, temp_path)

        now = time.time()
        metadata = dict(record.get("metadata") or {})
        metadata.update(dict(payload.get("metadata") or {}))
        projection_path = self._write_projection_if_requested(store, final_path, str(record["logical_name"]), metadata)
        if projection_path is not None:
            metadata["projection_materialized_path"] = str(projection_path)
        object_record = self._object_record(
            object_id=str(record["object_id"]),
            version_id=str(record["version_id"]),
            store=store,
            agent=agent,
            zone=str(record["zone"]),
            status="active",
            logical_name=str(record["logical_name"]),
            content_type=str(record["content_type"]),
            size_bytes=final_size,
            content_hash=actual_hash,
            path=final_path,
            parent_object_id=None,
            metadata=metadata,
            created_at=float(record["created_at"]),
            updated_at=now,
        )
        record["status"] = "committed"
        record["received_size"] = final_size
        record["updated_at"] = now
        self.index.upsert_upload_session(record)
        self._persist_object(object_record, event_type=event_type, action="commit")
        response = _public_object(object_record)
        response["operation_id"] = self._record_operation(
            operation_type="commit_upload",
            actor=agent,
            requesting_service=store.service,
            source_ref=str(record["upload_id"]),
            target_ref=str(object_record["storage_ref"]),
            object_id=str(record["object_id"]),
            content_hash=actual_hash,
            idempotency_key=idempotency_key,
            hash_before=actual_hash,
            hash_after=actual_hash,
            metadata={"upload_id": upload_id, "object": response},
        )
        self.index.upsert_idempotency_key(
            scope=idempotency_scope,
            idempotency_key=idempotency_key,
            payload_hash=payload_hash,
            status="completed",
            object_id=str(record["object_id"]),
            response=response,
            expires_at=now + self._idempotency_ttl_seconds(),
        )
        self.index.commit()
        return response

    def materialize_artifact(self, payload: dict[str, Any], *, idempotency_key: str | None) -> dict[str, Any]:
        """Write a caller-provided artifact to an explicitly requested final path."""

        self._require_enabled()
        if not idempotency_key:
            raise StorageControlError("idempotency_key_required", "Idempotency-Key is required", status_code=428)

        clean_agent, owner_cfg, store, zone = self._authorize(
            agent=str(payload["agent"]),
            action="materialize",
            store_name=payload.get("store"),
            zone=str(payload.get("zone") or ""),
        )
        destination = self._materialize_destination(str(payload["destination_path"]))
        logical_name = _validate_logical_name(str(payload.get("logical_name") or destination.name))
        policy = self.config.policy_for(store)
        self._validate_extension(logical_name, policy.values)
        self._validate_extension(destination.name, policy.values)

        try:
            content = base64.b64decode(str(payload["content_base64"]), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise StorageControlError("invalid_content_base64", "content_base64 must be valid base64") from exc
        self._validate_quota(clean_agent, owner_cfg, len(content))

        content_hash = "sha256:" + hashlib.sha256(content).hexdigest()
        expected_hash = payload.get("sha256")
        if expected_hash and _normalize_sha256(str(expected_hash)) != content_hash:
            raise StorageControlError("checksum_mismatch", "sha256 does not match content", status_code=409)

        metadata = dict(payload.get("metadata") or {})
        extract_archive = bool(payload.get("extract_archive"))
        extract_destination: Path | None = None
        extract_plan: ArchiveExtractionPlan | None = None
        if extract_archive:
            raw_extract_destination = str(payload.get("extract_destination_path") or destination.with_suffix(""))
            extract_destination = self._materialize_destination(raw_extract_destination)
            if extract_destination.exists() and not extract_destination.is_dir():
                raise StorageControlError(
                    "extract_destination_not_directory",
                    f"archive extraction destination is not a directory: {extract_destination}",
                    status_code=409,
                )
            max_members = int(payload.get("max_archive_members") or 5000)
            max_bytes = int(payload.get("max_archive_uncompressed_bytes") or 512 * 1024 * 1024)
            extract_plan = _inspect_archive_for_extraction(
                content,
                extract_destination,
                max_members=max_members,
                max_uncompressed_bytes=max_bytes,
            )
        object_id = _new_object_id()
        version_id = _new_version_id()
        managed_path = self._object_path(store, object_id, version_id, content_hash=content_hash)
        response = {
            "status": "materialized",
            "agent": clean_agent,
            "store": store.name,
            "zone": zone,
            "object_id": object_id,
            "version_id": version_id,
            "logical_name": logical_name,
            "content_type": str(payload.get("content_type") or "application/octet-stream"),
            "destination_path": str(destination),
            "size_bytes": len(content),
            "sha256": content_hash,
            "metadata": metadata,
            "archive_extracted": False,
        }
        if extract_destination is not None and extract_plan is not None:
            response.update(
                {
                    "extract_destination_path": str(extract_destination),
                    "extracted_path": str(extract_destination),
                    "extracted_files_count": extract_plan.files_count,
                    "extracted_bytes": extract_plan.total_bytes,
                    "extracted_top_level_paths": extract_plan.top_level_paths,
                }
            )
        idempotency_scope = f"{clean_agent}:{store.name}:materialize"
        payload_hash = _payload_hash(
            {
                "agent": clean_agent,
                "store": store.name,
                "zone": zone,
                "logical_name": logical_name,
                "content_type": response["content_type"],
                "destination_path": str(destination),
                "sha256": content_hash,
                "metadata": metadata,
                "overwrite": bool(payload.get("overwrite")),
                "extract_archive": extract_archive,
                "extract_destination_path": str(extract_destination) if extract_destination is not None else None,
                "max_archive_members": int(payload.get("max_archive_members") or 5000),
                "max_archive_uncompressed_bytes": int(payload.get("max_archive_uncompressed_bytes") or 512 * 1024 * 1024),
            }
        )
        existing_idempotency = self.index.get_idempotency_key(idempotency_scope, idempotency_key)
        if existing_idempotency is not None:
            if str(existing_idempotency.get("payload_hash")) != payload_hash:
                raise StorageControlError(
                    "idempotency_conflict",
                    "Idempotency-Key was reused with a different payload",
                    status_code=409,
                )
            if destination.exists() and hash_file(destination) == content_hash:
                if extract_archive and extract_destination is not None:
                    _extract_archive_content(
                        content,
                        extract_destination,
                        replace_top_level_paths=(
                            extract_plan.top_level_paths
                            if extract_plan is not None and bool(payload.get("overwrite"))
                            else ()
                        ),
                    )
                    response["archive_extracted"] = True
                stored_response = existing_idempotency.get("response")
                if isinstance(stored_response, dict) and stored_response:
                    return {**stored_response, "archive_extracted": response["archive_extracted"]}
                return response
            if destination.exists():
                raise StorageControlError(
                    "materialized_destination_changed",
                    f"destination exists with unexpected content: {destination}",
                    status_code=409,
                )
            self._write_bytes_atomic(destination, content)
            if hash_file(destination) != content_hash:
                raise StorageControlError("checksum_mismatch", "materialized file hash mismatch", status_code=500)
            if extract_archive and extract_destination is not None:
                _extract_archive_content(
                    content,
                    extract_destination,
                    replace_top_level_paths=(
                        extract_plan.top_level_paths
                        if extract_plan is not None and bool(payload.get("overwrite"))
                        else ()
                    ),
                )
                response["archive_extracted"] = True
            stored_response = existing_idempotency.get("response")
            if isinstance(stored_response, dict) and stored_response:
                return {**stored_response, "archive_extracted": response["archive_extracted"]}
            return response

        if destination.exists() and not bool(payload.get("overwrite")):
            raise StorageControlError("destination_exists", f"destination already exists: {destination}", status_code=409)
        if destination.exists() and destination.is_dir():
            raise StorageControlError("destination_is_directory", f"destination is a directory: {destination}", status_code=409)

        self._write_bytes_atomic(destination, content)
        if hash_file(destination) != content_hash:
            raise StorageControlError("checksum_mismatch", "materialized file hash mismatch", status_code=500)
        if managed_path.exists():
            if hash_file(managed_path) != content_hash:
                raise StorageControlError("content_address_collision", "content-addressed object path contains different bytes", status_code=409)
        else:
            self._write_bytes_atomic(managed_path, content)
        if extract_archive and extract_destination is not None:
            _extract_archive_content(
                content,
                extract_destination,
                replace_top_level_paths=(
                    extract_plan.top_level_paths
                    if extract_plan is not None and bool(payload.get("overwrite"))
                    else ()
                ),
            )
            response["archive_extracted"] = True
        now = time.time()
        object_record = self._object_record(
            object_id=object_id,
            version_id=version_id,
            store=store,
            agent=clean_agent,
            zone=zone,
            status="materialized",
            logical_name=logical_name,
            content_type=str(response["content_type"]),
            size_bytes=len(content),
            content_hash=content_hash,
            path=managed_path,
            parent_object_id=None,
            metadata={
                **metadata,
                "materialized_ref": str(destination),
                "artifact_materialized": True,
            },
            created_at=now,
            updated_at=now,
        )
        object_record["materialized_ref"] = str(destination)
        self._persist_object(object_record, event_type="artifact_materialized_object", action="materialize")
        response["storage_ref"] = object_record["storage_ref"]
        response["operation_id"] = self._record_operation(
            operation_type="materialize",
            actor=clean_agent,
            requesting_service=store.service,
            target_ref=str(destination),
            object_id=object_id,
            content_hash=content_hash,
            idempotency_key=idempotency_key,
            hash_after=content_hash,
            metadata=response,
        )
        self.index.insert_control_event(
            event_type="artifact_materialized",
            agent=clean_agent,
            action="materialize",
            allowed=True,
            reason="allowed",
            object_id=None,
            metadata=response,
        )
        self.index.upsert_idempotency_key(
            scope=idempotency_scope,
            idempotency_key=idempotency_key,
            payload_hash=payload_hash,
            status="completed",
            object_id=None,
            response=response,
            expires_at=now + self._idempotency_ttl_seconds(),
        )
        self.index.commit()
        return response

    def promote_object(self, object_id: str, *, agent: str, target_zone: str) -> dict[str, Any]:
        """Promote an object by changing governance metadata, not its immutable blob."""

        self._require_enabled()
        object_record = self._get_object_or_raise(object_id)
        clean_agent = _safe_segment(agent, "agent")
        owner_cfg = self._owner_config(clean_agent)
        self._validate_action("promote", owner_cfg)
        self._validate_object_actor(clean_agent, object_record)
        target_zone = self._validate_zone(target_zone, owner_cfg)
        store = self._store_by_name(str(object_record["store_id"]))
        updated = dict(object_record)
        updated["zone"] = target_zone
        updated["updated_at"] = time.time()
        self.index.upsert_storage_object(updated)
        self.index.insert_control_event(
            event_type="object_promoted",
            agent=clean_agent,
            action="promote",
            allowed=True,
            reason="allowed",
            object_id=str(object_record["object_id"]),
            metadata={"store": store.name, "from_zone": object_record.get("zone"), "to_zone": target_zone},
        )
        response = _public_object(updated)
        response["operation_id"] = self._record_operation(
            operation_type="promote",
            actor=clean_agent,
            requesting_service=store.service,
            source_ref=str(object_record.get("storage_ref") or object_id),
            target_ref=f"zone:{target_zone}",
            object_id=str(object_record["object_id"]),
            content_hash=str(object_record.get("content_hash") or ""),
            hash_before=str(object_record.get("content_hash") or ""),
            hash_after=str(object_record.get("content_hash") or ""),
            metadata={"from_zone": object_record.get("zone"), "to_zone": target_zone},
        )
        self.index.commit()
        return response

    def soft_delete_object(self, object_id: str, *, agent: str, reason: str | None = None) -> dict[str, Any]:
        """Soft-delete an object; hard purge is intentionally not agent-facing."""

        self._require_enabled()
        object_record = self._get_object_or_raise(object_id)
        clean_agent = _safe_segment(agent, "agent")
        owner_cfg = self._owner_config(clean_agent)
        self._validate_action("delete", owner_cfg)
        self._validate_object_actor(clean_agent, object_record)
        updated = dict(object_record)
        updated["status"] = "soft_deleted"
        updated["updated_at"] = time.time()
        metadata = dict(updated.get("metadata") or {})
        metadata["delete_reason"] = reason or "soft_delete"
        updated["metadata"] = metadata
        self.index.upsert_storage_object(updated)
        self.index.insert_control_event(
            event_type="object_soft_deleted",
            agent=clean_agent,
            action="delete",
            allowed=True,
            reason=reason or "soft_delete",
            object_id=str(object_record["object_id"]),
            metadata={"from": object_record, "to": updated},
        )
        response = _public_object(updated)
        response["operation_id"] = self._record_operation(
            operation_type="soft_delete",
            actor=clean_agent,
            requesting_service=str(object_record.get("owner_service") or object_record.get("store_id") or "storage_guardian"),
            source_ref=str(object_record.get("storage_ref") or object_id),
            object_id=str(object_record["object_id"]),
            content_hash=str(object_record.get("content_hash") or ""),
            hash_before=str(object_record.get("content_hash") or ""),
            hash_after=str(object_record.get("content_hash") or ""),
            metadata={"reason": reason or "soft_delete"},
        )
        self.index.commit()
        return response

    def create_directory(self, payload: dict[str, Any], *, idempotency_key: str | None) -> dict[str, Any]:
        """Create or register a governed directory under a managed store."""

        self._require_enabled()
        if not idempotency_key:
            raise StorageControlError("idempotency_key_required", "Idempotency-Key is required", status_code=428)
        clean_agent, _, store, zone = self._authorize(
            agent=str(payload["agent"]),
            action="create_directory",
            store_name=payload.get("store"),
            zone=str(payload.get("zone") or ""),
        )
        relative = _safe_projection_path(str(payload["relative_path"]))
        if OBJECT_DIR in relative.parts:
            raise StorageControlError("invalid_directory_path", "directory path cannot target internal object storage")
        root = location_for_store(self.config, store).root.resolve()
        path = (root / relative).resolve()
        if not path.is_relative_to(root):
            raise StorageControlError("path_escape", "directory path escaped managed root", status_code=403)
        idempotency_scope = f"{clean_agent}:{store.name}:create_directory"
        payload_hash = _payload_hash({"agent": clean_agent, "store": store.name, "zone": zone, "relative_path": relative.as_posix()})
        replay = self._idempotency_replay(idempotency_scope, idempotency_key, payload_hash)
        if replay is not None:
            return replay
        if path.exists() and not path.is_dir():
            raise StorageControlError("directory_path_conflict", "target exists and is not a directory", status_code=409)
        path.mkdir(parents=True, exist_ok=True)
        now = time.time()
        dir_id = directory_id(store.service, store.name, relative.as_posix())
        record = {
            "directory_id": dir_id,
            "service": store.service,
            "store_id": store.name,
            "relative_path": relative.as_posix(),
            "parent_directory_id": parent_directory_id(store.service, store.name, relative),
            "owner": store.owner,
            "zone": zone,
            "mode": store.mode,
            "policy": store.policy,
            "expected_by_schema": False,
            "status": "active",
            "created_by": clean_agent,
            "created_at": now,
            "last_seen_at": now,
            "protected": False,
            "writable_by_storage_guardian": True,
            "readable_by_callers": True,
            "absolute_path_hash": path_hash(path),
            "metadata": {"path_kind": "managed_directory"},
        }
        self.index.upsert_directory(record)
        response = _public_directory(record)
        response["operation_id"] = self._record_operation(
            operation_type="create_directory",
            actor=clean_agent,
            requesting_service=store.service,
            target_ref=f"directory:{dir_id}",
            target_directory_id=dir_id,
            idempotency_key=idempotency_key,
            metadata=response,
        )
        self.index.upsert_idempotency_key(
            scope=idempotency_scope,
            idempotency_key=idempotency_key,
            payload_hash=payload_hash,
            status="completed",
            object_id=None,
            response=response,
            expires_at=now + self._idempotency_ttl_seconds(),
        )
        self.index.commit()
        return response

    def copy_object(self, object_id: str, payload: dict[str, Any], *, idempotency_key: str | None) -> dict[str, Any]:
        return self._copy_or_move_object(object_id, payload, idempotency_key=idempotency_key, move=False)

    def move_object(self, object_id: str, payload: dict[str, Any], *, idempotency_key: str | None) -> dict[str, Any]:
        return self._copy_or_move_object(object_id, payload, idempotency_key=idempotency_key, move=True)

    def rename_object(self, object_id: str, payload: dict[str, Any], *, idempotency_key: str | None) -> dict[str, Any]:
        self._require_enabled()
        if not idempotency_key:
            raise StorageControlError("idempotency_key_required", "Idempotency-Key is required", status_code=428)
        object_record = self._get_object_or_raise(object_id)
        clean_agent = _safe_segment(str(payload["agent"]), "agent")
        owner_cfg = self._owner_config(clean_agent)
        self._validate_action("rename_object", owner_cfg)
        self._validate_object_actor(clean_agent, object_record)
        store = self._store_by_name(str(object_record["store_id"]))
        self._validate_store_access(clean_agent, owner_cfg, store)
        logical_name = _validate_logical_name(str(payload["logical_name"]))
        self._validate_extension(logical_name, self.config.policy_for(store).values)
        idempotency_scope = f"{clean_agent}:{store.name}:rename_object"
        payload_hash = _payload_hash({"object_id": object_id, "logical_name": logical_name})
        replay = self._idempotency_replay(idempotency_scope, idempotency_key, payload_hash)
        if replay is not None:
            return replay
        previous_name = str(object_record.get("logical_name") or "")
        updated = dict(object_record)
        metadata = dict(updated.get("metadata") or {})
        metadata["renamed_from"] = previous_name
        updated["logical_name"] = logical_name
        updated["metadata"] = metadata
        updated["updated_at"] = time.time()
        self.index.upsert_storage_object(updated)
        response = _public_object(updated)
        response["operation_id"] = self._record_operation(
            operation_type="rename_object",
            actor=clean_agent,
            requesting_service=store.service,
            source_ref=previous_name,
            target_ref=logical_name,
            object_id=str(object_record["object_id"]),
            content_hash=str(object_record.get("content_hash") or ""),
            idempotency_key=idempotency_key,
            hash_before=str(object_record.get("content_hash") or ""),
            hash_after=str(object_record.get("content_hash") or ""),
            metadata={"from_logical_name": previous_name, "to_logical_name": logical_name},
        )
        now = time.time()
        self.index.upsert_idempotency_key(
            scope=idempotency_scope,
            idempotency_key=idempotency_key,
            payload_hash=payload_hash,
            status="completed",
            object_id=str(object_record["object_id"]),
            response=response,
            expires_at=now + self._idempotency_ttl_seconds(),
        )
        self.index.commit()
        return response

    def hard_purge_object(self, object_id: str, payload: dict[str, Any], *, idempotency_key: str | None) -> dict[str, Any]:
        self._require_enabled()
        if not idempotency_key:
            raise StorageControlError("idempotency_key_required", "Idempotency-Key is required", status_code=428)
        if not bool(self._control_config().get("hard_purge_enabled", False)):
            raise StorageControlError("hard_purge_blocked", "hard purge is disabled by storage policy", status_code=403)
        object_record = self._get_object_or_raise(object_id)
        clean_agent = _safe_segment(str(payload["agent"]), "agent")
        owner_cfg = self._owner_config(clean_agent)
        self._validate_action("hard_purge", owner_cfg)
        self._validate_object_actor(clean_agent, object_record)
        store = self._store_by_name(str(object_record["store_id"]))
        if not bool(self.config.policy_for(store).values.get("hard_purge_allowed", False)):
            raise StorageControlError("hard_purge_blocked", "store policy does not allow hard purge", status_code=403)
        if not bool(payload.get("confirm")):
            raise StorageControlError("hard_purge_confirmation_required", "hard purge requires confirm=true", status_code=428)
        content_hash = str(object_record.get("content_hash") or "")
        other_refs = [
            item
            for item in self.index.list_storage_objects()
            if item.get("object_id") != object_record.get("object_id")
            and item.get("content_hash") == content_hash
            and item.get("status") not in {"hard_purged"}
        ]
        if other_refs:
            raise StorageControlError("content_still_referenced", "content hash still has live registry references", status_code=409)
        path = Path(str(object_record.get("current_path") or ""))
        self._ensure_internal_object_path(path, store)
        idempotency_scope = f"{clean_agent}:{store.name}:hard_purge"
        payload_hash = _payload_hash({"object_id": object_id, "content_hash": content_hash, "confirm": True})
        replay = self._idempotency_replay(idempotency_scope, idempotency_key, payload_hash)
        if replay is not None:
            return replay
        if path.exists():
            path.unlink()
        updated = dict(object_record)
        updated["status"] = "hard_purged"
        updated["updated_at"] = time.time()
        self.index.upsert_storage_object(updated)
        response = _public_object(updated)
        response["operation_id"] = self._record_operation(
            operation_type="hard_purge",
            actor=clean_agent,
            requesting_service=store.service,
            source_ref=str(object_record.get("storage_ref") or object_id),
            object_id=str(object_record["object_id"]),
            content_hash=content_hash,
            idempotency_key=idempotency_key,
            hash_before=content_hash,
            metadata={"reason": payload.get("reason") or "hard_purge"},
        )
        now = time.time()
        self.index.upsert_idempotency_key(
            scope=idempotency_scope,
            idempotency_key=idempotency_key,
            payload_hash=payload_hash,
            status="completed",
            object_id=str(object_record["object_id"]),
            response=response,
            expires_at=now + self._idempotency_ttl_seconds(),
        )
        self.index.commit()
        return response

    def list_objects(self, *, agent: str | None = None, zone: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        return [_public_object(item) for item in self.index.list_storage_objects(agent=agent, zone=zone, status=status)]

    def get_object(self, object_id: str) -> dict[str, Any] | None:
        item = self.index.get_storage_object(_safe_object_id(object_id))
        return _public_object(item) if item else None

    def read_object_text(self, object_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Read a bounded UTF-8 excerpt from an active object owned by the caller."""

        self._require_enabled()
        object_record = self._get_object_or_raise(object_id)
        clean_agent = _safe_segment(str(payload["agent"]), "agent")
        owner_cfg = self._owner_config(clean_agent)
        self._validate_action("read_text", owner_cfg)
        self._validate_object_actor(clean_agent, object_record)
        if str(object_record.get("status") or "") != "active":
            raise StorageControlError("object_not_active", "only active objects can be read as text", status_code=409)
        store = self._store_by_name(str(object_record["store_id"]))
        self._validate_store_access(clean_agent, owner_cfg, store)
        path = Path(str(object_record.get("current_path") or ""))
        self._ensure_internal_object_path(path, store)
        if not path.is_file():
            raise StorageControlError("object_blob_missing", "object blob is missing", status_code=404)
        max_bytes = self._text_read_max_bytes(payload.get("max_bytes"))
        with path.open("rb") as handle:
            raw = handle.read(max_bytes + 1)
        truncated = len(raw) > max_bytes
        data = raw[:max_bytes]
        response = {
            **_public_object(object_record),
            "encoding": "utf-8",
            "bytes_read": len(data),
            "max_bytes": max_bytes,
            "truncated": truncated,
            "text": data.decode("utf-8", errors="replace"),
        }
        self.index.insert_control_event(
            event_type="object_text_read",
            agent=clean_agent,
            action="read_text",
            allowed=True,
            reason="allowed",
            object_id=str(object_record["object_id"]),
            metadata={
                "store": store.name,
                "bytes_read": response["bytes_read"],
                "truncated": truncated,
                "content_type": response.get("content_type"),
            },
        )
        self.index.commit()
        return response

    def cleanup_expired_uploads(self) -> dict[str, Any]:
        now = time.time()
        cleaned = 0
        for record in self.index.expired_upload_sessions(now):
            record["status"] = "expired"
            record["updated_at"] = now
            temp_path = Path(str(record.get("temp_path") or ""))
            store: StoreConfig | None = None
            try:
                store = self._store_by_name(str(record.get("store_id") or ""))
            except StorageControlError:
                store = None
            try:
                if temp_path.exists():
                    temp_path.unlink()
                    cleaned += 1
            except OSError:
                pass
            if store is not None:
                self._prune_empty_upload_parents(store, temp_path)
            self.index.upsert_upload_session(record)
            self.index.insert_control_event(
                event_type="upload_session_expired",
                agent=str(record.get("created_by") or "system"),
                action="cleanup",
                allowed=True,
                reason="expired",
                object_id=record.get("object_id"),
                metadata=_public_upload(record),
            )
        self.index.commit()
        return {"expired_uploads": cleaned}

    def policies(self) -> dict[str, Any]:
        cfg = self._control_config()
        owners = cfg.get("owners", {})
        schema = storage_schema_payload(self.config.root)
        return {
            "enabled": bool(cfg.get("enabled", True)),
            "zones": self._zones(),
            "require_known_agent": self._require_known_agent(),
            "owners": owners,
            "storage_contract_version": STORAGE_CONTRACT_VERSION,
            "api_schema_hash": storage_contract_schema_hash(),
            "storage_schema": {
                "version": schema["version"],
                "hash": schema["computed_schema_hash"],
                "locked": schema["locked"],
            },
            "stores": [self._store_capabilities(store) for store in self.config.stores],
        }

    def _copy_or_move_object(
        self,
        object_id: str,
        payload: dict[str, Any],
        *,
        idempotency_key: str | None,
        move: bool,
    ) -> dict[str, Any]:
        self._require_enabled()
        if not idempotency_key:
            raise StorageControlError("idempotency_key_required", "Idempotency-Key is required", status_code=428)
        object_record = self._get_object_or_raise(object_id)
        if str(object_record.get("status") or "") not in {"active", "materialized"}:
            raise StorageControlError("object_not_active", "only active/materialized objects can be copied or moved", status_code=409)
        clean_agent = _safe_segment(str(payload["agent"]), "agent")
        owner_cfg = self._owner_config(clean_agent)
        action = "move_object" if move else "copy_object"
        self._validate_action(action, owner_cfg)
        self._validate_object_actor(clean_agent, object_record)
        source_store = self._store_by_name(str(object_record["store_id"]))
        target_store = self._resolve_store(clean_agent, owner_cfg, payload.get("target_store") or source_store.name)
        self._validate_store_access(clean_agent, owner_cfg, target_store)
        self._validate_store_mode_for_write(target_store)
        target_zone = self._validate_zone(str(payload.get("target_zone") or object_record.get("zone") or ""), owner_cfg)
        target_logical_name = _validate_logical_name(str(payload.get("target_logical_name") or object_record.get("logical_name") or object_id))
        self._validate_extension(target_logical_name, self.config.policy_for(target_store).values)
        source_path = Path(str(object_record.get("current_path") or ""))
        self._ensure_internal_object_path(source_path, source_store)
        if not source_path.is_file():
            raise StorageControlError("object_blob_missing", "object blob is missing", status_code=404)
        content_hash = str(object_record.get("content_hash") or hash_file(source_path))
        if hash_file(source_path) != content_hash:
            raise StorageControlError("checksum_mismatch", "source object hash no longer matches registry", status_code=409)
        idempotency_scope = f"{clean_agent}:{target_store.name}:{action}"
        payload_hash = _payload_hash(
            {
                "object_id": object_id,
                "target_store": target_store.name,
                "target_zone": target_zone,
                "target_logical_name": target_logical_name,
                "move": move,
            }
        )
        replay = self._idempotency_replay(idempotency_scope, idempotency_key, payload_hash)
        if replay is not None:
            return replay

        target_object_id = _new_object_id()
        target_version_id = _new_version_id()
        target_path = self._object_path(target_store, target_object_id, target_version_id, content_hash=content_hash)
        if target_path.exists():
            if hash_file(target_path) != content_hash:
                raise StorageControlError("content_address_collision", "content-addressed object path contains different bytes", status_code=409)
        else:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            temp = target_path.with_name(f".{target_path.name}.tmp-{uuid4().hex}")
            shutil.copyfile(source_path, temp)
            self._fsync_if_strong(temp)
            os.replace(temp, target_path)
            self._fsync_if_strong(target_path)
        now = time.time()
        metadata = dict(object_record.get("metadata") or {})
        metadata.update(
            {
                "lineage_root": object_record.get("lineage_root") or object_record.get("object_id"),
                "source_object_id": object_record.get("object_id"),
                "source_storage_ref": object_record.get("storage_ref"),
                "operation": action,
            }
        )
        target_record = self._object_record(
            object_id=target_object_id,
            version_id=target_version_id,
            store=target_store,
            agent=clean_agent,
            zone=target_zone,
            status="active",
            logical_name=target_logical_name,
            content_type=str(object_record.get("content_type") or "application/octet-stream"),
            size_bytes=int(object_record.get("size_bytes") or target_path.stat().st_size),
            content_hash=content_hash,
            path=target_path,
            parent_object_id=str(object_record.get("object_id") or ""),
            metadata=metadata,
            created_at=now,
            updated_at=now,
        )
        target_record["source_ref"] = str(object_record.get("storage_ref") or object_record.get("object_id") or "")
        target_record["lineage_root"] = str(object_record.get("lineage_root") or object_record.get("object_id") or target_object_id)
        self._persist_object(target_record, event_type="object_moved" if move else "object_copied", action=action)
        response: dict[str, Any] = {"target": _public_object(target_record)}
        source_after: dict[str, Any] | None = None
        if move:
            source_after = dict(object_record)
            source_metadata = dict(source_after.get("metadata") or {})
            source_metadata["moved_to_object_id"] = target_object_id
            source_metadata["moved_to_storage_ref"] = target_record["storage_ref"]
            source_after["metadata"] = source_metadata
            source_after["status"] = "moved"
            source_after["updated_at"] = now
            self.index.upsert_storage_object(source_after)
            response["source"] = _public_object(source_after)
        response["operation_id"] = self._record_operation(
            operation_type=action,
            actor=clean_agent,
            requesting_service=target_store.service,
            source_ref=str(object_record.get("storage_ref") or object_record.get("object_id") or ""),
            target_ref=str(target_record["storage_ref"]),
            object_id=target_object_id,
            content_hash=content_hash,
            idempotency_key=idempotency_key,
            hash_before=content_hash,
            hash_after=content_hash,
            metadata=response,
        )
        self.index.upsert_idempotency_key(
            scope=idempotency_scope,
            idempotency_key=idempotency_key,
            payload_hash=payload_hash,
            status="completed",
            object_id=target_object_id,
            response=response,
            expires_at=now + self._idempotency_ttl_seconds(),
        )
        self.index.commit()
        return response

    def _persist_object(self, record: dict[str, Any], *, event_type: str, action: str) -> None:
        version = {
            "version_id": record["latest_version_id"],
            "object_id": record["object_id"],
            "store_id": record["store_id"],
            "created_by": record["created_by"],
            "created_at": record["created_at"],
            "zone": record["zone"],
            "status": record["status"],
            "policy": record["policy"],
            "logical_name": record["logical_name"],
            "content_type": record["content_type"],
            "current_path": record["current_path"],
            "relative_path": record["relative_path"],
            "size_bytes": record["size_bytes"],
            "hash_algo": record["hash_algo"],
            "content_hash": record["content_hash"],
            "parent_object_id": record.get("parent_object_id"),
            "metadata": record.get("metadata") or {},
        }
        self.index.upsert_storage_object(record)
        self.index.upsert_storage_version(version)
        self.index.insert_control_event(
            event_type=event_type,
            agent=str(record["created_by"]),
            action=action,
            allowed=True,
            reason="allowed",
            object_id=str(record["object_id"]),
            metadata=record,
        )

    def _record_operation(
        self,
        *,
        operation_type: str,
        actor: str,
        requesting_service: str,
        source_ref: str | None = None,
        target_ref: str | None = None,
        source_directory_id: str | None = None,
        target_directory_id: str | None = None,
        object_id: str | None = None,
        directory_id: str | None = None,
        content_hash: str | None = None,
        idempotency_key: str | None = None,
        hash_before: str | None = None,
        hash_after: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        operation_id = _new_operation_id()
        custody_event_id = self.index.insert_custody_event(
            {
                "custody_event_id": _new_custody_event_id(),
                "event_type": _custody_event_type(operation_type),
                "actor": actor,
                "requesting_service": requesting_service,
                "operation_id": operation_id,
                "object_id": object_id,
                "directory_id": directory_id or target_directory_id or source_directory_id,
                "source_ref": source_ref,
                "target_ref": target_ref,
                "content_hash": content_hash,
                "metadata": metadata or {},
            }
        )
        self.index.insert_operation(
            {
                "operation_id": operation_id,
                "operation_type": operation_type,
                "actor": actor,
                "requesting_service": requesting_service,
                "source_ref": source_ref,
                "target_ref": target_ref,
                "source_directory_id": source_directory_id,
                "target_directory_id": target_directory_id,
                "policy_decision": "allowed",
                "dry_run_result": {"allowed": True},
                "idempotency_key": idempotency_key,
                "preconditions": {},
                "hash_before": hash_before,
                "hash_after": hash_after,
                "status": "completed",
                "started_at": time.time(),
                "finished_at": time.time(),
                "custody_event_id": custody_event_id,
                "rollback_plan": {"strategy": "registry_compensating_operation"},
                "metadata": metadata or {},
            }
        )
        return operation_id

    def _object_record(
        self,
        *,
        object_id: str,
        version_id: str,
        store: StoreConfig,
        agent: str,
        zone: str,
        status: str,
        logical_name: str,
        content_type: str,
        size_bytes: int,
        content_hash: str,
        path: Path,
        parent_object_id: str | None,
        metadata: dict[str, Any],
        created_at: float,
        updated_at: float,
    ) -> dict[str, Any]:
        return {
            "object_id": object_id,
            "latest_version_id": version_id,
            "store_id": store.name,
            "store": store.name,
            "created_by": agent,
            "created_at": created_at,
            "updated_at": updated_at,
            "purpose": "managed_object",
            "zone": zone,
            "status": status,
            "policy": store.policy,
            "current_path": str(path),
            "relative_path": relative_to_store_location(self.config, store, path),
            "size_bytes": size_bytes,
            "hash_algo": "sha256",
            "content_hash": content_hash,
            "sha256": content_hash,
            "source_file": None,
            "source_content_hash": None,
            "parent_object_id": parent_object_id,
            "model": None,
            "logical_name": logical_name,
            "content_type": content_type,
            "directory_id": directory_id(store.service, store.name, "."),
            "owner_service": store.service,
            "producer_agent": agent,
            "object_kind": _object_kind(logical_name, content_type),
            "media_type": content_type,
            "semantic_role": metadata.get("semantic_role"),
            "source_ref": metadata.get("source_ref"),
            "lineage_root": metadata.get("lineage_root") or object_id,
            "storage_ref": f"storage://{store.service}/{store.name}/{object_id}/{version_id}",
            "materialized_ref": metadata.get("materialized_ref"),
            "retention_policy": metadata.get("retention_policy"),
            "metadata": metadata,
        }

    def _authorize(
        self,
        *,
        agent: str,
        action: str,
        store_name: str | None,
        zone: str,
    ) -> tuple[str, dict[str, Any], StoreConfig, str]:
        clean_agent = _safe_segment(agent, "agent")
        owner_cfg = self._owner_config(clean_agent)
        self._validate_action(action, owner_cfg)
        target_zone = self._validate_zone(zone or str(owner_cfg.get("default_zone") or self._control_config().get("default_zone", "ingest")), owner_cfg)
        store = self._resolve_store(clean_agent, owner_cfg, store_name)
        self._validate_store_access(clean_agent, owner_cfg, store)
        self._validate_store_mode_for_write(store)
        return clean_agent, owner_cfg, store, target_zone

    def _control_config(self) -> dict[str, Any]:
        return dict(self.config.root.get("storage_control", {}))

    def _require_enabled(self) -> None:
        if not self._control_config().get("enabled", True):
            raise StorageControlError("storage_control_disabled", "storage control plane is disabled", status_code=503)

    def _zones(self) -> tuple[str, ...]:
        zones = self._control_config().get("zones", DEFAULT_ZONES)
        return tuple(str(zone) for zone in zones)

    def _require_known_agent(self) -> bool:
        return bool(self._control_config().get("require_known_agent", True))

    def _owner_config(self, agent: str) -> dict[str, Any]:
        owners = self._control_config().get("owners", {})
        if agent in owners:
            return dict(owners[agent] or {})
        if self._require_known_agent():
            raise StorageControlError("unknown_agent", f"agent is not registered for storage control: {agent}", status_code=403)
        return {}

    def _validate_action(self, action: str, owner_cfg: dict[str, Any]) -> None:
        allowed = tuple(str(item) for item in owner_cfg.get("allowed_actions", DEFAULT_ACTIONS))
        if action not in allowed:
            raise StorageControlError("action_not_allowed", f"action {action!r} is not allowed", status_code=403)

    def _validate_zone(self, zone: str, owner_cfg: dict[str, Any]) -> str:
        clean_zone = _safe_segment(zone, "ingest")
        if clean_zone not in self._zones():
            raise StorageControlError("zone_not_registered", f"zone is not registered: {clean_zone}")
        allowed = tuple(str(item) for item in owner_cfg.get("allowed_zones", self._zones()))
        if clean_zone not in allowed:
            raise StorageControlError("zone_not_allowed", f"zone {clean_zone!r} is not allowed", status_code=403)
        return clean_zone

    def _resolve_store(self, agent: str, owner_cfg: dict[str, Any], store_name: str | None) -> StoreConfig:
        selected = store_name or owner_cfg.get("default_store")
        if selected:
            return self._store_by_name(str(selected))
        for store in self.config.stores:
            if store.owner == agent:
                return store
        raise StorageControlError("store_not_registered", "request did not resolve to a registered store")

    def _store_by_name(self, store_name: str) -> StoreConfig:
        for store in self.config.stores:
            if store.name == store_name:
                return store
        raise StorageControlError("store_not_registered", f"store is not registered: {store_name}", status_code=404)

    def _validate_store_access(self, agent: str, owner_cfg: dict[str, Any], store: StoreConfig) -> None:
        if not store.enabled:
            raise StorageControlError("store_disabled", f"store is disabled: {store.name}", status_code=403)
        allowed = owner_cfg.get("allowed_stores")
        if allowed is None:
            allowed = [store.name for store in self.config.stores if store.owner in {agent, owner_cfg.get("owner_alias", agent)}]
        allowed = tuple(str(item) for item in allowed)
        if "*" not in allowed and store.name not in allowed:
            raise StorageControlError("store_not_allowed", f"agent {agent!r} cannot use store {store.name!r}", status_code=403)

    @staticmethod
    def _validate_store_mode_for_write(store: StoreConfig) -> None:
        if store.mode in {"catalog_only", "snapshot_only"}:
            raise StorageControlError("store_not_writable", f"store {store.name!r} is {store.mode}", status_code=403)

    @staticmethod
    def _validate_extension(logical_name: str, policy: dict[str, Any]) -> None:
        extensions = tuple(str(ext).lower() for ext in policy.get("extensions", ()))
        if not extensions:
            return
        suffix = Path(logical_name).suffix.lower()
        if suffix not in extensions:
            raise StorageControlError("extension_not_allowed", f"extension {suffix!r} is not allowed by policy")

    def _validate_quota(self, agent: str, owner_cfg: dict[str, Any], size_estimate: int) -> None:
        quota_gb = owner_cfg.get("quota_gb")
        if quota_gb is None:
            return
        quota_bytes = int(float(quota_gb) * 1024 * 1024 * 1024)
        used = self.index.storage_usage_bytes(agent)
        if used + size_estimate > quota_bytes:
            raise StorageControlError("quota_exceeded", f"quota exceeded for {agent}", status_code=403)

    def _text_read_max_bytes(self, requested: Any) -> int:
        try:
            value = int(requested or 12_000)
        except (TypeError, ValueError) as exc:
            raise StorageControlError("invalid_max_bytes", "max_bytes must be an integer") from exc
        if value < 1:
            raise StorageControlError("invalid_max_bytes", "max_bytes must be >= 1")
        configured = int(self._control_config().get("max_object_text_read_bytes") or DEFAULT_TEXT_READ_MAX_BYTES)
        return min(value, max(1, configured))

    def _get_object_or_raise(self, object_id: str) -> dict[str, Any]:
        object_record = self.index.get_storage_object(_safe_object_id(object_id))
        if object_record is None:
            raise StorageControlError("object_not_found", f"object not found: {object_id}", status_code=404)
        return object_record

    def _get_upload_or_raise(self, upload_id: str) -> dict[str, Any]:
        record = self.index.get_upload_session(_safe_object_id(upload_id))
        if record is None:
            raise StorageControlError("upload_not_found", f"upload not found: {upload_id}", status_code=404)
        return record

    def _require_uploading(self, record: dict[str, Any]) -> None:
        if record["status"] != "uploading":
            raise StorageControlError("upload_not_open", f"upload is {record['status']}", status_code=409)
        if float(record["expires_at"]) < time.time():
            record["status"] = "expired"
            self.index.upsert_upload_session(record)
            self.index.commit()
            raise StorageControlError("upload_expired", "upload session expired", status_code=410)

    @staticmethod
    def _validate_object_actor(agent: str, object_record: dict[str, Any]) -> None:
        if str(object_record.get("created_by")) != agent:
            raise StorageControlError("object_owner_mismatch", "agent does not own this object", status_code=403)

    def _object_path(self, store: StoreConfig, object_id: str, version_id: str, *, content_hash: str | None = None) -> Path:
        root = location_for_store(self.config, store).root
        if content_hash:
            digest = _normalize_sha256(content_hash).removeprefix("sha256:")
            return root / OBJECT_DIR / "objects" / "sha256" / digest[:2] / digest[2:4] / digest
        shard = object_id.removeprefix("obj_")[:4]
        return root / OBJECT_DIR / "objects_by_id" / shard[:2] / shard[2:] / object_id / version_id

    def _upload_temp_path(self, store: StoreConfig, upload_id: str) -> Path:
        root = location_for_store(self.config, store).root
        shard = upload_id.removeprefix("upl_")[:4]
        return root / OBJECT_DIR / "uploads" / shard[:2] / shard[2:] / f"{upload_id}.part"

    def _prune_empty_upload_parents(self, store: StoreConfig, temp_path: Path) -> None:
        uploads_root = (location_for_store(self.config, store).root / OBJECT_DIR / "uploads").resolve()
        current = temp_path.parent.resolve()
        while current != uploads_root and _is_relative_to(current, uploads_root):
            try:
                current.rmdir()
            except OSError:
                return
            current = current.parent

    def _write_projection_if_requested(
        self,
        store: StoreConfig,
        source_path: Path,
        logical_name: str,
        metadata: dict[str, Any],
    ) -> Path | None:
        projection = str(metadata.get("projection_path") or "").strip()
        if not projection:
            return None
        relative = _safe_projection_path(projection)
        if OBJECT_DIR in relative.parts:
            raise StorageControlError("invalid_projection_path", "projection_path cannot target internal object storage")
        policy = self.config.policy_for(store)
        self._validate_extension(relative.name or logical_name, policy.values)
        root = location_for_store(self.config, store).root.resolve()
        destination = (root / relative).resolve()
        if not destination.is_relative_to(root):
            raise StorageControlError("projection_path_escape", "projection_path escaped store root", status_code=403)
        if destination.exists():
            if not destination.is_file():
                raise StorageControlError("projection_path_conflict", "projection_path already exists and is not a file", status_code=409)
            if hash_file(destination) == hash_file(source_path):
                return destination
        destination.parent.mkdir(parents=True, exist_ok=True)
        temp = destination.with_name(f".{destination.name}.tmp-{uuid4().hex}")
        shutil.copyfile(source_path, temp)
        self._fsync_if_strong(temp)
        os.replace(temp, destination)
        self._fsync_if_strong(destination)
        return destination

    def _ensure_replayed_projection(
        self,
        response: dict[str, Any],
        *,
        store: StoreConfig,
        logical_name: str,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        projection = str(metadata.get("projection_path") or "").strip()
        object_id = str(response.get("object_id") or "").strip()
        if not projection or not object_id:
            return response

        object_record = self.index.get_storage_object(_safe_object_id(object_id))
        if object_record is None:
            return response
        source_path = Path(str(object_record["current_path"])).resolve()
        self._ensure_internal_object_path(source_path, store)
        projection_path = self._write_projection_if_requested(store, source_path, logical_name, metadata)
        if projection_path is None:
            return response

        merged_metadata = dict(object_record.get("metadata") or {})
        merged_metadata.update(metadata)
        merged_metadata["projection_materialized_path"] = str(projection_path)
        object_record["metadata"] = merged_metadata
        object_record["updated_at"] = time.time()
        self.index.upsert_storage_object(object_record)
        self.index.insert_control_event(
            event_type="projection_reconciled",
            agent=str(object_record.get("created_by") or ""),
            action="projection_reconcile",
            allowed=True,
            reason="allowed",
            object_id=object_id,
            metadata={"projection_path": projection, "projection_materialized_path": str(projection_path)},
        )
        self.index.commit()
        updated_response = dict(response)
        updated_response["metadata"] = merged_metadata
        return updated_response

    def _ensure_internal_object_path(self, path: Path, store: StoreConfig) -> None:
        candidates = [
            store.path / OBJECT_DIR,
            location_for_store(self.config, store).root / OBJECT_DIR,
        ]
        candidates.extend(root / OBJECT_DIR for root in pending_store_root_candidates(self.config, store))
        if not any(_is_relative_to(path.resolve(), root.resolve()) for root in candidates):
            raise StorageControlError("path_escape", "internal object path escaped managed root", status_code=500)

    def _materialize_destination(self, value: str) -> Path:
        errors: list[str] = []
        for root in self._materialize_allowed_roots():
            try:
                return safe_path_under_root(root, value, field_name="destination_path")
            except UnsafePathError as exc:
                errors.append(str(exc))
        configured = ", ".join(str(root) for root in self._materialize_allowed_roots())
        detail = errors[0] if errors else "destination_path is outside allowed roots"
        raise StorageControlError(
            "destination_not_allowed",
            f"{detail}; allowed roots: {configured}",
            status_code=403,
        )

    def _materialize_allowed_roots(self) -> tuple[Path, ...]:
        raw_roots = list(self._control_config().get("materialize_allowed_roots") or [])
        env_roots = os.environ.get("STORAGE_GUARDIAN_MATERIALIZE_ROOTS", "")
        raw_roots.extend(item for item in env_roots.split(":") if item.strip())
        roots = [self.config.project_root, *(Path(str(item)) for item in raw_roots)]
        seen: set[str] = set()
        unique: list[Path] = []
        for root in roots:
            resolved = Path(root).expanduser().resolve()
            text = str(resolved)
            if text not in seen:
                unique.append(resolved)
                seen.add(text)
        return tuple(unique)

    def _write_bytes_atomic(self, final_path: Path, content: bytes) -> None:
        store = self._store_for_internal_path(final_path)
        if store is not None:
            self._ensure_internal_object_path(final_path, store)
        final_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = final_path.with_name(f".{final_path.name}.tmp-{time.time_ns()}")
        with temp_path.open("wb") as handle:
            handle.write(content)
            if self._durability_mode() == "strong":
                handle.flush()
                os.fsync(handle.fileno())
        if temp_path.stat().st_dev != final_path.parent.stat().st_dev:
            raise StorageControlError("cross_device_write", "temporary and final object paths are on different filesystems", status_code=500)
        os.replace(temp_path, final_path)
        self._fsync_dir_if_strong(final_path.parent)

    def _store_for_internal_path(self, path: Path) -> StoreConfig | None:
        for store in self.config.stores:
            roots = [
                store.path / OBJECT_DIR,
                location_for_store(self.config, store).root / OBJECT_DIR,
            ]
            roots.extend(root / OBJECT_DIR for root in pending_store_root_candidates(self.config, store))
            if any(_is_relative_to(path, root) for root in roots):
                return store
        return None

    def _fsync_if_strong(self, path: Path) -> None:
        if self._durability_mode() != "strong":
            return
        with path.open("rb") as handle:
            os.fsync(handle.fileno())
        self._fsync_dir_if_strong(path.parent)

    def _fsync_dir_if_strong(self, path: Path) -> None:
        if self._durability_mode() != "strong":
            return
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _durability_mode(self) -> str:
        mode = str(self._control_config().get("durability_mode", "fast"))
        return "strong" if mode == "strong" else "fast"

    def _max_inline_bytes(self, store: StoreConfig, owner_cfg: dict[str, Any]) -> int:
        raw = owner_cfg.get("max_inline_bytes") or self._control_config().get("max_inline_bytes") or DEFAULT_MAX_INLINE_BYTES
        policy_raw = self.config.policy_for(store).values.get("max_inline_bytes")
        if policy_raw is not None:
            raw = min(int(raw), int(policy_raw))
        return int(raw)

    def _max_upload_ttl_seconds(self) -> int:
        return int(self._control_config().get("max_upload_ttl_seconds", 86_400))

    def _idempotency_ttl_seconds(self) -> int:
        return int(self._control_config().get("idempotency_ttl_seconds", DEFAULT_IDEMPOTENCY_TTL_SECONDS))

    def _idempotency_replay(self, scope: str, idempotency_key: str, payload_hash: str) -> dict[str, Any] | None:
        existing = self.index.get_idempotency_key(scope, idempotency_key)
        if existing is None:
            return None
        if str(existing.get("payload_hash")) != payload_hash:
            raise StorageControlError("idempotency_conflict", "Idempotency-Key was reused with a different payload", status_code=409)
        response = existing.get("response")
        if isinstance(response, dict) and response:
            self.index.insert_control_event(
                event_type="idempotency_replayed",
                agent=scope.split(":", 1)[0],
                action="idempotency_replay",
                allowed=True,
                reason="allowed",
                object_id=response.get("object_id"),
                metadata={"scope": scope, "idempotency_key": idempotency_key},
            )
            self.index.commit()
            return response
        return None

    def _store_capabilities(self, store: StoreConfig) -> dict[str, Any]:
        policy = self.config.policy_for(store).values
        return {
            "name": store.name,
            "service": store.service,
            "owner": store.owner,
            "mode": store.mode,
            "policy": store.policy,
            "placement": store.placement,
            "zones": list(self._zones()),
            "max_inline_bytes": int(policy.get("max_inline_bytes") or self._control_config().get("max_inline_bytes") or DEFAULT_MAX_INLINE_BYTES),
            "max_upload_bytes": policy.get("max_upload_bytes"),
            "allowed_extensions": list(policy.get("extensions") or []),
            "supports_versioning": True,
            "supports_soft_delete": True,
            "requires_sha256": True,
            "backend_capabilities": {
                "kind": "local_fs",
                "supports_atomic_rename": True,
                "supports_fsync": self._durability_mode() == "strong",
                "supports_versioning": True,
                "supports_conditional_put": False,
                "supports_content_addressed_blobs": True,
            },
        }


def _public_upload(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "upload_id": record["upload_id"],
        "store": record["store_id"],
        "zone": record["zone"],
        "status": record["status"],
        "logical_name": record["logical_name"],
        "expected_size": int(record["expected_size"]),
        "received_size": int(record.get("received_size") or 0),
        "sha256": record["expected_hash"],
        "content_type": record["content_type"],
        "expires_at": float(record["expires_at"]),
        "created_by": record["created_by"],
        "object_id": record.get("object_id"),
        "version_id": record.get("version_id"),
        "metadata": record.get("metadata") or {},
    }


def _public_object(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "object_id": record["object_id"],
        "latest_version_id": record.get("latest_version_id"),
        "storage_ref": record.get("storage_ref"),
        "store": record.get("store") or record.get("store_id"),
        "owner_service": record.get("owner_service"),
        "zone": record.get("zone"),
        "status": record.get("status"),
        "logical_name": record.get("logical_name") or (record.get("metadata") or {}).get("logical_name"),
        "content_type": record.get("content_type") or (record.get("metadata") or {}).get("content_type"),
        "object_kind": record.get("object_kind"),
        "semantic_role": record.get("semantic_role"),
        "lineage_root": record.get("lineage_root"),
        "materialized_ref": record.get("materialized_ref"),
        "retention_policy": record.get("retention_policy"),
        "size_bytes": int(record.get("size_bytes") or 0),
        "sha256": record.get("sha256") or record.get("content_hash"),
        "created_by": record.get("created_by"),
        "created_at": float(record.get("created_at") or 0),
        "updated_at": float(record.get("updated_at") or 0),
        "parent_object_id": record.get("parent_object_id"),
        "metadata": record.get("metadata") or {},
    }


def _public_directory(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "directory_id": record["directory_id"],
        "service": record.get("service"),
        "store": record.get("store_id"),
        "relative_path": record.get("relative_path"),
        "parent_directory_id": record.get("parent_directory_id"),
        "owner": record.get("owner"),
        "zone": record.get("zone"),
        "mode": record.get("mode"),
        "policy": record.get("policy"),
        "expected_by_schema": bool(record.get("expected_by_schema")),
        "status": record.get("status"),
        "created_by": record.get("created_by"),
        "protected": bool(record.get("protected")),
        "writable_by_storage_guardian": bool(record.get("writable_by_storage_guardian", True)),
        "readable_by_callers": bool(record.get("readable_by_callers", True)),
        "metadata": record.get("metadata") or {},
    }


def _validate_logical_name(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value).strip()
    if not normalized or len(normalized) > _MAX_LOGICAL_NAME_LENGTH:
        raise StorageControlError("invalid_logical_name", "logical_name is empty or too long")
    if "\x00" in normalized or "/" in normalized or "\\" in normalized:
        raise StorageControlError("invalid_logical_name", "logical_name must be a file name, not a path")
    if normalized in {".", ".."}:
        raise StorageControlError("invalid_logical_name", "logical_name is not allowed")
    return normalized


def _safe_projection_path(value: str) -> Path:
    normalized = unicodedata.normalize("NFC", value).strip()
    if not normalized or len(normalized) > 1024 or "\x00" in normalized:
        raise StorageControlError("invalid_projection_path", "projection_path is empty or too long")
    relative = PurePosixPath(normalized)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise StorageControlError("invalid_projection_path", "projection_path must be a safe relative path")
    return Path(*relative.parts)


def _safe_segment(value: str, fallback: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in "._-" else "_" for char in str(value).strip()).strip("._-")
    return cleaned or fallback


def _safe_object_id(value: str) -> str:
    cleaned = _safe_segment(value, "object")
    if len(cleaned) > 160:
        raise StorageControlError("invalid_object_id", "object id is too long")
    return cleaned


def _inspect_archive_for_extraction(
    content: bytes,
    target_dir: Path,
    *,
    max_members: int,
    max_uncompressed_bytes: int,
) -> ArchiveExtractionPlan:
    target_root = target_dir.resolve()
    files_count = 0
    total_bytes = 0
    top_levels: set[str] = set()
    try:
        if zipfile.is_zipfile(io.BytesIO(content)):
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                bad_member = archive.testzip()
                if bad_member:
                    raise StorageControlError("archive_corrupt", f"zip CRC failed for member: {bad_member}", status_code=409)
                for info in archive.infolist():
                    if info.is_dir():
                        _safe_archive_destination(info.filename, target_root)
                        top_levels.update(_top_level(info.filename))
                        continue
                    _reject_zip_symlink(info)
                    destination = _safe_archive_destination(info.filename, target_root)
                    total_bytes += int(info.file_size)
                    files_count += 1
                    top_levels.update(_top_level(info.filename))
                    _enforce_archive_limits(files_count, total_bytes, max_members, max_uncompressed_bytes)
                    if destination.exists() and destination.is_dir():
                        raise StorageControlError("extract_destination_conflict", f"archive member targets a directory: {destination}", status_code=409)
                return ArchiveExtractionPlan(files_count=files_count, total_bytes=total_bytes, top_level_paths=sorted(top_levels))
        with tarfile.open(fileobj=io.BytesIO(content), mode="r:*") as archive:
            for member in archive.getmembers():
                _reject_unsafe_tar_member(member)
                if member.isdir():
                    _safe_archive_destination(member.name, target_root)
                    top_levels.update(_top_level(member.name))
                    continue
                if not member.isfile():
                    continue
                destination = _safe_archive_destination(member.name, target_root)
                total_bytes += int(member.size)
                files_count += 1
                top_levels.update(_top_level(member.name))
                _enforce_archive_limits(files_count, total_bytes, max_members, max_uncompressed_bytes)
                if destination.exists() and destination.is_dir():
                    raise StorageControlError("extract_destination_conflict", f"archive member targets a directory: {destination}", status_code=409)
            return ArchiveExtractionPlan(files_count=files_count, total_bytes=total_bytes, top_level_paths=sorted(top_levels))
    except StorageControlError:
        raise
    except (tarfile.TarError, zipfile.BadZipFile, EOFError, OSError, RuntimeError) as exc:
        raise StorageControlError("archive_unsupported_or_corrupt", str(exc)[:300], status_code=409) from exc


def _extract_archive_content(
    content: bytes,
    target_dir: Path,
    *,
    replace_top_level_paths: Iterable[str] = (),
) -> None:
    target_root = target_dir.resolve()
    target_root.mkdir(parents=True, exist_ok=True)
    _replace_archive_top_level_paths(target_root, replace_top_level_paths)
    if zipfile.is_zipfile(io.BytesIO(content)):
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            for info in archive.infolist():
                destination = _safe_archive_destination(info.filename, target_root)
                if info.is_dir():
                    destination.mkdir(parents=True, exist_ok=True)
                    continue
                _reject_zip_symlink(info)
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, destination.open("wb") as output:
                    shutil.copyfileobj(source, output)
        return
    with tarfile.open(fileobj=io.BytesIO(content), mode="r:*") as archive:
        for member in archive.getmembers():
            _reject_unsafe_tar_member(member)
            destination = _safe_archive_destination(member.name, target_root)
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                continue
            source = archive.extractfile(member)
            if source is None:
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            with source, destination.open("wb") as output:
                shutil.copyfileobj(source, output)


def _replace_archive_top_level_paths(target_root: Path, top_level_paths: Iterable[str]) -> None:
    for top_level in top_level_paths:
        normalized = str(top_level or "").replace("\\", "/")
        pure = PurePosixPath(normalized)
        if not normalized or pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
            raise StorageControlError("unsafe_archive_member", f"archive top-level path is unsafe: {top_level!r}", status_code=409)
        candidate = target_root.joinpath(*pure.parts)
        if not _is_relative_to(candidate.parent.resolve(), target_root):
            raise StorageControlError("unsafe_archive_member", f"archive top-level path escapes extraction root: {top_level!r}", status_code=409)
        if not candidate.exists() and not candidate.is_symlink():
            continue
        if candidate.is_symlink() or candidate.is_file():
            candidate.unlink()
            continue
        if candidate.is_dir():
            shutil.rmtree(candidate)


def _safe_archive_destination(member_name: str, target_root: Path) -> Path:
    normalized = str(member_name or "").replace("\\", "/")
    pure = PurePosixPath(normalized)
    if not normalized or pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise StorageControlError("unsafe_archive_member", f"archive member path is unsafe: {member_name!r}", status_code=409)
    destination = (target_root / Path(*pure.parts)).resolve()
    if not _is_relative_to(destination, target_root):
        raise StorageControlError("unsafe_archive_member", f"archive member escapes extraction root: {member_name!r}", status_code=409)
    return destination


def _reject_unsafe_tar_member(member: tarfile.TarInfo) -> None:
    _safe_archive_destination(member.name, Path("/tmp/archive-safety-root").resolve())
    if member.issym() or member.islnk():
        raise StorageControlError("unsafe_archive_member", f"archive links are not materialized: {member.name!r}", status_code=409)
    if member.ischr() or member.isblk() or member.isfifo() or member.isdev():
        raise StorageControlError("unsafe_archive_member", f"archive device entries are not materialized: {member.name!r}", status_code=409)


def _reject_zip_symlink(info: zipfile.ZipInfo) -> None:
    mode = (info.external_attr >> 16) & 0o170000
    if stat.S_ISLNK(mode):
        raise StorageControlError("unsafe_archive_member", f"archive symlinks are not materialized: {info.filename!r}", status_code=409)


def _enforce_archive_limits(files_count: int, total_bytes: int, max_members: int, max_uncompressed_bytes: int) -> None:
    if files_count > max_members:
        raise StorageControlError("archive_member_limit_exceeded", "archive member count exceeds materialization policy", status_code=413)
    if total_bytes > max_uncompressed_bytes:
        raise StorageControlError("archive_size_limit_exceeded", "archive uncompressed size exceeds materialization policy", status_code=413)


def _top_level(member_name: str) -> set[str]:
    parts = PurePosixPath(str(member_name or "").replace("\\", "/")).parts
    return {parts[0]} if parts else set()


def _normalize_sha256(value: str) -> str:
    digest = value.split(":", 1)[-1].lower()
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise StorageControlError("invalid_sha256", "sha256 must be a 64 character hex digest")
    return f"sha256:{digest}"


def _payload_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _new_object_id() -> str:
    return "obj_" + uuid4().hex


def _new_version_id() -> str:
    return "ver_" + uuid4().hex


def _new_upload_id() -> str:
    return "upl_" + uuid4().hex


def _new_operation_id() -> str:
    return "op_" + uuid4().hex


def _new_custody_event_id() -> str:
    return "custody_" + uuid4().hex


def _custody_event_type(operation_type: str) -> str:
    return {
        "create_directory": "directory_created",
        "create_object": "object_created",
        "commit_upload": "upload_committed",
        "copy_object": "object_copied",
        "move_object": "object_moved",
        "rename_object": "object_renamed",
        "soft_delete": "object_soft_deleted",
        "hard_purge": "object_hard_purged",
        "materialize": "artifact_materialized",
        "promote": "object_promoted",
        "reconcile_structure": "schema_reconciled",
    }.get(operation_type, operation_type)


def _object_kind(logical_name: str, content_type: str) -> str:
    media_type = content_type.split(";", 1)[0].strip().lower()
    suffix = Path(logical_name).suffix.lower()
    if media_type.startswith("audio/") or suffix in {".wav", ".mp3", ".m4a", ".flac", ".opus", ".aiff"}:
        return "audio"
    if media_type in {"application/pdf"} or suffix in {".pdf", ".docx", ".pptx", ".xlsx", ".odt", ".ods", ".odp"}:
        return "document"
    if media_type.startswith("text/") or suffix in {".txt", ".md", ".json", ".jsonl", ".csv", ".log", ".yaml", ".yml", ".toml"}:
        return "text"
    if suffix in {".zip", ".tar", ".gz", ".tgz", ".7z", ".zst"}:
        return "archive"
    return "binary"


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False
