"""Configuration loader for reasoning_and_response."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic_settings import BaseSettings
from sharedai.llm.settings import CommonLLMSettings, llm_settings_data

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore[no-redef]


def _load_toml() -> dict:
    candidates = [
        Path(os.environ.get("REASONING_AND_RESPONSE_CONFIG", "")),
        Path.cwd() / "config.toml",
        Path(__file__).resolve().parent.parent / "config.toml",
    ]
    for path in candidates:
        if path.is_file():
            return tomllib.loads(path.read_text(encoding="utf-8"))
    return {}


_TOML = _load_toml()


class LLMSettings(CommonLLMSettings):
    temperature: float = 0.3
    max_tokens: int = 768
    timeout_seconds: float = 20.0

    model_config = {"env_prefix": "REASONING_AND_RESPONSE_LLM_"}


class SynthesisSettings(BaseSettings):
    progressive_refinement: bool = True
    max_source_chars: int = 3000

    model_config = {"env_prefix": "REASONING_AND_RESPONSE_SYNTHESIS_"}


class ResponseSettings(BaseSettings):
    max_history_messages: int = 4
    max_context_chars: int = 6000

    model_config = {"env_prefix": "REASONING_AND_RESPONSE_RESPONSE_"}


class DecompositionSettings(BaseSettings):
    max_subtasks: int = 5
    default_budget_tokens: int = 2000
    available_capabilities: list[str] = [
        "research",
        "local_evidence",
        "system_info",
        "personal",
        "synthesis",
        "critique",
        "planning",
    ]

    model_config = {"env_prefix": "REASONING_AND_RESPONSE_DECOMPOSITION_"}


class EvaluationSettings(BaseSettings):
    confidence_threshold: float = 0.7
    max_eval_chars: int = 2000
    enable_heuristics: bool = True
    heuristic_overlap_ratio: float = 0.3
    heuristic_min_length: int = 500

    model_config = {"env_prefix": "REASONING_AND_RESPONSE_EVALUATION_"}


class ClassificationSettings(BaseSettings):
    available_agents: list[str] = ["research", "code", "system", "personal"]
    max_agents_per_query: int = 3

    model_config = {"env_prefix": "REASONING_AND_RESPONSE_CLASSIFICATION_"}


class ServerSettings(BaseSettings):
    host: str = "0.0.0.0"
    port: int = 8000
    workers: int = 1

    model_config = {"env_prefix": "REASONING_AND_RESPONSE_SERVER_"}


class SecuritySettings(BaseSettings):
    api_key: str = ""

    model_config = {"env_prefix": "REASONING_AND_RESPONSE_SECURITY_"}


class Settings:
    def __init__(self) -> None:
        llm_data = llm_settings_data(_TOML.get("llm", {}), env_prefix="REASONING_AND_RESPONSE")
        self.llm = LLMSettings(**llm_data)
        self.synthesis = SynthesisSettings(
            **{k: v for k, v in _TOML.get("synthesis", {}).items() if v is not None}
        )
        self.response = ResponseSettings(
            **{k: v for k, v in _TOML.get("response", {}).items() if v is not None}
        )
        self.decomposition = DecompositionSettings(
            **{k: v for k, v in _TOML.get("decomposition", {}).items() if v is not None}
        )
        self.evaluation = EvaluationSettings(
            **{k: v for k, v in _TOML.get("evaluation", {}).items() if v is not None}
        )
        self.classification = ClassificationSettings(
            **{k: v for k, v in _TOML.get("classification", {}).items() if v is not None}
        )
        self.server = ServerSettings(
            **{k: v for k, v in _TOML.get("server", {}).items() if v is not None}
        )
        self.security = SecuritySettings(
            **{k: v for k, v in _TOML.get("security", {}).items() if v is not None}
        )


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
