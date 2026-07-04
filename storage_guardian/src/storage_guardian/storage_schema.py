"""Immutable service storage schema helpers."""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

from storage_guardian.types import StoreConfig

_SAFE_SCHEMA_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_PENDING_STORE_LAYOUT = "pending_external/services/{service}/stores/{store}"


def storage_schema(config_root: dict[str, Any]) -> dict[str, Any]:
    schema = config_root.get("storage_schema")
    if not isinstance(schema, dict):
        raise _config_error("storage_schema must be declared as an immutable service storage contract")
    return schema


def storage_schema_hash(schema: dict[str, Any]) -> str:
    payload = deepcopy(schema)
    payload.pop("schema_hash", None)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def storage_schema_payload(config_root: dict[str, Any]) -> dict[str, Any]:
    schema = deepcopy(storage_schema(config_root))
    schema["computed_schema_hash"] = storage_schema_hash(schema)
    schema["locked"] = bool(schema.get("immutable", True)) and schema.get("schema_hash") == schema["computed_schema_hash"]
    return schema


def validate_storage_schema(config_root: dict[str, Any], stores: list[StoreConfig]) -> None:
    schema = storage_schema(config_root)
    version = schema.get("version")
    if not isinstance(version, int) or version < 1:
        raise _config_error("storage_schema.version must be a positive integer")
    if schema.get("immutable") is not True:
        raise _config_error("storage_schema.immutable must be true")
    if schema.get("fallback_store_layout", _PENDING_STORE_LAYOUT) != _PENDING_STORE_LAYOUT:
        raise _config_error(f"storage_schema.fallback_store_layout must be {_PENDING_STORE_LAYOUT!r}")

    declared_hash = schema.get("schema_hash")
    if declared_hash:
        computed_hash = storage_schema_hash(schema)
        if declared_hash != computed_hash:
            raise _config_error(
                "storage_schema.schema_hash does not match the immutable schema "
                f"(expected {computed_hash}, found {declared_hash})"
            )

    strict_coverage = bool(schema.get("strict_store_coverage", True))
    schema_index = schema_store_index(config_root)
    configured = {store.name: store for store in stores}
    if strict_coverage:
        missing_from_schema = sorted(set(configured) - set(schema_index))
        missing_from_config = sorted(set(schema_index) - set(configured))
        if missing_from_schema:
            raise _config_error("storage_schema missing configured stores: " + ", ".join(missing_from_schema))
        if missing_from_config:
            raise _config_error("storage_schema references unknown stores: " + ", ".join(missing_from_config))

    for store in stores:
        entry = schema_index.get(store.name)
        if entry is None:
            continue
        _validate_store_field(store, entry, "mode", store.mode)
        _validate_store_field(store, entry, "policy", store.policy)
        _validate_store_field(store, entry, "placement", store.placement)
        if "enabled" in entry and bool(entry["enabled"]) != store.enabled:
            raise _config_error(f"store {store.name!r} enabled value does not match storage_schema")


def schema_store_index(config_root: dict[str, Any]) -> dict[str, dict[str, Any]]:
    schema = storage_schema(config_root)
    services = schema.get("services")
    if not isinstance(services, dict) or not services:
        raise _config_error("storage_schema.services must be a non-empty mapping")

    index: dict[str, dict[str, Any]] = {}
    for service_name, service_cfg in services.items():
        _validate_schema_id("service", service_name)
        if not isinstance(service_cfg, dict):
            raise _config_error(f"storage_schema.services.{service_name} must be a mapping")
        stores = service_cfg.get("stores", {})
        if stores is None:
            stores = {}
        if not isinstance(stores, dict):
            raise _config_error(f"storage_schema.services.{service_name}.stores must be a mapping")
        for store_name, store_cfg in stores.items():
            _validate_schema_id("store", store_name)
            if store_name in index:
                raise _config_error(f"store {store_name!r} appears in more than one storage_schema service")
            if not isinstance(store_cfg, dict):
                raise _config_error(f"storage_schema store {store_name!r} must be a mapping")
            index[str(store_name)] = dict(store_cfg) | {
                "service": str(service_name),
                "service_owner": str(service_cfg.get("owner", service_name)),
            }
    return index


def service_for_store_name(config_root: dict[str, Any], store_name: str) -> str | None:
    try:
        entry = schema_store_index(config_root).get(store_name)
    except ValueError:
        return None
    return str(entry["service"]) if entry else None


def service_schema_roots(config_root: dict[str, Any]) -> list[dict[str, str]]:
    schema = storage_schema(config_root)
    services = schema.get("services", {})
    roots: list[dict[str, str]] = []
    for service_name, service_cfg in services.items():
        if not isinstance(service_cfg, dict):
            continue
        declared_roots = service_cfg.get("roots", {})
        if not isinstance(declared_roots, dict):
            continue
        for root_name, root_cfg in declared_roots.items():
            if not isinstance(root_cfg, dict):
                continue
            roots.append(
                {
                    "service": str(service_name),
                    "name": str(root_name),
                    "role": str(root_cfg.get("role", root_name)),
                    "relative_path": str(root_cfg.get("relative_path", "")),
                    "env_key": str(root_cfg.get("env_key", "")),
                }
            )
    return roots


def schema_pending_store_root(config_root: dict[str, Any], pending_root: Path, store: StoreConfig) -> Path:
    layout = str(storage_schema(config_root).get("fallback_store_layout", _PENDING_STORE_LAYOUT))
    service = store.service or service_for_store_name(config_root, store.name) or store.owner
    relative = layout.removeprefix("pending_external/").format(service=service, store=store.name)
    return pending_root / relative


def schema_services(config_root: dict[str, Any]) -> list[str]:
    return sorted(str(service) for service in storage_schema(config_root).get("services", {}))


def _validate_store_field(store: StoreConfig, schema_entry: dict[str, Any], key: str, actual: str) -> None:
    expected = schema_entry.get(key)
    if expected is not None and str(expected) != actual:
        raise _config_error(
            f"store {store.name!r} {key}={actual!r} does not match storage_schema {key}={expected!r}"
        )


def _validate_schema_id(kind: str, value: str) -> None:
    if not _SAFE_SCHEMA_ID.match(str(value)):
        raise _config_error(f"storage_schema {kind} id is invalid: {value!r}")


def _config_error(message: str) -> ValueError:
    try:
        from storage_guardian.config import ConfigError

        return ConfigError(message)
    except ImportError:
        return ValueError(message)
