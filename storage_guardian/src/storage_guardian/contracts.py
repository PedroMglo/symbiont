"""Pydantic contracts for the storage_guardian object gateway."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

STORAGE_CONTRACT_VERSION = "storage-control.v2"
DEFAULT_MAX_INLINE_BYTES = 256 * 1024
DEFAULT_UPLOAD_TTL_SECONDS = 15 * 60


class _StorageModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StorageObjectCreate(_StorageModel):
    agent: str
    logical_name: str
    content_base64: str
    sha256: str
    store: str | None = None
    zone: str | None = None
    content_type: str = "application/octet-stream"
    metadata: dict[str, Any] = Field(default_factory=dict)
    parent_object_id: str | None = None
    authority: dict[str, Any] | None = None


class StorageUploadSessionCreate(_StorageModel):
    agent: str
    logical_name: str
    expected_size: int
    sha256: str
    store: str | None = None
    zone: str | None = None
    content_type: str = "application/octet-stream"
    metadata: dict[str, Any] = Field(default_factory=dict)
    ttl_seconds: int | None = None
    parent_object_id: str | None = None
    authority: dict[str, Any] | None = None


class StorageUploadCommit(_StorageModel):
    sha256: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class StorageMaterializeRequest(_StorageModel):
    agent: str
    destination_path: str
    content_base64: str
    sha256: str
    logical_name: str | None = None
    store: str | None = None
    zone: str | None = None
    content_type: str = "application/octet-stream"
    metadata: dict[str, Any] = Field(default_factory=dict)
    overwrite: bool = False
    extract_archive: bool = False
    extract_destination_path: str | None = None
    max_archive_members: int = Field(default=5000, ge=1, le=50000)
    max_archive_uncompressed_bytes: int = Field(default=512 * 1024 * 1024, ge=1, le=10 * 1024 * 1024 * 1024)
    authority: dict[str, Any] | None = None


class StorageDirectoryCreate(_StorageModel):
    agent: str
    relative_path: str
    store: str | None = None
    zone: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    authority: dict[str, Any] | None = None


class StorageObjectCopyMoveRequest(_StorageModel):
    agent: str
    target_store: str | None = None
    target_zone: str | None = None
    target_logical_name: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    authority: dict[str, Any] | None = None


class StorageObjectRenameRequest(_StorageModel):
    agent: str
    logical_name: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    authority: dict[str, Any] | None = None


class StorageObjectHardPurgeRequest(_StorageModel):
    agent: str
    reason: str | None = None
    confirm: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)
    authority: dict[str, Any] | None = None


class StorageObjectTextRead(_StorageModel):
    agent: str
    max_bytes: int = Field(default=12_000, ge=1, le=1_048_576)


class StorageQueryRequest(BaseModel):
    query: str = ""
    budget_tokens: int = 2000
    timeout_seconds: float | None = None
    workspace_path: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class StorageQueryResponse(BaseModel):
    content: str = ""
    source: str = "storage"
    token_estimate: int = 0
    success: bool = True
    latency_ms: float = 0.0
    metadata: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class RestoreDryRunRequest(_StorageModel):
    manifest_ref: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class RestoreDryRunResponse(_StorageModel):
    restore_plan: dict[str, Any] = Field(default_factory=dict)
    chain_of_custody: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


def storage_contract_schema_hash() -> str:
    payload = {
        "version": STORAGE_CONTRACT_VERSION,
        "object": StorageObjectCreate.model_json_schema(),
        "upload": StorageUploadSessionCreate.model_json_schema(),
        "commit": StorageUploadCommit.model_json_schema(),
        "materialize": StorageMaterializeRequest.model_json_schema(),
        "create_directory": StorageDirectoryCreate.model_json_schema(),
        "copy_move": StorageObjectCopyMoveRequest.model_json_schema(),
        "rename": StorageObjectRenameRequest.model_json_schema(),
        "hard_purge": StorageObjectHardPurgeRequest.model_json_schema(),
        "read_text": StorageObjectTextRead.model_json_schema(),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()
