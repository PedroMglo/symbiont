"""Search public runtime tool envelopes without executing owners."""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from orchestrator.agentic.tool_envelope import (
    RuntimeToolEnvelope,
    resolve_runtime_tool_envelope,
    runtime_tool_envelope,
    runtime_tool_envelopes,
)

CapabilitySearchKind = Literal["all", "action", "service"]

_SECRET_KEY_PATTERN = re.compile(r"(api[_-]?key|token|secret|password|authorization|auth)", re.IGNORECASE)
_LOCAL_PATH_PATTERN = re.compile(r"^/(home|mnt|media|run/user|tmp)/")
_TOKEN_PATTERN = re.compile(r"[a-z0-9_./:-]+")


class CapabilitySearchResult(BaseModel):
    """One ranked public capability search result."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    score: int = Field(..., ge=0)
    matched_fields: list[str] = Field(default_factory=list)
    tool_envelope: dict[str, Any] = Field(default_factory=dict)


def search_capabilities(
    query: str,
    *,
    max_results: int = 8,
    kind: CapabilitySearchKind = "all",
) -> tuple[CapabilitySearchResult, ...]:
    """Return ranked public runtime tool envelopes for a natural-language query."""

    normalized_query = " ".join((query or "").strip().split())
    max_results = max(1, min(int(max_results), 50))
    selected = _selected_capability_id(normalized_query)
    if selected:
        envelope = runtime_tool_envelope(selected)
        if envelope is None or not _kind_matches(envelope, kind):
            return ()
        return (
            CapabilitySearchResult(
                score=10_000,
                matched_fields=["capability_id"],
                tool_envelope=_redact_public_payload(envelope.to_public_dict()),
            ),
        )

    query_tokens = _tokens(normalized_query)
    if not query_tokens:
        return ()

    results: list[CapabilitySearchResult] = []
    for envelope in runtime_tool_envelopes(kind=kind):
        score, matched_fields = _score_envelope(envelope, query_tokens)
        if score <= 0:
            continue
        results.append(
            CapabilitySearchResult(
                score=score,
                matched_fields=matched_fields,
                tool_envelope=_redact_public_payload(envelope.to_public_dict()),
            )
        )
    results.sort(key=lambda item: (-item.score, item.tool_envelope.get("capability_id", "")))
    return tuple(results[:max_results])


def select_capability(capability_id: str) -> dict[str, Any] | None:
    """Return one public runtime tool envelope by capability id or manifest alias."""

    selected = _selected_capability_id(capability_id) or (capability_id or "").strip()
    if not selected:
        return None
    envelope = resolve_runtime_tool_envelope(selected)
    if envelope is None:
        return None
    payload = envelope.to_public_dict()
    if selected != envelope.capability_id:
        _apply_manifest_alias_path(payload, selected)
        payload["selected_ref"] = selected
        payload["selection_match"] = "manifest_alias"
    return _redact_public_payload(payload)


def _score_envelope(envelope: RuntimeToolEnvelope, query_tokens: set[str]) -> tuple[int, list[str]]:
    fields = _search_fields(envelope)
    score = 0
    matched_fields: list[str] = []
    for field_name, raw_values in fields.items():
        values = [value.lower() for value in raw_values if value]
        if not values:
            continue
        field_score = 0
        for token in query_tokens:
            for value in values:
                if token == value:
                    field_score += _field_weight(field_name) * 3
                elif token in value:
                    field_score += _field_weight(field_name)
        if field_score > 0:
            score += field_score
            matched_fields.append(field_name)
    return score, matched_fields


def _search_fields(envelope: RuntimeToolEnvelope) -> dict[str, list[str]]:
    transport = envelope.transport if isinstance(envelope.transport, dict) else {}
    return {
        "capability_id": [envelope.capability_id],
        "owner": [envelope.owner],
        "kind": [envelope.kind, envelope.service_kind or ""],
        "service_name": [envelope.service_name or ""],
        "description": [envelope.description],
        "capabilities": list(envelope.capabilities),
        "policy_action": [envelope.policy_action],
        "risk_level": [envelope.risk_level],
        "evidence_types": list(envelope.evidence_types),
        "schema_refs": list(envelope.schema_refs.values()),
        "supported_action_types": list(envelope.supported_action_types),
        "events_published": list(envelope.events_published),
        "transport": [
            str(transport.get("type") or ""),
            str(transport.get("service") or ""),
            str(transport.get("method") or ""),
            str(transport.get("path") or ""),
            *[
                str(path)
                for path in (transport.get("provider_paths") or {}).values()
                if isinstance(transport.get("provider_paths"), dict)
            ],
        ],
    }


def _field_weight(field_name: str) -> int:
    return {
        "capability_id": 20,
        "owner": 12,
        "service_name": 12,
        "capabilities": 10,
        "description": 6,
        "policy_action": 6,
        "evidence_types": 5,
        "schema_refs": 5,
        "transport": 4,
        "supported_action_types": 3,
        "events_published": 3,
        "kind": 2,
        "risk_level": 2,
    }.get(field_name, 1)


def _tokens(value: str) -> set[str]:
    return {token for token in _TOKEN_PATTERN.findall(value.lower()) if len(token) > 1}


def _selected_capability_id(value: str) -> str:
    value = (value or "").strip()
    if not value.lower().startswith("select:"):
        return ""
    return value.split(":", 1)[1].strip()


def _apply_manifest_alias_path(payload: dict[str, Any], selected_ref: str) -> None:
    transport = payload.get("transport")
    if not isinstance(transport, dict):
        return
    provider_paths = transport.get("provider_paths")
    if not isinstance(provider_paths, dict):
        return
    provider_key = selected_ref.rsplit(".", 1)[-1].strip()
    path = provider_paths.get(provider_key)
    if not isinstance(path, str) or not path:
        return
    transport["path"] = path
    payload["endpoint"] = path


def _kind_matches(envelope: RuntimeToolEnvelope, kind: CapabilitySearchKind) -> bool:
    return kind == "all" or envelope.kind == kind


def _redact_public_payload(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if _SECRET_KEY_PATTERN.search(key_text):
                redacted[key] = "[redacted]"
            else:
                redacted[key] = _redact_public_payload(item)
        return redacted
    if isinstance(value, list):
        return [_redact_public_payload(item) for item in value]
    if isinstance(value, str) and _LOCAL_PATH_PATTERN.match(value):
        return "[local-path-redacted]"
    return value
