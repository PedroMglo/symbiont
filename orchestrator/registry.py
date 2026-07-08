"""Central model registry — loads models.json and provides typed access.

Single source of truth for all model definitions, agent LLM configurations,
system prompts, backend configuration, and parameters across both the
symbiont and RAG projects.

v2: Agent configs are defined under orchestration.agents.{agent_name} and
    are discovered dynamically — any new agent added to the JSON is
    automatically available without code changes.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

log = logging.getLogger(__name__)


def _service_env_key(service: str) -> str:
    return service.strip().replace("-", "_").upper()


def _first_env(*names: str) -> str:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value.rstrip("/")
    return ""


def _openai_compatible_url(url: str) -> str:
    clean = url.rstrip("/")
    return clean if clean.endswith("/v1") else f"{clean}/v1"


def _require_https_url(url: str, field_name: str) -> str:
    clean = (url or "").rstrip("/")
    parts = urlsplit(clean)
    if parts.scheme == "http":
        raise ValueError(f"{field_name} uses forbidden plain HTTP; configure an HTTPS URL")
    if parts.scheme != "https" or not parts.netloc:
        raise ValueError(f"{field_name} must be an absolute HTTPS URL")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError(f"{field_name} must not contain credentials, query, or fragment")
    return clean


def _resolve_backend_url(backend: dict[str, Any], default_backend: dict[str, Any] | None = None) -> str:
    """Resolve backend URL from explicit config, generated env, then safe local defaults."""
    default_backend = default_backend or {}
    backend_type = str(backend.get("type") or default_backend.get("type") or "ollama")
    service = str(backend.get("service") or default_backend.get("service") or "").strip()

    explicit_url = str(backend.get("url") or default_backend.get("url") or "").strip()
    if explicit_url:
        secure_url = _require_https_url(explicit_url, "model registry backend.url")
        return _openai_compatible_url(secure_url) if backend_type == "openai" else secure_url

    if backend_type == "ollama":
        url = _first_env("ORC_OLLAMA_BASE_URL", "OLLAMA_BASE_URL") or "https://host.docker.internal:11434"
        return _require_https_url(url, "ollama backend URL")

    if backend_type == "openai":
        service_key = _service_env_key(service)
        env_url = _first_env(
            f"ORC_SERVICES_{service_key}_URL",
            f"{service_key}_URL",
        )
        if not env_url:
            if service_key == "LLAMA_CPP_FAST":
                env_url = "https://llama-cpp-fast:8080"
            elif service_key == "LLAMA_CPP_AUX":
                env_url = "https://llama-cpp-aux:8080"
            else:
                env_url = "https://vllm:8000"
        return _openai_compatible_url(_require_https_url(env_url, f"{service_key} backend URL"))

    return ""

# ---------------------------------------------------------------------------
# Registry path resolution
# ---------------------------------------------------------------------------

def _find_registry_path() -> Path:
    """Locate the symbiont model registry."""
    env_path = os.environ.get("AI_MODELS_REGISTRY")
    if env_path:
        return Path(env_path).expanduser()

    project_root = Path(__file__).resolve().parent.parent

    # Root central configuration registry.
    config_path = project_root / "config" / "models" / "orc.config.json"
    if config_path.exists():
        return config_path

    # Historical project-root registry file, kept only as a locator for existing
    # installations that have not moved the file into config/models yet.
    if (project_root / "models.json").exists():
        return project_root / "models.json"

    # Default path
    return Path.home() / "ai-local" / "models.json"


# ---------------------------------------------------------------------------
# Agent config dataclass
# ---------------------------------------------------------------------------

@dataclass
class AgentLLMConfig:
    """LLM configuration for a single agent — extracted from models.json."""

    agent_name: str
    model: str
    backend_type: str = "ollama"
    backend_url: str = ""
    system_prompt: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)
    description: str = ""
    # Extra prompts (e.g. polish_prompt for reasoning_and_response)
    extra_prompts: dict[str, str] = field(default_factory=dict)
    # Full backend config (for non-standard backends like whisper)
    backend_raw: dict[str, Any] = field(default_factory=dict)

    @property
    def temperature(self) -> float:
        return self.parameters.get("temperature", 0.7)

    @property
    def max_tokens(self) -> int:
        return self.parameters.get("max_tokens", 2048)

    @property
    def timeout(self) -> float:
        return self.parameters.get("timeout", 15.0)

    def to_inject_payload(self) -> dict[str, Any]:
        """Produce a payload suitable for injecting into agent HTTP requests."""
        return {
            "llm_config": {
                "model": self.model,
                "backend_type": self.backend_type,
                "backend_url": self.backend_url,
                "system_prompt": self.system_prompt,
                "parameters": self.parameters,
                "extra_prompts": self.extra_prompts,
            }
        }


# ---------------------------------------------------------------------------
# Registry class
# ---------------------------------------------------------------------------

class ModelRegistry:
    """Parsed model registry with lookup helpers.

    v2 structure:
        orchestration.settings — terminal alias, default backend
        orchestration.routing.profiles — internal intent×complexity model map
        orchestration.agents.{name} — per-agent LLM config (dynamic discovery)
        rag.roles.{name} — RAG pipeline models
    """

    def __init__(self, data: dict[str, Any], mtime: float) -> None:
        self._data = data
        self._mtime = mtime
        self._agent_configs: dict[str, AgentLLMConfig] = {}
        self._profile_models: dict[str, str] = {}
        self._model_to_role: dict[str, dict[str, Any]] = {}
        self._build_indexes()

    def _build_indexes(self) -> None:
        """Build internal lookup indexes from the loaded JSON."""
        orch = self._data.get("orchestration", {})

        # Routing profiles (internal model selection)
        profiles = orch.get("routing", {}).get("profiles", {})
        for key, profile in profiles.items():
            model = profile.get("model", "")
            if model:
                self._profile_models[key] = model

        # Agent configs (dynamic discovery)
        agents = orch.get("agents", {})
        default_backend = orch.get("settings", {}).get("default_backend", {})
        for agent_name, agent_cfg in agents.items():
            backend_override = agent_cfg.get("backend")
            backend = backend_override or default_backend
            default_for_url = default_backend if backend_override is None else {}
            backend_url = _resolve_backend_url(backend, default_for_url)
            # Collect extra prompts (any key ending in _prompt that isn't system_prompt)
            extra_prompts = {}
            for k, v in agent_cfg.items():
                if k.endswith("_prompt") and k != "system_prompt" and isinstance(v, str):
                    extra_prompts[k] = v

            self._agent_configs[agent_name] = AgentLLMConfig(
                agent_name=agent_name,
                model=agent_cfg.get("model", ""),
                backend_type=backend.get("type", "ollama"),
                backend_url=backend_url,
                system_prompt=agent_cfg.get("system_prompt", ""),
                parameters=agent_cfg.get("parameters", {}),
                description=agent_cfg.get("description", ""),
                extra_prompts=extra_prompts,
                backend_raw={**backend, "url": backend_url},
            )

        # RAG roles — index by model name
        for section_key in ("rag",):
            section = self._data.get(section_key, {})
            roles = section.get("roles", {})
            for role_name, role_cfg in roles.items():
                model = role_cfg.get("model", "")
                self._model_to_role[model] = role_cfg

    # --- Agent config access (dynamic — reads any agent defined in JSON) ---

    def get_agent_config(self, agent_name: str) -> AgentLLMConfig | None:
        """Get LLM config for a specific agent. Returns None if not defined."""
        return self._agent_configs.get(agent_name)

    def get_all_agent_configs(self) -> dict[str, AgentLLMConfig]:
        """Get all agent configs — for discovery and health checks."""
        return dict(self._agent_configs)

    def list_agent_names(self) -> list[str]:
        """List all configured agent names."""
        return list(self._agent_configs.keys())

    # --- Routing profile access (internal model selection) ---

    def get_model_for_profile(self, profile_key: str) -> str:
        """Resolve a routing profile key (fast/default/code/deep) to model name."""
        return self._profile_models.get(profile_key, self._profile_models.get("default", ""))

    def get_model_for_key(self, key: str) -> str:
        """Get model name for a given key (routing profile or RAG role).

        Lookup order:
          1. orchestration.routing.profiles.{key}.model
          2. rag.roles.{key}.model
          3. Empty string
        """
        if key in self._profile_models:
            return self._profile_models[key]
        rag_role = self._data.get("rag", {}).get("roles", {}).get(key)
        if rag_role:
            return rag_role.get("model", "")
        return ""

    def get_default_chat_model(self) -> str:
        """Get the default chat model from routing profiles."""
        return self._profile_models.get("default", "")

    def is_rag_capable(self, model_name: str) -> bool:
        """Check if a model has RAG capability."""
        rag_capable = self._data.get("orchestration", {}).get("routing", {}).get("rag_capable_models", [])
        return model_name in rag_capable

    # --- Terminal alias ---

    def get_terminal_alias(self) -> str:
        """Get the configured terminal alias for the symbiont."""
        return (
            self._data.get("orchestration", {})
            .get("settings", {})
            .get("terminal_alias", "@")
        )

    # --- RAG role access ---

    def get_role(self, section: str, role_name: str) -> dict[str, Any] | None:
        """Get a specific role config by section and name."""
        return self._data.get(section, {}).get("roles", {}).get(role_name)

    def get_rag_role(self, role_name: str) -> dict[str, Any] | None:
        return self.get_role("rag", role_name)

    # --- Prompt access ---

    def get_prompt(self, key: str) -> str:
        """Get a RAG prompt template by key (e.g., 'rag_context_instruction')."""
        return self._data.get("rag", {}).get("prompts", {}).get(key, "")

    def get_context_instruction(self) -> str:
        """Get the routing context instruction for RAG-augmented responses."""
        return (
            self._data.get("orchestration", {})
            .get("routing", {})
            .get("context_instruction", "")
        )

    def get_system_prompt_for_agent(self, agent_name: str) -> str:
        """Get system prompt for a specific agent."""
        cfg = self.get_agent_config(agent_name)
        return cfg.system_prompt if cfg else ""

    # --- Default backend ---

    def get_default_backend(self) -> dict[str, Any]:
        """Get the default backend configuration."""
        backend = (
            self._data.get("orchestration", {})
            .get("settings", {})
            .get("default_backend", {"type": "ollama"})
        )
        return {**backend, "url": _resolve_backend_url(backend)}

    # --- Raw data ---

    @property
    def data(self) -> dict[str, Any]:
        return self._data

    @property
    def orchestration(self) -> dict[str, Any]:
        return self._data.get("orchestration", {})

    @property
    def rag(self) -> dict[str, Any]:
        return self._data.get("rag", {})

    @property
    def settings(self) -> dict[str, Any]:
        """Global runtime settings from the 'settings' section."""
        return self._data.get("settings", {})


# ---------------------------------------------------------------------------
# Singleton with hot-reload (mtime check)
# ---------------------------------------------------------------------------

_registry: ModelRegistry | None = None
_registry_path: Path | None = None


def get_registry() -> ModelRegistry:
    """Get the model registry singleton, auto-reloading if the file changed."""
    global _registry, _registry_path

    if _registry_path is None:
        _registry_path = _find_registry_path()

    if not _registry_path.exists():
        raise FileNotFoundError(
            f"Model registry not found at {_registry_path}. "
            f"All model configuration is centralized in models.json. "
            "Set AI_MODELS_REGISTRY or place the registry under config/models/orc.config.json."
        )

    current_mtime = _registry_path.stat().st_mtime
    if _registry is not None and current_mtime == _registry._mtime:
        return _registry

    # Load or reload
    with open(_registry_path) as f:
        data = json.load(f)

    _registry = ModelRegistry(data, current_mtime)
    log.info("Model registry loaded from %s (%d agents, %d routing profiles, %d rag roles)",
             _registry_path,
             len(data.get("orchestration", {}).get("agents", {})),
             len(data.get("orchestration", {}).get("routing", {}).get("profiles", {})),
             len(data.get("rag", {}).get("roles", {})))
    return _registry


def _reset_registry() -> None:
    """Reset singleton — for testing."""
    global _registry, _registry_path
    _registry = None
    _registry_path = None
