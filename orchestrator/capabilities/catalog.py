"""Static service capability catalog.

The factory consumes this module to build the dispatch service registry. Keeping
the catalog outside the factory prevents service/capability ownership from
being hidden inside graph wiring code.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from orchestrator.capabilities.action_manifest import (
    ActionCapabilityManifest,
    action_capability_manifest,
    load_action_capability_manifests,
)
from orchestrator.capabilities.context_routing import ContextRoutingManifest, load_context_routing_manifests
from orchestrator.capabilities.service_manifest import (
    ServiceCapabilityManifest,
    load_service_capability_manifests,
    service_capability_manifest,
    service_capability_manifests_by_service,
)
from orchestrator.capabilities.source_selection import (
    SourceSelectionManifest,
    load_source_selection_manifests,
)
from orchestrator.capabilities.workspace_manifest import (
    WorkspaceCapabilityManifest,
    load_workspace_capability_manifests,
)


@dataclass(frozen=True)
class ServiceCatalogEntry:
    """Declarative service entry for the dispatch registry."""

    name: str
    url_attr: str
    kind: str
    capabilities: tuple[str, ...]
    description: str
    timeout: float
    default_url: str | None = None
    retries: int = 2
    health_path: str = "/health"
    enabled: bool = True

    def to_registry_config(self, cfg: Any) -> dict[str, Any]:
        """Render this catalog entry into the existing ServiceRegistry shape."""
        return self.to_registry_config_with_manifest(cfg, None)

    def to_registry_config_with_manifest(self, cfg: Any, manifest: ServiceCapabilityManifest | None) -> dict[str, Any]:
        """Render registry config, using capability manifests when available."""
        if self.default_url is None:
            url = getattr(cfg.services, self.url_attr)
        else:
            url = getattr(cfg.services, self.url_attr, self.default_url)
        return {
            "name": self.name,
            "url": url,
            "type": manifest.kind if manifest is not None else self.kind,
            "capabilities": list(manifest.capabilities if manifest is not None and manifest.capabilities else self.capabilities),
            "description": manifest.description if manifest is not None and manifest.description else self.description,
            "timeout": manifest.timeout_seconds if manifest is not None and manifest.timeout_seconds is not None else self.timeout,
            "retries": self.retries,
            "health_path": self.health_path,
            "enabled": self.enabled,
        }


SERVICE_CATALOG: tuple[ServiceCatalogEntry, ...] = (
    ServiceCatalogEntry(
        name="reasoning_and_response",
        url_attr="reasoning_and_response_url",
        kind="agent",
        capabilities=(
            "agent.reasoning_and_response.respond",
            "agent.reasoning_and_response.decompose",
            "agent.reasoning_and_response.synthesize",
            "agent.reasoning_and_response.critique",
            "agent.reasoning_and_response.classify",
            "chat",
            "direct_response",
            "planning",
            "decomposition",
            "synthesis",
            "critique",
            "classification",
        ),
        description="Read-only reasoning and response provider family",
        timeout=15.0,
    ),
    ServiceCatalogEntry(
        name="audio_transcribe",
        url_attr="audio_transcribe_url",
        kind="agent",
        capabilities=("transcription", "audio"),
        description="Audio transcription with Whisper",
        timeout=120.0,
    ),
    ServiceCatalogEntry(
        name="research",
        url_attr="research_url",
        kind="feature",
        capabilities=("rag", "cag", "search", "notes"),
        description="RAG/CAG search over notes and code",
        timeout=10.0,
    ),
    ServiceCatalogEntry(
        name="local_evidence_operator",
        url_attr="local_evidence_operator_url",
        kind="agent",
        capabilities=(
            "local_evidence_operator",
            "code_analysis",
            "graph",
            "repo",
            "data_analysis",
            "data_profile",
            "data_quality",
            "schema_drift",
            "sqlite_reconcile",
            "metric_reconcile",
            "ops_diagnostics",
            "log_performance",
            "incident_timeline",
            "compose_diagnostics",
            "security_analysis",
            "local_security_evidence",
        ),
        description="Consolidated read-only local evidence operator",
        timeout=30.0,
        default_url="https://local-evidence-operator:8000",
    ),
    ServiceCatalogEntry(
        name="execution_policy_operator",
        url_attr="execution_policy_operator_url",
        kind="agent",
        capabilities=(
            "execution_policy_operator",
            "bash_safety",
            "shell_static_analysis",
            "command_risk_classification",
            "destructive_command_detection",
            "dry_run_planning",
            "portable_shell_review",
        ),
        description="Deterministic execution policy and shell risk evidence",
        timeout=20.0,
        default_url="https://execution-policy-operator:8000",
    ),
    ServiceCatalogEntry(
        name="material_builder",
        url_attr="material_builder_url",
        kind="agent",
        capabilities=(
            "material_planning",
            "material_file_generation",
            "material_patch_proposal",
            "chunk_protocol",
            "no_static_fallback",
        ),
        description="Material proposal agent for structured plans, files, chunks and patches",
        timeout=120.0,
        default_url="https://material-builder:8000",
    ),
    ServiceCatalogEntry(
        name="workspace_execution",
        url_attr="workspace_execution_url",
        kind="feature",
        capabilities=(
            "workspace_execution",
            "disposable_sessions",
            "sandbox_command_contracts",
            "structured_diffs",
            "transient_artifacts",
            "storage_guardian_publish_contract",
        ),
        description="Disposable workspace execution sessions and sandbox evidence contracts",
        timeout=30.0,
        default_url="https://workspace-execution:8000",
    ),
    ServiceCatalogEntry(
        name="material_execution_kernel",
        url_attr="material_execution_kernel_url",
        kind="feature",
        capabilities=(
            "material_sessions",
            "incremental_manifest",
            "event_stream",
            "repair_loop",
            "patch_first",
            "requires_vm_backed_sandbox",
        ),
        description="Material execution session coordinator and completion evidence kernel",
        timeout=60.0,
        default_url="https://material-execution-kernel:8000",
    ),
    ServiceCatalogEntry(
        name="personal_context",
        url_attr="personal_context_url",
        kind="feature",
        capabilities=("calendar", "email", "rss", "personal"),
        description="Personal context (calendar, email, RSS)",
        timeout=10.0,
    ),
    ServiceCatalogEntry(
        name="extrator",
        url_attr="extrator_url",
        kind="feature",
        capabilities=("document_etl", "document_extraction", "file_conversion", "rag_bundle"),
        description="Document ETL and file conversion service",
        timeout=60.0,
    ),
    ServiceCatalogEntry(
        name="storage_guardian",
        url_attr="storage_guardian_url",
        kind="feature",
        capabilities=("storage_control", "archive", "restore", "lifecycle_storage"),
        description="Storage control plane, archive lifecycle and restore service",
        timeout=60.0,
    ),
    ServiceCatalogEntry(
        name="translation",
        url_attr="translation_url",
        kind="feature",
        capabilities=("i18n", "translation", "normalization", "pt_pt_lint"),
        description="Language normalization and PT/EN translation service",
        timeout=45.0,
    ),
)


def service_registry_config(cfg: Any) -> list[dict[str, Any]]:
    """Return ServiceRegistry config rendered from the capability catalog."""
    manifests = service_capability_manifest_map()
    return [entry.to_registry_config_with_manifest(cfg, manifests.get(entry.name)) for entry in SERVICE_CATALOG]


def service_catalog_entry_map() -> dict[str, ServiceCatalogEntry]:
    """Return endpoint catalog entries keyed by service name."""

    return {entry.name: entry for entry in SERVICE_CATALOG}


def workspace_capability_manifests() -> tuple[WorkspaceCapabilityManifest, ...]:
    """Return declarative workspace capability manifests."""

    return load_workspace_capability_manifests()


def source_selection_manifests() -> tuple[SourceSelectionManifest, ...]:
    """Return declarative source-selection manifests."""

    return load_source_selection_manifests()


def context_routing_manifests() -> tuple[ContextRoutingManifest, ...]:
    """Return declarative context routing manifests."""

    return load_context_routing_manifests()


def action_capability_manifests() -> tuple[ActionCapabilityManifest, ...]:
    """Return declarative agentic action capability manifests."""

    return load_action_capability_manifests()


def get_action_capability_manifest(capability_id: str) -> ActionCapabilityManifest | None:
    """Return one agentic action capability manifest by id."""

    return action_capability_manifest(capability_id)


def service_capability_manifests() -> tuple[ServiceCapabilityManifest, ...]:
    """Return declarative service/agent capability manifests."""

    return load_service_capability_manifests()


def get_service_capability_manifest(service_name: str) -> ServiceCapabilityManifest | None:
    """Return one service capability manifest by service name."""

    return service_capability_manifest(service_name)


def service_capability_manifest_map() -> dict[str, ServiceCapabilityManifest]:
    """Return service capability manifests keyed by service name."""

    return service_capability_manifests_by_service()
