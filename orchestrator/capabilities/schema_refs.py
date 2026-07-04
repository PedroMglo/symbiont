"""Schema reference helpers for capability manifest validation."""

from __future__ import annotations

import importlib
from dataclasses import is_dataclass
from typing import Any

from pydantic import BaseModel, TypeAdapter

SCHEMA_REF_PREFIX = "python:"


def schema_ref_json_schema(schema_ref: str) -> dict[str, Any]:
    """Resolve a schema reference and return a JSON Schema document."""
    target = resolve_schema_ref(schema_ref)
    if isinstance(target, type) and issubclass(target, BaseModel):
        return target.model_json_schema()
    if is_dataclass(target):
        return TypeAdapter(target).json_schema()
    return TypeAdapter(target).json_schema()


def resolve_schema_ref(schema_ref: str) -> Any:
    """Resolve ``python:<module>:<symbol>`` refs without executing owner logic."""
    normalized = schema_ref.strip()
    if not normalized.startswith(SCHEMA_REF_PREFIX):
        raise ValueError(f"unsupported schema_ref scheme: {schema_ref!r}")
    body = normalized[len(SCHEMA_REF_PREFIX) :]
    module_name, separator, symbol_path = body.partition(":")
    if not module_name or not separator or not symbol_path:
        raise ValueError(f"invalid schema_ref: {schema_ref!r}")
    module = importlib.import_module(module_name)
    target: Any = module
    for part in symbol_path.split("."):
        if not part:
            raise ValueError(f"invalid schema_ref symbol: {schema_ref!r}")
        target = getattr(target, part)
    return target


def schema_property_names(schema: dict[str, Any]) -> set[str]:
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return set()
    return {str(key) for key in properties}


def schema_required_names(schema: dict[str, Any]) -> set[str]:
    required = schema.get("required")
    if not isinstance(required, list):
        return set()
    return {str(item) for item in required}
