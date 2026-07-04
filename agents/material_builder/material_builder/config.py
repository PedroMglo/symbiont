"""Configuration for the material_builder agent."""

from __future__ import annotations

import os
from pathlib import Path

from sharedai.llm.settings import CommonLLMSettings, llm_settings_data
from pydantic_settings import BaseSettings


def _read_secret_file(path: str) -> str:
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


class SecuritySettings(BaseSettings):
    api_key: str = ""

    model_config = {"env_prefix": "MATERIAL_BUILDER_SECURITY_"}


class LLMSettings(CommonLLMSettings):
    base_url: str = ""
    model: str = ""
    temperature: float = 0.2
    max_tokens: int = 4096
    timeout_seconds: float = 600.0
    no_progress_timeout_seconds: float = 120.0
    wall_budget_seconds: float = 1200.0
    contract_repair_attempts: int = 3
    lane: str = "default"

    model_config = {"env_prefix": "MATERIAL_BUILDER_LLM_"}

    @property
    def configured(self) -> bool:
        return bool(self.base_url.strip() and self.model.strip())

    @property
    def route(self) -> dict[str, object]:
        return {
            "lane": self.lane,
            "base_url": self.base_url,
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "timeout_seconds": self.timeout_seconds,
            "no_progress_timeout_seconds": self.no_progress_timeout_seconds,
            "wall_budget_seconds": self.wall_budget_seconds,
            "contract_repair_attempts": self.contract_repair_attempts,
            "configured": self.configured,
        }


def _lane_llm_settings(lane: str, *, fallback: LLMSettings) -> LLMSettings:
    prefix = f"MATERIAL_BUILDER_{lane.upper()}_LLM_"
    return LLMSettings(
        base_url=os.environ.get(f"{prefix}BASE_URL", fallback.base_url),
        model=os.environ.get(f"{prefix}MODEL", fallback.model),
        temperature=float(os.environ.get(f"{prefix}TEMPERATURE", fallback.temperature)),
        max_tokens=int(os.environ.get(f"{prefix}MAX_TOKENS", fallback.max_tokens)),
        timeout_seconds=float(os.environ.get(f"{prefix}TIMEOUT_SECONDS", fallback.timeout_seconds)),
        no_progress_timeout_seconds=float(
            os.environ.get(f"{prefix}NO_PROGRESS_TIMEOUT_SECONDS", fallback.no_progress_timeout_seconds)
        ),
        wall_budget_seconds=float(os.environ.get(f"{prefix}WALL_BUDGET_SECONDS", fallback.wall_budget_seconds)),
        contract_repair_attempts=int(
            os.environ.get(f"{prefix}CONTRACT_REPAIR_ATTEMPTS", fallback.contract_repair_attempts)
        ),
        lane=lane,
    )


class Settings:
    def __init__(self) -> None:
        api_key = os.environ.get("MATERIAL_BUILDER_SECURITY_API_KEY", "").strip()
        if not api_key:
            secret_file = (
                os.environ.get("MATERIAL_BUILDER_SECURITY_API_KEY_FILE", "").strip()
                or os.environ.get("INTERNAL_API_KEY_FILE", "").strip()
                or os.environ.get("API_KEY_FILE", "").strip()
            )
            api_key = _read_secret_file(secret_file) if secret_file else ""
        self.security = SecuritySettings(api_key=api_key)
        self.llm = LLMSettings(**llm_settings_data({}, env_prefix="MATERIAL_BUILDER"))
        self.llm_plan = _lane_llm_settings("plan", fallback=self.llm)
        self.llm_file = _lane_llm_settings("file", fallback=self.llm)
        self.llm_patch = _lane_llm_settings("patch", fallback=self.llm)
        self.llm_repair = _lane_llm_settings("repair", fallback=self.llm)
        self.llm_critic = _lane_llm_settings("critic", fallback=self.llm)

    @property
    def llm_lanes(self) -> dict[str, LLMSettings]:
        return {
            "plan": self.llm_plan,
            "file": self.llm_file,
            "patch": self.llm_patch,
            "repair": self.llm_repair,
            "critic": self.llm_critic,
        }


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
