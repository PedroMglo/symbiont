"""Storage authority envelopes for agentic storage operations."""

from __future__ import annotations

import json
from hashlib import sha256
from typing import Any

from orchestrator.agentic.context import get_agentic_context

AUTHORITY_NAME = "storage_guardian"


def storage_authority_metadata(
    *,
    operation: str,
    intent_payload: dict[str, Any],
    query: str = "",
    component: str = "FeatureClient.storage",
) -> dict[str, Any]:
    """Return correlation and idempotency metadata for storage_guardian calls."""

    ctx = get_agentic_context()
    stable_payload = {
        "operation": operation,
        "intent": intent_payload,
        "task_id": ctx.task_id if ctx is not None else "",
    }
    digest = sha256(
        json.dumps(stable_payload, sort_keys=True, default=str, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    metadata: dict[str, Any] = {
        "storage_authority": AUTHORITY_NAME,
        "authority_required": True,
        "component": component,
        "operation": operation,
        "idempotency_key": (
            f"agentic:{ctx.task_id}:storage:{operation}:{digest[:16]}"
            if ctx is not None
            else f"storage:{operation}:{digest[:16]}"
        ),
    }
    if query:
        metadata["query_hash"] = sha256(query.encode("utf-8")).hexdigest()
    if ctx is not None:
        metadata.update({
            "task_id": ctx.task_id,
            "trace_id": ctx.trace_id,
            "request_id": ctx.request_id,
            "session_id": ctx.session_id,
            "mode": ctx.mode,
        })
    return {key: value for key, value in metadata.items() if value is not None}


def storage_authority_envelope(
    *,
    operation: str,
    payload: dict[str, Any],
    query: str = "",
    component: str = "FeatureClient.storage",
) -> dict[str, Any]:
    """Attach authority metadata to a storage_guardian payload."""

    metadata = storage_authority_metadata(
        operation=operation,
        intent_payload=payload,
        query=query,
        component=component,
    )
    merged = dict(payload)
    existing_metadata = merged.get("metadata")
    merged["metadata"] = {
        **(existing_metadata if isinstance(existing_metadata, dict) else {}),
        **metadata,
    }
    for key in ("task_id", "trace_id", "request_id", "idempotency_key"):
        if key in metadata:
            merged.setdefault(key, metadata[key])
    merged.setdefault("requested_by", "@")
    merged.setdefault("storage_authority", AUTHORITY_NAME)
    return merged
