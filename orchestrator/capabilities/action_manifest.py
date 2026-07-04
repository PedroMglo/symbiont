"""Declarative agentic action capability manifests."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

from orchestrator.agentic.contracts import CapabilityActionMetadata

MANIFEST_PATH = Path(__file__).with_name("action_capabilities.toml")


def _string_list(value: Any, *, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{field_name} must be a list of strings")
    return tuple(value)


def _schema_field(value: Any, *, capability_id: str, field_name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{capability_id}.{field_name} must be a table")
    if not value:
        raise ValueError(f"{capability_id}.{field_name} must not be empty")
    schema_ref = value.get("schema_ref")
    if not isinstance(schema_ref, str) or not schema_ref.strip():
        raise ValueError(f"{capability_id}.{field_name}.schema_ref is required")
    return dict(value)


@dataclass(frozen=True)
class ActionCapabilityManifest:
    """Transport metadata for one agentic action capability."""

    capability_id: str
    owner: str
    endpoint: str
    policy_action: str
    transport: dict[str, Any] = field(default_factory=dict)
    risk_level: str = "medium"
    supported_action_types: tuple[str, ...] = ("api_call",)
    resource_profile: dict[str, Any] = field(default_factory=dict)
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)
    evidence_types: tuple[str, ...] = ()
    writes_allowed: bool = False
    idempotency_policy: str = "required_for_writes"
    dry_run_supported: bool = False
    rollback_supported: bool = False
    events_published: tuple[str, ...] = ()
    risk_review_criteria: tuple[str, ...] = ()
    round_dependencies: tuple[str, ...] = ()
    timeout_seconds: float | None = None

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> "ActionCapabilityManifest":
        required = ("capability_id", "owner", "endpoint", "policy_action")
        missing = [field_name for field_name in required if not isinstance(raw.get(field_name), str) or not raw.get(field_name)]
        if missing:
            raise ValueError(f"action capability manifest missing required fields: {', '.join(missing)}")
        resource_profile = raw.get("resource_profile") or {}
        if not isinstance(resource_profile, dict):
            raise ValueError(f"{raw['capability_id']}.resource_profile must be a table")
        if not resource_profile:
            raise ValueError(f"{raw['capability_id']}.resource_profile must not be empty")
        transport = raw.get("transport") or {}
        if not isinstance(transport, dict):
            raise ValueError(f"{raw['capability_id']}.transport must be a table")
        if not transport:
            raise ValueError(f"{raw['capability_id']}.transport must not be empty")
        input_schema = _schema_field(raw.get("input_schema") or {}, capability_id=raw["capability_id"], field_name="input_schema")
        output_schema = _schema_field(raw.get("output_schema") or {}, capability_id=raw["capability_id"], field_name="output_schema")
        supported_action_types = _string_list(raw.get("supported_action_types"), field_name="supported_action_types")
        if not supported_action_types:
            raise ValueError(f"{raw['capability_id']}.supported_action_types must not be empty")
        evidence_types = _string_list(raw.get("evidence_types"), field_name="evidence_types")
        if not evidence_types:
            raise ValueError(f"{raw['capability_id']}.evidence_types must not be empty")
        events_published = _string_list(raw.get("events_published"), field_name="events_published")
        if not events_published:
            raise ValueError(f"{raw['capability_id']}.events_published must not be empty")
        writes_allowed = raw.get("writes_allowed", False)
        if not isinstance(writes_allowed, bool):
            raise ValueError(f"{raw['capability_id']}.writes_allowed must be a boolean")
        dry_run_supported = raw.get("dry_run_supported", False)
        if not isinstance(dry_run_supported, bool):
            raise ValueError(f"{raw['capability_id']}.dry_run_supported must be a boolean")
        rollback_supported = raw.get("rollback_supported", False)
        if not isinstance(rollback_supported, bool):
            raise ValueError(f"{raw['capability_id']}.rollback_supported must be a boolean")
        timeout_seconds = raw.get("timeout_seconds")
        if timeout_seconds is not None and not isinstance(timeout_seconds, (int, float)):
            raise ValueError(f"{raw['capability_id']}.timeout_seconds must be numeric")
        if timeout_seconds is None:
            raise ValueError(f"{raw['capability_id']}.timeout_seconds is required")
        if writes_allowed and not (dry_run_supported or rollback_supported):
            raise ValueError(f"{raw['capability_id']} writes require dry_run_supported or rollback_supported")
        risk_review_criteria = _string_list(raw.get("risk_review_criteria"), field_name="risk_review_criteria")
        if not risk_review_criteria:
            raise ValueError(f"{raw['capability_id']}.risk_review_criteria must not be empty")
        return cls(
            capability_id=raw["capability_id"],
            owner=raw["owner"],
            endpoint=raw["endpoint"],
            policy_action=raw["policy_action"],
            transport=dict(transport),
            risk_level=str(raw.get("risk_level") or "medium"),
            supported_action_types=supported_action_types,
            resource_profile=dict(resource_profile),
            input_schema=dict(input_schema),
            output_schema=dict(output_schema),
            evidence_types=evidence_types,
            writes_allowed=writes_allowed,
            idempotency_policy=str(raw.get("idempotency_policy") or "required_for_writes"),
            dry_run_supported=dry_run_supported,
            rollback_supported=rollback_supported,
            events_published=events_published,
            risk_review_criteria=risk_review_criteria,
            round_dependencies=_string_list(raw.get("round_dependencies"), field_name="round_dependencies"),
            timeout_seconds=float(timeout_seconds) if timeout_seconds is not None else None,
        )

    def to_action_metadata(self) -> CapabilityActionMetadata:
        return CapabilityActionMetadata(
            capability_id=self.capability_id,
            owner=self.owner,
            endpoint=self.endpoint,
            policy_action=self.policy_action,
            transport=self.transport,
            risk_level=self.risk_level,  # type: ignore[arg-type]
            supported_action_types=list(self.supported_action_types),
            resource_profile=self.resource_profile,
            input_schema=self.input_schema,
            output_schema=self.output_schema,
            evidence_types=list(self.evidence_types),
            writes_allowed=self.writes_allowed,
            idempotency_policy=self.idempotency_policy,
            dry_run_supported=self.dry_run_supported,
            rollback_supported=self.rollback_supported,
            events_published=list(self.events_published),
            risk_review_criteria=list(self.risk_review_criteria),
            round_dependencies=list(self.round_dependencies),
            timeout_seconds=self.timeout_seconds,
        )


@cache
def load_action_capability_manifests(path: Path = MANIFEST_PATH) -> tuple[ActionCapabilityManifest, ...]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    raw_items = data.get("action_capabilities", [])
    if not isinstance(raw_items, list):
        raise ValueError("action_capabilities must be a list")
    return tuple(ActionCapabilityManifest.from_mapping(item) for item in raw_items if isinstance(item, dict))


def action_capability_manifest(capability_id: str) -> ActionCapabilityManifest | None:
    normalized = capability_id.strip()
    if not normalized:
        return None
    for manifest in load_action_capability_manifests():
        if manifest.capability_id == normalized:
            return manifest
    return None
