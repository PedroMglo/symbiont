"""Response normalization contracts for feature dispatch."""

from __future__ import annotations

import json
from typing import Any

from orchestrator.dispatch.types import FeatureQueryResponse


def normalize_feature_response(
    *,
    data: dict[str, Any],
    source: str,
    latency_ms: float,
) -> FeatureQueryResponse:
    """Normalize a feature HTTP payload into the dispatch response contract."""
    return FeatureQueryResponse(
        content=extract_feature_content(data),
        source=source,
        token_estimate=int(data.get("token_estimate", data.get("tokens", 0)) or 0),
        success=bool(data.get("success", True)),
        latency_ms=latency_ms,
        metadata=dict(data.get("metadata", {}) or {}),
        error=str(data.get("error", "") or ""),
    )


def extract_feature_content(data: dict[str, Any]) -> str:
    """Extract display/context text from known feature response shapes."""
    if "content" in data:
        return str(data["content"])

    code_parts = []
    for key, label in (
        ("file_context", "File Context"),
        ("graph_context", "Graph Context"),
        ("repo_context", "Repo Context"),
    ):
        value = data.get(key)
        if value:
            code_parts.append(f"## {label}\n{value}")
    if code_parts:
        return "\n\n".join(code_parts)

    if "report" in data:
        return str(data["report"])

    if "results" in data and isinstance(data["results"], list):
        parts = []
        for item in data["results"]:
            if isinstance(item, dict):
                parts.append(item.get("content", item.get("text", str(item))))
            else:
                parts.append(str(item))
        return "\n\n".join(parts)

    if "probes" in data:
        parts = []
        for probe in data["probes"]:
            if isinstance(probe, dict):
                parts.append(f"[{probe.get('subsystem', '?')}] {probe.get('output', '')}")
        return "\n".join(parts)

    if "events" in data:
        parts = []
        for event in data["events"]:
            if isinstance(event, dict):
                parts.append(f"• {event.get('summary', '?')} ({event.get('start', '')})")
        return "\n".join(parts) if parts else "(no events)"

    if "emails" in data:
        parts = []
        for email in data["emails"]:
            if isinstance(email, dict):
                parts.append(f"• {email.get('subject', '?')} from {email.get('sender', '?')}")
        return "\n".join(parts) if parts else "(no emails)"

    if "items" in data:
        parts = []
        for item in data["items"]:
            if isinstance(item, dict):
                parts.append(f"• {item.get('title', '?')} ({item.get('feed_name', '')})")
        return "\n".join(parts) if parts else "(no feed items)"

    return json.dumps(data, ensure_ascii=False, default=str)[:2000]
