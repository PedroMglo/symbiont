"""Declarative shortcuts for the terminal alias local bridge."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

MANIFEST_PATH = Path(__file__).with_name("local_command_shortcuts.toml")


def _tuple_of_strings(value: Any, *, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{field} must be a list of strings")
    return tuple(value)


def _mapping_of_string_lists(value: Any, *, field: str) -> dict[str, tuple[str, ...]]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be a table")
    result: dict[str, tuple[str, ...]] = {}
    for key, raw in value.items():
        result[str(key)] = _tuple_of_strings(raw, field=f"{field}.{key}")
    return result


def _mapping_of_strings(value: Any, *, field: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict) or not all(isinstance(item, str) for item in value.values()):
        raise ValueError(f"{field} must be a string table")
    return {str(key): str(item) for key, item in value.items()}


@dataclass(frozen=True)
class ResourceStatusSignals:
    """Terms used to identify live resource telemetry shortcuts."""

    metric_terms: tuple[str, ...]
    state_terms: tuple[str, ...]


@dataclass(frozen=True)
class LocalCommandShortcutManifest:
    """Manifest for terminal alias shortcut planning."""

    common: dict[str, tuple[str, ...]]
    commands: dict[str, tuple[str, ...]]
    route_labels: dict[str, str]
    resource_status: ResourceStatusSignals

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> "LocalCommandShortcutManifest":
        resource = raw.get("resource_status")
        if not isinstance(resource, dict):
            raise ValueError("resource_status must be a table")
        return cls(
            common=_mapping_of_string_lists(
                raw.get("local_command_shortcuts", {}).get("common")
                if isinstance(raw.get("local_command_shortcuts"), dict)
                else None,
                field="local_command_shortcuts.common",
            ),
            commands=_mapping_of_string_lists(
                raw.get("local_command_shortcuts", {}).get("commands")
                if isinstance(raw.get("local_command_shortcuts"), dict)
                else None,
                field="local_command_shortcuts.commands",
            ),
            route_labels=_mapping_of_strings(
                raw.get("local_command_shortcuts", {}).get("route_labels")
                if isinstance(raw.get("local_command_shortcuts"), dict)
                else None,
                field="local_command_shortcuts.route_labels",
            ),
            resource_status=ResourceStatusSignals(
                metric_terms=_tuple_of_strings(
                    resource.get("metric_terms"),
                    field="resource_status.metric_terms",
                ),
                state_terms=_tuple_of_strings(
                    resource.get("state_terms"),
                    field="resource_status.state_terms",
                ),
            ),
        )


@cache
def local_command_shortcuts(path: Path = MANIFEST_PATH) -> LocalCommandShortcutManifest:
    """Load local command shortcut manifests from TOML."""

    return LocalCommandShortcutManifest.from_mapping(tomllib.loads(path.read_text(encoding="utf-8")))
