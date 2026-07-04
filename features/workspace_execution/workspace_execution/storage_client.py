"""HTTP client for publishing workspace_execution artifacts via storage_guardian."""

from __future__ import annotations

import base64
import hashlib
import mimetypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx

from workspace_execution.config import StorageGuardianSettings
from workspace_execution.types import ArtifactDescriptor


class StorageGuardianPublishError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}


@dataclass(frozen=True)
class StoragePublishResult:
    storage_object_ref: str
    chain_of_custody_ref: str
    materialized_path: str | None = None
    materialized_sha256: str | None = None
    extracted_path: str | None = None
    extracted_files_count: int | None = None
    extracted_top_level_paths: list[str] = field(default_factory=list)
    response: dict[str, Any] = field(default_factory=dict)


class ArtifactPublisher(Protocol):
    def publish_artifact(
        self,
        path: Path,
        artifact: ArtifactDescriptor,
        *,
        target: dict[str, Any],
        metadata: dict[str, Any],
        idempotency_key: str,
    ) -> StoragePublishResult:
        ...


@dataclass(frozen=True)
class StorageGuardianClient:
    settings: StorageGuardianSettings

    def publish_artifact(
        self,
        path: Path,
        artifact: ArtifactDescriptor,
        *,
        target: dict[str, Any],
        metadata: dict[str, Any],
        idempotency_key: str,
    ) -> StoragePublishResult:
        if not path.is_file():
            raise StorageGuardianPublishError(
                "artifact_not_found",
                "artifact file is not available for publication",
                details={"path": str(path), "artifact_id": artifact.artifact_id},
            )
        if not self.settings.url:
            raise StorageGuardianPublishError(
                "storage_guardian_url_required",
                "Storage Guardian URL is required for artifact publication",
            )

        content = path.read_bytes()
        digest = "sha256:" + hashlib.sha256(content).hexdigest()
        if digest.removeprefix("sha256:") != artifact.sha256:
            raise StorageGuardianPublishError(
                "artifact_checksum_mismatch",
                "artifact checksum changed before publication",
                details={"artifact_id": artifact.artifact_id},
            )

        payload_metadata = {
            "producer_service": "workspace_execution",
            "artifact_id": artifact.artifact_id,
            "artifact_path": artifact.path,
            "artifact_origin": artifact.origin,
            **metadata,
        }
        logical_name = _logical_name(str(target.get("logical_name") or artifact.path), path)
        store = str(target.get("store") or target.get("storage_class") or self.settings.store)
        zone = str(target.get("zone") or self.settings.zone)
        agent = str(target.get("agent") or self.settings.agent)
        content_type = str(target.get("content_type") or artifact.media_type or _content_type(path))
        headers = _auth_headers(self.settings.token)
        timeout = httpx.Timeout(self.settings.timeout_seconds)

        with httpx.Client(verify=self.settings.verify_tls, timeout=timeout) as client:
            if len(content) <= self.settings.max_inline_bytes:
                response = client.post(
                    f"{self.settings.url}/internal/storage/objects",
                    headers={**headers, "Idempotency-Key": idempotency_key},
                    json={
                        "agent": agent,
                        "store": store,
                        "zone": zone,
                        "logical_name": logical_name,
                        "content_type": content_type,
                        "content_base64": base64.b64encode(content).decode("ascii"),
                        "sha256": digest,
                        "metadata": payload_metadata,
                    },
                )
                result = _publish_result(response)
                return _with_materialization(
                    result,
                    client=client,
                    settings=self.settings,
                    target=target,
                    metadata=payload_metadata,
                    idempotency_key=idempotency_key,
                    content=content,
                    digest=digest,
                    logical_name=logical_name,
                    content_type=content_type,
                    agent=agent,
                    store=store,
                    zone=zone,
                    headers=headers,
                )

            upload = client.post(
                f"{self.settings.url}/internal/storage/uploads",
                headers=headers,
                json={
                    "agent": agent,
                    "store": store,
                    "zone": zone,
                    "logical_name": logical_name,
                    "content_type": content_type,
                    "expected_size": len(content),
                    "sha256": digest,
                    "metadata": payload_metadata,
                },
            )
            _raise_for_storage_error(upload)
            upload_id = str(upload.json()["upload_id"])
            append = client.put(
                f"{self.settings.url}/internal/storage/uploads/{upload_id}",
                headers=headers,
                content=content,
            )
            _raise_for_storage_error(append)
            committed = client.post(
                f"{self.settings.url}/internal/storage/uploads/{upload_id}/commit",
                headers={**headers, "Idempotency-Key": f"{idempotency_key}:upload:{upload_id}:commit"},
                json={"sha256": digest, "metadata": payload_metadata},
            )
            result = _publish_result(committed)
            return _with_materialization(
                result,
                client=client,
                settings=self.settings,
                target=target,
                metadata=payload_metadata,
                idempotency_key=idempotency_key,
                content=content,
                digest=digest,
                logical_name=logical_name,
                content_type=content_type,
                agent=agent,
                store=store,
                zone=zone,
                headers=headers,
            )


