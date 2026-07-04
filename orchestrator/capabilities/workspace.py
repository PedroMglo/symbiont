"""Workspace capability matching.

The pipeline route node calls this module to resolve workspace-scoped tasks to
logical context sources. Domain vocabulary lives here, alongside capability
metadata, instead of being embedded in the central graph node.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from orchestrator.capabilities.catalog import workspace_capability_manifests
from orchestrator.capabilities.workspace_manifest import WorkspaceCapabilityManifest


@dataclass(frozen=True)
class WorkspaceCapabilityRoute:
    """Routing result for a workspace-scoped capability."""

    key: str
    context_sources: tuple[str, ...]
    agent_source: str
    selected_agents: tuple[str, ...] = ("reasoning_and_response",)


def _has_any(q: str, terms: tuple[str, ...]) -> bool:
    return any(term in q for term in terms)


def is_local_code_or_script_audit(query: str, *, workspace_bound: bool) -> bool:
    """Return True for workspace-scoped code/script inspection or safety audit tasks."""
    q = " ".join((query or "").lower().split())
    if not q:
        return False

    operational_incident_terms = (
        "502",
        "access.log",
        "app.log",
        "compressed logs",
        "diagnose intermittent",
        "error.log",
        "http 502",
        "incident",
        "incidente",
        "labroot",
        "nginx",
        "normalized timeline",
        "rotated logs",
        "timeline",
        "worker.log",
    )
    explicit_code_audit_terms = (
        "audit scripts",
        "audita scripts",
        "auditar scripts",
        "bash audit",
        "code audit",
        "dangerous scripts",
        "script audit",
        "scripts bash",
        "scripts perigos",
        "security audit",
        "shellcheck",
    )
    if _has_any(q, operational_incident_terms) and not _has_any(q, explicit_code_audit_terms):
        return False

    analysis_terms = (
        "analisa",
        "analisar",
        "audit",
        "audita",
        "auditar",
        "debug",
        "inspect",
        "inspeciona",
        "inspecionar",
        "review",
        "reve",
        "rev\u00ea",
        "rever",
        "security",
        "seguran\u00e7a",
        "seguranca",
        "safety",
    )
    code_keyword_terms = (
        "bash",
        "shell",
        "scripts/",
        "```sh",
        "#!/bin/sh",
        "#!/usr/bin/env bash",
        "shellcheck",
    )
    code_extension_patterns = (
        r"(?<![a-z0-9_])\.sh(?![a-z0-9_])",
        r"(?<![a-z0-9_])\.py(?![a-z0-9_])",
        r"(?<![a-z0-9_])\.js(?![a-z0-9_])",
        r"(?<![a-z0-9_])\.ts(?![a-z0-9_])",
        r"(?<![a-z0-9_])\.sql(?![a-z0-9_])",
    )
    destructive_terms = (
        "rm -rf",
        "find ",
        "-delete",
        "rsync",
        "--delete",
        "eval",
        "sudo",
        "mktemp -u",
        "tar ",
    )
    has_code_term = _has_any(q, code_keyword_terms) or any(
        re.search(pattern, q) for pattern in code_extension_patterns
    )
    return (
        _has_any(q, analysis_terms)
        and has_code_term
    ) or (
        workspace_bound
        and has_code_term
        and _has_any(q, destructive_terms)
    )


def _custom_local_code_audit(query: str, workspace_bound: bool) -> bool:
    return workspace_bound and is_local_code_or_script_audit(query, workspace_bound=workspace_bound)


CUSTOM_MATCHERS = {
    "local_code_audit": _custom_local_code_audit,
}


def _route_from_manifest(manifest: WorkspaceCapabilityManifest) -> WorkspaceCapabilityRoute:
    return WorkspaceCapabilityRoute(
        key=manifest.key,
        context_sources=manifest.context_sources,
        agent_source=manifest.agent_source,
        selected_agents=manifest.selected_agents,
    )


def _manifest_matches(manifest: WorkspaceCapabilityManifest, query: str, workspace_bound: bool) -> bool:
    if manifest.workspace_required and not workspace_bound:
        return False

    q = " ".join((query or "").lower().split())
    if not q:
        return False

    for group in manifest.positive_groups:
        if not _has_any(q, group):
            return False
    if manifest.positive_regexes and not any(re.search(pattern, q) for pattern in manifest.positive_regexes):
        return False
    if manifest.negative_terms and _has_any(q, manifest.negative_terms):
        return False

    if manifest.custom_matcher:
        matcher = CUSTOM_MATCHERS.get(manifest.custom_matcher)
        if matcher is None:
            raise ValueError(f"Unknown workspace capability matcher: {manifest.custom_matcher}")
        return matcher(query, workspace_bound)

    return bool(manifest.positive_groups or manifest.positive_regexes)


def match_workspace_capability(query: str, *, workspace_bound: bool) -> WorkspaceCapabilityRoute | None:
    """Return the first matching workspace capability route."""
    for manifest in workspace_capability_manifests():
        if _manifest_matches(manifest, query, workspace_bound):
            return _route_from_manifest(manifest)
    return None


def has_workspace_capability(query: str, *, workspace_bound: bool) -> bool:
    """Return True when a workspace-scoped query should gather capability context."""
    return match_workspace_capability(query, workspace_bound=workspace_bound) is not None
