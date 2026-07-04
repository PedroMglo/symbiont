"""Declarative terminal alias command registry."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

MANIFEST_PATH = Path(__file__).with_name("command_registry.toml")
ALLOWED_TARGET_TYPES = frozenset({"api", "make", "capability"})
_SECRET_KEY_PARTS = ("api_key", "apikey", "token", "secret", "password", "authorization")
_LOCAL_PATH_PREFIXES = ("/home/", "/mnt/", "/media/", "/run/user/", "/tmp/")


def _string(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _string_list(value: Any, *, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise ValueError(f"{field} must be a list of non-empty strings")
    return tuple(item.strip() for item in value)


def _target(value: Any, *, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be a table")
    target_type = _string(value.get("type"), field=f"{field}.type")
    if target_type not in ALLOWED_TARGET_TYPES:
        raise ValueError(f"{field}.type must be one of {sorted(ALLOWED_TARGET_TYPES)}")
    _assert_no_private_values(value, field=field)
    return dict(value)


def _assert_no_private_values(value: Any, *, field: str) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key).lower()
            if any(part in key_text for part in _SECRET_KEY_PARTS):
                raise ValueError(f"{field}.{key} must not contain secret material")
            _assert_no_private_values(item, field=f"{field}.{key}")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _assert_no_private_values(item, field=f"{field}[{index}]")
        return
    if isinstance(value, str) and value.startswith(_LOCAL_PATH_PREFIXES):
        raise ValueError(f"{field} must not contain host-local paths")


def _normalize_command_name(value: str) -> str:
    value = value.strip()
    return value if value.startswith("/") else f"/{value}"


@dataclass(frozen=True)
class CommandRegistryEntry:
    """One declarative slash command entry for the terminal alias."""

    name: str
    owner: str
    description: str
    target: dict[str, Any]
    policy_action: str
    read_only: bool
    capability_id: str | None
    aliases: tuple[str, ...]
    evidence_types: tuple[str, ...]
    tags: tuple[str, ...]

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> "CommandRegistryEntry":
        name = _normalize_command_name(_string(raw.get("name"), field="commands.name"))
        aliases = tuple(_normalize_command_name(alias) for alias in _string_list(raw.get("aliases"), field=f"{name}.aliases"))
        read_only = raw.get("read_only", True)
        if not isinstance(read_only, bool):
            raise ValueError(f"{name}.read_only must be a boolean")
        capability_id = raw.get("capability_id")
        if capability_id is not None:
            capability_id = _string(capability_id, field=f"{name}.capability_id")
        return cls(
            name=name,
            owner=_string(raw.get("owner"), field=f"{name}.owner"),
            description=_string(raw.get("description"), field=f"{name}.description"),
            target=_target(raw.get("target"), field=f"{name}.target"),
            policy_action=_string(raw.get("policy_action"), field=f"{name}.policy_action"),
            read_only=read_only,
            capability_id=capability_id,
            aliases=aliases,
            evidence_types=_string_list(raw.get("evidence_types"), field=f"{name}.evidence_types"),
            tags=_string_list(raw.get("tags"), field=f"{name}.tags"),
        )

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "owner": self.owner,
            "description": self.description,
            "target": dict(self.target),
            "policy_action": self.policy_action,
            "read_only": self.read_only,
            "capability_id": self.capability_id,
            "aliases": list(self.aliases),
            "evidence_types": list(self.evidence_types),
            "tags": list(self.tags),
        }


@cache
def command_registry_entries(path: Path = MANIFEST_PATH) -> tuple[CommandRegistryEntry, ...]:
    """Load declarative command registry entries."""

    data = tomllib.loads(path.read_text(encoding="utf-8"))
    raw_items = data.get("commands", [])
    if not isinstance(raw_items, list):
        raise ValueError("commands must be a list")
    entries = tuple(CommandRegistryEntry.from_mapping(item) for item in raw_items if isinstance(item, dict))
    seen: set[str] = set()
    for entry in entries:
        names = (entry.name, *entry.aliases)
        for name in names:
            if name in seen:
                raise ValueError(f"duplicate command registry name or alias: {name}")
            seen.add(name)
    return entries


def command_registry_entry(name: str) -> CommandRegistryEntry | None:
    """Return one registry entry by slash name or alias."""

    normalized = _normalize_command_name(name)
    for entry in command_registry_entries():
        if entry.name == normalized or normalized in entry.aliases:
            return entry
    return None


def match_command_registry_query(query: str) -> CommandRegistryEntry | None:
    """Return a registry entry when the query begins with a known slash command."""

    first_token = (query or "").strip().split(maxsplit=1)[0] if (query or "").strip() else ""
    if not first_token.startswith("/"):
        return None
    return command_registry_entry(first_token)
