"""Resource-aware access to on-demand ETL/transcription services."""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from rag_config import settings

log = logging.getLogger(__name__)


class ExternalServicePending(RuntimeError):
    """Raised when a file should be retried by a later background cycle."""

    def __init__(self, service: str, reason: str, retry_after_seconds: int = 30) -> None:
        self.service = service
        self.reason = reason
        self.retry_after_seconds = retry_after_seconds
        super().__init__(service, reason, retry_after_seconds)

    def __str__(self) -> str:
        return f"{self.service}: {self.reason}"


def _read_optional_secret(path: str) -> str:
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _httpx_verify():
    cert = os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE")
    if cert and Path(cert).is_file():
        return cert
    return True


def _auth_headers(key: str) -> dict[str, str]:
    if not key:
        return {}
    return {"Authorization": f"Bearer {key}", "X-API-Key": key}


def _ensure_httpx():
    try:
        import httpx
    except ImportError as exc:
        raise ExternalServicePending("external_service", "httpx is not installed") from exc
    return httpx


def _service_health(url: str, *, timeout: float = 2.0) -> bool:
    if not url:
        return False
    httpx = _ensure_httpx()
    try:
        with httpx.Client(timeout=timeout, verify=_httpx_verify()) as client:
            response = client.get(f"{url.rstrip('/')}/health")
        return response.status_code < 400
    except Exception:
        return False


def ensure_lifecycle_service(service_name: str, service_url: str, *, timeout_seconds: int | None = None) -> None:
    """Start a lifecycle-managed service if it is not already healthy."""
    service_url = service_url.rstrip("/")
    if _service_health(service_url):
        return
    if not settings.sync.agent_wakeup_enabled:
        raise ExternalServicePending(service_name, "agent wakeup is disabled")

    lifecycle_url = (settings.sync.lifecycle_url or os.environ.get("AI_RESOURCE_GOVERNOR_URL", "")).rstrip("/")
    key = _read_optional_secret(settings.sync.lifecycle_api_key_file)
    if not lifecycle_url or not key:
        raise ExternalServicePending(service_name, "lifecycle endpoint or API key is not configured")

    httpx = _ensure_httpx()
    timeout = int(timeout_seconds or settings.sync.lifecycle_start_timeout_seconds)
    try:
        with httpx.Client(timeout=max(5, timeout), verify=_httpx_verify()) as client:
            response = client.post(
                f"{lifecycle_url}/lifecycle/{service_name}/start",
                headers=_auth_headers(key),
            )
            if response.status_code >= 400:
                detail = response.text[:300]
                raise ExternalServicePending(service_name, f"lifecycle start failed: {detail}")
    except ExternalServicePending:
        raise
    except Exception as exc:
        raise ExternalServicePending(service_name, f"lifecycle start unavailable: {exc}") from exc

    deadline = time.monotonic() + max(1, timeout)
    while time.monotonic() < deadline:
        if _service_health(service_url):
            return
        time.sleep(1)
    raise ExternalServicePending(service_name, f"service did not become healthy within {timeout}s")


def request_background_lease(
    *,
    service_name: str,
    capability: str,
    resource_class: str,
    path: Path,
    estimated_ram_mb: int,
    estimated_vram_mb: int | None = None,
    estimated_duration_seconds: int = 120,
):
    """Request a governor lease for work that can be safely deferred."""
    try:
        from integrations.resource_governor_client import request_lease

        size_mb = max(1, int(path.stat().st_size / (1024 * 1024)))
        lane = "heavy_gpu" if resource_class == "vram" else "background"
        lease = request_lease(
            component=f"external_{service_name}",
            lane=lane,
            lease_scope="request",
            resource_class=resource_class,
            capability=capability,
            estimated_duration_seconds=estimated_duration_seconds,
            estimated_ram_mb=estimated_ram_mb,
            estimated_vram_mb=estimated_vram_mb,
            estimated_io_mb=size_mb,
            preemptible=True,
            quality_policy="preserve",
            estimated_quality_impact="high",
            idempotency_suffix=f"{path.name}:{path.stat().st_mtime_ns}",
        )
    except Exception as exc:
        raise ExternalServicePending(service_name, f"resource lease unavailable: {exc}") from exc

    if not lease.granted:
        reason = getattr(lease.decision, "reason", "") or "resources unavailable"
        retry = getattr(lease.decision, "retry_after_seconds", None) or 30
        raise ExternalServicePending(service_name, reason, int(retry))
    return lease


def request_background_lease_with_wait(
    *,
    service_name: str,
    capability: str,
    resource_class: str,
    path: Path,
    estimated_ram_mb: int,
    estimated_vram_mb: int | None = None,
    estimated_duration_seconds: int = 120,
    max_wait_seconds: int = 120,
):
    """Wait for a retryable owner-service lease instead of dropping evidence.

    Evidence ingestion is usually quality-preserving background work. A transient
    pressure spike should delay extraction/transcription, not permanently skip a
    source file in the current RAG preparation run.
    """
    deadline = time.monotonic() + max(0, int(max_wait_seconds))
    last_error: ExternalServicePending | None = None
    while True:
        try:
            return request_background_lease(
                service_name=service_name,
                capability=capability,
                resource_class=resource_class,
                path=path,
                estimated_ram_mb=estimated_ram_mb,
                estimated_vram_mb=estimated_vram_mb,
                estimated_duration_seconds=estimated_duration_seconds,
            )
        except ExternalServicePending as exc:
            last_error = exc
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise last_error
            sleep_for = min(max(1, int(exc.retry_after_seconds or 1)), max(1, int(remaining)))
            log.info(
                "Owner-service lease deferred for %s (%s); retrying in %ss",
                service_name,
                exc.reason,
                sleep_for,
            )
            time.sleep(sleep_for)


