"""Runtime tool envelopes built from owner-published manifests.

The envelope is orchestration metadata only. It does not execute owner behavior
or import owner packages.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from orchestrator.capabilities.action_manifest import ActionCapabilityManifest
from orchestrator.capabilities.catalog import action_capability_manifests, service_capability_manifests
from orchestrator.capabilities.service_manifest import ServiceCapabilityManifest

EnvelopeKind = Literal["action", "service"]
EnvelopeFilter = Literal["all", "action", "service"]


class RuntimeToolEnvelope(BaseModel):
    """Public runtime metadata for one callable capability."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    capability_id: str = Field(..., min_length=1, max_length=200)
    owner: str = Field(..., min_length=1, max_length=200)
    transport: dict[str, Any] = Field(default_factory=dict)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    schema_refs: dict[str, str] = Field(default_factory=dict)
    policy_action: str = Field(..., min_length=1, max_length=200)
    risk_level: str = Field("medium", max_length=40)
    is_read_only: bool = False
    is_concurrency_safe: bool = False
    resource_profile: dict[str, Any] = Field(default_factory=dict)
    idempotency_policy: str = Field("required_for_writes", max_length=200)
    evidence_types: list[str] = Field(default_factory=list)
    owner_family: str = Field("", max_length=200)
    lifecycle_status: str = Field("active", max_length=80)
    consolidation_target: str | None = Field(None, max_length=200)
    events_published: list[str] = Field(default_factory=list)
    result_persistence: dict[str, Any] = Field(default_factory=dict)
    kind: EnvelopeKind
    source: str = Field(..., min_length=1, max_length=40)
    service_name: str | None = Field(None, max_length=200)
    service_kind: str | None = Field(None, max_length=40)
    endpoint: str | None = Field(None, max_length=1000)
    description: str = Field("", max_length=2000)
    capabilities: list[str] = Field(default_factory=list)
    model_profile: str | None = Field(None, max_length=200)
    supported_action_types: list[str] = Field(default_factory=list)
    writes_allowed: bool = False
    dry_run_supported: bool = False
    rollback_supported: bool = False
    risk_review_criteria: list[str] = Field(default_factory=list)
    round_dependencies: list[str] = Field(default_factory=list)
    timeout_seconds: float | None = Field(None, gt=0)

    def to_public_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def from_action_manifest(manifest: ActionCapabilityManifest) -> RuntimeToolEnvelope:
    return RuntimeToolEnvelope(
        capability_id=manifest.capability_id,
        owner=manifest.owner,
        transport=dict(manifest.transport),
        input_schema=dict(manifest.input_schema),
        output_schema=dict(manifest.output_schema),
        schema_refs=_schema_refs(manifest.input_schema, manifest.output_schema),
        policy_action=manifest.policy_action,
        risk_level=manifest.risk_level,
        is_read_only=not manifest.writes_allowed,
        is_concurrency_safe=_is_concurrency_safe(manifest.resource_profile, manifest.transport),
        resource_profile=dict(manifest.resource_profile),
        idempotency_policy=manifest.idempotency_policy,
        evidence_types=list(manifest.evidence_types),
        events_published=list(manifest.events_published),
        result_persistence=_result_persistence(
            writes_allowed=manifest.writes_allowed,
            evidence_types=manifest.evidence_types,
        ),
        kind="action",
        source="action_manifest",
        endpoint=manifest.endpoint,
        supported_action_types=list(manifest.supported_action_types),
        writes_allowed=manifest.writes_allowed,
        dry_run_supported=manifest.dry_run_supported,
        rollback_supported=manifest.rollback_supported,
        risk_review_criteria=list(manifest.risk_review_criteria),
        round_dependencies=list(manifest.round_dependencies),
        timeout_seconds=manifest.timeout_seconds,
    )


