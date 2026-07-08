"""Client for publishing scratch artifacts through the storage_guardian API."""

from __future__ import annotations

import base64
import hashlib
import mimetypes
import os
from pathlib import Path
from typing import Any

import httpx

from storage_guardian.scratch import ScratchPathError, assert_scratch_path

DEFAULT_MAX_INLINE_BYTES = 262_144


class StorageGuardianPublishError(RuntimeError):
    """Raised when required storage_guardian publication fails."""


class StorageGuardianRestoreTestError(RuntimeError):
    """Raised when storage_guardian rejects or cannot run a restore test."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def publish_file(
    path: Path,
    *,
    agent: str,
    store: str,
    logical_name: str,
    metadata: dict[str, Any] | None = None,
    zone: str = "ingest",
) -> str:
    """Publish a local scratch artifact and return its storage_guardian URI."""

    if not path.is_file():
        raise StorageGuardianPublishError(f"artifact does not exist: {path}")

    try:
        assert_scratch_path(path, label="persistent artifact")
    except ScratchPathError as exc:
        raise StorageGuardianPublishError(str(exc)) from exc
    cfg = _client_config()
    if not cfg["url"]:
        raise StorageGuardianPublishError("STORAGE_GUARDIAN_URL is required for persistent outputs")

    content = path.read_bytes()
    digest = "sha256:" + hashlib.sha256(content).hexdigest()
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    clean_name = _logical_name(logical_name, path)
    payload_metadata = {
        "service": agent,
        "local_scratch_path": str(path),
        **(metadata or {}),
    }

    headers = _auth_headers(cfg["token"])
    timeout = httpx.Timeout(float(os.environ.get("STORAGE_GUARDIAN_TIMEOUT_SECONDS", "60")))
    with httpx.Client(verify=cfg["verify_tls"], timeout=timeout) as client:
        if len(content) <= cfg["max_inline_bytes"]:
            response = client.post(
                f"{cfg['url']}/internal/storage/objects",
                headers={
                    **headers,
                    "Idempotency-Key": _idempotency_key(agent, store, clean_name, digest),
                },
                json={
                    "agent": agent,
                    "store": store,
                    "zone": zone,
                    "logical_name": clean_name,
                    "content_type": content_type,
                    "content_base64": base64.b64encode(content).decode("ascii"),
                    "sha256": digest,
                    "metadata": payload_metadata,
                },
            )
            response.raise_for_status()
            return _object_uri(response.json())

        upload = client.post(
            f"{cfg['url']}/internal/storage/uploads",
            headers=headers,
            json={
                "agent": agent,
                "store": store,
                "zone": zone,
                "logical_name": clean_name,
                "content_type": content_type,
                "expected_size": len(content),
                "sha256": digest,
                "metadata": payload_metadata,
            },
        )
        upload.raise_for_status()
        upload_id = str(upload.json()["upload_id"])
        append = client.put(
            f"{cfg['url']}/internal/storage/uploads/{upload_id}",
            headers=headers,
            content=content,
        )
        append.raise_for_status()
        committed = client.post(
            f"{cfg['url']}/internal/storage/uploads/{upload_id}/commit",
            headers={
                **headers,
                "Idempotency-Key": _idempotency_key(agent, store, f"{clean_name}:{upload_id}:commit", digest),
            },
            json={"sha256": digest, "metadata": payload_metadata},
        )
        committed.raise_for_status()
        return _object_uri(committed.json())


def request_restore_test(
    volume: str,
    *,
    requested_by: str = "storage_guardian.client",
) -> dict[str, Any]:
    cfg = _client_config()
    if not cfg["url"]:
        raise StorageGuardianRestoreTestError("STORAGE_GUARDIAN_URL is required for restore tests")

    timeout = httpx.Timeout(float(os.environ.get("STORAGE_GUARDIAN_TIMEOUT_SECONDS", "60")))
    response = httpx.post(
        f"{cfg['url']}/internal/storage/restore-tests",
        headers=_auth_headers(cfg["token"]),
        json={"volume": volume, "requested_by": requested_by},
        timeout=timeout,
        verify=cfg["verify_tls"],
    )
    if response.status_code >= 400:
        raise StorageGuardianRestoreTestError(
            _error_detail(response),
            status_code=response.status_code,
        )
    return dict(response.json())


def _client_config() -> dict[str, Any]:
    url = (
        os.environ.get("STORAGE_GUARDIAN_URL")
        or os.environ.get("ORC_SERVICES_STORAGE_GUARDIAN_URL")
        or ""
    ).rstrip("/")
    token = os.environ.get("STORAGE_GUARDIAN_INTERNAL_TOKEN", "").strip()
    token_file = (
        os.environ.get("STORAGE_GUARDIAN_INTERNAL_TOKEN_FILE")
        or os.environ.get("INTERNAL_API_KEY_FILE")
        or os.environ.get("AI_RESOURCE_GOVERNOR_TOKEN_FILE")
        or ""
    )
    if not token and token_file:
        try:
            token = Path(token_file).read_text(encoding="utf-8").strip()
        except OSError:
            token = ""
    return {
        "url": url,
        "token": token,
        "verify_tls": os.environ.get("STORAGE_GUARDIAN_VERIFY_TLS", "true").lower()
        not in {"0", "false", "no", "off"},
        "max_inline_bytes": int(os.environ.get("STORAGE_GUARDIAN_MAX_INLINE_BYTES", DEFAULT_MAX_INLINE_BYTES)),
    }


def _auth_headers(token: str) -> dict[str, str]:
    return {"X-Internal-Token": token} if token else {}


def _idempotency_key(agent: str, store: str, logical_name: str, digest: str) -> str:
    return f"{agent}:{store}:{hashlib.sha256(f'{logical_name}:{digest}'.encode('utf-8')).hexdigest()}"


def _logical_name(logical_name: str, path: Path) -> str:
    name = "".join(char if char.isalnum() or char in "._-" else "_" for char in logical_name).strip("._-")
    if not Path(name).suffix:
        name = f"{name}{path.suffix}"
    return name[:240] or path.name


def _object_uri(payload: dict[str, Any]) -> str:
    return f"storage_guardian://{payload.get('store')}/{payload.get('object_id')}"


def _error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text
    detail = payload.get("detail") if isinstance(payload, dict) else None
    if isinstance(detail, str):
        return detail
    return str(payload)
