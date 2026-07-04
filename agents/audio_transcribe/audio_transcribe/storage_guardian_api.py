"""HTTP boundary client for publishing audio_transcribe artifacts to storage_guardian."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import mimetypes
import os
import time
from pathlib import Path
from typing import Any

import httpx

from audio_transcribe.scratch import ScratchPathError, assert_scratch_path

DEFAULT_MAX_INLINE_BYTES = 262_144
DEFAULT_PUBLISH_RETRY_ATTEMPTS = 4
DEFAULT_PUBLISH_RETRY_DELAY_SECONDS = 1.0
REUSE_CONTRACT_VERSION = "audio_transcription_reuse.v1"
PROJECTION_CONTRACT_VERSION = "audio_transcription_projection.v2"
logger = logging.getLogger(__name__)


class StorageGuardianPublishError(RuntimeError):
    """Raised when required storage_guardian publication fails."""


def publish_file(
    path: Path,
    *,
    agent: str,
    store: str,
    logical_name: str,
    metadata: dict[str, Any] | None = None,
    projection_path: str | None = None,
    zone: str = "ingest",
) -> str:
    """Publish a local scratch artifact through the storage_guardian HTTP API."""

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
    if projection_path:
        payload_metadata["projection_path"] = projection_path

    headers = _auth_headers(str(cfg["token"]))
    timeout = httpx.Timeout(float(os.environ.get("STORAGE_GUARDIAN_TIMEOUT_SECONDS", "60")))
    with httpx.Client(verify=bool(cfg["verify_tls"]), timeout=timeout) as client:
        if len(content) <= int(cfg["max_inline_bytes"]):
            response = _request_with_retries(
                lambda: client.post(
                    f"{cfg['url']}/internal/storage/objects",
                    headers={
                        **headers,
                        "Idempotency-Key": _idempotency_key(
                            agent,
                            store,
                            clean_name,
                            digest,
                            projection_path=projection_path,
                        ),
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
                ),
                purpose="create object",
            )
            return _object_uri(response.json())

        upload = _request_with_retries(
            lambda: client.post(
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
            ),
            purpose="create upload",
        )
        upload_id = str(upload.json()["upload_id"])
        append = client.put(
            f"{cfg['url']}/internal/storage/uploads/{upload_id}",
            headers=headers,
            content=content,
        )
        append.raise_for_status()
        committed = _request_with_retries(
            lambda: client.post(
                f"{cfg['url']}/internal/storage/uploads/{upload_id}/commit",
                headers={
                    **headers,
                    "Idempotency-Key": _idempotency_key(
                        agent,
                        store,
                        f"{clean_name}:{upload_id}:commit",
                        digest,
                        projection_path=projection_path,
                    ),
                },
                json={"sha256": digest, "metadata": payload_metadata},
            ),
            purpose="commit upload",
        )
        return _object_uri(committed.json())


def list_objects(
    *,
    agent: str,
    zone: str | None = None,
    status: str | None = "active",
) -> list[dict[str, Any]]:
    """List Storage Guardian objects through the public read-only API."""

    cfg = _client_config()
    if not cfg["url"]:
        return []

    params: dict[str, str] = {"agent": agent}
    if zone:
        params["zone"] = zone
    if status:
        params["status"] = status

    headers = _auth_headers(str(cfg["token"]))
    timeout = httpx.Timeout(float(os.environ.get("STORAGE_GUARDIAN_TIMEOUT_SECONDS", "60")))
    with httpx.Client(verify=bool(cfg["verify_tls"]), timeout=timeout) as client:
        response = client.get(f"{cfg['url']}/storage/objects", headers=headers, params=params)
        response.raise_for_status()
        payload = response.json()
    return payload if isinstance(payload, list) else []


def read_storage_object_text(
    uri: str,
    *,
    agent: str = "audio_transcribe",
    max_bytes: int = 12_000,
) -> dict[str, Any]:
    """Read a bounded text excerpt from an owned Storage Guardian object URI."""

    cfg = _client_config()
    if not cfg["url"]:
        return {}
    store, object_id = _parse_storage_guardian_uri(uri)
    if not store or not object_id:
        return {}

    headers = _auth_headers(str(cfg["token"]))
    timeout = httpx.Timeout(float(os.environ.get("STORAGE_GUARDIAN_TIMEOUT_SECONDS", "60")))
    try:
        with httpx.Client(verify=bool(cfg["verify_tls"]), timeout=timeout) as client:
            response = client.post(
                f"{cfg['url']}/internal/storage/objects/{object_id}/read-text",
                headers=headers,
                json={"agent": agent, "max_bytes": int(max_bytes)},
            )
            response.raise_for_status()
            payload = response.json()
    except Exception as exc:
        logger.info("Storage Guardian text read skipped for %s: %s", uri, exc)
        return {}
    if not isinstance(payload, dict):
        return {}
    if str(payload.get("store") or "") != store:
        return {}
    return payload


def source_reuse_metadata(
    source_path: Path | str | None,
    *,
    options: Any | None = None,
) -> dict[str, Any]:
    """Build privacy-preserving source metadata for transcript reuse."""

    metadata: dict[str, Any] = {
        "reuse_contract_version": REUSE_CONTRACT_VERSION,
        "projection_contract_version": PROJECTION_CONTRACT_VERSION,
    }
    if source_path:
        resolved = _normalized_source_path(source_path)
        metadata["source_path_hash"] = _sha256_text(resolved)
        metadata["source_filename"] = Path(str(source_path)).name
        content_hash = _source_content_hash(Path(str(source_path)))
        if content_hash:
            metadata["source_content_hash"] = content_hash
    option_payload = _options_payload(options)
    if option_payload:
        metadata["transcription_options"] = option_payload
        metadata["transcription_options_hash"] = _sha256_json(option_payload)
    return metadata


def find_published_transcription(
    source_path: Path,
    *,
    options: Any | None = None,
) -> dict[str, Any] | None:
    """Find reusable Storage Guardian transcript artifacts for a source file."""

    target_metadata = source_reuse_metadata(source_path, options=options)
    source_content_hash = target_metadata.get("source_content_hash")
    if not source_content_hash:
        return None

    try:
        objects = list_objects(agent="audio_transcribe", status="active")
    except Exception as exc:
        logger.info("Storage Guardian transcript reuse lookup skipped: %s", exc)
        return None

    requested_options = _options_payload(options)
    grouped: dict[str, dict[str, Any]] = {}
    for item in objects:
        metadata = item.get("metadata")
        if not isinstance(metadata, dict):
            continue
        if metadata.get("reuse_contract_version") != REUSE_CONTRACT_VERSION:
            continue
        if metadata.get("projection_contract_version") != PROJECTION_CONTRACT_VERSION:
            continue
        if metadata.get("source_content_hash") != source_content_hash:
            continue
        if not _options_compatible(metadata.get("transcription_options"), requested_options):
            continue
        artifact = str(metadata.get("artifact") or "").strip()
        job_id = str(metadata.get("job_id") or "").strip()
        store = str(item.get("store") or item.get("store_id") or "audio_outputs")
        object_id = str(item.get("object_id") or "").strip()
        if not artifact or not object_id:
            continue
        if not job_id:
            job_id = f"storage-{source_content_hash.split(':', 1)[-1][:12]}"
        entry = grouped.setdefault(
            job_id,
            {
                "job_id": job_id,
                "status": "completed",
                "outputs": {},
                "summary": {
                    "source_content_hash": source_content_hash,
                    "reused_from": "storage_guardian_objects",
                    "objects_count": 0,
                },
                "metadata": {
                    "reuse_contract_version": REUSE_CONTRACT_VERSION,
                    "projection_contract_version": PROJECTION_CONTRACT_VERSION,
                    "source_filename": metadata.get("source_filename"),
                    "source_path_hash": metadata.get("source_path_hash"),
                },
                "updated_at": float(item.get("updated_at") or item.get("created_at") or 0.0),
            },
        )
        entry["outputs"][artifact] = f"storage_guardian://{store}/{object_id}"
        projection = str(metadata.get("projection_materialized_path") or "").strip()
        if projection:
            entry.setdefault("managed_projections", {})[artifact] = projection
        entry["summary"]["objects_count"] = int(entry["summary"]["objects_count"]) + 1
        entry["updated_at"] = max(float(entry["updated_at"]), float(item.get("updated_at") or item.get("created_at") or 0.0))

    candidates = [
        item
        for item in grouped.values()
        if any(key in item["outputs"] for key in ("transcript_md", "transcript_txt", "transcript_clean_json", "rag_ready_json"))
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda item: float(item.get("updated_at") or 0.0), reverse=True)
    for candidate in candidates:
        if not _has_managed_transcript_projection(candidate):
            logger.info(
                "Storage Guardian transcript reuse skipped for %s: no managed transcript projection",
                candidate.get("job_id"),
            )
            continue
        if _has_readable_transcript_output(candidate):
            return candidate
        logger.info(
            "Storage Guardian transcript reuse skipped for %s: no readable transcript object",
            candidate.get("job_id"),
        )
    return None


def _has_managed_transcript_projection(candidate: dict[str, Any]) -> bool:
    projections = candidate.get("managed_projections")
    if not isinstance(projections, dict):
        return False
    return any(
        str(projections.get(artifact) or "").strip()
        for artifact in ("transcript_txt", "transcript_md", "transcript_clean_json", "rag_ready_json")
    )


def _has_readable_transcript_output(candidate: dict[str, Any]) -> bool:
    outputs = candidate.get("outputs")
    if not isinstance(outputs, dict):
        return False
    for artifact in ("transcript_txt", "transcript_md", "transcript_clean_json", "rag_ready_json"):
        uri = str(outputs.get(artifact) or "").strip()
        if not uri.startswith("storage_guardian://"):
            continue
        payload = read_storage_object_text(uri, max_bytes=512)
        text = str(payload.get("text") or "").strip()
        if payload and text:
            return True
    return False


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


def _request_with_retries(operation, *, purpose: str) -> httpx.Response:
    attempts = max(1, int(os.environ.get("STORAGE_GUARDIAN_PUBLISH_RETRY_ATTEMPTS", DEFAULT_PUBLISH_RETRY_ATTEMPTS)))
    delay = max(
        0.0,
        float(os.environ.get("STORAGE_GUARDIAN_PUBLISH_RETRY_DELAY_SECONDS", DEFAULT_PUBLISH_RETRY_DELAY_SECONDS)),
    )
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = operation()
            response.raise_for_status()
            return response
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code if exc.response is not None else 0
            if status_code not in {500, 502, 503, 504} or attempt >= attempts:
                raise
            last_error = exc
        except httpx.TransportError as exc:
            if attempt >= attempts:
                raise
            last_error = exc
        logger.info("Storage Guardian publish %s retry %s/%s after %s", purpose, attempt + 1, attempts, last_error)
        if delay:
            time.sleep(delay)
    if last_error:
        raise last_error
    raise StorageGuardianPublishError(f"Storage Guardian publish {purpose} did not return a response")


def _idempotency_key(
    agent: str,
    store: str,
    logical_name: str,
    digest: str,
    *,
    projection_path: str | None = None,
) -> str:
    projection = f":projection:{projection_path}" if projection_path else ""
    return f"{agent}:{store}:{hashlib.sha256(f'{logical_name}:{digest}{projection}'.encode('utf-8')).hexdigest()}"


def _logical_name(logical_name: str, path: Path) -> str:
    name = "".join(char if char.isalnum() or char in "._-" else "_" for char in logical_name).strip("._-")
    if not Path(name).suffix:
        name = f"{name}{path.suffix}"
    return name[:240] or path.name


def _object_uri(payload: dict[str, Any]) -> str:
    return f"storage_guardian://{payload.get('store')}/{payload.get('object_id')}"


def _parse_storage_guardian_uri(uri: str) -> tuple[str, str]:
    value = str(uri or "").strip()
    if not value.startswith("storage_guardian://"):
        return "", ""
    rest = value.removeprefix("storage_guardian://")
    store, sep, object_id = rest.partition("/")
    if not sep or not store or not object_id:
        return "", ""
    return store.strip(), object_id.strip()


def _normalized_source_path(value: Path | str) -> str:
    return os.path.realpath(os.path.abspath(os.path.expanduser(str(value))))


def _sha256_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _source_content_hash(path: Path) -> str:
    try:
        resolved = Path(_normalized_source_path(path))
        if not resolved.is_file():
            return ""
        digest = hashlib.sha256()
        with resolved.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return "sha256:" + digest.hexdigest()
    except OSError:
        return ""


def _options_payload(options: Any | None) -> dict[str, Any]:
    if options is None:
        return {}
    if hasattr(options, "model_dump"):
        payload = options.model_dump()
    elif isinstance(options, dict):
        payload = dict(options)
    else:
        return {}
    return {
        key: payload.get(key)
        for key in (
            "model",
            "language",
            "device",
            "compute_type",
            "diarization",
            "vad",
            "noise_reduction",
            "rag_ready",
        )
        if key in payload
    }


def _sha256_json(value: dict[str, Any]) -> str:
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(data.encode("utf-8")).hexdigest()


def _options_compatible(existing: object, requested: dict[str, Any]) -> bool:
    if not isinstance(existing, dict):
        return False
    if not requested:
        return True
    requested_language = str(requested.get("language") or "auto")
    existing_language = str(existing.get("language") or "auto")
    if requested_language != "auto" and existing_language not in {requested_language, "auto"}:
        return False
    for key in ("diarization", "vad", "noise_reduction", "rag_ready"):
        if key in requested and key in existing and bool(existing[key]) != bool(requested[key]):
            return False
    return True