def from_service_manifest(manifest: ServiceCapabilityManifest) -> RuntimeToolEnvelope:
    return RuntimeToolEnvelope(
        capability_id=manifest.capability_id,
        owner=manifest.owner,
        transport=dict(manifest.transport),
        input_schema=dict(manifest.input_schema),
        output_schema=dict(manifest.output_schema),
        schema_refs=_schema_refs(manifest.input_schema, manifest.output_schema),
        policy_action=manifest.policy_action,
        risk_level=manifest.risk_level,
        is_read_only=not manifest.writes_allowed,
        is_concurrency_safe=_is_concurrency_safe(manifest.resource_profile, manifest.transport),
        resource_profile=dict(manifest.resource_profile),
        idempotency_policy=manifest.idempotency_policy,
        evidence_types=list(manifest.evidence_types),
        owner_family=manifest.owner_family,
        lifecycle_status=manifest.lifecycle_status,
        consolidation_target=manifest.consolidation_target,
        events_published=list(manifest.events_published),
        result_persistence=_result_persistence(
            writes_allowed=manifest.writes_allowed,
            evidence_types=manifest.evidence_types,
        ),
        kind="service",
        source="service_manifest",
        service_name=manifest.service_name,
        service_kind=manifest.kind,
        endpoint=str(manifest.transport.get("path") or "") or None,
        description=manifest.description,
        capabilities=list(manifest.capabilities),
        model_profile=manifest.model_profile,
        supported_action_types=list(manifest.supported_action_types),
        writes_allowed=manifest.writes_allowed,
        dry_run_supported=manifest.dry_run_supported,
        rollback_supported=manifest.rollback_supported,
        risk_review_criteria=list(manifest.risk_review_criteria),
        round_dependencies=list(manifest.round_dependencies),
        timeout_seconds=manifest.timeout_seconds,
    )


def action_tool_envelopes() -> tuple[RuntimeToolEnvelope, ...]:
    return tuple(from_action_manifest(manifest) for manifest in action_capability_manifests())


def service_tool_envelopes() -> tuple[RuntimeToolEnvelope, ...]:
    return tuple(from_service_manifest(manifest) for manifest in service_capability_manifests())


def runtime_tool_envelopes(kind: EnvelopeFilter = "all") -> tuple[RuntimeToolEnvelope, ...]:
    if kind == "action":
        return action_tool_envelopes()
    if kind == "service":
        return service_tool_envelopes()
    return (*service_tool_envelopes(), *action_tool_envelopes())


def runtime_tool_envelope(capability_id: str) -> RuntimeToolEnvelope | None:
    normalized = capability_id.strip()
    if not normalized:
        return None
    for envelope in runtime_tool_envelopes():
        if envelope.capability_id == normalized:
            return envelope
    return None


def resolve_runtime_tool_envelope(
    capability_ref: str,
    *,
    kind: EnvelopeFilter = "all",
) -> RuntimeToolEnvelope | None:
    """Resolve a public capability reference to its current runtime envelope.

    Canonical capability IDs win. Service manifests may also publish stable
    aliases in their ``capabilities`` field during migrations; resolving those
    aliases returns the current owner transport without executing it.
    """

    normalized = capability_ref.strip()
    if not normalized:
        return None
    for envelope in runtime_tool_envelopes(kind=kind):
        if envelope.capability_id == normalized:
            return envelope
    for envelope in runtime_tool_envelopes(kind=kind):
        if envelope.kind != "service":
            continue
        if normalized in {envelope.service_name or "", envelope.capability_id, *envelope.capabilities}:
            return envelope
    return None


def _is_concurrency_safe(*metadata_sources: dict[str, Any]) -> bool:
    for metadata in metadata_sources:
        for key in ("is_concurrency_safe", "concurrency_safe"):
            if metadata.get(key) is True:
                return True
    return False


def _schema_refs(input_schema: dict[str, Any], output_schema: dict[str, Any]) -> dict[str, str]:
    refs: dict[str, str] = {}
    for name, schema in (("input", input_schema), ("output", output_schema)):
        ref = schema.get("schema_ref") if isinstance(schema, dict) else None
        if isinstance(ref, str) and ref.strip():
            refs[name] = ref.strip()
    return refs


def _result_persistence(*, writes_allowed: bool, evidence_types: tuple[str, ...]) -> dict[str, Any]:
    if writes_allowed:
        return {
            "mode": "owner_evidence_required",
            "durable_outputs": "owner_contract",
            "preview_required": True,
            "requires_evidence_ref": True,
            "evidence_types": list(evidence_types),
        }
    return {
        "mode": "read_only_evidence",
        "durable_outputs": "none",
        "preview_required": False,
        "requires_evidence_ref": False,
        "evidence_types": list(evidence_types),
    }
