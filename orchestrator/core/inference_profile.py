"""Inference profile resolution — maps routing keys to LLM parameters."""

from __future__ import annotations

from dataclasses import dataclass

from orchestrator.config import InferenceProfileConfig, get_settings


@dataclass
class InferenceProfile:
    """Resolved inference parameters for an LLM call."""

    key: str  # "fast", "default", "code", "deep"
    num_ctx: int
    num_predict: int
    temperature: float
    top_p: float | None = None

    @classmethod
    def from_config(cls, key: str, cfg: InferenceProfileConfig) -> "InferenceProfile":
        return cls(
            key=key,
            num_ctx=cfg.num_ctx,
            num_predict=cfg.num_predict,
            temperature=cfg.temperature,
            top_p=cfg.top_p,
        )


def resolve_profile(key: str) -> InferenceProfile:
    """Resolve a routing key to its inference profile.

    Falls back to 'default' profile if key not found.
    """
    cfg = get_settings()
    profiles = cfg.inference_profiles

    if key in profiles:
        return InferenceProfile.from_config(key, profiles[key])

    # Fallback to default
    if "default" in profiles:
        return InferenceProfile.from_config("default", profiles["default"])

    # Hardcoded fallback
    return InferenceProfile(
        key="default",
        num_ctx=4096,
        num_predict=512,
        temperature=0.3,
    )
