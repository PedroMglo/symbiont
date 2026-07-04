"""Declarative source-selection signals."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

MANIFEST_PATH = Path(__file__).with_name("source_selection.toml")


def _tuple_of_strings(value: Any, *, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{field} must be a list of strings")
    return tuple(value)


@dataclass(frozen=True)
class SourceSelectionManifest:
    """Declarative signals used by the orchestrator to select a source."""

    key: str
    source: str
    path_required: bool = False
    action_terms: tuple[str, ...] = ()
    processing_action_terms: tuple[str, ...] = ()
    processing_target_terms: tuple[str, ...] = ()
    intent_terms: tuple[str, ...] = ()
    extensions: tuple[str, ...] = ()
    folder_terms: tuple[str, ...] = ()
    guard_terms: tuple[str, ...] = ()
    source_terms: tuple[str, ...] = ()
    operation_terms: tuple[str, ...] = ()
    direct_terms: tuple[str, ...] = ()
    recovery_terms: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> "SourceSelectionManifest":
        key = raw.get("key")
        source = raw.get("source")
        if not isinstance(key, str) or not key:
            raise ValueError("source selection manifest requires non-empty key")
        if not isinstance(source, str) or not source:
            raise ValueError(f"{key}: source must be a non-empty string")
        path_required = raw.get("path_required", False)
        if not isinstance(path_required, bool):
            raise ValueError(f"{key}.path_required must be a boolean")
        return cls(
            key=key,
            source=source,
            path_required=path_required,
            action_terms=_tuple_of_strings(raw.get("action_terms"), field=f"{key}.action_terms"),
            processing_action_terms=_tuple_of_strings(
                raw.get("processing_action_terms"),
                field=f"{key}.processing_action_terms",
            ),
            processing_target_terms=_tuple_of_strings(
                raw.get("processing_target_terms"),
                field=f"{key}.processing_target_terms",
            ),
            intent_terms=_tuple_of_strings(raw.get("intent_terms"), field=f"{key}.intent_terms"),
            extensions=_tuple_of_strings(raw.get("extensions"), field=f"{key}.extensions"),
            folder_terms=_tuple_of_strings(raw.get("folder_terms"), field=f"{key}.folder_terms"),
            guard_terms=_tuple_of_strings(raw.get("guard_terms"), field=f"{key}.guard_terms"),
            source_terms=_tuple_of_strings(raw.get("source_terms"), field=f"{key}.source_terms"),
            operation_terms=_tuple_of_strings(raw.get("operation_terms"), field=f"{key}.operation_terms"),
            direct_terms=_tuple_of_strings(raw.get("direct_terms"), field=f"{key}.direct_terms"),
            recovery_terms=_tuple_of_strings(raw.get("recovery_terms"), field=f"{key}.recovery_terms"),
        )


@cache
def load_source_selection_manifests(path: Path = MANIFEST_PATH) -> tuple[SourceSelectionManifest, ...]:
    """Load source-selection manifests from TOML."""

    data = tomllib.loads(path.read_text(encoding="utf-8"))
    raw_items = data.get("source_selection", [])
    if not isinstance(raw_items, list):
        raise ValueError("source_selection must be a list")
    manifests: list[SourceSelectionManifest] = []
    for index, item in enumerate(raw_items):
        if not isinstance(item, dict):
            raise ValueError(f"source_selection[{index}] must be a table")
        manifests.append(SourceSelectionManifest.from_mapping(item))
    return tuple(manifests)


def source_selection_manifest(key: str) -> SourceSelectionManifest:
    """Return one source-selection manifest by key."""

    for manifest in load_source_selection_manifests():
        if manifest.key == key:
            return manifest
    raise KeyError(f"Unknown source selection manifest: {key}")
