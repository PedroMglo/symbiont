"""Declarative context routing manifests."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

from orchestrator.types import Intent

MANIFEST_PATH = Path(__file__).with_name("context_routing.toml")


def _string_list(value: Any, *, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise ValueError(f"{field} must be a list of non-empty strings")
    return tuple(item.strip() for item in value)


@dataclass(frozen=True)
class ContextRoutingManifest:
    """Default context sources for one classified intent."""

    intent: Intent
    sources: tuple[str, ...]
    required_sources: tuple[str, ...]

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> "ContextRoutingManifest":
        raw_intent = raw.get("intent")
        if not isinstance(raw_intent, str) or not raw_intent.strip():
            raise ValueError("context route requires non-empty intent")
        try:
            intent = Intent(raw_intent.strip())
        except ValueError as exc:
            raise ValueError(f"unknown context route intent: {raw_intent}") from exc
        sources = _string_list(raw.get("sources"), field=f"{intent.value}.sources")
        required_sources = _string_list(raw.get("required_sources"), field=f"{intent.value}.required_sources")
        missing = sorted(set(required_sources) - set(sources))
        if missing:
            raise ValueError(f"{intent.value}.required_sources must be a subset of sources: {missing}")
        return cls(intent=intent, sources=sources, required_sources=required_sources)

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent.value,
            "sources": list(self.sources),
            "required_sources": list(self.required_sources),
        }


@cache
def load_context_routing_manifests(path: Path = MANIFEST_PATH) -> tuple[ContextRoutingManifest, ...]:
    """Load context routing manifests from TOML."""

    data = tomllib.loads(path.read_text(encoding="utf-8"))
    raw_items = data.get("context_routes", [])
    if not isinstance(raw_items, list):
        raise ValueError("context_routes must be a list")
    manifests = tuple(ContextRoutingManifest.from_mapping(item) for item in raw_items if isinstance(item, dict))
    seen: set[Intent] = set()
    for manifest in manifests:
        if manifest.intent in seen:
            raise ValueError(f"duplicate context route intent: {manifest.intent.value}")
        seen.add(manifest.intent)
    return manifests


def context_routing_manifest_map() -> dict[Intent, ContextRoutingManifest]:
    """Return context routing manifests keyed by intent."""

    return {manifest.intent: manifest for manifest in load_context_routing_manifests()}


def context_sources_for_intent(intent: Intent) -> tuple[str, ...]:
    """Return configured context sources for an intent."""

    manifest = context_routing_manifest_map().get(intent)
    return manifest.sources if manifest is not None else ()


def required_context_sources_for_intent(intent: Intent) -> tuple[str, ...]:
    """Return required context sources for coverage validation."""

    manifest = context_routing_manifest_map().get(intent)
    return manifest.required_sources if manifest is not None else ()
