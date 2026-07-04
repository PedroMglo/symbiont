"""Declarative workspace capability manifests."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

MANIFEST_PATH = Path(__file__).with_name("workspace_capabilities.toml")


def _tuple_of_strings(value: Any, *, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{field} must be a list of strings")
    return tuple(value)


def _tuple_of_string_groups(value: Any, *, field: str) -> tuple[tuple[str, ...], ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list of string lists")
    groups: list[tuple[str, ...]] = []
    for index, group in enumerate(value):
        if not isinstance(group, list) or not all(isinstance(item, str) for item in group):
            raise ValueError(f"{field}[{index}] must be a list of strings")
        groups.append(tuple(group))
    return tuple(groups)


@dataclass(frozen=True)
class WorkspaceCapabilityManifest:
    """Routing manifest for one workspace-scoped capability."""

    key: str
    context_sources: tuple[str, ...]
    agent_source: str
    selected_agents: tuple[str, ...] = ("reasoning_and_response",)
    workspace_required: bool = True
    positive_groups: tuple[tuple[str, ...], ...] = ()
    negative_terms: tuple[str, ...] = ()
    positive_regexes: tuple[str, ...] = ()
    custom_matcher: str | None = None

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> "WorkspaceCapabilityManifest":
        key = raw.get("key")
        agent_source = raw.get("agent_source")
        if not isinstance(key, str) or not key:
            raise ValueError("workspace capability manifest requires non-empty key")
        if not isinstance(agent_source, str) or not agent_source:
            raise ValueError(f"{key}: agent_source must be a non-empty string")
        selected_agents = _tuple_of_strings(raw.get("selected_agents"), field=f"{key}.selected_agents")
        custom_matcher = raw.get("custom_matcher")
        if custom_matcher is not None and not isinstance(custom_matcher, str):
            raise ValueError(f"{key}.custom_matcher must be a string")
        workspace_required = raw.get("workspace_required", True)
        if not isinstance(workspace_required, bool):
            raise ValueError(f"{key}.workspace_required must be a boolean")
        return cls(
            key=key,
            context_sources=_tuple_of_strings(raw.get("context_sources"), field=f"{key}.context_sources"),
            agent_source=agent_source,
            selected_agents=selected_agents or ("reasoning_and_response",),
            workspace_required=workspace_required,
            positive_groups=_tuple_of_string_groups(raw.get("positive_groups"), field=f"{key}.positive_groups"),
            negative_terms=_tuple_of_strings(raw.get("negative_terms"), field=f"{key}.negative_terms"),
            positive_regexes=_tuple_of_strings(raw.get("positive_regexes"), field=f"{key}.positive_regexes"),
            custom_matcher=custom_matcher,
        )


@cache
def load_workspace_capability_manifests(path: Path = MANIFEST_PATH) -> tuple[WorkspaceCapabilityManifest, ...]:
    """Load workspace capability manifests from TOML."""

    data = tomllib.loads(path.read_text(encoding="utf-8"))
    raw_items = data.get("workspace_capabilities", [])
    if not isinstance(raw_items, list):
        raise ValueError("workspace_capabilities must be a list")
    manifests: list[WorkspaceCapabilityManifest] = []
    for index, item in enumerate(raw_items):
        if not isinstance(item, dict):
            raise ValueError(f"workspace_capabilities[{index}] must be a table")
        manifests.append(WorkspaceCapabilityManifest.from_mapping(item))
    return tuple(manifests)
