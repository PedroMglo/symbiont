"""Typed Config Center projection for the stateless terminal client."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .orc_catalogs import ORC_SETTINGS_PATH, OrchestratorSettingsError, load_orchestrator_setting
from .schema import ConfigError

CLI_RUNTIME_CONTRACT = "ai-local.cli-runtime.v2"
CLI_SERVER_CONFIG_PATH = ORC_SETTINGS_PATH


def resolve_cli_runtime(
    path: Path = CLI_SERVER_CONFIG_PATH,
) -> dict[str, Any]:
    """Resolve the client continuity switch without importing orchestrator internals."""

    try:
        payload = load_orchestrator_setting("server", path=path)
    except OrchestratorSettingsError as exc:
        raise ConfigError(f"could not load terminal runtime config {path}: {exc}") from exc
    session = payload.get("session") if isinstance(payload, Mapping) else None
    if not isinstance(session, Mapping):
        raise ConfigError("orchestrator server settings must contain a session mapping")
    enabled = session.get("cli_default_session")
    if not isinstance(enabled, bool):
        raise ConfigError("session.cli_default_session must be a boolean")
    if "cli_session_state_file" in session:
        raise ConfigError("session.cli_session_state_file is retired; the terminal client is stateless")
    return {
        "contract": CLI_RUNTIME_CONTRACT,
        "default_session": enabled,
        "identity_mode": "canonical_cwd_sha256",
        "persistence_owner": "orchestrator_postgres",
        "source_path": str(path),
    }


def cli_runtime_env_values(runtime: Mapping[str, Any]) -> dict[str, str]:
    if runtime.get("contract") != CLI_RUNTIME_CONTRACT:
        raise ConfigError("cli_runtime contract is missing or unsupported")
    enabled = runtime.get("default_session")
    if not isinstance(enabled, bool):
        raise ConfigError("cli_runtime.default_session must be a boolean")
    if runtime.get("identity_mode") != "canonical_cwd_sha256":
        raise ConfigError("cli_runtime.identity_mode must be canonical_cwd_sha256")
    if runtime.get("persistence_owner") != "orchestrator_postgres":
        raise ConfigError("cli_runtime.persistence_owner must be orchestrator_postgres")
    return {
        "ORC_SESSION_CLI_DEFAULT_SESSION": "true" if enabled else "false",
    }
