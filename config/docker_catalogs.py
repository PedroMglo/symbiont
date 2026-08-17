"""Typed access to the single Docker catalog contract owned by Config Center."""

from __future__ import annotations

import copy
import json
import math
import tomllib
from pathlib import Path
from typing import Any, Final

DOCKER_CATALOGS_CONTRACT: Final = "ai-local.docker-catalogs.v1"
DOCKER_CATALOGS_PATH: Final = Path(__file__).resolve().parent / "docker" / "catalogs.json"
DOCKER_CATALOG_NAMES: Final = frozenset(
    {
        "compose_projects",
        "image_build_catalog",
        "released_image_sources",
        "service_catalog",
        "volumes_catalog",
    }
)


class DockerCatalogError(ValueError):
    """Raised when the central Docker catalog contract is missing or malformed."""


def _mapping(value: Any, *, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise DockerCatalogError(f"{field} must be an object")
    return value


def _expand_service_catalog(source: dict[str, Any]) -> dict[str, Any]:
    catalog = copy.deepcopy(source)
    raw_host_capabilities = _mapping(
        catalog.get("host_capabilities", {}),
        field="service_catalog.host_capabilities",
    )
    ollama = _mapping(
        raw_host_capabilities.get("ollama"),
        field="service_catalog.host_capabilities.ollama",
    )
    if ollama.get("owner") != "infra/docker" or ollama.get("class") != "host-native":
        raise DockerCatalogError(
            "service_catalog.host_capabilities.ollama must remain the Stack-owned host capability"
        )
    catalog["host_capabilities"] = {"ollama": copy.deepcopy(ollama)}
    inherited = _mapping(catalog.pop("_service_defaults", {}), field="service_catalog._service_defaults")
    defaults = _mapping(catalog.get("defaults"), field="service_catalog.defaults")
    review_after = defaults.get("review_after")
    if not isinstance(review_after, str) or not review_after:
        raise DockerCatalogError("service_catalog.defaults.review_after must be a non-empty string")
    inherited = {"review_after": review_after, **inherited}
    services = _mapping(catalog.get("services"), field="service_catalog.services")
    catalog["services"] = {
        name: dict(sorted({**copy.deepcopy(inherited), **_mapping(policy, field=f"service_catalog.services.{name}")}.items()))
        for name, policy in services.items()
    }
    return catalog


def _expand_released_image_sources(source: dict[str, Any]) -> dict[str, Any]:
    catalog = copy.deepcopy(source)
    defaults = _mapping(catalog.pop("_identity_defaults", {}), field="released_image_sources._identity_defaults")
    required_defaults = {"env_prefix", "env_suffix", "namespace", "repository_prefix"}
    if set(defaults) != required_defaults or any(not isinstance(defaults[key], str) for key in required_defaults):
        raise DockerCatalogError("released_image_sources._identity_defaults must define exactly " + repr(sorted(required_defaults)))
    records = catalog.get("images")
    if not isinstance(records, list) or not records:
        raise DockerCatalogError("released_image_sources.images must be a non-empty array")
    expanded: list[dict[str, Any]] = []
    for index, raw in enumerate(records):
        record = _mapping(raw, field=f"released_image_sources.images[{index}]")
        name = record.get("name")
        if not isinstance(name, str) or not name:
            raise DockerCatalogError(f"released_image_sources.images[{index}].name must be non-empty")
        token = name.upper().replace("-", "_")
        expanded.append({
            "env_var": record.get("env_var", f'{defaults["env_prefix"]}{token}{defaults["env_suffix"]}'),
            "ghcr_repository": record.get("ghcr_repository", f'{defaults["namespace"]}/{defaults["repository_prefix"]}{name}'),
            "name": name,
            "repository_id": record.get("repository_id", name),
        })
    catalog["images"] = expanded
    return catalog


def _expand_volumes_catalog(source: dict[str, Any]) -> dict[str, Any]:
    catalog = copy.deepcopy(source)
    policy = _mapping(catalog.pop("_backup_required_by_kind", {}), field="volumes_catalog._backup_required_by_kind")
    if not policy or any(not isinstance(value, bool) for value in policy.values()):
        raise DockerCatalogError("volumes_catalog._backup_required_by_kind must map kinds to booleans")
    volumes = _mapping(catalog.get("volumes"), field="volumes_catalog.volumes")
    expanded: dict[str, dict[str, Any]] = {}
    for name, raw in volumes.items():
        volume = _mapping(raw, field=f"volumes_catalog.volumes.{name}")
        kind = volume.get("kind")
        if kind not in policy:
            raise DockerCatalogError(f"volumes_catalog.volumes.{name}.kind has no backup policy")
        backup_required = policy[kind]
        expanded[name] = dict(sorted({"backup_required": backup_required, **volume, "restore_test_required": backup_required}.items()))
    catalog["volumes"] = expanded
    return catalog


def _expand_catalog(name: str, source: dict[str, Any]) -> dict[str, Any]:
    if name == "service_catalog":
        return _expand_service_catalog(source)
    if name == "released_image_sources":
        return _expand_released_image_sources(source)
    if name == "volumes_catalog":
        return _expand_volumes_catalog(source)
    return copy.deepcopy(source)


def _read_payload(path: Path) -> dict[str, Any]:
    try:
        if path.suffix == ".toml":
            payload = tomllib.loads(path.read_text(encoding="utf-8"))
        else:
            payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
        raise DockerCatalogError(f"{path}: unable to load Docker catalog: {exc}") from exc
    if not isinstance(payload, dict):
        raise DockerCatalogError(f"{path}: Docker catalog root must be an object")
    return payload


def load_docker_catalog(name: str, *, path: Path | None = None) -> dict[str, Any]:
    if name not in DOCKER_CATALOG_NAMES:
        raise DockerCatalogError(f"unknown Docker catalog {name!r}")
    catalog_path = path or DOCKER_CATALOGS_PATH
    payload = _read_payload(catalog_path)
    if catalog_path.suffix == ".toml":
        return copy.deepcopy(payload)
    if set(payload) != {"contract", "catalogs"}:
        raise DockerCatalogError(f"{catalog_path}: fields must be exactly ['catalogs', 'contract']")
    if payload.get("contract") != DOCKER_CATALOGS_CONTRACT:
        raise DockerCatalogError(f"{catalog_path}: contract must be {DOCKER_CATALOGS_CONTRACT!r}")
    catalogs = payload.get("catalogs")
    if not isinstance(catalogs, dict) or set(catalogs) != DOCKER_CATALOG_NAMES:
        raise DockerCatalogError(f"{catalog_path}: catalogs must be exactly {sorted(DOCKER_CATALOG_NAMES)!r}")
    selected = catalogs.get(name)
    if not isinstance(selected, dict):
        raise DockerCatalogError(f"{catalog_path}: catalogs.{name} must be an object")
    return _expand_catalog(name, selected)


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise DockerCatalogError("non-finite values cannot be projected to TOML")
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list) and not any(isinstance(item, dict) for item in value):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise DockerCatalogError(f"unsupported TOML projection value type: {type(value).__name__}")


