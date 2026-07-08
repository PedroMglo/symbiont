"""Owner-published capability manifests for internal evidence routing."""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class OwnerCapabilityManifest:
    component: str
    owner: str
    path: Path
    routing_terms: tuple[str, ...]
    context_sources: tuple[str, ...]
    local_evidence_required: bool = True
    generic_answer_allowed: bool = False

    def matches(self, query: str) -> bool:
        q = " ".join((query or "").lower().split())
        return bool(q and any(term in q for term in self.routing_terms))


def owner_context_sources_for_query(query: str) -> tuple[str, ...]:
    sources: list[str] = []
    for manifest in owner_capability_manifests():
        if not manifest.matches(query):
            continue
        for source in manifest.context_sources:
            if source not in sources:
                sources.append(source)
    return tuple(sources)


def owner_evidence_required_for_query(query: str) -> bool:
    return any(
        manifest.local_evidence_required and manifest.matches(query)
        for manifest in owner_capability_manifests()
    )


@lru_cache(maxsize=1)
def owner_capability_manifests() -> tuple[OwnerCapabilityManifest, ...]:
    paths = sorted(
        path
        for base in (ROOT / "orchestrator", ROOT / "obsidian-rag", ROOT / "storage_guardian")
        if base.exists()
        for path in base.rglob("CAPABILITY.yaml")
    )
    return tuple(_load_manifest(path) for path in paths)


def _load_manifest(path: Path) -> OwnerCapabilityManifest:
    raw = _load_yaml_or_json(path)
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: manifest must be a mapping")
    routing = raw.get("routing") if isinstance(raw.get("routing"), dict) else {}
    evidence = raw.get("evidence_policy") if isinstance(raw.get("evidence_policy"), dict) else {}
    return OwnerCapabilityManifest(
        component=str(raw.get("component") or path.parent.name),
        owner=str(raw.get("owner") or path.parent.as_posix()),
        path=path,
        routing_terms=tuple(_as_lower_strings(routing.get("terms"))),
        context_sources=tuple(_as_lower_strings(routing.get("context_sources"))),
        local_evidence_required=bool(evidence.get("local_evidence_required", True)),
        generic_answer_allowed=bool(evidence.get("generic_answer_allowed", False)),
    )


def _load_yaml_or_json(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    try:
        import yaml
    except ImportError:
        if path.suffix.lower() == ".json":
            return json.loads(text)
        return _load_simple_yaml_mapping(text)
    return yaml.safe_load(text)


def _load_simple_yaml_mapping(text: str) -> dict[str, Any]:
    root: dict[str, Any] = {}
    current_map: dict[str, Any] | None = None
    current_list_key: str | None = None
    for raw_line in text.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        line = raw_line.strip()
        if indent == 0:
            current_map = None
            current_list_key = None
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip()
            if value:
                root[key] = _parse_scalar(value)
            else:
                root[key] = {}
                current_map = root[key]
            continue
        if current_map is None:
            continue
        if indent == 2:
            current_list_key = None
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip()
            if value:
                current_map[key] = _parse_scalar(value)
            else:
                current_map[key] = []
                current_list_key = key
            continue
        if indent >= 4 and current_list_key and line.startswith("- "):
            current_map[current_list_key].append(_parse_scalar(line[2:].strip()))
    return root


def _parse_scalar(value: str) -> Any:
    if value in {"true", "True"}:
        return True
    if value in {"false", "False"}:
        return False
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _as_lower_strings(value: Any) -> list[str]:
    if not isinstance(value, list | tuple):
        return []
    return [str(item).strip().lower() for item in value if str(item).strip()]
