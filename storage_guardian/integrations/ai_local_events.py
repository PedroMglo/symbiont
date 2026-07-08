"""Best-effort publisher for ai-local cross-service events."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 2.0
_REDACTED_PATH = "[REDACTED_PATH]"
_PATH_KEYS = {
    "archive_path",
    "current_path",
    "filelist_path",
    "manifest_path",
    "path",
    "paths",
    "restore_root",
    "source_file",
    "summary_path",
    "verify_path",
}


def publish_storage_event(
    event_type: str,
    *,
    payload: dict[str, Any],
    severity: str = "info",
    evidence_ref: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> bool:
    """Publish a storage-owned event to the orchestrator event bus.

    This is intentionally best-effort. The storage index remains authoritative
    even when the runtime event bus is unavailable.
    """

    base_url = _event_bus_base_url()
    token = _event_bus_token()
    if not base_url or not token:
        return False
    endpoint = _event_bus_endpoint(base_url)
    if not endpoint:
        return False

    body = {
        "producer": "storage_guardian",
        "type": event_type,
        "severity": severity,
        "payload": sanitize_storage_event_payload(payload),
        "evidence_ref": evidence_ref,
        "metadata": sanitize_storage_event_payload(metadata or {}),
    }
    data = json.dumps(body, default=str).encode("utf-8")
    timeout = _event_bus_timeout()
    try:
        response = requests.post(
            endpoint,
            data=data,
            headers={
                "Content-Type": "application/json",
                "X-Internal-Token": token,
                "User-Agent": "storage_guardian/ai-local-events",
            },
            timeout=timeout,
            verify=_requests_verify(),
        )
        return 200 <= response.status_code < 300
    except Exception as exc:
        log.debug("ai-local event publish skipped for %s: %s", event_type, exc)
        return False


def storage_lifecycle_event_type(local_event_type: str) -> str:
    if local_event_type in {"archive_plan_created", "explicit_archive_plan_created"}:
        return "storage.plan.created"
    return "storage.lifecycle.changed"


def sanitize_storage_event_payload(value: Any) -> Any:
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if _is_path_key(key_text):
                clean[key_text] = _REDACTED_PATH
            else:
                clean[key_text] = sanitize_storage_event_payload(item)
        return clean
    if isinstance(value, (list, tuple)):
        return [sanitize_storage_event_payload(item) for item in value]
    return value


def _is_path_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return normalized in _PATH_KEYS or normalized.endswith("_path") or normalized.endswith("_paths")


def _event_bus_base_url() -> str:
    configured = os.environ.get("AI_LOCAL_EVENT_BUS_URL", "").strip()
    if configured:
        return configured
    governor_url = os.environ.get("AI_RESOURCE_GOVERNOR_URL", "").strip()
    return governor_url


def _event_bus_endpoint(base_url: str) -> str:
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return f"{base_url.rstrip('/')}/agentic/ai-events"


def _event_bus_token() -> str:
    for env_name in ("AI_LOCAL_EVENT_BUS_TOKEN", "INTERNAL_API_KEY", "STORAGE_GUARDIAN_INTERNAL_TOKEN"):
        token = os.environ.get(env_name, "").strip()
        if token:
            return token
    for env_name in (
        "AI_LOCAL_EVENT_BUS_TOKEN_FILE",
        "STORAGE_GUARDIAN_INTERNAL_TOKEN_FILE",
        "AI_RESOURCE_GOVERNOR_TOKEN_FILE",
        "INTERNAL_API_KEY_FILE",
    ):
        token = _read_secret_file(os.environ.get(env_name, ""))
        if token:
            return token
    return ""


def _read_secret_file(path: str) -> str:
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _event_bus_timeout() -> float:
    raw = os.environ.get("AI_LOCAL_EVENT_BUS_TIMEOUT_SECONDS", "").strip()
    if not raw:
        return DEFAULT_TIMEOUT_SECONDS
    try:
        return max(0.1, min(float(raw), 10.0))
    except ValueError:
        return DEFAULT_TIMEOUT_SECONDS


def _requests_verify() -> str | bool:
    ca_file = os.environ.get("SSL_CERT_FILE", "").strip() or os.environ.get("REQUESTS_CA_BUNDLE", "").strip()
    if ca_file and Path(ca_file).exists():
        return ca_file
    return True
