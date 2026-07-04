"""Declarative service capability manifests for agentic planning."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

MANIFEST_PATH = Path(__file__).with_name("service_capabilities.toml")
MANIFEST_FILES_ENV = "ORC_AGENTIC_SERVICE_CAPABILITY_MANIFESTS"
MANIFEST_DIRS_ENV = "ORC_AGENTIC_SERVICE_CAPABILITY_MANIFEST_DIRS"
OWNER_MANIFEST_NAMES = ("service_capabilities.toml", "agentic_capabilities.toml", "capabilities.toml")


def _string_list(value: Any, *, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{field_name} must be a list of strings")
    return tuple(value)


def _dict_field(value: Any, *, field_name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be a table")
    return dict(value)


def _string_list_dict(value: Any, *, field_name: str) -> dict[str, tuple[str, ...]]:
    raw = _dict_field(value, field_name=field_name)
    result: dict[str, tuple[str, ...]] = {}
    for key, item in raw.items():
        if not isinstance(key, str):
            raise ValueError(f"{field_name} keys must be strings")
        result[key] = _string_list(item, field_name=f"{field_name}.{key}")
    return result


def _schema_field(value: Any, *, service_name: str, field_name: str) -> dict[str, Any]:
    schema = _dict_field(value, field_name=f"{service_name}.{field_name}")
    if not schema:
        raise ValueError(f"{service_name}.{field_name} must not be empty")
    schema_ref = schema.get("schema_ref")
    if not isinstance(schema_ref, str) or not schema_ref.strip():
        raise ValueError(f"{service_name}.{field_name}.schema_ref is required")
    return schema


@dataclass(frozen=True)
class ServiceCapabilityManifest:
    """Agentic metadata for one registered service/agent.

    This manifest is intentionally transport/runtime metadata. It does not
    implement service behavior or duplicate feature semantics.
    """

    service_name: str
    capability_id: str
    kind: str
    owner: str
    capabilities: tuple[str, ...]
    policy_action: str
    description: str = ""
    transport: dict[str, Any] = field(default_factory=dict)
    risk_level: str = "low"
    supported_action_types: tuple[str, ...] = ("agent_invoke",)
    resource_profile: dict[str, Any] = field(default_factory=dict)
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)
    evidence_types: tuple[str, ...] = ()
    model_profile: str | None = None
    owner_family: str = ""
    lifecycle_status: str = "active"
    consolidation_target: str | None = None
    writes_allowed: bool = False
    idempotency_policy: str = "read_only"
    dry_run_supported: bool = False
    rollback_supported: bool = False
    events_published: tuple[str, ...] = ()
    risk_review_criteria: tuple[str, ...] = ()
    round_dependencies: tuple[str, ...] = ()
    timeout_seconds: float | None = None
    route_hints: dict[str, tuple[str, ...]] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> "ServiceCapabilityManifest":
        required = ("service_name", "capability_id", "kind", "owner", "policy_action")
        missing = [field_name for field_name in required if not isinstance(raw.get(field_name), str) or not raw.get(field_name)]
        if missing:
            raise ValueError(f"service capability manifest missing required fields: {', '.join(missing)}")
        kind = raw["kind"]
        if kind not in {"agent", "feature", "service"}:
            raise ValueError(f"{raw['service_name']}.kind must be one of agent, feature, service")
        capabilities = _string_list(raw.get("capabilities"), field_name=f"{raw['service_name']}.capabilities")
        if not capabilities:
            raise ValueError(f"{raw['service_name']}.capabilities must not be empty")
        transport = _dict_field(raw.get("transport"), field_name=f"{raw['service_name']}.transport")
        if not transport:
            raise ValueError(f"{raw['service_name']}.transport must not be empty")
        resource_profile = _dict_field(raw.get("resource_profile"), field_name=f"{raw['service_name']}.resource_profile")
        if not resource_profile:
            raise ValueError(f"{raw['service_name']}.resource_profile must not be empty")
        input_schema = _schema_field(raw.get("input_schema"), service_name=raw["service_name"], field_name="input_schema")
        output_schema = _schema_field(raw.get("output_schema"), service_name=raw["service_name"], field_name="output_schema")
        evidence_types = _string_list(raw.get("evidence_types"), field_name=f"{raw['service_name']}.evidence_types")
        if not evidence_types:
            raise ValueError(f"{raw['service_name']}.evidence_types must not be empty")
        supported_action_types = _string_list(
            raw.get("supported_action_types"),
            field_name=f"{raw['service_name']}.supported_action_types",
        )
        if not supported_action_types:
            raise ValueError(f"{raw['service_name']}.supported_action_types must not be empty")
        events_published = _string_list(raw.get("events_published"), field_name=f"{raw['service_name']}.events_published")
        if not events_published:
            raise ValueError(f"{raw['service_name']}.events_published must not be empty")
        writes_allowed = raw.get("writes_allowed", False)
        if not isinstance(writes_allowed, bool):
            raise ValueError(f"{raw['service_name']}.writes_allowed must be a boolean")
        dry_run_supported = raw.get("dry_run_supported", False)
        if not isinstance(dry_run_supported, bool):
            raise ValueError(f"{raw['service_name']}.dry_run_supported must be a boolean")
        rollback_supported = raw.get("rollback_supported", False)
        if not isinstance(rollback_supported, bool):
            raise ValueError(f"{raw['service_name']}.rollback_supported must be a boolean")
        timeout_seconds = raw.get("timeout_seconds")
        if timeout_seconds is not None and not isinstance(timeout_seconds, (int, float)):
            raise ValueError(f"{raw['service_name']}.timeout_seconds must be numeric")
        if timeout_seconds is None:
            raise ValueError(f"{raw['service_name']}.timeout_seconds is required")
        model_profile = raw.get("model_profile")
        if model_profile is not None and not isinstance(model_profile, str):
            raise ValueError(f"{raw['service_name']}.model_profile must be a string")
        if kind != "agent" and (model_profile or resource_profile.get("model_profile")):
            raise ValueError(f"{raw['service_name']}.model_profile is only valid for agent manifests")
        owner_family = str(raw.get("owner_family") or "").strip()
        lifecycle_status = str(raw.get("lifecycle_status") or "active").strip()
        if lifecycle_status not in {"active", "active_runtime_owner", "consolidate_candidate", "migrating", "delete_after_cutover"}:
            raise ValueError(
                f"{raw['service_name']}.lifecycle_status must be active, active_runtime_owner, "
                "consolidate_candidate, migrating, or delete_after_cutover"
            )
        consolidation_target = raw.get("consolidation_target")
        if consolidation_target is not None and not isinstance(consolidation_target, str):
            raise ValueError(f"{raw['service_name']}.consolidation_target must be a string")
        if writes_allowed and not (dry_run_supported or rollback_supported):
            raise ValueError(f"{raw['service_name']} writes require dry_run_supported or rollback_supported")
        risk_review_criteria = _string_list(
            raw.get("risk_review_criteria"),
            field_name=f"{raw['service_name']}.risk_review_criteria",
        )
        if not risk_review_criteria:
            raise ValueError(f"{raw['service_name']}.risk_review_criteria must not be empty")
        return cls(
            service_name=raw["service_name"],
            capability_id=raw["capability_id"],
            kind=kind,
            owner=raw["owner"],
            capabilities=capabilities,
            policy_action=raw["policy_action"],
            description=str(raw.get("description") or ""),
            transport=transport,
            risk_level=str(raw.get("risk_level") or "low"),
            supported_action_types=supported_action_types,
            resource_profile=resource_profile,
            input_schema=input_schema,
            output_schema=output_schema,
            evidence_types=evidence_types,
            model_profile=model_profile,
            owner_family=owner_family,
            lifecycle_status=lifecycle_status,
            consolidation_target=consolidation_target,
            writes_allowed=writes_allowed,
            idempotency_policy=str(raw.get("idempotency_policy") or "read_only"),
            dry_run_supported=dry_run_supported,
            rollback_supported=rollback_supported,
            events_published=events_published,
            risk_review_criteria=risk_review_criteria,
            round_dependencies=_string_list(
                raw.get("round_dependencies"),
                field_name=f"{raw['service_name']}.round_dependencies",
            ),
            timeout_seconds=float(timeout_seconds) if timeout_seconds is not None else None,
            route_hints=_string_list_dict(
                raw.get("route_hints"),
                field_name=f"{raw['service_name']}.route_hints",
            ),
        )

    def to_event_payload(self) -> dict[str, Any]:
        return {
            "service_name": self.service_name,
            "capability_id": self.capability_id,
            "kind": self.kind,
            "owner": self.owner,
            "capabilities": list(self.capabilities),
            "policy_action": self.policy_action,
            "description": self.description,
            "transport": self.transport,
            "risk_level": self.risk_level,
            "supported_action_types": list(self.supported_action_types),
            "resource_profile": self.resource_profile,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "evidence_types": list(self.evidence_types),
            "model_profile": self.model_profile,
            "owner_family": self.owner_family,
            "lifecycle_status": self.lifecycle_status,
            "consolidation_target": self.consolidation_target,
            "writes_allowed": self.writes_allowed,
            "idempotency_policy": self.idempotency_policy,
            "dry_run_supported": self.dry_run_supported,
            "rollback_supported": self.rollback_supported,
            "events_published": list(self.events_published),
            "risk_review_criteria": list(self.risk_review_criteria),
            "round_dependencies": list(self.round_dependencies),
            "timeout_seconds": self.timeout_seconds,
            "route_hints": {key: list(value) for key, value in self.route_hints.items()},
        }


@cache
def load_service_capability_manifests(
    path: Path = MANIFEST_PATH,
    extra_paths: tuple[Path, ...] = (),
) -> tuple[ServiceCapabilityManifest, ...]:
    manifest_by_service: dict[str, ServiceCapabilityManifest] = {}
    for manifest_path in (path, *_default_owner_manifest_paths(), *extra_paths, *_env_manifest_paths()):
        if not manifest_path.is_file():
            continue
        for manifest in _load_manifest_file(manifest_path):
            manifest_by_service[manifest.service_name] = manifest
    return tuple(manifest_by_service.values())


def _load_manifest_file(path: Path) -> tuple[ServiceCapabilityManifest, ...]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    raw_items = data.get("service_capabilities", [])
    if not isinstance(raw_items, list):
        raise ValueError("service_capabilities must be a list")
    manifests: list[ServiceCapabilityManifest] = []
    for index, item in enumerate(raw_items):
        if not isinstance(item, dict):
            raise ValueError(f"service_capabilities[{index}] must be a table")
        manifests.append(ServiceCapabilityManifest.from_mapping(item))
    return tuple(manifests)


def _env_manifest_paths() -> tuple[Path, ...]:
    paths: list[Path] = []
    for raw in os.environ.get(MANIFEST_FILES_ENV, "").split(os.pathsep):
        if raw.strip():
            paths.append(Path(raw).expanduser())
    for raw in os.environ.get(MANIFEST_DIRS_ENV, "").split(os.pathsep):
        if not raw.strip():
            continue
        directory = Path(raw).expanduser()
        if directory.is_dir():
            paths.extend(sorted(directory.glob("*.toml")))
    return tuple(paths)


def _default_owner_manifest_paths() -> tuple[Path, ...]:
    repo_root = Path(__file__).resolve().parents[2]
    roots = (
        repo_root / "agents",
        repo_root / "features",
        repo_root / "storage_guardian",
        repo_root / "obsidian-rag",
    )
    paths: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        for name in OWNER_MANIFEST_NAMES:
            candidate = root / name
            if candidate.is_file():
                paths.append(candidate)
        for child in sorted(item for item in root.iterdir() if item.is_dir()):
            for name in OWNER_MANIFEST_NAMES:
                candidate = child / name
                if candidate.is_file():
                    paths.append(candidate)
    return tuple(paths)


def service_capability_manifest(service_name: str) -> ServiceCapabilityManifest | None:
    normalized = service_name.strip()
    if not normalized:
        return None
    for manifest in load_service_capability_manifests():
        if manifest.service_name == normalized:
            return manifest
    return None


def service_capability_manifests_by_service() -> dict[str, ServiceCapabilityManifest]:
    return {manifest.service_name: manifest for manifest in load_service_capability_manifests()}