def _render_table(payload: dict[str, Any], prefix: tuple[str, ...], *, emit_header: bool = True) -> list[str]:
    lines: list[str] = []
    scalars = [(key, value) for key, value in payload.items() if not isinstance(value, dict) and not (isinstance(value, list) and any(isinstance(item, dict) for item in value))]
    children = [(key, value) for key, value in payload.items() if isinstance(value, dict)]
    arrays = [(key, value) for key, value in payload.items() if isinstance(value, list) and any(isinstance(item, dict) for item in value)]
    if prefix and emit_header:
        lines.append("[" + ".".join(prefix) + "]")
    lines.extend(f"{key} = {_toml_value(value)}" for key, value in scalars)
    for key, child in children:
        if lines:
            lines.append("")
        lines.extend(_render_table(child, (*prefix, key)))
    for key, items in arrays:
        for item in items:
            if not isinstance(item, dict):
                raise DockerCatalogError("mixed scalar/object TOML arrays are unsupported")
            if lines:
                lines.append("")
            lines.append("[[" + ".".join((*prefix, key)) + "]]" )
            lines.extend(_render_table(item, (*prefix, key), emit_header=False))
    return lines


def render_docker_catalog(name: str) -> str:
    return "\n".join([*_render_table(load_docker_catalog(name), ()), ""])


__all__ = [
    "DOCKER_CATALOGS_CONTRACT",
    "DOCKER_CATALOGS_PATH",
    "DOCKER_CATALOG_NAMES",
    "DockerCatalogError",
    "load_docker_catalog",
    "render_docker_catalog",
]