def to_agent_visible_path(path: Path) -> str:
    """Map RAG container paths to the equivalent agent container paths."""
    resolved = Path(path).resolve()

    host_home = Path(os.environ.get("AI_RAG_HOST_HOME", "/app/sources/host_home")).resolve()
    try:
        return str(Path("/host_home") / resolved.relative_to(host_home))
    except ValueError:
        pass

    projects_root = Path(os.environ.get("AI_RAG_PROJECTS_ROOT", "/app/sources/repos/_projects")).resolve()
    try:
        return str(Path("/projects") / resolved.relative_to(projects_root))
    except ValueError:
        return str(resolved)


def extract_chunks_with_extrator(path: Path, *, force: bool = False) -> list[dict[str, Any]]:
    """Run the extrator feature and return normalized chunk payloads."""
    url = settings.sync.extrator_url.rstrip("/")
    key = _read_optional_secret(settings.sync.extrator_api_key_file)
    if not url or not key:
        raise ExternalServicePending("extrator", "extrator URL or API key is not configured")

    size_mb = max(1, int(path.stat().st_size / (1024 * 1024)))
    lease = request_background_lease_with_wait(
        service_name="extrator",
        capability="document_etl",
        resource_class="ram",
        path=path,
        estimated_ram_mb=min(2048, max(512, size_mb * 4)),
        estimated_duration_seconds=max(60, min(settings.sync.extrator_timeout_seconds, size_mb * 2)),
        max_wait_seconds=max(30, int(settings.sync.extrator_timeout_seconds)),
    )
    try:
        ensure_lifecycle_service("extrator", url, timeout_seconds=settings.sync.lifecycle_start_timeout_seconds)
        return _extract_chunks_with_extrator_unleased(path, url=url, key=key, force=force)
    finally:
        lease.release()


def _extract_chunks_with_extrator_unleased(path: Path, *, url: str, key: str, force: bool) -> list[dict[str, Any]]:
    httpx = _ensure_httpx()
    timeout = max(10, int(settings.sync.extrator_timeout_seconds))
    headers = _auth_headers(key)
    input_path = to_agent_visible_path(path)
    with httpx.Client(timeout=max(10, timeout), verify=_httpx_verify()) as client:
        response = client.post(
            f"{url}/v1/extrator/extractions/path",
            headers=headers,
            json={
                "input_path": input_path,
                "recursive": False,
                "force": force,
                "targets": ["markdown", "chunks", "tables", "graph_candidates"],
                "metadata": {
                    "requested_by": "obsidian-rag",
                    "rag_source_path": str(path),
                },
            },
        )
        if response.status_code >= 500:
            raise ExternalServicePending("extrator", f"service error: {response.text[:300]}")
        response.raise_for_status()
        job_id = response.json().get("job_id")
        if not job_id:
            raise ExternalServicePending("extrator", "job was not created")

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = client.get(f"{url}/v1/extrator/jobs/{job_id}", headers=headers)
            if status.status_code >= 500:
                raise ExternalServicePending("extrator", f"status error: {status.text[:300]}")
            status.raise_for_status()
            payload = status.json()
            state = payload.get("status")
            if state in {"completed", "completed_with_errors"}:
                return _chunks_from_extrator_result(client, url=url, headers=headers, payload=payload)
            if state in {"failed", "failed_recoverable", "cancelled"}:
                error = payload.get("error") or json.dumps(payload.get("summary", {}), ensure_ascii=False)
                raise RuntimeError(f"extrator failed for {input_path}: {error}")
            time.sleep(2)

    raise ExternalServicePending("extrator", f"job did not finish within {timeout}s")


def _chunks_from_extrator_result(client, *, url: str, headers: dict[str, str], payload: dict[str, Any]) -> list[dict[str, Any]]:
    outputs = payload.get("outputs") or {}
    chunks: list[dict[str, Any]] = []
    for doc_id in sorted(outputs):
        response = client.get(f"{url}/v1/extrator/documents/{doc_id}/chunks", headers=headers)
        if response.status_code >= 500:
            raise ExternalServicePending("extrator", f"chunk read failed: {response.text[:300]}")
        response.raise_for_status()
        raw_chunks = response.json()
        if isinstance(raw_chunks, list):
            chunks.extend(item for item in raw_chunks if isinstance(item, dict))
    if chunks:
        return chunks

    summary = payload.get("summary") or {}
    if summary.get("errors"):
        raise RuntimeError(f"extrator produced no chunks: {json.dumps(summary['errors'], ensure_ascii=False)}")
    return []
