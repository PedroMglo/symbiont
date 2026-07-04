"""Capability catalog and routing helpers."""

from orchestrator.capabilities.action_manifest import ActionCapabilityManifest
from orchestrator.capabilities.catalog import (
    ServiceCatalogEntry,
    action_capability_manifests,
    context_routing_manifests,
    get_action_capability_manifest,
    get_service_capability_manifest,
    service_capability_manifest_map,
    service_capability_manifests,
    service_catalog_entry_map,
    service_registry_config,
    source_selection_manifests,
    workspace_capability_manifests,
)
from orchestrator.capabilities.command_registry import (
    CommandRegistryEntry,
    command_registry_entries,
    command_registry_entry,
    match_command_registry_query,
)
from orchestrator.capabilities.context_routing import (
    ContextRoutingManifest,
    context_sources_for_intent,
    load_context_routing_manifests,
    required_context_sources_for_intent,
)
from orchestrator.capabilities.local_command_shortcuts import local_command_shortcuts
from orchestrator.capabilities.service_manifest import ServiceCapabilityManifest
from orchestrator.capabilities.source_selection import (
    SourceSelectionManifest,
    source_selection_manifest,
)
from orchestrator.capabilities.workspace import (
    WorkspaceCapabilityRoute,
    has_workspace_capability,
    is_local_code_or_script_audit,
    match_workspace_capability,
)
from orchestrator.capabilities.workspace_manifest import WorkspaceCapabilityManifest

__all__ = [
    "ActionCapabilityManifest",
    "CommandRegistryEntry",
    "ContextRoutingManifest",
    "ServiceCatalogEntry",
    "ServiceCapabilityManifest",
    "SourceSelectionManifest",
    "WorkspaceCapabilityManifest",
    "WorkspaceCapabilityRoute",
    "action_capability_manifests",
    "command_registry_entries",
    "command_registry_entry",
    "context_routing_manifests",
    "context_sources_for_intent",
    "get_action_capability_manifest",
    "get_service_capability_manifest",
    "has_workspace_capability",
    "is_local_code_or_script_audit",
    "local_command_shortcuts",
    "load_context_routing_manifests",
    "match_command_registry_query",
    "match_workspace_capability",
    "required_context_sources_for_intent",
    "service_capability_manifest_map",
    "service_capability_manifests",
    "service_catalog_entry_map",
    "service_registry_config",
    "source_selection_manifest",
    "source_selection_manifests",
    "workspace_capability_manifests",
]