def _publish_result(response: httpx.Response) -> StoragePublishResult:
    _raise_for_storage_error(response)
    payload = dict(response.json())
    store = str(payload.get("store") or "")
    object_id = str(payload.get("object_id") or "")
    version_id = str(payload.get("latest_version_id") or "latest")
    if not store or not object_id:
        raise StorageGuardianPublishError(
            "storage_guardian_response_invalid",
            "Storage Guardian publish response did not include object identity",
            details={"response": payload},
        )
    return StoragePublishResult(
        storage_object_ref=f"storage_guardian://{store}/{object_id}",
        chain_of_custody_ref=f"storage_control://{object_id}/{version_id}",
        materialized_path=None,
        materialized_sha256=None,
        extracted_path=None,
        extracted_files_count=None,
        extracted_top_level_paths=[],
        response=payload,
    )


def _with_materialization(
    result: StoragePublishResult,
    *,
    client: httpx.Client,
    settings: StorageGuardianSettings,
    target: dict[str, Any],
    metadata: dict[str, Any],
    idempotency_key: str,
    content: bytes,
    digest: str,
    logical_name: str,
    content_type: str,
    agent: str,
    store: str,
    zone: str,
    headers: dict[str, str],
) -> StoragePublishResult:
    destination = str(
        target.get("materialize_destination_path")
        or target.get("destination_path")
        or ""
    ).strip()
    if not destination:
        return result
    response = client.post(
        f"{settings.url}/internal/storage/materialize",
        headers={**headers, "Idempotency-Key": f"{idempotency_key}:materialize"},
        json={
            "agent": agent,
            "store": store,
            "zone": zone,
            "logical_name": logical_name,
            "content_type": content_type,
            "destination_path": destination,
            "content_base64": base64.b64encode(content).decode("ascii"),
            "sha256": digest,
            "overwrite": bool(target.get("overwrite_materialized")),
            "extract_archive": bool(target.get("extract_archive")),
            "extract_destination_path": target.get("extract_destination_path"),
            "max_archive_members": target.get("max_archive_members") or 5000,
            "max_archive_uncompressed_bytes": target.get("max_archive_uncompressed_bytes") or 512 * 1024 * 1024,
            "metadata": {
                **metadata,
                "storage_object_ref": result.storage_object_ref,
                "chain_of_custody_ref": result.chain_of_custody_ref,
                "materialization_owner": "storage_guardian",
            },
        },
    )
    _raise_for_storage_error(response)
    payload = dict(response.json())
    extracted_top_level_paths = payload.get("extracted_top_level_paths")
    return StoragePublishResult(
        storage_object_ref=result.storage_object_ref,
        chain_of_custody_ref=result.chain_of_custody_ref,
        materialized_path=str(payload.get("destination_path") or destination),
        materialized_sha256=str(payload.get("sha256") or digest),
        extracted_path=str(payload.get("extracted_path") or "") or None,
        extracted_files_count=(
            int(payload["extracted_files_count"])
            if isinstance(payload.get("extracted_files_count"), int)
            else None
        ),
        extracted_top_level_paths=[
            str(item)
            for item in (extracted_top_level_paths if isinstance(extracted_top_level_paths, list) else [])
        ],
        response={
            **result.response,
            "materialized": payload,
        },
    )


def _raise_for_storage_error(response: httpx.Response) -> None:
    if response.status_code < 400:
        return
    try:
        payload = response.json()
    except ValueError:
        payload = {"detail": response.text}
    detail = payload.get("detail") if isinstance(payload, dict) else None
    if not isinstance(detail, str):
        detail = str(payload)
    code = payload.get("reason") if isinstance(payload, dict) else None
    if not isinstance(code, str):
        code = "storage_guardian_publish_failed"
    raise StorageGuardianPublishError(
        code,
        detail,
        details={"status_code": response.status_code, "response": payload},
    )


def _auth_headers(token: str) -> dict[str, str]:
    return {"X-Internal-Token": token} if token else {}


def _logical_name(value: str, path: Path) -> str:
    name = "".join(char if char.isalnum() or char in "._-" else "_" for char in value).strip("._-")
    if not Path(name).suffix and path.suffix:
        name = f"{name}{path.suffix}"
    return name[:240] or path.name


def _content_type(path: Path) -> str:
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"
