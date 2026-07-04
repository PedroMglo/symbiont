"""Configuration loading and validation."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from storage_guardian.storage_schema import service_for_store_name, validate_storage_schema
from storage_guardian.types import PolicyConfig, StoreConfig

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class ConfigError(ValueError):
    """Raised when the storage_guardian configuration is invalid."""


@dataclass(frozen=True)
class StorageGuardianConfig:
    path: Path
    raw: dict[str, Any]

    @property
    def root(self) -> dict[str, Any]:
        return self.raw["storage_guardian"]

    @property
    def enabled(self) -> bool:
        return bool(self.root.get("enabled", True))

    @property
    def project_root(self) -> Path:
        return Path(self.root["identity"]["project_root"])

    @property
    def project_name(self) -> str:
        return str(self.root["identity"].get("project_name", self.project_root.name))

    @property
    def data_root(self) -> Path:
        return Path(self.root["placement"]["local"]["data_root"])

    @property
    def local_archive_root(self) -> Path:
        return Path(self.root["placement"]["local"]["archive_root"])

    @property
    def restore_root(self) -> Path:
        return Path(self.root["safety"]["restore_root"])

    @property
    def hot_until_days(self) -> int:
        return int(self.root["lifecycle"]["hot_until_days"])

    @property
    def cold_after_days(self) -> int:
        return int(self.root["lifecycle"]["cold_after_days"])

    @property
    def stores(self) -> list[StoreConfig]:
        stores: list[StoreConfig] = []
        for item in self.root.get("stores", []):
            stores.append(
                StoreConfig(
                    name=str(item["name"]),
                    enabled=bool(item.get("enabled", True)),
                    path=Path(item["path"]),
                    owner=str(item.get("owner", "unknown")),
                    type=str(item.get("type", "mixed")),
                    mode=str(item.get("mode", "managed")),
                    policy=str(item.get("policy", "mixed_policy")),
                    placement=str(item.get("placement", "inherit")),
                    service=str(item.get("service") or service_for_store_name(self.root, str(item["name"])) or item.get("owner", "unknown")),
                )
            )
        return stores

    @property
    def policies(self) -> dict[str, PolicyConfig]:
        return {name: PolicyConfig(name=name, values=dict(values)) for name, values in self.root.get("policies", {}).items()}

    def policy_for(self, store: StoreConfig) -> PolicyConfig:
        policies = self.policies
        if store.policy not in policies:
            raise ConfigError(f"store {store.name!r} references missing policy {store.policy!r}")
        policy = policies[store.policy]
        parent = policy.get("inherit_from")
        if parent:
            if parent not in policies:
                raise ConfigError(f"policy {store.policy!r} inherits missing policy {parent!r}")
            merged = dict(policies[parent].values)
            merged.update(policy.values)
            return PolicyConfig(name=store.policy, values=merged)
        return policy


def default_config_path(project_root: Path | None = None) -> Path:
    root = project_root or Path.cwd()
    return root / "config" / "storage_guardian.yaml"


def load_config(path: str | Path | None = None) -> StorageGuardianConfig:
    cfg_path = Path(path or os.getenv("STORAGE_GUARDIAN_CONFIG") or default_config_path()).resolve()
    raw = _load_mapping(cfg_path)
    if "storage_guardian" not in raw:
        raise ConfigError("config must contain a storage_guardian root key")

    process_env = dict(os.environ)
    bootstrap_env = dict(process_env)
    bootstrap_env.setdefault("HOME", str(Path.home()))
    bootstrap_env.setdefault("PROJECT_ROOT", str(cfg_path.parent.parent))
    bootstrap_env.setdefault("AI_LOCAL_PROJECT_ROOT", bootstrap_env["PROJECT_ROOT"])
    project_root_raw = str(raw["storage_guardian"]["identity"]["project_root"])
    project_root = Path(_expand_string_recursive(project_root_raw, bootstrap_env)).expanduser().resolve()
    env = _load_env_files(project_root)
    env.setdefault("PROJECT_ROOT", str(project_root))
    env.setdefault("AI_LOCAL_PROJECT_ROOT", str(project_root))
    env.setdefault("HOME", str(Path.home()))
    project_id = _safe_project_id(str(raw["storage_guardian"]["identity"].get("project_name") or project_root.name))
    storage_profile = env.get("AI_LOCAL_STORAGE_PROFILE") or env.get("AI_LOCAL_STORAGE_MODE") or "user_local"
    env.setdefault("AI_LOCAL_STORAGE_PROFILE", storage_profile)
    if storage_profile == "project_local":
        default_storage_root = str(project_root / ".local")
    else:
        default_storage_root = str(_xdg_data_home(env) / "ai-local" / project_id)
        env.setdefault("STORAGE_GUARDIAN_STATE_DIR", str(_xdg_state_home(env) / "ai-local" / project_id / "storage_guardian"))
        env.setdefault("STORAGE_GUARDIAN_CACHE_DIR", str(_xdg_cache_home(env) / "ai-local" / project_id / "storage_guardian"))
    runtime_storage_root = _runtime_storage_root(process_env)
    storage_root = runtime_storage_root or env.get("AI_STORAGE_EXTERNAL_ROOT") or env.get("AI_LOCAL_STORAGE_ROOT") or default_storage_root
    if runtime_storage_root:
        env["AI_LOCAL_STORAGE_ROOT"] = storage_root
    else:
        env.setdefault("AI_LOCAL_STORAGE_ROOT", storage_root)
    env.setdefault("AI_STORAGE_EXTERNAL_ROOT", storage_root)
    env.setdefault("AI_LOCAL_LOGS_ROOT", str(Path(storage_root) / "logs"))
    guardian_root = (
        env.get("AI_STORAGE_GUARDIAN_ROOT")
        or env.get("AI_STORAGE_CONTAINER_BIND_ROOT")
        or storage_root
    )
    env.setdefault("AI_STORAGE_GUARDIAN_ROOT", guardian_root)
    if "AI_STORAGE_GUARDIAN_EXTERNAL_ROOT" not in env:
        mode = env.get("AI_LOCAL_STORAGE_MODE", "")
        env["AI_STORAGE_GUARDIAN_EXTERNAL_ROOT"] = guardian_root if mode == "external" and env.get("AI_STORAGE_EXTERNAL_ROOT") else ""
    _set_storage_dir_default(env, process_env, "RAG_DATA_DIR", str(Path(storage_root) / "data" / "rag"))
    _set_storage_dir_default(env, process_env, "GRAPHIFY_OUT_DIR", str(Path(storage_root) / "data" / "graphify"))
    _set_storage_dir_default(
        env,
        process_env,
        "AUDIO_TRANSCRIBE_DATA_DIR",
        str(Path(storage_root) / "data" / "audio"),
    )
    _set_storage_dir_default(env, process_env, "EXTRATOR_DATA_DIR", str(Path(storage_root) / "data" / "extrator"))
    _set_storage_dir_default(env, process_env, "LLM_MODELS_DIR", str(Path(storage_root) / "data" / "models" / "gguf"))
    _set_storage_dir_default(env, process_env, "OLLAMA_MODELS", str(Path(storage_root) / "data" / "models" / "ollama"))
    _set_storage_dir_default(env, process_env, "HF_CACHE_DIR", str(Path(storage_root) / "data" / "cache" / "hf"))
    _set_storage_dir_default(env, process_env, "STORAGE_GUARDIAN_DATA_DIR", str(Path(storage_root) / "data" / "storage_guardian"))
    _set_storage_dir_default(
        env,
        process_env,
        "STORAGE_GUARDIAN_STATE_DIR",
        str(Path(storage_root) / "state" / "storage_guardian"),
    )
    _set_storage_dir_default(
        env,
        process_env,
        "STORAGE_GUARDIAN_CACHE_DIR",
        str(Path(storage_root) / "cache" / "storage_guardian"),
    )
    _set_storage_dir_default(
        env,
        process_env,
        "AI_LOCAL_PROJECT_SCRATCH_ROOT",
        str(Path(env["STORAGE_GUARDIAN_DATA_DIR"]) / "scratch" / "project"),
    )
    expanded = _expand_value(raw, env)
    # The storage schema is an architectural contract, not a machine-root
    # fingerprint. Keep it in its declarative form so schema_hash remains stable
    # across host paths, XDG roots, and container bind layouts.
    expanded["storage_guardian"]["storage_schema"] = raw["storage_guardian"]["storage_schema"]
    config = StorageGuardianConfig(path=cfg_path, raw=expanded)
    validate_config(config)
    return config


def validate_config(config: StorageGuardianConfig) -> None:
    if config.hot_until_days < 0:
        raise ConfigError("lifecycle.hot_until_days must be >= 0")
    if config.cold_after_days <= config.hot_until_days:
        raise ConfigError("lifecycle.cold_after_days must be greater than hot_until_days")
    if not config.project_root.is_absolute():
        raise ConfigError("identity.project_root must be absolute")
    if not config.data_root.is_absolute():
        raise ConfigError("placement.local.data_root must be absolute")
    names: set[str] = set()
    for store in config.stores:
        if store.name in names:
            raise ConfigError(f"duplicate store name: {store.name}")
        names.add(store.name)
        if not store.path.is_absolute():
            raise ConfigError(f"store {store.name} path must be absolute")
        config.policy_for(store)
    validate_storage_schema(config.root, config.stores)


def _load_mapping(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    try:
        import yaml

        value = yaml.safe_load(text) or {}
    except ModuleNotFoundError:
        value = json.loads(text)
    if not isinstance(value, dict):
        raise ConfigError("config root must be a mapping")
    return value


def _load_env_files(project_root: Path) -> dict[str, str]:
    env = dict(os.environ)
    for env_path in [project_root / ".env", project_root / ".env.storage.generated"]:
        if not env_path.exists():
            continue
        for line in env_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            env.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    return env


def _runtime_storage_root(process_env: dict[str, str]) -> str:
    for key in (
        "AI_STORAGE_EXTERNAL_ROOT",
        "AI_LOCAL_STORAGE_ROOT",
        "AI_STORAGE_GUARDIAN_ROOT",
        "AI_STORAGE_CONTAINER_BIND_ROOT",
    ):
        value = process_env.get(key)
        if value:
            return value
    return ""


def _set_storage_dir_default(env: dict[str, str], process_env: dict[str, str], key: str, value: str) -> None:
    if _runtime_storage_root(process_env) and key not in process_env:
        env[key] = value
        return
    env.setdefault(key, value)


def _expand_string(value: str, env: dict[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        default = match.group(2)
        found = env.get(key)
        if found is not None and found != "":
            return found
        return default or ""

    return _ENV_PATTERN.sub(replace, os.path.expanduser(value))


def _expand_string_recursive(value: str, env: dict[str, str]) -> str:
    expanded = _expand_string(value, env)
    previous = None
    while previous != expanded and "${" in expanded:
        previous = expanded
        expanded = _expand_string(expanded, env)
    return expanded


def _expand_value(value: Any, env: dict[str, str]) -> Any:
    if isinstance(value, str):
        return _expand_string_recursive(value, env)
    if isinstance(value, list):
        return [_expand_value(item, env) for item in value]
    if isinstance(value, dict):
        return {key: _expand_value(item, env) for key, item in value.items()}
    return value


def _xdg_data_home(env: dict[str, str]) -> Path:
    return Path(env.get("XDG_DATA_HOME") or Path(env.get("HOME", str(Path.home()))) / ".local" / "share").expanduser()


def _xdg_state_home(env: dict[str, str]) -> Path:
    return Path(env.get("XDG_STATE_HOME") or Path(env.get("HOME", str(Path.home()))) / ".local" / "state").expanduser()


def _xdg_cache_home(env: dict[str, str]) -> Path:
    return Path(env.get("XDG_CACHE_HOME") or Path(env.get("HOME", str(Path.home()))) / ".cache").expanduser()


def _safe_project_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-") or "default"
